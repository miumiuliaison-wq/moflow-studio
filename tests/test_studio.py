import asyncio,json,subprocess
from pathlib import Path
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from backend.core import Store,Settings,StoragePaused
from backend.engine import Engine,tool_path
from backend.studio import DraftCreate,DraftUpdate
from backend.studio_api import router

@pytest.fixture
def engine(tmp_path,monkeypatch):
    import backend.studio_api as module
    monkeypatch.setattr(module,'ROOT',tmp_path)
    db=Store(tmp_path/'test.sqlite3');db.set('settings',Settings(storage_root=str(tmp_path/'media'),reserve_gb=.05).model_dump())
    db.execute('INSERT INTO accounts VALUES(?,?,?,?,?)',('default','测试账号','not_logged_in','{}','2026-10-30'))
    e=Engine(db)
    monkeypatch.setattr(e.studio,'environment',lambda:{'issues':[],'stages':{s:{'issues':[],'unit':'RH币'} for s in ['image','dress','motion']}})
    monkeypatch.setattr(e.production,'tick',lambda:None)
    return e

def create(engine,**kwargs):return engine.studio.create(DraftCreate(request_id='create-fixture',**kwargs))

def asset(engine,tmp_path,kind='video',identifier='local'):
    path=tmp_path/(identifier+('.mp4' if kind=='video' else '.png'));path.write_bytes(b'fixture')
    engine.db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',(identifier,identifier,kind,str(path),identifier,path.stat().st_size,'2026',1,'{}'))
    return identifier,path

def test_draft_create_idempotent_without_start_or_charge(engine):
    a=create(engine);b=create(engine)
    assert a['id']==b['id'] and a['version']==1 and not a['ready']
    assert '填写 DeepSeek 预算' in a['issues'] and '填写 RunningHub 预算' in a['issues']
    assert not engine.db.query('SELECT id FROM productions') and not engine.db.query('SELECT id FROM production_charges')

def test_draft_revision_conflict_and_delete(engine):
    d=create(engine);updated=engine.studio.update(d['id'],DraftUpdate(**{**d['inputs'],'title':'修改后的草稿'},expected_version=1))
    assert updated['version']==2 and updated['title']=='修改后的草稿'
    with pytest.raises(HTTPException) as error:engine.studio.update(d['id'],DraftUpdate(**d['inputs'],expected_version=1))
    assert error.value.status_code==409
    with pytest.raises(HTTPException):engine.studio.remove(d['id'],1)
    engine.studio.remove(d['id'],2);assert not engine.studio.list()

async def test_double_start_reuses_existing_production_and_budgets(engine):
    d=create(engine,ds_budget=1,rh_budget=50)
    first,again=await asyncio.gather(engine.studio.start(d['id'],1),engine.studio.start(d['id'],1))
    assert first['id']==again['id'] and len(engine.production.list())==1
    assert engine.studio.get(d['id'])['production_id']==first['id']
    assert len(engine.db.query('SELECT * FROM production_budgets'))==2
    with pytest.raises(HTTPException):engine.studio.update(d['id'],DraftUpdate(**d['inputs'],expected_version=1))
    with pytest.raises(HTTPException):engine.studio.remove(d['id'],1)

async def test_incomplete_card_does_not_hold_ready_card(engine):
    incomplete=create(engine)
    ready=engine.studio.create(DraftCreate(request_id='second-fixture',ds_budget=1,rh_budget=30))
    with pytest.raises(HTTPException):await engine.studio.start(incomplete['id'],1)
    result=await engine.studio.start(ready['id'],1)
    assert result['state']=='queued' and not engine.studio.get(incomplete['id'])['production_id']

def test_reference_download_link_is_idempotent_and_resolves_asset(engine,tmp_path):
    d=create(engine,reference_input='https://www.douyin.com/video/123456789')
    first=engine.studio.reference(d['id'],1);again=engine.studio.reference(d['id'],1)
    assert first['reference_job']['id']==again['reference_job']['id']
    a,path=asset(engine,tmp_path)
    engine.db.update_job(first['reference_job']['id'],'completed','已完成',1,{'asset_ids':[a]})
    resolved=engine.studio.get(d['id']);assert resolved['inputs']['reference_asset']==a
    path.unlink();assert '参考素材文件已移动或删除' in engine.studio.get(d['id'])['issues']

