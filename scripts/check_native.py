"""Development CDP acceptance for the owned Tauri window, not the user's other apps."""
import asyncio,json
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path('D:/素材采集/studio');shots=Path('C:/Users/lenovo/comfyui/pipeline/runs/2026-10-22_墨流验收')
records=[]
def record(name,**details):
    row={'name':name,'state':'passed',**details};records.append(row)
    (ROOT/'native-report.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(row,ensure_ascii=False),flush=True)

async def main():
    async with async_playwright() as pw:
        browser=await pw.chromium.connect_over_cdp('http://127.0.0.1:9224')
        page=next(p for c in browser.contexts for p in c.pages if '127.0.0.1:7861' in p.url)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        # Clear any dialog left by an interrupted development check.
        await page.keyboard.press('Escape')
        data=await page.evaluate('async () => (await fetch("/api/v1/state")).json()')
        result=await page.evaluate('''async () => {let ipc;try {await window.__TAURI_INTERNALS__.invoke('open_path',{path:'D:/素材采集/studio/does-not-exist.txt'});ipc='unexpected success'}catch(e){ipc=String(e)}return {width:innerWidth,ipc}}''')
        assert result['ipc']=='本地文件或目录不存在' and len(data['assets'])>=1083
        record('native_ipc_and_data',**result,asset_count=len(data['assets']),import_count=sum(a['imported'] for a in data['assets']))
        for route in ['overview','discover','library','downloads','inbox','workflows','analytics','settings']:
            await page.goto('http://127.0.0.1:7861/#'+route);await page.wait_for_selector('h1');await page.wait_for_timeout(350)
            info=await page.evaluate('() => ({heading:document.querySelector("h1")?.textContent,overflow:document.documentElement.scrollWidth>innerWidth})')
            assert not info['overflow'];record('route_'+route,**info)
        await page.goto('http://127.0.0.1:7861/#overview')
        await page.get_by_role('button',name='AI 助手',exact=True).click()
        box=await page.locator('dialog[open]').bounding_box();edge=await page.evaluate('document.documentElement.clientWidth')
        assert abs(box['x']+box['width']-edge)<3
        await page.keyboard.press('Escape');assert await page.locator('dialog[open]').count()==0;record('assistant_drawer_and_escape')
        await page.get_by_role('button',name='新建任务',exact=True).focus();await page.keyboard.press('Enter')
        assert await page.locator('dialog[open]').count()==1;await page.keyboard.press('Escape');record('keyboard_open_close')
        await page.set_viewport_size({'width':390,'height':844})
        for route in ['overview','library','workflows','settings']:
            await page.goto('http://127.0.0.1:7861/#'+route);await page.wait_for_selector('h1')
            assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
        record('narrow_390px')
        await page.set_viewport_size({'width':1150,'height':784})
        await page.goto('http://127.0.0.1:7861/#overview');await page.emulate_media(reduced_motion='reduce')
        await page.wait_for_timeout(200);first=await page.locator('.sea').inner_text();await page.wait_for_timeout(350)
        assert first==await page.locator('.sea').inner_text();record('reduced_motion_static_ascii')
        await page.evaluate('document.body.style.zoom="1.25"')
        assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
        await page.evaluate('document.body.style.zoom=""');record('zoom_125_percent')
        await page.emulate_media(reduced_motion='no-preference');await page.screenshot(path=str(shots/'墨流总览.png'))
        await page.goto('http://127.0.0.1:7861/#library');await page.wait_for_selector('.media')
        assert await page.locator('.media').count()<=60;record('library_60_items',rendered=await page.locator('.media').count())
        await page.screenshot(path=str(shots/'墨流资料库.png'))
        video=next(a for a in data['assets'] if a['kind']=='video' and a['source_id'].isdigit())
        await page.get_by_role('textbox',name='筛选素材').fill(video['source_id'])
        await page.locator('.media').filter(has=page.get_by_text('Video',exact=True)).first.locator('.media-cover').click()
        await page.wait_for_function('document.querySelector("dialog[open] video")?.readyState>=1')
        duration=await page.locator('dialog[open] video').evaluate('(v)=>v.duration');assert duration>0
        record('native_video_decode',duration=duration);await page.keyboard.press('Escape')
        await page.goto('http://127.0.0.1:7861/#overview');record('page_errors',errors=errors);assert not errors
        await browser.close()

if __name__=='__main__':asyncio.run(main())
