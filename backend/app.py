from __future__ import annotations
import asyncio,hashlib,json,os,secrets,shutil,sys,time
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path
from typing import Any
from fastapi import FastAPI,HTTPException,Request,UploadFile,File
from fastapi.responses import FileResponse,JSONResponse,StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,ConfigDict
from .core import ROOT,Store,SecretVault,Settings,Waiting,Unsupported,now,uid,redact
from .engine import Engine
from .ai import Plan,Prompt,Draft

db=Store();engine=Engine(db);vault=SecretVault()
SESSION=secrets.token_urlsafe(32)
if not vault.get('mcp'):vault.set('mcp',secrets.token_urlsafe(32))
MCP_TOKEN=vault.get('mcp')
DIST=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent.parent))/'dist'

@asynccontextmanager
async def lifespan(app):
    from .mcp_api import manager
    async with manager.run():
        await engine.start()
        try:yield
        finally:await engine.stop()

app=FastAPI(title='墨流 Moflow Studio',version='0.1.0',lifespan=lifespan)
from .rh_api import router as runninghub_router
app.include_router(runninghub_router(engine))
from .production_api import router as production_router
app.include_router(production_router(engine))
from .chat_api import router as chat_router
app.include_router(chat_router(engine))
from .studio_api import router as studio_router
app.include_router(studio_router(engine))

@app.middleware('http')
async def local_only(request:Request,call_next):
    host=request.url.hostname
    if host not in {'127.0.0.1','localhost','testserver'}:return JSONResponse({'detail':'仅接受本机请求'},403)
    origin=request.headers.get('origin')
    if origin and origin not in {'http://127.0.0.1:7861','http://localhost:7861','http://127.0.0.1:5178','http://localhost:5178','http://tauri.localhost','tauri://localhost'}:return JSONResponse({'detail':'跨站请求已拒绝'},403)
    is_mcp=request.url.path.startswith('/mcp')
    if is_mcp and request.headers.get('authorization')!='Bearer '+MCP_TOKEN:return JSONResponse({'detail':'MCP 需要 Bearer token'},401)
    control=os.environ.get('MOFLOW_CONTROL_TOKEN','')
    trusted_control=bool(control and request.headers.get('x-moflow-control')==control)
    if request.method not in {'GET','HEAD','OPTIONS'} and not is_mcp and not trusted_control and request.cookies.get('moflow_session')!=SESSION:return JSONResponse({'detail':'请刷新墨流页面建立会话'},401)
    response=await call_next(request)
    if request.method=='GET' and not is_mcp and not request.url.path.startswith('/api/'):
        response.set_cookie('moflow_session',SESSION,httponly=True,samesite='strict')
    response.headers['X-Content-Type-Options']='nosniff';response.headers['Referrer-Policy']='no-referrer'
    return response

@app.exception_handler(Waiting)
async def waiting_handler(request,error):return JSONResponse({'detail':str(error),'state':'waiting'},409)
@app.exception_handler(Unsupported)
async def unsupported_handler(request,error):return JSONResponse({'detail':str(error),'state':'unverified'},422)
@app.exception_handler(ValueError)
async def value_handler(request,error):return JSONResponse({'detail':str(error)},400)
@app.exception_handler(Exception)
async def operation_error(request,error):
    db.event('error',request.url.path,str(error))
    return JSONResponse({'detail':'操作失败：'+redact(type(error).__name__+': '+str(error))},500)

@app.get('/api/v1/health')
async def health():return {'app':'moflow','version':'0.1.0','status':'ready','original_engine':False,'automation_paused':db.get('automation_paused',False)}

@app.post('/api/v1/system/exit')
async def exit_service(request:Request):
    control=os.environ.get('MOFLOW_CONTROL_TOKEN','')
    if not control or request.headers.get('x-moflow-control')!=control:raise HTTPException(403,'只有桌面宿主可以退出服务')
    if hasattr(app.state,'stop'):app.state.stop()
    return {'stopping':True}

@app.get('/api/v1/platform/{kind}')
async def read_platform(kind:str,account:str='default',target:str='',cursor:int=0,count:int=20):
    params={'sec_user_id':target,'aweme_id':target,'collects_id':target,'mix_id':target,'cursor':cursor,'max_cursor':cursor,'count':min(count,100)}
    return await engine.platform.read_f2(kind,account,**params)

