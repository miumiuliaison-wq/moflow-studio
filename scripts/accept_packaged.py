"""Read/download acceptance against the packaged service; never sends account actions."""
import asyncio,json,time,sys
from pathlib import Path
import httpx,psutil

ROOT=Path('D:/素材采集/studio')
REPORT=ROOT/'packaged-report.json'
records=[]
def record(name,state='passed',**details):
    item={'name':name,'state':state,**details};records.append(item)
    REPORT.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(item,ensure_ascii=False),flush=True)

async def main():
    async with httpx.AsyncClient(base_url='http://127.0.0.1:7861',trust_env=False,timeout=120) as client:
        await client.get('/')
        state=(await client.get('/api/v1/state')).json()
        health=(await client.get('/api/v1/health')).json();assert health['original_engine'] is False
        record('packaged_health',**health)
        asset=next(x for x in state['assets'] if x['kind']=='video' and x['source_id'].isdigit())
        response=await client.get('/api/v1/assets/'+asset['id']+'/file',headers={'Range':'bytes=0-2047'})
        assert response.status_code==206 and len(response.content)==2048
        record('local_playback_range',status=response.status_code,bytes=len(response.content),source_id=asset['source_id'])
        search=await client.post('/api/v1/search',json={'keyword':'舞蹈','count':3})
        if search.status_code==200:
            items=search.json()['items'];record('real_search','passed' if items else 'blocked',count=len(items),source_links=[x['source'] for x in items])
            fresh=next((x for x in items if x['id'] not in {a['source_id'] for a in state['assets']}),None)
            if fresh:
                jobs=await client.post('/api/v1/downloads',json={'inputs':[fresh['id']]});jobs.raise_for_status();identifier=jobs.json()['job_ids'][0]
                for _ in range(75):
                    await asyncio.sleep(2);current=(await client.get('/api/v1/state')).json();job=next(x for x in current['jobs'] if x['id']==identifier)
                    if job['state'] in {'completed','failed','waiting_login','waiting_model','waiting_storage'}:break
                data={'source_id':fresh['id'],'job_state':job['state'],'message':job['message']}
                if job['state']=='completed':
                    data['files']=[{'path':a['path'],'bytes':a['bytes'],'media':{k:a['payload'].get('probe',{}).get(k) for k in ['width','height','duration']}} for a in current['assets'] if a['id'] in job['payload'].get('asset_ids',[])]
                record('packaged_real_download','passed' if job['state']=='completed' else 'blocked',**data)
        else:record('real_search','blocked',status=search.status_code,reason=search.json().get('detail'))
        for name,path in [('liked_list','/api/v1/browse/liked?count=3'),('collected_list','/api/v1/browse/collected?count=3'),('collection_folders','/api/v1/platform/folders?count=3'),('notifications','/api/v1/social/notices'),('conversations','/api/v1/social/friends')]:
            try:
                response=await client.get(path);data=response.json()
                if response.is_success:record(name,count=len(data.get('items',data.get('collects_list',[])) or []))
                else:record(name,'blocked',status=response.status_code,reason=data.get('detail'))
            except Exception as error:record(name,'blocked',reason=type(error).__name__)
        try:
            async with client.stream('GET','/api/v1/stream/'+asset['source_id'],headers={'Range':'bytes=0-2047'}) as response:
                response.raise_for_status();total=0
                async for chunk in response.aiter_bytes():
                    total+=len(chunk)
                    if total>=2048:break
                assert response.status_code==206 and total==2048
                record('online_playback_range',status=response.status_code,bytes=total)
        except Exception as error:record('online_playback_range','blocked',reason=type(error).__name__+': '+str(error)[:200])
        for folder in ['D:/素材采集/视频/待筛选','C:/Users/lenovo/comfyui/图片']:
            response=await client.post('/api/v1/assets/import',json={'path':folder});response.raise_for_status();record('legacy_import',folder=folder,**response.json())
        record('packaged_processes',processes=[{'name':p.name(),'path':p.exe(),'rss_mb':round(p.memory_info().rss/1048576)} for p in psutil.process_iter() if p.name() in {'moflow.exe','moflow-service.exe'}])

if __name__=='__main__':asyncio.run(main())
