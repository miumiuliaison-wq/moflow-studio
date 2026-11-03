"""Verify the packaged HTTP service after the browser-driver fix, without social writes."""
import asyncio,json,time
from pathlib import Path
from datetime import datetime,timezone
import httpx

REPORT=Path('D:/素材采集/studio/data/packaged-browser-fix-report.json')
records=[]
def record(name,**values):
    row={'time':datetime.now(timezone.utc).isoformat(),'name':name,**values};records.append(row)
    REPORT.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(row,ensure_ascii=False),flush=True)

async def main():
    async with httpx.AsyncClient(base_url='http://127.0.0.1:7861',trust_env=False,timeout=120) as client:
        await client.get('/')
        health=(await client.get('/api/v1/health')).json();assert health['original_engine'] is False
        record('packaged_health',**health)
        state=(await client.get('/api/v1/state')).json();known={a['source_id'] for a in state['assets']};candidates=[]
        for keyword in ['舞蹈','穿搭']:
            started=time.monotonic();response=await client.post('/api/v1/search',json={'keyword':keyword,'count':3})
            if not response.is_success:
                record('search',keyword=keyword,state='failed',status=response.status_code,detail=response.json().get('detail'));raise AssertionError('Packaged search failed')
            items=response.json()['items'];assert len(items)==3
            assert all(x['source']=='https://www.douyin.com/video/'+x['id'] for x in items)
            candidates.extend(x for x in items if x['id'] not in known and x.get('urls'))
            record('search',keyword=keyword,state='passed',elapsed=round(time.monotonic()-started,2),sources=[x['source'] for x in items])
        account=(await client.get('/api/v1/accounts/default/status')).json();assert account['state']=='connected';record('account',**account)
        assert candidates,'Need a fresh video from the actual search'
        source=candidates[0];response=await client.post('/api/v1/downloads',json={'inputs':[source['id']]});response.raise_for_status();job_id=response.json()['job_ids'][0]
        for _ in range(90):
            await asyncio.sleep(2);state=(await client.get('/api/v1/state')).json();job=next(j for j in state['jobs'] if j['id']==job_id)
            if job['state'] in {'completed','failed','waiting_login','waiting_storage','cancelled'}:break
        if job['state']!='completed':
            record('download',state=job['state'],source=source['source'],message=job['message']);raise AssertionError('Download did not finish')
        files=[{'path':a['path'],'source_id':a['source_id'],'bytes':a['bytes'],'sha256':a['sha'],'probe':{'duration':a['payload'].get('probe',{}).get('format',{}).get('duration'),'video':[{k:s.get(k) for k in ['codec_name','width','height']} for s in a['payload'].get('probe',{}).get('streams',[]) if s.get('codec_type')=='video']}} for a in state['assets'] if a['id'] in job['payload']['asset_ids']]
        assert files and all(Path(f['path']).is_file() and f['bytes']>0 for f in files)
        record('download',state='passed',source=source['source'],files=files)

if __name__=='__main__':asyncio.run(main())
