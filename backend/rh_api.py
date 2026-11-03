from fastapi import APIRouter,Request,HTTPException
import os

def router(engine):
    api=APIRouter(prefix='/api/v1/runninghub');rh=engine.runninghub
    def native(request):
        control=os.environ.get('MOFLOW_CONTROL_TOKEN','')
        if not control or request.headers.get('x-moflow-control')!=control:raise HTTPException(403,'此接口仅供墨流桌面宿主使用')
    @api.get('/apps')
    async def apps():return {'items':rh.apps(),'last_sync':rh.db.get('rh_last_sync'),'sync':rh.sync_status()}
    @api.post('/apps/sync')
    async def sync():return rh.start_sync()
    @api.get('/status')
    async def status():return await rh.browser.status()
    @api.post('/login')
    async def login():return await rh.browser.login()
    @api.get('/session')
    async def session(request:Request,force:bool=False):
        native(request);return await rh.export_session(force)
    @api.post('/favorites-report')
    async def favorites_report(request:Request,body:dict):
        native(request);return {'count':rh.replace_favorites(body.get('records'),bool(body.get('complete')))}
    @api.get('/input-policy')
    async def input_policy(request:Request):
        native(request);s=rh.db.settings();return {'image':s.reference_image_dir,'video':s.reference_video_dir}
    @api.get('/download-policy')
    async def download_policy(request:Request):
        native(request);return {'directory':str(rh.download_directory())}
    @api.post('/download-complete')
    async def downloaded(request:Request,body:dict):
        native(request);return await rh.register_download(body['path'],body.get('source',''))
    return api
