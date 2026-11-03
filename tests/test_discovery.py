import asyncio,time,json
import pytest
from backend.core import Store,LoginRequired
from backend.platform import Douyin
from backend.discovery_cache import SearchCache,TTL
from backend.recommendations import Recommendations,rank_candidates,KEYWORDS

@pytest.fixture
def db(tmp_path):
    db=Store(tmp_path/'search.sqlite3');Douyin(db)
    db.execute('INSERT INTO accounts VALUES(?,?,?,?,?)',('second','第二账号','connected','{}','now'))
    return db

def row(i,age=3600,likes=2000,author=''):
    return {'id':str(i),'title':'甜美 单人舞蹈','author_id':author,'published':time.time()-age,'stats':{'digg_count':likes}}

async def test_search_persists_cache_refresh_and_singleflight(db):
    calls=[];entered=asyncio.Event();release=asyncio.Event()
    async def fetch(*args):calls.append(args);entered.set();await release.wait();return [row('1')]
    cache=SearchCache(db,fetch)
    a=asyncio.create_task(cache.search('  单人舞蹈  '));await entered.wait()
    b=asyncio.create_task(cache.search('单人舞蹈'));await asyncio.sleep(0);release.set()
    assert (await a)['search']['source']=='live' and (await b)['items'][0]['id']=='1' and len(calls)==1
    restarted=SearchCache(db,fetch)
    assert (await restarted.search('单人舞蹈'))['search']['source']=='query_cache'
    await restarted.search('单人舞蹈',refresh=True)
    assert len(calls)==2

async def test_search_account_kind_expiration_and_candidate_index(db):
    calls=[]
    async def fetch(*args):calls.append(args);return [row('live')]
    cache=SearchCache(db,fetch);cache.remember('default',[row(i) for i in range(15)])
    result=await cache.search('甜美 单人',count=12)
    assert result['search']['source']=='candidate_cache' and len(result['items'])==12 and not calls
    assert (await cache.search('甜美 单人',account='second'))['search']['source']=='live'
    assert (await cache.search('甜美 单人',kind='user'))['search']['source']=='live'
    await cache.search('unique');db.execute('UPDATE discovery_queries SET updated=?',(time.time()-TTL-1,))
    await cache.search('unique');assert len(calls)==4
    with pytest.raises(ValueError):await cache.search('a',account='missing')
    with pytest.raises(ValueError):await cache.search('a',kind='invalid')
    with pytest.raises(ValueError):await cache.search('   ')

async def test_search_login_failure_not_cached_and_cancellation(db):
    async def gated(*args):raise LoginRequired('验证')
    cache=SearchCache(db,gated)
    with pytest.raises(LoginRequired):await cache.search('舞蹈')
    assert not db.query('SELECT * FROM discovery_queries')
    entered=asyncio.Event();exited=asyncio.Event()
    async def slow(*args):
        entered.set()
        try:await asyncio.Event().wait()
        finally:exited.set()
    cache.fetch=slow;request=asyncio.create_task(cache.search('舞蹈'));await entered.wait()
    await cache.close()
    with pytest.raises(asyncio.CancelledError):await request
    assert exited.is_set()

def test_index_bounded_and_upserted(db):
    cache=SearchCache(db,None);cache.remember('default',[row(i) for i in range(510)])
    assert len(db.query('SELECT * FROM discovery_candidates'))==500
    cache.remember('default',[row(509,likes=9000)])
    assert len(db.query('SELECT * FROM discovery_candidates'))==500
    assert json.loads(db.query('SELECT payload FROM discovery_candidates WHERE id=?',('509',))[0]['payload'])['stats']['digg_count']==9000

def test_ranking_balances_newness_caps_authors_and_has_no_fake_metrics():
    rows=[row('old',age=6*86400,likes=100000,author='a'),row('new',age=3600,likes=20000,author='b'),row('third',age=5000,likes=19000,author='b'),row('fourth',age=6000,likes=18000,author='b')]
    assert rank_candidates(rows,sort='likes')[0]['id']=='old'
    assert rank_candidates(rows,sort='latest')[0]['id']=='new'
    result=rank_candidates(rows)
    assert result[0]['id']=='new' and len([r for r in result if r['author_id']=='b'])==2
    assert result[0]['recommendation']['visual_status']=='unverified'
    assert 'views_per_follower' not in result[0]['recommendation']
    assert len(rank_candidates([row(i) for i in range(5)]))==5
    assert len(rank_candidates([row('same'),row('same')]))==1
    assert rank_candidates([{**row('bad'),'published':'invalid'},row('nan',likes=float('nan'))])==[]
    with pytest.raises(ValueError):rank_candidates(rows,sort='fake')

async def test_progressive_queries_publish_before_completion_and_stop_at_verification(db):
    entered=asyncio.Event();release=asyncio.Event();calls=[]
    class Platform:
        async def recommendation_search(self,keyword,account):
            calls.append(keyword)
            if len(calls)==2:entered.set();await release.wait();raise LoginRequired('需要验证')
            return [row('first')]
    service=Recommendations(db,Platform());service.start();await entered.wait()
    state=service.state();assert state['status']=='refreshing' and state['queries_done']==1 and state['items'][0]['id']=='first'
    # Re-sorting the existing pool performs no extra search.
    assert service.state(sort='latest')['items'] and len(calls)==2
    release.set();await service.tasks['default']
    assert service.state()['status']=='waiting_login' and service.state()['items'][0]['id']=='first' and len(calls)==2

async def test_multi_query_dedup_and_legacy_cache(db):
    class Platform:
        async def recommendation_search(self,keyword,account):return [row('same'),row(keyword)]
    service=Recommendations(db,Platform())
    from backend.core import now
    db.set('dance_recommendations_v2:default',{'items':[row('legacy')],'updated':now()})
    assert service.state()['items'][0]['id']=='legacy'
    service.start(refresh=True);await service.tasks['default']
    assert len(service.state()['items'])==4 and service.state()['queries_done']==len(KEYWORDS)
    with pytest.raises(ValueError):service.state(sort='fake')

async def test_native_search_fast_path_and_gate_not_bypassed(db,monkeypatch):
    platform=Douyin(db);calls=[]
    async def page(*args):return object()
    async def native(*args,**kwargs):
        calls.append(args)
        return {'data':[{'aweme_info':{'aweme_id':'123456789','desc':'单人舞蹈','video':{'duration':12000},'author':{}}}],'has_more':0}
    async def no_navigation(*args,**kwargs):pytest.fail('native success must not navigate')
    monkeypatch.setattr(platform,'page',page);monkeypatch.setattr(platform,'read_native',native);monkeypatch.setattr(platform,'goto',no_navigation)
    assert (await platform.search('单人舞蹈'))[0]['id']=='123456789'
    assert (await platform.search('单人舞蹈'))[0]['duration']==12 and len(calls)==1
    async def blocked(*args,**kwargs):raise LoginRequired('验证')
    monkeypatch.setattr(platform,'read_native',blocked);platform.last.clear()
    with pytest.raises(LoginRequired):await platform.search('另一个词')
