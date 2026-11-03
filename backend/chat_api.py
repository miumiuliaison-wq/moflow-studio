import json
from typing import Literal
from fastapi import APIRouter, UploadFile, File, HTTPException
from pydantic import BaseModel, Field

class SessionInput(BaseModel):
    account:str='default'
class ProjectContext(BaseModel):
    kind:Literal['draft','production']
    id:str=Field(min_length=1,max_length=120)

class MessageInput(BaseModel):
    text:str=Field(min_length=1,max_length=6000)
    request_id:str=Field(min_length=8,max_length=120)
    asset_ids:list[str]=Field(default_factory=list,max_length=5)
    context:ProjectContext|None=None
class PluginConfig(BaseModel):
    enabled:bool
    auto_tools:list[str]=Field(default_factory=list,max_length=100)
class NoteInput(BaseModel):
    title:str=Field(min_length=1,max_length=200)
    text:str=Field(min_length=1,max_length=6000)

def router(engine):
    from .plugins import BUILTINS
    from .core import now
    api=APIRouter(prefix='/api/v1');chat=engine.chat;plugins=chat.plugins
    @api.get('/chat/sessions')
    async def sessions():return {'items':chat.sessions()}
    @api.post('/chat/sessions')
    async def create(body:SessionInput):return chat.create(body.account)
    @api.get('/chat/sessions/{identifier}')
    async def get(identifier:str):return chat.get(identifier)
    @api.post('/chat/sessions/{identifier}/messages')
    async def send(identifier:str,body:MessageInput):return chat.send(identifier,body.text,body.request_id,body.asset_ids,body.context.model_dump() if body.context else None)
    @api.post('/chat/messages/{identifier}/{action}')
    async def control(identifier:str,action:str):return await chat.control(identifier,action)
    @api.get('/plugins')
    async def listing():return {'items':plugins.rows(),'builtins':list(BUILTINS.values()),'format':'moflow-plugin/v1'}
    @api.post('/plugins/builtin/{identifier}')
    async def builtin(identifier:str):return plugins.install_builtin(identifier)
    @api.post('/plugins/install')
    async def install(file:UploadFile=File(...)):
        data=await file.read(262145)
        if len(data)>262144:raise ValueError('插件清单最多256KB')
        try:manifest=json.loads(data.decode('utf-8-sig'))
        except (UnicodeDecodeError,json.JSONDecodeError):raise ValueError('请选择UTF-8 JSON插件清单') from None
        if not isinstance(manifest,dict):raise ValueError('插件清单须为JSON对象')
        return plugins.install(manifest)
    @api.put('/plugins/{identifier}')
    async def configure(identifier:str,body:PluginConfig):return plugins.configure(identifier,body.enabled,body.auto_tools)
    @api.post('/plugins/{identifier}/connect')
    async def connect(identifier:str):
        try:return await plugins.inspect(identifier)
        except Exception as e:
            from .core import redact
            raise ValueError('连接失败：'+redact(str(e) or type(e).__name__)) from None
    @api.delete('/plugins/{identifier}')
    async def remove(identifier:str):
        plugins.get(identifier);engine.db.execute('DELETE FROM chat_plugins WHERE id=?',(identifier,));return {'ok':True}
    @api.put('/chat/notes/{identifier}')
    async def edit_note(identifier:str,body:NoteInput):
        if not engine.db.query('SELECT id FROM chat_notes WHERE id=?',(identifier,)):raise HTTPException(404,'笔记不存在')
        engine.db.execute('UPDATE chat_notes SET title=?,text=?,updated=? WHERE id=?',(body.title,body.text,now(),identifier));return {'ok':True}
    @api.delete('/chat/notes/{identifier}')
    async def delete_note(identifier:str):
        engine.db.execute('DELETE FROM chat_notes WHERE id=?',(identifier,));return {'ok':True}
    return api
