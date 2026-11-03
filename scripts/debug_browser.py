import asyncio,sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['MOFLOW_BROWSER_DEBUG_PORT']='9226'
from backend.platform import Douyin
from backend.core import Store
async def main():
    browser=Douyin(Store());page=await browser.page();await page.goto('https://www.douyin.com/',wait_until='domcontentloaded');print('Browser ready on 9226',flush=True)
    try:await asyncio.sleep(600)
    finally:await browser.close()
if __name__=='__main__':asyncio.run(main())
