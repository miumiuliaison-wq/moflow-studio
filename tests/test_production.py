import asyncio,hashlib,json,socket,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx,pytest,respx
from PIL import Image
from backend.core import Store,Settings,ModelConfig,SecretVault,Waiting,ModelRequired,redact
from backend.engine import Engine,tool_path
from backend.ai import Prompt,Analysis
from backend.billing import Ledger,BudgetRequired,inference_budget
from backend.production_config import ProductionRequest,Binding,ProductionSettings
from backend.runninghub_api import normalize_schema,FixedPrice,UncertainSubmission

@pytest.fixture
def engine(tmp_path,monkeypatch):
    import backend.engine as em,backend.production as pm
    monkeypatch.setattr(em,'ROOT',tmp_path);monkeypatch.setattr(pm,'ROOT',tmp_path)
    db=Store(tmp_path/'test.sqlite3');db.set('settings',Settings(storage_root=str(tmp_path/'media'),reserve_gb=.05).model_dump())
    return Engine(db)

def asset(engine,tmp_path,kind='image',name='fixture'):
    path=tmp_path/(name+('.png' if kind=='image' else '.mp4'))
    if kind=='image':Image.new('RGB',(120,200),'white').save(path)
    else:path.write_bytes(b'video-fixture')
    engine.db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',(name,name,kind,str(path),name,path.stat().st_size,'2026-10-23',0,'{}'))
    return name

def test_deepseek_cannot_send_key_to_custom_host():
    cfg=ModelConfig(provider='deepseek',base_url='https://other.example',model='')
    assert cfg.base_url=='https://api.deepseek.com' and cfg.model=='deepseek-flash'
    assert 'sk-' not in redact('bad sk-this-is-a-fixture-key-123456 ?apiKey=private')
    assert 'private' not in redact('?apiKey=private')

async def test_cloud_models_return_api_ids_not_display_names(engine,tmp_path,monkeypatch):
    engine.db.set('settings',Settings(storage_root=str(tmp_path/'media'),text_model=ModelConfig(provider='deepseek')).model_dump())
    monkeypatch.setattr(SecretVault,'get',lambda *a:'test-key')
    with respx.mock() as router:
        router.get('https://api.deepseek.com/models').mock(return_value=httpx.Response(200,json={'data':[{'id':'deepseek-flash','name':'DeepSeek-V4.1-Flash'},{'name':'missing-id'}]}))
        assert await engine.models.list()==['deepseek-flash']

def test_budget_atomic_across_workers(engine):
    l=engine.production.ledger;l.configure('batch','deepseek','USD',1)
    def reserve(_):
        try:return l.reserve('batch','deepseek','USD',.6)
        except BudgetRequired:return None
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(reserve,range(2)))
    assert sum(bool(x) for x in results)==1
    assert l.summary('batch')[0]['held']==.6
    l.settle(next(x for x in results if x),.1)
    assert l.reserve('batch','deepseek','USD',.9)

def test_unknown_fee_held_and_units_never_mixed(engine):
    l=engine.production.ledger;l.configure('batch','runninghub','RH币',10)
    r=l.reserve('batch','runninghub','RH币',8);l.settle(r,state='uncertain')
    with pytest.raises(BudgetRequired):l.reserve('batch','runninghub','RH币',3)
    with pytest.raises(BudgetRequired):l.reserve('batch','runninghub','USD',1)
    with pytest.raises(ValueError):l.configure('batch','runninghub','USD',20)

def test_idempotency_snapshot_and_missing_budget(engine):
    p=engine.production;body=ProductionRequest(request_id='same');first=p.create(body);again=p.create(body)
    assert first['id']==again['id'] and len(p.list())==1
    engine.db.set('production_preferences',{'wardrobe':'新档案','version':9})
    assert p.get(first['id'])['payload']['preferences']['version']==1
    with pytest.raises(ValueError):p.create(ProductionRequest(request_id='same',count=2))

async def test_missing_budget_pauses_before_any_research(engine,monkeypatch):
    calls=[]
    async def search(*a,**k):calls.append(1);return []
    monkeypatch.setattr(engine.platform,'recommendation_search',search)
    row=engine.production.create(ProductionRequest(request_id='zero'))
    await engine.start()
    try:
        await engine.production.tasks[row['id']]
        assert engine.production.get(row['id'])['state']=='waiting_budget' and not calls
    finally:await engine.stop()

