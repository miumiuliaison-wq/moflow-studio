// Uses WebView2's in-process protocol. No remote debugging socket or file-read IPC.
async fn protocol(view:tauri::Webview,method:&str,params:Value)->Result<Value,String>{
 let method=method.to_string();
 tauri::async_runtime::spawn_blocking(move||{
  let (tx,rx)=std::sync::mpsc::channel();
  view.with_webview(move|platform|{
   let fail=tx.clone();
   let result=(||unsafe{
    let core=platform.controller().CoreWebView2()?;
    let handler=webview2_com::CallDevToolsProtocolMethodCompletedHandler::create(Box::new(move|status,result|{
     let value=status.map_err(|e|e.to_string()).and_then(|_|serde_json::from_str::<Value>(&result).map_err(|e|e.to_string()));
     let _=tx.send(value);Ok(())
    }));
    core.CallDevToolsProtocolMethod(&windows::core::HSTRING::from(method),&windows::core::HSTRING::from(params.to_string()),&handler)
   })();
   if let Err(error)=result{let _=fail.send(Err(error.to_string()));}
  }).map_err(|e|e.to_string())?;
  rx.recv_timeout(Duration::from_secs(12)).map_err(|_|"素材输入控件未及时响应".to_string())?
 }).await.map_err(|e|e.to_string())?
}

static PICKER_BUSY:std::sync::atomic::AtomicBool=std::sync::atomic::AtomicBool::new(false);
struct PickerGuard;
impl Drop for PickerGuard{fn drop(&mut self){PICKER_BUSY.store(false,std::sync::atomic::Ordering::SeqCst);}}

#[tauri::command]
pub async fn runninghub_pick_file(app:tauri::AppHandle,webview:tauri::Webview,token:String,kind:String)->Result<Value,String>{
 if webview.label()!="runninghub-site"||!matches!(kind.as_str(),"image"|"video")||token.len()!=36||!token.bytes().all(|b|b.is_ascii_hexdigit()||b==b'-'){return Err("无效素材选择请求".into())}
 let before=webview.url().map_err(|e|e.to_string())?;
 if before.origin().ascii_serialization()!=SITE{return Err("仅允许 RunningHub 官网选择素材".into())}
 if PICKER_BUSY.swap(true,std::sync::atomic::Ordering::SeqCst){return Err("请先完成已打开的素材选择".into())}
 let _guard=PickerGuard;
 let selector=format!("input[type=file][data-moflow-picker='{token}']");
 let expression=format!("(()=>{{let e=document.querySelector({});return e?{{multiple:e.multiple,accept:e.accept,disabled:e.disabled}}:null}})()",json!(selector));
 let info=protocol(webview.clone(),"Runtime.evaluate",json!({"expression":expression,"returnByValue":true})).await?;
 let value=&info["result"]["value"];
 if !value.is_object()||value["disabled"]==true{return Err("上传控件已变更，请重新点击".into())}
 let accept=value["accept"].as_str().unwrap_or("").to_lowercase();
 let image=accept.contains("image/")||[".png",".jpg",".jpeg",".webp",".gif",".bmp"].iter().any(|s|accept.contains(s));
 let video=accept.contains("video/")||[".mp4",".mov",".webm",".mkv",".m4v"].iter().any(|s|accept.contains(s));
 if (image&&!video&&kind!="image")||(video&&!image&&kind!="video")||(!accept.is_empty()&&!image&&!video){return Err("素材类型与上传控件不符".into())}
 let policy=tauri::async_runtime::spawn_blocking({let app=app.clone();move||service(&app,"input-policy",None)}).await.map_err(|e|e.to_string())??;
 let directory=PathBuf::from(policy[&kind].as_str().ok_or("未设置素材目录")?);
 if !directory.is_absolute()||!directory.is_dir(){return Err(format!("素材目录不存在，请在设置中重新选择：{}",directory.display()))}
 let mut dialog=rfd::AsyncFileDialog::new().set_title(if kind=="image"{"选择参考图片"}else{"选择动作视频"}).set_directory(&directory);
 if let Some(parent)=app.get_window("main"){dialog=dialog.set_parent(&parent);}
 let extensions:Vec<&str>=if kind=="image"{vec!["png","jpg","jpeg","webp","gif","bmp"]}else{vec!["mp4","mov","webm","mkv","m4v"]};
 // Honor explicit extension/MIME restrictions where present.
 let specific:Vec<&str>=extensions.iter().copied().filter(|e|{
  let mime=match *e{"jpg"|"jpeg"=>"image/jpeg","mov"=>"video/quicktime","mkv"=>"video/x-matroska","m4v"=>"video/x-m4v",_=>""};
  accept.is_empty()||accept.contains("*/*")||accept.contains(&format!("{kind}/*"))||accept.contains(&format!(".{e}"))||accept.contains(&format!("{kind}/{e}"))||(!mime.is_empty()&&accept.contains(mime))
 }).collect();
 if specific.is_empty(){return Err("暂不支持该控件的文件格式".into())}
 dialog=dialog.add_filter(if kind=="image"{"图片"}else{"视频"},&specific);
 let files=if value["multiple"]==true{dialog.pick_files().await}else{dialog.pick_file().await.map(|p|vec![p])};
 let Some(files)=files else{return Ok(json!({"cancelled":true}))};
 if webview.url().ok().as_ref()!=Some(&before){return Err("网页已经切换，请重新选择素材".into())}
 let paths:Vec<_>=files.iter().map(|f|f.path().to_string_lossy().to_string()).collect();
 if files.iter().any(|f|!f.path().is_file()||!f.path().extension().and_then(|s|s.to_str()).is_some_and(|e|specific.iter().any(|x|x.eq_ignore_ascii_case(e)))){return Err("所选文件不存在或格式不符".into())}
 let node=protocol(webview.clone(),"Runtime.evaluate",json!({"expression":format!("document.querySelector({})",json!(selector))})).await?;
 let object=node["result"]["objectId"].as_str().ok_or("上传控件已移除，请重新点击")?;
 protocol(webview.clone(),"DOM.setFileInputFiles",json!({"files":paths,"objectId":object})).await?;
 let _=protocol(webview,"Runtime.releaseObject",json!({"objectId":object})).await;
 Ok(json!({"selected":files.len()}))
}
