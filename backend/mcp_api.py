"""Original public tool contracts, independent service implementation."""
import json
from pathlib import Path
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool,TextContent
from jsonschema import validate

server=Server('moflow-studio',version='0.1.0')
contracts=json.loads((Path(__file__).parent/'contracts.json').read_text(encoding='utf-8-sig'))
for contract in contracts:
    properties=contract['inputSchema'].get('properties',{})
    if 'confirm' in properties:properties['request_id']={'type':'string','minLength':1,'maxLength':120,'description':'同一次操作的稳定ID；重试时必须复用，另一次操作使用新ID'}
contracts.extend([
 {'name':'moflow_create_production','description':'按已启用规则创建自动视频生产任务，不能修改规则或提高限额','inputSchema':{'type':'object','properties':{'request_id':{'type':'string'},'account':{'type':'string'},'count':{'type':'integer','minimum':1},'ds_budget':{'type':'number'},'rh_budget':{'type':'number'},'rh_unit':{'type':'string'},'notes':{'type':'string'}},'required':['request_id'],'additionalProperties':False}},
 {'name':'moflow_production_status','description':'查询自动生产进度和已有结果','inputSchema':{'type':'object','properties':{'id':{'type':'string'}},'additionalProperties':False}},
 {'name':'moflow_create_workflow','description':'创建独立AI工作流计划','inputSchema':{'type':'object','properties':{'plan':{'type':'object'}},'required':['plan']}},
 {'name':'moflow_analyze_asset','description':'对已有素材执行视觉分析','inputSchema':{'type':'object','properties':{'asset_id':{'type':'string'},'requirements':{'type':'string'}},'required':['asset_id']}},
 {'name':'moflow_reverse_image','description':'图片提示词逆向，保存Markdown','inputSchema':{'type':'object','properties':{'asset_id':{'type':'string'},'rewrite':{'type':'boolean'}},'required':['asset_id']}},
])

@server.list_tools()
async def tools():return [Tool(name=x['name'],description=x['description'],inputSchema=x['inputSchema']) for x in contracts]