async def test_missing_generation_key_pauses_before_model_or_research(engine,monkeypatch):
    calls=[]
    monkeypatch.setattr(SecretVault,'get',lambda *a:'')
    async def forbidden(*a,**k):calls.append(1);raise AssertionError('must preflight first')
    monkeypatch.setattr(engine.models,'request',forbidden)
    monkeypatch.setattr(engine.production,'research',forbidden)
    row=engine.production.create(ProductionRequest(request_id='no-generation-key',ds_budget=1,rh_budget=10))
    await engine.start()
    try:
        await engine.production.tasks[row['id']]
        assert engine.production.get(row['id'])['state']=='waiting_model' and not calls
        assert all(b['held']==b['spent']==0 for b in engine.production.get(row['id'])['budget'])
    finally:await engine.stop()

def test_two_same_label_media_fields_stay_distinct():
    nodes=normalize_schema({'nodeInfoList':[{'nodeId':'9223372036854775807','fieldName':'image','fieldType':'IMAGE','description':'图片'}, {'nodeId':'2','fieldName':'image','fieldType':'IMAGE','description':'图片'}]})
    assert nodes[0]['key']=='9223372036854775807:image' and nodes[0]['key']!=nodes[1]['key']
    with pytest.raises(Waiting):normalize_schema({'nodeInfoList':[{'nodeId':'1','fieldName':'x'},{'nodeId':'1','fieldName':'x'}]})

def test_estimate_and_stale_schema_never_authorize(engine):
    rh=engine.production.rh;engine.db.set('rh_schema:123',{'hash':'current'})
    assert not rh.price('123','current')['verified']
    with pytest.raises(ValueError):rh.save_fixed_price('123',FixedPrice(amount=2,unit='RH币',source='https://www.runninghub.ai/ai-detail/123',schema_hash='old',confirmed_fixed=True))
    with pytest.raises(ValueError):rh.save_fixed_price('123',FixedPrice(amount=2,unit='RH币',source='https://evil.example',schema_hash='current',confirmed_fixed=True))
    saved=rh.save_fixed_price('123',FixedPrice(amount=2,unit='RH币',source='https://www.runninghub.ai/ai-detail/123',schema_hash='current',confirmed_fixed=True))
    assert saved['verified'] and not rh.price('123','changed')['verified']

async def test_uncertain_paid_submission_never_reposts(engine,monkeypatch):
    rh=engine.production.rh;calls=[]
    engine.db.execute('INSERT INTO production_rh_tasks VALUES(?,?,?,?,?,?,?)',('task','batch','123','uncertain','','{}','now'))
    async def request(*a,**k):calls.append(1)
    monkeypatch.setattr(rh,'request',request)
    with pytest.raises(UncertainSubmission):await rh.generate('batch','task',Binding(app_id='123'),{}, {},lambda:None)
    assert calls==[]

def prepare_binding(engine):
    schema={'app_id':'123','hash':'hash','nodes':[{'key':'1:prompt','nodeId':'1','fieldName':'prompt','label':'prompt','type':'STRING','default':'','options':None,'min':None,'max':None}]}
    engine.db.set('rh_schema:123',schema);engine.production.rh.save_fixed_price('123',FixedPrice(amount=2,unit='RH币',source='https://www.runninghub.ai/ai-detail/123',schema_hash='hash',confirmed_fixed=True))
    return schema,Binding(app_id='123',schema_hash='hash',fields={'positive':'1:prompt'})

async def test_lost_receipt_is_durable_and_single_post(engine,monkeypatch):
    rh=engine.production.rh;schema,binding=prepare_binding(engine);engine.production.ledger.configure('batch','runninghub','RH币',10);calls=[]
    async def read(*a):return schema
    async def fail(*a,**k):calls.append(a[0]);raise Waiting('network timeout')
    monkeypatch.setattr(rh,'schema',read);monkeypatch.setattr(rh,'request',fail)
    with pytest.raises(Waiting):await rh.generate('batch','task',binding,{'positive':'test'}, {},lambda:None)
    with pytest.raises(UncertainSubmission):await rh.generate('batch','task',binding,{'positive':'test'}, {},lambda:None)
    assert calls==['/task/openapi/ai-app/run'] and rh.row('task')['state']=='uncertain'
    assert engine.production.ledger.summary('batch')[0]['held']==2

