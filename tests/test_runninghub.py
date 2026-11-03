import asyncio,hashlib,json,zipfile
from pathlib import Path
import pytest,httpx
from PIL import Image
from backend.core import Store,Settings,StoragePaused
from backend.engine import Engine

@pytest.fixture
def rh(tmp_path):
    db=Store(tmp_path/'test.sqlite3');db.set('settings',Settings(storage_root=str(tmp_path/'media'),reserve_gb=.05).model_dump())
    return Engine(db).runninghub

def app(identifier='2099611210999488514',name='图像放大'):
    return {'id':identifier,'name':name,'owner':{'name':'测试作者'},'covers':[{'url':'https://rh-hk-images.xiaoyaoyou.com/cover.jpg'}]}

def test_favorite_sync_removal_preserves_cached_metadata(rh):
    rh.replace_favorites([app(),app('2071308534918246402','换衣')])
    rh.replace_favorites([app()])
    rows=rh.apps()
    assert len(rows)==2 and sum(r['available'] for r in rows)==1
    assert next(r for r in rows if r['name']=='换衣')['available'] is False
    assert rows[0]['id']=='2099611210999488514'

def test_partial_favorites_do_not_remove_previous_pages(rh):
    rh.replace_favorites([app()]);rh.replace_favorites([app('2071308534918246402','换衣')],False)
    assert len([a for a in rh.apps() if a['available']])==2
    with pytest.raises(ValueError):rh.replace_favorites([app('../unsafe')])
    assert len(rh.apps())==2

async def test_download_registration_decodes_and_deduplicates(rh):
    folder=rh.download_directory();file=folder/'sample.png';Image.new('RGB',(48,72),'gray').save(file)
    data=file.read_bytes();result=await rh.register_download(file,'https://www.runninghub.ai/zh-cn/ai-detail/2099611210999488514')
    assert len(result['asset_ids'])==1
    row=rh.db.list('assets')[0]
    assert row['payload']['provider']=='runninghub' and row['sha']==hashlib.sha256(data).hexdigest()
    assert row['payload']['probe']=={'width':48,'height':72} and Path(row['path']).is_file()
    duplicate=folder/'second.png';duplicate.write_bytes(data);again=await rh.register_download(duplicate)
    assert again['asset_ids']==result['asset_ids'] and len(rh.db.list('assets'))==1

async def test_zip_traversal_and_symlinks_never_extract(rh,tmp_path):
    folder=rh.download_directory();archive=folder/'bad.zip'
    with zipfile.ZipFile(archive,'w') as z:z.writestr('../escape.png',b'bad')
    with pytest.raises(ValueError,match='不安全路径'):await rh.register_download(archive)
    assert not (folder.parent/'escape.png').exists() and archive.exists()
    link=folder/'link.zip'
    with zipfile.ZipFile(link,'w') as z:
        item=zipfile.ZipInfo('link.png');item.external_attr=0o120777<<16;z.writestr(item,'/etc/passwd')
    with pytest.raises(ValueError,match='不安全路径'):await rh.register_download(link)
    with pytest.raises(ValueError,match='归档目录'):await rh.register_download(tmp_path/'elsewhere.png')

async def test_zip_keeps_original_and_imports_only_media(rh):
    import io
    stream=io.BytesIO();Image.new('RGB',(42,64),'white').save(stream,'PNG')
    path=rh.download_directory()/'result.zip'
    with zipfile.ZipFile(path,'w') as z:z.writestr('nested/image.png',stream.getvalue());z.writestr('run.exe',b'not executed')
    result=await rh.register_download(path)
    assert path.exists() and result['archive_preserved'] and len(result['asset_ids'])==1
    assert not list(path.parent.rglob('*.exe')) and not list(path.parent.glob('unpack-*'))

def test_unindexed_web_downloads_count_towards_quota(rh):
    path=rh.download_directory()/'large.part'
    settings=rh.db.settings();settings.quota_gb=.1;rh.db.set('settings',settings.model_dump())
    with path.open('wb') as stream:stream.truncate(110*1024*1024)
    with pytest.raises(StoragePaused):rh.db.check_space()

