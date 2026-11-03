import asyncio,json,time,sys,subprocess
from pathlib import Path
import httpx,psutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import Store,Settings,ROOT
from backend.ai import Models
from backend.engine import Engine
async def main():
    db=Store(ROOT/'model-test.sqlite3');cfg=Settings();cfg.vision_model.base_url='http://127.0.0.1:11435';cfg.vision_model.model='qwen3-vl:4b-instruct-q4_K_M';db.set('settings',cfg.model_dump());engine=Engine(db)
    live=Store();asset=live.query('SELECT * FROM assets WHERE kind=? LIMIT 1',('video',))[0];paths=await engine.frames(asset)
    t=time.monotonic();task=asyncio.create_task(engine.models.reverse(paths[2]));peak_ram=0;peak_vram=0
    while not task.done():
        family={}
        for p in psutil.process_iter(['name','memory_info']):
            try:
                if p.info['name'] and 'ollama' in p.info['name'].lower():
                    family[p.pid]=p
                    for child in p.children(recursive=True):family[child.pid]=child
            except psutil.Error:pass
        total_rss=0
        for process in family.values():
            try:total_rss+=process.memory_info().rss
            except psutil.Error:pass
        peak_ram=max(peak_ram,total_rss)
        try:
            report=await asyncio.to_thread(subprocess.check_output,['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits'],creationflags=0x08000000);peak_vram=max(peak_vram,int(report.decode().strip().splitlines()[0]))
        except Exception:pass
        await asyncio.sleep(1)
    try:
        result=await task;loaded=httpx.get('http://127.0.0.1:11435/api/ps',trust_env=False).json();record={'passed':True,'seconds':round(time.monotonic()-t,2),'peak_ollama_process_mb':round(peak_ram/1048576),'peak_gpu_used_mb':peak_vram,'loaded':loaded,'result':result.model_dump()}
        record['memory_measurement']='Ollama process family aggregate RSS; NVIDIA GPU memory sampling includes other applications'
        print(json.dumps({k:v for k,v in record.items() if k not in {'result','loaded'}},ensure_ascii=False),flush=True);(ROOT/'model-report.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        import shutil
        if paths:shutil.rmtree(Path(paths[0]).parent)
if __name__=='__main__':asyncio.run(main())
