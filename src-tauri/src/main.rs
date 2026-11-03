#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{io::{Read, Write},net::{TcpStream,SocketAddr},path::Path,process::{Child,Command},sync::Mutex,time::Duration};
use tauri::{Emitter,Manager,menu::MenuBuilder,tray::TrayIconBuilder};
mod runninghub;
struct Service(Mutex<Option<Child>>,String);
fn ready()->bool{
 let addr:SocketAddr="127.0.0.1:7861".parse().unwrap();
 if let Ok(mut s)=TcpStream::connect_timeout(&addr,Duration::from_millis(300)){
  let _=s.set_read_timeout(Some(Duration::from_millis(500)));let _=s.write_all(b"GET /api/v1/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n");let mut text=String::new();let _=s.read_to_string(&mut text);return text.contains("\"app\":\"moflow\"")&&text.contains("\"ready\"");
 }false
}
#[tauri::command]
async fn choose_directory()->Option<String>{rfd::AsyncFileDialog::new().set_title("选择素材目录").pick_folder().await.map(|p|p.path().to_string_lossy().to_string())}
#[tauri::command]
fn open_path(path:String)->Result<(),String>{
 if path.starts_with("https://"){
  let u=url::Url::parse(&path).map_err(|e|e.to_string())?;let host=u.host_str().unwrap_or("");if host!="douyin.com"&&!host.ends_with(".douyin.com"){return Err("只允许打开抖音来源链接".into())}
 }else{
  let p=Path::new(&path);if !p.is_absolute()||!p.exists(){return Err("本地文件或目录不存在".into())}
  if p.is_file(){let ext=p.extension().and_then(|s|s.to_str()).unwrap_or("").to_lowercase();if !["mp4","mov","webm","jpg","jpeg","png","webp","md","json"].contains(&ext.as_str()){return Err("不能通过素材入口运行程序".into())}}
 }
 Command::new("explorer.exe").arg(path).spawn().map_err(|e|e.to_string())?;Ok(())
}
#[tauri::command]
fn reveal_file(path:String)->Result<(),String>{
 let p=Path::new(&path);if !p.is_absolute()||!p.is_file(){return Err("文件已移动或删除".into())}
 Command::new("explorer.exe").arg("/select,").arg(p).spawn().map_err(|e|e.to_string())?;Ok(())
}
fn main(){
 tauri::Builder::default()
 .plugin(tauri_plugin_single_instance::init(|app,args,_|{if args.iter().any(|a|a=="--quit"){app.exit(0);return;}if let Some(w)=app.get_window("main"){let _=w.show();let _=w.set_focus();}}))
 .manage(Service(Mutex::new(None),uuid::Uuid::new_v4().to_string()))
 .invoke_handler(tauri::generate_handler![choose_directory,open_path,reveal_file,runninghub::runninghub_pick_file,runninghub::runninghub_open,runninghub::runninghub_bounds,runninghub::runninghub_action,runninghub::runninghub_report])
 .setup(|app|{
  if std::env::args().any(|a|a=="--quit"){app.handle().exit(0);return Ok(());}
  let menu=MenuBuilder::new(app).text("open","打开墨流 / Open Studio").text("pause","暂停全部自动化").separator().text("exit","退出 / Exit").build()?;
  let icon=tauri::image::Image::from_bytes(include_bytes!("../icons/icon.png"))?;
  TrayIconBuilder::new().icon(icon).tooltip("墨流 · Moflow Studio").menu(&menu).on_menu_event(|app,e|match e.id().as_ref(){"open"=>{if let Some(w)=app.get_window("main"){let _=w.show();let _=w.set_focus();}},"pause"=>{let _=app.emit("pause-all",());},"exit"=>app.exit(0),_=>{}}).build(app)?;
  if !ready(){
   let exe=std::env::current_exe()?;let direct=exe.parent().unwrap().join("moflow-service.exe");let resource=app.path().resource_dir()?.join("moflow-service.exe");let binary=if direct.exists(){direct}else{resource};
   if !binary.exists(){return Err("本地服务程序缺失，请完整解压墨流文件夹".into())}
   let mut command=Command::new(binary);command.current_dir(exe.parent().unwrap()).env("MOFLOW_CONTROL_TOKEN",&app.state::<Service>().1);
   #[cfg(windows)]{use std::os::windows::process::CommandExt;command.creation_flags(0x08000000);}
   let child=command.spawn()?;*app.state::<Service>().0.lock().unwrap()=Some(child);
  }
  let mut builder=tauri::WebviewWindowBuilder::new(app,"main",tauri::WebviewUrl::App("index.html".into())).title("墨流 · Moflow Studio").theme(Some(tauri::Theme::Dark)).inner_size(1380.,940.).min_inner_size(760.,600.).visible(false);
  if let Ok(port)=std::env::var("MOFLOW_UI_DEBUG_PORT"){if port.parse::<u16>().is_ok(){builder=builder.additional_browser_args(&format!("--remote-debugging-port={port}"));}}
  let window=builder.build()?;
  let handle=app.handle().clone();std::thread::spawn(move||{
   for _ in 0..120{if ready(){let _=window.navigate("http://127.0.0.1:7861".parse().unwrap());let _=window.show();return;}std::thread::sleep(Duration::from_millis(500));}
   rfd::MessageDialog::new().set_title("墨流启动失败").set_description("本地服务未能就绪。请检查7861端口及 D:\\素材采集\\studio\\data 中的运行文件。").show();handle.exit(1);
  });Ok(())
 })
 .on_window_event(|window,event|{if window.label()=="main"{if let tauri::WindowEvent::CloseRequested{api,..}=event{api.prevent_close();let _=window.hide();}}})
 .build(tauri::generate_context!()).expect("墨流桌面启动失败")
 .run(|app,event|{if let tauri::RunEvent::Exit=event{if let Some(mut child)=app.state::<Service>().0.lock().unwrap().take(){
  if let Ok(mut socket)=TcpStream::connect("127.0.0.1:7861"){let _=socket.write_all(format!("POST /api/v1/system/exit HTTP/1.0\r\nHost: 127.0.0.1\r\nX-Moflow-Control: {}\r\nContent-Length: 0\r\n\r\n",app.state::<Service>().1).as_bytes());}
  for _ in 0..200{if child.try_wait().ok().flatten().is_some(){return;}std::thread::sleep(Duration::from_millis(100));}let _=child.kill();let _=child.wait();
 }}});
}
