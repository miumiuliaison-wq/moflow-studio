import asyncio,json,os,socket
from pathlib import Path
import pytest,respx,httpx
from backend.core import Store,Settings,ModelRequired,StoragePaused,SecretVault,words,clean
from backend.engine import Engine
from backend.platform import normalize

@pytest.fixture
def db(tmp_path):
    store=Store(tmp_path/'test.sqlite3');settings=Settings(storage_root=str(tmp_path/'media'),reserve_gb=.05);store.set('settings',settings.model_dump());return store

def test_keywords_preserve_phrases():assert words('甜妹 舞蹈，御姐\n甜妹 舞蹈,  御姐')==['甜妹 舞蹈','御姐']
def test_windows_names():assert clean('CON')=='_CON' and '/' not in clean('a/b')
def test_job_roundtrip(db):
    job=db.job('download',{'input':'123'});assert db.list('jobs')[0]['state']=='queued';db.update_job(job,'paused','test');assert db.list('jobs')[0]['state']=='paused'
def test_real_metadata_only():
    rows=normalize({'data':[{'aweme_id':'123','desc':'作品','video':{'duration':9000,'width':720,'height':1280,'play_addr':{'url_list':['https://v.douyinvod.com/test']}}}]})
    assert rows[0]['duration']==9 and rows[0]['id']=='123' and rows[0]['urls']==['https://v.douyinvod.com/test']
    assert normalize({'message':'登录后继续'})==[]
def test_quota_pause(db):
    db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',('x','x','video','x','hash',9*1024**3,'2026-01-01',0,'{}'))
    with pytest.raises(StoragePaused):db.check_space()
def test_dpapi(tmp_path,monkeypatch):
    vault=SecretVault();vault.path=tmp_path/'vault.json';vault.set('test','do-not-print-secret');assert vault.get('test')=='do-not-print-secret';assert 'do-not-print-secret' not in vault.path.read_text();vault.set('test','');assert vault.get('test')==''
async def test_no_model_does_not_fake_analysis(db):
    engine=Engine(db)
    with pytest.raises(ModelRequired):await engine.models.plan('下载3个视频')
async def test_actions_require_scoped_rule(db,monkeypatch):
    engine=Engine(db);calls=[]
    async def action(*args):calls.append(args);return {'state':'confirmed'}
    monkeypatch.setattr(engine.platform,'action',action)
    out=await engine.perform_action('default','message','Alice','你好',request_id='one');assert out['state']=='draft';assert not calls
    cfg={'account':'default','targets':['Alice'],'allowed_actions':['message'],'daily_limit':1}
    db.execute('INSERT INTO rules VALUES(?,?,?,?,?)',('rule','test',1,json.dumps(cfg),'2026-01-01'))
    out=await engine.perform_action('default','message','Bob','你好','rule',request_id='two');assert out['state']=='draft' and not calls
    out=await engine.perform_action('default','message','Alice','你好','rule',request_id='three');assert out['state']=='confirmed' and len(calls)==1
    with pytest.raises(Exception):await engine.perform_action('default','message','Alice','你好','rule',request_id='four')
async def test_uncertain_send_not_retried(db,monkeypatch):
    engine=Engine(db);calls=[]
    async def action(*args):calls.append(args);return {'state':'uncertain'}
    monkeypatch.setattr(engine.platform,'action',action)
    first=await engine.perform_action('default','message','Alice','hi',manual=True,request_id='same')
    again=await engine.perform_action('default','message','Alice','hi',manual=True,request_id='same')
    assert first['state']==again['state']=='uncertain' and len(calls)==1 and again['replayed']
async def test_transfer_resume_and_ignored_range(db,tmp_path,monkeypatch):
    engine=Engine(db);job=db.job('download',{});db.update_job(job,'running');part=tmp_path/'test.part';part.write_bytes(b'abc')
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('8.8.8.8',443))])
    with respx.mock() as router:
        endpoint=router.get('https://v.douyinvod.com/video').mock(return_value=httpx.Response(206,content=b'def',headers={'content-range':'bytes 3-5/6'}))
        await engine.transfer('https://v.douyinvod.com/video',part,'default',job);assert part.read_bytes()==b'abcdef';assert endpoint.calls[0].request.headers['range']=='bytes=3-'
    with respx.mock() as router:
        router.get('https://v.douyinvod.com/video').mock(return_value=httpx.Response(200,content=b'new'))
        await engine.transfer('https://v.douyinvod.com/video',part,'default',job);assert part.read_bytes()==b'new'
async def test_private_or_unknown_media_url_rejected(db,tmp_path):
    engine=Engine(db);job=db.job('download',{})
    for url in ['http://127.0.0.1/test','https://evil.example/test','file:///C:/test']:
        with pytest.raises(ValueError):await engine.transfer(url,tmp_path/'x','default',job)
async def test_corrupt_video_rejected(db,tmp_path):
    file=tmp_path/'broken.mp4';file.write_bytes(b'not a video')
    with pytest.raises(ValueError):await Engine(db).probe(file)
