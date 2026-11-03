"""Inspect owned desktop webviews over temporary development ports; never run an app."""
import asyncio,json
from pathlib import Path
from datetime import datetime,timezone
from playwright.async_api import async_playwright

ROOT=Path('D:/素材采集/studio')
SHOTS=Path('C:/Users/lenovo/comfyui/pipeline/runs/2026-10-22_RunningHub内嵌')
REPORT=ROOT/'embedded-runninghub-report.json'
rows=[]
def record(name,**data):
    rows.append({'time':datetime.now(timezone.utc).isoformat(),'name':name,**data});REPORT.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(rows[-1],ensure_ascii=False),flush=True)

async def main():
    SHOTS.mkdir(parents=True,exist_ok=True)
    async with async_playwright() as pw:
        root=await pw.chromium.connect_over_cdp('http://127.0.0.1:9224')
        main=next(p for c in root.contexts for p in c.pages if '127.0.0.1:7861' in p.url)
        errors=[];main.on('pageerror',lambda e:errors.append(str(e)))
        await main.goto('http://127.0.0.1:7861/#aiapps')
        if await main.get_by_role('button',name='应用列表',exact=True).count():await main.get_by_role('button',name='应用列表',exact=True).click()
        await main.wait_for_selector('.rh-app-card')
        count=await main.locator('.rh-app-card:not(.unavailable)').count();assert count>=21
        await main.screenshot(path=str(SHOTS/'收藏应用列表.png'))
        ipc=await main.evaluate("async()=>{try{await window.__TAURI_INTERNALS__.invoke('open_path',{path:'D:/素材采集/studio/does-not-exist.txt'});return 'unexpected'}catch(e){return String(e)}}")
        assert ipc=='本地文件或目录不存在';record('favorite_list',count=count,main_ipc=ipc)
        await main.locator('.rh-app-card').filter(has_text='图像放大').click();await main.wait_for_selector('.rh-host')
        embedded=None
        for _ in range(40):
            try:embedded=await pw.chromium.connect_over_cdp('http://127.0.0.1:9225',timeout=1500);break
            except Exception:await asyncio.sleep(1)
        assert embedded,'Embedded browser debug endpoint unavailable'
        pages=[p for c in embedded.contexts for p in c.pages]
        page=next((p for p in pages if 'runninghub.ai' in p.url),pages[0])
        await page.wait_for_url('https://www.runninghub.ai/**',timeout=45000)
        await page.wait_for_selector('.item-plane',timeout=45000)
        state=await page.evaluate("() => ({url:location.href,title:document.title,loggedIn:!!localStorage.getItem('Rh-Accesstoken'),files:document.querySelectorAll('input[type=file]').length,fields:document.querySelectorAll('.item-plane').length,runVisible:[...document.querySelectorAll('.run-cell-text')].some(e=>e.textContent.includes('立即运行'))})")
        assert state['loggedIn'] and state['files']==1 and state['fields']==3 and state['runVisible']
        await page.screenshot(path=str(SHOTS/'图像放大原网页.png'));record('embedded_original_app',**state)
        denied=await page.evaluate("async()=>{try{await window.__TAURI_INTERNALS__.invoke('open_path',{path:'D:/素材采集/studio/does-not-exist.txt'});return 'allowed'}catch(e){return String(e)}}")
        assert denied!='allowed' and denied!='本地文件或目录不存在';record('remote_file_ipc_denied',message=denied[:300])
        assert await main.locator('iframe').count()==0
        views=await main.evaluate("async()=>window.__TAURI_INTERNALS__.invoke('plugin:webview|get_all_webviews')")
        record('native_child_views',views=views,iframe_count=0)
        await main.get_by_role('button',name='应用列表',exact=True).click();await main.get_by_role('button',name='打开我的收藏',exact=True).click()
        await page.wait_for_url('**/user-center',timeout=15000)
        await page.wait_for_function("() => [...document.querySelectorAll('.radio-item')].some(e=>e.classList.contains('active')&&e.textContent.trim()==='我的收藏')",timeout=20000)
        await page.wait_for_function("() => document.querySelectorAll('.collectBtn').length>0",timeout=20000)
        await page.screenshot(path=str(SHOTS/'内置收藏原网页.png'))
        record('embedded_favorites',visible_cards=await page.locator('.collectBtn').count())
        await main.get_by_role('button',name='资料库',exact=True).click();await main.wait_for_selector('.media')
        await main.get_by_role('button',name='AI 应用',exact=True).click();await main.wait_for_selector('.rh-app-card')
        assert not await main.evaluate('document.documentElement.scrollWidth>innerWidth')
        record('navigation_and_main_errors',page_errors=errors);assert not errors

if __name__=='__main__':asyncio.run(main())
