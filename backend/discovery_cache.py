"""Small account-scoped search index. No models, downloads or implicit feedback."""
from __future__ import annotations
import asyncio,json,re,time

TTL=300
CORPUS_TTL=1800

class SearchCache:
    def __init__(self,db,fetch):
        self.db=db;self.fetch=fetch;self.tasks={}
        db.execute('CREATE TABLE IF NOT EXISTS discovery_queries(account TEXT,kind TEXT,query TEXT,requested INTEGER,payload TEXT,updated REAL,PRIMARY KEY(account,kind,query))')
        db.execute('CREATE TABLE IF NOT EXISTS discovery_candidates(account TEXT,id TEXT,payload TEXT,updated REAL,PRIMARY KEY(account,id))')
    def remember(self,account,rows):
        stamp=time.time()
        with self.db.conn() as c:
            for row in rows:
                if row.get('id'):c.execute('INSERT OR REPLACE INTO discovery_candidates VALUES(?,?,?,?)',(account,str(row['id']),json.dumps(row,ensure_ascii=False),stamp))
            c.execute('DELETE FROM discovery_candidates WHERE account=? AND id NOT IN (SELECT id FROM discovery_candidates WHERE account=? ORDER BY updated DESC,id LIMIT 500)',(account,account))
    async def search(self,keyword,account='default',kind='video',count=20,refresh=False):
        if not self.db.query('SELECT id FROM accounts WHERE id=?',(account,)):raise ValueError('账号不存在')
        query=re.sub(r'\s+',' ',keyword.strip())
        if not query:raise ValueError('请输入搜索关键词')
        if kind not in {'video','user'}:raise ValueError('搜索类型须为作品或用户')
        start=time.monotonic();stamp=time.time()
        if not refresh:
            cached=self.db.query('SELECT * FROM discovery_queries WHERE account=? AND kind=? AND query=?',(account,kind,query))
            if cached and stamp-cached[0]['updated']<TTL and cached[0]['requested']>=count:
                return self.result(json.loads(cached[0]['payload'])[:count],'query_cache',start,cached[0]['updated'])
            if kind=='video':
                terms=query.casefold().split(' ');matches=[]
                for record in self.db.query('SELECT * FROM discovery_candidates WHERE account=? AND updated>? ORDER BY updated DESC,id',(account,stamp-CORPUS_TTL)):
                    row=json.loads(record['payload']);text=str(row.get('title','')).casefold()
                    if all(term in text for term in terms):matches.append((row,record['updated']))
                if len(matches)>=min(10,count):
                    return self.result([r for r,_ in matches[:count]],'candidate_cache',start,min(t for _,t in matches[:count]))
        key=(account,kind,query,count)
        if key not in self.tasks:
            task=asyncio.create_task(self.load(query,account,kind,count));self.tasks[key]=task
            task.add_done_callback(lambda done:self.finished(key,done))
        rows,updated=await asyncio.shield(self.tasks[key])
        return self.result(rows,'live',start,updated)
    def finished(self,key,task):
        self.tasks.pop(key,None)
        if not task.cancelled():task.exception() # Retrieve even if its HTTP caller disconnected.
    async def load(self,query,account,kind,count):
        rows=await self.fetch(query,account,kind,count);stamp=time.time()
        self.db.execute('INSERT OR REPLACE INTO discovery_queries VALUES(?,?,?,?,?,?)',(account,kind,query,count,json.dumps(rows,ensure_ascii=False),stamp))
        self.db.execute('DELETE FROM discovery_queries WHERE account=? AND rowid NOT IN (SELECT rowid FROM discovery_queries WHERE account=? ORDER BY updated DESC LIMIT 100)',(account,account))
        if kind=='video':self.remember(account,rows)
        return rows,stamp
    def result(self,rows,source,start,updated):
        return {'items':rows,'search':{'source':source,'elapsed_ms':round((time.monotonic()-start)*1000),'updated':updated,'cached':source!='live','visual_verified':False}}
    async def close(self):
        tasks=list(self.tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