@server.call_tool()
async def call(name,arguments):
    from .app import db,engine
    from .ai import Plan
    from .core import now,uid,Unsupported
    catalog={x['name']:x for x in contracts}
    try:
        if name not in catalog:raise ValueError('未知工具')
        validate(arguments,catalog[name]['inputSchema']);a=arguments;account=db.get('active_account','default');short=name.removeprefix('douyin_')
        if name=='moflow_create_production':
            from .production_config import ProductionRequest
            result=engine.production.create(ProductionRequest.model_validate(a),mcp=True);engine.production.tick()
        elif name=='moflow_production_status':result=engine.production.get(a['id']) if a.get('id') else engine.production.list()
        elif name=='moflow_create_workflow':result={'run_id':await engine.create_run(Plan.model_validate(a['plan']))}
        elif name in {'moflow_analyze_asset','moflow_reverse_image'}:result={'job_id':db.job('analyze' if 'analyze' in name else 'reverse',a)}
        elif short=='auth_status':result=await engine.platform.status(account)
        elif short=='account_list':result=db.list('accounts')
        elif short=='parse_video':result=await engine.platform.detail(a['input'],account)
        elif short=='search_videos':result=await engine.platform.search(a['keyword'],account,count=a.get('count',20))
        elif short=='search_users':result=await engine.platform.search(a.get('keyword',''),account,'user')
        elif short in {'get_user','get_current_user','get_comments','get_comment_replies','get_collection_folders','get_collected_mixes','get_collection_folder_videos','get_mix_videos','get_following'}:
            kind={'get_user':'profile','get_current_user':'current_user','get_comments':'comments','get_comment_replies':'replies','get_collection_folders':'folders','get_collected_mixes':'folders','get_collection_folder_videos':'folder_videos','get_mix_videos':'mix','get_following':'following'}[short]
            result=await engine.platform.read_f2(kind,account,sec_user_id=a.get('sec_uid',''),user_id=a.get('user_id',''),aweme_id=a.get('aweme_id'),item_id=a.get('aweme_id'),comment_id=a.get('comment_id'),collects_id=a.get('collects_id'),mix_id=a.get('series_id'),count=a.get('count',20),cursor=a.get('cursor',0),offset=a.get('offset',0))
        elif short in {'get_feed','get_liked','get_collected','get_user_videos'}:
            kind={'get_feed':'feed','get_liked':'liked','get_collected':'collected','get_following':'following','get_user_videos':'user','get_user':'user'}[short];result=await engine.platform.listing(kind,account,a.get('sec_uid',''),a.get('count',30))
        elif short in {'download_tasks','get_download_history','download_history','list_failed_downloads'}:
            data=db.list('jobs' if short in {'download_tasks','list_failed_downloads'} else 'assets',1000)
            if short=='list_failed_downloads':data=[x for x in data if x['state']=='failed']
            if short=='get_download_history':
                hidden=set(db.get('hidden_download_history',[]));data=[x for x in data if x['source_id'].isdigit() and x['id'] not in hidden]
            query=a.get('query','');result=[x for x in data if query in json.dumps(x,ensure_ascii=False)][a.get('offset',0):a.get('offset',0)+a.get('limit',100)]
        elif short=='get_download_settings':result=db.settings().model_dump()
        elif short in {'download_video','download_videos','download_user','download_liked','download_collected','download_liked_authors','download_mix','download_collection_folder'}:
            if a.get('confirm') is not True:raise ValueError('下载操作需要 confirm=true')
            if short=='download_video':ids=[a['input']]
            elif short=='download_videos':ids=a['aweme_ids']
            elif short in {'download_liked','download_collected','download_user'}:
                rows=await engine.platform.listing({'download_liked':'liked','download_collected':'collected','download_user':'user'}[short],account,a.get('sec_uid',''),a.get('count',30));ids=[x['id'] for x in rows]
            elif short in {'download_mix','download_collection_folder'}:
                from .platform import normalize
                body=await engine.platform.read_f2('mix' if short=='download_mix' else 'folder_videos',account,mix_id=a.get('series_id'),collects_id=a.get('collects_id'),count=a.get('count',20),cursor=a.get('cursor',0));ids=[m['id'] for m in normalize(body)]
            else:
                ids=[]
                for author in a.get('selected_sec_uids',[]):ids.extend(m['id'] for m in await engine.platform.listing('user',account,author,a.get('count',20)))
                if not a.get('selected_sec_uids'):raise ValueError('下载点赞作者作品时必须明确 selected_sec_uids')
            result={'job_ids':[db.job('download',{'input':i,'account':account}) for i in ids]}
        elif short in {'pause_download','resume_download','cancel_download','remove_download_task'}:
            if a.get('confirm') is not True:raise ValueError('修改任务需要 confirm=true')
            from .app import job_action
            result=await job_action(a['task_id'],{'pause_download':'pause','resume_download':'resume','cancel_download':'cancel','remove_download_task':'cancel'}[short])
        elif short=='update_download_settings':
            if a.get('confirm') is not True:raise ValueError('修改设置需要 confirm=true')
            from .core import Settings
            patch=a['patch'];mapped={('quality' if k=='download_quality' else 'concurrent_downloads' if k=='max_concurrent' else k):v for k,v in patch.items()}
            unsupported=set(mapped)-set(Settings.model_fields)
            if unsupported:raise Unsupported('这些偏好尚未适配，设置未更新：'+','.join(sorted(unsupported)))
            settings=Settings.model_validate({**db.settings().model_dump(),**mapped});db.set('settings',settings.model_dump());result=settings.model_dump()
        elif short in {'get_messages','get_friends','get_friend_status','get_notices','get_unread','get_friend_chat_state','get_im_connection_status'}:result=await engine.platform.read_social('notices' if short in {'get_notices','get_unread'} else 'messages',account,a.get('to_user_id',''))
        elif short in {'like_video','collect_video','follow_user','send_message','share_video','unfollow_user','send_image_message','send_video_message','publish_comment','reply_notice','like_comment'}:
            if a.get('confirm') is not True:raise ValueError('账号操作需要 confirm=true')
            action={'like_video':'like','collect_video':'collect','follow_user':'follow','send_message':'message','share_video':'share','unfollow_user':'unfollow','send_image_message':'send_image','send_video_message':'send_video','publish_comment':'comment','reply_notice':'comment','like_comment':'like_comment'}[short]
            # MCP cannot implicitly grant the account-action rules used by the AI.
            target=a.get('to_user_id') or a.get('aweme_id') or a.get('user_id') or a.get('uid') or a.get('sec_uid') or ''
            desired=a.get('liked',a.get('collected',a.get('follow',True)))
            if desired is False:action={'like':'unlike','collect':'uncollect','follow':'unfollow'}.get(action,action)
            text=a.get('text',a.get('content',a.get('input','')))
            asset_id=''
            if short=='send_image_message':
                import base64,io
                from starlette.datastructures import UploadFile
                from .app import upload
                encoded=a['image_data_url']
                if not encoded.startswith('data:image/') or ';base64,' not in encoded:raise ValueError('需要base64图片数据URL')
                raw=base64.b64decode(encoded.split(';base64,',1)[1],validate=True)
                if len(raw)>5*1024**2:raise ValueError('图片最多5MB')
                uploaded=await upload([UploadFile(file=io.BytesIO(raw),filename=a.get('file_name') or 'attachment.png')]);asset_id=uploaded['asset_ids'][0]
            elif short=='send_video_message':
                rows=db.query("SELECT id FROM assets WHERE path=? AND kind='video'",(str(Path(a['video_path']).resolve()),))
                if not rows:raise ValueError('视频必须已登记在资料库中')
                asset_id=rows[0]['id']
            import hashlib
            stable=a.get('request_id') or hashlib.sha256((now()[:10]+'|'+target+'|'+action+'|'+text+'|'+asset_id+'|'+a.get('comment_id','')).encode()).hexdigest()
            result=await engine.perform_action(account,action,target,text,db.settings().mcp_rule_id,False,stable,asset_id,a.get('comment_id',''))
        elif short=='check_download_files':result=[{'id':x['id'],'exists':Path(x['path']).exists()} for x in db.list('assets',100000)]
        elif short=='get_creator_monitor':result=[x for x in db.list('rules') if x['payload'].get('template')=='author']
        elif short in {'delete_download_history','clear_download_history','retry_failed_downloads','remove_failed_downloads','delete_download_files'}:
            if a.get('confirm') is not True:raise ValueError('此操作需要 confirm=true')
            if short in {'clear_download_history','delete_download_history'}:
                rows=db.query('SELECT id,source_id FROM assets') if short=='clear_download_history' else db.query('SELECT id,source_id FROM assets WHERE source_id=?',(a['aweme_id'],))
                hidden=set(db.get('hidden_download_history',[]));hidden.update(x['id'] for x in rows if x['source_id'].isdigit());db.set('hidden_download_history',list(hidden));result={'files_preserved':True,'library_preserved':True}
            elif short=='delete_download_files':
                result=[]
                for path in a['paths']:
                    rows=db.query('SELECT * FROM assets WHERE path=?',(str(Path(path).resolve()),))
                    if not rows:raise ValueError('只能删除资料库中登记的文件')
                    Path(rows[0]['path']).unlink(missing_ok=True);db.execute('DELETE FROM assets WHERE id=?',(rows[0]['id'],));result.append(rows[0]['id'])
            else:
                result=[]
                for identifier in a['failure_ids']:
                    if short=='retry_failed_downloads':db.update_job(identifier,'queued','等待重试')
                    else:db.execute("DELETE FROM jobs WHERE id=? AND state='failed'",(identifier,))
                    result.append(identifier)
        elif short in {'upsert_creator_monitor_target','remove_creator_monitor_target','update_creator_monitor','run_creator_monitor_now'}:
            if a.get('confirm') is not True:raise ValueError('修改监控需要 confirm=true')
            monitors=[r for r in db.list('rules') if r['payload'].get('template')=='author']
            if short=='upsert_creator_monitor_target':
                from .app import save_rule,Rule
                result=await save_rule('author:'+a['sec_uid'],Rule(name=a.get('nickname') or a['sec_uid'],template='author',account=account,enabled=a.get('enabled',False),author_id=a['sec_uid']))
            elif short=='remove_creator_monitor_target':db.execute('DELETE FROM rules WHERE id=?',('author:'+a['sec_uid'],));result={'ok':True}
            elif short=='run_creator_monitor_now':
                for rule in monitors:
                    if rule['enabled']:await engine.rule_tick(rule['id'])
                result={'checked':len(monitors)}
            else:
                for rule in monitors:
                    cfg=rule['payload'];cfg.update({k:v for k,v in a['patch'].items() if k in {'interval_seconds'}})
                    db.execute('UPDATE rules SET enabled=?,payload=? WHERE id=?',(int(a['patch'].get('enabled',rule['enabled'])),json.dumps(cfg),rule['id']))
                result={'updated':len(monitors)}
        elif short=='get_liked_authors':
            rows=await engine.platform.listing('liked',account,count=a.get('count',20));authors={x.get('author_id'):{'sec_uid':x.get('author_id'),'nickname':x.get('author')} for x in rows if x.get('author_id')};result=list(authors.values())
        elif short=='get_user_series':
            from .platform import walk
            body=await engine.platform.read_f2('posts',account,sec_user_id=a['sec_uid'],count=a.get('count',20),max_cursor=a.get('cursor',0));seen={}
            for node in walk(body):
                identifier=node.get('mix_id') or node.get('series_id')
                if identifier and any(k in node for k in ['mix_name','series_name','mix_info','desc']):seen[str(identifier)]={'id':str(identifier),'name':node.get('mix_name',node.get('series_name',node.get('desc',''))),'source':node}
            result=list(seen.values())
        elif short=='suggest_ai_interaction':
            from .ai import Draft
            result=(await engine.models.request('仅生成建议，不发送：'+json.dumps(a,ensure_ascii=False),Draft)).model_dump()
        else:raise Unsupported('独立实现尚未通过此工具的功能适配；未调用原版程序，也未返回模拟数据：'+name)
        return [TextContent(type='text',text=json.dumps({'success':True,'data':result},ensure_ascii=False,default=str))]
    except Exception as error:return [TextContent(type='text',text=json.dumps({'success':False,'message':str(error),'tool':name},ensure_ascii=False))]

manager=StreamableHTTPSessionManager(app=server,json_response=True,stateless=True,max_request_body_size=8*1024**2)
async def mcp_asgi(scope,receive,send):await manager.handle_request(scope,receive,send)
