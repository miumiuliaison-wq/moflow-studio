"""Read-only RunningHub favorite sync and local session handoff to the embedded view.
No application submission, parameter rewriting, or paid task execution exists here.
"""
from __future__ import annotations
import asyncio,hashlib,json,re,time,shutil,zipfile
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import async_playwright
from .core import ROOT,LoginRequired,Unsupported,now,uid,clean,redact
SITE='https://www.runninghub.ai'
class RunningHubBrowser:
    def __init__(self,db):
        self.db=db;self.pw=None;self.ctx=None;self.catalog=None;self.runner=None;self.lock=asyncio.Lock();self.receipts=[];self.favorite_pages={};self.task_records={}
    async def context(self):
        if self.ctx:return self.ctx
        if not self.pw:self.pw=await async_playwright().start()
        try:self.ctx=await self.pw.chromium.launch_persistent_context(str(ROOT/'profiles'/'runninghub'/'default'),channel='chrome',headless=False,no_viewport=True,accept_downloads=True,args=['--start-minimized'])
        except BaseException:
            await self.pw.stop();self.pw=None;raise
        self.ctx.on('response',self.observe)
        self.ctx.on('close',self.disconnected)
        return self.ctx
    def disconnected(self,*args):
        self.ctx=None;self.catalog=None;self.runner=None;self.db.set('rh_connection',{'state':'not_connected','updated':now()})
    async def observe(self,response):
        path=urlsplit(response.url).path
        if urlsplit(response.url).hostname!='www.runninghub.ai' or 'json' not in response.headers.get('content-type',''):return
        if path not in {'/uc/getUserInfo','/api/likeOrCollect/webapp/list','/task/list'} and not re.search(r'/task/(?:webapp/)?(?:run|create|submit)',path):return
        try:
            data=await response.json();body=response.request.post_data_json or {}
            if path=='/uc/getUserInfo':
                user=data.get('data') or {};connected=data.get('code')==0 and bool(user.get('id') or user.get('userId'))
                self.db.set('rh_connection',{'state':'connected' if connected else 'not_logged_in','name':user.get('nickName') or user.get('name') or user.get('nickname') or '', 'updated':now()})
            elif path=='/api/likeOrCollect/webapp/list' and body.get('operateType')==2 and data.get('code')==0:
                self.favorite_pages[int(body.get('current',1))]=data.get('data') or {}
            elif path=='/task/list' and data.get('code')==0:
                for row in (data.get('data') or {}).get('records') or []:
                    identifier=row.get('id') or row.get('taskId')
                    if identifier:self.task_records[str(identifier)]=row
            elif data.get('code')==0:
                result=data.get('data') or {};identifier=result.get('taskId') or result.get('id')
                if identifier:self.receipts.append({'task_id':str(identifier),'at':time.time()})
        except Exception:pass
    async def page(self,kind='catalog'):
        ctx=await self.context();page=getattr(self,kind)
        if not page or page.is_closed():page=await ctx.new_page();setattr(self,kind,page)
        return page
    async def goto(self,page,url):
        await page.goto(url,wait_until='domcontentloaded',timeout=60000);await page.wait_for_timeout(1800)
    async def check_login(self,page):
        state=self.db.get('rh_connection',{}).get('state')
        text=await page.locator('body').inner_text()
        if re.search('验证码|安全验证|访问验证',await page.title()) or '请完成验证' in text:
            self.db.set('rh_connection',{'state':'waiting_login','updated':now()});raise LoginRequired('请在墨流的 RunningHub 浏览器完成验证')
        if state!='connected' or '登录/注册' in text:
            raise LoginRequired('请先在墨流专用 Chrome 登录 RunningHub')
    async def login(self):
        async with self.lock:
            page=await self.page();await self.goto(page,SITE+'/zh-cn/user-center')
            try:
                session=await self.ctx.new_cdp_session(page);window=await session.send('Browser.getWindowForTarget');await session.send('Browser.setWindowBounds',{'windowId':window['windowId'],'bounds':{'windowState':'normal'}});await session.detach()
            except Exception:pass
            await page.bring_to_front();return {'opened':True}
    async def status(self):
        return {**self.db.get('rh_connection',{'state':'not_connected'}),'browser_open':self.ctx is not None,'dedicated_profile':True}
    async def sync(self):
        async with self.lock:
            page=await self.page();self.favorite_pages={};await self.goto(page,SITE+'/zh-cn/user-center');await self.check_login(page)
            await page.locator('.radio-item').filter(has_text=re.compile('^我的收藏$')).click()
            tab=page.locator('.radio-item').filter(has_text=re.compile('^AI应用$'))
            if await tab.count():await tab.click()
            records={};current=1
            while True:
                for _ in range(60):
                    if current in self.favorite_pages:break
                    await page.wait_for_timeout(250)
                data=self.favorite_pages.get(current)
                if data is None:raise Unsupported('未能完整读取收藏分页，已保留上次成功列表')
                for row in data.get('records') or []:records[str(row['id'])]=row
                more=data.get('hasNext',current<int(data.get('pages') or 1))
                if not more:break
                current+=1
                await page.evaluate("() => {const e=[...document.querySelectorAll('div,main')].filter(x=>x.scrollHeight>x.clientHeight+200&&x.clientHeight>200).pop();if(e)e.scrollTop=e.scrollHeight;else window.scrollTo(0,document.body.scrollHeight)}")
                await page.mouse.wheel(0,3000)
            with self.db.conn() as conn:
                conn.execute('UPDATE rh_apps SET available=0')
                for identifier,row in records.items():
                    cover=(row.get('covers') or [{}])[0]
                    item={'id':identifier,'name':row.get('name',''),'description':row.get('intro') or '', 'author':(row.get('owner') or {}).get('name',''),'cover':cover.get('thumbnailUri') or cover.get('url',''),'source':SITE+'/zh-cn/ai-detail/'+identifier}
                    conn.execute('INSERT INTO rh_apps VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET available=1,payload=excluded.payload,updated=excluded.updated',(identifier,1,json.dumps(item,ensure_ascii=False),now()))
            self.db.set('rh_last_sync',now());self.db.event('runninghub','','已同步 '+str(len(records))+' 个收藏应用');return {'count':len(records),'pages':current}
    async def close(self):
        if self.ctx:await self.ctx.close()
        if self.pw:await self.pw.stop();self.pw=None

