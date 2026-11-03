"""Progressive discovery with transparent local ranking, not the platform feed algorithm."""
from __future__ import annotations
import asyncio,re,time
from datetime import datetime
from .core import LoginRequired,now,redact

KEYWORD='美女单人热舞'
KEYWORDS=(KEYWORD,'甜美 单人舞蹈','御姐 单人舞蹈')
DAYS=7
TTL=300
MIN_LIKES=1000
SORTS={'balanced','latest','likes'}

def number(value,default=0):
    try:
        n=float(value)
        return n if n==n and abs(n)!=float('inf') else default
    except (TypeError,ValueError):return default

def rank_candidates(rows,at=None,sort='balanced',limit=18):
    if sort not in SORTS:raise ValueError('不支持的推荐排序')
    at=time.time() if at is None else at;cutoff=at-DAYS*86400;found={}
    for original in rows:
        row=dict(original);title=str(row.get('title') or '');published=number(row.get('published'))
        likes=number((row.get('stats') or {}).get('digg_count'))
        if not row.get('id') or row.get('images') or not published:continue
        if row.get('author_gender') in {1,'1'} or likes<MIN_LIKES:continue
        if not cutoff<=published<=at+300:continue
        if not re.search(r'舞|dance|choreography',title,re.I):continue
        if re.search(r'双人|多人|群舞|齐舞|舞团|各跳各的|三人|四人|两人|二人|男团|男生|男孩|小朋友|幼儿|儿童|宝宝',title):continue
        if re.search(r'教程|教学|虚拟|动画|3d|ai舞|模板|面具|遮脸|挡脸',title,re.I):continue
        row['published']=published
        found[str(row['id'])]=row
    candidates=list(found.values())
    metrics={
        'likes':lambda r:(number((r.get('stats') or {}).get('digg_count')),r['published'],str(r['id'])),
        'latest':lambda r:(r['published'],number((r.get('stats') or {}).get('digg_count')),str(r['id'])),
        # This is a snapshot proxy, never a measured growth rate or views/follower score.
        'momentum':lambda r:(number((r.get('stats') or {}).get('digg_count'))/(max(0,(at-r['published'])/3600)+6)**.5,r['published'],str(r['id']))}
    positions={key:{str(r['id']):i+1 for i,r in enumerate(sorted(candidates,key=metric,reverse=True))} for key,metric in metrics.items()}
    for row in candidates:
        rid=str(row['id']);score=sum(1/(60+positions[k][rid]) for k in metrics)
        row['recommendation']={'score':round(score,6),'sort':sort,'signals':['近7天','千赞以上','标题匹配舞蹈'],'visual_status':'unverified','method':'点赞、新鲜度及时间归一化点赞的排名融合；非实测涨速'}
    ordered=sorted(candidates,key=(lambda r:(r['recommendation']['score'],metrics['likes'](r))) if sort=='balanced' else metrics[sort],reverse=True)
    # Hard per-author cap; unknown authors remain separate, never grouped under an empty ID.
    counts={};result=[]
    for row in ordered:
        author=row.get('author_id') or row.get('author') or 'unknown:'+str(row['id'])
        if counts.get(author,0)>=2:continue
        counts[author]=counts.get(author,0)+1;result.append(row)
        if len(result)>=limit:break
    return result

class Recommendations:
    def __init__(self,db,platform):self.db=db;self.platform=platform;self.tasks={};self.errors={}
    def key(self,account):return 'dance_recommendations_v3:'+account
    def state(self,account='default',sort='balanced'):
        if sort not in SORTS:raise ValueError('不支持的推荐排序')
        if not self.db.query('SELECT id FROM accounts WHERE id=?',(account,)):raise ValueError('账号不存在')
        cached=self.db.get(self.key(account),self.db.get('dance_recommendations_v2:'+account,{}));updated=cached.get('updated')
        age=time.time()-datetime.fromisoformat(updated).timestamp() if updated else None
        running=account in self.tasks and not self.tasks[account].done();error=self.errors.get(account)
        items=rank_candidates(cached.get('pool',cached.get('items',[])),sort=sort) if age is not None and age<1800 else []
        return {'account':account,'keyword':KEYWORD,'keywords':list(KEYWORDS),'sort':sort,'days':DAYS,'min_likes':MIN_LIKES,'items':items,'updated':updated,'stale':age is None or age>TTL,'status':'refreshing' if running else error['status'] if error else 'ready' if updated else 'idle','message':error['message'] if error else cached.get('message',''),'queries_done':cached.get('queries_done',0),'queries_total':len(KEYWORDS),'candidate_count':len(cached.get('pool',cached.get('items',[]))),'matching':'标题与账号信息初筛；未视觉确认女性、单人和不挡脸。综合排序使用公开点赞与发布时间，每位作者最多2条；缺失播放和粉丝数不编造爆款分。'}
    def start(self,account='default',refresh=False,sort='balanced'):
        state=self.state(account,sort)
        if state['status']!='refreshing' and (refresh or state['stale'] or state['status'] in {'error','waiting_login'}):
            self.errors.pop(account,None);self.tasks[account]=asyncio.create_task(self.refresh(account))
        return self.state(account,sort)
    async def refresh(self,account):
        pool={};done=0
        try:
            for keyword in KEYWORDS:
                try:
                    async with asyncio.timeout(25):rows=await self.platform.recommendation_search(keyword,account)
                except LoginRequired:raise
                except Exception as error:
                    if not pool:raise
                    self.errors[account]={'status':'error','message':'部分搜索未完成，已保留本批候选：'+redact(error)}
                    break
                for row in rows:
                    if row.get('id'):pool[str(row['id'])]=row
                done+=1
                if hasattr(self.platform,'search_cache'):self.platform.search_cache.remember(account,rows)
                self.db.set(self.key(account),{'pool':list(pool.values()),'updated':now(),'queries_done':done,'message':'' if rank_candidates(pool.values()) else '本次未取得近7天、千赞以上的匹配视频；未用旧视频填充。'})
        except asyncio.CancelledError:raise
        except Exception as error:
            self.errors[account]={'status':'waiting_login' if isinstance(error,LoginRequired) else 'error','message':'单路搜索超过25秒，请稍后重试；已有结果保留。' if isinstance(error,TimeoutError) else redact(error)}
    async def close(self):
        tasks=list(self.tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
