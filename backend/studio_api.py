import asyncio,hashlib,json,os,shutil
from pathlib import Path
from fastapi import APIRouter,UploadFile,File,HTTPException,Request
from .studio import DraftCreate,DraftUpdate,Version
from .core import ROOT,now,uid

def router(engine):
    api=APIRouter(prefix='/api/v1/studio');studio=engine.studio;db=engine.db;upload_lock=asyncio.Lock()
    @api.get('/state')
    async def state():return {'drafts':studio.list(),'projects':engine.production.list()}
    @api.get('/drafts')
    async def drafts():return {'items':studio.list()}
    @api.post('/drafts')
    async def create(body:DraftCreate):return studio.create(body)
    @api.get('/drafts/{identifier}')
    async def get(identifier:str):return studio.get(identifier)
    @api.put('/drafts/{identifier}')
    async def update(identifier:str,body:DraftUpdate):return studio.update(identifier,body)
    @api.delete('/drafts/{identifier}')
    async def remove(identifier:str,expected_version:int):studio.remove(identifier,expected_version);return {'ok':True}
    @api.post('/drafts/{identifier}/reference')
    async def reference(identifier:str,body:Version):return studio.reference(identifier,body.expected_version)
    @api.post('/drafts/{identifier}/start')
    async def start(identifier:str,body:Version,request:Request):
        if request.headers.get('x-moflow-mcp'):raise HTTPException(403,'草稿启动仅供用户界面，MCP沿用原生产权限')
        return await studio.start(identifier,body.expected_version)
    @api.post('/videos')
    async def upload_video(file:UploadFile=File(...)):
        async with upload_lock:
            temp=ROOT/'tmp'/(uid()+'.upload');temp.parent.mkdir(exist_ok=True,parents=True);part=None;total=0
            try:
                with temp.open('wb') as f:
                    while chunk:=await file.read(256*1024):
                        total+=len(chunk)
                        if total>600*1024**2:raise ValueError('参考视频最多600MB')
                        db.check_space(len(chunk));f.write(chunk)
                metadata=await engine.probe(temp)
                suffix=Path(file.filename or '').suffix.lower()
                if suffix not in {'.mp4','.mov','.webm','.mkv','.m4v','.avi'}:raise ValueError('请选择MP4、MOV、WebM、MKV、M4V或AVI视频')
                with temp.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                old=db.query('SELECT id,path FROM assets WHERE sha=? AND kind=?',(digest,'video'))
                for row in old:
                    if Path(row['path']).is_file():return {'asset_id':row['id'],'duplicate':True}
                folder=Path(db.settings().storage_root)/'视频'/'墨流'/'上传';folder.mkdir(parents=True,exist_ok=True)
                identifier=uid();dest=folder/(identifier+suffix);part=dest.with_suffix('.part'+suffix)
                try:temp.replace(part)
                except OSError:
                    db.check_space(total);await asyncio.to_thread(shutil.copyfile,temp,part);temp.unlink()
                part.replace(dest)
                payload={'probe':metadata,'media':{'title':Path(file.filename or '本地参考').stem,'author':'本地上传'}}
                db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',(identifier,'studio-upload:'+digest,'video',str(dest),digest,total,now(),0,json.dumps(payload,ensure_ascii=False)))
                return {'asset_id':identifier,'duplicate':False}
            finally:
                temp.unlink(missing_ok=True)
                if part:part.unlink(missing_ok=True)
    return api
