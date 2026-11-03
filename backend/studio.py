"""Persistent creation cards over the existing production engine; no second queue."""
from __future__ import annotations
import asyncio,json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,Field,ConfigDict
from fastapi import HTTPException
from .core import SecretVault,now,uid
from .production_config import ProductionRequest,settings

class DraftInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    title:str=Field('新的舞蹈创作',min_length=1,max_length=120)
    account:str='default'
    reference_asset:str=''
    reference_input:str=Field('',max_length=2000)
    clothing_asset:str=''
    duration:int=Field(12,ge=8,le=20)
    ds_budget:float=Field(0,ge=0,le=10000)
    rh_budget:float=Field(0,ge=0,le=100000)
    rh_unit:Literal['RH币','USD','CNY']='RH币'
    notes:str=Field('',max_length=3000)

class DraftCreate(DraftInput):
    request_id:str=Field(min_length=8,max_length=120)
class DraftUpdate(DraftInput):
    expected_version:int=Field(ge=1)
class Version(BaseModel):
    expected_version:int=Field(ge=1)

class Studio:
    def __init__(self,engine):
        self.engine=engine;self.db=engine.db;self.lock=asyncio.Lock()
        self.db.execute('''CREATE TABLE IF NOT EXISTS studio_drafts(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,
            title TEXT,version INTEGER,payload TEXT,production_id TEXT,created TEXT,updated TEXT)''')

    def validate_assets(self,body):
        if not self.db.query('SELECT id FROM accounts WHERE id=?',(body.account,)):raise ValueError('账号不存在')
        for identifier,kind in [(body.reference_asset,'video'),(body.clothing_asset,'image')]:
            if identifier and not self.db.query('SELECT id FROM assets WHERE id=? AND kind=?',(identifier,kind)):raise ValueError('参考素材不存在或类型不匹配')

    def environment(self):
        cfg=self.db.settings();vault=SecretVault();config=settings(self.db);issues=[]
        for role in ['text','vision']:
            model=getattr(cfg,role+'_model')
            if not model.model:issues.append('尚未配置'+('文本' if role=='text' else '视觉')+'模型')
            elif model.provider=='deepseek' and not vault.get('deepseek'):issues.append('尚未保存 DeepSeek 凭据')
        if not vault.get('runninghub'):issues.append('尚未配置 RunningHub API 凭据')
        stages={}
        for stage,required in [('image',{'positive'}),('dress',{'character','clothing'}),('motion',{'character','video'})]:
            b=getattr(config,stage);schema=self.db.get('rh_schema:'+b.app_id,{})
            messages=[];keys={n['key'] for n in schema.get('nodes',[])}
            if not schema or b.schema_hash!=schema.get('hash') or not required<=b.fields.keys() or not set(b.fields.values())<=keys:messages.append('需配置'+{'image':'角色图','dress':'换衣','motion':'动作迁移'}[stage]+'应用字段')
            price=self.engine.production.rh.price(b.app_id,schema.get('hash',''))
            if not price['verified']:messages.append('需核对'+{'image':'角色图','dress':'换衣','motion':'动作迁移'}[stage]+'固定计费依据')
            stages[stage]={'issues':messages,'unit':price.get('unit')}
        return {'issues':list(dict.fromkeys(issues)),'stages':stages}

    def decorate(self,row,env=None):
        r=dict(row);r['inputs']=json.loads(r.pop('payload'));job_id=r['inputs'].pop('_reference_job','')
        r['reference_job']=None
        if job_id:
            job=self.db.query('SELECT id,state,message,payload FROM jobs WHERE id=?',(job_id,))
            if job:
                j=job[0];r['reference_job']={k:j[k] for k in ['id','state','message']}
                if j['state']=='completed':
                    for a in json.loads(j['payload']).get('asset_ids',[]):
                        if self.db.query('SELECT id FROM assets WHERE id=? AND kind=?',(a,'video')):r['inputs']['reference_asset']=a;break
        if not r['production_id']:
            old=self.db.query('SELECT id FROM productions WHERE request_id=?',('studio:'+r['id'],))
            if old:r['production_id']=old[0]['id']
        issues=[];inputs=r['inputs'];env=env if env is not None else self.environment()
        if not inputs['ds_budget']:issues.append('填写 DeepSeek 预算')
        if not inputs['rh_budget']:issues.append('填写 RunningHub 预算')
        if inputs.get('reference_input') and not inputs['reference_asset']:issues.append('先导入参考链接，等待下载完成')
        for key in ['reference_asset','clothing_asset']:
            a=inputs[key]
            if a:
                rows=self.db.query('SELECT path FROM assets WHERE id=?',(a,))
                if not rows or not Path(rows[0]['path']).is_file():issues.append('参考素材文件已移动或删除')
        issues+=env['issues']
        for stage in ['image','motion']+(['dress'] if inputs['clothing_asset'] else []):
            issues+=env['stages'][stage]['issues']
            unit=env['stages'][stage]['unit']
            if unit and unit!=inputs['rh_unit']:issues.append('应用计费单位与项目预算不符')
        r['issues']=list(dict.fromkeys(issues));r['ready']=not issues and not r['production_id'];return r

    def get(self,identifier,env=None):
        rows=self.db.query('SELECT * FROM studio_drafts WHERE id=?',(identifier,))
        if not rows:raise HTTPException(404,'草稿不存在')
        return self.decorate(rows[0],env)

    def list(self):
        env=self.environment()
        return [self.decorate(r,env) for r in self.db.query('SELECT * FROM studio_drafts ORDER BY updated DESC LIMIT 200')]

    def create(self,body:DraftCreate):
        data=body.model_dump(exclude={'request_id'});self.validate_assets(body)
        old=self.db.query('SELECT id FROM studio_drafts WHERE request_id=?',(body.request_id,))
        if old:return self.get(old[0]['id'])
        identifier=uid();stamp=now()
        self.db.execute('INSERT OR IGNORE INTO studio_drafts VALUES(?,?,?,?,?,?,?,?)',(identifier,body.request_id,body.title,1,json.dumps(data,ensure_ascii=False),'',stamp,stamp))
        old=self.db.query('SELECT id FROM studio_drafts WHERE request_id=?',(body.request_id,))
        return self.get(old[0]['id'])

    def update(self,identifier,body:DraftUpdate):
        old=self.get(identifier);self.validate_assets(body)
        if old['production_id']:raise HTTPException(409,'该草稿已启动，请复制为新创作')
        data=body.model_dump(exclude={'expected_version'})
        raw=json.loads(self.db.query('SELECT payload FROM studio_drafts WHERE id=?',(identifier,))[0]['payload'])
        if raw.get('reference_input')==data['reference_input'] and not data['reference_asset'] and raw.get('_reference_job'):data['_reference_job']=raw['_reference_job']
        n=self.db.execute("UPDATE studio_drafts SET title=?,payload=?,version=version+1,updated=? WHERE id=? AND version=? AND production_id=''",(body.title,json.dumps(data,ensure_ascii=False),now(),identifier,body.expected_version))
        if not n:raise HTTPException(409,'草稿已被其他窗口修改，请重新载入；你的未保存内容仍保留')
        return self.get(identifier)

    def remove(self,identifier,version):
        old=self.get(identifier)
        if old['production_id']:raise HTTPException(409,'已启动项目保留在项目列表中')
        if not self.db.execute("DELETE FROM studio_drafts WHERE id=? AND version=? AND production_id=''",(identifier,version)):raise HTTPException(409,'草稿版本已变化，请刷新')

    def reference(self,identifier,version):
        old=self.get(identifier)
        if old['production_id'] or old['version']!=version:raise HTTPException(409,'草稿已变化，请刷新')
        value=old['inputs']['reference_input'].strip()
        if not value:raise ValueError('请先填写抖音作品链接或ID')
        request='studio-ref:'+identifier+':'+str(version)
        job=self.db.job('download',{'input':value,'account':old['inputs']['account']},request)
        raw=json.loads(self.db.query('SELECT payload FROM studio_drafts WHERE id=?',(identifier,))[0]['payload']);raw['_reference_job']=job
        self.db.execute('UPDATE studio_drafts SET payload=? WHERE id=?',(json.dumps(raw,ensure_ascii=False),identifier))
        return self.get(identifier)

    async def start(self,identifier,version):
        async with self.lock:
            old=self.get(identifier)
            if old['production_id']:return self.engine.production.get(old['production_id'])
            if old['version']!=version:raise HTTPException(409,'草稿版本已变化，请刷新后启动')
            if old['issues']:raise HTTPException(409,{'message':'项目条件尚未齐备','issues':old['issues']})
            data={k:v for k,v in old['inputs'].items() if k not in {'title','reference_input'}}
            body=ProductionRequest(request_id='studio:'+identifier,count=1,**data)
            result=self.engine.production.create(body)
            self.db.execute('UPDATE studio_drafts SET production_id=?,updated=? WHERE id=?',(result['id'],now(),identifier))
            self.engine.production.tick();return result

    def context(self,context):
        if context['kind']=='draft':
            r=self.get(context['id']);return {'kind':'draft','id':r['id'],'title':r['title'],'account':r['inputs']['account'],'inputs':r['inputs'],'issues':r['issues'],'version':r['version']}
        r=self.engine.production.get(context['id']);p=r['payload']
        return {'kind':'production','id':r['id'],'title':'舞蹈项目 '+r['id'][:6],'account':p['request']['account'],'state':r['state'],'stage':p['stage'],'request':p['request'],'items':p['items'],'message':r['message'],'budget':r['budget']}
