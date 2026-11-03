from __future__ import annotations
import asyncio, hashlib, json, os, time
from pathlib import Path
from urllib.parse import urlparse
from PIL import Image
from .core import now, uid

IMAGE_EXTENSIONS={'.png','.jpg','.jpeg','.webp','.bmp','.gif'}
VIDEO_EXTENSIONS={'.mp4','.mov','.webm','.mkv','.m4v'}
SOURCES={'douyin':'抖音下载','comfyui':'ComfyUI','runninghub':'RunningHub','other':'其他'}

def beneath(path, root):
    # Indexed paths and settings are canonicalized on import/save. Classification
    # is lexical, avoiding thousands of filesystem resolves on every UI poll.
    try:
        target=os.path.normcase(os.path.abspath(path));base=os.path.normcase(os.path.abspath(root))
        return os.path.commonpath((target,base))==base
    except (OSError,ValueError):return False

def source_of(asset, settings):
    path=asset['path'];payload=asset.get('payload',{})
    if isinstance(payload,str):payload=json.loads(payload)
    if any(beneath(path,p) for p in settings.comfyui_material_dirs):return 'comfyui'
    media=payload.get('media',{});host=urlparse(media.get('source','')).hostname or ''
    if payload.get('provider')=='runninghub' or str(asset.get('source_id','')).startswith('runninghub:') or host=='runninghub.ai' or host.endswith('.runninghub.ai'):return 'runninghub'
    if host=='douyin.com' or host.endswith('.douyin.com') or (str(asset.get('source_id','')).isdigit() and not asset.get('imported')):return 'douyin'
    root=Path(settings.storage_root)
    if any(beneath(path,root/k/'墨流'/'RunningHub') for k in ('图片','视频')):return 'runninghub'
    return 'other'