async def test_restart_queries_original_task_and_only_download_retries(engine,monkeypatch):
    rh=engine.production.rh;calls=[]
    payload={'outputs':[{'fileUrl':'fixture','fileType':'png'}],'charge_id':'none','price':{'unit':'RH币'}}
    engine.db.execute('INSERT INTO production_rh_tasks VALUES(?,?,?,?,?,?,?)',('task','batch','123','download_ready','9876543210123456789',json.dumps(payload),'now'))
    async def downloaded(row,check):calls.append(row['task_id']);return ['asset']
    async def request(*a,**k):raise AssertionError('must not submit or poll a completed task')
    monkeypatch.setattr(rh,'download_results',downloaded);monkeypatch.setattr(rh,'request',request)
    assert await rh.generate('batch','task',Binding(app_id='123'),{},{},lambda:None)==['asset']
    assert calls==['9876543210123456789']

async def test_deepseek_images_json_usage_and_shared_key(engine,tmp_path,monkeypatch):
    engine.db.set('settings',Settings(storage_root=str(tmp_path/'media'),reserve_gb=.05,text_model=ModelConfig(provider='deepseek'),vision_model=ModelConfig(provider='deepseek')).model_dump())
    monkeypatch.setattr(SecretVault,'get',lambda self,key:'test-shared-key' if key=='deepseek' else '')
    image=tmp_path/'input.png';Image.new('RGB',(100,100)).save(image)
    l=engine.production.ledger;l.configure('batch','deepseek','USD',1);token=inference_budget.set('batch')
    answer={'positive':'这是一张用于测试的完整成年人物摄影图像，单人全身。','negative':['无模糊'],'uncertainties':[]}
    try:
        with respx.mock() as router:
            route=router.post('https://api.deepseek.com/chat/completions').mock(return_value=httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(answer)}}],'usage':{'prompt_tokens':100,'completion_tokens':50}}))
            out=await engine.models.request('分析图片',Prompt,[image],'vision');sent=json.loads(route.calls[0].request.content)
            assert out.negative==['无模糊'] and sent['model']=='deepseek-flash' and sent['thinking']['type']=='disabled'
            assert sent['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
            assert route.calls[0].request.headers['authorization']=='Bearer test-shared-key'
        assert l.summary('batch')[0]['spent']>0 and l.summary('batch')[0]['held']==0
        assert 'test-shared-key' not in json.dumps(engine.db.get('last_inference'))
    finally:inference_budget.reset(token)

async def test_cloud_concurrency_two_and_local_serial(engine,tmp_path,monkeypatch):
    engine.db.set('settings',Settings(storage_root=str(tmp_path/'media'),text_model=ModelConfig(provider='deepseek')).model_dump())
    monkeypatch.setattr(SecretVault,'get',lambda *a:'test-key')
    current=0;peak=0
    async def response(request):
        nonlocal current,peak
        current+=1;peak=max(peak,current);await asyncio.sleep(.03);current-=1
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'positive':'成年女性单人全身摄影画面，自然光线，清晰。','negative':['无模糊']})}}],'usage':{}})
    with respx.mock() as router:
        router.post('https://api.deepseek.com/chat/completions').mock(side_effect=response)
        await asyncio.gather(*(engine.models.request('test',Prompt) for _ in range(4)))
    assert peak==2

async def test_timeout_holds_budget_and_does_not_immediately_retry(engine,tmp_path,monkeypatch):
    engine.db.set('settings',Settings(storage_root=str(tmp_path/'media'),text_model=ModelConfig(provider='deepseek')).model_dump());monkeypatch.setattr(SecretVault,'get',lambda *a:'test-key')
    l=engine.production.ledger;l.configure('batch','deepseek','USD',1);token=inference_budget.set('batch')
    try:
        with respx.mock() as router:
            route=router.post('https://api.deepseek.com/chat/completions').mock(side_effect=httpx.ReadTimeout('fixture'))
            with pytest.raises(ModelRequired):await engine.models.request('test',Prompt)
            assert route.call_count==1
        assert l.summary('batch')[0]['held']>0
    finally:inference_budget.reset(token)

def test_visual_unknowns_fail_closed(engine):
    good={'people':1,'face_visible':'yes','clear':'yes','matches':'yes','full_body':'yes','eye_level':'yes'}
    assert engine.production.accepts(good,True)
    for key,value in [('people',2),('face_visible','no'),('clear','no'),('full_body','no'),('matches','unknown')]:
        assert not engine.production.accepts({**good,key:value},True)
    assert not engine.production.accepts({**good,'eye_level':'no'})

