import asyncio,json,sys
from pathlib import Path
import pytest
from backend.core import Store,Settings,ModelRequired
from backend.engine import Engine
from backend.chat import Reply, safe

@pytest.fixture
def engine(tmp_path):
    db=Store(tmp_path/'studio.db');db.set('settings',Settings(storage_root=str(tmp_path/'media'),reserve_gb=.05).model_dump())
    db.execute('INSERT INTO accounts VALUES(?,?,?,?,?)',('default','测试账号','not_logged_in','{}','2026-10-28'))
    return Engine(db)

async def wait(chat,message):
    task=chat.tasks.get(message['id'])
    if task:await asyncio.wait_for(task,15)
    return chat.message(message['id'])

async def test_conversation_persists_and_duplicate_click_does_not_call_twice(engine,monkeypatch):
    calls=[]
    async def request(*a,**k):calls.append(a);return Reply(reply='你好，今天需要什么素材？')
    monkeypatch.setattr(engine.models,'request',request)
    c=engine.chat;s=c.create();m=c.send(s['id'],'你好','request-one',[])
    assert c.send(s['id'],'你好','request-one',[])['id']==m['id']
    m=await wait(c,m);assert m['state']=='completed' and len(calls)==1
    assert Engine(engine.db).chat.get(s['id'])['messages'][1]['text']==m['text']
    with pytest.raises(ValueError):c.send(s['id'],'另一个要求','request-one',[])

async def test_real_fixed_tool_record_and_followup_context(engine,monkeypatch):
    contexts=[]
    async def request(prompt,*a,**k):
        contexts.append(prompt)
        if len(contexts)==1:return Reply(reply='我先查本地素材。',calls=[{'tool':'moflow.media::library_search','arguments':{'kind':'video'}}])
        assert '"total": 0' in prompt
        return Reply(reply='当前资料库没有视频。')
    monkeypatch.setattr(engine.models,'request',request)
    c=engine.chat;s=c.create();m=await wait(c,c.send(s['id'],'看看本地视频','request-two',[]))
    assert m['state']=='completed' and m['steps'][0]['state']=='completed'
    assert m['steps'][0]['result']=={'total':0,'items':[]}

async def test_step_visible_before_tool_completes_and_readonly_pause_resumes(engine,monkeypatch):
    entered=asyncio.Event();hold=asyncio.Event();calls=[]
    async def request(*a,**k):return Reply(reply='读取素材',calls=[] if calls else [{'tool':'moflow.media::library_search','arguments':{}}])
    async def call(*a,**k):calls.append(a);entered.set();await hold.wait();return {}
    monkeypatch.setattr(engine.models,'request',request);monkeypatch.setattr(engine.chat.plugins,'call',call)
    c=engine.chat;s=c.create();m=c.send(s['id'],'查素材','request-three',[])
    await asyncio.wait_for(entered.wait(),2)
    assert c.message(m['id'])['steps'][0]['state']=='running'
    await c.control(m['id'],'pause');assert c.message(m['id'])['steps'][0]['state']=='paused'
    hold.set();await c.control(m['id'],'resume');m=await wait(c,m)
    assert m['state']=='completed' and len(calls)==2 and len(m['steps'])==1

async def test_unknown_or_invalid_tool_plan_executes_nothing(engine,monkeypatch):
    async def request(*a,**k):return Reply(reply='计划',calls=[{'tool':'moflow.media::library_search','arguments':{}},{'tool':'shell','arguments':{}}])
    monkeypatch.setattr(engine.models,'request',request)
    c=engine.chat;s=c.create();m=await wait(c,c.send(s['id'],'执行','request-four',[]))
    assert m['state']=='failed' and not m['steps']

async def test_model_unavailable_can_resume_without_losing_user_input(engine,monkeypatch):
    async def unavailable(*a,**k):raise ModelRequired('未配置')
    monkeypatch.setattr(engine.models,'request',unavailable)
    c=engine.chat;s=c.create();m=await wait(c,c.send(s['id'],'你好','request-five',[]))
    assert m['state']=='waiting_model'
    with pytest.raises(ValueError,match='取消当前任务'):c.send(s['id'],'你好2','request-six',[])
    async def works(*a,**k):return Reply(reply='已恢复')
    monkeypatch.setattr(engine.models,'request',works)
    await c.control(m['id'],'resume');m=await wait(c,m)
    assert m['state']=='completed' and m['text']=='已恢复'

def test_plugin_install_toggle_remove_and_native_dsh_incompatibility(engine):
    p=engine.chat.plugins
    p.install_builtin('moflow.notes');assert any(t['name'].endswith('::note_save') for t in p.catalog())
    p.configure('moflow.notes',False,[]);assert not any(t['name'].endswith('::note_save') for t in p.catalog())
    engine.db.execute('DELETE FROM chat_plugins WHERE id=?',('moflow.media',))
    assert not any(t['name'].startswith('moflow.media::') for t in Engine(engine.db).chat.plugins.catalog())
    with pytest.raises(ValueError,match='DSH'):p.install({'dsh':{},'name':'example'})
    with pytest.raises(ValueError):p.install({'id':'moflow.evil','name':'x','version':'1','command':'test'})