class Library:
    def __init__(self,engine):
        self.engine=engine;self.db=engine.db;self.task=None;self.lock=asyncio.Lock();self.file_cache={}
        self.state={'state':'idle','checked':0,'imported':0,'updated':0,'skipped':0,'errors':[]}
    def roots(self):
        s=self.db.settings()
        paths=[*s.comfyui_material_dirs,str(Path(s.storage_root)/'图片'),str(Path(s.storage_root)/'视频'),s.reference_image_dir,s.reference_video_dir,*self.db.get('library_import_dirs',[])]
        return list(dict.fromkeys(str(Path(p).resolve()) for p in paths))
    def decorate(self,assets):
        settings=self.db.settings();stamp=time.monotonic()
        for a in assets:
            cached=self.file_cache.get(a['path'])
            if not cached or stamp-cached[0]>30:
                try:exists=Path(a['path']).is_file()
                except OSError:exists=False
                cached=(stamp,exists);self.file_cache[a['path']]=cached
            a['source_category']=source_of(a,settings);a['available']=cached[1]
        return assets
    def directories(self,source='all',kind='all'):
        settings=self.db.settings();items={}
        def add(path,label):
            p=str(Path(path).resolve());items.setdefault(p,{'path':p,'label':label,'exists':Path(p).is_dir()})
        if source in {'all','comfyui'}:
            for p in settings.comfyui_material_dirs:
                if kind=='all' or Path(p).name!=('图片' if kind=='video' else '视频'):add(p,'ComfyUI · '+Path(p).name)
        if source in {'all','douyin'}:
            if kind in {'all','video'}:add(settings.reference_video_dir,'抖音视频')
            if kind in {'all','image'}:add(Path(settings.storage_root)/'图片'/'墨流','下载图片')
        if source in {'all','runninghub'}:
            for k in (('图片',) if kind=='image' else ('视频',) if kind=='video' else ('图片','视频')):add(Path(settings.storage_root)/k/'墨流'/'RunningHub','RunningHub · '+k)
        for a in self.db.list('assets',100000):
            if kind!='all' and a['kind']!=kind:continue
            if source!='all' and source_of(a,settings)!=source:continue
            parent=Path(a['path']).parent
            if any(beneath(parent,p) for p in items):continue
            add(parent,SOURCES[source_of(a,settings)])
        return list(items.values())
    def start(self,path=None):
        if path:
            root=Path(path)
            if not root.is_absolute() or not root.is_dir():raise ValueError('请选择存在的本地目录')
            paths=self.db.get('library_import_dirs',[]);paths.append(str(root.resolve()));self.db.set('library_import_dirs',list(dict.fromkeys(paths)))
        if self.task and not self.task.done():return self.status()
        self.state={'state':'running','checked':0,'imported':0,'updated':0,'skipped':0,'errors':[],'started':now()}
        self.task=asyncio.create_task(self.scan());return self.status()
    def status(self):return dict(self.state)
    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
    async def scan(self):
        try:
            roots=self.roots();selected=[]
            for p in sorted(roots,key=lambda x:len(Path(x).parts)):
                if not any(beneath(p,r) for r in selected):selected.append(p)
            for root in selected:
                if not Path(root).is_dir():self.state['errors'].append('目录不存在：'+root);continue
                await self.import_folder(root,self.state)
            self.file_cache.clear();self.state.update(state='completed',finished=now());self.db.set('library_last_scan',self.state)
            self.db.event('import','',f"素材刷新完成：新增 {self.state['imported']}，更新 {self.state['updated']}，跳过 {self.state['skipped']}")
        except asyncio.CancelledError:self.state['state']='interrupted';raise
        except Exception as error:self.state.update(state='failed',message=str(error))
    async def import_folder(self,folder,progress=None):
        async with self.lock:
            root=Path(folder).resolve()
            if not root.is_dir():raise ValueError('目录不存在')
            progress=progress if progress is not None else {'checked':0,'imported':0,'updated':0,'skipped':0,'errors':[]}
            count=0
            def discover():
                files=[]
                for parent,dirs,names in os.walk(root,followlinks=False):
                    dirs[:]=[d for d in dirs if not Path(parent,d).is_symlink() and not Path(parent,d).is_junction()]
                    for name in names:
                        p=Path(parent,name)
                        if p.suffix.lower() in IMAGE_EXTENSIONS|VIDEO_EXTENSIONS and not p.is_symlink():files.append(p)
                return files
            files=await asyncio.to_thread(discover)
            existing={os.path.normcase(str(Path(a['path']).resolve())):a for a in self.db.list('assets',100000)}
            for path in files:
                progress['checked']+=1
                try:
                    stat=await asyncio.to_thread(path.stat);fingerprint=[stat.st_size,stat.st_mtime_ns];old=existing.get(os.path.normcase(str(path)))
                    payload=old['payload'] if old else {'media':{'title':path.stem,'author':'历史导入','source':''}}
                    if old and payload.get('library_fingerprint')==fingerprint:progress['skipped']+=1;continue
                    kind='image' if path.suffix.lower() in IMAGE_EXTENSIONS else 'video'
                    if kind=='image':
                        def inspect():
                            with Image.open(path) as im:im.verify()
                            with Image.open(path) as im:return {'width':im.width,'height':im.height}
                        probe=await asyncio.to_thread(inspect)
                    else:probe=await self.engine.probe(path)
                    def digest():
                        with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
                    sha=await asyncio.to_thread(digest)
                    after=await asyncio.to_thread(path.stat)
                    if fingerprint!=[after.st_size,after.st_mtime_ns]:raise ValueError('文件正在写入，稍后刷新')
                    changed=old and sha!=old['sha']
                    if changed:
                        for key in ('analysis','prompt'):payload.pop(key,None)
                    payload.update(library_fingerprint=fingerprint,probe=probe)
                    if old:
                        self.db.execute('UPDATE assets SET kind=?,sha=?,bytes=?,payload=? WHERE id=?',(kind,sha,stat.st_size,json.dumps(payload,ensure_ascii=False),old['id']));progress['updated']+=1
                    else:
                        self.db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?,?,?,?)',(uid(),'import:'+sha,kind,str(path),sha,stat.st_size,now(),1,json.dumps(payload,ensure_ascii=False)));count+=1;progress['imported']+=1
                except Exception as error:
                    progress['skipped']+=1
                    if len(progress['errors'])<50:progress['errors'].append(path.name+'：'+str(error))
                if progress['checked']%10==0:await asyncio.sleep(0)
            self.file_cache.clear();return count