@app.get('/api/v1/state')
async def state():
    events=db.query('SELECT * FROM events ORDER BY seq DESC LIMIT 50')
    return {'accounts':db.list('accounts'),'assets':await asyncio.to_thread(engine.library.decorate,db.list('assets',100000)),'jobs':db.list('jobs',200),'runs':db.list('runs',100),'rules':db.list('rules'),'events':events,'paused':db.get('automation_paused',False),'inference':db.get('last_inference'), 'settings':db.settings().model_dump()}

@app.get('/api/v1/events')
async def events(request:Request,after:int=0):
    async def stream():
        cursor=after
        while not await request.is_disconnected():
            rows=db.query('SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 50',(cursor,))
            for row in rows:
                cursor=row['seq'];yield 'id: '+str(cursor)+'\ndata: '+json.dumps(row,ensure_ascii=False)+'\n\n'
            yield ': heartbeat\n\n';await asyncio.sleep(3)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache'})

class SearchInput(BaseModel):
    keyword:str=Field(min_length=1,max_length=200)
    account:str='default'
    kind:str='video'
    count:int=Field(20,ge=1,le=100)
    refresh:bool=False

@app.post('/api/v1/search')
async def search(body:SearchInput):return await engine.platform.search_cache.search(body.keyword,body.account,body.kind,body.count,body.refresh)
@app.get('/api/v1/browse/{kind}')
async def browse(kind:str,account:str='default',target:str='',count:int=30):return {'items':await engine.platform.listing(kind,account,target,min(count,100))}
@app.get('/api/v1/recommendations')
async def recommendation_state(account:str='default',sort:str='balanced'):return engine.recommendations.state(account,sort)
class RecommendationInput(BaseModel):
    sort:str='balanced'
    account:str='default'
    refresh:bool=False
@app.post('/api/v1/recommendations')
async def recommendations(body:RecommendationInput):return engine.recommendations.start(body.account,body.refresh,body.sort)
@app.post('/api/v1/resolve')
async def resolve(body:dict):return await engine.platform.detail(str(body.get('input','')),body.get('account','default'))

@app.get('/api/v1/stream/{identifier}')
async def remote_stream(identifier:str,request:Request,account:str='default'):
    import httpx,ipaddress,socket
    from urllib.parse import urlparse,urljoin
    if not identifier.isdigit():raise ValueError('作品ID无效')
    client=httpx.AsyncClient(timeout=httpx.Timeout(60,connect=15),trust_env=False,follow_redirects=False)
    try:
        media=await engine.platform.detail(identifier,account)
        if not media.get('urls'):raise ValueError('该作品不包含视频流')
        url=media['urls'][0];headers={'Referer':'https://www.douyin.com/','User-Agent':'Mozilla/5.0'}
        if request.headers.get('range'):headers['Range']=request.headers['range']
        response=None
        for attempt in range(6):
            host=urlparse(url).hostname or ''
            allowed=('douyinvod.com','douyin.com','bytecdn.cn','ibytedtos.com','amemv.com','ixigua.com','pstatp.com','snssdk.com','bytednsdoc.com')
            if not any(host==d or host.endswith('.'+d) for d in allowed):raise ValueError('视频流来源不是抖音CDN')
            addresses=await asyncio.to_thread(socket.getaddrinfo,host,443,type=socket.SOCK_STREAM)
            if any(not ipaddress.ip_address(r[4][0]).is_global for r in addresses):raise ValueError('不允许访问非公网媒体地址')
            response=await client.send(client.build_request('GET',url,headers=headers),stream=True)
            if response.is_redirect:
                url=urljoin(url,response.headers['location']);await response.aclose();continue
            if response.status_code in {401,403,404,410} and attempt<2:
                await response.aclose();media=await engine.platform.detail(identifier,account,force=True);url=media['urls'][0];continue
            response.raise_for_status();break
        if response is None or response.is_redirect:raise ValueError('视频流跳转失败')
        async def stream():
            try:
                async for chunk in response.aiter_bytes(128*1024):yield chunk
            finally:await response.aclose();await client.aclose()
        outgoing={k:v for k,v in response.headers.items() if k.lower() in {'content-length','content-range','accept-ranges'}}
        return StreamingResponse(stream(),status_code=response.status_code,media_type='video/mp4',headers=outgoing)
    except BaseException:
        await client.aclose();raise

