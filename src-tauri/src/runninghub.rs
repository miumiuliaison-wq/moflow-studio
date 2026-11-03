use std::{io::{Read,Write},net::TcpStream,path::PathBuf,time::Duration};
use tauri::{Emitter,Manager,webview::{Cookie,DownloadEvent,NewWindowResponse,PageLoadEvent,WebviewBuilder}};
use serde_json::{Value,json};

const SITE:&str="https://www.runninghub.ai";
pub(super) fn service(app:&tauri::AppHandle,path:&str,body:Option<Value>)->Result<Value,String>{
 let control=app.state::<crate::Service>().1.clone();
 let mut socket=TcpStream::connect_timeout(&"127.0.0.1:7861".parse().unwrap(),Duration::from_secs(3)).map_err(|_|"本地服务未连接")?;
 socket.set_read_timeout(Some(Duration::from_secs(60))).map_err(|e|e.to_string())?;
 let data=body.as_ref().map(|v|v.to_string()).unwrap_or_default();let method=if body.is_some(){"POST"}else{"GET"};
 let request=format!("{method} /api/v1/runninghub/{path} HTTP/1.0\r\nHost: 127.0.0.1\r\nX-Moflow-Control: {control}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{data}",data.len());
 socket.write_all(request.as_bytes()).map_err(|_|"本地服务请求失败")?;let mut raw=String::new();socket.take(4*1024*1024).read_to_string(&mut raw).map_err(|_|"本地服务未及时返回")?;
 let (headers,body)=raw.split_once("\r\n\r\n").ok_or("本地服务响应无效")?;
 if !headers.lines().next().unwrap_or("").contains(" 200 "){return Err("操作未完成，请检查 RunningHub 登录状态或素材空间".into())}
 serde_json::from_str(body).map_err(|_|"本地服务响应无法读取".into())
}
fn site_url(id:Option<&str>)->Result<url::Url,String>{
 let path=match id{Some(id) if (8..=25).contains(&id.len())&&id.bytes().all(|b|b.is_ascii_digit())=>format!("/zh-cn/ai-detail/{id}"),None=>"/zh-cn/user-center".into(),_=>return Err("无效应用ID".into())};
 url::Url::parse(&format!("{SITE}{path}")).map_err(|e|e.to_string())
}
fn same_application(current:&url::Url,target:&url::Url)->bool{
 if current.origin()!=target.origin(){return false}
 let app_id=|u:&url::Url|{let parts:Vec<_>=u.path().trim_matches('/').split('/').collect();if parts.len()==3&&parts[0]=="zh-cn"&&["ai-detail","ai-detail-mobile"].contains(&parts[1]){Some(parts[2].to_string())}else{None}};
 matches!((app_id(current),app_id(target)),(Some(a),Some(b)) if a==b)
}
fn allowed(url:&url::Url)->bool{
 if url.as_str()=="about:blank"{return true}
 if url.scheme()!="https"{return false}
 let host=url.host_str().unwrap_or("");host=="runninghub.ai"||host.ends_with(".runninghub.ai")||host.ends_with(".xiaoyaoyou.com")||["accounts.google.com","open.weixin.qq.com","github.com"].contains(&host)
}
fn bounds(app:&tauri::AppHandle,x:f64,y:f64,width:f64,height:f64,ratio:f64)->Result<(tauri::LogicalPosition<f64>,tauri::LogicalSize<f64>),String>{
 if ![x,y,width,height,ratio].iter().all(|n|n.is_finite())||x<0.||y<0.||width<40.||height<40.||!(0.25..=8.).contains(&ratio){return Err("网页区域尺寸无效".into())}
 let window=app.get_window("main").ok_or("主窗口不存在")?;let size=window.inner_size().map_err(|e|e.to_string())?;let dpi=window.scale_factor().map_err(|e|e.to_string())?;
 if (x+width)*ratio>size.width as f64+4.||(y+height)*ratio>size.height as f64+4.{return Err("网页区域超出主窗口".into())}
 let factor=ratio/dpi;Ok((tauri::LogicalPosition::new(x*factor,y*factor),tauri::LogicalSize::new(width*factor,height*factor)))
}
const FAVORITES_SCRIPT:&str=r#"(() => {
 if(location.origin!=='https://www.runninghub.ai')return;
 const pages=new Map();
 const report=(url,body,data)=>{try{
  if(new URL(url,location.origin).pathname!=='/api/likeOrCollect/webapp/list'||JSON.parse(body||'{}').operateType!==2||data.code!==0)return;
  const d=data.data,p=Number(d.current||1);if(p===1)pages.clear();pages.set(p,d.records||[]);
  const expected=Number(d.pages||1),complete=(!d.hasNext&&p>=expected)&&pages.size>=expected;
  const records=[...pages.keys()].sort((a,b)=>a-b).flatMap(k=>pages.get(k)).map(r=>({id:String(r.id),name:r.name,intro:r.intro,owner:{name:r.owner?.name},covers:r.covers}));
  window.__TAURI_INTERNALS__?.invoke('runninghub_report',{report:{records,complete}}).catch(()=>{});
 }catch{}};
 const originalFetch=window.fetch;window.fetch=async function(input,options){const result=await originalFetch.apply(this,arguments);const url=typeof input==='string'?input:input.url;
  if(url?.includes('/api/likeOrCollect/webapp/list'))result.clone().json().then(data=>report(url,options?.body,data)).catch(()=>{});return result;};
 const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
 XMLHttpRequest.prototype.open=function(method,url){this._moflowUrl=String(url);return open.apply(this,arguments)};
 XMLHttpRequest.prototype.send=function(body){if(this._moflowUrl?.includes('/api/likeOrCollect/webapp/list'))this.addEventListener('load',()=>{try{report(this._moflowUrl,body,this.responseType==='json'?this.response:JSON.parse(this.responseText))}catch{}});return send.apply(this,arguments)};
 if(location.pathname.endsWith('/user-center')){let attempts=0;const timer=setInterval(()=>{const tabs=[...document.querySelectorAll('.radio-item')];const favorite=tabs.find(e=>e.textContent.trim()==='我的收藏');if(favorite&&!favorite.classList.contains('active'))favorite.click();const apps=[...document.querySelectorAll('.radio-item')].find(e=>e.textContent.trim()==='AI应用');if(apps&&!apps.classList.contains('active'))apps.click();if((favorite?.classList.contains('active')&&apps?.classList.contains('active'))||++attempts>60)clearInterval(timer)},300)}
})();"#;

