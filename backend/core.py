from __future__ import annotations
import base64, ctypes, hashlib, json, os, re, secrets, shutil, sqlite3, threading, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(os.environ.get('MOFLOW_DATA', r'D:\素材采集\studio\data'))
ROOT.mkdir(parents=True, exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat()
def uid(): return uuid.uuid4().hex
def clean(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(s)).strip(' .')[:90] or 'untitled'
    return '_'+s if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',s) else s
def words(value): return list(dict.fromkeys(v.strip() for v in (re.split(r'[\n\r,，]+',value) if isinstance(value,str) else value) if v.strip()))
def redact(value):
    text = str(value)
    text = re.sub(r'(?i)sk-[a-z0-9_-]{12,}', '[redacted]', text)
    text = re.sub(r'(?i)([?&](?:apiKey|token|key)=)[^&\s]+',r'\1[redacted]',text)
    text = re.sub(r'(?i)(authorization|cookie|api[_-]?key|token)["\s:=]+[^\s,;}]+',r'\1=[redacted]',text)
    return text[:2000]

class ModelConfig(BaseModel):
    provider: str = 'ollama'
    base_url: str = 'http://127.0.0.1:11434'
    model: str = ''
    @field_validator('provider')
    @classmethod
    def provider_check(cls,v):
        if v not in {'ollama','compatible','deepseek'}: raise ValueError('不支持的模型来源')
        return v
    @field_validator('base_url')
    @classmethod
    def base_check(cls,v):
        from urllib.parse import urlparse
        u=urlparse(v)
        if u.scheme not in {'http','https'} or not u.hostname or u.username or u.password: raise ValueError('模型地址须为HTTP地址，不能内嵌密钥')
        return v.rstrip('/')

    @model_validator(mode='after')
    def official_deepseek(self):
        if self.provider=='deepseek':
            self.base_url='https://api.deepseek.com'
            self.model=self.model or 'deepseek-flash'
        return self

class Settings(BaseModel):
    mcp_rule_id: str = ''
    storage_root: str = r'D:\素材采集'
    comfyui_material_dirs: list[str] = Field(default_factory=lambda:[r'C:\Users\lenovo\comfyui\图片',r'C:\Users\lenovo\comfyui\视频'])
    reference_image_dir: str = r'C:\Users\lenovo\comfyui\图片\生成'
    reference_video_dir: str = r'D:\素材采集\视频\墨流'
    @field_validator('comfyui_material_dirs')
    @classmethod
    def material_paths(cls,values):
        if len(values)>20:raise ValueError('ComfyUI 素材目录最多20个')
        if any(not Path(v).is_absolute() for v in values):raise ValueError('素材目录必须是绝对路径')
        return list(dict.fromkeys(str(Path(v).resolve()) for v in values))
    reserve_gb: float = Field(10, ge=.05, le=1000)
    quota_gb: float = Field(8,ge=.1,le=10000)
    concurrent_downloads: int = Field(2,ge=1,le=5)
    quality: str = 'highest'
    filename_template: str = '{author}_{title}_{id}'
    save_cover: bool = False
    save_metadata: bool = True
    download_images: bool = True
    download_video: bool = True
    request_interval: float = Field(3,ge=2,le=120)
    text_model: ModelConfig = Field(default_factory=ModelConfig)
    vision_model: ModelConfig = Field(default_factory=ModelConfig)
    @field_validator('storage_root','reference_image_dir','reference_video_dir')
    @classmethod
    def absolute(cls,v):
        if not Path(v).is_absolute(): raise ValueError('素材目录必须是绝对路径')
        return str(Path(v).resolve())

class SecretVault:
    def __init__(self): self.path=ROOT/'secrets.json'
    def crypt(self,data:bytes,decrypt=False):
        if os.name!='nt': raise RuntimeError('凭据存储需要 Windows DPAPI')
        class Blob(ctypes.Structure): _fields_=[('size',ctypes.c_ulong),('data',ctypes.POINTER(ctypes.c_ubyte))]
        buf=ctypes.create_string_buffer(data); source=Blob(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte))); out=Blob()
        fn=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
        if not fn(ctypes.byref(source),None,None,None,None,0,ctypes.byref(out)): raise ctypes.WinError()
        try: return ctypes.string_at(out.data,out.size)
        finally: ctypes.windll.kernel32.LocalFree(out.data)
    def get(self,key):
        values=json.loads(self.path.read_text()) if self.path.exists() else {}
        return self.crypt(base64.b64decode(values[key]),True).decode() if key in values else ''
    def set(self,key,value):
        values=json.loads(self.path.read_text()) if self.path.exists() else {}
        if value: values[key]=base64.b64encode(self.crypt(value.encode())).decode()
        else: values.pop(key,None)
        tmp=self.path.with_suffix('.tmp'); tmp.write_text(json.dumps(values)); tmp.replace(self.path)