@app.get('/api/v1/accounts')
async def accounts():return db.list('accounts')
@app.post('/api/v1/accounts')
async def add_account(body:dict):
    identifier=uid();db.execute('INSERT INTO accounts VALUES(?,?,?,?,?)',(identifier,str(body.get('name','新账号'))[:80],'not_logged_in','{}',now()));return {'id':identifier}
@app.post('/api/v1/accounts/{identifier}/login')
async def login(identifier:str):return await engine.platform.login(identifier)
@app.post('/api/v1/accounts/{identifier}/activate')
async def activate(identifier:str):
    if not db.query('SELECT id FROM accounts WHERE id=?',(identifier,)):raise HTTPException(404,'账号不存在')
    db.set('active_account',identifier);return {'active_account':identifier}
@app.get('/api/v1/accounts/{identifier}/status')
async def status(identifier:str):return await engine.platform.status(identifier)
@app.post('/api/v1/accounts/{identifier}/logout')
async def logout(identifier:str):
    async with engine.platform.lock(identifier):
        ctx=await engine.platform.context(identifier);await ctx.clear_cookies();db.execute('UPDATE accounts SET state=? WHERE id=?',('not_logged_in',identifier))
    return {'ok':True}
@app.delete('/api/v1/accounts/{identifier}')
async def remove_account(identifier:str):
    if identifier in engine.platform.contexts:await engine.platform.contexts[identifier][0].close()
    db.execute('DELETE FROM accounts WHERE id=?',(identifier,));return {'ok':True,'profile_preserved':True}

@app.get('/api/v1/settings')
async def settings():return {'settings':db.settings().model_dump(),'keys':{r:bool(vault.get(r)) for r in ['text','vision','deepseek','runninghub']},'mcp_url':'http://127.0.0.1:7861/mcp','mcp_token':MCP_TOKEN,'runtime':str(ROOT)}
@app.put('/api/v1/settings')
async def save_settings(body:dict):
    config=Settings.model_validate(body['settings']);db.set('settings',config.model_dump())
    for role in ['text','vision']:
        if role+'_key' in body:vault.set(role,body[role+'_key'])
    engine.sem=asyncio.Semaphore(config.concurrent_downloads);db.event('settings','','设置已保存');return {'ok':True}
@app.get('/api/v1/models/{role}')
async def models(role:str):
    if role not in {'text','vision'}:raise ValueError('未知模型角色')
    return {'models':await engine.models.list(role)}

@app.post('/api/v1/downloads')
async def download(body:dict):
    inputs=body.get('inputs') or [body.get('input')]
    if not isinstance(inputs,list) or not 1<=len(inputs)<=100 or any(not isinstance(v,str) or not v.strip() for v in inputs):raise ValueError('请提供1—100条作品链接或ID')
    ids=[db.job('download',{'input':v.strip(),'account':body.get('account','default')}) for v in dict.fromkeys(inputs)];await engine.tick();return {'job_ids':ids}
@app.post('/api/v1/jobs/{identifier}/{action}')
async def job_action(identifier:str,action:str):
    if action not in {'pause','resume','cancel','retry'}:raise ValueError('未知操作')
    if not db.query('SELECT id FROM jobs WHERE id=?',(identifier,)):raise HTTPException(404,'任务不存在')
    target={'pause':'paused','resume':'queued','retry':'queued','cancel':'cancelled'}[action];db.update_job(identifier,target,{'paused':'已暂停','queued':'等待执行','cancelled':'已取消'}[target])
    task=engine.active.get(identifier)
    if task and action in {'pause','cancel'}:task.cancel()
    if action=='cancel':
        for path in (ROOT/'tmp').glob(identifier+'-*.part'):path.unlink(missing_ok=True)
    return {'ok':True}

def asset_row(identifier):
    rows=db.query('SELECT * FROM assets WHERE id=?',(identifier,))
    if not rows:raise HTTPException(404,'素材不存在')
    return rows[0]
@app.get('/api/v1/assets/{identifier}/file')
async def preview(identifier:str):
    row=asset_row(identifier)
    if not Path(row['path']).is_file():raise HTTPException(404,'素材文件已移动或删除')
    return FileResponse(row['path'])
@app.get('/api/v1/assets/{identifier}/prompt')
async def prompt(identifier:str):
    row=asset_row(identifier);info=json.loads(row['payload']).get('prompt')
    if not info:raise HTTPException(404,'尚未生成提示词')
    return FileResponse(info['path'],filename=Path(info['path']).name)
