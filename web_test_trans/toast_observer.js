(() => {
  const defaults='[role="alert"],[role="status"],.ant-message-notice,.ant-notification-notice,.el-message,.el-notification,.Toastify__toast,.toast,.snackbar,.q-notification,.v-snackbar';
  const seen=new WeakMap();
  const send=payload=>window.__wtt_toast_emit({at:Date.now(),...payload}).catch(()=>{});
  function scan() {
    if(window.__wtt.paused)return;
    const selector=defaults+(window.__wtt.toastSelector?','+window.__wtt.toastSelector:'');
    let nodes;
    try {nodes=document.querySelectorAll(selector);}catch {return;}
    for(const el of nodes){
      if(!el.getClientRects().length||getComputedStyle(el).visibility==='hidden'||getComputedStyle(el).display==='none'){
        seen.delete(el);continue;
      }
      if(el.parentElement?.closest(selector))continue;
      const text=(el.textContent||'').trim();
      if(seen.get(el)===text)continue;
      seen.set(el,text);
      // Toasts are asserted as a CSS-located collection; never choose an ordinal.
      try {send({type:'toast',target:{css:window.__wtt.selector(el,true),kind:'toast'},text});}
      catch(error){send({type:'warning',message:'Toast 定位失败：'+error.message});}
    }
  }
  function observe(){
    new MutationObserver(scan).observe(document.documentElement,{subtree:true,childList:true,attributes:true,characterData:true});
    setInterval(scan,50);scan();
  }
  if(document.documentElement)observe();else document.addEventListener('DOMContentLoaded',observe,{once:true});
})();
