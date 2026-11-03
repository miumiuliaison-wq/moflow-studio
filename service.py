import argparse,os,sys
from pathlib import Path
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=7861);parser.add_argument('--data');args=parser.parse_args()
    if args.data:os.environ['MOFLOW_DATA']=args.data
    os.environ['LANGSMITH_TRACING']='false';os.environ['LANGCHAIN_TRACING_V2']='false'
    import uvicorn
    from backend.core import ROOT
    ROOT.mkdir(parents=True,exist_ok=True)
    if getattr(sys,'frozen',False):
        log=(ROOT/'service.log').open('a',encoding='utf-8',buffering=1)
        sys.stdout=log;sys.stderr=log
    if os.name=='nt':
        import ctypes
        handle=ctypes.windll.kernel32.CreateMutexW(None,False,'Local\\MoflowService'+str(args.port))
        if ctypes.windll.kernel32.GetLastError()==183:sys.exit('墨流服务已经运行')
    from backend.app import app
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,log_level='warning',access_log=False,timeout_graceful_shutdown=5))
    app.state.stop=lambda:setattr(server,'should_exit',True)
    server.run()
if __name__=='__main__':main()