@app.post('/api/v1/assets/import')
async def import_assets(body:dict):return engine.library.start(body['path'])
@app.get('/api/v1/library/index')
async def library_index():return engine.library.status()
@app.post('/api/v1/library/index')
async def refresh_library():return engine.library.start()
@app.get('/api/v1/library/directories')
async def library_directories(source:str='all',kind:str='all'):
    if source not in {'all','douyin','comfyui','runninghub','other'} or kind not in {'all','image','video'}:raise ValueError('未知素材分类')
    return {'items':await asyncio.to_thread(engine.library.directories,source,kind)}
@app.post('/api/v1/assets/upload')
async def upload(files:list[UploadFile]=File(...)):
    from PIL import Image
    results=[];folder=Path(db.settings().storage_root)/'图片'/'墨流'/'上传';folder.mkdir(parents=True,exist_ok=True)
    if len(files)>30:raise ValueError('每次最多上传30张图片')
    for f in files:
        temp=ROOT/'tmp'/f'{uid()}.upload';temp.parent.mkdir(exist_ok=True);total=0
        try:
            with temp.open('wb') as out:
                while chunk:=await f.read(256*1024):
                    total+=len(chunk)
                    if total>30*1024**2:raise ValueError('单张图片不能超过30MB')
                    db.check_space(len(chunk));out.write(chunk)
            try:
                with Image.open(temp) as im:im.verify()
            except (OSError,Image.DecompressionBombError) as error:raise ValueError('图片损坏或格式不支持') from error
            with Image.open(temp) as im:
                if im.format not in {'JPEG','PNG','WEBP'}:raise ValueError('仅接受JPEG、PNG和WebP')
                suffix='.'+('jpg' if im.format=='JPEG' else im.format.lower())
            with temp.open('rb') as raw:digest=hashlib.file_digest(raw,'sha256').hexdigest()
            old=db.query('SELECT id FROM assets WHERE sha=?',(digest,))
            if old:results.append(old[0]['id']);continue
            identifier=uid();dest=folder/(identifier+suffix);shutil.move(str(temp),str(dest))
            db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'upload:'+digest,'image',str(dest),digest,total,now(),0,json.dumps({'media':{'title':Path(f.filename or '上传图片').stem,'author':'本地上传'}},ensure_ascii=False)));results.append(identifier)
        finally:temp.unlink(missing_ok=True)
    return {'asset_ids':results}
@app.post('/api/v1/assets/{identifier}/{action}')
async def asset_action(identifier:str,action:str,body:dict):
    asset_row(identifier)
    if action not in {'analyze','reverse'}:raise ValueError('未知素材操作')
    job=db.job(action,{'asset_id':identifier,**body});await engine.tick();return {'job_id':job}
@app.delete('/api/v1/assets/{identifier}')
async def delete_asset(identifier:str,delete_file:bool=False):
    row=asset_row(identifier)
    if delete_file:Path(row['path']).unlink(missing_ok=True)
    db.execute('DELETE FROM assets WHERE id=?',(identifier,));return {'ok':True}

@app.get('/api/v1/social/{kind}')
async def social(kind:str,account:str='default',target:str=''):
    if kind not in {'friends','messages','notices'}:raise ValueError('未知社交列表')
    return {'items':await engine.platform.read_social(kind,account,target)}
@app.post('/api/v1/social/action')
async def social_action(body:dict):
    if body.get('confirm') is not True:raise ValueError('账号动作需要明确提交')
    return await engine.perform_action(body.get('account','default'),body['action'],body['target'],body.get('text',''),manual=True,request_id=body.get('request_id') or uid(),asset_id=body.get('asset_id',''),comment_id=body.get('comment_id',''))
@app.post('/api/v1/social/draft')
async def social_draft(body:dict):return (await engine.models.request('仅生成回复草稿，不发送。要求：'+body.get('instructions','')+'\n消息数据：'+str(body.get('messages','')),Draft)).model_dump()

@app.post('/api/v1/ai/command')
async def command(body:dict):
    text=str(body.get('text','')).strip()
    if not text or len(text)>6000:raise ValueError('请输入不超过6000字的任务')
    j=db.job('command',{'text':text,'account':body.get('account','default')});await engine.tick();return {'job_id':j}
