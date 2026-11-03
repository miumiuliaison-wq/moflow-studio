import {useEffect,useRef,useState} from 'react';
import {ArrowLeft,ArrowRight,RefreshCw,Search,Grid2X2,Globe,ExternalLink,FolderOpen} from 'lucide-react';
import {invoke} from '@tauri-apps/api/core';
import {listen} from '@tauri-apps/api/event';
import './runninghub.css';

type App={id:string;name:string;author:string;description:string;cover:string;source:string;available:boolean};
type Props={api:(path:string,body?:unknown,method?:string)=>Promise<any>;notify:(message:string)=>void;suspended:boolean};
let cachedApps:App[]=[];
export function RunningHubPage({api,notify,suspended}:Props){
 const [apps,setApps]=useState<App[]>(cachedApps),[query,setQuery]=useState(''),[opened,setOpened]=useState<App|true|null>(null),[busy,setBusy]=useState(false),[address,setAddress]=useState(''),[error,setError]=useState(''),[syncing,setSyncing]=useState(false),[pageLoading,setPageLoading]=useState(false);
 const host=useRef<HTMLDivElement>(null),alive=useRef(true),created=useRef(false),selected=useRef<App|true|null>(null),opening=useRef(false),syncFlight=useRef(false),lastBounds=useRef('');
 const desktop=!!(window as any).__TAURI_INTERNALS__;
 const load=async()=>{const result=await api('/runninghub/apps');cachedApps=result.items;if(alive.current)setApps(result.items);return result};
 const openMaterialFolder=async(kind:string)=>{try{const {settings}=await api('/settings');await invoke('open_path',{path:settings[kind==='image'?'reference_image_dir':'reference_video_dir']})}catch(e){notify(String(e))}};
 const action=async(value:string)=>{if(value==='reload')setPageLoading(true);try{await invoke('runninghub_action',{action:value})}catch(e){setPageLoading(false);notify(String(e))}};
 const region=()=>{const rect=host.current?.getBoundingClientRect();if(!rect)return null;const height=Math.min(rect.height,innerHeight-rect.top-8);if(height<40||rect.width<40||rect.top<0)return null;return {x:Math.max(0,rect.left),y:rect.top,width:Math.min(rect.width,innerWidth-rect.left),height,pixelRatio:devicePixelRatio}};
 const resize=async()=>{if(!created.current||!selected.current||suspended)return;const box=region();if(box){const key=JSON.stringify(box);if(lastBounds.current!==key)try{await invoke('runninghub_bounds',box);lastBounds.current=key}catch{}}};
 const open=async(app:App|true)=>{
  if(opening.current)return;opening.current=true;
  setOpened(app);selected.current=app;setError('');setBusy(true);setPageLoading(true);
  try{
   await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
   const box=region();if(!box)throw new Error('窗口空间不足，请调整窗口大小');
   const result=await invoke<{embedded:boolean;reused?:boolean}>('runninghub_open',{appId:app===true?null:app.id,...box});
   if(!result.embedded)throw new Error('内置网页未能打开');created.current=true;
   lastBounds.current=JSON.stringify(box);if(result.reused)setPageLoading(false);
   if(!alive.current||!selected.current){await invoke('runninghub_action',{action:'hide'});return}
   setAddress(app===true?'https://www.runninghub.ai/zh-cn/user-center':app.source);
  }catch(e){if(alive.current){setError(String(e));setPageLoading(false)}}finally{opening.current=false;if(alive.current)setBusy(false)}
 };
 const backToGrid=()=>{selected.current=null;setOpened(null);action('hide');load().catch(()=>{})};
 const sync=async(start=true)=>{
  if(syncFlight.current)return;syncFlight.current=true;setSyncing(true);
  try{
   let state=start?await api('/runninghub/apps/sync',{}):(await load()).sync;const deadline=Date.now()+49000;
   while(state?.state==='syncing'&&alive.current){
    if(Date.now()>deadline)throw Error('收藏仍在后台更新，可以继续打开应用，稍后再查看。');
    await new Promise(r=>setTimeout(r,650));if(!alive.current)return;
    state=(await load()).sync;
   }
   if(!alive.current)return;
   if(state?.state==='error'||state?.state==='waiting_login')throw Error(state.message);
   await load();notify(`已同步 ${state?.count??cachedApps.filter(a=>a.available).length} 个收藏应用`);
  }catch(e){if(alive.current)notify(String(e))}finally{syncFlight.current=false;if(alive.current)setSyncing(false)}
 };
 useEffect(()=>{
  alive.current=true;load().then(v=>{if(alive.current&&v.sync?.state==='syncing')sync(false)}).catch(e=>notify(String(e)));const stops:(()=>void)[]=[];
  if(desktop){
   listen<{url:string;phase?:string}>('runninghub-page',e=>{if(alive.current){setAddress(e.payload.url);setPageLoading(e.payload.phase==='loading')}}).then(f=>alive.current?stops.push(f):f());
   listen('runninghub-favorites',()=>load().catch(()=>{})).then(f=>alive.current?stops.push(f):f());
   listen<{message:string}>('runninghub-notice',e=>{notify(e.payload.message)}).then(f=>alive.current?stops.push(f):f());
  }
  return()=>{alive.current=false;selected.current=null;stops.forEach(f=>f());if(desktop)invoke('runninghub_action',{action:'hide'}).catch(()=>{})};
 },[]);
 useEffect(()=>{if(!opened||!host.current)return;const observer=new ResizeObserver(()=>resize());observer.observe(host.current);const move=()=>resize();window.addEventListener('resize',move);window.addEventListener('scroll',move,true);return()=>{observer.disconnect();window.removeEventListener('resize',move);window.removeEventListener('scroll',move,true)}},[opened,suspended]);
 useEffect(()=>{if(!opened||!created.current)return;if(suspended)action('hide');else{resize().then(()=>action('show'))}},[suspended,opened]);
 const visible=apps.filter(a=>(a.name+' '+a.author+' '+a.description).toLowerCase().includes(query.toLowerCase()));
 if(opened)return <section className="rh-browser"><div className="rh-browser-toolbar"><button onClick={backToGrid}><Grid2X2 size={15}/>应用列表</button><div className="rh-history"><button className="icon" aria-label="网页后退" onClick={()=>action('back')}><ArrowLeft size={16}/></button><button className="icon" aria-label="网页前进" onClick={()=>action('forward')}><ArrowRight size={16}/></button><button className="icon" aria-label="刷新应用网页" onClick={()=>action('reload')}><RefreshCw size={15}/></button></div><span className="rh-address" title={address}>{opened===true?'我的收藏':opened.name}<small>{address}</small></span><button title="打开默认参考图片目录" onClick={()=>openMaterialFolder('image')}><FolderOpen size={15}/>图片目录</button><button title="打开默认动作视频目录" onClick={()=>openMaterialFolder('video')}><FolderOpen size={15}/>视频目录</button><span className="rh-page-state" role="status">{busy?'正在连接…':pageLoading?<><RefreshCw size={12} className="rh-spin"/>网页加载中…</>:'已打开'}</span><button className="icon" aria-label="打开收藏网页" title="我的收藏" onClick={()=>open(true)}><Globe size={17}/></button></div><div className="rh-host" ref={host}>{busy?<div className="rh-loading"><RefreshCw size={22}/><p>正在软件内打开 RunningHub…</p><small>正在复用本地登录与网页缓存。</small></div>:error?<div className="rh-loading"><p>{error}</p><button onClick={()=>open(opened)}>重新打开</button></div>:<div className="rh-loading"><Globe size={23}/><p>{suspended?'网页暂时隐藏，关闭浮层后继续。':'RunningHub 原网页'}</p></div>}</div></section>;
 return <section className="rh-apps"><div className="rh-intro"><div><h2>Your apps, one place.</h2><p>你收藏的 RunningHub 应用，在墨流里直接打开。</p></div><button className="primary" disabled={!desktop||busy} onClick={()=>open(true)}><Globe size={16}/>打开我的收藏</button></div>{!desktop&&<div className="banner">请从桌面“墨流 Studio”打开此页面，内置网页需要桌面窗口。</div>}<div className="toolbar rh-toolbar"><div className="search-field"><Search size={16}/><input aria-label="搜索收藏应用" placeholder="搜索名称、作者或用途…" value={query} onChange={e=>setQuery(e.target.value)}/></div><span className="rh-count">{apps.filter(a=>a.available).length} 个收藏应用</span><div className="spacer"/><button disabled={syncing} onClick={()=>sync()}><RefreshCw size={15} className={syncing?'rh-spin':''}/>{syncing?'后台同步中…':'刷新收藏'}</button></div>{syncing&&<p className="rh-sync-note" role="status">正在后台同步收藏，可以继续打开应用。</p>}{visible.length?<div className="rh-app-grid">{visible.map(app=><button className={'rh-app-card'+(!app.available?' unavailable':'')} key={app.id} disabled={!desktop||!app.available||busy} onClick={()=>open(app)}><div className="rh-app-cover">{app.cover&&!/\.mp4(?:\?|$)/i.test(app.cover)?<img loading="lazy" src={app.cover} alt=""/>:<Globe size={30}/>}<span>{app.available?'在软件中打开':'已取消收藏或下架'}<ExternalLink size={12}/></span></div><div className="rh-app-info"><h3>{app.name}</h3><p>{app.author}</p></div></button>)}</div>:<div className="rh-empty"><Globe size={30}/><h3>{query?'没有匹配的收藏应用':'打开你的 RunningHub 收藏'}</h3><p>在内置网页进入“个人中心 → 我的收藏 → AI应用”，应用列表会自动同步。</p>{!query&&<button disabled={!desktop} onClick={()=>open(true)}>打开收藏网页</button>}</div>}<div className="rh-footnote"><FolderOpen size={14}/><span>上传、参数和运行使用官网原界面。网页下载自动保存到素材目录。</span></div></section>;
}
