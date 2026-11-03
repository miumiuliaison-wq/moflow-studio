import json
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from .core import SecretVault, now, uid
from .production_config import Preferences, ProductionSettings, ProductionRequest, preferences, settings
from .runninghub_api import FixedPrice, normalize_schema

class Credentials(BaseModel):
    deepseek_key: str | None = Field(None,max_length=500)
    runninghub_key: str | None = Field(None,max_length=500)
    use_deepseek: bool = True

def router(engine):
    api=APIRouter(prefix='/api/v1');p=engine.production;db=engine.db
    def local_user(request):
        if request.headers.get('x-moflow-mcp'):raise HTTPException(403,'MCP 不能修改生产权限或预算')

    @api.get('/production/config')
    async def config():
        vault=SecretVault()
        return {'preferences':preferences(db).model_dump(),'settings':settings(db).model_dump(),
                'credentials':{k:bool(vault.get(k)) for k in ('deepseek','runninghub')},'units':{'deepseek':'USD'},
                'apps':engine.runninghub.apps()}

    @api.put('/production/preferences')
    async def save_preferences(body:Preferences,request:Request):
        local_user(request);old=preferences(db);body.version=old.version+1;db.set('production_preferences',body.model_dump());return body

    @api.put('/production/settings')
    async def save_config(body:ProductionSettings,request:Request):
        local_user(request)
        for role in ('image','dress','motion'):
            binding=getattr(body,role)
            if not binding.app_id.isdigit():raise ValueError('应用 ID 必须是数字字符串')
            schema=db.get('rh_schema:'+binding.app_id)
            if binding.fields or binding.defaults:
                if not schema or binding.schema_hash!=schema['hash']:raise ValueError('请读取最新应用字段后保存映射')
                keys={n['key'] for n in schema['nodes']}
                if any(v not in keys for v in binding.fields.values()) or any(k not in keys for k in binding.defaults):raise ValueError('包含不存在的应用字段')
                if len(set(binding.fields.values()))!=len(binding.fields):raise ValueError('同一输入不能被映射为两个不同用途')
        db.set('production_settings',body.model_dump());return body

    @api.put('/production/credentials')
    async def save_credentials(body:Credentials,request:Request):
        local_user(request);vault=SecretVault()
        for key,value in [('deepseek',body.deepseek_key),('runninghub',body.runninghub_key)]:
            if value is not None:vault.set(key,value.strip())
        if body.use_deepseek:
            cfg=db.settings();from .core import ModelConfig
            cfg.text_model=ModelConfig(provider='deepseek',model='deepseek-flash');cfg.vision_model=ModelConfig(provider='deepseek',model='deepseek-flash')
            db.set('settings',cfg.model_dump())
        return {'saved':True,'credentials':{k:bool(vault.get(k)) for k in ('deepseek','runninghub')}}

    @api.post('/production/check/{provider}')
    async def check(provider:str):
        if provider=='runninghub':return await p.rh.check()
        if provider!='deepseek':raise ValueError('未知服务商')
        cfg=db.settings()
        if cfg.text_model.provider!='deepseek':raise ValueError('请先保存 DeepSeek 配置')
        models=await engine.models.list('text')
        return {'connected':True,'models':models,'selected_available':cfg.text_model.model in models,'vision_available':'deepseek-flash' in models}

    @api.get('/production/apps/{app_id}/schema')
    async def schema(app_id:str):return await p.rh.schema(app_id)

    @api.put('/production/apps/{app_id}/pricing')
    async def pricing(app_id:str,body:FixedPrice,request:Request):
        local_user(request);return p.rh.save_fixed_price(app_id,body)

    @api.get('/production/batches')
    async def batches():return {'items':p.list()}

    @api.post('/production/batches')
    async def create(body:ProductionRequest):
        row=p.create(body);p.tick();return row

    @api.get('/production/batches/{identifier}')
    async def batch(identifier:str):return p.get(identifier)

    @api.post('/production/batches/{identifier}/{action}')
    async def action(identifier:str,action:str):return await p.action(identifier,action)

    @api.put('/production/batches/{identifier}/budget')
    async def budget(identifier:str,body:dict,request:Request):
        local_user(request);row=p.get(identifier)
        if row['state'] in {'running','queued'}:raise ValueError('请暂停任务后调整预算')
        cfg=ProductionRequest.model_validate({**row['payload']['request'],**{k:v for k,v in body.items() if k in {'ds_budget','rh_budget','rh_unit'}}})
        # Validate both units/limits first, then commit together.
        from .billing import micros
        with db.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            for provider,unit,limit in [('deepseek','USD',cfg.ds_budget),('runninghub',cfg.rh_unit,cfg.rh_budget)]:
                old=c.execute('SELECT unit FROM production_budgets WHERE owner=? AND provider=?',(identifier,provider)).fetchone()
                count=c.execute('SELECT COUNT(*) FROM production_charges WHERE owner=? AND provider=?',(identifier,provider)).fetchone()[0]
                if old and old['unit']!=unit and count:raise ValueError('已有费用记录，不能更改单位')
                c.execute('INSERT OR REPLACE INTO production_budgets VALUES(?,?,?,?)',(identifier,provider,unit,micros(limit)))
            row['payload']['request']=cfg.model_dump()
            c.execute('UPDATE productions SET payload=?,updated=? WHERE id=?',(json.dumps(row['payload'],ensure_ascii=False),now(),identifier))
        return p.get(identifier)

    @api.put('/production/cloud/{identifier}/receipt')
    async def receipt(identifier:str,body:dict,request:Request):
        local_user(request);row=p.rh.row(identifier);task_id=str(body.get('task_id',''))
        if not row or row['task_id'] or row['state'] not in {'submitting','uncertain'}:raise ValueError('该任务不需要补录回执')
        if not task_id.isdigit():raise ValueError('请从官网历史复制原任务 ID')
        await p.rh.request('/task/openapi/status',{'taskId':task_id})
        p.rh.save(identifier,'running',row['payload'],task_id)
        return {'saved':True,'message':'只查询这个原任务，不重新生成'}

    return api
