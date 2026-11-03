"""Owned native webview QA; no Run clicks, uploads or cloud task submission."""
import asyncio,json,time
from pathlib import Path
from playwright.async_api import async_playwright

REPORT=Path('D:/素材采集/studio/data/rh-performance-native.json')
rows=[]
def record(name,**data):
    row={'name':name,**data};rows.append(row);REPORT.write_text(json.dumps({'client_date':'2026-10-23','checks':rows},ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(row,ensure_ascii=False),flush=True)

async def main():
    async with async_playwright() as pw:
        root=await pw.chromium.connect_over_cdp('http://127.0.0.1:9224')
        pages=[p for c in root.contexts for p in c.pages]
        main=next((p for p in pages if '7861' in p.url),pages[0])
        await main.wait_for_url('http://127.0.0.1:7861/**',timeout=45000)
        errors=[];main.on('pageerror',lambda e:errors.append(str(e)))
        await main.goto('http://127.0.0.1:7861/#aiapps');await main.wait_for_selector('.rh-app-card')
        count=await main.locator('.rh-app-card:not(.unavailable)').count();assert count==21
        started=time.perf_counter();await main.locator('.rh-app-card').filter(has_text='图像放大').click()
        child=None
        for _ in range(60):
            try:child=await pw.chromium.connect_over_cdp('http://127.0.0.1:9225',timeout=600);break
            except Exception:await asyncio.sleep(.15)
        assert child
        page=next(p for c in child.contexts for p in c.pages)
        await page.wait_for_url('https://www.runninghub.ai/**',timeout=30000)
        await page.wait_for_selector('.item-plane',timeout=45000)
        info=await page.evaluate("() => ({loggedIn:!!localStorage.getItem('Rh-Accesstoken'),fields:document.querySelectorAll('.item-plane').length,url:location.href})")
        assert info['loggedIn'] and info['fields']>=1
        await main.wait_for_function("() => {const e=document.querySelector('.rh-page-state');return e&&!e.textContent.includes('正在连接')}")
        record('first_app',seconds=round(time.perf_counter()-started,3),favorites=count,**info)
        await page.evaluate("window.__rhPreserve='kept';window.__rhOrigin=performance.timeOrigin")
        old_origin=await page.evaluate('performance.timeOrigin')
        await main.get_by_role('button',name='应用列表',exact=True).click();await main.wait_for_selector('.rh-app-card')
        started=time.perf_counter();await main.locator('.rh-app-card').filter(has_text='图像放大').click()
        await main.wait_for_function("() => {const e=document.querySelector('.rh-page-state');return e&&!e.textContent.includes('正在连接')}")
        assert await page.evaluate("window.__rhPreserve==='kept'&&performance.timeOrigin===window.__rhOrigin")
        record('reopen_same_app',seconds=round(time.perf_counter()-started,3),document_preserved=True)
        native=await main.evaluate("""async () => {const r=document.querySelector('.rh-host').getBoundingClientRect(),t=performance.now();const result=await window.__TAURI_INTERNALS__.invoke('runninghub_open',{appId:'2099611210999488514',x:r.left,y:r.top,width:r.width,height:Math.min(r.height,innerHeight-r.top-8),pixelRatio:devicePixelRatio});return {milliseconds:performance.now()-t,result};}""")
        assert native['result']['reused'];record('native_reuse_command',**native)
        # Returning to the app after leaving the AI page must also reuse it.
        await main.get_by_role('button',name='资料库',exact=True).click();await main.wait_for_selector('.media')
        await main.get_by_role('button',name='AI 应用',exact=True).click();await main.wait_for_selector('.rh-app-card')
        await main.locator('.rh-app-card').filter(has_text='图像放大').click()
        await main.wait_for_function("() => {const e=document.querySelector('.rh-page-state');return e&&!e.textContent.includes('正在连接')}")
        assert await page.evaluate("window.__rhPreserve==='kept'&&performance.timeOrigin===window.__rhOrigin")
        record('return_from_library',document_preserved=True)
        await main.get_by_role('button',name='刷新应用网页',exact=True).click()
        await page.wait_for_function('(old) => performance.timeOrigin!==old',arg=old_origin)
        await page.wait_for_selector('.item-plane',timeout=45000)
        record('explicit_reload',document_changed=await page.evaluate('performance.timeOrigin')!=old_origin)
        denied=await page.evaluate("async()=>{try{await window.__TAURI_INTERNALS__.invoke('open_path',{path:'D:/素材采集/studio/nonexistent'});return 'allowed'}catch(e){return String(e)}}")
        assert 'not allowed' in denied;record('remote_permissions',local_file_denied=True)
        await main.get_by_role('button',name='应用列表',exact=True).click()
        await main.locator('.rh-app-card').filter(has_text='FireRed').click()
        await page.wait_for_url('**/*2071308534918246402',timeout=30000);await page.wait_for_selector('.item-plane',timeout=45000)
        assert await page.evaluate("typeof window.__rhPreserve==='undefined'")
        record('different_application',url=page.url)
        await main.get_by_role('button',name='应用列表',exact=True).click()
        await main.evaluate("""() => {window.__rhSync=[];const original=window.fetch;window.fetch=async function(input,options){const t=performance.now();const response=await original.apply(this,arguments);if(String(input).endsWith('/runninghub/apps/sync'))window.__rhSync.push({ms:performance.now()-t,state:await response.clone().json()});return response;}}""")
        await main.get_by_role('button',name='刷新收藏',exact=True).click()
        await main.wait_for_function('() => window.__rhSync.length>0')
        state=await main.evaluate("() => ({...window.__rhSync[0],count:document.querySelectorAll('.rh-app-card:not(:disabled)').length,background_note:!!document.querySelector('.rh-sync-note')})")
        assert state['state']['state']=='syncing' and state['count']==21 and state['background_note']
        record('background_sync_immediate',**state)
        assert not await main.evaluate('document.documentElement.scrollWidth>innerWidth')
        record('main_errors',errors=errors);assert not errors

if __name__=='__main__':asyncio.run(main())