@app.post('/api/v1/ai/plan')
async def plan(body:dict):return (await engine.models.plan(body['text'],body.get('account','default'))).model_dump()
@app.post('/api/v1/runs')
async def create_run(body:Plan):return {'run_id':await engine.create_run(body)}
@app.post('/api/v1/runs/{identifier}/{action}')
async def run_action(identifier:str,action:str):
    if not db.query('SELECT id FROM runs WHERE id=?',(identifier,)):raise HTTPException(404,'流程不存在')
    if action=='resume':engine.spawn('run:'+identifier,engine.execute_run(identifier,True))
    elif action in {'pause','cancel'}:
        if 'run:'+identifier in engine.active:engine.active['run:'+identifier].cancel()
        db.execute('UPDATE runs SET state=? WHERE id=?',('paused' if action=='pause' else 'cancelled',identifier))
    else:raise ValueError('未知流程操作')
    return {'ok':True}

class Rule(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    enabled:bool=False
    account:str='default'
    template:str='plan'
    interval_seconds:int=Field(300,ge=60,le=31536000)
    author_id:str=''
    targets:list[str]=Field(default_factory=list,max_length=100)
    allowed_actions:list[str]=Field(default_factory=list)
    daily_limit:int=Field(10,ge=0,le=1000)
    instructions:str=''
    plan:Plan|None=None

@app.put('/api/v1/rules/{identifier}')
async def save_rule(identifier:str,body:Rule):
    data=body.model_dump()
    if body.template not in {'plan','author','reply','shared'}:raise ValueError('未知规则类型')
    if body.template=='plan' and not body.plan:raise ValueError('规则缺少执行计划')
    if body.template=='author' and not body.author_id:raise ValueError('请填写作者ID')
    if body.template in {'reply','shared'} and not body.targets:raise ValueError('规则需要明确的联系人名单')
    if not set(body.allowed_actions)<={'like','unlike','collect','uncollect','follow','unfollow','message','share','send_image','send_video','comment','like_comment'}:raise ValueError('包含不支持的自动动作')
    if body.allowed_actions and (not body.targets or body.daily_limit<1):raise ValueError('账号自动动作必须配置对象和日上限')
    old=db.query('SELECT payload FROM rules WHERE id=?',(identifier,))
    if old:
        previous=json.loads(old[0]['payload'])
        for field in ['baseline','seen','last_trigger']:
            if field in previous:data[field]=previous[field]
    db.execute('INSERT OR REPLACE INTO rules VALUES(?,?,?,?,?)',(identifier,body.name,int(body.enabled),json.dumps(data,ensure_ascii=False),now()));return {'id':identifier}
@app.delete('/api/v1/rules/{identifier}')
async def delete_rule(identifier:str):db.execute('DELETE FROM rules WHERE id=?',(identifier,));return {'ok':True}
@app.post('/api/v1/automation/pause')
async def pause_all(body:dict):
    paused=bool(body.get('paused',True));db.set('automation_paused',paused)
    if paused:
        await engine.production.close(resume_on_restart=False)
        await engine.chat.close()
        for key,task in list(engine.active.items()):
            if key.startswith(('run:','rule:')):task.cancel()
    return {'paused':paused}
@app.get('/api/v1/analytics')
async def analytics():
    days=90;start=(datetime.now(timezone.utc)-timedelta(days=days-1)).date();counts={row['day']:row['count'] for row in db.query("SELECT substr(created,1,10) day,COUNT(*) count FROM assets WHERE imported=0 GROUP BY day")}
    assets=db.list('assets',100000);authors={};keywords={}
    for asset in assets:
        author=asset['payload'].get('media',{}).get('author') or '未标注';authors[author]=authors.get(author,0)+1
        for tag in asset['payload'].get('analysis',{}).get('tags',[]):keywords[tag]=keywords.get(tag,0)+1
    return {'days':[{'date':(start+timedelta(days=i)).isoformat(),'count':counts.get((start+timedelta(days=i)).isoformat(),0)} for i in range(days)],'authors':[{'id':k,'author':k,'count':v} for k,v in authors.items()],'tags':[{'id':k,'tag':k,'count':v} for k,v in keywords.items()],'states':db.query('SELECT state,COUNT(*) count FROM jobs GROUP BY state'),'bytes':sum(a['bytes'] for a in assets),'imported':sum(a['imported'] for a in assets)}

from .mcp_api import mcp_asgi
app.mount('/mcp',mcp_asgi)
if DIST.exists():app.mount('/',StaticFiles(directory=DIST,html=True),name='web')
else:
    @app.get('/')
    async def root():return JSONResponse({'app':'moflow','message':'前端开发地址：http://127.0.0.1:5178'})