class RunningHub:
    def __init__(self,engine):self.engine=engine;self.db=engine.db;self.browser=RunningHubBrowser(self.db);self.sync_task=None;self.sync_error=None
    def apps(self):
        return [{**json.loads(r['payload']),'available':bool(r['available']),'updated':r['updated']} for r in self.db.query('SELECT * FROM rh_apps ORDER BY available DESC,rowid')]
    def sync_status(self):
        running=self.sync_task is not None and not self.sync_task.done()
        return {'state':'syncing' if running else self.sync_error['state'] if self.sync_error else 'ready','message':self.sync_error['message'] if self.sync_error else '', 'count':sum(r['available'] for r in self.apps()),'last_sync':self.db.get('rh_last_sync')}
    def start_sync(self):
        if self.sync_task is None or self.sync_task.done():
            self.sync_error=None;self.sync_task=asyncio.create_task(self.sync())
        return self.sync_status()
    async def sync(self):
        try:
            async with asyncio.timeout(45):await self.browser.sync()
        except asyncio.CancelledError:raise
        except Exception as error:
            message='收藏同步超过45秒，已保留上次列表；可稍后重试。' if isinstance(error,TimeoutError) else '收藏同步未完成，已保留上次列表；可在内置网页打开“我的收藏”继续同步。'
            self.sync_error={'state':'waiting_login' if isinstance(error,LoginRequired) else 'error','message':str(error) if isinstance(error,LoginRequired) else message}
            self.db.event('runninghub','','收藏同步未完成：'+redact(error))
    async def close(self):
        if self.sync_task and not self.sync_task.done():
            self.sync_task.cancel();await asyncio.gather(self.sync_task,return_exceptions=True)
        await self.browser.close()
    async def export_session(self,force=False):
        profile=ROOT/'webview-runninghub'
        config={'webview_profile':str(profile),'storage_root':self.db.settings().storage_root}
        # WebView2 already persists cookies/localStorage. Reopening Chrome here
        # adds network latency and can contend with an existing Chrome profile.
        if not force and (profile/'EBWebView'/'Default'/'Local Storage').is_dir():
            return {**config,'persistent_profile':True,'origins':[],'cookies':[]}
        async with self.browser.lock:
            page=await self.browser.page();await self.browser.goto(page,SITE+'/zh-cn/user-center');await self.browser.check_login(page)
            state=await self.browser.ctx.storage_state()
            origins=[]
            for item in state.get('origins',[]):
                if item['origin']==SITE:
                    origins=[{'origin':SITE,'localStorage':[v for v in item['localStorage'] if v['name'].startswith('Rh-') or v['name'] in {'userInfo','userId'}]}]
            return {**config,'origins':origins,'cookies':[c for c in state['cookies'] if c['domain'].lstrip('.') in {'www.runninghub.ai','runninghub.ai'}]}
    def replace_favorites(self,records,complete=True):
        if not isinstance(records,list) or len(records)>10000:raise ValueError('收藏格式不正确')
        normalized=[]
        for r in records:
            identifier=str(r.get('id',''))
            if not re.fullmatch(r'\d{8,25}',identifier):raise ValueError('无效收藏应用ID')
            covers=r.get('covers') or [{}];cover=covers[0]
            url=cover.get('thumbnailUri') or cover.get('url') or r.get('cover','')
            if url and (urlsplit(url).scheme!='https' or not (urlsplit(url).hostname or '').endswith('.xiaoyaoyou.com')):url=''
            normalized.append({'id':identifier,'name':str(r.get('name',''))[:300],'description':str(r.get('intro') or r.get('description') or '')[:5000],'author':str((r.get('owner') or {}).get('name') or r.get('author') or '')[:200],'cover':url,'source':SITE+'/zh-cn/ai-detail/'+identifier})
        with self.db.conn() as conn:
            if complete:conn.execute('UPDATE rh_apps SET available=0')
            for item in normalized:conn.execute('INSERT INTO rh_apps VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET available=1,payload=excluded.payload,updated=excluded.updated',(item['id'],1,json.dumps(item,ensure_ascii=False),now()))
        self.db.set('rh_last_sync',now());return len(normalized)
    def download_directory(self):
        self.db.check_space();folder=Path(self.db.settings().storage_root)/'下载'/'RunningHub';folder.mkdir(parents=True,exist_ok=True);return folder
    async def register_download(self,path,source=''):
        root=(Path(self.db.settings().storage_root)/'下载'/'RunningHub').resolve();path=Path(path).resolve()
        if not path.is_relative_to(root) or not path.is_file():raise ValueError('下载文件不在 RunningHub 归档目录中')
        self.db.check_space()
        if urlsplit(source).hostname!='www.runninghub.ai':source=SITE
        source=source.split('?')[0];registered=[]
        async def register(file):
            from PIL import Image
            try:
                with Image.open(file) as im:im.verify()
                kind='image'
                with Image.open(file) as im:probe={'width':im.width,'height':im.height}
            except (OSError,ValueError):
                if file.suffix.lower() not in {'.mp4','.mov','.webm','.mkv'}:return
                probe=await self.engine.probe(file,True);kind='video'
            with file.open('rb') as data:digest=hashlib.file_digest(data,'sha256').hexdigest()
            found=self.db.query('SELECT id FROM assets WHERE sha=?',(digest,))
            if found:registered.append(found[0]['id']);return
            folder=Path(self.db.settings().storage_root)/('图片' if kind=='image' else '视频')/'墨流'/'RunningHub';folder.mkdir(parents=True,exist_ok=True)
            dest=folder/(clean(file.stem)+'_'+digest[:8]+file.suffix.lower())
            if file!=dest:shutil.move(str(file),str(dest))
            identifier=uid();payload={'provider':'runninghub','media':{'title':file.stem,'author':'RunningHub','source':source},'probe':probe}
            self.db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'runninghub:'+digest,kind,str(dest),digest,dest.stat().st_size,now(),0,json.dumps(payload,ensure_ascii=False)));registered.append(identifier)
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                items=archive.infolist()
                if len(items)>1000:raise ValueError('压缩包文件数量过多，已保留原件')
                for item in items:
                    relative=Path(item.filename)
                    if relative.is_absolute() or '..' in relative.parts or '\\' in item.filename or (item.external_attr>>16)&0o170000==0o120000:raise ValueError('压缩包含有不安全路径，已保留原件')
                allowed=[i for i in items if not i.is_dir() and Path(i.filename).suffix.lower() in {'.jpg','.jpeg','.png','.webp','.mp4','.mov','.webm','.mkv'}]
                self.db.check_space(sum(i.file_size for i in allowed));temp=root/('unpack-'+uid());temp.mkdir()
                try:
                    for index,item in enumerate(allowed):
                        dest=temp/(str(index)+Path(item.filename).suffix.lower())
                        with archive.open(item) as data,dest.open('wb') as out:
                            while chunk:=data.read(256*1024):self.db.check_space(len(chunk));out.write(chunk)
                        await register(dest)
                finally:shutil.rmtree(temp,ignore_errors=True)
        else:await register(path)
        self.db.event('runninghub','',f'网页下载完成，已登记 {len(registered)} 个素材')
        return {'asset_ids':registered,'path':str(path),'archive_preserved':path.suffix.lower()=='.zip'}
