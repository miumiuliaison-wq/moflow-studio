from __future__ import annotations
import asyncio, json, re, time, os
from urllib.parse import quote,urlparse,parse_qs
from pathlib import Path
from patchright.async_api import async_playwright
from .core import ROOT,Store,LoginRequired,Unsupported,now,uid

def walk(value):
    pending=[value]; count=0
    while pending and count<40000:
        node=pending.pop(); count+=1
        if isinstance(node,dict): yield node; pending.extend(v for v in node.values() if isinstance(v,(list,dict)))
        elif isinstance(node,list): pending.extend(node)

def normalize(value):
    out={}
    for n in walk(value):
        identifier=n.get('aweme_id'); video=n.get('video')
        if not identifier or not (isinstance(video,dict) or isinstance(n.get('images'),list)): continue
        author=n.get('author') or {}; video=video or {}; streams=[]
        rates=sorted(video.get('bit_rate') or [],key=lambda x:x.get('bit_rate',0),reverse=True)
        for item in rates+[video]: streams.extend((item.get('play_addr') or {}).get('url_list') or [])
        streams.extend((video.get('download_addr') or {}).get('url_list') or [])
        images=[(x.get('url_list') or [])[-1] for x in n.get('images') or [] if x.get('url_list')]
        cover=(video.get('origin_cover') or video.get('cover') or {}).get('url_list') or []
        out[str(identifier)]={'id':str(identifier),'title':n.get('desc',''),'author':author.get('nickname',''),'author_id':author.get('sec_uid',''),'author_gender':author.get('gender'),'source':f'https://www.douyin.com/video/{identifier}','urls':list(dict.fromkeys(streams)),'images':images,'cover':cover[-1] if cover else '', 'duration':video.get('duration',0)/1000,'width':video.get('width',0),'height':video.get('height',0),'stats':n.get('statistics') or {},'published':n.get('create_time')}
    return list(out.values())

DOM = r'''() => {
 const records=[],users=[],data=[];
 for(const a of document.querySelectorAll('a[href]')){
  const m=a.href.match(/\/video\/(\d+)/);const img=a.querySelector('img');
  if(m)records.push({id:m[1],source:a.href,title:(a.innerText||img?.alt||'').slice(0,500),cover:img?.currentSrc||'',urls:[],images:[]});
  const u=a.href.match(/\/user\/([^/?]+)/);if(u)users.push({id:u[1],name:(a.innerText||img?.alt||'').slice(0,160),avatar:img?.currentSrc||''});
  let f=a[Object.keys(a).find(k=>k.startsWith('__reactFiber'))];
  for(let i=0;f&&i<10;i++,f=f.return){const v=f.memoizedProps?.awemeInfo;if(v){try{data.push(JSON.parse(JSON.stringify(v)))}catch{}break;}}
 }
 for(const k of ['__INITIAL_STATE__','__NEXT_DATA__','_SSR_DATA'])try{if(window[k])data.push(JSON.parse(JSON.stringify(window[k])))}catch{}
 const s=document.getElementById('RENDER_DATA');if(s)try{data.push(JSON.parse(decodeURIComponent(s.textContent)))}catch{}
 return {records,users,data};
}'''