async def test_legacy_import_preserves_files_and_marks_imported(db,tmp_path):
    from PIL import Image
    folder=tmp_path/'old';folder.mkdir();path=folder/'one.png';Image.new('RGB',(30,40)).save(path)
    engine=Engine(db);assert await engine.import_folder(folder)==1;assert await engine.import_folder(folder)==0
    assert path.exists() and db.list('assets')[0]['imported']==1

async def test_checkpoint_resume_does_not_repeat_completed_steps(db,tmp_path,monkeypatch):
    import backend.engine as module
    from backend.ai import Plan
    monkeypatch.setattr(module,'ROOT',tmp_path)
    engine=Engine(db);calls={'search':0,'download':0}
    async def search(*args,**kwargs):calls['search']+=1;return [{'id':'123456789'}]
    async def download(payload,identifier):
        calls['download']+=1
        if calls['download']==1:raise ModelRequired('模拟可恢复中断')
        db.update_job(identifier,'completed','done',1,{'asset_ids':['saved']});return ['saved']
    monkeypatch.setattr(engine.platform,'search',search);monkeypatch.setattr(engine,'download',download)
    await engine.start()
    try:
        run=await engine.create_run(Plan(title='resume',steps=[{'tool':'search','keyword':'test'},{'tool':'download'}]))
        await engine.active['run:'+run]
        assert db.list('runs')[0]['state']=='paused' and db.list('jobs')[0]['state']=='paused'
        await engine.execute_run(run,True)
        assert db.list('runs')[0]['state']=='completed' and calls=={'search':1,'download':2} and len(db.list('jobs'))==1
    finally:await engine.stop()

def test_http_boundaries_and_invalid_upload(db,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    import backend.app as module,backend.engine as em
    monkeypatch.setattr(module,'db',db);monkeypatch.setattr(module,'engine',Engine(db));monkeypatch.setattr(em,'ROOT',tmp_path)
    with TestClient(module.app,raise_server_exceptions=False) as client:
        assert client.post('/api/v1/downloads',json={'inputs':['123456789']}).status_code==401
        client.get('/')
        assert client.post('/api/v1/downloads',json={'inputs':['123456789']},headers={'Origin':'https://evil.example'}).status_code==403
        assert client.get('/api/v1/health',headers={'Host':'evil.example'}).status_code==403
        assert client.get('/mcp/').status_code==401
        response=client.post('/api/v1/assets/upload',files={'files':('bad.jpg',b'broken','image/jpeg')})
        assert response.status_code==400 and '图片损坏' in response.json()['detail']
        assert client.put('/api/v1/rules/r',json={'name':'invalid','template':'reply','enabled':True,'targets':[]}).status_code==400

async def test_clear_history_preserves_library_and_prompt_metadata(db,monkeypatch):
    import backend.app as app
    from backend.mcp_api import call
    monkeypatch.setattr(app,'db',db);monkeypatch.setattr(app,'engine',Engine(db))
    db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',('a','123456789','video','some.mp4','sha',100,'2026-10-21',0,json.dumps({'analysis':{'tags':['舞蹈']},'prompt':{'positive':'test'}})))
    result=await call('douyin_clear_download_history',{'confirm':True})
    assert json.loads(result[0].text)['success'] and db.list('assets')[0]['payload']['analysis']['tags']==['舞蹈']
    result=await call('douyin_get_download_history',{})
    assert json.loads(result[0].text)['data']==[]

async def test_attachment_only_uses_registered_files(db,tmp_path,monkeypatch):
    engine=Engine(db);path=tmp_path/'image.png';path.write_bytes(b'fixture');seen=[]
    async def action(*args,**kwargs):seen.append(kwargs);return {'state':'uncertain'}
    monkeypatch.setattr(engine.platform,'action',action)
    db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',('image','upload:x','image',str(path),'sha',7,'2026-10-22',0,'{}'))
    result=await engine.perform_action('default','send_image','Alice','',manual=True,request_id='a',asset_id='image')
    assert result['state']=='uncertain' and seen[0]['file_path']==str(path)
    with pytest.raises(ValueError):await engine.perform_action('default','send_video','Alice','',manual=True,request_id='b',asset_id='image')

async def test_restart_finishes_checkpoint_without_repeating_effect(db,tmp_path,monkeypatch):
    import backend.engine as module
    from backend.ai import Plan
    monkeypatch.setattr(module,'ROOT',tmp_path)
    calls=[]
    async def search(*args,**kwargs):calls.append('search');return []
    first=Engine(db);monkeypatch.setattr(first.platform,'search',search)
    await first.start()
    run=await first.create_run(Plan(title='restart',steps=[{'tool':'search','keyword':'test'}]))
    await first.active['run:'+run];await first.stop()
    # Simulate a crash after the graph checkpoint committed, before its final status write.
    db.execute("UPDATE runs SET state='running' WHERE id=?",(run,))
    second=Engine(db);monkeypatch.setattr(second.platform,'search',search)
    await second.start()
    try:
        await second.active['run:'+run]
        assert db.list('runs')[0]['state']=='completed' and calls==['search']
    finally:await second.stop()

async def test_native_read_serializes_body_and_keeps_session_cookie_out_of_url(monkeypatch):
    import backend.native_api as api
    from urllib.parse import parse_qs,urlsplit
    seen=[]
    class Response:
        status_code=200
        def json(self):return {'status_code':0,'collects_list':None,'has_more':0}
    class Session:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,method,url,**kwargs):seen.append((method,url,kwargs));return Response()
    monkeypatch.setattr(api,'AsyncSession',Session)
    fp={'ua':'Mozilla/5.0 Chrome/146.0.0.0','platform':'Win32','language':'zh-CN','cores':8,'memory':8,'width':1360,'height':900}
    data=await api.read('collected',{'cursor':0,'count':20},fp,{'sessionid':'private-value'})
    method,url,kwargs=seen[0]
    assert method=='POST' and urlsplit(url).hostname=='www.douyin.com'
    assert parse_qs(kwargs['data'])=={'cursor':['0'],'count':['20']}
    assert 'private-value' not in url and kwargs['cookies']['sessionid']=='private-value'
    assert kwargs['allow_redirects'] is False and data['has_more']==0
    with pytest.raises(ValueError):await api.read('https://evil.example',{},fp,{})
    assert len(seen)==1

