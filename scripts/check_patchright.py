"""Read-only compatibility probe against the program's existing Chrome profile."""
import asyncio,json,sys,time
from pathlib import Path
from urllib.parse import quote,urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from patchright.async_api import async_playwright
from backend.core import ROOT,now
from backend.platform import DOM,normalize

async def main():
    report={'time':now(),'driver':'patchright 1.64.1','runs':[]}
    for keyword in ['舞蹈','穿搭']:
        started=time.monotonic();responses=[]
        async with async_playwright() as pw:
            ctx=await pw.chromium.launch_persistent_context(str(ROOT/'profiles'/'default'),channel='chrome',headless=False,no_viewport=True)
            try:
                page=await ctx.new_page()
                async def observe(response):
                    if '/search/' not in response.url or 'json' not in response.headers.get('content-type',''):return
                    try:
                        data=await response.json()
                        responses.append({'endpoint':urlsplit(response.url).path,'status':response.status,'code':data.get('status_code'),'nil':data.get('search_nil_info'),'items':normalize(data)})
                    except Exception:pass
                page.on('response',observe)
                await page.goto('https://www.douyin.com/search/'+quote(keyword)+'?type=video',wait_until='domcontentloaded',timeout=45000)
                await page.wait_for_timeout(6000)
                rows={}
                for _ in range(3):
                    dom=await page.evaluate(DOM,isolated_context=False)
                    for row in dom['records']+[r for data in dom['data'] for r in normalize(data)]+[r for res in responses for r in res['items']]:rows[row['id']]=row
                    if rows:break
                    await page.mouse.wheel(0,1000);await page.wait_for_timeout(2000)
                logged=any(c['name'] in {'sessionid','sid_guard','sessionid_ss'} and c['value'] for c in await ctx.cookies('https://www.douyin.com'))
                row={'keyword':keyword,'elapsed':round(time.monotonic()-started,2),'logged_in':logged,'webdriver':await page.evaluate('navigator.webdriver'),'title':await page.title(),'count':len(rows),'sources':[r['source'] for r in list(rows.values())[:5]],'responses':[{k:v for k,v in r.items() if k!='items'}|{'count':len(r['items'])} for r in responses]}
                report['runs'].append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
            finally:await ctx.close()
        (ROOT/'patchright-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        await asyncio.sleep(5)

if __name__=='__main__':asyncio.run(main())
