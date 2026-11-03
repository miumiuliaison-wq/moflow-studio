"""Restartable production pipeline, sharing the desktop's fixed executors and LangGraph saver."""
from __future__ import annotations
import asyncio, hashlib, json, math, os, shutil
from datetime import datetime,timezone
from pathlib import Path
from typing import TypedDict
from PIL import Image
from langgraph.graph import StateGraph, START, END
from .core import ROOT, Waiting, LoginRequired, ModelRequired, StoragePaused, Unsupported, now, uid, clean, redact
from .ai import Prompt
from .billing import Ledger, BudgetRequired, inference_budget
from .production_config import ProductionRequest, Preferences, preferences, settings, Binding
from .runninghub_api import RunningHubAPI, UncertainSubmission
from .recommendations import rank_candidates

STAGES=['research','reference','prompt','image','dress','motion','export','complete']
class State(TypedDict):
    batch: str
    done: bool

class Production:
    def __init__(self,engine):
        self.engine=engine;self.db=engine.db;self.ledger=Ledger(self.db);self.rh=RunningHubAPI(engine)
        self.tasks={};self.slots=asyncio.Semaphore(2);self.encoding=asyncio.Lock();self.graph=None
        with self.db.conn() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS productions(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,state TEXT,
                payload TEXT,message TEXT,created TEXT,updated TEXT);
                CREATE TABLE IF NOT EXISTS production_steps(id TEXT PRIMARY KEY,batch TEXT,stage TEXT,item INTEGER,
                payload TEXT,created TEXT);''')

    def get(self,identifier):
        rows=self.db.query('SELECT * FROM productions WHERE id=?',(identifier,))
        if not rows:raise ValueError('生产任务不存在')
        row=rows[0];row['payload']=json.loads(row['payload']);row['budget']=self.ledger.summary(identifier)
        row['cloud_tasks']=[]
        for task in self.db.query('SELECT id,app_id,state,task_id,updated FROM production_rh_tasks WHERE batch=?',(identifier,)):
            row['cloud_tasks'].append(task)
        return row

    def list(self):return [self.get(r['id']) for r in self.db.query('SELECT id FROM productions ORDER BY created DESC LIMIT 60')]

    def create(self,body:ProductionRequest,mcp=False):
        config=settings(self.db)
        if mcp:
            if not config.mcp_enabled:raise ValueError('未启用 MCP 自动生产权限')
            if body.count>config.mcp_max_count or body.ds_budget>config.mcp_ds_budget or body.rh_budget>config.mcp_rh_budget or body.rh_unit!=config.mcp_rh_unit:raise ValueError('请求超过 MCP 已启用的数量或预算规则')
        if not self.db.query('SELECT id FROM accounts WHERE id=?',(body.account,)):raise ValueError('账号不存在')
        previous=self.db.query('SELECT id,payload FROM productions WHERE request_id=?',(body.request_id,))
        if previous:
            if json.loads(previous[0]['payload'])['request']!=body.model_dump():raise ValueError('同一请求 ID 不能用于不同条件')
            return self.get(previous[0]['id'])
        for asset,kind in [(body.reference_asset,'video'),(body.clothing_asset,'image')]:
            if asset and not self.db.query('SELECT id FROM assets WHERE id=? AND kind=?',(asset,kind)):raise ValueError('参考素材不存在或类型不符')
        identifier=uid();payload={'request':body.model_dump(),'preferences':preferences(self.db).model_dump(),
            'config':config.model_dump(),'origin':'mcp' if mcp else 'desktop','current':0,'stage':'research','items':[{} for _ in range(body.count)],'research':{},'started':now()}
        # Create batch and both budgets as one transaction before the scheduler can see it.
        with self.db.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            from .billing import micros
            if mcp:
                from datetime import timedelta
                cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
                recent=[json.loads(x['payload']) for x in c.execute('SELECT payload FROM productions WHERE created>=?',(cutoff,))]
                recent=[x for x in recent if x.get('origin')=='mcp']
                if sum(micros(x['request']['ds_budget']) for x in recent)+micros(body.ds_budget)>micros(config.mcp_ds_budget) or sum(micros(x['request']['rh_budget']) for x in recent)+micros(body.rh_budget)>micros(config.mcp_rh_budget):
                    raise ValueError('MCP 24小时累计预算已达到规则上限')
            c.execute('INSERT INTO productions VALUES(?,?,?,?,?,?,?)',(identifier,body.request_id,'queued',json.dumps(payload,ensure_ascii=False),'等待执行',now(),now()))
            for provider,unit,limit in [('deepseek','USD',body.ds_budget),('runninghub',body.rh_unit,body.rh_budget)]:
                c.execute('INSERT INTO production_budgets VALUES(?,?,?,?)',(identifier,provider,unit,micros(limit)))
        self.db.event('production',identifier,'创建自动生产任务');return self.get(identifier)

    async def start(self,checkpointer):
        graph=StateGraph(State);graph.add_node('advance',self.advance);graph.add_edge(START,'advance')
        graph.add_conditional_edges('advance',lambda s:'done' if s['done'] else 'again',{'done':END,'again':'advance'})
        self.graph=graph.compile(checkpointer=checkpointer)
        self.db.execute("UPDATE productions SET state='queued',message='重启后继续查询已有任务' WHERE state='running'")
        # A crash between HTTP submission and receipt must never cause another paid POST.
        self.db.execute("UPDATE production_rh_tasks SET state='uncertain' WHERE state='submitting' AND task_id=''")
        self.tick()

    def tick(self):
        if not self.graph or self.db.get('automation_paused',False):return
        for row in self.db.query("SELECT id FROM productions WHERE state='queued' ORDER BY created LIMIT 10"):
            if row['id'] not in self.tasks:
                identifier=row['id'];self.tasks[identifier]=asyncio.create_task(self.worker(identifier))
                self.tasks[identifier].add_done_callback(lambda _,key=identifier:self.tasks.pop(key,None))

    async def close(self,resume_on_restart=True):
        pending=[key for key in self.tasks if self.get(key)['state'] in {'queued','running'}]
        tasks=list(self.tasks.values())
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if resume_on_restart:
            for key in pending:self.db.execute("UPDATE productions SET state='queued',message='下次启动继续，已提交生成只查询原任务' WHERE id=? AND state!='cancelled'",(key,))

    def check(self,identifier):
        if self.db.get('automation_paused',False) or self.get(identifier)['state'] in {'paused','cancelled'}:raise asyncio.CancelledError()

    def persist(self,identifier,payload,message='',state='running'):
        self.db.execute('UPDATE productions SET state=?,payload=?,message=?,updated=? WHERE id=?',
            (state,json.dumps(payload,ensure_ascii=False),redact(message),now(),identifier))
        self.db.event('production',identifier,message or payload['stage'])

    async def worker(self,identifier):
        async with self.slots:
            if self.get(identifier)['state']!='queued':return
            self.db.execute("UPDATE productions SET state='running',updated=? WHERE id=?",(now(),identifier))
            token=inference_budget.set(identifier)
            try:
                await self.graph.ainvoke({'batch':identifier,'done':False},{'configurable':{'thread_id':'production:'+identifier},'recursion_limit':512})
            except asyncio.CancelledError:
                if self.get(identifier)['state']!='cancelled':self.db.execute("UPDATE productions SET state='paused',message='已暂停，新生成不会提交' WHERE id=?",(identifier,))
            except Exception as error:
                state='waiting_login' if isinstance(error,LoginRequired) else 'waiting_model' if isinstance(error,ModelRequired) else 'waiting_storage' if isinstance(error,StoragePaused) else 'uncertain' if isinstance(error,UncertainSubmission) else 'waiting_budget' if isinstance(error,BudgetRequired) else 'waiting_configuration' if isinstance(error,(Waiting,Unsupported)) else 'failed'
                self.db.execute('UPDATE productions SET state=?,message=?,updated=? WHERE id=?',(state,redact(error),now(),identifier));self.db.event('production',identifier,redact(error))
            finally:inference_budget.reset(token)

    async def action(self,identifier,action):
        row=self.get(identifier)
        if action=='resume':
            if row['state'] in {'completed','running','queued','cancelled'}:return row
            self.db.execute("UPDATE productions SET state='queued',message='等待继续' WHERE id=?",(identifier,));self.tick()
        elif action in {'pause','cancel'}:
            self.db.execute('UPDATE productions SET state=?,message=? WHERE id=?',('cancelled' if action=='cancel' else 'paused','停止新提交；已提交的云端任务可能仍在执行',identifier))
            if identifier in self.tasks:self.tasks[identifier].cancel();await asyncio.gather(self.tasks[identifier],return_exceptions=True)
        else:raise ValueError('未知任务操作')
        return self.get(identifier)

    async def advance(self,state):
        identifier=state['batch'];self.check(identifier);row=self.get(identifier);p=row['payload'];r=p['request'];index=p['current'];stage=p['stage']
        if stage=='complete':return {**state,'done':True}
        self.db.check_space();item=p['items'][index]
        if stage=='research':
            if not r['ds_budget'] or not r['rh_budget']:raise BudgetRequired('请填写本批 DeepSeek（USD）与 RunningHub 预算后恢复')
            await self.preflight(p)
            research=await self.research(p);p['research']=research;p['stage']='reference'
        elif stage=='reference':
            item['reference']=await self.reference(identifier,p,index);p['stage']='prompt'
        elif stage=='prompt':
            clothing=self.asset(r['clothing_asset']) if r['clothing_asset'] else None
            reverse=await self.engine.models.reverse(clothing['path']) if clothing else None
            prompt=await self.engine.models.request('为原创成年女性舞蹈角色编写中文图像生成提示词。单人全身、头顶到双脚完整、9:16、正常光线、真实摄影。五官和发型适当变化，保持成年形象。严格遵守制作档案：'+json.dumps(p['preferences'],ensure_ascii=False)+'；本条序号：'+str(index+1)+'；参考近期检索事实，不捏造品牌或热度：'+json.dumps(p['research'].get('fashion',[]),ensure_ascii=False)[:9000]+'；服装视觉参考：'+(reverse.positive if reverse else '无，直接设计裙装')+'；补充要求：'+r['notes']+'。negative只包含以“无”开头的视觉缺陷。',Prompt)
            prompt.negative=[s if s.startswith('无') else '无'+s for s in prompt.negative]
            item['prompt']=prompt.model_dump();item['prompt_path']=self.write_prompt(identifier,index,p,prompt,reverse);p['stage']='image'
        elif stage in {'image','dress','motion'}:
            config=settings(self.db);binding=getattr(config,stage)
            if stage=='dress' and not r['clothing_asset']:p['stage']='motion'
            else:
                attempt=item.get(stage+'_attempt',0)
                if attempt>=2:raise Waiting('该阶段已达到两次生成上限，请查看失败素材')
                task_id=identifier+'-'+str(index)+'-'+stage+'-'+str(attempt)
                if stage=='image':
                    positive=item['prompt']['positive'];negative='，'.join(item['prompt']['negative'])
                    values={'positive':positive}
                    if 'negative' in binding.fields:values['negative']=negative
                    else:values['positive']+='\n负向提示词：'+negative
                    files={}
                elif stage=='dress':values={};files={'character':self.asset(item['image'])['path'],'clothing':self.asset(r['clothing_asset'])['path']}
                else:values={};files={'character':self.asset(item.get('dress') or item['image'])['path'],'video':self.asset(item['reference'])['path']}
                results=await self.rh.generate(identifier,task_id,binding,values,files,lambda:self.check(identifier))
                kind='video' if stage=='motion' else 'image';valid=[]
                for a in results:
                    asset=self.asset(a)
                    if asset['kind']!=kind:continue
                    analysis=await self.analyze(a,p,full=kind=='image')
                    if self.accepts(analysis,full=kind=='image'):valid.append(a)
                if not valid:
                    item[stage+'_attempt']=attempt+1
                    self.persist(identifier,p,'生成素材未通过检查')
                    if attempt>=1:raise Waiting('该阶段两次生成均未通过检查，请查看素材与分析记录')
                    return {**state,'done':False}
                item[stage]=valid[0];p['stage']={'image':'dress','dress':'motion','motion':'export'}[stage]
        elif stage=='export':
            item['output']=await self.export(identifier,index,item,r,lambda:self.check(identifier))
            self.write_report(identifier,index,p)
            if index+1<r['count']:p['current']=index+1;p['stage']='reference'
            else:p['stage']='complete'
        self.db.execute('INSERT INTO production_steps VALUES(?,?,?,?,?,?)',(uid(),identifier,stage,index,json.dumps({'result':item,'at':now()},ensure_ascii=False),now()))
        done=p['stage']=='complete';self.persist(identifier,p,'已完成成片归档' if done else '执行阶段：'+p['stage'],'completed' if done else 'running')
        return {**state,'done':done}

    def asset(self,identifier):
        rows=self.db.query('SELECT * FROM assets WHERE id=?',(identifier,))
        if not rows or not Path(rows[0]['path']).is_file():raise Waiting('素材文件缺失：'+identifier)
        return rows[0]

    @staticmethod
    def accepts(analysis,full=False):
        return analysis.get('people')==1 and all(analysis.get(k)=='yes' for k in ['face_visible','clear','matches']+(['full_body'] if full else ['eye_level']))

    async def analyze(self,identifier,p,full=False):
        requirements=p['preferences']['framing']+'；明确成年女性，服装符合性感甜美裙装风格。'+('必须全身，头顶和双脚完整可见。' if full else '单人舞蹈，近似平视，脸部不被遮挡。')
        original=self.asset(identifier);prior=json.loads(original['payload'])
        fingerprint=hashlib.sha256((requirements+original['sha']+self.db.settings().vision_model.model_dump_json()).encode()).hexdigest()
        if prior.get('analysis_sampling',{}).get('fingerprint')==fingerprint and prior.get('analysis'):return prior['analysis']
        result=await self.engine.analyze_asset(identifier,requirements)
        row=self.asset(identifier);payload=json.loads(row['payload']);payload['analysis_sampling']={'fingerprint':fingerprint,'method':'single_image' if row['kind']=='image' else '5_uniform_frames','timestamps':[]}
        if row['kind']=='video':
            probe=await self.engine.probe(row['path']);duration=float(probe['format']['duration']);payload['analysis_sampling']['timestamps']=[round(duration*(i+.5)/5,3) for i in range(5)]
        self.db.execute('UPDATE assets SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),identifier));return result

    async def preflight(self,p):
        # Only non-billable reads: do not spend on analysis before generation is configured.
        self.rh.key()
        config=settings(self.db);r=p['request']
        for role in ('text','vision'):
            model=getattr(self.db.settings(),role+'_model')
            if not model.model:raise ModelRequired('请先配置文本与视觉模型')
            if model.provider=='deepseek' and role=='vision' and model.model!='deepseek-flash':raise ModelRequired('当前视觉配置请选择 deepseek-flash')
            if model.model not in await self.engine.models.list(role):raise ModelRequired('所选模型不在服务可用列表：'+model.model)
        await self.rh.check()
        for stage,required in [('image',{'positive'}),('motion',{'character','video'})]+([('dress',{'character','clothing'})] if r['clothing_asset'] else []):
            binding=getattr(config,stage);schema=await self.rh.schema(binding.app_id)
            keys={n['key'] for n in schema['nodes']}
            if binding.schema_hash!=schema['hash'] or not required.issubset(binding.fields) or any(v not in keys for v in binding.fields.values()) or any(k not in keys for k in binding.defaults):
                raise Waiting('请先完成应用字段映射并保存：'+binding.app_id)
            for n in schema['nodes']:
                if n['type'] not in {'INT','FLOAT','BOOLEAN','STRING','IMAGE','VIDEO','AUDIO','LIST'}:raise Waiting('暂不支持字段类型：'+n['type'])
                if n['type'] in {'IMAGE','VIDEO','AUDIO'} and n['key'] not in binding.fields.values():raise Waiting('请显式映射媒体字段：'+n['label'])
            price=self.rh.price(binding.app_id,schema['hash'])
            if not price['verified']:raise BudgetRequired(price['message'])
            if price['unit']!=r['rh_unit']:raise BudgetRequired('应用计费单位与本批 RunningHub 预算不符')

    async def research(self,p):
        pref=p['preferences'];account=p['request']['account'];dance=[]
        if not p['request']['reference_asset']:
            dance=rank_candidates(await self.engine.platform.recommendation_search(pref['dance_keyword'],account))
            if not dance:raise Waiting('实际搜索没有近7天且互动量达标的舞蹈候选，请稍后重试或选择已有视频')
        fashion=[]
        for keyword in pref['fashion_keywords'][:2]:
            rows=await self.engine.platform.search(keyword,account,count=12)
            cutoff=datetime.now(timezone.utc).timestamp()-7*86400
            for row in rows:
                if not row.get('published') or row['published']<cutoff:continue
                fashion.append({'keyword':keyword,'title':row.get('title',''),'source':row.get('source') or 'https://www.douyin.com/video/'+str(row['id']),'published':row['published'],'stats':row.get('stats',{}),'retrieved':now()})
        # Store metadata only; playback URLs expire and are refreshed from the source later.
        return {'at':now(),'dance':[{k:v for k,v in x.items() if k not in {'urls','images'}} for x in dance],
                'fashion':fashion,'note':'实际检索候选；穿搭不足时依据现有档案设计，不声称为平台热门榜单。'}

    async def reference(self,identifier,p,index):
        r=p['request'];candidates=[{'asset':r['reference_asset']}] if r['reference_asset'] else p['research']['dance'][:15]
        used={x.get('reference') for x in p['items'][:index]}
        for candidate in candidates:
            self.check(identifier)
            if candidate.get('asset'):assets=[candidate['asset']]
            else:
                job_id=hashlib.sha256((identifier+':reference:'+str(index)+':'+candidate['id']).encode()).hexdigest()
                job=self.db.job('download',{'account':r['account'],'input':candidate['id']},job_id)
                old=self.db.query('SELECT state,payload FROM jobs WHERE id=?',(job,))[0]
                if old['state']=='completed':assets=json.loads(old['payload']).get('asset_ids',[])
                else:
                    self.db.update_job(job,'running','生产任务下载参考')
                    try:assets=await self.engine.download({'account':r['account'],'input':candidate['id']},job)
                    except (LoginRequired,StoragePaused):raise
                    except Exception as error:self.db.update_job(job,'failed',redact(error));continue
            for a in assets:
                asset=self.asset(a)
                if asset['kind']!='video' or a in used:continue
                probe=await self.engine.probe(asset['path'],True);stream=next(s for s in probe['streams'] if s['codec_type']=='video');duration=float(probe['format']['duration'])
                if not 8<=duration<=20 or min(stream['width'],stream['height'])<720:continue
                if self.accepts(await self.analyze(a,p)):return a
        raise Waiting('候选视频未通过时长、尺寸或抽帧画面检查；没有用不合格素材填充')

    def write_prompt(self,identifier,index,p,prompt,reverse):
        folder=Path(self.db.settings().storage_root)/'提示词'/'墨流'/'生产'/identifier;folder.mkdir(parents=True,exist_ok=True)
        text='# 角色图像提示词\n\n## 正向提示词\n\n'+prompt.positive+'\n\n## 负向提示词\n\n'+'，'.join(prompt.negative)+'\n\n## 记录\n\n'+json.dumps({'模型':self.db.settings().text_model.model,'时间':now(),'制作档案版本':p['preferences']['version'],'服装参考':p['request']['clothing_asset'],'视觉逆向说明':'视觉描述，不是原始提示词恢复','不确定项':reverse.uncertainties if reverse else []},ensure_ascii=False,indent=2)
        self.db.check_space(len(text.encode()));path=folder/(str(index+1)+'.md');path.write_text(text,encoding='utf-8');return str(path)

    async def command(self,args,timeout=300):
        process=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE,creationflags=0x08000000 if os.name=='nt' else 0)
        try:
            _,err=await asyncio.wait_for(process.communicate(),timeout)
            if process.returncode:raise ValueError('FFmpeg 处理失败：'+err.decode(errors='replace')[-300:])
        except BaseException:
            if process.returncode is None:process.kill();await process.wait()
            raise

    async def export(self,identifier,index,item,r,check):
        from .engine import tool_path
        async with self.encoding:
            check();video=self.asset(item['motion']);reference=self.asset(item['reference']);probe=await self.engine.probe(video['path'],True)
            stream=next(s for s in probe['streams'] if s['codec_type']=='video');duration=min(r['duration'],float(probe['format']['duration']))
            if duration<8:raise Waiting('生成片段不足8秒')
            # Fit into a 9:16 canvas without upscaling or cropping the subject.
            h=math.ceil(min(stream['height'],stream['width']*16/9)/32)*32;w=h*9//16
            if w>stream['width'] or h>stream['height']:
                # Letterbox can add canvas, but never increase source picture pixels.
                h=math.ceil(stream['height']/32)*32;w=h*9//16
                if w<stream['width']:h=math.ceil(stream['width']*16/9/32)*32;w=h*9//16
            folder=Path(self.db.settings().storage_root)/'视频'/'墨流'/'成片'/identifier;folder.mkdir(parents=True,exist_ok=True)
            dest=folder/(str(index+1)+'.mp4');part=folder/(str(index+1)+'.part.mp4')
            self.db.check_space(max(16*1024**2,Path(video['path']).stat().st_size*2))
            args=[tool_path('ffmpeg'),'-y','-v','error','-i',video['path']]
            refprobe=await self.engine.probe(reference['path']);audio=any(s.get('codec_type')=='audio' for s in refprobe['streams'])
            if audio:args+=['-i',reference['path']]
            args+=['-map','0:v:0']+(['-map','1:a:0','-c:a','aac','-b:a','192k'] if audio else ['-an'])
            args+=['-vf',fr'scale=w=min(iw\,{w}):h=min(ih\,{h}):force_original_aspect_ratio=decrease:force_divisible_by=2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1',
                   '-t',str(duration),'-c:v','libx264','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',str(part)]
            try:
                await self.command(args);check();await self.engine.probe(part,True);self.db.check_space();part.replace(dest)
            except BaseException:part.unlink(missing_ok=True);raise
            with dest.open('rb') as data:digest=hashlib.file_digest(data,'sha256').hexdigest()
            found=self.db.query('SELECT id FROM assets WHERE sha=?',(digest,))
            if found:return found[0]['id']
            asset_id=uid();payload={'provider':'production','batch':identifier,'reference':item['reference'],'generated':item['motion'],
                'probe':await self.engine.probe(dest),'media':{'title':'舞蹈成片 '+str(index+1),'author':'墨流'}}
            self.db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',(asset_id,'production:'+identifier+':'+str(index),'video',str(dest),digest,dest.stat().st_size,now(),0,json.dumps(payload,ensure_ascii=False)))
            return asset_id

    def write_report(self,identifier,index,p):
        folder=Path(self.db.settings().storage_root)/'pipeline'/'runs'/'墨流生产'/identifier;folder.mkdir(parents=True,exist_ok=True)
        tasks=self.db.query('SELECT id,app_id,state,task_id FROM production_rh_tasks WHERE batch=?',(identifier,))
        report={'batch':identifier,'at':now(),'item':index,'preferences':p['preferences'],'research':p['research'],'result':p['items'][index],'tasks':tasks,'budget':self.ledger.summary(identifier),
            'limits':'视频分析基于5个均匀抽帧，不能保证帧间没有遮脸或其他缺陷；最终视频已完整解码检查。'}
        content=json.dumps(report,ensure_ascii=False,indent=2);self.db.check_space(len(content.encode()));(folder/(str(index+1)+'.json')).write_text(content,encoding='utf-8')