class Store:
    def __init__(self,path=None):
        self.path=Path(path) if path else ROOT/'studio.sqlite3'
        with self.conn() as c:
            c.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,name TEXT,state TEXT,payload TEXT,updated TEXT);
            CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,source_id TEXT,kind TEXT,path TEXT UNIQUE,sha TEXT,bytes INTEGER,created TEXT,imported INTEGER,payload TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS asset_source ON assets(source_id,path);
            CREATE INDEX IF NOT EXISTS asset_hash ON assets(sha);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,state TEXT,progress REAL,payload TEXT,message TEXT,created TEXT,updated TEXT);
            CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY,name TEXT,enabled INTEGER,payload TEXT,updated TEXT);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,rule_id TEXT,state TEXT,payload TEXT,created TEXT,updated TEXT);
            CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,rule_id TEXT,account TEXT,target TEXT,action TEXT,state TEXT,payload TEXT,created TEXT,updated TEXT);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,kind TEXT,ref TEXT,message TEXT);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS rh_apps(id TEXT PRIMARY KEY,available INTEGER,payload TEXT,updated TEXT);
            ''')
    @contextmanager
    def conn(self):
        c=sqlite3.connect(self.path,timeout=30); c.row_factory=sqlite3.Row
        try:
            with c: yield c
        finally: c.close()
    def query(self,sql,args=()):
        with self.conn() as c: return [dict(x) for x in c.execute(sql,args)]
    def execute(self,sql,args=()):
        with self.conn() as c: return c.execute(sql,args).rowcount
    def event(self,kind,ref,message): self.execute('INSERT INTO events(at,kind,ref,message) VALUES(?,?,?,?)',(now(),kind,ref,redact(message)))
    def get(self,key,default=None):
        v=self.query('SELECT value FROM kv WHERE key=?',(key,)); return json.loads(v[0]['value']) if v else default
    def set(self,key,value): self.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
    def settings(self): return Settings.model_validate(self.get('settings',{}))
    def list(self,table,limit=100):
        assert table in {'accounts','assets','jobs','rules','runs','actions'}
        data=self.query(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?',(limit,))
        for d in data: d['payload']=json.loads(d['payload'])
        return data
    def job(self,kind,payload,identifier=None):
        identifier=identifier or uid(); stamp=now()
        self.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,?,?)',(identifier,kind,'queued',0,json.dumps(payload,ensure_ascii=False),'等待执行',stamp,stamp))
        return identifier
    def update_job(self,identifier,state,message='',progress=None,payload=None):
        self.execute('UPDATE jobs SET state=?,message=?,updated=? WHERE id=?',(state,redact(message),now(),identifier))
        if progress is not None: self.execute('UPDATE jobs SET progress=? WHERE id=?',(progress,identifier))
        if payload is not None: self.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),identifier))
    def check_space(self,additional=0):
        s=self.settings(); target=Path(s.storage_root); target.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(target).free-additional<s.reserve_gb*1024**3: raise StoragePaused('磁盘空闲低于保留额度')
        count=self.query('SELECT COALESCE(SUM(bytes),0) n FROM assets WHERE imported=0')[0]['n']
        transient=sum(p.stat().st_size for p in (ROOT/'tmp').rglob('*') if p.is_file()) if (ROOT/'tmp').exists() else 0
        indexed={r['path'] for r in self.query('SELECT path FROM assets')}
        rh_folder=target/'下载'/'RunningHub'
        rh_bytes=sum(p.stat().st_size for p in rh_folder.rglob('*') if p.is_file() and str(p) not in indexed) if rh_folder.exists() else 0
        if count+transient+rh_bytes+additional>s.quota_gb*1024**3: raise StoragePaused('本轮素材存储额度已用完，请调整额度或目录')

class Waiting(Exception): pass
class LoginRequired(Waiting): pass
class ModelRequired(Waiting): pass
class StoragePaused(Waiting): pass
class Unsupported(Exception): pass
