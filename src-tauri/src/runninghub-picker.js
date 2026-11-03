(() => {
 if(location.origin!=='https://www.runninghub.ai'||window.__moflowPickerInstalled)return;
 window.__moflowPickerInstalled=true;
 let busy=false;
 const chooseKind=()=>new Promise(resolve=>{
  const layer=document.createElement('div');layer.style.cssText='position:fixed;inset:0;z-index:2147483647;background:#000a;display:grid;place-items:center';
  const box=document.createElement('div');box.style.cssText='background:#161616;color:white;padding:24px;border:1px solid #444;border-radius:12px;font:14px Microsoft YaHei UI';
  const title=document.createElement('p');title.textContent='请选择参考素材类型';box.append(title);
  for(const [kind,label] of [['image','图片'],['video','视频'],['','取消']]){const button=document.createElement('button');button.textContent=label;button.style.cssText='padding:10px 20px;margin:6px;background:#303030;color:white;border:1px solid #666;border-radius:6px';button.onclick=()=>{layer.remove();resolve(kind)};box.append(button)}
  layer.append(box);document.documentElement.append(layer);
 });
 const select=async input=>{
  if(busy||input.disabled)return;busy=true;
  const token=crypto.randomUUID();input.setAttribute('data-moflow-picker',token);
  try{
   const accept=input.accept.toLowerCase();const image=/image\/|\.(png|jpe?g|webp|gif|bmp)\b/.test(accept),video=/video\/|\.(mp4|mov|webm|mkv|m4v)\b/.test(accept);
   if(accept&&!image&&!video){originalClick.call(input);return}
   const kind=image&&!video?'image':video&&!image?'video':await chooseKind();
   if(!kind)return;
   await window.__TAURI_INTERNALS__.invoke('runninghub_pick_file',{token,kind});
  }catch(error){window.alert('墨流选择素材：'+String(error))}
  finally{input.removeAttribute('data-moflow-picker');busy=false}
 };
 const originalClick=HTMLInputElement.prototype.click;
 HTMLInputElement.prototype.click=function(){if(this.type==='file'){select(this);return}return originalClick.apply(this,arguments)};
 const originalShow=HTMLInputElement.prototype.showPicker;
 if(originalShow)HTMLInputElement.prototype.showPicker=function(){if(this.type==='file'){select(this);return}return originalShow.apply(this,arguments)};
 document.addEventListener('click',event=>{if(event.target instanceof HTMLInputElement&&event.target.type==='file'){
  const accept=event.target.accept.toLowerCase();if(accept&&!/image\/|video\/|\.(png|jpe?g|webp|gif|bmp|mp4|mov|webm|mkv|m4v)\b/.test(accept))return;
  event.preventDefault();event.stopImmediatePropagation();select(event.target);
 }},true);
})();