async def test_api_verification_does_not_resume_from_stale_cookie(db,monkeypatch):
    engine=Engine(db)
    class Context:
        async def cookies(self,*args):return [{'name':'sessionid','value':'still-present'}]
    ctx=Context();engine.platform.contexts['default']=(ctx,False)
    db.execute('UPDATE accounts SET state=?,payload=? WHERE id=?',('waiting_login','{"waiting_api":true}','default'))
    status=await engine.platform.status('default')
    assert status['state']=='waiting_login'

async def test_search_verify_check_is_login_pause_not_zero_candidates(monkeypatch):
    import backend.native_api as api
    from backend.core import LoginRequired
    requests=[]
    class Response:
        status_code=200
        url='https://www.douyin.com/aweme/v1/web/general/search/single/?keyword=test&count=10'
        def json(self):return {'status_code':0,'data':[],'has_more':0,'search_nil_info':{'search_nil_type':'verify_check','search_nil_item':'verify_check'}}
    class Session:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,*args,**kwargs):requests.append(args);return Response()
    monkeypatch.setattr(api,'AsyncSession',Session)
    fp={'ua':'Mozilla/5.0 Chrome/146.0.0.0','platform':'Win32','language':'zh-CN','cores':8,'memory':8,'width':1360,'height':900}
    with pytest.raises(LoginRequired,match='verify_check'):await api.read('search',{'keyword':'test','count':3},fp,{}, {})
    assert len(requests)==1

def test_verification_recovery_requires_matching_real_read(db):
    engine=Engine(db);platform=engine.platform
    platform.waiting('default','search','舞蹈','https://www.douyin.com/search/test')
    platform.verified('default','detail')
    platform.verified('default','search','穿搭')
    assert db.list('accounts')[0]['state']=='waiting_login'
    platform.verified('default','search','舞蹈')
    account=db.list('accounts')[0]
    assert account['state']=='connected' and 'waiting_api' not in account['payload']

async def test_opening_login_does_not_claim_verification_succeeded(db,monkeypatch):
    platform=Engine(db).platform;visited=[]
    class Page:
        async def goto(self,url,**kwargs):visited.append(url)
        async def bring_to_front(self):pass
    async def context(*args):return object()
    async def page(*args):return Page()
    monkeypatch.setattr(platform,'context',context);monkeypatch.setattr(platform,'page',page)
    platform.waiting('default','search','舞蹈','https://www.douyin.com/search/test?type=video')
    await platform.login()
    assert visited==['https://www.douyin.com/search/test?type=video']
    assert db.list('accounts')[0]['payload']['waiting_api'] is True

async def test_workflow_resumes_verification_without_repeating_finished_step(db,tmp_path,monkeypatch):
    import backend.engine as module
    from backend.ai import Plan
    from backend.core import LoginRequired
    monkeypatch.setattr(module,'ROOT',tmp_path)
    engine=Engine(db);calls=[]
    async def search(keyword,*args,**kwargs):
        calls.append(keyword)
        if calls.count('blocked')==1 and keyword=='blocked':raise LoginRequired('verify_check')
        return []
    async def status(*args):return {'state':'connected'}
    monkeypatch.setattr(engine.platform,'search',search);monkeypatch.setattr(engine.platform,'status',status)
    await engine.start()
    try:
        run=await engine.create_run(Plan(title='resume',steps=[{'tool':'search','keyword':'first'},{'tool':'search','keyword':'blocked'}]))
        await engine.active['run:'+run]
        assert db.list('runs')[0]['state']=='waiting_login'
        engine.platform.contexts['default']=(object(),True)
        db.set('automation_paused',True);await engine.tick()
        assert 'run:'+run not in engine.active
        db.set('automation_paused',False);await engine.tick();await engine.active['run:'+run]
        assert db.list('runs')[0]['state']=='completed' and calls==['first','blocked','blocked']
    finally:
        engine.platform.contexts.clear();await engine.stop()
