"""Small plugin registry: fixed local tools and opt-in MCP stdio contributions.

Inspired by DSH's manifest/registry/lifecycle separation; no Cordis code loaded.
"""
from __future__ import annotations
import asyncio, hashlib, json, os, re
from pathlib import Path
from typing import Literal
from contextlib import asynccontextmanager
from pydantic import BaseModel, ConfigDict, Field, field_validator
from jsonschema import Draft202012Validator
from .core import now, redact

def schema(**fields):
    required=[k for k,v in fields.items() if 'default' not in v]
    return {'type':'object','properties':fields,'required':required,'additionalProperties':False}

TEXT={'type':'string','maxLength':2000}
COUNT={'type':'integer','minimum':1,'maximum':30,'default':10}

def tool(name,description,arguments,read_only=True):
    return {'name':name,'description':description,'inputSchema':arguments,'read_only':read_only}

MEDIA=[
 tool('library_search','按文字和类型搜索本地素材，返回真实素材ID',schema(query={**TEXT,'default':''},kind={'type':'string','enum':['all','image','video'],'default':'all'},count=COUNT)),
 tool('search_videos','搜索抖音视频；标题不能证明单人或不挡脸，下载后再分析',schema(keyword={**TEXT,'minLength':1},count=COUNT)),
 tool('download_video','下载一个真实抖音作品链接或ID到资料库',schema(input={**TEXT,'minLength':1}),False),
 tool('analyze_asset','分析资料库图片或视频；视频抽5帧，并非逐帧审核',schema(asset_id=TEXT,requirements={**TEXT,'default':''}),False),
 tool('reverse_image','图片逆向正负提示词并保存Markdown',schema(asset_id=TEXT,rewrite={'type':'boolean','default':False}),False),
 tool('runninghub_apps','读取已经同步的RunningHub收藏应用；不提交生成',schema()),
 tool('production_status','读取已有自动生产任务和预算；不修改权限',schema()),
]
NOTES=[
 tool('note_list','读取本机保存的制作笔记',schema()),
 tool('note_save','根据用户要求保存可编辑制作笔记；不修改权限或预算',schema(title={**TEXT,'minLength':1},text={**TEXT,'minLength':1}),False),
]
BUILTINS={
 'moflow.media':{'id':'moflow.media','name':'素材工具箱','version':'1.0.0','description':'本地素材、抖音搜索下载、视觉分析与提示词逆向。','kind':'builtin','tools':MEDIA},
 'moflow.notes':{'id':'moflow.notes','name':'制作笔记','version':'1.0.0','description':'让聊天助手保存、查询本机制作笔记。','kind':'builtin','tools':NOTES},
}

class PluginManifest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    format:Literal['moflow-plugin/v1']='moflow-plugin/v1'
    id:str=Field(pattern=r'^[a-z][a-z0-9.-]{2,70}$')
    name:str=Field(min_length=1,max_length=80)
    version:str=Field(min_length=1,max_length=40)
    description:str=Field('',max_length=2000)
    kind:Literal['mcp']='mcp'
    command:str=Field(min_length=1,max_length=500)
    args:list[str]=Field(default_factory=list,max_length=50)
    cwd:str=''
    @field_validator('cwd')
    @classmethod
    def directory(cls,value):
        if value and (not Path(value).is_absolute() or not Path(value).is_dir()):raise ValueError('MCP 工作目录必须是已存在的绝对目录')
        return value
    @field_validator('command','args')
    @classmethod
    def bounded(cls,value):
        for s in ([value] if isinstance(value,str) else value):
            if len(s)>2000 or '\x00' in s:raise ValueError('启动参数无效或过长')
        return value