async def test_session_export_filters_other_sites_and_analytics(rh,monkeypatch,tmp_path):
    monkeypatch.setattr('backend.runninghub.ROOT',tmp_path)
    browser=rh.browser
    class Context:
        async def storage_state(self):return {'origins':[{'origin':'https://www.runninghub.ai','localStorage':[{'name':'Rh-Accesstoken','value':'private-test'},{'name':'userInfo','value':'{}'},{'name':'fclog_large','value':'ignored'}]},{'origin':'https://other.example','localStorage':[{'name':'other','value':'secret'}]}],'cookies':[{'domain':'www.runninghub.ai','name':'session','value':'private'},{'domain':'other.example','name':'other','value':'secret'}]}
    async def page():return object()
    async def noop(*args):pass
    browser.ctx=Context();monkeypatch.setattr(browser,'page',page);monkeypatch.setattr(browser,'goto',noop);monkeypatch.setattr(browser,'check_login',noop)
    result=await rh.export_session()
    assert [x['name'] for x in result['origins'][0]['localStorage']]==['Rh-Accesstoken','userInfo']
    assert len(result['origins'])==len(result['cookies'])==1

async def test_existing_webview_profile_opens_without_chrome_or_network(rh,monkeypatch,tmp_path):
    monkeypatch.setattr('backend.runninghub.ROOT',tmp_path)
    profile=tmp_path/'webview-runninghub'/'EBWebView'/'Default'/'Local Storage';profile.mkdir(parents=True)
    async def forbidden():raise AssertionError('已有WebView登录不应启动Chrome')
    monkeypatch.setattr(rh.browser,'page',forbidden)
    result=await rh.export_session()
    assert result['persistent_profile'] and result['origins']==[] and result['cookies']==[]
    assert result['webview_profile']==str(tmp_path/'webview-runninghub')

async def test_background_sync_coalesces_and_preserves_cached_apps(rh,monkeypatch):
    rh.replace_favorites([app()]);started=asyncio.Event();finish=asyncio.Event();calls=[]
    async def sync():
        calls.append('sync');started.set();await finish.wait();rh.replace_favorites([app(),app('2071308534918246402','换衣')])
    monkeypatch.setattr(rh.browser,'sync',sync)
    initial=rh.start_sync();assert initial['state']=='syncing' and initial['count']==1
    await started.wait();task=rh.sync_task;rh.start_sync();assert rh.sync_task is task and len(calls)==1
    finish.set();await task
    assert rh.sync_status()['state']=='ready' and rh.sync_status()['count']==2
    await rh.close()

async def test_sync_failure_keeps_apps_and_close_cancels_pending_sync(rh,monkeypatch):
    from backend.core import LoginRequired
    rh.replace_favorites([app()])
    async def blocked():raise LoginRequired('请完成RunningHub登录')
    monkeypatch.setattr(rh.browser,'sync',blocked);rh.start_sync();await rh.sync_task
    assert rh.sync_status()['state']=='waiting_login' and rh.apps()[0]['available']
    started=asyncio.Event();cancelled=asyncio.Event()
    async def pending():
        started.set()
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    monkeypatch.setattr(rh.browser,'sync',pending);rh.start_sync();await started.wait();await rh.close();assert cancelled.is_set()

async def test_sync_http_returns_cached_list_before_browser_finishes(rh,monkeypatch):
    from fastapi import FastAPI
    from backend.rh_api import router
    import time
    rh.replace_favorites([app()])
    async def pending():await asyncio.sleep(30)
    monkeypatch.setattr(rh.browser,'sync',pending)
    app_server=FastAPI();app_server.include_router(router(rh.engine))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_server),base_url='http://testserver') as client:
            start=time.perf_counter();response=await client.post('/api/v1/runninghub/apps/sync')
            assert time.perf_counter()-start<1 and response.json()['state']=='syncing'
            catalog=(await client.get('/api/v1/runninghub/apps')).json()
            assert len(catalog['items'])==1 and catalog['sync']['state']=='syncing'
    finally:await rh.close()

async def test_private_session_handoff_requires_host_token(monkeypatch):
    from backend.app import app
    monkeypatch.setenv('MOFLOW_CONTROL_TOKEN','host-only-test')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        denied=await client.get('/api/v1/runninghub/session');assert denied.status_code==403
        listed=await client.get('/api/v1/runninghub/apps');assert listed.status_code==200 and 'cookies' not in listed.text and 'Rh-Accesstoken' not in listed.text
        paths=set(app.openapi()['paths'])
        assert not any(p.endswith('/runninghub/runs') for p in paths)