async def test_real_ffmpeg_export_preserves_ratio_and_audio(engine,tmp_path):
    if not tool_path('ffmpeg'):pytest.skip('FFmpeg absent')
    p=engine.production
    # Actual media, not a mocked successful decode.
    video=tmp_path/'generated.mp4';ref=tmp_path/'reference.mp4'
    await p.command([tool_path('ffmpeg'),'-y','-v','error','-f','lavfi','-i','color=c=gray:s=320x240:r=5:d=8','-c:v','libx264','-pix_fmt','yuv420p',str(video)])
    await p.command([tool_path('ffmpeg'),'-y','-v','error','-f','lavfi','-i','color=c=black:s=320x240:r=5:d=8','-f','lavfi','-i','sine=frequency=440:duration=8','-c:v','libx264','-c:a','aac','-shortest',str(ref)])
    for name,path in [('motion',video),('ref',ref)]:engine.db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',(name,name,'video',str(path),name,path.stat().st_size,'2026-10-23',0,'{}'))
    output=await p.export('fixture',0,{'motion':'motion','reference':'ref'},{'duration':8},lambda:None)
    probe=await engine.probe(p.asset(output)['path'],True);v=next(s for s in probe['streams'] if s['codec_type']=='video')
    assert v['width']*16==v['height']*9 and any(s['codec_type']=='audio' for s in probe['streams'])
    assert float(probe['format']['duration'])>=8

def test_mcp_disabled_and_bounded(engine):
    p=engine.production
    with pytest.raises(ValueError):p.create(ProductionRequest(request_id='mcp'),mcp=True)
    engine.db.set('production_settings',ProductionSettings(mcp_enabled=True,mcp_ds_budget=1,mcp_rh_budget=10).model_dump())
    with pytest.raises(ValueError):p.create(ProductionRequest(request_id='mcp',count=2,ds_budget=1,rh_budget=10),mcp=True)
    p.create(ProductionRequest(request_id='one',ds_budget=.6,rh_budget=6),mcp=True)
    with pytest.raises(ValueError):p.create(ProductionRequest(request_id='two',ds_budget=.6,rh_budget=6),mcp=True)

async def test_shutdown_resumes_but_explicit_pause_stays_paused(engine,monkeypatch):
    entered=asyncio.Event()
    async def ready(*a):pass
    async def research(*a):entered.set();await asyncio.Event().wait()
    monkeypatch.setattr(engine.production,'preflight',ready)
    monkeypatch.setattr(engine.production,'research',research)
    row=engine.production.create(ProductionRequest(request_id='restart',ds_budget=1,rh_budget=10))
    await engine.start();await asyncio.wait_for(entered.wait(),5);await engine.stop()
    assert engine.production.get(row['id'])['state']=='queued'
    entered.clear();await engine.start();await asyncio.wait_for(entered.wait(),5)
    try:
        await engine.production.action(row['id'],'pause')
        assert engine.production.get(row['id'])['state']=='paused'
    finally:await engine.stop()
    assert engine.production.get(row['id'])['state']=='paused'

async def test_pipeline_resume_keeps_completed_generation(engine,tmp_path,monkeypatch):
    p=engine.production;reference=asset(engine,tmp_path,'video','reference');generated=asset(engine,tmp_path,'image','image');motion=asset(engine,tmp_path,'video','motion')
    events=[];fail_export=True
    async def ready(*a):pass
    async def research(*a):events.append('research');return {'fashion':[],'at':'now'}
    async def choose(*a):return reference
    async def model(*a,**k):return Prompt(positive='成年女性完整全身摄影，白色连衣裙，自然明亮光线。',negative=['无模糊'])
    async def gen(batch,key,binding,values,files,check):events.append(key.split('-')[-2]);return [motion if '-motion-' in key else generated]
    async def analyze(*a,**k):return {'people':1,'full_body':'yes','face_visible':'yes','clear':'yes','matches':'yes','eye_level':'yes'}
    async def export(*a):
        nonlocal fail_export
        if fail_export:fail_export=False;raise StoragePaused('fixture full disk')
        return motion
    from backend.core import StoragePaused
    for name,fn in [('preflight',ready),('research',research),('reference',choose),('analyze',analyze),('export',export)]:monkeypatch.setattr(p,name,fn)
    monkeypatch.setattr(engine.models,'request',model);monkeypatch.setattr(p.rh,'generate',gen)
    row=p.create(ProductionRequest(request_id='resume',ds_budget=1,rh_budget=10,reference_asset=reference))
    await engine.start()
    try:
        await p.tasks[row['id']];assert p.get(row['id'])['state']=='waiting_storage'
        assert p.get(row['id'])['payload']['stage']=='export'
        await p.action(row['id'],'resume');await p.tasks[row['id']]
        assert p.get(row['id'])['state']=='completed' and events==['research','image','motion']
    finally:await engine.stop()
