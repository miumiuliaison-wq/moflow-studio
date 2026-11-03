import asyncio,time
from datetime import datetime,timezone,timedelta
import pytest
from backend.core import Store,LoginRequired
from backend.platform import Douyin
from backend.recommendations import Recommendations,rank_candidates,KEYWORD,KEYWORDS

@pytest.fixture
def db(tmp_path):
    db=Store(tmp_path/'recommend.sqlite3');Douyin(db);return db

def row(identifier,title='女生单人舞蹈',likes=2000,age=3600):
    return {'id':identifier,'title':title,'published':time.time()-age,'stats':{'digg_count':likes},'images':[]}

def test_only_recent_dance_video_ranked_by_likes():
    rows=[row('low'),row('old',likes=9000,age=8*86400),row('group','女生双人舞蹈',5000),row('group2','各跳各的 舞蹈',5000),row('not-dance','购物分享',9000),row('high',likes=3000),row('child','儿童舞蹈',5000),{**row('image'),'images':['x']}, {**row('missing'),'published':None},row('future',age=-86400),row('unpopular',likes=30),row('tutorial','女生单人舞蹈教程'),{**row('male'),'author_gender':1}]
    assert [r['id'] for r in rank_candidates(rows)]==['high','low']

async def test_duplicate_click_shares_one_request_and_reuses_cache(db):
    started=asyncio.Event();release=asyncio.Event();calls=[]
    class Platform:
        async def recommendation_search(self,keyword,account):
            calls.append((keyword,account));started.set();await release.wait();return [row('one')]
    service=Recommendations(db,Platform())
    assert service.start()['status']=='refreshing'
    await started.wait()
    assert service.start(refresh=True)['status']=='refreshing' and len(calls)==1
    release.set();await service.tasks['default']
    assert service.start()['items'][0]['id']=='one' and len(calls)==len(KEYWORDS) and calls[0][0]==KEYWORD
    # Cache survives service reconstruction without a new browser request.
    assert Recommendations(db,Platform()).start()['items'][0]['id']=='one'
    await service.close()

async def test_failed_refresh_retains_cache_and_reports_login(db):
    class Platform:
        async def recommendation_search(self,*args):raise LoginRequired('请完成平台验证')
    service=Recommendations(db,Platform());db.set(service.key('default'),{'items':[row('cached')],'updated':datetime.now(timezone.utc).isoformat()})
    service.start(refresh=True);await service.tasks['default']
    state=service.state();assert state['items'][0]['id']=='cached' and state['status']=='waiting_login'
    assert '验证' in state['message']
    await service.close()

async def test_account_cache_isolation_and_expired_cache(db):
    class Platform:pass
    db.execute('INSERT INTO accounts VALUES(?,?,?,?,?)',('second','另一个账号','not_logged_in','{}','now'))
    service=Recommendations(db,Platform());db.set(service.key('default'),{'items':[row('private')],'updated':(datetime.now(timezone.utc)-timedelta(minutes=31)).isoformat()})
    assert service.state('second')['items']==[] and service.state()['items']==[]
    with pytest.raises(ValueError):service.state('missing')

async def test_close_cancels_inflight_browser_request(db):
    started=asyncio.Event();cancelled=asyncio.Event()
    class Platform:
        async def recommendation_search(self,*args):
            started.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
    service=Recommendations(db,Platform());service.start();await started.wait();await service.close()
    assert cancelled.is_set() and service.tasks['default'].cancelled()

def test_recommendation_http_starts_without_waiting_for_search(db,monkeypatch,tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from backend.engine import Engine
    import backend.app as app_module
    import backend.engine as engine_module,backend.mcp_api as mcp_module
    @asynccontextmanager
    async def isolated_manager():yield
    monkeypatch.setattr(mcp_module,'manager',SimpleNamespace(run=isolated_manager))
    monkeypatch.setattr(engine_module,'ROOT',tmp_path)
    engine=Engine(db)
    async def slow(*args):await asyncio.sleep(20)
    monkeypatch.setattr(engine.platform,'recommendation_search',slow)
    monkeypatch.setattr(app_module,'db',db);monkeypatch.setattr(app_module,'engine',engine)
    with TestClient(app_module.app) as client:
        client.get('/');started=time.monotonic();response=client.post('/api/v1/recommendations',json={})
        assert time.monotonic()-started<1 and response.json()['status']=='refreshing'
        assert client.get('/api/v1/recommendations').json()['status']=='refreshing'
        assert client.post('/api/v1/recommendations',json={'account':'missing'}).status_code==400
