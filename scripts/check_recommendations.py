"""Run with the desktop stopped; real read-only search, no downloads or actions."""
import asyncio,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import Store,ROOT
from backend.platform import Douyin
from backend.recommendations import Recommendations

async def main():
    db=Store();platform=Douyin(db);service=Recommendations(db,platform);report={}
    original=platform.recommendation_search
    async def observe(*args):
        rows=await original(*args)
        report['candidates']=[{k:r.get(k) for k in ['id','source','title','published','stats']} for r in rows]
        return rows
    platform.recommendation_search=observe
    try:
        started=time.monotonic();initial=service.start(refresh=True);report['initial_seconds']=round(time.monotonic()-started,4);report['initial_status']=initial['status']
        await service.tasks['default'];state=service.state();report.update(first_result_seconds=round(time.monotonic()-started,2),status=state['status'],message=state['message'],count=len(state['items']),items=[{k:r.get(k) for k in ['id','source','title','published','stats']} for r in state['items']])
        started=time.monotonic();cached=service.start();report.update(cached_seconds=round(time.monotonic()-started,4),cached_status=cached['status'])
        print(json.dumps({k:v for k,v in report.items() if k not in {'items','candidates'}},ensure_ascii=False),flush=True)
    finally:
        await service.close();await platform.close();(ROOT/'recommendation-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':asyncio.run(main())
