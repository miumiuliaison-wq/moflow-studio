"""Independent live adapter check; no messages or account actions are sent."""
import asyncio,json,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import Store,ROOT
from backend.engine import Engine

async def main():
    engine=Engine(Store());report=[]
    try:
        for name,fn in [('search',lambda:engine.platform.search('舞蹈','default',count=3)),('friends',lambda:engine.platform.read_social('friends')),('notices',lambda:engine.platform.read_social('notices'))]:
            if len(sys.argv)>1 and name not in sys.argv[1:]:continue
            start=time.monotonic()
            try:
                result=await asyncio.wait_for(fn(),90);report.append({'name':name,'state':'passed','count':len(result),'seconds':round(time.monotonic()-start,2)})
                if name=='search':(ROOT/'search-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            except Exception as error:
                page=engine.platform.pages.get('default');observed={'title':await page.title(),'url':page.url,'text':(await page.locator('body').inner_text())[:2400]} if page else {}
                report.append({'name':name,'state':'blocked','error':str(error),'observed':observed,'seconds':round(time.monotonic()-start,2)})
            print(json.dumps({k:v for k,v in report[-1].items() if k!='observed'},ensure_ascii=False),flush=True)
    finally:
        await engine.platform.close();(ROOT/'live-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':asyncio.run(main())
