"""Non-billable acceptance against the packaged, owned Tauri WebView."""
import asyncio,json,time
from pathlib import Path
from playwright.async_api import async_playwright

OUT=Path('C:/Users/lenovo/comfyui/pipeline/runs/2026-10-23_墨流自动生产')
OUT.mkdir(parents=True,exist_ok=True)
records=[]
def record(name,**details):
    records.append({'name':name,**details})
    (OUT/'desktop-check.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(records[-1],ensure_ascii=False),flush=True)

async def main():
    async with async_playwright() as pw:
        browser=await pw.chromium.connect_over_cdp('http://127.0.0.1:9224')
        page=next(p for c in browser.contexts for p in c.pages if '127.0.0.1:7861' in p.url)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        async def read(path):
            return await page.evaluate('async p=>{const r=await fetch("/api/v1"+p);if(!r.ok)throw Error(r.status);return r.json()}',path)
        await page.goto('http://127.0.0.1:7861/#workflows')
        await page.wait_for_selector('.production-section')
        state=await read('/state');config=await read('/production/config')
        assert len(state['assets'])>=1083 and len(config['apps'])==21
        assert config['credentials']['deepseek'] and 'sk-' not in json.dumps(config)
        assert state['settings']['text_model']['model']=='deepseek-flash'
        record('data_and_configuration',assets=len(state['assets']),favorites=len(config['apps']),credentials=config['credentials'])
        before=await read('/production/batches')
        if not any(b['payload']['request']['ds_budget']==0 and b['payload']['request']['rh_budget']==0 for b in before['items']):
            await page.get_by_role('button',name='创建生产任务',exact=True).click()
        await page.wait_for_function('async()=>{const d=await(await fetch("/api/v1/production/batches")).json();return d.items.some(b=>b.state==="waiting_budget"&&b.payload.request.ds_budget===0&&b.payload.request.rh_budget===0)}')
        batches=(await read('/production/batches'))['items']
        waiting=next(b for b in batches if b['state']=='waiting_budget' and b['payload']['request']['ds_budget']==0 and b['payload']['request']['rh_budget']==0)
        assert all(b['held']==b['spent']==0 for b in waiting['budget']) and not waiting['cloud_tasks']
        await page.locator('.production-run').first.locator('summary').first.click()
        await page.screenshot(path=str(OUT/'自动生产.png'),full_page=True)
        record('real_ui_create_zero_budget',batch=waiting['id'],state=waiting['state'],cloud_tasks=0,budget=waiting['budget'])
        await page.goto('http://127.0.0.1:7861/#settings')
        await page.wait_for_selector('.production-settings')
        assert await page.locator('.production-settings input[type=password]').count()==2
        assert await page.locator('.production-settings input[type=password]').first.input_value()==''
        assert '已加密保存' in await page.locator('.production-settings input[type=password]').first.get_attribute('placeholder')
        await page.screenshot(path=str(OUT/'生产设置.png'),full_page=True)
        record('settings_and_secret_placeholder')
        check=await page.evaluate('async()=>{const r=await fetch("/api/v1/production/check/deepseek",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});return {status:r.status,data:await r.json()}}')
        assert check['status']==200 and check['data']['selected_available'] and check['data']['vision_available']
        record('packaged_official_models_check',**check['data'])
        for width in (760,390):
            await page.set_viewport_size({'width':width,'height':844})
            for route,selector in [('workflows','.production-section'),('settings','.production-settings')]:
                await page.goto('http://127.0.0.1:7861/#'+route);await page.wait_for_selector(selector)
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth'),(width,route)
            record('responsive',width=width)
        await page.set_viewport_size({'width':1150,'height':850})
        await page.goto('http://127.0.0.1:7861/#workflows')
        await page.wait_for_selector('.production-section')
        await page.evaluate('document.body.style.zoom="1.25"')
        assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
        await page.evaluate('document.body.style.zoom=""')
        button=page.get_by_role('button',name='创建生产任务',exact=True)
        await button.focus();assert await button.evaluate('(b)=>b===document.activeElement')
        record('zoom_and_keyboard_focus')
        started=time.monotonic();recommendation=await read('/recommendations');elapsed=time.monotonic()-started
        record('existing_recommendation_cache',seconds=round(elapsed,3),items=len(recommendation.get('items',[])))
        await page.goto('http://127.0.0.1:7861/#aiapps')
        await page.wait_for_selector('.rh-app-card')
        record('runninghub_favorites_ui',cards=await page.locator('.rh-app-card').count())
        assert not errors,errors
        record('page_errors',errors=errors)
        await page.goto('http://127.0.0.1:7861/#workflows')
        await browser.close()

if __name__=='__main__':asyncio.run(main())