def test_wrong_asset_kind_or_unknown_account_rejected(engine,tmp_path):
    a,_=asset(engine,tmp_path,'image')
    with pytest.raises(ValueError):create(engine,reference_asset=a)
    with pytest.raises(ValueError):create(engine,account='missing')

def test_recovery_after_production_commit_before_draft_link(engine):
    from backend.production_config import ProductionRequest
    d=create(engine,ds_budget=1,rh_budget=30)
    result=engine.production.create(ProductionRequest(request_id='studio:'+d['id'],ds_budget=1,rh_budget=30))
    assert engine.studio.get(d['id'])['production_id']==result['id'] and not engine.studio.get(d['id'])['ready']

async def test_chat_context_resolves_server_project_and_preserves_permissions(engine,monkeypatch):
    from backend.chat import Reply
    d=create(engine,notes='项目要求：单人、平视')
    captured=[]
    async def request(prompt,*a,**k):captured.append(prompt);return Reply(reply='已读取项目要求，暂不生成。')
    monkeypatch.setattr(engine.models,'request',request)
    session=engine.chat.create();m=engine.chat.send(session['id'],'看看这个项目','context-fixture',[],{'kind':'draft','id':d['id']})
    await engine.chat.tasks[m['id']]
    assert 'project_context' in captured[0] and '项目要求：单人、平视' in captured[0]
    assert not engine.production.list() and not engine.db.get('production_settings',{}).get('mcp_enabled',False)

def client_for(engine):
    app=FastAPI();app.include_router(router(engine))
    @app.exception_handler(ValueError)
    async def invalid(request,error):return JSONResponse({'detail':str(error)},400)
    @app.exception_handler(StoragePaused)
    async def space(request,error):return JSONResponse({'detail':str(error)},409)
    return TestClient(app,raise_server_exceptions=False)

def test_real_video_upload_dedup_corruption_and_quota(engine,tmp_path,monkeypatch):
    video=tmp_path/'中文舞蹈.mp4'
    subprocess.run([tool_path('ffmpeg'),'-y','-v','error','-f','lavfi','-i','color=c=gray:s=160x240:d=0.5','-c:v','libx264','-pix_fmt','yuv420p',str(video)],check=True,creationflags=0x08000000)
    with client_for(engine) as c:
        first=c.post('/api/v1/studio/videos',files={'file':('中文舞蹈.mp4',video.read_bytes(),'video/mp4')})
        assert first.status_code==200 and not first.json()['duplicate']
        again=c.post('/api/v1/studio/videos',files={'file':('中文舞蹈.mp4',video.read_bytes(),'video/mp4')})
        assert again.status_code==200 and again.json()['asset_id']==first.json()['asset_id'] and again.json()['duplicate']
        bad=c.post('/api/v1/studio/videos',files={'file':('bad.mp4',b'broken','video/mp4')})
        assert bad.status_code==400 and len(engine.db.list('assets'))==1
        def no_space(*a):raise StoragePaused('空间不足')
        monkeypatch.setattr(engine.db,'check_space',no_space)
        assert c.post('/api/v1/studio/videos',files={'file':('x.mp4',video.read_bytes())}).status_code==409
    assert not list((tmp_path/'tmp').glob('*.upload'))

def test_missing_configuration_and_mcp_start_boundary(engine,monkeypatch):
    monkeypatch.setattr(engine.studio,'environment',lambda:{'issues':['缺少凭据'],'stages':{s:{'issues':[],'unit':None} for s in ['image','dress','motion']}})
    d=create(engine,ds_budget=1,rh_budget=30);assert not d['ready'] and '缺少凭据' in d['issues']
    with client_for(engine) as c:
        assert c.post('/api/v1/studio/drafts/'+d['id']+'/start',json={'expected_version':1},headers={'x-moflow-mcp':'1'}).status_code==403
    assert not engine.production.list()