class Plugins:
    def __init__(self,engine):
        self.engine=engine;self.db=engine.db
        self.db.execute('CREATE TABLE IF NOT EXISTS chat_plugins(id TEXT PRIMARY KEY,enabled INTEGER,payload TEXT,updated TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS chat_notes(id TEXT PRIMARY KEY,title TEXT,text TEXT,updated TEXT)')
        # Seed once. Removing a built-in must survive restart.
        if not self.db.get('chat_plugins_seeded',False):
            self.install_builtin('moflow.media');self.db.set('chat_plugins_seeded',True)

    def rows(self):
        rows=self.db.query('SELECT * FROM chat_plugins ORDER BY id')
        for r in rows:r['payload']=json.loads(r['payload'])
        return rows

    def get(self,identifier):
        rows=[p for p in self.rows() if p['id']==identifier]
        if not rows:raise ValueError('插件未安装')
        return rows[0]

    def install_builtin(self,identifier):
        if identifier not in BUILTINS:raise ValueError('未知内置插件')
        self.db.execute('INSERT OR IGNORE INTO chat_plugins VALUES(?,?,?,?)',(identifier,1,json.dumps(BUILTINS[identifier],ensure_ascii=False),now()))
        return self.get(identifier)

    def install(self,manifest):
        if 'dsh' in manifest or 'facets' in manifest or 'peerDependencies' in manifest:
            raise ValueError('这是 DSH/Cordis 原生插件，需要 DSH 宿主；墨流支持 moflow-plugin/v1 与 MCP stdio，不执行原生 DSH 包。')
        m=PluginManifest.model_validate(manifest)
        if m.id.startswith('moflow.'):raise ValueError('moflow.* 是内置插件保留名称')
        previous=self.db.query('SELECT id FROM chat_plugins WHERE id=?',(m.id,))
        if previous:raise ValueError('插件已安装；先卸载再安装新版本，避免静默扩大工具权限')
        payload=m.model_dump();payload.update(tools=[],auto_tools=[],state='unconnected')
        self.db.execute('INSERT INTO chat_plugins VALUES(?,?,?,?)',(m.id,0,json.dumps(payload,ensure_ascii=False),now()))
        return self.get(m.id)

    def configure(self,identifier,enabled,auto_tools):
        row=self.get(identifier);p=row['payload']
        known={t['name'] for t in p.get('tools',[])}
        if not set(auto_tools)<=known:raise ValueError('只能授权已经连接并读取到的工具')
        if p['kind']=='mcp' and enabled and not known:raise ValueError('请先连接并读取工具清单')
        p['auto_tools']=list(dict.fromkeys(auto_tools))
        self.db.execute('UPDATE chat_plugins SET enabled=?,payload=?,updated=? WHERE id=?',(int(enabled),json.dumps(p,ensure_ascii=False),now(),identifier))
        return self.get(identifier)

    @asynccontextmanager
    async def connection(self,p):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        # MCP SDK provides a minimal inherited environment, never our credential vault.
        try:
            with open(os.devnull,'w') as errors:
                async with stdio_client(StdioServerParameters(command=p['command'],args=p['args'],cwd=p.get('cwd') or None),errlog=errors) as (reader,writer):
                    async with ClientSession(reader,writer) as session:
                        await session.initialize();yield session
        except ExceptionGroup as group:
            def leaves(error):
                return [leaf for item in error.exceptions for leaf in leaves(item)] if isinstance(error,ExceptionGroup) else [error]
            raise ValueError('MCP 连接或调用失败：'+'；'.join(redact(str(e) or type(e).__name__) for e in leaves(group))[:2000]) from None

    async def inspect(self,identifier):
        row=self.get(identifier);p=row['payload']
        if p['kind']=='builtin':return row
        async with asyncio.timeout(35):
            async with self.connection(p) as session:
                listing=await session.list_tools();tools=[]
                if len(listing.tools)>100:raise ValueError('插件工具超过100个，请使用较小的工具服务器')
                for t in listing.tools:
                    Draft202012Validator.check_schema(t.inputSchema)
                    tools.append({'name':t.name,'description':(t.description or '')[:2000],'inputSchema':t.inputSchema})
        encoded=json.dumps(tools,ensure_ascii=False,sort_keys=True)
        if len(encoded)>200000:raise ValueError('插件工具描述过大')
        p.update(tools=tools,state='ready',schema_hash=hashlib.sha256(encoded.encode()).hexdigest())
        # Reconnecting resets grants; changed schemas cannot inherit old approvals.
        p['auto_tools']=[]
        self.db.execute('UPDATE chat_plugins SET enabled=0,payload=?,updated=? WHERE id=?',(json.dumps(p,ensure_ascii=False),now(),identifier))
        return self.get(identifier)

    def catalog(self):
        out=[]
        for row in self.rows():
            if not row['enabled']:continue
            p=row['payload']
            for t in p.get('tools',[]):
                out.append({**t,'name':row['id']+'::'+t['name'],'plugin':row['id'],'auto':p['kind']=='builtin' or t['name'] in p.get('auto_tools',[])})
        return out

    async def call(self,name,args,account,call_id,confirmed=False):
        candidates=[t for t in self.catalog() if t['name']==name]
        if not candidates:raise ValueError('工具未安装或已停用')
        spec=candidates[0];Draft202012Validator(spec['inputSchema']).validate(args)
        if not spec['auto'] and not confirmed:raise PermissionError('此插件工具尚未授权自动调用，请在聊天步骤中确认本次调用')
        p=self.get(spec['plugin'])['payload'];short=name.split('::',1)[1]
        if p['kind']=='mcp':
            async with asyncio.timeout(120):
                async with self.connection(p) as session:
                    listing=await session.list_tools()
                    current=[t.model_dump(include={'name','description','inputSchema'}) for t in listing.tools]
                    old={t['name']:t for t in p['tools']};live={t['name']:t for t in current}
                    if short not in live or live[short]['inputSchema']!=old[short]['inputSchema']:raise ValueError('工具结构已变化，请在插件页重新连接并授权')
                    result=await session.call_tool(short,args)
                    if result.isError:raise ValueError('MCP 工具返回错误：'+'\n'.join(getattr(c,'text','') for c in result.content)[:2000])
                    return {'content':'\n'.join(getattr(c,'text','[非文本结果]') for c in result.content)[:20000]}
        e=self.engine;db=self.db
        if short=='library_search':
            rows=db.list('assets',100000);q=args.get('query','').lower();kind=args.get('kind','all')
            rows=[a for a in rows if (kind=='all' or a['kind']==kind) and (not q or q in (a['path']+json.dumps(a['payload'],ensure_ascii=False)).lower())]
            return {'total':len(rows),'items':[{'asset_id':a['id'],'kind':a['kind'],'filename':Path(a['path']).name,'available':Path(a['path']).is_file(),'analysis':a['payload'].get('analysis'),'prompt':a['payload'].get('prompt')} for a in rows[:args.get('count',10)]]}
        if short=='search_videos':
            rows=await e.platform.search(args['keyword'],account,count=args.get('count',10))
            return {'items':[{k:r.get(k) for k in ['id','title','author','duration','source','published','stats']} for r in rows]}
        if short=='download_video':
            job=db.job('download',{'input':args['input'],'account':account},'chat-'+call_id)
            row=db.query('SELECT state,payload FROM jobs WHERE id=?',(job,))[0]
            if row['state']=='completed':return {'job_id':job,**json.loads(row['payload'])}
            db.update_job(job,'running','聊天工具下载')
            try:return {'job_id':job,'asset_ids':await e.download({'input':args['input'],'account':account},job)}
            except BaseException:
                db.update_job(job,'paused','聊天下载中断；可在下载页续跑');raise
        if short in {'analyze_asset','reverse_image'}:
            asset=db.query('SELECT kind,path FROM assets WHERE id=?',(args['asset_id'],))
            if not asset or not Path(asset[0]['path']).is_file():raise ValueError('素材不存在或文件已移动')
            return await (e.analyze_asset(args['asset_id'],args.get('requirements','')) if short=='analyze_asset' else e.reverse_asset(args['asset_id'],args.get('rewrite',False)))
        if short=='runninghub_apps':
            apps=[{k:a.get(k) for k in ['id','name','description','author','source','available']} for a in e.runninghub.apps()]
            return {'total':len(apps),'available_count':sum(bool(a['available']) for a in apps),'apps':apps}
        if short=='production_status':
            return {'items':[{'id':a['id'],'state':a['state'],'message':a['message'],'budget':a['budget']} for a in e.production.list()]}
        if short=='note_list':return {'items':db.query('SELECT * FROM chat_notes ORDER BY updated DESC LIMIT 50')}
        if short=='note_save':
            db.execute('INSERT OR REPLACE INTO chat_notes VALUES(?,?,?,?)',(call_id,args['title'],args['text'],now()));return {'saved':True,'id':call_id,'title':args['title']}
        raise ValueError('插件未注册该执行器')
