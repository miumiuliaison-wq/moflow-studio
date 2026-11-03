"""Official RunningHub AI App API; no browser cookies and no model-owned credentials."""
from __future__ import annotations
import asyncio, hashlib, ipaddress, json, socket, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urljoin
import httpx
from pydantic import BaseModel, Field, ConfigDict
from .core import SecretVault, ROOT, Waiting, ModelRequired, now, clean, uid
from .billing import Ledger, BudgetRequired

SITE='https://www.runninghub.ai'

class UncertainSubmission(Waiting): pass

class FixedPrice(BaseModel):
    model_config=ConfigDict(extra='forbid')
    amount: float=Field(gt=0,le=100000)
    unit: str=Field(pattern=r'^(RH币|USD|CNY)$')
    source: str
    confirmed_fixed: bool=False
    schema_hash: str

def schema_digest(nodes):
    return hashlib.sha256(json.dumps(nodes,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def normalize_schema(data):
    nodes=data.get('nodeInfoList',[])
    if not isinstance(nodes,list) or not nodes:raise Waiting('应用没有可调用的参数，请先在官网检查 API 权限')
    result=[];seen=set()
    for n in nodes:
        key=str(n.get('nodeId',''))+':'+str(n.get('fieldName',''))
        if not n.get('nodeId') or not n.get('fieldName') or key in seen:raise Waiting('应用字段标识不明确')
        seen.add(key)
        kind=str(n.get('fieldType','')).upper()
        # Never infer media inputs from their order or their file extension.
        if not kind:
            name=str(n.get('nodeName','')).lower()
            kind='IMAGE' if name=='loadimage' else 'VIDEO' if 'loadvideo' in name else 'AUDIO' if name=='loadaudio' else 'LIST' if n.get('fieldData') else 'STRING' if str(n.get('fieldName','')).lower() in {'prompt','text','positive','negative'} else 'UNKNOWN'
        result.append({'key':key,'nodeId':str(n['nodeId']),'fieldName':str(n['fieldName']),
                       'label':str(n.get('description') or n['fieldName'])[:500],'type':kind,
                       'default':n.get('fieldValue'),'options':n.get('fieldData'),
                       'min':n.get('min'),'max':n.get('max')})
    return result

class RunningHubAPI:
    def __init__(self,engine):
        self.engine=engine;self.db=engine.db;self.vault=SecretVault();self.serial=asyncio.Lock();self.ledger=Ledger(self.db)
        with self.db.conn() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS production_rh_tasks(
                id TEXT PRIMARY KEY,batch TEXT,app_id TEXT,state TEXT,task_id TEXT,payload TEXT,updated TEXT);''')

    def key(self):
        key=self.vault.get('runninghub')
        if not key:raise ModelRequired('请在设置中配置 RunningHub API Key（与 DeepSeek 密钥不同）')
        return key

    async def request(self,path,payload=None,params=None,files=None,retry=True):
        key=self.key()
        async with httpx.AsyncClient(timeout=90,trust_env=False,follow_redirects=False) as client:
            for attempt in range(3 if retry else 1):
                try:
                    if params is not None:
                        response=await client.get(SITE+path,params={**params,'apiKey':key},headers={'Authorization':'Bearer '+key})
                    elif files is not None:
                        # Reset stream on a retry, otherwise a truncated upload can be accepted.
                        for _,(_,stream,_) in files.items():stream.seek(0)
                        response=await client.post(SITE+path,data={'apiKey':key,'fileType':'input'},files=files,headers={'Authorization':'Bearer '+key})
                    else:
                        response=await client.post(SITE+path,json={**(payload or {}),'apiKey':key},headers={'Authorization':'Bearer '+key})
                    response.raise_for_status();body=response.json()
                    if str(body.get('code'))!='0':
                        raise Waiting('RunningHub API 返回 '+str(body.get('code'))+'，请检查账号权益、余额或应用状态')
                    return body.get('data')
                except (httpx.HTTPError,ValueError):
                    if not retry or attempt==2:raise Waiting('RunningHub 网络或响应异常') from None
                    await asyncio.sleep(2**attempt)

    async def check(self):
        data=await self.request('/uc/openapi/accountStatus',{})
        # Return only non-secret account entitlement/balance values.
        return {'connected':True,'account':{k:v for k,v in (data or {}).items() if k in {'remainCoins','remainMoney','currentTaskCounts','apiType'}}}

    async def schema(self,app_id):
        if not str(app_id).isdigit():raise ValueError('应用 ID 必须是数字字符串')
        data=await self.request('/api/webapp/apiCallDemo',params={'webappId':str(app_id)})
        if not isinstance(data,dict):raise Waiting('应用参数不可用')
        nodes=normalize_schema(data);result={'app_id':str(app_id),'name':str(data.get('webappName','')),
                                            'nodes':nodes,'hash':schema_digest(nodes),'fetched':now()}
        self.db.set('rh_schema:'+str(app_id),result)
        return {**result,'pricing':self.price(str(app_id),result['hash'])}

    def price(self,app_id,digest):
        evidence=self.db.get('rh_price:'+str(app_id),{})
        valid=bool(evidence and evidence.get('confirmed_fixed') and evidence.get('schema_hash')==digest)
        if valid:
            valid=(datetime.now(timezone.utc)-datetime.fromisoformat(evidence['checked'])).total_seconds()<86400
        return {**evidence,'verified':valid,'message':'' if valid else '缺少与当前参数对应的固定计费依据；浮动估价不能作为自动生成最高费用。'}

    def save_fixed_price(self,app_id,body:FixedPrice):
        schema=self.db.get('rh_schema:'+str(app_id),{})
        url=urlsplit(body.source)
        if url.scheme!='https' or url.hostname not in {'www.runninghub.ai','www.runninghub.cn'} or url.username or url.password:
            raise ValueError('计费依据必须是 RunningHub 官网 HTTPS 页面')
        if not body.confirmed_fixed:raise ValueError('只有平台明确固定计价的应用才可登记；估价不能登记')
        if schema.get('hash')!=body.schema_hash:raise ValueError('应用参数已变化，请重新读取')
        # This is an explicit operator attestation, never an inferred or model-created quote.
        record={**body.model_dump(),'source':url._replace(query='',fragment='').geturl(),'checked':now(),'attested_by':'local_user'}
        self.db.set('rh_price:'+str(app_id),record);return self.price(app_id,body.schema_hash)

    async def upload(self,path):
        path=Path(path)
        if not path.is_file() or path.stat().st_size>30*1024**2:raise Waiting('RunningHub 输入超过30MB或文件缺失，请调整素材')
        import mimetypes
        with path.open('rb') as stream:
            data=await self.request('/task/openapi/upload',files={'file':(path.name,stream,mimetypes.guess_type(path.name)[0] or 'application/octet-stream')})
        if not isinstance(data,dict) or not data.get('fileName'):raise Waiting('上传没有返回文件标识')
        return str(data['fileName'])

    def row(self,identifier):
        found=self.db.query('SELECT * FROM production_rh_tasks WHERE id=?',(identifier,))
        if not found:return None
        row=found[0];row['payload']=json.loads(row['payload']);return row

    def save(self,identifier,state,payload,task_id=None):
        if task_id is None:self.db.execute('UPDATE production_rh_tasks SET state=?,payload=?,updated=? WHERE id=?',(state,json.dumps(payload,ensure_ascii=False),now(),identifier))
        else:self.db.execute('UPDATE production_rh_tasks SET state=?,payload=?,task_id=?,updated=? WHERE id=?',(state,json.dumps(payload,ensure_ascii=False),str(task_id),now(),identifier))

    async def generate(self,batch,identifier,binding,values,files,check):
        async with self.serial:
            row=self.row(identifier)
            if row and row['state']=='completed':return row['payload']['asset_ids']
            if row and row['state'] in {'submitting','uncertain'} and not row['task_id']:
                raise UncertainSubmission('提交回执不明确，请在官网历史核对后填写原任务 ID，禁止自动重发')
            if not row or not row['task_id']:
                check();schema=await self.schema(binding.app_id)
                if binding.schema_hash!=schema['hash']:raise Waiting('应用字段未映射或配置已变更，请在设置核对：'+binding.app_id)
                price=self.price(binding.app_id,schema['hash'])
                if not price['verified']:raise BudgetRequired(price['message'])
                fields={n['key']:n for n in schema['nodes']};mapped={}
                for semantic,value in values.items():
                    if semantic not in binding.fields:raise Waiting('缺少应用参数映射：'+semantic)
                    mapped[binding.fields[semantic]]=value
                for key,value in binding.defaults.items():mapped[key]=value
                for semantic,path in files.items():
                    key=binding.fields.get(semantic)
                    if not key or key not in fields:raise Waiting('缺少素材参数映射：'+semantic)
                    mapped[key]=await self.upload(path)
                if any(k not in fields for k in mapped):raise Waiting('映射引用的字段已不存在')
                nodes=[]
                for n in schema['nodes']:
                    value=mapped.get(n['key'],n['default'])
                    if value is None or value=='':raise Waiting('请补齐应用字段：'+n['label'])
                    if n['type'] in {'IMAGE','VIDEO','AUDIO'} and n['key'] not in mapped:raise Waiting('媒体字段必须显式映射：'+n['label'])
                    if n['type']=='INT':
                        try:value=int(str(value))
                        except (ValueError,TypeError):raise Waiting('整数参数无效：'+n['label'])
                    elif n['type']=='FLOAT':value=float(value)
                    elif n['type']=='BOOLEAN':
                        if str(value).lower() not in {'true','false','0','1'}:raise Waiting('开关参数无效：'+n['label'])
                        value=str(value).lower() in {'true','1'}
                    elif n['type'] not in {'STRING','IMAGE','VIDEO','AUDIO','LIST'}:raise Waiting('暂不支持字段类型：'+n['type'])
                    if n['type'] in {'INT','FLOAT'}:
                        import math
                        if not math.isfinite(value) or (n['min'] is not None and value<float(n['min'])) or (n['max'] is not None and value>float(n['max'])):raise Waiting('数值超出范围：'+n['label'])
                    if n['type']=='LIST' and n['options']:
                        options=json.loads(n['options']) if isinstance(n['options'],str) else n['options']
                        allowed=[str(o.get('index',o.get('name'))) for o in options if isinstance(o,dict) and ('index' in o or 'name' in o)]
                        if allowed and str(value) not in allowed:raise Waiting('选项已失效：'+n['label'])
                    nodes.append({'nodeId':n['nodeId'],'fieldName':n['fieldName'],'fieldValue':value})
                check();self.db.check_space()
                charge=self.ledger.reserve(batch,'runninghub',price['unit'],price['amount'],identifier)
                payload={'nodes':nodes,'charge_id':charge,'price':price,'schema_hash':schema['hash']}
                with self.db.conn() as c:
                    c.execute('INSERT INTO production_rh_tasks VALUES(?,?,?,?,?,?,?)',(identifier,batch,binding.app_id,'submitting','',json.dumps(payload,ensure_ascii=False),now()))
                try:
                    data=await self.request('/task/openapi/ai-app/run',{'webappId':str(binding.app_id),'nodeInfoList':nodes},retry=False)
                    if not isinstance(data,dict) or not data.get('taskId'):raise Waiting('未收到任务 ID')
                    self.save(identifier,'running',payload,str(data['taskId']))
                except BaseException:
                    self.save(identifier,'uncertain',payload);self.ledger.settle(charge,state='uncertain');raise
                row=self.row(identifier)
            await self.poll(row,check)
            return await self.download_results(self.row(identifier),check)

    async def poll(self,row,check):
        if row['state'] in {'download_ready','completed'}:return
        for _ in range(180):
            check()
            status=await self.request('/task/openapi/status',{'taskId':row['task_id']})
            if isinstance(status,dict):status=status.get('taskStatus') or status.get('status')
            if status in {'SUCCESS','COMPLETED'}:
                outputs=await self.request('/task/openapi/outputs',{'taskId':row['task_id']})
                if not isinstance(outputs,list) or not outputs:raise Waiting('平台完成但尚未返回产物')
                payload={**row['payload'],'outputs':outputs};self.save(row['id'],'download_ready',payload)
                # Fees can repeat on each output: they describe one task, not each file.
                field={'RH币':'consumeCoins','CNY':'consumeMoney','USD':'consumeMoney'}[payload['price']['unit']]
                amounts=[o[field] for o in outputs if o.get(field) is not None]
                extras=[o.get('thirdPartyConsumeMoney') for o in outputs if o.get('thirdPartyConsumeMoney') not in (None,0,'0','0.0')]
                if amounts and not extras and field=='consumeCoins':self.ledger.settle(payload['charge_id'],max(float(a) for a in amounts))
                else:self.ledger.settle(payload['charge_id'],state='uncertain')
                return
            if status in {'FAILED','FAIL','CANCELLED'}:
                self.save(row['id'],'cloud_failed',row['payload']);self.ledger.settle(row['payload']['charge_id'],state='uncertain')
                raise Waiting('RunningHub 云端任务失败；已保留任务 ID，费用待核对')
            await asyncio.sleep(5)
        raise Waiting('云端仍在排队或执行，已保存任务 ID，恢复时继续查询')

    async def download_results(self,row,check):
        if row['state']=='completed':return row['payload']['asset_ids']
        folder=self.engine.runninghub.download_directory();assets=[]
        for i,output in enumerate(row['payload']['outputs']):
            check();url=output.get('fileUrl','');suffix='.'+str(output.get('fileType','')).lower().lstrip('.')
            if suffix not in {'.jpg','.jpeg','.png','.webp','.mp4','.mov','.webm','.mkv','.zip'}:continue
            dest=folder/(row['id']+'-'+str(i)+suffix);part=dest.with_suffix(suffix+'.part')
            if not dest.exists():
                await self.fetch_file(url,part,check);part.replace(dest)
            result=await self.engine.runninghub.register_download(str(dest),SITE+'/zh-cn/ai-detail/'+row['app_id'])
            assets.extend(result['asset_ids'])
            if suffix!='.zip' and dest.exists() and result['asset_ids']:
                registered=self.db.query('SELECT path FROM assets WHERE id=?',(result['asset_ids'][0],))
                if registered and Path(registered[0]['path']).resolve()!=dest.resolve():dest.unlink()
        if not assets:raise Waiting('没有可验证的图片或视频产物')
        self.save(row['id'],'completed',{**row['payload'],'asset_ids':assets})
        return assets

    async def fetch_file(self,url,path,check):
        async def validate(target):
            u=urlsplit(target);host=u.hostname or ''
            if u.scheme!='https' or u.username or u.password or not any(host==d or host.endswith('.'+d) for d in ('runninghub.ai','runninghub.cn','xiaoyaoyou.com')):raise ValueError('产物地址不属于 RunningHub CDN')
            records=await asyncio.to_thread(socket.getaddrinfo,host,443,0,socket.SOCK_STREAM)
            if any(not ipaddress.ip_address(r[4][0]).is_global for r in records):raise ValueError('产物地址不能指向内网')
        async with httpx.AsyncClient(timeout=90,trust_env=False,follow_redirects=False) as client:
            for _ in range(5):
                await validate(url)
                async with client.stream('GET',url) as response:
                    if response.is_redirect:url=urljoin(url,response.headers['location']);continue
                    response.raise_for_status();size=int(response.headers.get('content-length','0'));self.db.check_space(size)
                    received=0
                    try:
                        with path.open('wb') as out:
                            async for chunk in response.aiter_bytes(256*1024):
                                check();self.db.check_space(len(chunk));received+=len(chunk);out.write(chunk)
                        if size and received!=size:raise ValueError('产物下载不完整')
                        return
                    except BaseException:path.unlink(missing_ok=True);raise
            raise ValueError('产物重定向次数过多')
