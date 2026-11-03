from __future__ import annotations
import asyncio,hashlib,json,os,re,shutil,subprocess,sys,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from typing import TypedDict,Any
import httpx
from PIL import Image
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from langgraph.graph import StateGraph,START,END
from langgraph.types import interrupt,Command
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from .core import ROOT,Store,Waiting,LoginRequired,ModelRequired,StoragePaused,Unsupported,now,uid,clean,words
from .platform import Douyin
from .ai import Models,Plan,Draft
from .runninghub import RunningHub
from .recommendations import Recommendations
from .production import Production
from .library import Library
from .chat import Chat
from .studio import Studio

def tool_path(name):
    bundled=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent.parent))/'media-tools'/(name+'.exe')
    return str(bundled) if bundled.exists() else shutil.which(name)

class RunState(TypedDict,total=False):
    run_id:str
    plan:dict
    index:int
    items:list[dict]
    assets:list[str]
    results:list[dict]
    rule_id:str
    message:str

class Engine:
    def __init__(self,db:Store):
        self.db=db;self.platform=Douyin(db);self.models=Models(db);self.active={};self.sem=asyncio.Semaphore(db.settings().concurrent_downloads);self.scheduler=AsyncIOScheduler();self.graph=None;self.checkpointer_cm=None
        self.runninghub=RunningHub(self)
        self.recommendations=Recommendations(db,self.platform)
        self.production=Production(self)
        self.library=Library(self)
        self.chat=Chat(self)
        self.studio=Studio(self)
    async def start(self):
        os.environ['LANGCHAIN_TRACING_V2']='false';os.environ['LANGSMITH_TRACING']='false'
        self.chat.recover()
        self.checkpointer_cm=AsyncSqliteSaver.from_conn_string(str(ROOT/'checkpoints.sqlite3'));checkpointer=await self.checkpointer_cm.__aenter__()
        builder=StateGraph(RunState);builder.add_node('execute',self.run_step);builder.add_edge(START,'execute');builder.add_conditional_edges('execute',lambda s: 'again' if s['index']<len(s['plan']['steps']) else 'done',{'again':'execute','done':END});self.graph=builder.compile(checkpointer=checkpointer)
        await self.production.start(checkpointer)
        self.db.execute("UPDATE jobs SET state='queued',message='重启后恢复任务' WHERE state='running'")
        self.db.execute("UPDATE actions SET state='uncertain' WHERE state='executing'")
        interrupted_runs=self.db.query("SELECT id FROM runs WHERE state IN ('running','queued')")
        self.db.execute("UPDATE runs SET state='paused' WHERE state IN ('running','queued')")
        self.scheduler.add_job(self.tick,'interval',seconds=5,max_instances=1,coalesce=True,misfire_grace_time=30);self.scheduler.start()
        await self.tick()
        if not self.db.get('automation_paused',False):
            for run in interrupted_runs:self.spawn('run:'+run['id'],self.execute_run(run['id'],True))
    async def stop(self):
        self.scheduler.shutdown(wait=False)
        await self.chat.close()
        await self.library.close()
        await self.production.close()
        tasks=list(self.active.values())
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True);await self.recommendations.close();await self.platform.close();await self.runninghub.close()
        if self.checkpointer_cm:await self.checkpointer_cm.__aexit__(None,None,None)
    def spawn(self,key,coro):
        if key in self.active:return
        task=asyncio.create_task(coro);self.active[key]=task
        def done(t):
            self.active.pop(key,None)
            if not t.cancelled() and t.exception():self.db.event('error',key,str(t.exception()))
        task.add_done_callback(done)
    async def tick(self):
        self.production.tick()
        connected=set()
        for account in self.db.list('accounts'):
            if account['id'] in self.platform.contexts:
                state=await self.platform.status(account['id'])
                if state['state']=='connected':
                    connected.add(account['id'])
                    for j in self.db.list('jobs',1000):
                        if j['state']=='waiting_login' and j['payload'].get('account','default')==account['id']:self.db.update_job(j['id'],'queued','已检测到登录，继续任务')
        for row in self.db.query("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 10"):
            if len(self.active)>6:break
            self.spawn(row['id'],self.job_worker(row['id']))
        if self.db.get('automation_paused',False):return
        for row in self.db.query("SELECT id,payload FROM runs WHERE state='waiting_login'"):
            if json.loads(row['payload'])['plan']['account'] in connected and 'run:'+row['id'] not in self.active:
                self.spawn('run:'+row['id'],self.execute_run(row['id'],True))
        for rule in self.db.list('rules'):
            cfg=rule['payload']
            if rule['enabled'] and time.time()-cfg.get('last_trigger',0)>=cfg.get('interval_seconds',300):
                cfg['last_trigger']=time.time();self.db.execute('UPDATE rules SET payload=? WHERE id=?',(json.dumps(cfg,ensure_ascii=False),rule['id']))
                self.spawn('rule:'+rule['id'],self.rule_tick(rule['id']))
    async def job_worker(self,identifier):
        row=self.db.query('SELECT * FROM jobs WHERE id=?',(identifier,))[0];payload=json.loads(row['payload']);self.db.update_job(identifier,'running','正在执行')
        try:
            if row['kind']=='download':await self.download(payload,identifier)
            elif row['kind']=='reverse':await self.reverse_asset(payload['asset_id'],payload.get('rewrite',False),identifier)
            elif row['kind']=='analyze':await self.analyze_asset(payload['asset_id'],payload.get('requirements',''),identifier)
            elif row['kind']=='command':
                plan=await self.models.plan(payload['text'],payload.get('account','default'));run=await self.create_run(plan);payload['run_id']=run;payload['plan']=plan.model_dump();self.db.update_job(identifier,'completed','计划已创建，可在AI工作流中查看执行步骤',1,payload)
            else:raise ValueError('未知任务类型')
        except asyncio.CancelledError:raise
        except Waiting as error:
            state='waiting_login' if isinstance(error,LoginRequired) else 'waiting_model' if isinstance(error,ModelRequired) else 'waiting_storage'
            self.db.update_job(identifier,state,str(error));self.db.event(state,identifier,str(error))
        except Exception as error:self.db.update_job(identifier,'failed',str(error));self.db.event('error',identifier,str(error))
    def check_job(self,identifier):
        state=self.db.query('SELECT state FROM jobs WHERE id=?',(identifier,))[0]['state']
        if state in {'paused','cancelled'}:raise asyncio.CancelledError()
    async def transfer(self,url,path,account,identifier):
        from urllib.parse import urlparse
        import ipaddress,socket
        def validate(destination):
            u=urlparse(destination)
            if u.scheme not in {'https','http'} or u.username or u.password:raise ValueError('无效媒体地址')
            host=u.hostname or ''
            domains=('douyin.com','douyinvod.com','douyinpic.com','byteimg.com','ibytedtos.com','bytecdn.cn','douyinstatic.com','pstatp.com','snssdk.com','amemv.com','ixigua.com','bytedance.com','bytednsdoc.com')
            if not any(host==d or host.endswith('.'+d) for d in domains):raise ValueError('媒体地址不在抖音CDN范围内：'+host)
            for result in socket.getaddrinfo(host,u.port or 443,type=socket.SOCK_STREAM):
                if not ipaddress.ip_address(result[4][0]).is_global:raise ValueError('媒体地址指向非公网地址')
        await asyncio.to_thread(validate,url)
        offset=path.stat().st_size if path.exists() else 0
        headers={'Referer':'https://www.douyin.com/','User-Agent':'Mozilla/5.0'}
        if offset:headers['Range']=f'bytes={offset}-'
        async with httpx.AsyncClient(timeout=httpx.Timeout(60,connect=15),trust_env=False,follow_redirects=False) as client:
            for _ in range(5):
                async with client.stream('GET',url,headers=headers) as response:
                    if response.is_redirect:
                        from urllib.parse import urljoin
                        url=urljoin(url,response.headers['location']);await asyncio.to_thread(validate,url);continue
                    if response.status_code==416 and offset:path.unlink(missing_ok=True);headers.pop('Range',None);offset=0;continue
                    response.raise_for_status()
                    if response.status_code==206:
                        content_range=response.headers.get('content-range','')
                        if not content_range.startswith(f'bytes {offset}-'):raise ValueError('下载断点与服务器范围不一致')
                    else:offset=0
                    expected=int(response.headers.get('content-length','0'));self.db.check_space(expected)
                    received=offset
                    with path.open('ab' if offset else 'wb') as output:
                        async for chunk in response.aiter_bytes(256*1024):
                            self.check_job(identifier);self.db.check_space(len(chunk));output.write(chunk);received+=len(chunk)
                            self.db.update_job(identifier,'running',f'已接收 {received/1048576:.1f} MB',min(.95,received/max(1,expected+offset)))
                    if expected and received-offset!=expected:raise ValueError('下载长度不完整')
                    return
            raise ValueError('媒体跳转次数过多')
    async def probe(self,path,full=False):
        binary=tool_path('ffprobe')
        if not binary:raise Unsupported('缺少 FFprobe，不能确认下载文件是否有效')
        proc=await asyncio.create_subprocess_exec(binary,'-v','error','-show_format','-show_streams','-of','json',str(path),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,creationflags=0x08000000 if os.name=='nt' else 0)
        out,err=await asyncio.wait_for(proc.communicate(),60)
        if proc.returncode:raise ValueError('视频格式校验失败：'+err.decode(errors='replace')[:200])
        data=json.loads(out)
        if not any(s.get('codec_type')=='video' for s in data['streams']):raise ValueError('媒体文件不含视频画面')
        if full:
            proc=await asyncio.create_subprocess_exec(tool_path('ffmpeg'),'-v','error','-xerror','-i',str(path),'-f','null','-',stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE,creationflags=0x08000000 if os.name=='nt' else 0)
            _,err=await asyncio.wait_for(proc.communicate(),300)
            if proc.returncode:raise ValueError('视频解码校验失败')
        return data
    async def download(self,payload,identifier):
        async with self.sem:
            account=payload.get('account','default');source=payload.get('input') or payload.get('media',{}).get('id')
            media=payload.get('media') or await self.platform.detail(source,account)
            existing=self.db.query('SELECT id FROM assets WHERE source_id=? AND imported=0',(media['id'],))
            if existing:
                self.db.update_job(identifier,'completed','作品已在资料库，已跳过重复下载',1,{**payload,'asset_ids':[x['id'] for x in existing]});return [x['id'] for x in existing]
            settings=self.db.settings();parts=[]
            if settings.download_video and media.get('urls'):parts.append(('video',media['urls']))
            if settings.download_images:parts.extend(('image',[u]) for u in media.get('images',[]))
            if not parts:raise ValueError('当前作品没有启用类型的可下载媒体')
            assets=[];temporary=ROOT/'tmp';temporary.mkdir(exist_ok=True)
            for index,(kind,urls) in enumerate(parts):
                path=temporary/f'{identifier}-{index}.part';error=None
                for attempt in range(3):
                    try:await self.transfer(urls[attempt%len(urls)],path,account,identifier);error=None;break
                    except httpx.HTTPStatusError as e:
                        error=e
                        if e.response.status_code in {401,403,404,410}:
                            refreshed=await self.platform.detail(media['id'],account,force=True)
                            urls=refreshed.get('urls',[]) if kind=='video' else [refreshed.get('images',[])[index-(1 if media.get('urls') else 0)]]
                    except (httpx.HTTPError,ValueError) as e:error=e
                    await asyncio.sleep(attempt+1)
                if error:raise error
                self.check_job(identifier)
                if kind=='video':metadata=await self.probe(path,True);suffix='.mp4'
                else:
                    with Image.open(path) as im:im.verify()
                    with Image.open(path) as im:metadata={'width':im.width,'height':im.height};suffix='.'+('jpg' if im.format=='JPEG' else im.format.lower())
                def hash_file():
                    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
                digest=await asyncio.to_thread(hash_file)
                duplicates=self.db.query('SELECT id FROM assets WHERE sha=?',(digest,))
                if duplicates:path.unlink(missing_ok=True);assets.append(duplicates[0]['id']);continue
                date=datetime.now().strftime('%Y-%m-%d');folder=Path(settings.storage_root)/('视频' if kind=='video' else '图片')/'墨流'/date/clean(media.get('author') or '抖音');folder.mkdir(parents=True,exist_ok=True)
                filename=clean(settings.filename_template.format(author=media.get('author',''),title=media.get('title','')[:45],id=media['id']))+f'_{index}'+suffix
                dest=folder/filename
                if dest.exists():dest=folder/(dest.stem+'_'+identifier[:8]+suffix)
                shutil.move(str(path),str(dest));asset_id=uid();size=dest.stat().st_size
                self.db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',(asset_id,media['id'],kind,str(dest),digest,size,now(),0,json.dumps({'media':media,'probe':metadata},ensure_ascii=False)));assets.append(asset_id)
                if settings.save_metadata:dest.with_suffix(dest.suffix+'.json').write_text(json.dumps({k:v for k,v in media.items() if k not in {'urls','images'}},ensure_ascii=False,indent=2),encoding='utf-8')
                self.db.event('download',asset_id,'下载完成：'+media.get('title',media['id']))
            self.db.update_job(identifier,'completed','已保存到素材库',1,{**payload,'asset_ids':assets});return assets
    async def frames(self,asset):
        path=Path(asset['path'])
        if asset['kind']=='image':return [str(path)]
        probe=await self.probe(path);duration=float(probe['format'].get('duration',1));folder=ROOT/'tmp'/uid();folder.mkdir(parents=True)
        for i in range(5):
            proc=await asyncio.create_subprocess_exec(tool_path('ffmpeg'),'-v','error','-ss',str(duration*(i+.5)/5),'-i',str(path),'-frames:v','1','-vf','scale=1280:1280:force_original_aspect_ratio=decrease',str(folder/f'{i}.jpg'),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE,creationflags=0x08000000 if os.name=='nt' else 0)
            await asyncio.wait_for(proc.communicate(),30)
        return [str(p) for p in folder.glob('*.jpg')]
    async def analyze_asset(self,asset_id,requirements='',job=None):
        asset=self.db.query('SELECT * FROM assets WHERE id=?',(asset_id,))[0];paths=await self.frames(asset)
        try:
            if not paths:raise ValueError('没有可分析画面')
            result=await self.models.analyze(paths,requirements);payload=json.loads(asset['payload']);payload['analysis']=result.model_dump();self.db.execute('UPDATE assets SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),asset_id))
            if job:self.db.update_job(job,'completed','分析完成',1)
            return result.model_dump()
        finally:
            if asset['kind']=='video' and paths:shutil.rmtree(Path(paths[0]).parent,ignore_errors=True)
    async def reverse_asset(self,asset_id,rewrite=False,job=None):
        asset=self.db.query('SELECT * FROM assets WHERE id=?',(asset_id,))[0]
        if asset['kind']!='image':raise ValueError('请选择图片进行提示词逆向')
        result=await self.models.reverse(asset['path'],rewrite);settings=self.db.settings();folder=Path(settings.storage_root)/'提示词'/'墨流';folder.mkdir(parents=True,exist_ok=True);dest=folder/(clean(Path(asset['path']).stem)+'_'+asset_id[:6]+'.md')
        dest.write_text('# 图像提示词\n\n## 正向提示词\n\n'+result.positive+'\n\n## 负向提示词\n\n'+'，'.join(result.negative)+'\n\n## 记录\n\n- 来源：'+asset['path']+'\n- 模型：'+settings.vision_model.model+'\n- 时间：'+now()+'\n- 说明：视觉描述，不是原始提示词恢复。\n'+''.join('- 不确定：'+x+'\n' for x in result.uncertainties),encoding='utf-8')
        payload=json.loads(asset['payload']);payload['prompt']={'path':str(dest),**result.model_dump()};self.db.execute('UPDATE assets SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),asset_id))
        if job:self.db.update_job(job,'completed','提示词已保存为 Markdown',1)
        return payload['prompt']
    async def create_run(self,plan:Plan,rule_id=''):
        run=uid();stamp=now();self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',(run,rule_id,'queued',json.dumps({'plan':plan.model_dump(),'index':0,'results':[]},ensure_ascii=False),stamp,stamp));self.spawn('run:'+run,self.execute_run(run,False));return run
    async def execute_run(self,run,resume=False):
        row=self.db.query('SELECT * FROM runs WHERE id=?',(run,))[0];payload=json.loads(row['payload']);self.db.execute('UPDATE runs SET state=?,updated=? WHERE id=?',('running',now(),run))
        try:
            config={'configurable':{'thread_id':run},'recursion_limit':100}
            existing=await self.graph.aget_state(config)
            initial=Command(resume=True) if resume and existing.tasks else (None if existing.values else {'run_id':run,'plan':payload['plan'],'index':0,'items':[],'assets':[],'results':[],'rule_id':row['rule_id']})
            output=await self.graph.ainvoke(initial,config)
            interrupted=bool(output.get('__interrupt__'))
            login_wait=any(isinstance(i.value,dict) and i.value.get('reason')=='waiting_login' for i in output.get('__interrupt__',()))
            state='waiting_login' if login_wait else 'paused' if interrupted else 'completed'
            self.db.execute('UPDATE runs SET state=?,payload=?,updated=? WHERE id=?',(state,json.dumps(output,ensure_ascii=False,default=str),now(),run));self.db.event('workflow',run,'流程已暂停' if interrupted else '流程执行完成')
        except asyncio.CancelledError:self.db.execute('UPDATE runs SET state=? WHERE id=?',('paused',run));raise
        except Exception as e:self.db.execute('UPDATE runs SET state=?,updated=? WHERE id=?',('failed',now(),run));self.db.event('error',run,str(e))
    async def run_step(self,state:RunState):
        if self.db.get('automation_paused',False):interrupt('全部自动化已暂停')
        step=state['plan']['steps'][state['index']];account=state['plan']['account'];items=state.get('items',[]);assets=state.get('assets',[])
        try:
            tool=step['tool'];result={}
            if tool=='search':items=await self.platform.search(step['keyword'],account,count=step['count']);result={'found':len(items)}
            elif tool=='author':items=await self.platform.listing('user',account,step['target'],step['count']);result={'found':len(items)}
            elif tool=='download':
                assets=[]
                for item in items[:step['count']]:
                    stable_id=hashlib.sha256((state.get('run_id','')+':'+str(state['index'])+':'+item['id']).encode()).hexdigest()
                    j=self.db.job('download',{'account':account,'input':item['id']},stable_id)
                    prior=self.db.query('SELECT * FROM jobs WHERE id=?',(j,))[0]
                    if prior['state']=='completed':assets.extend(json.loads(prior['payload']).get('asset_ids',[]));continue
                    self.db.update_job(j,'running','工作流下载')
                    try:assets.extend(await self.download({'account':account,'input':item['id']},j))
                    except asyncio.CancelledError:self.db.update_job(j,'paused','工作流已暂停');raise
                    except Waiting as error:self.db.update_job(j,'paused',str(error));raise
                result={'assets':assets,'actual':len(assets),'requested':step['count']}
            elif tool=='analyze':result={'analysis':[await self.analyze_asset(a,step['requirements']) for a in assets]}
            elif tool=='reverse':result={'prompts':[await self.reverse_asset(a) for a in assets]}
            elif tool=='inbox':items=await self.platform.read_social('messages',account,step['target']);result={'messages':items}
            elif tool=='draft':result=(await self.models.request('总结以下消息并生成回复草稿，不发送。用户要求：'+step['requirements']+'\n消息数据：'+json.dumps(items,ensure_ascii=False),Draft)).model_dump()
            elif tool=='interact':result=await self.perform_action(account,step['action'],step['target'],step['text'],state.get('rule_id',''),False,state.get('run_id','')+':'+str(state['index']),step.get('asset_id',''),step.get('comment_id',''))
            return {**state,'index':state['index']+1,'items':items,'assets':assets,'results':state.get('results',[])+[{'step':step,'result':result,'at':now()}]}
        except LoginRequired as e:
            interrupt({'reason':'waiting_login','account':account,'message':str(e)});return state
        except Waiting as e:
            interrupt(str(e));return state
        except Unsupported as e:
            interrupt('需要处理：'+str(e));return state
    async def perform_action(self,account,action,target,text,rule_id='',manual=False,request_id='',asset_id='',comment_id=''):
        if not target:raise ValueError('必须明确目标对象')
        identifier=hashlib.sha256((account+'|'+action+'|'+target+'|'+request_id).encode()).hexdigest()
        old=self.db.query('SELECT * FROM actions WHERE id=?',(identifier,))
        if old:return {'state':old[0]['state'],'replayed':True}
        if not manual:
            if self.db.get('automation_paused',False):raise Waiting('全部自动化已暂停')
            rules=self.db.query('SELECT * FROM rules WHERE id=? AND enabled=1',(rule_id,))
            cfg=json.loads(rules[0]['payload']) if rules else {}
            if cfg.get('account')!=account or action not in cfg.get('allowed_actions',[]) or target not in cfg.get('targets',[]):return {'state':'draft','message':'未授权的账号动作已保留为草稿','target':target,'text':text,'action':action}
            day=datetime.now(timezone.utc).date().isoformat();count=self.db.query("SELECT COUNT(*) n FROM actions WHERE rule_id=? AND created>=? AND state IN ('confirmed','executing','uncertain','already_applied')",(rule_id,day))[0]['n']
            if count>=cfg.get('daily_limit',0):raise Waiting('该规则今日账号动作额度已用完')
        self.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?)',(identifier,rule_id,account,target,action,'executing',json.dumps({'text':text},ensure_ascii=False),now(),now()))
        try:
            file_path=None
            if action in {'send_image','send_video'}:
                assets=self.db.query('SELECT * FROM assets WHERE id=?',(asset_id,))
                if not assets:raise ValueError('附件必须是资料库中的素材')
                asset=assets[0];expected='image' if action=='send_image' else 'video'
                if asset['kind']!=expected:raise ValueError('附件类型不匹配')
                limit=(5 if expected=='image' else 100)*1024**2
                if asset['bytes']>limit:raise ValueError('图片最多5MB，视频最多100MB')
                file_path=asset['path']
                if not Path(file_path).is_file():raise ValueError('附件文件已不存在')
            if file_path or comment_id:result=await self.platform.action(action,account,target,text,file_path=file_path,comment_id=comment_id)
            else:result=await self.platform.action(action,account,target,text)
            self.db.execute('UPDATE actions SET state=?,updated=? WHERE id=?',(result['state'],now(),identifier));self.db.event('account_action',identifier,action+'：'+result['state']);return result
        except BaseException:
            self.db.execute('UPDATE actions SET state=?,updated=? WHERE id=?',('uncertain',now(),identifier));raise
    async def rule_tick(self,rule_id):
        rule=self.db.query('SELECT * FROM rules WHERE id=?',(rule_id,))[0];cfg=json.loads(rule['payload']);account=cfg.get('account','default')
        try:
            if cfg.get('template')=='author':
                current=await self.platform.listing('user',account,cfg['author_id']);ids=[x['id'] for x in current]
                if 'baseline' not in cfg:cfg['baseline']=ids;self.db.event('monitor',rule_id,'首次已建立作者基线，不下载历史作品')
                else:
                    for media in current:
                        if media['id'] not in cfg['baseline']:self.db.job('download',{'account':account,'input':media['id']})
                    cfg['baseline']=list(dict.fromkeys(ids+cfg['baseline']))[:1000]
                self.db.execute('UPDATE rules SET payload=? WHERE id=?',(json.dumps(cfg,ensure_ascii=False),rule_id))
            elif cfg.get('template') in {'reply','shared'}:
                for target in cfg.get('targets',[]):
                    messages=await self.platform.read_social('messages',account,target);seen=cfg.get('seen',[])
                    fresh=[m for m in messages if m['id'] not in seen and m.get('direction')=='incoming']
                    if 'seen' in cfg:
                        for msg in fresh:
                            if cfg['template']=='shared':
                                for url in re.findall(r'https://(?:www\.|v\.)?douyin\.com/[^\s]+',msg['text']):self.db.job('download',{'account':account,'input':url})
                            elif cfg.get('instructions'):
                                draft=await self.models.request('根据规则生成回复：'+cfg['instructions']+'\n待分析消息：'+msg['text'],Draft)
                                await self.perform_action(account,'message',target,draft.reply,rule_id,False,msg['id'])
                    cfg['seen']=list(dict.fromkeys([m['id'] for m in messages]+seen))[:1000]
                self.db.execute('UPDATE rules SET payload=? WHERE id=?',(json.dumps(cfg,ensure_ascii=False),rule_id))
            else:await self.create_run(Plan.model_validate(cfg['plan']),rule_id)
        except Exception as error:self.db.event('monitor',rule_id,str(error))
    async def import_folder(self,folder):
        count=await self.library.import_folder(folder)
        self.db.event('import','',f'已建立 {count} 个现有素材索引，原文件未移动');return count
