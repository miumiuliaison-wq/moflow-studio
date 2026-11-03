"""Read a key from stdin, store with DPAPI, and perform a non-billable /models check.
Never pass credentials on argv or put them in a source file.
"""
import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import Store,SecretVault,ModelConfig
from backend.ai import Models

async def main():
    key=sys.stdin.readline().strip()
    if not key or len(key)>500:raise SystemExit('未收到有效密钥')
    db=Store();vault=SecretVault();vault.set('deepseek',key);key=''
    cfg=db.settings();cfg.text_model=ModelConfig(provider='deepseek',model='deepseek-flash');cfg.vision_model=ModelConfig(provider='deepseek',model='deepseek-flash');db.set('settings',cfg.model_dump())
    try:
        models=await Models(db).list('text')
        print(json.dumps({'saved_encrypted':True,'connected':True,'selected_available':'deepseek-flash' in models,'models':models},ensure_ascii=False))
    except Exception as error:
        # HTTP errors can contain URLs or headers; only output the exception type.
        print(json.dumps({'saved_encrypted':True,'connected':False,'error_type':type(error).__name__}))
if __name__=='__main__':asyncio.run(main())
