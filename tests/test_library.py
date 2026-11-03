import asyncio,json,os
from pathlib import Path
import pytest
from PIL import Image
from backend.core import Settings,Store
from backend.engine import Engine
from backend.library import source_of

@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'library.sqlite3');comfy=tmp_path/'ComfyUI'
    settings=Settings(storage_root=str(tmp_path/'downloads'),comfyui_material_dirs=[str(comfy/'图片'),str(comfy/'视频')],reference_image_dir=str(comfy/'图片'/'生成'),reference_video_dir=str(tmp_path/'downloads'/'视频'/'墨流'),reserve_gb=.05)
    store.set('settings',settings.model_dump());return Engine(store),settings

def asset(path,source='',source_id='import:123',imported=1,**payload):
    return {'path':str(path),'payload':{'media':{'source':source},**payload},'source_id':source_id,'imported':imported}

def test_sources_respect_path_boundaries_and_known_provenance(setup):
    _,s=setup;comfy=Path(s.comfyui_material_dirs[0])
    assert source_of(asset(comfy/'one.png'),s)=='comfyui'
    assert source_of(asset(comfy.parent/'图片-other'/'one.png'),s)=='other'
    assert source_of(asset(comfy/'one.png',provider='runninghub'),s)=='comfyui'
    assert source_of(asset('x',source='https://www.douyin.com/video/123'),s)=='douyin'
    assert source_of(asset('x',source_id='123',imported=0),s)=='douyin'
    assert source_of(asset('x',source_id='123'),s)=='other'
    assert source_of(asset('x',source='https://www.runninghub.ai/zh-cn/ai-detail/123'),s)=='runninghub'
    assert source_of(asset('x',source='https://www.douyin.com.evil.example/video/123'),s)=='other'

def test_old_settings_gain_defaults_and_reject_relative_paths():
    s=Settings();assert s.reference_image_dir.endswith('图片\\生成');assert len(s.comfyui_material_dirs)==2
    for values in [{'reference_image_dir':'relative'},{'comfyui_material_dirs':['relative']}]:
        with pytest.raises(ValueError):Settings(**values)

async def test_incremental_index_preserves_ids_and_files(setup,tmp_path,monkeypatch):
    engine,s=setup;root=Path(s.reference_image_dir);root.mkdir(parents=True);path=root/'人物.png';Image.new('RGB',(20,40),'white').save(path)
    assert await engine.library.import_folder(root)==1
    row=engine.db.list('assets')[0];original=row['id'];original_sha=row['sha']
    def no_read(*args,**kwargs):raise AssertionError('unchanged image must not be decoded again')
    with monkeypatch.context() as m:
        m.setattr('backend.library.Image.open',no_read)
        assert await engine.library.import_folder(root)==0
    payload=row['payload'];payload.update(analysis={'stale':True},prompt={'stale':True});engine.db.execute('UPDATE assets SET payload=? WHERE id=?',(json.dumps(payload),original))
    Image.new('RGB',(30,60),'black').save(path)
    assert await engine.library.import_folder(root)==0
    row=engine.db.list('assets')[0];assert row['id']==original and row['sha']!=original_sha and 'analysis' not in row['payload'];assert path.exists()
    assert engine.library.decorate([row])[0]['available'] is True
    path.unlink();engine.library.file_cache.clear();row=engine.library.decorate(engine.db.list('assets'))[0];assert row['available'] is False and row['source_category']=='comfyui'
    assert engine.db.list('assets')[0]['id']==original

async def test_background_scan_coalesces_and_covers_both_types(setup,monkeypatch):
    engine,s=setup
    for root in s.comfyui_material_dirs:Path(root).mkdir(parents=True)
    image=Path(s.comfyui_material_dirs[0])/'图.png';Image.new('RGB',(10,20)).save(image)
    video=Path(s.comfyui_material_dirs[1])/'舞蹈.mp4';video.write_bytes(b'fixture')
    async def probe(path):await asyncio.sleep(.05);return {'duration':10}
    monkeypatch.setattr(engine,'probe',probe)
    first=engine.library.start();task=engine.library.task;assert first['state']=='running'
    engine.library.start();assert engine.library.task is task
    await task;assert engine.library.status()['state']=='completed'
    assert {a['kind'] for a in engine.db.list('assets')}=={'image','video'}
    assert all(a['imported']==1 for a in engine.db.list('assets'))
    engine.library.start();await engine.library.task;assert len(engine.db.list('assets'))==2 and engine.library.state['imported']==0

async def test_corrupt_and_missing_paths_are_reported(setup,tmp_path):
    engine,s=setup;root=Path(s.comfyui_material_dirs[0]);root.mkdir(parents=True);(root/'坏图.png').write_bytes(b'broken')
    engine.library.start();await engine.library.task
    assert engine.library.state['state']=='completed' and engine.library.state['errors'] and not engine.db.list('assets')
    with pytest.raises(ValueError):engine.library.start(str(tmp_path/'missing'))

def test_directories_follow_source_and_kind(setup):
    engine,s=setup
    paths=engine.library.directories('comfyui','image');assert len(paths)==1 and paths[0]['path']==s.comfyui_material_dirs[0]
    assert engine.library.directories('douyin','video')[0]['path']==s.reference_video_dir
    assert all('图片' in row['path'] for row in engine.library.directories('runninghub','image'))
