"""Small Douyin read adapter; vendored request primitives never receive cloud credentials."""
from __future__ import annotations
import asyncio,json,re
from urllib.parse import urlencode,urlsplit,parse_qs
from curl_cffi.requests import AsyncSession
from .core import LoginRequired,Unsupported
from .vendor.dtk_native.abogus import ABogus,browser_info_from_screen
from .vendor.dtk_native.websign import encode_pairs,pick_uifid,sign

PATHS={
 'detail':('/aweme/v1/web/aweme/detail/','GET'),
 'profile':('/aweme/v1/web/user/profile/other/','GET'),
 'posts':('/aweme/v1/web/aweme/post/','GET'),
 'liked':('/aweme/v1/web/aweme/favorite/','GET'),
 'collected':('/aweme/v1/web/aweme/listcollection/','POST'),
 'folders':('/aweme/v1/web/collects/list/','GET'),
 'folder_videos':('/aweme/v1/web/collects/video/list/','GET'),
 'mix':('/aweme/v1/web/mix/aweme/','GET'),
 'comments':('/aweme/v1/web/comment/list/','GET'),
 'replies':('/aweme/v1/web/comment/list/reply/','GET'),
 'current_user':('/aweme/v1/web/query/user/','GET'),
 'following':('/aweme/v1/web/user/following/list/','GET'),
 'search':('/aweme/v1/web/general/search/single/','GET'),
 'search_users':('/aweme/v1/web/discover/search/','GET'),
}

FINGERPRINT_JS='''() => ({ua:navigator.userAgent,platform:navigator.platform,language:navigator.language,cores:navigator.hardwareConcurrency,memory:navigator.deviceMemory||8,width:screen.width,height:screen.height})'''

def base_params(fp):
    version=(re.search(r'(?:Chrome|Edg)/([\d.]+)',fp['ua']) or [None,'146.0.0.0'])[1]
    return {'device_platform':'webapp','aid':'6383','channel':'channel_pc_web','pc_client_type':'1','pc_libra_divert':'Windows','version_code':'290100','version_name':'29.1.0','update_version_code':'170400','cookie_enabled':'true','screen_width':str(fp['width']),'screen_height':str(fp['height']),'browser_language':fp['language'],'browser_platform':fp['platform'],'browser_name':'Chrome','browser_version':version,'browser_online':'true','engine_name':'Blink','engine_version':version,'os_name':'Windows','os_version':'10','cpu_core_num':str(fp['cores']),'device_memory':str(fp['memory']),'platform':'PC','downlink':'10','effective_type':'4g','round_trip_time':'0'}

def signed_query(params,fp,cookies,body=''):
    pairs=[(str(k),str(v)) for k,v in params.items() if v is not None]
    token=cookies.get('msToken')
    if token and not any(k=='msToken' for k,_ in pairs):pairs.append(('msToken',token))
    query=encode_pairs(pairs)
    bogus=ABogus(fp['ua'],browser_info=browser_info_from_screen(fp['width'],fp['height'],fp['platform'])).get_value(query,body=body)
    pairs.append(('a_bogus',bogus));headers={}
    if cookies.get('s_v_web_id'):pairs.extend((k,cookies['s_v_web_id']) for k in ['verifyFp','fp'])
    uifid=pick_uifid(cookies)
    if uifid:query,_,headers=sign(pairs,uifid)
    else:query=encode_pairs(pairs)
    return query,headers

async def read(kind,params,fp,cookies,trace=None,*,attempts=None,timeout=25):
    if kind not in PATHS:raise ValueError('未注册的读取接口')
    path,method=PATHS[kind];business={k:v for k,v in params.items() if v is not None}
    if kind in {'search','search_users'}:
        business={'search_channel':'aweme_user_web' if kind=='search_users' else 'aweme_general','enable_history':'1','search_source':'switch_tab','query_correct_type':'1','is_filter_search':'0','from_group_id':'','disable_rs':'0','offset':0,'count':20,'need_filter_settings':'0','list_type':'single',**business}
        if kind=='search':business['pc_search_top_1_params']='{"enable_ai_search_top_1":1}'
        business['count']=max(10,int(business['count']))
    body=urlencode(business) if method=='POST' else ''
    query,sign_headers=signed_query({**base_params(fp),**({} if method=='POST' else business)},fp,cookies,body)
    headers={'User-Agent':fp['ua'],'Referer':'https://www.douyin.com/','Accept':'application/json',**sign_headers}
    if body:headers['Content-Type']='application/x-www-form-urlencoded'
    async with AsyncSession(impersonate='chrome',trust_env=False,timeout=timeout) as client:
        for attempt in range(attempts if attempts is not None else 3 if kind in {'search','search_users'} else 1):
            if attempt:
                await asyncio.sleep(attempt*3)
                query,sign_headers=signed_query({**base_params(fp),**business},fp,cookies)
                headers.update(sign_headers)
            response=await client.request(method,'https://www.douyin.com'+path+'?'+query,headers=headers,cookies=cookies,data=body or None,allow_redirects=False)
            if trace is not None:
                sent=parse_qs(urlsplit(response.url).query)
                trace.update({'kind':kind,'fingerprint':fp,'attempt':attempt+1,'http_status':response.status_code,'request':{k:sent.get(k) for k in ['keyword','count','search_channel','search_source','pc_search_top_1_params']}})
            if response.status_code in {401,403,429}:raise LoginRequired('抖音请求需要重新登录、验证或冷却，请打开账号窗口处理')
            if response.status_code!=200:raise Unsupported('抖音读取接口返回HTTP '+str(response.status_code))
            try:data=response.json()
            except (ValueError,json.JSONDecodeError) as error:raise Unsupported('抖音读取接口返回空内容或非JSON') from error
            if not isinstance(data,dict):raise Unsupported('抖音读取接口返回了不支持的结构')
            if trace is not None:trace['response']={'keys':list(data),'status_code':data.get('status_code'),'data_count':len(data.get('data') or []),'user_count':len(data.get('user_list') or []),'nil_info':data.get('search_nil_info'),'has_more':data.get('has_more')}
            if data.get('status_code',0)!=0:raise LoginRequired('抖音读取返回状态 '+str(data.get('status_code'))+'，请检查登录或验证')
            nil=data.get('search_nil_info') or {}
            if isinstance(nil,dict) and any(re.search(r'verify|captcha|login',str(nil.get(k,'')),re.I) for k in ['search_nil_type','search_nil_item']):
                raise LoginRequired('抖音搜索要求验证（'+str(nil.get('search_nil_type','verify'))+'），请在账号浏览器中完成验证后继续')
            if kind not in {'search','search_users'} or data.get('data') or data.get('user_list') or data.get('aweme_list') or data.get('search_nil_info'):return data
        return data