class Douyin:
    def __init__(self,store:Store):
        self.db=store;self.pw=None;self.contexts={};self.pages={};self.locks={};self.media={};self.last={};self.received={};self.details={}
        from .discovery_cache import SearchCache
        self.search_cache=SearchCache(store,self._search)
        if not store.list('accounts'):
            store.execute('INSERT INTO accounts VALUES(?,?,?,?,?)',('default','主账号','not_logged_in','{}',now()))
    def lock(self,account): return self.locks.setdefault(account,asyncio.Lock())
    async def context(self,account='default',visible=False):
        if not self.db.query('SELECT id FROM accounts WHERE id=?',(account,)): raise ValueError('账号不存在')
        if not self.pw: self.pw=await async_playwright().start()
        existing=self.contexts.get(account)
        if existing and visible and not existing[1]:
            await existing[0].close(); self.contexts.pop(account,None);self.pages.pop(account,None);existing=None
        if existing: return existing[0]
        profile=ROOT/'profiles'/account
        debug_args=['--remote-debugging-port='+os.environ['MOFLOW_BROWSER_DEBUG_PORT']] if os.environ.get('MOFLOW_BROWSER_DEBUG_PORT') else []
        ctx=await self.pw.chromium.launch_persistent_context(str(profile),channel='chrome',headless=False,no_viewport=True,accept_downloads=True,args=([] if visible else ['--start-minimized'])+debug_args)
        self.contexts[account]=(ctx,visible)
        ctx.on('close',lambda *_: (self.contexts.pop(account,None),self.pages.pop(account,None)))
        return ctx
    async def page(self,account='default'):
        ctx=await self.context(account)
        if account in self.pages and not self.pages[account].is_closed():return self.pages[account]
        page=await ctx.new_page();self.pages[account]=page
        async def observe(response):
            try:
                if response.status!=200:return
                if 'json' in response.headers.get('content-type','') and any(k in response.url for k in ['aweme','search','feed','comment','notice','message','friend']):
                    data=await response.json();items=normalize(data);self.media.setdefault(account,[]).extend(items)
                    if data.get('status_code',0)==0:
                        from .native_api import PATHS
                        parsed=urlparse(response.url)
                        kind=next((k for k,(path,_) in PATHS.items() if path==parsed.path),'search' if '/search/' in parsed.path else 'page')
                        keyword=parse_qs(parsed.query).get('keyword',[''])[0]
                        if items or kind=='search_users' and any(n.get('sec_uid') and n.get('nickname') for n in walk(data)):
                            self.verified(account,kind,keyword)
                    if len(self.media[account])>1000:self.media[account]=self.media[account][-500:]
                    self.received.setdefault(account,[]).append((response.url,data));self.received[account]=self.received[account][-30:]
            except Exception:pass
        page.on('response',observe)
        return page
    def waiting(self,account,kind='page',keyword='',url=''):
        payload=json.loads(self.db.query('SELECT payload FROM accounts WHERE id=?',(account,))[0]['payload'])
        payload.update(waiting_api=True,waiting_kind=kind,waiting_keyword=keyword,waiting_url=url)
        self.db.execute('UPDATE accounts SET state=?,payload=?,updated=? WHERE id=?',('waiting_login',json.dumps(payload),now(),account))
    def verified(self,account,kind,keyword=''):
        payload=json.loads(self.db.query('SELECT payload FROM accounts WHERE id=?',(account,))[0]['payload'])
        if not payload.get('waiting_api'):return
        expected=payload.get('waiting_kind','page')
        if expected!='page' and kind!=expected:return
        if payload.get('waiting_keyword') and payload['waiting_keyword']!=keyword:return
        for key in ['waiting_api','waiting_kind','waiting_keyword','waiting_url']:payload.pop(key,None)
        self.db.execute('UPDATE accounts SET state=?,payload=?,updated=? WHERE id=?',('connected',json.dumps(payload),now(),account))
        self.db.event('account',account,'已从真实平台响应确认验证恢复')
    async def goto(self,page,url,account,settle=2200,timeout=45000):
        await asyncio.sleep(max(0,self.db.settings().request_interval-(time.monotonic()-self.last.get(account,0))))
        self.last[account]=time.monotonic();self.media[account]=[];self.received[account]=[]
        try:await page.goto(url,wait_until='domcontentloaded',timeout=timeout)
        except Exception:
            if page.url=='about:blank':raise
        if settle:await page.wait_for_timeout(settle)
    async def gate(self,page,account,has_results=False):
        text=(await page.locator('body').inner_text())[:14000];title=await page.title()
        if re.search(r'验证码|安全验证|访问验证',title) or re.search(r'/passport|/captcha|/verify',page.url) or any(x in text for x in ['请完成验证','访问过于频繁','网络环境存在风险']):
            self.waiting(account,url=page.url);raise LoginRequired('抖音需要验证，请在账号设置中打开浏览器处理')
        if not has_results and any(x in text for x in ['登录后搜索','请先登录','登录后查看更多','扫码登录']):
            self.waiting(account,url=page.url);raise LoginRequired('当前页面需要登录，请打开账号浏览器')
        return text
    async def login(self,account='default'):
        async with self.lock(account):
            await self.context(account,True);p=await self.page(account)
            payload=json.loads(self.db.query('SELECT payload FROM accounts WHERE id=?',(account,))[0]['payload'])
            url=payload.get('waiting_url') or 'https://www.douyin.com/'
            if urlparse(url).hostname!='www.douyin.com':url='https://www.douyin.com/'
            await p.goto(url,wait_until='domcontentloaded',timeout=45000);await p.bring_to_front()
            self.db.event('account',account,'已打开独立 Chrome 登录窗口');return {'opened':True}
    async def status(self,account='default'):
        ctx=self.contexts.get(account)
        if not ctx:return {'id':account,'state':self.db.query('SELECT state FROM accounts WHERE id=?',(account,))[0]['state']}
        cookies=await ctx[0].cookies('https://www.douyin.com')
        authenticated=any(c['name'] in {'sessionid','sessionid_ss','sid_guard'} and c['value'] for c in cookies)
        verification=False
        page=self.pages.get(account)
        if page and not page.is_closed():
            verification=bool(re.search(r'验证码|安全验证|访问验证',await page.title()))
        payload=json.loads(self.db.query('SELECT payload FROM accounts WHERE id=?',(account,))[0]['payload'])
        state='waiting_login' if verification or payload.get('waiting_api') else 'connected' if authenticated else 'not_logged_in'
        self.db.execute('UPDATE accounts SET state=?,updated=? WHERE id=?',(state,now(),account))
        return {'id':account,'state':state}
    async def cookie_header(self,account):
        ctx=await self.context(account);return '; '.join(c['name']+'='+c['value'] for c in await ctx.cookies('https://www.douyin.com'))
    async def read_native(self,kind,account='default',request_timeout=25,attempts=None,**params):
        from .native_api import read,FINGERPRINT_JS
        page=await self.page(account);ctx=await self.context(account)
        fingerprint=await page.evaluate(FINGERPRINT_JS)
        cookies={c['name']:c['value'] for c in await ctx.cookies('https://www.douyin.com')}
        trace={}
        try:
            result=await read(kind,params,fingerprint,cookies,trace,timeout=request_timeout,attempts=attempts)
            self.verified(account,kind,params.get('keyword',''))
            return result
        except LoginRequired:
            url='https://www.douyin.com/search/'+quote(params['keyword'])+'?type='+('user' if kind=='search_users' else 'video') if kind in {'search','search_users'} else page.url
            self.waiting(account,kind,params.get('keyword',''),url);raise
        finally:
            if kind in {'search','search_users'}:self.db.set('last_native_search',trace)
    async def read_f2(self,kind,account='default',**params):
        from f2.apps.douyin.crawler import DouyinCrawler
        from f2.apps.douyin import model
        methods={'profile':('fetch_user_profile','UserProfile'),'posts':('fetch_user_post','UserPost'),'liked':('fetch_user_like','UserLike'),'collected':('fetch_user_collection','UserCollection'),'folders':('fetch_user_collects','UserCollects'),'folder_videos':('fetch_user_collects_video','UserCollectsVideo'),'mix':('fetch_user_mix','UserMix'),'comments':('fetch_post_comment','PostComment'),'replies':('fetch_post_comment_reply','PostCommentReply'),'current_user':('fetch_query_user','QueryUser'),'following':('fetch_user_following','UserFollowing')}
        if kind not in methods:raise ValueError('未知读取能力')
        method,model_name=methods[kind];cls=getattr(model,model_name);fields={k:v for k,v in params.items() if k in cls.model_fields and v is not None}
        async with self.lock(account):
            try:
                async with DouyinCrawler({'cookie':await self.cookie_header(account)}) as crawler:
                    result=await getattr(crawler,method)(cls(**fields))
                if result:
                    code=result.get('status_code',0)
                    if code!=0:raise LoginRequired('抖音接口返回状态 '+str(code)+'，请检查登录或平台验证')
                    self.verified(account,kind)
                    return result
            except LoginRequired:
                self.waiting(account,kind,url='https://www.douyin.com/');raise
            except Exception as error:self.db.event('adapter',account,'F2读取未成功，使用本地请求适配：'+type(error).__name__)
            return await self.read_native(kind,account,**fields)
    async def search(self,keyword,account='default',kind='video',count=20):
        return (await self.search_cache.search(keyword,account,kind,count))['items']
    async def _search(self,keyword,account='default',kind='video',count=20):
        async with self.lock(account):
            await self.page(account)
            await asyncio.sleep(max(0,self.db.settings().request_interval-(time.monotonic()-self.last.get(account,0))))
            self.last[account]=time.monotonic()
            try:
                data=await self.read_native('search_users' if kind=='user' else 'search',account,keyword=keyword,count=min(count,50),request_timeout=7,attempts=1)
                if kind=='video':found=normalize(data)
                else:
                    users={}
                    for node in walk(data):
                        if node.get('sec_uid') and node.get('nickname'):
                            avatar=(node.get('avatar_thumb') or {}).get('url_list') or []
                            users[node['sec_uid']]={'id':node['sec_uid'],'name':node['nickname'],'avatar':avatar[-1] if avatar else ''}
                    found=list(users.values())
                if found and (len(found)>=count or not data.get('has_more')):return found[:count]
            except LoginRequired:raise
            except Exception as error:self.db.event('adapter',account,'搜索快速读取未完成，改读搜索页：'+type(error).__name__)

            page=await self.page(account);await self.goto(page,'https://www.douyin.com/search/'+quote(keyword)+'?type='+('user' if kind=='user' else 'video'),account)
            results={};users={}
            for _ in range(5):
                dom=await page.evaluate(DOM,isolated_context=False)
                for row in dom['records']:results.setdefault(row['id'],row)
                for row in self.media.get(account,[])+[r for data in dom['data'] for r in normalize(data)]:results[row['id']]=row
                for user in dom['users']:users[user['id']]=user
                await self.gate(page,account,bool(users if kind=='user' else results))
                if len(users if kind=='user' else results)>=count:break
                await page.mouse.wheel(0,1500);await page.wait_for_timeout(1200)
            found=list((users if kind=='user' else results).values())[:count]
            if not found:
                self.db.event('adapter',account,'搜索页暂无可解析结果，尝试本地请求适配')
                data=await self.read_native('search_users' if kind=='user' else 'search',account,keyword=keyword,count=min(count,50))
                if kind=='user':
                    for node in walk(data):
                        if node.get('sec_uid') and node.get('nickname'):
                            avatar=(node.get('avatar_thumb') or {}).get('url_list') or []
                            users[node['sec_uid']]={'id':node['sec_uid'],'name':node['nickname'],'avatar':avatar[-1] if avatar else ''}
                    found=list(users.values())[:count]
                else:found=normalize(data)[:count]
                if found:
                    self.db.event('adapter',account,'本地请求搜索取得 '+str(len(found))+' 条结果');return found
                text=await page.locator('body').inner_text()
                if not re.search(r'暂无.{0,6}(结果|作品|用户)|未找到.{0,8}(结果|作品|用户)|没有找到',text):
                    raise Unsupported('搜索页没有提供可解析的结果，可能尚未加载或需要平台验证；不能将此状态当作候选不足')
            if found:self.verified(account,'search_users' if kind=='user' else 'search',keyword)
            return found
    async def recommendation_search(self,keyword,account='default'):
        async with self.lock(account):
            # Reuse the authenticated browser without reopening the homepage.
            await self.page(account)
            await asyncio.sleep(max(0,self.db.settings().request_interval-(time.monotonic()-self.last.get(account,0))))
            self.last[account]=time.monotonic()
            filters=json.dumps({'sort_type':'1','publish_time':'7','filter_duration':'0','search_range':'0','content_type':'1'},separators=(',',':'))
            try:
                data=await self.read_native('search',account,keyword=keyword,count=30,is_filter_search=1,filter_selected=filters,request_timeout=7,attempts=1)
                rows=normalize(data)
                if rows:return rows
            except LoginRequired:raise
            except Exception as error:self.db.event('adapter',account,'推荐快速读取未完成，改读搜索页：'+type(error).__name__)
            page=await self.page(account)
            await self.goto(page,'https://www.douyin.com/search/'+quote(keyword)+'?type=video&sort_type=1&publish_time=7',account,settle=0,timeout=12000)
            deadline=time.monotonic()+7
            while time.monotonic()<deadline:
                rows=[]
                for url,data in self.received.get(account,[]):
                    parsed=urlparse(url)
                    if '/search/' in parsed.path and parse_qs(parsed.query).get('keyword',[''])[0]==keyword:rows.extend(normalize(data))
                if rows:
                    self.verified(account,'search',keyword);return rows
                await asyncio.sleep(.2)
            await self.gate(page,account)
            raise Unsupported('平台搜索尚未返回可解析的视频，请稍后换一批')
    async def detail(self,value,account='default',force=False):
        match=re.search(r'(?:/video/|/note/|[?&]modal_id=|^)(\d{8,25})',value)
        cache_key=(account,match[1]) if match else None
        if not force and cache_key in self.details and time.monotonic()-self.details[cache_key][0]<120:return self.details[cache_key][1]
        if match:
            try:
                from f2.apps.douyin.crawler import DouyinCrawler
                from f2.apps.douyin.model import PostDetail
                async with DouyinCrawler({'cookie':await self.cookie_header(account)}) as crawler:
                    data=await crawler.fetch_post_detail(PostDetail(aweme_id=match[1]))
                rows=normalize(data)
                matching=next((r for r in rows if r['id']==match[1] and (r['urls'] or r['images'])),None)
                if matching:
                    self.details[(account,matching['id'])]=(time.monotonic(),matching);return matching
            except Exception as error:self.db.event('adapter',account,'F2详情读取未成功，改读官方页面：'+type(error).__name__)
        if not match:
            from urllib.parse import urlsplit
            found=re.search(r'https?://[^\s]+',value)
            if not found:raise ValueError('请输入抖音作品 ID 或链接')
            value=found[0].rstrip('，。)')
            host=urlsplit(value).hostname or ''
            if host!='douyin.com' and not host.endswith('.douyin.com'):raise ValueError('仅支持抖音来源链接')
        async with self.lock(account):
            if match:
                try:
                    rows=normalize(await self.read_native('detail',account,aweme_id=match[1]))
                    matching=next((r for r in rows if r['id']==match[1] and (r['urls'] or r['images'])),None)
                    if matching:
                        self.details[(account,matching['id'])]=(time.monotonic(),matching);return matching
                except LoginRequired:raise
                except Exception as error:self.db.event('adapter',account,'本地请求适配未成功，改读官方页面：'+type(error).__name__)
            page=await self.page(account);await self.goto(page,f'https://www.douyin.com/video/{match[1]}' if match else value,account)
            dom=await page.evaluate(DOM,isolated_context=False);rows=self.media.get(account,[])+[r for d in dom['data'] for r in normalize(d)]
            identifier=(re.search(r'/video/(\d+)',page.url) or match)
            if identifier:
                for row in rows:
                    if row['id']==identifier[1] and (row.get('urls') or row.get('images')):return row
            await self.gate(page,account,bool(rows))
            if identifier:
                urls=await page.locator('video').evaluate_all('(vs)=>vs.map(v=>v.currentSrc||v.src).filter(s=>s.startsWith("http"))')
                if urls:return {'id':identifier[1],'source':page.url,'title':(await page.title()).removesuffix(' - 抖音'),'author':'','urls':urls,'images':[],'cover':''}
            raise Unsupported('页面没有提供可解析的作品媒体，未把推荐视频当作目标下载')
    async def listing(self,kind,account='default',target='',count=30):
        async with self.lock(account):
            page=await self.page(account)
            urls={'feed':'https://www.douyin.com/?recommend=1','liked':'https://www.douyin.com/user/self?showTab=like','collected':'https://www.douyin.com/user/self?showTab=collection','following':'https://www.douyin.com/follow','user':f'https://www.douyin.com/user/{quote(target)}'}
            if kind not in urls:raise ValueError('未知列表类型')
            await self.goto(page,urls[kind],account);dom=await page.evaluate(DOM,isolated_context=False)
            rows={r['id']:r for r in dom['records']}
            for r in self.media.get(account,[])+[r for d in dom['data'] for r in normalize(d)]:rows[r['id']]=r
            await self.gate(page,account,bool(rows));return list(rows.values())[:count]
    async def read_social(self,kind,account='default',target=''):
        async with self.lock(account):
            page=await self.page(account);await self.goto(page,'https://www.douyin.com/',account)
            label='消息' if kind in {'friends','messages'} else '通知'
            trigger=page.get_by_text(label,exact=True).filter(visible=True)
            if await trigger.count()==1:
                await trigger.hover();await page.wait_for_timeout(700)
                if kind!='notices':await trigger.click()
                await page.wait_for_timeout(1400)
            await self.gate(page,account)
            if target:
                person=page.get_by_text(target,exact=True)
                if await person.count()!=1:raise Unsupported('未唯一定位到指定会话，请使用页面中的准确联系人名称')
                await person.click();await page.wait_for_timeout(1000)
            if kind=='notices':
                notices=[];observed_list=False
                for url,data in self.received.get(account,[]):
                    if 'notice' not in url:continue
                    for node in walk(data):
                        for key in ['notice_list','notice_infos','notices']:
                            if isinstance(node.get(key),list):
                                observed_list=True
                                for entry in node[key]:
                                    if not isinstance(entry,dict):continue
                                    identifier=str(entry.get('notice_id') or entry.get('notice_id_str') or '')
                                    text=entry.get('text') or entry.get('content') or entry.get('desc') or json.dumps(entry,ensure_ascii=False)
                                    notices.append({'id':identifier or __import__('hashlib').sha256(str(text).encode()).hexdigest()[:24],'text':str(text),'kind':'notice','direction':'incoming'})
                if observed_list:return notices
                empty=page.get_by_text(re.compile('^(暂无通知|暂无消息|暂时没有新通知|还没有收到通知)$')).filter(visible=True)
                if await empty.count():return []
            selector='[data-e2e*="notice"],[class*="notice-item"],[class*="notification-item"]' if kind=='notices' else '[data-e2e*="message"],[data-e2e*="conversation"],[class*="message-item"],[class*="conversation-item"]'
            records=await page.evaluate('''selector => [...document.querySelectorAll(selector)].filter(e=>e.getBoundingClientRect().width>0).map(e=>({text:e.innerText,id:e.getAttribute('data-id')||'',kind:e.getAttribute('data-e2e')||'',direction:e.getAttribute('data-direction')||(e.hasAttribute('data-is-self')?(e.getAttribute('data-is-self')==='true'?'outgoing':'incoming'):'unknown')})).filter(x=>x.text).slice(-100)''',selector)
            if not records:raise Unsupported('当前抖音页面未暴露可识别的会话/通知结构，需要适配；没有返回模拟消息')
            for row in records:
                row['direction']=row.get('direction','unknown')
            return [{**r,'id':r['id'] or __import__('hashlib').sha256(r['text'].encode()).hexdigest()[:24]} for r in records]
    async def action(self,action,account,target,text='',file_path=None,comment_id=''):
        async with self.lock(account):
            page=await self.page(account)
            if action in {'like','collect','follow','unlike','uncollect','unfollow'}:
                base={'unlike':'like','uncollect':'collect','unfollow':'follow'}.get(action,action);desired=not action.startswith('un')
                destination=f'https://www.douyin.com/user/{quote(target)}' if base=='follow' else f'https://www.douyin.com/video/{target}'
                await self.goto(page,destination,account);await self.gate(page,account)
                selector={'like':'[data-e2e="like-icon"]','collect':'[data-e2e="collect-icon"]','follow':'button:has-text("关注")'}[base]
                control=page.locator(selector).filter(visible=True)
                if await control.count()!=1:raise Unsupported('操作控件不能唯一定位，未执行账号动作')
                before=await control.get_attribute('aria-pressed')
                if before==str(desired).lower():return {'state':'already_applied'}
                await control.click();await page.wait_for_timeout(1000)
                if action=='unfollow':
                    confirmation=page.get_by_role('button',name='取消关注',exact=True).filter(visible=True)
                    if await confirmation.count()==1:await confirmation.click();await page.wait_for_timeout(800)
                after=await control.get_attribute('aria-pressed');body=await page.locator('body').inner_text()
                if after==str(desired).lower() or (action=='follow' and '已关注' in body):return {'state':'confirmed'}
                return {'state':'uncertain','message':'操作已点击，但页面未明确确认，请核对后再操作'}
            if action in {'comment','like_comment'}:
                if not re.fullmatch(r'\d+',target):raise ValueError('评论目标必须是作品ID')
                await self.goto(page,'https://www.douyin.com/video/'+target,account);await self.gate(page,account)
                entry=page.locator('[data-e2e="comment-icon"]').filter(visible=True)
                if await entry.count()==1:await entry.click();await page.wait_for_timeout(800)
                if action=='like_comment':
                    if not re.fullmatch(r'\d+',comment_id):raise ValueError('需要明确评论ID')
                    item=page.locator('[data-cid="'+comment_id+'"],[data-comment-id="'+comment_id+'"]')
                    control=item.locator('[data-e2e*="like"],button[aria-label*="赞"]')
                    if await control.count()!=1:raise Unsupported('无法唯一定位该评论的点赞控件，未执行')
                    if await control.get_attribute('aria-pressed')=='true':return {'state':'already_applied'}
                    await control.click();await page.wait_for_timeout(1000)
                    return {'state':'confirmed' if await control.get_attribute('aria-pressed')=='true' else 'uncertain'}
                if not text.strip():raise ValueError('评论内容不能为空')
                edit=page.get_by_placeholder(re.compile('留下.*评论|善语结善缘|有爱评论|友善.*评论')).filter(visible=True)
                if await edit.count()!=1:raise Unsupported('无法唯一定位评论输入框，未发布')
                await edit.fill(text)
                submit=page.get_by_role('button',name=re.compile('^(发送|发布)$')).filter(visible=True)
                if await submit.count()!=1:raise Unsupported('无法唯一定位评论发送按钮，未发布')
                await submit.click();await page.wait_for_timeout(1200)
                return {'state':'uncertain','message':'评论已提交，需要核对平台回执，禁止自动重发'}
            if action not in {'message','share','send_image','send_video'}:raise Unsupported('该账号动作尚无已验证的网页控件适配')
            await self.goto(page,'https://www.douyin.com/',account);trigger=page.get_by_text('消息',exact=True)
            if await trigger.count()!=1:raise Unsupported('无法唯一定位消息入口')
            await trigger.click();await page.wait_for_timeout(1000)
            person=page.get_by_text(target,exact=True)
            if await person.count()!=1:raise Unsupported('无法唯一定位联系人，未发送')
            await person.click();await page.wait_for_timeout(800)
            if file_path:
                upload=page.locator('input[type=file]')
                if await upload.count()!=1:raise Unsupported('当前会话无可识别的附件上传入口')
                await upload.set_input_files(file_path)
            else:
                edit=page.locator('[contenteditable=true]')
                if await edit.count()!=1:raise Unsupported('无法唯一定位会话输入框，未发送')
                if action=='share' and text.isdigit():text='https://www.douyin.com/video/'+text
                if not text.strip():raise ValueError('消息内容不能为空')
                await edit.fill(text)
            send=page.get_by_role('button',name='发送',exact=True)
            if await send.count()!=1:raise Unsupported('无法唯一定位发送按钮，未发送')
            await send.click();await page.wait_for_timeout(1200)
            return {'state':'uncertain','message':'已触发发送，需要核对回执；禁止自动重发'}
    async def close(self):
        await self.search_cache.close()
        for ctx,_ in list(self.contexts.values()):await ctx.close()
        if self.pw:await self.pw.stop()