#[tauri::command]
pub async fn runninghub_open(app:tauri::AppHandle,app_id:Option<String>,x:f64,y:f64,width:f64,height:f64,pixel_ratio:f64)->Result<Value,String>{
 let url=site_url(app_id.as_deref())?;let (position,size)=bounds(&app,x,y,width,height,pixel_ratio)?;
 if let Some(view)=app.get_webview("runninghub-site"){
  view.set_position(position).map_err(|e|e.to_string())?;view.set_size(size).map_err(|e|e.to_string())?;view.show().map_err(|e|e.to_string())?;
  let reused=view.url().is_ok_and(|current|same_application(&current,&url));
  if !reused{view.navigate(url).map_err(|e|e.to_string())?;}
  return Ok(json!({"opened":true,"embedded":true,"reused":reused}));
 }
 let bootstrap=service(&app,"session",None).unwrap_or_else(|_|json!({}));
 let storage=bootstrap.get("origins").and_then(Value::as_array).and_then(|a|a.first()).and_then(|o|o.get("localStorage")).cloned().unwrap_or(json!([]));
 let script=format!("if(location.origin==='https://www.runninghub.ai'&&!localStorage.getItem('Rh-Accesstoken')){{try{{for(const item of {})localStorage.setItem(item.name,item.value)}}catch{{}}}}\n{}",storage,format!("{}\n{}",FAVORITES_SCRIPT,include_str!("runninghub-picker.js")));
 let profile=bootstrap.get("webview_profile").and_then(Value::as_str).map(PathBuf::from).unwrap_or_else(||PathBuf::from(r"D:\素材采集\studio\data\webview-runninghub"));
 std::fs::create_dir_all(&profile).map_err(|_|"无法创建网页登录资料目录")?;
 let new_window_app=app.clone();let event_app=app.clone();
 let mut builder=WebviewBuilder::new("runninghub-site",tauri::WebviewUrl::External("about:blank".parse().unwrap()))
  .data_directory(profile).disable_drag_drop_handler().initialization_script(script).background_color(tauri::utils::config::Color(0,0,0,255)).on_navigation(allowed)
  .on_new_window(move|url,_|{if allowed(&url)&&url.host_str().is_some_and(|h|h.ends_with("runninghub.ai")||h.ends_with("xiaoyaoyou.com")){let handle=new_window_app.clone();std::thread::spawn(move||{if let Some(v)=handle.get_webview("runninghub-site"){let _=v.navigate(url);}});NewWindowResponse::Deny}else if allowed(&url){NewWindowResponse::Allow}else{NewWindowResponse::Deny}})
  .on_page_load(move|_,payload|{let u=payload.url();if u.host_str()==Some("www.runninghub.ai"){let address=format!("{}://{}{}",u.scheme(),u.host_str().unwrap_or(""),u.path());let phase=if payload.event()==PageLoadEvent::Finished{"loaded"}else{"loading"};let _=event_app.emit_to("main","runninghub-page",json!({"url":address,"phase":phase}));}})
  .on_download(move|view,event|{
   let app=view.app_handle().clone();match event{
    DownloadEvent::Requested{destination,..}=>{
     match service(&app,"download-policy",None){Ok(policy)=>{let folder=PathBuf::from(policy["directory"].as_str().unwrap_or(""));if !folder.is_absolute(){return false}
      let name=destination.file_name().unwrap_or_default().to_string_lossy();let name:String=name.chars().map(|c|if "<>:\"/\\|?*".contains(c)||c.is_control(){'_'}else{c}).collect();let name=name.trim_matches([' ','.']);if name.is_empty(){return false}let mut target=folder.join(name);if target.exists(){target=folder.join(format!("{}_{}",uuid::Uuid::new_v4().simple(),name));}*destination=target;true},Err(message)=>{let _=app.emit_to("main","runninghub-notice",json!({"message":message}));false}}
    },
    DownloadEvent::Finished{path,success,..}=>{if success{if let Some(path)=path{let source=view.url().ok().map(|u|format!("https://www.runninghub.ai{}",u.path())).unwrap_or(SITE.into());std::thread::spawn(move||{let result=service(&app,"download-complete",Some(json!({"path":path.to_string_lossy(),"source":source})));let message=if result.is_ok(){"RunningHub 下载已保存并检查入库".into()}else{format!("文件已保存；{}",result.unwrap_err())};let _=app.emit_to("main","runninghub-notice",json!({"message":message}));});}}true},
    _=>false,
   }
  });
 if let Ok(port)=std::env::var("MOFLOW_RH_DEBUG_PORT"){if port.parse::<u16>().is_ok(){builder=builder.additional_browser_args(&format!("--remote-debugging-port={port}"));}}
 let parent=app.get_window("main").ok_or("主窗口不存在")?;let view=parent.add_child(builder,position,size).map_err(|e|e.to_string())?;
 if let Some(cookies)=bootstrap.get("cookies").and_then(Value::as_array){for c in cookies{if let (Some(name),Some(value),Some(domain))=(c["name"].as_str(),c["value"].as_str(),c["domain"].as_str()){
  let cookie=Cookie::build((name.to_string(),value.to_string())).domain(domain.to_string()).path(c["path"].as_str().unwrap_or("/").to_string()).secure(c["secure"].as_bool().unwrap_or(true)).http_only(c["httpOnly"].as_bool().unwrap_or(false)).build();let _=view.set_cookie(cookie);
 }}}
 view.navigate(url).map_err(|e|e.to_string())?;Ok(json!({"opened":true,"embedded":true,"login_imported":!storage.as_array().is_none_or(Vec::is_empty)}))
}
#[tauri::command]
pub async fn runninghub_bounds(app:tauri::AppHandle,x:f64,y:f64,width:f64,height:f64,pixel_ratio:f64)->Result<(),String>{
 let (position,size)=bounds(&app,x,y,width,height,pixel_ratio)?;if let Some(v)=app.get_webview("runninghub-site"){v.set_position(position).map_err(|e|e.to_string())?;v.set_size(size).map_err(|e|e.to_string())?;}Ok(())
}
#[tauri::command]
pub async fn runninghub_action(app:tauri::AppHandle,action:String)->Result<(),String>{
 if let Some(view)=app.get_webview("runninghub-site"){match action.as_str(){"hide"=>view.hide(),"show"=>view.show(),"reload"=>view.reload(),"back"=>view.eval("history.back()"),"forward"=>view.eval("history.forward()"),"close"=>view.close(),_=>return Err("未知网页操作".into())}.map_err(|e|e.to_string())?;}Ok(())
}
#[tauri::command]
pub async fn runninghub_report(app:tauri::AppHandle,report:Value)->Result<(),String>{
 service(&app,"favorites-report",Some(report))?;let _=app.emit_to("main","runninghub-favorites",json!({"updated":true}));Ok(())
}

include!("runninghub_picker.rs");
