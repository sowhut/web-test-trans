const $ = id => document.getElementById(id);
const state = {id:null, active:null, tab:'timeline', session:null, registry:{}, lastRender:'', busy:false};
const names = {click:'点击',dblclick:'双击',fill:'输入',press:'键盘操作',check:'勾选状态',select:'选择选项',hover:'鼠标悬停',wheel:'滚动',drag:'拖拽',toast:'Toast 断言',navigation:'页面导航',page_open:'打开页面',page_close:'关闭页面',auth_challenge:'浏览器认证',key_event:'原始键盘事件',mouse_event:'原始鼠标事件',key_down:'按下按键',key_up:'释放按键',fill_secret:'密码输入',mouse_down:'按下鼠标'};
const statuses = {starting:'启动中',recording:'录制中',stopped:'已完成',interrupted:'已中断',error:'启动失败',stopping:'停止中'};
function node(tag, className, text) { const el = document.createElement(tag); if(className) el.className=className; if(text !== undefined) el.textContent=text; return el; }
function notice(message) { $('notice').textContent=message; $('notice').hidden=false; clearTimeout(notice.timer); notice.timer=setTimeout(()=>$('notice').hidden=true,7000); }
async function api(path, options) {
  const response = await fetch('/api'+path,options);
  if(!response.ok) { const error=await response.json().catch(()=>({detail:response.statusText})); throw Error(typeof error.detail==='string'?error.detail:JSON.stringify(error.detail)); }
  return response.headers.get('content-type')?.includes('application/json')?response.json():response.text();
}
const post = (path, body={}) => api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function tab(name) {
  state.tab=name;
  for(const item of ['timeline','code','components']) $(item).hidden=item!==name;
  document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));
  refreshDetails(true).catch(e=>notice(e.message));
}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
$('nav-components').onclick=()=>tab('components');
$('nav-record').onclick=()=>tab('timeline');
document.querySelectorAll('[name=backend]').forEach(el=>el.onchange=()=>{
  const native = document.querySelector('[name=backend]:checked').value==='windows';
  $('web-fields').hidden=native; $('windows-fields').hidden=!native;
});
$('use-demo').onclick=()=>{ $('target-url').value=location.origin+'/demo'; };
$('show-raw').onchange=()=>{state.lastRender='';renderSession();};
$('new-session').onclick=()=>{
  if(state.active){notice('当前仍在录制，请先停止。');return;}
  state.id=null;state.session=null;state.lastRender='';
  location.reload();
};
$('start-form').onsubmit=async event=>{
  event.preventDefault();
  if(state.busy)return;
  state.busy=true; $('start-button').disabled=true; $('start-button').textContent='正在打开录制窗口…';
  try {
    const session=await post('/sessions',{
      name:$('session-name').value,backend:document.querySelector('[name=backend]:checked').value,
      url:$('target-url').value,process_id:Number($('process-id').value)||0,
      toast_window_ms:Number($('toast-window').value),toast_selector:$('toast-selector').value,
      toast_classes:$('toast-classes').value.split(',').map(s=>s.trim()).filter(Boolean),
      username:$('auth-user').value,password:$('auth-password').value
    });
    $('auth-password').value='';state.id=session.id;state.active=session.id;
    notice('录制已开始，请到新打开的目标窗口操作。');await refresh();
  } catch(error) { notice(error.message); }
  finally {state.busy=false;$('start-button').textContent='● 开始录制';$('start-button').disabled=!!state.active;}
};
$('stop-button').onclick=async()=>{
  $('stop-button').disabled=true;
  try{await post(`/sessions/${state.active}/stop`);state.active=null;notice('录制已保存，可以导出完整回放项目。');await refresh();}
  catch(error){notice(error.message);$('stop-button').disabled=false;}
};
$('export-button').onclick=()=>{if(state.id)location.href=`/api/sessions/${state.id}/export`;};
$('apply-auth').onclick=async()=>{
  $('apply-auth').disabled=true;
  try {await post(`/sessions/${state.active}/http-auth`,{username:$('auth-user').value,password:$('auth-password').value});$('auth-password').value='';notice('已应用 HTTP 认证并重新打开目标页面。');await refresh();}
  catch(error){notice(error.message);}
  finally{$('apply-auth').disabled=!state.active;}
};
function renderSession(){
  const session=state.session;if(!session)return;
  $('recording-title').textContent=session.name;
  $('state-badge').textContent=statuses[session.status]||session.status;
  $('state-badge').classList.toggle('live',session.status==='recording');
  $('action-count').textContent=session.events.filter(e=>!['mouse_event','key_event','mouse_down','page_open','page_close','navigation','toast','auth_challenge'].includes(e.type)&&!e.superseded).length;
  $('component-count').textContent=new Set(session.events.map(e=>e.component).filter(Boolean)).size;
  $('toast-count').textContent=session.events.filter(e=>e.type==='toast').length;
  $('page-count').textContent=session.backend==='windows'?'原生':new Set(session.events.filter(e=>e.type==='navigation').map(e=>e.url)).size;
  $('stop-button').disabled=session.id!==state.active;
  $('export-button').disabled=['recording','starting','stopping'].includes(session.status);
  $('apply-auth').disabled=session.backend!=='web'||session.id!==state.active;
  $('footer-status').textContent=session.status==='recording'?'正在监听键鼠操作与页面通知…':'录制已保存在本机';
  $('warnings').hidden=!session.warnings.length;
  $('warnings').replaceChildren(...session.warnings.map(w=>node('p','',w)));
  const signature=session.id+':'+session.events.length+':'+$('show-raw').checked;
  if(signature===state.lastRender)return;state.lastRender=signature;
  const visible=session.events.filter(e=>$('show-raw').checked||!['mouse_event','key_event','mouse_down'].includes(e.type)).filter(e=>!e.superseded);
  if(!visible.length)return;
  const rows=visible.slice(-300).map((e,i)=>{
    const row=node('div','event-row');
    row.append(node('span','event-index',String(i+1).padStart(2,'0')),node('span','event-icon',e.type==='toast'?'✓':e.type==='fill'?'T':e.type==='navigation'?'↗':'⌁'));
    const main=node('div','event-main');main.append(node('strong','',names[e.type]||e.type));
    let detail=e.url||e.component||e.scheme||'';
    if(e.secret_env) detail+=' → 环境变量 '+e.secret_env;
    else if(e.text!==undefined)detail+=' → '+e.text;
    else if(e.value!==undefined)detail+=' → '+JSON.stringify(e.value);
    else if(e.key)detail+=' → '+e.key;
    main.append(node('p','',detail));row.append(main,node('span','event-time',new Date(e.at).toLocaleTimeString('zh-CN',{hour12:false})));return row;
  });
  const nearEnd=$('timeline').scrollHeight-$('timeline').scrollTop-$('timeline').clientHeight<100;
  $('timeline').replaceChildren(...rows);if(nearEnd)$('timeline').scrollTop=$('timeline').scrollHeight;
}
async function refreshDetails(force=false){
  if(state.id){state.session=await api('/sessions/'+state.id);renderSession();}
  if(state.tab==='code'&&state.id)$('code').textContent=await api(`/sessions/${state.id}/code`);
  if(state.tab==='components'||force){
    state.registry=await api('/components');$('nav-count').textContent=Object.keys(state.registry).length;
    const cards=Object.values(state.registry).map(item=>{
      const card=node('div','component-card');card.append(node('h3','',item.kind+' · '+item.key));
      const value=item.locator.backend==='web'?[...(item.locator.frames||[]),item.locator.css].join(' → iframe → '):item.locator.path.map(p=>p.class_name).join(' > ');
      card.append(node('code','',value),node('code','',`components.get(${JSON.stringify(item.key)})`));return card;
    });
    $('components').replaceChildren(...(cards.length?cards:[node('div','empty-state','录制后，相同类型组件的定位会保存在这里。')]));
  }
}
async function refresh(){
  const [status,sessions]=await Promise.all([api('/status'),api('/sessions')]);
  state.active=status.active_id;
  $('system').textContent=status.platform+' · localhost';
  if(!status.windows_available)$('native-hint').textContent='当前为 '+status.platform+'，请在 Windows 上启动此工具后使用原生录制。';
  $('start-button').disabled=!!state.active||state.busy;
  if(!state.id&&state.active)state.id=state.active;
  $('history').replaceChildren(...sessions.map(s=>{
    const b=node('button',s.id===state.id?'active':'','◷  '+s.name);b.title=s.name+' · '+(statuses[s.status]||s.status);
    b.onclick=async()=>{state.id=s.id;state.lastRender='';await refresh();};return b;
  }));
  await refreshDetails(true);
}
async function poll(){try{await refresh();}catch(e){$('system').textContent='服务连接中断';}finally{setTimeout(poll,1200);}}
poll();
