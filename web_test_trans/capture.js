(() => {
  if (window.__wtt) return;
  const binding = payload => {
    const p = window.__wtt_emit?.({at: Date.now(), ...payload});
    if (p?.catch) p.catch(() => {});
  };
  const stable = value => /^[a-zA-Z_][a-zA-Z0-9_-]*$/.test(value) &&
    !/^(active|hover|focus|selected|disabled|is-|css-|ng-)|\d{5}|[a-f0-9]{8}/i.test(value);
  function segment(el, attributes) {
    let s = el.localName;
    const classes = Array.from(el.classList).filter(stable).slice(0, 3);
    if (classes.length) s += classes.map(c => '.' + CSS.escape(c)).join('');
    if (el.id && stable(el.id)) s += '#' + CSS.escape(el.id);
    if (attributes) {
      for (const attr of ['data-testid','data-test','data-qa','name','type','role']) {
        const value = el.getAttribute(attr);
        if (value && /^[a-zA-Z0-9_:-]+$/.test(value)) s += `[${attr}="${value}"]`;
      }
    }
    return s;
  }
  function selector(el, collection = false) {
    if (!(el instanceof Element)) throw Error('目标不是 DOM 元素');
    if (el.getRootNode() !== document) throw Error('基础版暂不支持 Shadow DOM 录制');
    const chain = [];
    for (let n = el; n; n = n.parentElement) chain.unshift(n);
    if (chain.length < 3) throw Error('目标不足三层真实 DOM，已跳过');
    for (let depth = 3; depth <= chain.length; depth++) {
      for (const attributes of [false, true]) {
        const css = chain.slice(-depth).map(n => segment(n, attributes)).join(' > ');
        const found = document.querySelectorAll(css);
        if ((found.length === 1 && found[0] === el) || (collection && [...found].includes(el))) return css;
      }
    }
    throw Error('无法生成唯一的三层 CSS；禁止顺序索引，请给重复控件增加稳定 class、id 或 data-testid');
  }
  function kind(el) {
    if (el.matches('input[type=checkbox],input[type=radio]')) return 'checkbox';
    if (el.matches('input,textarea,[contenteditable=true]')) return 'input';
    if (el.matches('select')) return 'select';
    if (el.matches('button,[role=button],input[type=submit]')) return 'button';
    if (el.matches('a')) return 'link';
    if (el.matches('iframe,frame')) return 'frame';
    return 'element';
  }
  function descriptor(el) { return {css: selector(el), kind: kind(el)}; }
  function target(e) {
    const el = e.composedPath()[0];
    return el?.closest?.('button,a,input,select,textarea,[role=button],[contenteditable=true]') || el;
  }
  function record(type, el, extra = {}) {
    try { binding({type, target: descriptor(el), ...extra}); }
    catch (err) { binding({type: 'warning', message: err.message}); }
  }
  const editable = el => el?.matches?.('textarea,[contenteditable=true],input:not([type=checkbox]):not([type=radio]):not([type=file]):not([type=button]):not([type=submit]):not([type=reset]):not([type=range]):not([type=color])');
  const secret = el => el?.matches?.('input[type=password]');
  let down = null, draggedAt = 0, hoverTimer, scrollTimer;
  const enabled = () => !window.__wtt.paused;
  document.addEventListener('mousedown', e => {
    if (!enabled()) return;
    down = {el: target(e), x: e.clientX, y: e.clientY, button: e.button};
    record('mouse_event', down.el, {phase: 'down', button: e.button, x: e.clientX, y: e.clientY});
  }, true);
  document.addEventListener('mouseup', e => {
    if (!enabled()) return;
    const el = target(e);
    record('mouse_event', el, {phase: 'up', button: e.button, x: e.clientX, y: e.clientY});
    if (down && down.button === 0 && Math.hypot(e.clientX-down.x, e.clientY-down.y) > 8) {
      try {
        record('drag', down.el, {destination: descriptor(el)});
        draggedAt = Date.now();
      } catch (err) { binding({type:'warning', message: err.message}); }
    }
    down = null;
  }, true);
  let htmlDragSource = null;
  document.addEventListener('dragstart', e => { if (enabled()) htmlDragSource = target(e); }, true);
  document.addEventListener('drop', e => {
    if (enabled() && htmlDragSource) {
      try { record('drag', htmlDragSource, {destination:descriptor(target(e))}); draggedAt=Date.now(); }
      catch (err) { binding({type:'warning',message:err.message}); }
      htmlDragSource=null; down=null;
    }
  }, true);
  for (const type of ['click', 'dblclick', 'contextmenu', 'auxclick']) {
    document.addEventListener(type, e => {
      if (!enabled() || Date.now() - draggedAt < 100) return;
      const el = target(e);
      if (el?.matches?.('input[type=checkbox],input[type=radio],select')) return;
      if (type === 'click' && e.detail > 1) return;
      if (type === 'auxclick' && e.button !== 1) return;
      record(type === 'dblclick' ? 'dblclick' : 'click', el,
        {button: ['left','middle','right'][e.button] || 'left',
         modifiers: ['Alt','Control','Meta','Shift'].filter((m,i) => [e.altKey,e.ctrlKey,e.metaKey,e.shiftKey][i])});
    }, true);
  }
  document.addEventListener('input', e => {
    if (!enabled() || !editable(e.target) || e.isComposing) return;
    record('fill', e.target, secret(e.target) ? {secret:true} : {value:e.target.value ?? e.target.textContent});
  }, true);
  document.addEventListener('compositionend', e => {
    if (enabled() && editable(e.target)) record('fill', e.target,
      secret(e.target) ? {secret:true} : {value:e.target.value ?? e.target.textContent});
  }, true);
  document.addEventListener('change', e => {
    if (!enabled()) return;
    if (e.target.matches('select')) record('select', e.target, {value:[...e.target.selectedOptions].map(o => o.value)});
    if (e.target.matches('input[type=checkbox],input[type=radio]')) record('check', e.target, {checked:e.target.checked});
    if (e.target.matches('input[type=file]')) binding({type:'warning', message:'文件选择需在导出脚本中手动补充 set_input_files，本地文件路径不自动收集'});
  }, true);
  for (const phase of ['keydown','keyup']) document.addEventListener(phase, e => {
    if (!enabled() || e.isComposing) return;
    const el = e.target;
    const sensitive = secret(el);
    // Password keys, key codes and characters never cross the browser boundary.
    record('key_event', el, {phase, key: sensitive ? '[REDACTED]' : e.key});
    if (phase !== 'keydown' || ['Shift','Control','Alt','Meta','Dead','Process'].includes(e.key)) return;
    const inInput = editable(el);
    const printable = e.key.length === 1;
    if (inInput && (printable && !e.ctrlKey && !e.metaKey || ['Backspace','Delete'].includes(e.key))) return;
    if (inInput && (e.ctrlKey || e.metaKey) && ['v','c','x','a'].includes(e.key.toLowerCase())) return;
    if (sensitive && printable) return;
    const modifiers = ['Control','Alt','Meta','Shift'].filter((m,i) => [e.ctrlKey,e.altKey,e.metaKey,e.shiftKey][i]);
    record('press', el, {key:[...modifiers, e.key === ' ' ? 'Space' : e.key].join('+')});
  }, true);
  document.addEventListener('mouseover', e => {
    clearTimeout(hoverTimer);
    const el = target(e);
    if (!el || el === document.body || el === document.documentElement) return;
    hoverTimer = setTimeout(() => { if (enabled() && !down) record('hover', el); }, 450);
  }, true);
  document.addEventListener('wheel', e => {
    if (!enabled()) return;
    const el = target(e);
    // Preserve wheel deltas, including horizontal scrolling, without storing text.
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? innerHeight : 1;
    record('wheel', el, {dx:e.deltaX*unit, dy:e.deltaY*unit});
  }, {capture:true, passive:true});
  window.__wtt = {selector, paused:false};
})();