async def test_mcp_real_protocol_and_explicit_call_grant(engine,monkeypatch):
    p=engine.chat.plugins
    p.install({'id':'local.echo','name':'MCP测试','version':'1','command':sys.executable,'args':[str(Path(__file__).with_name('chat_mcp_fixture.py'))]})
    assert p.get('local.echo')['enabled']==0
    connected=await p.inspect('local.echo');assert connected['payload']['tools'][0]['name']=='echo'
    p.configure('local.echo',True,[])
    with pytest.raises(PermissionError):await p.call('local.echo::echo',{'text':'hello'},'default','one')
    out=await p.call('local.echo::echo',{'text':'hello'},'default','one',confirmed=True)
    assert 'actual MCP response: hello' in out['content']
    p.configure('local.echo',True,['echo'])
    out=await p.call('local.echo::echo',{'text':'auto'},'default','two')
    assert 'actual MCP response: auto' in out['content']
    await p.inspect('local.echo');assert not p.get('local.echo')['enabled'] and not p.get('local.echo')['payload']['auto_tools']

async def test_mcp_waiting_approval_then_continue_without_model_replan(engine,monkeypatch):
    p=engine.chat.plugins
    manifest={'id':'local.fixture','kind':'mcp','name':'fixture','version':'1','tools':[{'name':'echo','description':'test','inputSchema':{'type':'object'}}],'auto_tools':[]}
    engine.db.execute('INSERT INTO chat_plugins VALUES(?,?,?,?)',('local.fixture',1,json.dumps(manifest),'2026'))
    calls=[];requests=[]
    async def request(*a,**k):
        requests.append(1)
        return Reply(reply='完成' if len(requests)>1 else '调用工具',calls=[] if len(requests)>1 else [{'tool':'local.fixture::echo','arguments':{'text':'x'}}])
    original=p.call
    async def call(name,args,account,call_id,confirmed=False):calls.append(confirmed);return {'echo':'x'}
    monkeypatch.setattr(engine.models,'request',request);monkeypatch.setattr(p,'call',call)
    c=engine.chat;s=c.create();m=await wait(c,c.send(s['id'],'测试','request-seven',[]))
    assert m['state']=='waiting_approval' and not calls
    await c.control(m['id'],'approve');m=await wait(c,m)
    assert m['state']=='completed' and calls==[True] and len(requests)==2 and len(m['steps'])==1

def test_restart_marks_running_steps_unknown_and_does_not_replay(engine):
    c=engine.chat;s=c.create();mid='crash'
    engine.db.execute('INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?)',(mid,s['id'],'assistant','','running','crash-id','{}','2026','2026'))
    engine.db.execute('INSERT INTO chat_steps VALUES(?,?,?,?,?,?,?,?,?,?)',('step',mid,0,'local.x::write','running','{}','{}','','2026',''))
    c.recover();assert c.message(mid)['state']=='paused' and c.message(mid)['steps'][0]['state']=='uncertain' and not c.tasks

def test_secrets_not_sent_in_tool_context():
    out=safe({'api_key':'never-export','nested':{'Cookie':'secret','text':'sk-1234567890abcdef'}})
    assert 'never-export' not in json.dumps(out) and 'secret' not in json.dumps(out)
    assert out['nested']['text']=='[redacted]'

async def test_unknown_result_not_retried_in_later_turn(engine,monkeypatch):
    c=engine.chat;s=c.create()
    engine.db.execute('INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?)',('prior',s['id'],'assistant','已取消','cancelled','prior-id','{}','2026','2026'))
    engine.db.execute('INSERT INTO chat_steps VALUES(?,?,?,?,?,?,?,?,?,?)',('prior-step','prior',0,'moflow.media::library_search','uncertain','{}','{}','unknown','2026',''))
    async def request(*a,**k):return Reply(reply='重试',calls=[{'tool':'moflow.media::library_search','arguments':{}}])
    monkeypatch.setattr(engine.models,'request',request)
    m=await wait(c,c.send(s['id'],'继续','request-eight',[]))
    assert m['state']=='failed' and not m['steps'] and '结果不明' in m['text']

async def test_real_mcp_schema_drift_revokes_execution(engine):
    p=engine.chat.plugins
    p.install({'id':'local.drift','name':'MCP测试','version':'1','command':sys.executable,'args':[str(Path(__file__).with_name('chat_mcp_fixture.py'))]})
    await p.inspect('local.drift');p.configure('local.drift',True,['echo'])
    payload=p.get('local.drift')['payload'];payload['tools'][0]['inputSchema']={'type':'object','properties':{'different':{'type':'string'}}}
    engine.db.execute('UPDATE chat_plugins SET payload=? WHERE id=?',(json.dumps(payload),'local.drift'))
    with pytest.raises(ValueError,match='结构已变化'):await p.call('local.drift::echo',{},'default','drift')

def test_plugin_http_install_validation_and_removal(engine):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    from backend.chat_api import router
    api=FastAPI();api.include_router(router(engine))
    @api.exception_handler(ValueError)
    async def invalid(request,error):return JSONResponse({'detail':str(error)},400)
    with TestClient(api,raise_server_exceptions=False) as client:
        assert client.post('/api/v1/plugins/install',files={'file':('large.json',b'x'*262145)}).status_code==400
        valid={'format':'moflow-plugin/v1','id':'local.http','name':'HTTP插件','version':'1','command':sys.executable}
        result=client.post('/api/v1/plugins/install',files={'file':('manifest.json',json.dumps(valid).encode())})
        assert result.status_code==200 and result.json()['enabled']==0
        assert client.get('/api/v1/plugins').json()['items'][-1]['id'] in {'local.http','moflow.media'}
        assert client.delete('/api/v1/plugins/local.http').json()['ok']
