"""Cold-start the actual production adapter; never prints credentials or media URLs."""
import asyncio,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import ROOT,Store,now
from backend.platform import Douyin

async def main():
    report={'time':now(),'driver':'patchright 1.64.1','runs':[]}
    for keyword in ['舞蹈','穿搭']:
        browser=Douyin(Store());started=time.monotonic()
        try:
            rows=await browser.search(keyword,count=3)
            row={'keyword':keyword,'elapsed':round(time.monotonic()-started,2),'count':len(rows),'state':(await browser.status())['state'],'sources':[r['source'] for r in rows]}
        except Exception as error:row={'keyword':keyword,'error':type(error).__name__,'message':str(error)}
        finally:await browser.close()
        report['runs'].append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
        (ROOT/'browser-adapter-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        await asyncio.sleep(5)

if __name__=='__main__':asyncio.run(main())
