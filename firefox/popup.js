// Project links are kept together here so release maintainers can update them easily.
const REPORT_BUG_URL='https://github.com/CrowCosmos/StashLibrary/issues/new?template=bug_report.md';
const SUPPORT_PROJECT_URL='https://github.com/sponsors/CrowCosmos';
const root=document.querySelector('#root'), statusEl=document.querySelector('#status'), ctx=document.querySelector('#ctx'), settingsPanel=document.querySelector('#settingsPanel');
let tree=null, openPanels=[], clipboard=null, dragState=null, refreshTimer=null, dragHoverTimer=null, dragHoverPath=null;
let selectedPaths=new Set();
let selectionAnchorPath=null;
let keyboardTreePath='';
let statusHoldUntil=0, operationStarted=0, operationLabel='';
let operationActive=false, backgroundRefreshPending=false, refreshInFlight=null, silentNativeProgress=0;
let activityTimer=null;
let manualBackupActive=false;
let lastTreeSignature='', lastBackgroundRefreshAt=0;
const BACKGROUND_REFRESH_INTERVAL=5000;
const debugEvents=[];


// Keep the background script informed that this browser-action popup is open.
// The global StashLibrary command can then behave as a real toggle.
let stashlibraryPresencePort=null;
try{
  stashlibraryPresencePort=browser.runtime.connect({name:'stashlibrary-popup-presence'});
}catch(_){}

// Firefox can move keyboard focus out of an open extension popup when the
// active page tab changes (notably after Ctrl+W). Mouse clicks then appear to
// "unfreeze" StashLibrary because they return focus to the popup. Keep a neutral,
// invisible focus target on the popup body and restore it after tab switches.
try{document.body.tabIndex=-1}catch(_){}

function restoreStashLibraryKeyboardFocus(){
  try{
    debugRecord('focus-restore',{
      hidden:!!document.hidden,
      activeTag:document.activeElement?.tagName||'',
      activeId:document.activeElement?.id||'',
      activeClass:document.activeElement?.className||''
    });
    if(document.hidden)return;
    if(topVisibleOverlay?.())return;

    const active=document.activeElement;
    if(active && active.matches?.('input,textarea,select,[contenteditable="true"]'))return;

    try{window.focus()}catch(_){}
    try{document.body.focus({preventScroll:true})}catch(_){try{document.body.focus()}catch(__){}}
  }catch(_){}
}

function scheduleStashLibraryKeyboardFocusRestore(){
  // The new Firefox tab can become active a fraction after the removal event,
  // so retry briefly rather than relying on one timing-sensitive focus call.
  for(const delay of [0,40,120]){
    setTimeout(restoreStashLibraryKeyboardFocus,delay);
  }
}

try{
  browser.tabs.onActivated.addListener(()=>scheduleStashLibraryKeyboardFocusRestore());
  browser.tabs.onRemoved.addListener(()=>scheduleStashLibraryKeyboardFocusRestore());
}catch(_){}

window.addEventListener('focus',()=>setTimeout(restoreStashLibraryKeyboardFocus,0));
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden)scheduleStashLibraryKeyboardFocusRestore();
});

browser.runtime.onMessage.addListener(message=>{
  if(message?.type==='close-stashlibrary-popup'){
    try{window.close()}catch(_){}
  }
});

function stashlibraryClickFeedback(el,{working=false,success=false}={}){
  if(!el)return;
  el.classList.remove('stashlibrary-click-pop','action-working','action-success');
  if(working)el.classList.add('action-working');
  if(success){
    el.classList.add('action-success');
    setTimeout(()=>el.classList.remove('action-success'),220);
  }
}
function stashlibraryClearWorking(el){
  if(el)el.classList.remove('action-working');
}

function debugRecord(kind,data={}){try{debugEvents.push({time:new Date().toISOString(),kind,...data});if(debugEvents.length>250)debugEvents.splice(0,debugEvents.length-250)}catch{}}

const SAVE_AUDIT_KEY='stashlibrarySaveAudit';
const SAVE_AUDIT_LIMIT=120;

async function readSaveAudit(){
  try{
    const r=await browser.storage.local.get(SAVE_AUDIT_KEY);
    return Array.isArray(r?.[SAVE_AUDIT_KEY])?r[SAVE_AUDIT_KEY]:[];
  }catch(_){return []}
}

async function writeSaveAudit(entries){
  const trimmed=(entries||[]).slice(0,SAVE_AUDIT_LIMIT);
  try{await browser.storage.local.set({[SAVE_AUDIT_KEY]:trimmed})}catch(_){}
  return trimmed;
}

async function addSaveAudit(entry){
  const list=await readSaveAudit();
  const row={
    id:entry.id||`${Date.now()}-${Math.random().toString(16).slice(2)}`,
    time:new Date().toISOString(),
    status:'started',
    kind:'save',
    ...entry
  };
  list.unshift(row);
  await writeSaveAudit(list);
  return row.id;
}

async function updateSaveAudit(id,patch={}){
  const list=await readSaveAudit();
  const row=list.find(x=>x.id===id);
  if(row)Object.assign(row,patch,{updatedAt:new Date().toISOString()});
  await writeSaveAudit(list);
}

async function markStaleSaveAuditInterrupted(){
  const list=await readSaveAudit();
  let changed=false;
  for(const row of list){
    if(['started','capturing'].includes(row.status)){
      row.status='interrupted';
      row.updatedAt=new Date().toISOString();
      row.error=row.error||'The previous StashLibrary session ended before this save reported completion.';
      changed=true;
    }
  }
  if(changed)await writeSaveAudit(list);
}
function debugPayloadSummary(payload){const p={cmd:payload?.cmd||''};for(const k of ['path','src','dest','parent','destinationPath','kind','reason'])if(payload?.[k]!=null)p[k]=String(payload[k]);if(payload?.collectionIDs)p.collectionCount=payload.collectionIDs.length;if(payload?.attachmentIDs)p.attachmentCount=payload.attachmentIDs.length;if(payload?.directAttachmentIDs)p.directAttachmentCount=payload.directAttachmentIDs.length;return p}
const send=async(payload)=>{const started=Date.now();try{const r=await browser.runtime.sendMessage({type:'native-send',payload});debugRecord('native',{request:debugPayloadSummary(payload),ok:!!r?.ok,error:r?.error||'',ms:Date.now()-started});return r}catch(e){debugRecord('native-error',{request:debugPayloadSummary(payload),error:String(e?.message||e),ms:Date.now()-started});throw e}};

async function withHistoryGroup(label,fn){
  const b=await send({cmd:'history_group_begin',label});
  if(!b?.ok)throw new Error(b?.error||'Could not start grouped Undo history.');
  try{return await fn()}
  finally{
    const e=await send({cmd:'history_group_end'});
    if(!e?.ok)throw new Error(e?.error||'Could not finish grouped Undo history.');
  }
}

function friendlyStatusText(text,busy=false){
  const raw=String(text??'').trim();
  if(/checking stashlibrary helper|connecting to local helper|^starting\b|stashlibrary helper (?:is )?compatible/i.test(raw))return 'Loading…';
  if(/stashlibrary helper not detected/i.test(raw))return 'Setup required';
  const ready=raw.match(/^Ready\s*[—-]\s*(\d+)\s+items?/i);
  if(ready)return `Ready — ${ready[1]} item${ready[1]==='1'?'':'s'}`;
  if(/^Ready\b/i.test(raw))return 'Ready';
  if(busy){const label=raw.split(/\s+—\s+/)[0].trim();return /\d+(?:\.\d+)?%$/.test(label)?label:`${label||'Working'}…`}
  return raw.replace(/\s+—\s+\d+(?:\.\d+)?s$/,'');
}
function hideActivity(){clearTimeout(activityTimer);activityTimer=null;const panel=document.querySelector('#activityPanel');if(panel)panel.hidden=true}
function showActivity(title,detail=''){
  const update=()=>{const panel=document.querySelector('#activityPanel');if(!panel)return;document.querySelector('#activityTitle').textContent=String(title||'Working').replace(/…?$/,'…');const d=document.querySelector('#activityDetail');const friendlyDetail=/[a-z]:[\\/]/i.test(String(detail||''))?'Working with your StashLibrary library':String(detail||'');d.textContent=friendlyDetail;d.hidden=!friendlyDetail;panel.hidden=false};
  const panel=document.querySelector('#activityPanel');
  if(panel&&!panel.hidden){update();return}
  if(activityTimer)return;
  activityTimer=setTimeout(()=>{activityTimer=null;if(operationActive)update()},450);
}
function status(text,{hold=0,busy=false}={}){
  text=String(text??'');
  const visible=friendlyStatusText(text,busy);
  const isReady=/^Ready(?:\b|\s|—|-)/i.test(visible.trim());
  statusEl.textContent=visible;
  statusEl.classList.toggle('busy',!!busy&&!isReady);
  statusEl.title=text;
  debugRecord('status',{text,visible,busy:!!busy});
  if(hold) statusHoldUntil=Date.now()+hold;
}
function beginOperation(label,detail=''){
  operationStarted=Date.now(); operationLabel=label; operationActive=true;
  showActivity(label,detail);
  status(`${label}${detail?' — '+detail:''}`,{busy:true});
}
function _releaseOperationLock(){
  operationActive=false;
  hideActivity();
  if(backgroundRefreshPending) scheduleBackgroundRefresh(250);
}
function finishOperation(text){
  const secs=operationStarted?((Date.now()-operationStarted)/1000).toFixed(1):null;
  operationStarted=0; operationLabel='';
  status(`${text}${secs?` — ${secs}s`:''}`,{hold:1800});
  _releaseOperationLock();
}
function errorStatus(e){
  operationStarted=0;operationLabel='';
  const raw=String(e?.message||e);
  debugRecord('error',{error:raw,stack:String(e?.stack||'')});
  const componentProblem=/(?:StashLibrary|StashLibrary) Helper update required|(?:StashLibrary|StashLibrary) update incomplete|(?:StashLibrary|StashLibrary) Helper is not installed|(?:StashLibrary|StashLibrary) Helper is not installed or connected|(?:StashLibrary|StashLibrary) Helper is not installed or has disconnected/i.test(raw);
  if(componentProblem){
    status('StashLibrary update incomplete — the Firefox and Windows components do not match or the Windows integration is missing. Install the latest StashLibrary release to continue.',{hold:8000});
  }else{
    status(`Error — ${raw}`,{hold:5000});
  }
  _releaseOperationLock();
}

async function refreshHistoryState(state=null){
  try{
    const r=state||await send({cmd:'history_state'});if(!r?.ok&&state==null)return;
    const u=document.querySelector('#undoBtn'),d=document.querySelector('#redoBtn');
    u.disabled=!r.canUndo;d.disabled=!r.canRedo;
    u.title=r.canUndo?`Undo: ${r.undoLabel||'last change'}`:'Undo';d.title=r.canRedo?`Redo: ${r.redoLabel||'last change'}`:'Redo';
  }catch{}
}


const STASHLIBRARY_THUMBNAIL_KEY='stashlibraryBookmarkThumbnails';
let bookmarkThumbnailCache={};

async function loadBookmarkThumbnailCache(){
  try{
    const r=await browser.storage.local.get(STASHLIBRARY_THUMBNAIL_KEY);
    bookmarkThumbnailCache=r?.[STASHLIBRARY_THUMBNAIL_KEY]||{};
  }catch(_){bookmarkThumbnailCache={}}
}
function thumbnailKeyForNode(n){return String(n?.path||'')}
function applyBookmarkThumbnail(el,n){
  if(!el||n?.type!=='bookmark')return;
  const src=bookmarkThumbnailCache[thumbnailKeyForNode(n)];
  const img=el.querySelector('.bookmark-thumb img');
  const placeholder=el.querySelector('.bookmark-thumb-placeholder');

  if(src&&img){
    img.src=src;
    img.hidden=false;
    el.classList.add('has-thumbnail');
    if(placeholder){
      placeholder.classList.remove('fallback-pdf','fallback-web');
      placeholder.removeAttribute('title');
    }
    return;
  }

  if(img){
    img.hidden=true;
    img.removeAttribute('src');
  }
  el.classList.remove('has-thumbnail');

  // Neutral Firefox-inspired fallback icons:
  // PDF = small document icon; HTML/web = small globe.
  if(placeholder){
    placeholder.classList.remove('fallback-pdf');
    placeholder.classList.add('fallback-web');
    placeholder.title='Web page';
  }
}
function allBookmarkThumbnailJobs(){
  return flattenedTreeNodes(tree)
    .filter(n=>n.type==='bookmark')
    .map(n=>({
      path:n.path,
      key:thumbnailKeyForNode(n),
      name:n.name||'',
      sourceUrl:n.sourceUrl||''
    }));
}
async function runThumbnailGeneration(){
  const s=document.querySelector('#thumbnailGenerationStatus'),items=allBookmarkThumbnailJobs();
  if(s)s.textContent='Refreshing favicons — 0 of '+items.length+' websites checked';
  try{
    const r=await browser.runtime.sendMessage({type:'thumbnail-start-batch',items,force:true});
    if(!r?.ok)throw new Error(r?.error||'Favicon refresh failed.');
    if(s)s.textContent='Refreshing favicons in the background — you can close StashLibrary.';
  }catch(e){if(s)s.textContent='Favicon error — '+(e?.message||e)}
}

async function openLatestStashLibraryRelease(){
  try{await browser.tabs.create({url:STASHLIBRARY_LATEST_RELEASE_URL});}catch(_){ }
}
let activeRepairPoll=null;
function stopActiveRepairPoll(){
  if(activeRepairPoll){clearInterval(activeRepairPoll);activeRepairPoll=null;}
}
function setStashLibraryDialogMessage(message){
  const el=document.querySelector('#stashlibraryDialogMessage');
  if(el)el.textContent=message;
}
async function showBlockingComponentRepair({kind,title,message,confirmLabel,checkReady}){
  stopActiveRepairPoll();
  let checking=false;
  const promise=showStashLibraryDialog({
    title,
    message,
    confirmLabel,
    cancelLabel:'',
    blocking:true,
    onConfirm:async()=>{
      // Keep the repair surface open. Opening the release page may cause the
      // toolbar popup itself to close; if that happens the same repair surface
      // is shown again on the next open until health detection succeeds.
      setStashLibraryDialogMessage(`${message} The installer/download page has been opened. StashLibrary will close this message automatically once ${kind} is detected and compatible.`);
      await openLatestStashLibraryRelease();
    }
  });
  const poll=async()=>{
    if(checking)return;
    checking=true;
    try{
      if(await checkReady()){
        stopActiveRepairPoll();
        finishStashLibraryDialog(true);
      }
    }catch(_){ }
    checking=false;
  };
  activeRepairPoll=setInterval(poll,1500);
  poll().catch(()=>{});
  try{return await promise;}finally{stopActiveRepairPoll();}
}
async function showHelperReinstallPrompt(){
  return showBlockingComponentRepair({
    kind:'the Windows helper',
    title:'StashLibrary Helper not detected',
    message:'StashLibrary cannot detect a compatible Windows helper. Install or reinstall StashLibrary Windows Helper to continue.',
    confirmLabel:'Install StashLibrary Helper',
    checkReady:async()=>{
      const info=await send({cmd:'backup_info'});
      const version=String(info?.hostVersion||'').trim();
      return !!(info?.ok && version===REQUIRED_WINDOWS_HELPER_VERSION && Number(info?.protocolVersion||0)===STASHLIBRARY_PROTOCOL_VERSION);
    }
  });
}
async function showZoteroPluginReinstallPrompt(){
  return showBlockingComponentRepair({
    kind:'the Zotero plugin',
    title:'StashLibrary Zotero plugin not detected',
    message:'Zotero is running, but StashLibrary cannot detect a compatible StashLibrary Zotero plugin. Install or reinstall the plugin to continue. If Zotero asks to restart, restart it and leave this message open; StashLibrary will keep checking.',
    confirmLabel:'Install Zotero Plugin',
    checkReady:async()=>{
      const zr=await send({cmd:'zotero_status'});
      return !!(zr?.ok && zr.connected);
    }
  });
}
async function checkExistingInstallationComponents(){
  let info=null;
  try{info=await send({cmd:'backup_info'});}catch(_){ }
  const helperVersion=String(info?.hostVersion||'').trim();
  const helperPresent=!!(info?.ok&&helperVersion);
  const helperCompatible=!!(helperPresent && Number(info?.protocolVersion||0)===STASHLIBRARY_PROTOCOL_VERSION && helperVersion===REQUIRED_WINDOWS_HELPER_VERSION);
  if(!helperCompatible){
    await showHelperReinstallPrompt();
    return {helper:false,zotero:null};
  }
  let zr=null;
  try{zr=await send({cmd:'zotero_status'});}catch(_){ }
  // Warn only with positive evidence that Zotero itself is running/reachable.
  const zoteroRunning=!!zr?.zoteroReachable;
  // `connected` is the compatibility-aware signal. An older plugin may still
  // report a version string, but if it cannot connect to this StashLibrary build it
  // should be repaired just like a missing plugin — never sent back to Setup.
  const pluginDetected=!!zr?.connected;
  if(zoteroRunning&&!pluginDetected)await showZoteroPluginReinstallPrompt();
  return {helper:true,zotero:zoteroRunning?pluginDetected:null};
}

async function init(){
  browser.storage.local.remove('stashlibraryAutomaticUpdates').catch(()=>{});
  const [, , prefs]=await Promise.all([
    markStaleSaveAuditInterrupted(),
    loadBookmarkThumbnailCache(),
    browser.storage.local.get(['theme'])
  ]);
  const theme=prefs.theme||'system';
  applyTheme(theme);
  status('Loading…',{busy:true});
  const onboardingShown=await maybeShowOnboarding({startup:true});
  if(onboardingShown){
    status('Setup required — complete the steps below to start saving');
    return;
  }
  const componentState=await checkExistingInstallationComponents();
  if(!componentState.helper){
    status('StashLibrary Helper not detected',{hold:5000});
    return;
  }
  await refreshTree('startup');
}
function applyTheme(t){
  if(!['system','light','dark'].includes(t)) t='system';
  document.documentElement.dataset.theme=t;
  browser.storage.local.set({theme:t});
  document.querySelectorAll('[data-theme]').forEach(b=>b.classList.toggle('selected',b.dataset.theme===t));
}

function treeSignature(node){
  const rows=[];
  const walk=n=>{
    for(const c of (n?.children||[])){
      rows.push(`${c.type}|${c.path}|${c.name}|${c.sourceUrl||''}|${c.accessedAt||''}`);
      if(c.type==='folder')walk(c);
    }
  };
  walk(node);
  return rows.join('\n');
}

function scheduleBackgroundRefresh(delay=null){
  backgroundRefreshPending=true;
  if(refreshTimer)return;
  const elapsed=Date.now()-lastBackgroundRefreshAt;
  const wait=delay!=null?delay:Math.max(250,BACKGROUND_REFRESH_INTERVAL-elapsed);
  refreshTimer=setTimeout(async()=>{
    refreshTimer=null;
    if(operationActive){scheduleBackgroundRefresh(500);return}
    backgroundRefreshPending=false;
    await refreshTree('background');
  },wait);
}

async function refreshTree(reason='manual'){
  const isBackground=reason==='background';
  if(isBackground&&operationActive){backgroundRefreshPending=true;return}

  // Coalesce overlapping background reads. Explicit user-operation refreshes
  // are allowed to wait for the current read and then run once.
  if(refreshInFlight){
    if(isBackground){backgroundRefreshPending=true;return refreshInFlight}
    try{await refreshInFlight}catch{}
  }

  const run=(async()=>{
    const reopenPaths=reason==='change'?openPanels.map(p=>p?.dataset?.folder).filter(Boolean):[];
    try{
      if(reason==='startup') beginOperation('Loading StashLibrary','Opening your library');
      else if(reason==='manual') beginOperation('Refreshing','Updating your library');
      if(isBackground)silentNativeProgress++;
      const r=await send({cmd:'tree'});
      if(!r.ok) throw new Error(r.error);
      if(isBackground)lastBackgroundRefreshAt=Date.now();

      const sig=treeSignature(r.tree);
      const unchanged=!!lastTreeSignature&&sig===lastTreeSignature;
      lastTreeSignature=sig;

      // A passive refresh that found no actual catalogue change must not
      // rebuild DOM/selection state underneath the user.
      if(isBackground&&unchanged){
        debugRecord('background-refresh',{changed:false});
        return;
      }

      tree=r.tree;
      selectedPaths=new Set([...selectedPaths].filter(p=>findNodeByPath(p,tree)));
      renderRoot();
      const welcome=document.querySelector('#welcome');
      const configured=!!r.root;
      welcome.hidden=configured;
      document.querySelector('#settingsBtn').classList.toggle('attention',!configured);
      if(reopenPaths.length) restoreOpenFolders(reopenPaths);
      refreshHistoryState().catch(()=>{});
      const count=countNodes(tree);
      debugRecord(isBackground?'background-refresh':'tree-refresh',{changed:true,reason,count});

      if(reason==='startup'||reason==='manual') finishOperation(`Ready — ${count} item${count===1?'':'s'}`);
      else if(!isBackground&&Date.now()>statusHoldUntil) status(`Ready — ${count} item${count===1?'':'s'}`);
    }catch(e){
      if(isBackground){
        debugRecord('background-refresh-error',{error:String(e?.message||e)});
        return;
      }
      // The helper can disappear while StashLibrary is already open (manual
      // uninstall, failed update, registry change, etc.). Re-run the component
      // gate before showing a dead-end error so recoverable companion problems
      // jump back into the appropriate Setup page.
      const prefs=await browser.storage.local.get([STASHLIBRARY_ONBOARDING_KEY]).catch(()=>({}));
      if(prefs?.[STASHLIBRARY_ONBOARDING_KEY]){
        await showHelperReinstallPrompt();
        status('StashLibrary Helper not detected',{hold:5000});
        return;
      }
      errorStatus(e);
      root.innerHTML='<div class="empty">StashLibrary could not open your library. Open Settings or Check & Repair Library for help.</div>';
    }finally{
      if(isBackground)silentNativeProgress=Math.max(0,silentNativeProgress-1);
    }
  })();

  refreshInFlight=run;
  try{return await run}
  finally{if(refreshInFlight===run)refreshInFlight=null}
}

function countNodes(node){
  // The main status bar reports saved StashLibrary items, not organisational folders.
  return (node?.children||[]).reduce((n,c)=>n+(c.type==='bookmark'?1:0)+(c.type==='folder'?countNodes(c):0),0)
}
function selectedNodes(){return [...selectedPaths].map(p=>findNodeByPath(p,tree)).filter(Boolean)}
function topLevelSelectedNodes(){
  const nodes=selectedNodes();const chosen=new Set(nodes.map(n=>n.path));
  return nodes.filter(n=>!nodes.some(parent=>parent.type==='folder'&&parent.path!==n.path&&isDescendantPath(n.path,parent.path)&&chosen.has(parent.path)))
}
function isDescendantPath(path,parent){
  const norm=x=>String(x||'').replace(/\\/g,'/').replace(/\/+$/,'');
  path=norm(path);parent=norm(parent);return path.startsWith(parent+'/');
}
function selectionForNode(n){
  if(!n)return [];
  if(selectedPaths.has(n.path))return topLevelSelectedNodes();
  return [n];
}
function clearSelection({quiet=false,keepAnchor=false}={}){
  selectedPaths.clear();
  document.querySelectorAll('.item.selected').forEach(el=>el.classList.remove('selected'));
  if(!keepAnchor)selectionAnchorPath=null;
  updateSelectionUI(quiet);
}
function syncSelectionClasses(){
  document.querySelectorAll('.item[data-path]').forEach(el=>{
    el.classList.toggle('selected',selectedPaths.has(el.dataset.path));
  });
}
function toggleSelection(n,el){
  if(!n)return;
  if(selectedPaths.has(n.path))selectedPaths.delete(n.path);else selectedPaths.add(n.path);
  selectionAnchorPath=n.path;
  document.querySelectorAll(`.item[data-path="${CSS.escape(n.path)}"]`).forEach(x=>x.classList.toggle('selected',selectedPaths.has(n.path)));
  updateSelectionUI();
}
function flattenedTreeNodes(rootNode){
  const out=[];
  const walk=node=>{
    for(const child of node?.children||[]){
      out.push(child);
      if(child.type==='folder')walk(child);
    }
  };
  walk(rootNode);
  return out;
}
function selectRangeTo(n,{additive=false}={}){
  if(!n)return false;
  const anchorPath=selectionAnchorPath||[...selectedPaths].at(-1);
  if(!anchorPath)return false;
  const all=flattenedTreeNodes(tree);
  const a=all.findIndex(x=>x.path===anchorPath);
  const b=all.findIndex(x=>x.path===n.path);
  if(a<0||b<0)return false;
  if(!additive)selectedPaths.clear();
  const lo=Math.min(a,b),hi=Math.max(a,b);
  for(let i=lo;i<=hi;i++)selectedPaths.add(all[i].path);
  syncSelectionClasses();
  updateSelectionUI();
  return true;
}
function selectAllTreeItems(){
  const all=flattenedTreeNodes(tree);
  selectedPaths=new Set(all.map(n=>n.path));
  selectionAnchorPath=all.length?all[0].path:null;
  syncSelectionClasses();
  updateSelectionUI();
}
function updateSelectionUI(quiet=false){
  const nodes=selectedNodes();const hint=document.querySelector('#selectionHint');
  if(hint)hint.hidden=nodes.length===0;
  if(!quiet&&nodes.length){const files=nodes.filter(n=>n.type==='bookmark').length,folders=nodes.length-files;status(`${nodes.length} selected${files?` · ${files} bookmark${files===1?'':'s'}`:''}${folders?` · ${folders} folder${folders===1?'':'s'}`:''} — Shift+click to select a range · Ctrl+Shift+click to add a range`,{hold:1800})}
}
function markSelected(el,n){el.classList.toggle('selected',selectedPaths.has(n.path))}
function renderRoot(){
  const started=performance.now();
  root?.classList?.remove('arrows-suppressed');
  collapseAll();
  const kids=tree?.children||[];
  if(!kids.length){
    keyboardTreePath='';
    root.innerHTML='<div class="empty">(empty)</div>';
    debugRecord('tree-render',{count:0,ms:Math.round((performance.now()-started)*10)/10});
    return;
  }

  // Build the entire root list off-DOM and attach it once. Appending hundreds
  // of bookmark rows directly to the live popup caused repeated layout work
  // and made larger libraries feel as though the catalogue was hanging.
  const fragment=document.createDocumentFragment();
  kids.forEach(n=>fragment.append(makeItem(n,0,tree.path)));
  root.replaceChildren(fragment);
  updateSelectionUI(true);
  if(keyboardTreePath){
    const el=itemElementByPath(keyboardTreePath);
    if(el)setKeyboardCurrent(el,{scroll:false});
  }
  debugRecord('tree-render',{count:kids.length,ms:Math.round((performance.now()-started)*10)/10});
}

function itemElementByPath(path){
  return [...document.querySelectorAll('.item')].find(el=>el.dataset.path===path)||null;
}
function restoreOpenFolders(paths){
  requestAnimationFrame(()=>{
    paths.forEach((path,i)=>{
      const node=findNodeByPath(path,tree), anchor=itemElementByPath(path);
      if(!node||node.type!=='folder'||!anchor)return;
      openFolder(anchor,node,i+1);
    });
  });
}

function clearDropMarks(){document.querySelectorAll('.drop-before,.drop-after,.drop-inside,.drop-folder-end').forEach(x=>x.classList.remove('drop-before','drop-after','drop-inside','drop-folder-end'))}
function dropModeFor(el,n,e){
  const r=el.getBoundingClientRect(), y=(e.clientY-r.top)/r.height;
  if(n.type==='folder' && y>=0.27 && y<=0.73) return 'inside';
  return y<0.5?'before':'after';
}

const bookmarkUrlPreview=document.querySelector('#bookmarkUrlPreview');
const bookmarkUrlPreviewCache=new Map();
let bookmarkUrlPreviewRequest=0;

function clearBookmarkUrlPreview(){
  if(bookmarkUrlPreview)bookmarkUrlPreview.textContent='';
}

function readablePreviewAddress(value){
  let text=String(value||'').trim();
  if(!text)return '';
  try{
    // Display-only decoding, similar to a browser address field: keep the real
    // URL untouched for opening, but remove percent-encoding from what users see.
    text=decodeURIComponent(text);
  }catch(_){
    try{text=decodeURI(text)}catch(__){}
  }
  if(/^file:\/\/\//i.test(text)){
    let p=text.replace(/^file:\/\/\//i,'');
    if(/^[A-Za-z]:\//.test(p))p=p.replace(/\//g,'\\');
    return p;
  }
  return text;
}

async function showBookmarkUrlPreview(n){
  if(!bookmarkUrlPreview || !n || n.type!=='bookmark'){
    clearBookmarkUrlPreview();
    return;
  }

  const key=String(n.path||'');
  if(!key){
    clearBookmarkUrlPreview();
    return;
  }

  const cached=bookmarkUrlPreviewCache.get(key);
  if(cached){
    bookmarkUrlPreview.textContent=cached;
    return;
  }

  const request=++bookmarkUrlPreviewRequest;
  bookmarkUrlPreview.textContent='';

  try{
    // open_alias only registers StashLibrary's local /view/ route and returns the
    // exact localhost URL. It does not open a new Firefox tab.
    const r=await send({cmd:'open_alias',path:n.path,displayName:n.name||''});
    if(request!==bookmarkUrlPreviewRequest)return;
    if(r?.ok && r.url){
      const readable=readablePreviewAddress(r.url);
      bookmarkUrlPreviewCache.set(key,readable);
      bookmarkUrlPreview.textContent=readable;
    }else{
      clearBookmarkUrlPreview();
    }
  }catch(_){
    if(request===bookmarkUrlPreviewRequest)clearBookmarkUrlPreview();
  }
}

function restoreKeyboardBookmarkUrlPreview(){
  const el=document.querySelector('.item.bookmark.keyboard-current');
  const n=el?.dataset?.path ? findNodeByPath(el.dataset.path,tree) : null;
  if(n)showBookmarkUrlPreview(n);
  else clearBookmarkUrlPreview();
}

function makeItem(n,depth,parentPath){
  const el=document.createElement('div');
  el.className='item '+(n.type==='folder'?'folder':'bookmark'); el.draggable=true;
  el.dataset.path=n.path; el.dataset.parent=parentPath||'';
  const folderDir=n.type==='folder'?expectedChildDirection(el,depth+1):0;
  const addArrow=(direction,glyph)=>{
    const arrow=document.createElement('span');
    arrow.className=`arrow arrow-${direction}`;
    arrow.setAttribute('aria-hidden','true');
    arrow.textContent=glyph;
    el.appendChild(arrow);
  };
  if(n.type==='folder'&&folderDir===-1)addArrow('left','◀');
  if(n.type==='folder'){
    const icon=document.createElement('span');
    icon.className='folder-icon';
    icon.setAttribute('aria-hidden','true');
    el.appendChild(icon);
  }else{
    const thumb=document.createElement('span');
    thumb.className='bookmark-thumb';
    thumb.setAttribute('aria-hidden','true');
    const img=document.createElement('img');
    img.hidden=true;
    const placeholder=document.createElement('span');
    placeholder.className='bookmark-thumb-placeholder';
    thumb.append(img,placeholder);
    el.appendChild(thumb);
  }
  const label=document.createElement('span');
  label.className='label';
  label.textContent=n.name;
  el.appendChild(label);
  if(n.type==='folder'&&folderDir!==-1)addArrow('right','▶');
  if(n.type==='bookmark')applyBookmarkThumbnail(el,n);
  markSelected(el,n);
  el.addEventListener('mouseenter',()=>{
    if(n.type==='folder'){
      clearBookmarkUrlPreview();
      openFolder(el,n,depth+1);
    }else{
      showBookmarkUrlPreview(n);
      closeFrom(depth+1);
      document.querySelectorAll('.item.active').forEach(x=>x.classList.remove('active'));
    }
  });
  el.addEventListener('mouseleave',()=>{
    if(n.type==='bookmark')restoreKeyboardBookmarkUrlPreview();
  });
  el.addEventListener('click',e=>{
    if(dragState)return;
    if(e.shiftKey){
      e.preventDefault();e.stopPropagation();
      if(selectionAnchorPath||selectedPaths.size)selectRangeTo(n,{additive:e.ctrlKey||e.metaKey});
      else toggleSelection(n,el);
      return;
    }
    if(e.ctrlKey||e.metaKey){
      e.preventDefault();e.stopPropagation();
      toggleSelection(n,el);
      return;
    }
    if(selectedPaths.size)clearSelection({quiet:true});
    selectionAnchorPath=n.path;
    if(n.type!=='folder'){e.preventDefault();openBookmark(n)}
  });

  el.addEventListener('dragstart',e=>{
    clearTimeout(dragHoverTimer);dragHoverTimer=null;dragHoverPath=null;
    const group=selectedPaths.has(n.path)?topLevelSelectedNodes():[n];
    dragState={path:n.path,parent:parentPath,name:n.name,type:n.type,paths:group.map(x=>x.path),names:group.map(x=>x.name),multi:group.length>1};
    group.forEach(x=>document.querySelectorAll(`.item[data-path="${CSS.escape(x.path)}"]`).forEach(k=>k.classList.add('dragging')));
    e.dataTransfer.effectAllowed='move';
    try{e.dataTransfer.setData('text/plain',n.path)}catch{}
    status(dragState.multi?`Dragging ${dragState.paths.length} selected items — drop in the centre of a folder to move them together`:`Dragging “${n.name}” — drop above/below to reorder, or centre of a folder to move inside`,{busy:true});
  });
  el.addEventListener('dragend',()=>{clearTimeout(dragHoverTimer);dragHoverTimer=null;dragHoverPath=null;document.querySelectorAll('.item.dragging').forEach(x=>x.classList.remove('dragging'));clearDropMarks();dragState=null;if(Date.now()>statusHoldUntil)status('Ready')});
  el.addEventListener('dragover',e=>{
    if(!dragState||dragState.path===n.path)return;
    e.preventDefault();e.stopPropagation();clearDropMarks();
    const mode=dropModeFor(el,n,e);el.classList.add('drop-'+mode);
    e.dataTransfer.dropEffect='move';
    if(n.type==='folder'&&mode==='inside'){
      if(dragHoverPath!==n.path){
        clearTimeout(dragHoverTimer);dragHoverPath=n.path;
        dragHoverTimer=setTimeout(()=>{
          if(dragState&&dragHoverPath===n.path){openFolder(el,n,depth+1);status(`Opened “${n.name}” — keep dragging into a subfolder or drop here`,{busy:true})}
        },420);
      }
    }else if(dragHoverPath===n.path){
      clearTimeout(dragHoverTimer);dragHoverTimer=null;dragHoverPath=null;
    }
    const words=mode==='inside'?`Move into “${n.name}” — hover to open`:`Place ${mode} “${n.name}”`;
    status(`Drop: ${words}`,{busy:true});
  });
  el.addEventListener('dragleave',e=>{
    if(!el.contains(e.relatedTarget)){
      el.classList.remove('drop-before','drop-after','drop-inside');
      if(dragHoverPath===n.path){clearTimeout(dragHoverTimer);dragHoverTimer=null;dragHoverPath=null}
    }
  });
  el.addEventListener('drop',async e=>{
    if(!dragState||dragState.path===n.path)return;
    e.preventDefault();e.stopPropagation();
    const mode=dropModeFor(el,n,e); clearDropMarks();clearTimeout(dragHoverTimer);dragHoverTimer=null;dragHoverPath=null;
    const dstate=dragState, src=dstate.path, srcParent=dstate.parent; dragState=null;
    try{
      if(mode==='inside'&&n.type==='folder'){
        const paths=(dstate?.paths?.length?dstate.paths:[src]).filter(p=>p!==n.path&&!isDescendantPath(n.path,p));
        beginOperation('Moving',paths.length>1?`${paths.length} selected items into “${n.name}”`:`into “${n.name}”`);
        await withHistoryGroup(`Move ${paths.length} items`,async()=>{for(const pth of paths){const r=await send({cmd:'move',src:pth,dest:n.path});if(!r.ok)throw new Error(r.error)}})
        clearSelection({quiet:true});await refreshTree('change'); finishOperation(paths.length>1?`Moved ${paths.length} items into “${n.name}”`:`Moved into “${n.name}”`);
      }else{
        const destParent=parentPath;
        if(srcParent===destParent){
          beginOperation('Reordering',`${mode} “${n.name}”`);
          const r=await send({cmd:'reorder',src,parent:destParent,target:n.path,position:mode}); if(!r.ok)throw new Error(r.error);
          await refreshTree('change'); finishOperation(`Reordered ${mode} “${n.name}”`);
        }else{
          beginOperation('Moving',`to ${PathLabel(destParent)}, ${mode} “${n.name}”`);
          const r=await send({cmd:'move',src,dest:destParent,target:n.path,position:mode}); if(!r.ok)throw new Error(r.error);
          await refreshTree('change'); finishOperation(`Moved and placed ${mode} “${n.name}”`);
        }
      }
    }catch(err){errorStatus(err)}
  });
  return el;
}
function PathLabel(p){if(!p)return 'folder';return p.split(/[\\/]/).filter(Boolean).pop()||'folder'}
function formatBackupTime(path){
  const name=PathLabel(path||'');
  const m=/^(?:StashLibrary|StashLibrary)-backup-(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(\d{2})/.exec(name);
  if(!m)return path?name:'Never';
  const d=new Date(Number(m[1]),Number(m[2])-1,Number(m[3]),Number(m[4]),Number(m[5]),Number(m[6]));
  return Number.isNaN(d.getTime())?name:d.toLocaleString();
}
function PathParent(p){const s=String(p||'').replace(/\\/g,'/');const i=s.lastIndexOf('/');return i>0?s.slice(0,i):''}

async function getCurrentTab(){
  const tabs=await browser.tabs.query({active:true,currentWindow:true});
  const tab=tabs&&tabs[0];
  if(!tab||!tab.url) throw new Error('Could not read the current Firefox tab.');
  return {id:tab.id,url:tab.url,title:(tab.title||'').trim()};
}

function bindRootDrop(){
  root.addEventListener('dragover',e=>{
    if(!dragState||e.target.closest('.item'))return;
    e.preventDefault();e.stopPropagation();root.classList.add('drop-folder-end');
    status('Drop: move to end of root folder',{busy:true});
  });
  root.addEventListener('dragleave',e=>{if(!root.contains(e.relatedTarget))root.classList.remove('drop-folder-end')});
  root.addEventListener('drop',async e=>{
    if(!dragState||e.target.closest('.item'))return;
    e.preventDefault();e.stopPropagation();root.classList.remove('drop-folder-end');
    const d=dragState;dragState=null;const dest=tree?.path;if(!dest)return;
    try{
      if(d.multi){
        const paths=(d.paths||[d.path]).filter(Boolean);beginOperation('Moving',`${paths.length} selected items to root folder`);
        await withHistoryGroup(`Move ${paths.length} items`,async()=>{for(const pth of paths){const node=findNodeByPath(pth,tree);const parent=node?PathParent(node.path):'';if(parent===dest){const x=await send({cmd:'reorder',src:pth,parent:dest,position:'end'});if(!x.ok)throw new Error(x.error)}else{const x=await send({cmd:'move',src:pth,dest,position:'end'});if(!x.ok)throw new Error(x.error)}}})
        clearSelection({quiet:true});await refreshTree('change');finishOperation(`Moved ${paths.length} items to root folder`);return;
      }
      if(d.parent===dest){
        beginOperation('Reordering','moving to end of root');
        const x=await send({cmd:'reorder',src:d.path,parent:dest,position:'end'});if(!x.ok)throw new Error(x.error);
        await refreshTree('change');finishOperation('Moved to end of root');
      }else{
        beginOperation('Moving','to root folder');
        const x=await send({cmd:'move',src:d.path,dest,position:'end'});if(!x.ok)throw new Error(x.error);
        await refreshTree('change');finishOperation('Moved to root folder');
      }
    }catch(err){errorStatus(err)}
  });
}
bindRootDrop();

// Clicking blank space in the main bookmark panel clears any keyboard cursor/highlight.
root.addEventListener('click',e=>{
  if(e.target!==root)return;
  clearKeyboardCurrent(document);
  document.querySelectorAll('.item.keyboard-current').forEach(el=>el.classList.remove('keyboard-current'));
  // Also clear ordinary multi-selection so blank space behaves like a true deselect area.
  clearSelection({quiet:true});
  updateSelectionUI(true);
});


function refreshArrowColumns(){
  const panels=[root,...openPanels.filter(Boolean)];

  // Every column except the deepest open one loses its arrows.
  panels.forEach(panel=>panel?.classList?.add('arrows-suppressed'));
  const deepest=panels[panels.length-1];
  deepest?.classList?.remove('arrows-suppressed');
}

function closeFrom(depth){
  while(openPanels.length>=depth){openPanels.pop()?.remove()}
  refreshArrowColumns();
}
function collapseAll(){
  openPanels.forEach(p=>p.remove());
  openPanels=[];
  document.querySelectorAll('.item.active').forEach(x=>x.classList.remove('active'));
  refreshArrowColumns();
}
function openFolder(anchor,n,depth){
  closeFrom(depth); document.querySelectorAll('.item.active').forEach(x=>x.classList.remove('active')); anchor.classList.add('active');
  const p=document.createElement('div');p.className='flyout';p.dataset.folder=n.path;
  const kids=n.children||[];
  if(!kids.length){
    p.innerHTML='<div class="empty">(empty)</div>';
  }else{
    kids.forEach(k=>p.append(makeItem(k,depth,n.path)));

    const directBookmarks=kids.filter(k=>k.type==='bookmark');
    if(directBookmarks.length>1){
      const openAll=document.createElement('button');
      openAll.type='button';
      openAll.className='flyout-open-all';
      openAll.textContent='Open All Bookmarks';
      openAll.title=`Open all ${directBookmarks.length} bookmarks in this folder`;
      openAll.addEventListener('mouseenter',e=>e.stopPropagation());
      openAll.addEventListener('click',e=>{
        e.preventDefault();
        e.stopPropagation();
        openBookmarks(directBookmarks);
      });
      p.append(openAll);
    }
  }
  document.body.append(p);
  p.addEventListener('dragover',e=>{if(!dragState||e.target.closest('.item'))return;e.preventDefault();e.stopPropagation();p.classList.add('drop-folder-end');status(`Drop: move to end of “${n.name}”`,{busy:true})});
  p.addEventListener('dragleave',e=>{if(!p.contains(e.relatedTarget))p.classList.remove('drop-folder-end')});
  p.addEventListener('drop',async e=>{if(!dragState||e.target.closest('.item'))return;e.preventDefault();e.stopPropagation();p.classList.remove('drop-folder-end');const d=dragState;dragState=null;try{if(d.multi){const paths=(d.paths||[d.path]).filter(pth=>pth!==n.path&&!isDescendantPath(n.path,pth));beginOperation('Moving',`${paths.length} selected items into “${n.name}”`);await withHistoryGroup(`Move ${paths.length} items`,async()=>{for(const pth of paths){const node=findNodeByPath(pth,tree);const parent=node?PathParent(node.path):'';const r=await send({cmd:parent===n.path?'reorder':'move',src:pth,...(parent===n.path?{parent:n.path,position:'end'}:{dest:n.path,position:'end'})});if(!r.ok)throw new Error(r.error)}});clearSelection({quiet:true});await refreshTree('change');finishOperation(`Moved ${paths.length} items into “${n.name}”`)}else if(d.parent===n.path){beginOperation('Reordering',`moving to end of “${n.name}”`);const r=await send({cmd:'reorder',src:d.path,parent:n.path,position:'end'});if(!r.ok)throw new Error(r.error);await refreshTree('change');finishOperation('Moved to end')}else{beginOperation('Moving',`into “${n.name}”`);const r=await send({cmd:'move',src:d.path,dest:n.path,position:'end'});if(!r.ok)throw new Error(r.error);await refreshTree('change');finishOperation(`Moved into “${n.name}”`)}}catch(err){errorStatus(err)}});
  const ar=anchor.getBoundingClientRect(), w=210, margin=5;
  const dir=expectedChildDirection(anchor,depth);

  // Physical flyout placement follows the exact same paired direction pattern
  // as keyboard navigation: L,L,R,R,L,L,R,R...
  let x=dir===-1 ? ar.left-w+1 : ar.right-1;

  // Keep the panel inside the extension window without changing its logical direction.
  x=Math.max(margin,Math.min(x,innerWidth-w-margin));

  let y=Math.min(ar.top,innerHeight-p.offsetHeight-margin);
  y=Math.max(20,y);
  p.style.left=x+'px';
  p.style.top=y+'px';
  openPanels[depth-1]=p;
  refreshArrowColumns();
}

function stashlibraryViewBasenameFromUrl(url){
  try{
    const u=new URL(url);
    if(u.hostname!=='127.0.0.1')return '';
    if(!u.pathname.startsWith('/view/'))return '';
    return decodeURIComponent(u.pathname.slice('/view/'.length));
  }catch(_){
    return '';
  }
}

async function closeOldPhysicalFileTabs(oldPhysicalName){
  const wanted=String(oldPhysicalName||'').trim();
  if(!wanted)return 0;

  const tabs=await browser.tabs.query({});
  const stale=tabs.filter(t=>stashlibraryViewBasenameFromUrl(t.url||'')===wanted);
  const ids=stale.map(t=>t.id).filter(id=>Number.isInteger(id));

  if(ids.length)await browser.tabs.remove(ids);
  return ids.length;
}

async function openBookmark(n,{newTab=true,active=true}={}){
 try{
  beginOperation('Opening',`“${n.name}”`);
  const r=await send({cmd:'open',path:n.path,displayName:n.name});
  if(!r.ok)throw new Error(r.error);
  if(r.url){
    if(newTab){
      await browser.tabs.create({url:r.url,active});
    }else{
      const tabs=await browser.tabs.query({active:true,currentWindow:true});
      const current=tabs?.[0];
      if(current?.id!=null)await browser.tabs.update(current.id,{url:r.url});
      else await browser.tabs.create({url:r.url,active:true});
    }
  }
  finishOperation(newTab&&!active?`Opened “${n.name}” in a background tab`:`Opened “${n.name}”`);
 }catch(e){errorStatus(e)}
}

async function openBookmarks(items){
  const bookmarks=(items||[]).filter(item=>item?.type==='bookmark');
  if(!bookmarks.length)return;

  if(bookmarks.length===1){
    await openBookmark(bookmarks[0]);
    return;
  }

  try{
    beginOperation('Opening bookmarks',`${bookmarks.length} bookmarks`);
    const openedTabs=[];

    // Open all tabs in the background first so activating the first one
    // cannot close the popup before the rest have been created.
    for(const item of bookmarks){
      const r=await send({cmd:'open',path:item.path,displayName:item.name});
      if(!r.ok)throw new Error(r.error);
      if(r.url){
        const tab=await browser.tabs.create({url:r.url,active:false});
        if(tab?.id!=null)openedTabs.push(tab.id);
      }
    }

    finishOperation(`Opened ${bookmarks.length} bookmarks`);

    if(openedTabs.length){
      try{await browser.tabs.update(openedTabs[0],{active:true})}catch(_){}
    }
  }catch(e){
    errorStatus(e);
  }
}

function showCtx(x,y,n,targetFolder){
  ctx.innerHTML='';
  const chosen=selectionForNode(n),count=chosen.length;
  const one=chosen.length===1?chosen[0]:null;
  const renameLabel=one?.type==='folder'?'Rename Folder':'Rename Bookmark';
  const renameActions=[[renameLabel,'rename']];
  if(one?.type==='bookmark')renameActions.push(['Rename Physical File…','renamePhysical']);
  const remainingActions=[
    [count>1?`Delete ${count} Items`:'Delete','delete'],
    [count>1?`Cut ${count} Items`:'Cut','cut'],
    [count>1?`Copy ${count} Items`:'Copy','copy'],
    ['Paste','paste']
  ];
  const groups=[
    renameActions,
    [['New Folder','addFolder']],
    remainingActions
  ];
  groups.forEach((group,gi)=>{
    if(gi){const sep=document.createElement('div');sep.className='ctx-separator';ctx.append(sep)}
    for(const [label,a] of group){
      const b=document.createElement('button');b.textContent=label;if(count>1&&['delete','cut','copy'].includes(a))b.classList.add('batch-label');
      const needsItem=['cut','copy','rename','renamePhysical','delete'].includes(a),needsClipboard=a==='paste';
      b.disabled=
        (needsItem&&!count) ||
        (a==='rename'&&count!==1) ||
        (a==='renamePhysical'&&(count!==1||chosen[0]?.type!=='bookmark')) ||
        (needsClipboard&&!clipboard);
      b.onclick=()=>{if(!b.disabled)doAction(a,n,targetFolder,chosen)};ctx.append(b);
    }
  });
  ctx.hidden=false;const r=ctx.getBoundingClientRect(),margin=4;ctx.style.left=Math.max(margin,Math.min(x,innerWidth-r.width-margin))+'px';ctx.style.top=Math.max(20,Math.min(y,innerHeight-r.height-margin))+'px';
}

function contextMenuButtons(){
  if(ctx.hidden)return [];
  return [...ctx.querySelectorAll(':scope > button')].filter(b=>b.offsetParent!==null && !b.disabled);
}

function selectContextMenuButton(index){
  const buttons=contextMenuButtons();
  if(!buttons.length)return false;
  const i=Math.max(0,Math.min(buttons.length-1,index));
  return setKeyboardCurrent(buttons[i],{focus:true,scroll:true});
}

function moveContextMenuSelection(direction){
  const buttons=contextMenuButtons();
  if(!buttons.length)return false;
  const current=buttons.find(b=>b.classList.contains('keyboard-current')) ||
                buttons.find(b=>b===document.activeElement);
  let i=current?buttons.indexOf(current):-1;
  if(i<0)i=direction>0?-1:buttons.length;
  i=Math.max(0,Math.min(buttons.length-1,i+direction));
  return setKeyboardCurrent(buttons[i],{focus:true,scroll:true});
}

function openKeyboardContextMenu(){
  if(!ctx.hidden)return selectContextMenuButton(0);

  let item=currentKeyboardItem();
  if(!item){
    const first=root.querySelector(':scope > .item');
    if(!first)return false;
    keyboardSelectItem(first,{autoExpand:false});
    item=first;
  }

  const node=nodeForItem(item);
  if(!node)return false;
  const folder=node.type==='folder'?node.path:(item.dataset.parent||PathParent(node.path));
  const rect=item.getBoundingClientRect();

  // Open beside the currently highlighted bookmark/folder, exactly like a
  // mouse right-click, then put the keyboard cursor on the first valid action.
  showCtx(rect.right-4,Math.max(20,rect.top+2),node,folder);
  return selectContextMenuButton(0);
}

function closeKeyboardContextMenu(){
  if(ctx.hidden)return false;
  ctx.hidden=true;
  const item=currentKeyboardItem();
  if(item)setKeyboardCurrent(item,{focus:false,scroll:false});
  return true;
}

function contextTarget(e){const item=e.target.closest('.item');if(item){const node=findNodeByPath(item.dataset.path,tree);const folder=node?.type==='folder'?node.path:item.dataset.parent;return{node,folder}}const fly=e.target.closest('.flyout');if(fly)return{node:null,folder:fly.dataset.folder||tree?.path};if(e.target.closest('#root')||e.target.closest('.empty')||e.target===document.body||e.target===document.documentElement)return{node:null,folder:tree?.path};return null}
function findNodeByPath(path,node){if(!node||!path)return null;if(node.path===path)return node;for(const child of node.children||[]){const found=findNodeByPath(path,child);if(found)return found}return null}
async function doAction(a,n,folder,chosen=null){ctx.hidden=true;try{
 const items=(chosen&&chosen.length?chosen:selectionForNode(n));
 if(a==='openBookmarks'&&items.length){
   const bookmarks=topLevelFromNodes(items).filter(item=>item.type==='bookmark');
   if(bookmarks.length!==topLevelFromNodes(items).length)throw new Error('Only bookmarks can be opened together.');
   await openBookmarks(bookmarks);
 }
 if(a==='openFolderBookmarks'&&items.length===1){
   const folderItem=items[0];
   if(folderItem.type!=='folder')throw new Error('Choose one folder first.');
   const bookmarks=(folderItem.children||[]).filter(item=>item.type==='bookmark');
   if(!bookmarks.length)throw new Error('This folder has no bookmarks to open.');
   await openBookmarks(bookmarks);
 }
 if(a==='openFileLocation'){
   const item=items[0];
   if(!item)throw new Error('Choose one bookmark or folder first.');
   beginOperation('Opening file location',`“${item.name}”`);
   const r=await send({cmd:'open_file_location',path:item.path});
   if(!r.ok)throw new Error(r.error);
   finishOperation(item.type==='folder'?'Opened StashLibrary archive folder':`Opened location of “${item.name}”`);
 }
 if(a==='addFolder'){const name=await stashlibraryPrompt({title:'New Folder',message:'Enter a name for the new folder.',inputLabel:'Folder name',confirmLabel:'Create'});if(name){beginOperation('Creating folder',`“${name}”`);const r=await send({cmd:'mkdir',parent:folder,name});if(!r.ok)throw new Error(r.error);await refreshTree('change');finishOperation(`Created folder “${name}”`)}}
 if(a==='sendZotero'&&items.length){
   const use=topLevelFromNodes(items);
   const bookmarks=use.filter(item=>item.type==='bookmark');
   if(!bookmarks.length)throw new Error('Choose one or more bookmarks first.');
   if(bookmarks.length!==use.length)throw new Error('Only bookmarks can be sent to Zotero.');
   if(bookmarks.length===1){
     const item=bookmarks[0];
     await openZoteroDestinationPicker({kind:'bookmark',path:item.path,name:item.name});
   }else{
     await openZoteroDestinationPicker({kind:'bookmarks',items:bookmarks.map(item=>({path:item.path,name:item.name}))});
   }
 }
 if(a==='addBookmark'){
   beginOperation('Adding current tab','reading active Firefox tab');
   const tab=await getCurrentTab();
   if(!/^(https?:|file:)/i.test(tab.url)) throw new Error(`This Firefox page cannot be archived: ${tab.url.split(':')[0]}: pages are protected by Firefox.`);
   let r;
   if(/^https?:/i.test(tab.url)){
     status(`Saving current tab — checking for a hosted PDF — ${tab.title||tab.url}`,{busy:true});
     r=await browser.runtime.sendMessage({type:'smart-save-current',parent:folder,tabId:tab.id,url:tab.url,title:tab.title||''});
   }else{
     status(`Adding current tab — copying local file`,{busy:true});
     r=await send({cmd:'add',parent:folder,url:tab.url,name:tab.title||''});
   }
   if(!r.ok)throw new Error(r.error);
   await refreshTree('change');finishOperation(`Saved “${PathLabel(r.path)}”`)
 }
 if(a==='cut'||a==='copy'){
   const use=items.length?topLevelFromNodes(items):[];clipboard={paths:use.map(x=>x.path),names:use.map(x=>x.name),mode:a};
   status(`${a==='cut'?'Cut':'Copied'} ${use.length===1?'“'+use[0].name+'”':use.length+' items'} — choose Paste in another folder`,{hold:2600})
 }
 if(a==='paste'&&clipboard){
   const paths=clipboard.paths||[];beginOperation(clipboard.mode==='cut'?'Moving':'Copying',`${paths.length} item${paths.length===1?'':'s'} into “${PathLabel(folder)}”`);
   const wasCut=clipboard.mode==='cut';
   const run=async()=>{for(const pth of paths){const r=await send({cmd:wasCut?'move':'copy',src:pth,dest:folder});if(!r.ok)throw new Error(r.error)}};
   if(paths.length>1)await withHistoryGroup(`${wasCut?'Move':'Copy'} ${paths.length} items`,run);else await run();
   if(wasCut)clipboard=null;clearSelection({quiet:true});await refreshTree('change');finishOperation(`${wasCut?'Moved':'Copied'} ${paths.length} item${paths.length===1?'':'s'}`)
 }
 if(a==='renamePhysical'&&items.length===1){
   const item=items[0];
   if(item.type!=='bookmark')throw new Error('Choose one bookmark first.');

   const oldPhysicalName=item.physicalName||item.name;
   const next=await stashlibraryPrompt({
     title:'Rename Physical File',
     message:'This changes only the backing PDF/HTML filename. The StashLibrary bookmark name will stay the same.',
     inputLabel:'Filename',
     value:oldPhysicalName,
     confirmLabel:'Rename'
   });

   if(next&&next.trim()){
     beginOperation('Renaming Physical File',`“${item.name}”`);
     const r=await send({cmd:'rename_physical_file',path:item.path,name:next.trim()});
     if(!r.ok)throw new Error(r.error);

     // Renaming the backing file must be silent: do not close, reload, or
     // reopen any Firefox tabs that currently have this StashLibrary item open.
     await refreshTree('change');
     finishOperation(`Renamed Physical File to “${r.physicalName||next.trim()}”`);
   }
 }
 if(a==='rename'&&items.length===1){const item=items[0],current=item.name;const kind=item.type==='folder'?'Folder':'Bookmark';const next=await stashlibraryPrompt({title:`Rename ${kind}`,message:`Enter a new name for this ${kind.toLowerCase()}.`,inputLabel:`${kind} name`,value:current,confirmLabel:'Rename'});if(next&&next.trim()&&next.trim()!==current){beginOperation(`Renaming ${kind}`,`“${current}” → “${next.trim()}”`);const r=await send({cmd:'rename',path:item.path,name:next.trim()});if(!r.ok)throw new Error(r.error);clearSelection({quiet:true});await refreshTree('change');finishOperation(`Renamed ${kind} to “${next.trim()}”`)}}
 if(a==='delete'&&items.length){
   const use=topLevelFromNodes(items);const label=use.length===1?`“${use[0].name}”`:`${use.length} selected items`;
   beginOperation('Deleting',label);let last=null;
   const run=async()=>{for(const item of use){last=await send({cmd:'delete',path:item.path});if(!last.ok)throw new Error(last.error)}};
   if(use.length>1)await withHistoryGroup(`Delete ${use.length} items`,run);else await run();
   clearSelection({quiet:true});
   await refreshTree('change');
   if(last)await refreshHistoryState(last);
   finishOperation(`Deleted ${use.length} item${use.length===1?'':'s'} — Undo available`);
 }
 await refreshHistoryState();
 }catch(e){errorStatus(e);try{await refreshHistoryState()}catch{}}}
function topLevelFromNodes(nodes){const paths=new Set(nodes.map(x=>x.path));return nodes.filter(n=>!nodes.some(p=>p.type==='folder'&&p.path!==n.path&&paths.has(p.path)&&isDescendantPath(n.path,p.path)))}

const STASHLIBRARY_ONBOARDING_KEY='stashlibraryOnboardingCompleteV5';
const STASHLIBRARY_ONBOARDING_SKIPPED_VERSION_KEY='stashlibraryOnboardingSkippedVersion';
const STASHLIBRARY_ONBOARDING_RESUME_KEY='stashlibraryOnboardingResume';
const STASHLIBRARY_ONBOARDING_PROGRESS_VERSION=1;
const STASHLIBRARY_WINDOWS_HELPER_URL='https://github.com/CrowCosmos/StashLibrary/releases';
const STASHLIBRARY_ZOTERO_HELPER_URL='https://github.com/CrowCosmos/StashLibrary/releases';
const STASHLIBRARY_PROTOCOL_VERSION=1;
const STASHLIBRARY_VERSION=browser.runtime.getManifest().version;
const STASHLIBRARY_COMPONENT_VERSION=STASHLIBRARY_VERSION;
const REQUIRED_WINDOWS_HELPER_VERSION='0.1.0';
const STASHLIBRARY_RELEASES_URL='https://github.com/CrowCosmos/StashLibrary/releases';
const STASHLIBRARY_LATEST_RELEASE_URL='https://github.com/CrowCosmos/StashLibrary/releases/latest';
const STASHLIBRARY_RELEASE_MANIFEST_URL='https://crowcosmos.github.io/StashLibrary/updates/release.json';
const MIN_WINDOWS_HELPER_VERSION=REQUIRED_WINDOWS_HELPER_VERSION;
let stashlibraryLatestRelease=null;
let stashlibraryStagedWindowsDownloadId=null;
let stashlibraryWindowsStagePromise=null;
function compareStashLibraryVersions(a,b){
  const pa=String(a||'').split('.').map(n=>Number(n)||0), pb=String(b||'').split('.').map(n=>Number(n)||0);
  for(let i=0;i<Math.max(pa.length,pb.length);i++){const x=pa[i]||0,y=pb[i]||0;if(x!==y)return x<y?-1:1;}
  return 0;
}
let onboardingState={helper:false,folder:false,zotero:false};
let onboardingUpdateRequired=false;
let onboardingZoteroUpdateRequired=false;
let onboardingRequireZotero=false;
let onboardingPollTimer=null;
let onboardingHelperPollBusy=false;
let helperMismatchPaused=false;
let helperInstallAttempted=false;
let zoteroInstallAttempted=false;
let onboardingTreePreloaded=false;
let onboardingTreePreloadPromise=null;
let onboardingPrefetchedTree=null;
let onboardingSlideIndex=0;
let onboardingStartSlide=0;
let onboardingFirstRun=true;
let onboardingOpenedManually=false;
let onboardingReturnStatus='Ready';

function normalizeOnboardingResume(value){
  if(!value)return null;
  if(value===true)return {active:true,legacy:true};
  if(typeof value!=='object'||value.active===false)return null;
  const slide=Number.isFinite(Number(value.slide))?Math.max(0,Math.min(6,Number(value.slide))):null;
  const startSlide=Number.isFinite(Number(value.startSlide))?Math.max(0,Math.min(6,Number(value.startSlide))):null;
  return {active:true,slide,startSlide,legacy:false};
}
function persistOnboardingProgress(){
  const modal=document.querySelector('#onboardingModal');
  if(!modal||modal.hidden)return Promise.resolve();
  return browser.storage.local.set({
    [STASHLIBRARY_ONBOARDING_RESUME_KEY]:{
      active:true,
      version:STASHLIBRARY_ONBOARDING_PROGRESS_VERSION,
      slide:onboardingSlideIndex,
      startSlide:onboardingStartSlide
    }
  }).catch(()=>{});
}

function setOnboardingCheck(id,done,status,problem=false){
  const row=document.querySelector(id);
  if(!row)return;
  row.classList.toggle('done',!!done);
  row.classList.toggle('problem',!!problem&&!done);
  const icon=row.querySelector('.onboarding-check-icon');
  if(icon)icon.textContent=done?'✓':problem?'!':'○';
  const statusEl=row.querySelector('.onboarding-check-status');
  if(statusEl)statusEl.textContent=status;
}
function setOnboardingLocked(id,locked){
  const row=document.querySelector(id);
  if(!row)return;
  row.classList.toggle('locked',!!locked);
  row.setAttribute('aria-disabled',locked?'true':'false');
  row.querySelectorAll('button').forEach(btn=>{btn.disabled=!!locked});
}
function updateOnboardingFinish(){
  const storageReady=!!(onboardingState.helper&&onboardingState.folder);
  const zoteroReady=!!onboardingState.zotero;
  const helperButton=document.querySelector('#onboardingInstallHelper');
  const zoteroButton=document.querySelector('#onboardingInstallZotero');
  if(helperButton)helperButton.hidden=storageReady;
  if(zoteroButton)zoteroButton.hidden=zoteroReady;
  renderOnboardingSlide();
}

function renderOnboardingSlide(){
  const slides=[...document.querySelectorAll('#onboardingModal [data-setup-slide]')];
  if(!slides.length)return;
  const max=slides.length-1;
  onboardingSlideIndex=Math.max(onboardingStartSlide,Math.min(max,onboardingSlideIndex));
  slides.forEach((slide,i)=>{slide.hidden=i!==onboardingSlideIndex});
  const prev=document.querySelector('#onboardingPrev');
  const next=document.querySelector('#onboardingNext');
  const progress=document.querySelector('#onboardingProgress');
  if(progress)progress.textContent=`${onboardingSlideIndex+1} / ${slides.length}`;
  if(prev)prev.disabled=onboardingSlideIndex<=onboardingStartSlide;
  if(!next)return;
  next.textContent=onboardingSlideIndex===max?'Finish':'Next';
  next.setAttribute('aria-label',onboardingSlideIndex===max?'Finish setup':'Next setup page');
  if(onboardingSlideIndex===4){
    next.disabled=!(onboardingState.helper&&onboardingState.folder);
  }else if(onboardingSlideIndex===5){
    next.disabled=!onboardingState.zotero;
  }else next.disabled=false;
}

function queueOnboardingTreePreload(){
  if(!onboardingState.folder || onboardingTreePreloaded || onboardingTreePreloadPromise)return onboardingTreePreloadPromise;

  // Prefetch catalogue DATA only. v0.10.115/116 rendered the whole bookmark
  // list behind the setup modal, so a few hundred root items could block the UI
  // and make Finish Setup appear frozen. Rendering is deferred until the modal
  // has actually closed.
  onboardingTreePreloadPromise=send({cmd:'tree'})
    .then(r=>{
      if(!r?.ok)throw new Error(r?.error||'Could not read StashLibrary catalogue.');
      onboardingPrefetchedTree=r;
      onboardingTreePreloaded=true;
      return true;
    })
    .catch(()=>{onboardingPrefetchedTree=null;onboardingTreePreloaded=false;return false})
    .finally(()=>{onboardingTreePreloadPromise=null});
  return onboardingTreePreloadPromise;
}

function applyOnboardingPrefetchedTree(reason='onboarding'){
  const r=onboardingPrefetchedTree;
  if(!r?.ok)return false;
  const started=performance.now();
  tree=r.tree;
  lastTreeSignature=treeSignature(tree);
  selectedPaths=new Set([...selectedPaths].filter(p=>findNodeByPath(p,tree)));
  renderRoot();
  const welcome=document.querySelector('#welcome');
  const configured=!!r.root;
  if(welcome)welcome.hidden=configured;
  document.querySelector('#settingsBtn')?.classList.toggle('attention',!configured);
  refreshHistoryState().catch(()=>{});
  const count=countNodes(tree);
  debugRecord('tree-refresh',{changed:true,reason,count,source:'onboarding-prefetch',ms:Math.round((performance.now()-started)*10)/10});
  return true;
}

async function applyOnboardingNativeInfo(info){
  const actual=String(info?.hostVersion||'').trim();
  const protocol=Number(info?.protocolVersion||0);
  const helper=!!(info?.ok && protocol===STASHLIBRARY_PROTOCOL_VERSION && actual===REQUIRED_WINDOWS_HELPER_VERSION);
  const folder=helper&&!!String(info?.bookmarks||'').trim();
  const helperProblem=!!actual&&!helper;
  const helperStatus=helper
    ? `${actual?`v${actual} · `:''}${folder?'Ready':'Preparing storage…'}`
    : helperProblem?`v${actual} · Update required`:'Not installed';
  setOnboardingCheck('#onboardingHelperCheck',helper,helperStatus,helperProblem);
  const helperBtn=document.querySelector('#onboardingInstallHelper');
  if(helperBtn){helperBtn.hidden=helper;helperBtn.textContent=helperProblem?'Update StashLibrary…':'Install StashLibrary Helper';}

  if(!helper && actual){
    helperMismatchPaused=true;
    browser.runtime.sendMessage({type:'native-pause'}).catch(()=>{});
  }else if(helper){
    helperMismatchPaused=false;
    helperInstallAttempted=false;
  }

  // Do not run the full Zotero target query while opening Getting Started.
  // That query can take several seconds when Zotero/the companion is absent.
  // Keep the previous optional state for the moment and refresh it separately
  // with the lightweight status endpoint after the local checks are painted.
  const zotero=folder?!!onboardingState.zotero:false;
  setOnboardingCheck('#onboardingZoteroCheck',zotero,zotero?'Connected':'Not connected',false);
  onboardingState={helper,folder,zotero};
  if(info?.webdav)renderOnboardingCloudStatus(info.webdav);
  updateOnboardingFinish();
  if(folder)queueOnboardingTreePreload();
  return onboardingState;
}

async function refreshOnboardingZoteroStatus(){
  if(!onboardingState.folder){
    onboardingState.zotero=false;
    setOnboardingCheck('#onboardingZoteroCheck',false,'Not connected',false);
    const zbtn=document.querySelector('#onboardingInstallZotero');
    if(zbtn)zbtn.hidden=false;
    updateOnboardingFinish();
    return false;
  }
  try{
    const zr=await send({cmd:'zotero_status'});
    const zactual=String(zr?.helperVersion||'').trim();
    const zprotocol=Number(zr?.protocolVersion||0);
    const connected=!!(zr?.ok && zr.connected && zprotocol===STASHLIBRARY_PROTOCOL_VERSION && zactual===REQUIRED_WINDOWS_HELPER_VERSION);
    const incompatible=!!zactual&&!connected;
    onboardingState.zotero=connected;
    if(incompatible)onboardingZoteroUpdateRequired=true;
    const zdetail=String(zr?.statusDetail||'').trim();
    setOnboardingCheck('#onboardingZoteroCheck',connected,connected?`${zactual?`v${zactual} · `:''}Connected`:incompatible?`v${zactual} · Update required`:(zdetail||'Not connected'),incompatible);
    const zbtn=document.querySelector('#onboardingInstallZotero');
    if(zbtn){zbtn.hidden=connected;zbtn.textContent=incompatible?'Update StashLibrary…':'Install Zotero Integration…';}
    updateOnboardingFinish();
    if(onboardingUpdateRequired && onboardingState.helper && onboardingState.folder){
      closeOnboarding();
      onboardingUpdateRequired=false;
      onboardingZoteroUpdateRequired=false;
      browser.storage.local.remove(STASHLIBRARY_ONBOARDING_RESUME_KEY).catch(()=>{});
      requestAnimationFrame(()=>refreshTree('startup').catch(()=>{}));
      status('StashLibrary Helper is compatible — continuing',{hold:2400});
    }
    return connected;
  }catch(_){
    onboardingState.zotero=false;
    setOnboardingCheck('#onboardingZoteroCheck',false,'Not connected',false);
    const zbtn=document.querySelector('#onboardingInstallZotero');
    if(zbtn)zbtn.hidden=false;
    updateOnboardingFinish();
    return false;
  }
}

async function detectInstalledHelperFast(timeout=900){
  helperMismatchPaused=false;
  try{
    const result=await browser.runtime.sendMessage({type:'native-probe',timeout});
    if(result?.ok&&result.info){
      await applyOnboardingNativeInfo(result.info);
      if(onboardingUpdateRequired && onboardingState.helper && onboardingState.folder){
        closeOnboarding();
        onboardingUpdateRequired=false;
        onboardingZoteroUpdateRequired=false;
        browser.storage.local.remove(STASHLIBRARY_ONBOARDING_RESUME_KEY).catch(()=>{});
        requestAnimationFrame(()=>refreshTree('startup').catch(()=>{}));
        status('StashLibrary Helper is compatible — continuing',{hold:2400});
      }
      return onboardingState.helper;
    }
  }catch(_){}
  return false;
}

function stopOnboardingHelperPolling(){
  if(onboardingPollTimer){clearTimeout(onboardingPollTimer);onboardingPollTimer=null;}
  onboardingHelperPollBusy=false;
}
function startOnboardingHelperPolling({immediate=true}={}){
  stopOnboardingHelperPolling();
  const tick=async()=>{
    const modal=document.querySelector('#onboardingModal');
    if(!modal||modal.hidden||onboardingState.helper){stopOnboardingHelperPolling();return;}
    if(!onboardingHelperPollBusy){
      onboardingHelperPollBusy=true;
      if(helperInstallAttempted||onboardingSlideIndex===4){
        setOnboardingCheck('#onboardingHelperCheck',false,'Waiting for StashLibrary Helper…',false);
      }
      try{await detectInstalledHelperFast(900);}catch(_){}
      onboardingHelperPollBusy=false;
      if(onboardingState.helper){stopOnboardingHelperPolling();return;}
    }
    onboardingPollTimer=setTimeout(tick,850);
  };
  onboardingPollTimer=setTimeout(tick,immediate?0:850);
}

async function refreshOnboardingStatus({forceNative=false}={}){
  if(helperMismatchPaused && !forceNative){
    onboardingState={helper:false,folder:false,zotero:false};
    setOnboardingCheck('#onboardingHelperCheck',false,'Waiting for StashLibrary Helper…',false);
    setOnboardingCheck('#onboardingZoteroCheck',false,'Not connected',false);
    const installBtn=document.querySelector('#onboardingInstallHelper');
      if(installBtn)installBtn.hidden=false;
    updateOnboardingFinish();
    return onboardingState;
  }
  try{
    const info=await send({cmd:'backup_info'});
    return await applyOnboardingNativeInfo(info);
  }catch(_){
    setOnboardingCheck('#onboardingHelperCheck',false,'Waiting for StashLibrary Helper…',false);
    setOnboardingCheck('#onboardingZoteroCheck',false,'Not connected',false);
    const installBtn=document.querySelector('#onboardingInstallHelper');
      if(installBtn)installBtn.hidden=false;
    onboardingState={helper:false,folder:false,zotero:false};
    updateOnboardingFinish();
    return onboardingState;
  }
}
async function openOnboarding({initialInfo=null,requireZotero=true,startSlide=null,currentSlide=null,manual=false}={}){
  const modal=document.querySelector('#onboardingModal');
  if(!modal)return;
  onboardingOpenedManually=!!manual;
  settingsPanel.hidden=true;ctx.hidden=true;collapseAll();
  onboardingUpdateRequired=false;
  onboardingZoteroUpdateRequired=false;
  onboardingRequireZotero=!!requireZotero;
  const prefs=await browser.storage.local.get(STASHLIBRARY_ONBOARDING_KEY).catch(()=>({}));
  onboardingFirstRun=!prefs[STASHLIBRARY_ONBOARDING_KEY];
  modal.classList.remove('update-required');
  const extensionStatus=modal.querySelector('#onboardingExtensionStatus');
  if(extensionStatus)extensionStatus.textContent=`v${browser.runtime.getManifest().version} · Installed`;
  const closeButton=modal.querySelector('#onboardingCloseX');
  if(closeButton)closeButton.hidden=!onboardingOpenedManually;
  modal.hidden=false;

  if(initialInfo)await applyOnboardingNativeInfo(initialInfo);
  else await refreshOnboardingStatus({forceNative:true});
  setTimeout(()=>refreshOnboardingZoteroStatus().catch(()=>{}),0);

  if(startSlide==null){
    if(onboardingFirstRun){
      onboardingStartSlide=0;
      onboardingSlideIndex=0;
    }else if(!(onboardingState.helper&&onboardingState.folder)){
      onboardingStartSlide=4;
      onboardingSlideIndex=4;
    }else{
      onboardingStartSlide=5;
      onboardingSlideIndex=5;
    }
  }else{
    onboardingStartSlide=Math.max(0,Math.min(5,Number(startSlide)||0));
    onboardingSlideIndex=onboardingStartSlide;
  }
  if(currentSlide!=null){
    onboardingSlideIndex=Math.max(onboardingStartSlide,Math.min(5,Number(currentSlide)||0));
  }
  renderOnboardingSlide();
  await persistOnboardingProgress();
  if(!onboardingState.helper)startOnboardingHelperPolling({immediate:false});
}
function closeOnboarding(){
  const modal=document.querySelector('#onboardingModal');if(modal)modal.hidden=true;
  stopOnboardingHelperPolling();
  onboardingOpenedManually=false;
}

async function maybeShowOnboarding({startup=false}={}){
  try{
    const prefs=await browser.storage.local.get([STASHLIBRARY_ONBOARDING_KEY,STASHLIBRARY_ONBOARDING_RESUME_KEY]);
    const resume=normalizeOnboardingResume(prefs[STASHLIBRARY_ONBOARDING_RESUME_KEY]);
    const setupComplete=!!prefs[STASHLIBRARY_ONBOARDING_KEY];

    // Setup is strictly first-run only. Once Finish has been pressed, component
    // health must never reopen the wizard — even if an old resume marker was
    // left behind when the popup closed during installation. Clear that stale
    // marker and let the normal repair prompts handle missing/incompatible
    // Windows or Zotero components instead.
    if(setupComplete){
      if(resume)browser.storage.local.remove(STASHLIBRARY_ONBOARDING_RESUME_KEY).catch(()=>{});
      return false;
    }

    let info=null;
    try{info=await Promise.race([send({cmd:'backup_info'}),new Promise((_,reject)=>setTimeout(()=>reject(new Error('Helper check timed out')),5000))]);}catch(_){}
    const helperVersion=String(info?.hostVersion||'').trim();
    const helperInstalled=!!helperVersion;
    const helperCompatible=!!(info?.ok && Number(info?.protocolVersion||0)===STASHLIBRARY_PROTOCOL_VERSION && helperVersion===REQUIRED_WINDOWS_HELPER_VERSION);
    // An unfinished setup session always wins over component auto-detection.
    // Opening GitHub to install a companion closes the Firefox popup, so keep
    // the wizard active and return to the exact page the user was on when the
    // popup is opened again. Older builds stored only a boolean resume flag;
    // page 5 (index 4) is the safest recovery point after a helper install.
    if(resume){
      let resumeStart=resume.startSlide;
      let resumeSlide=resume.slide;
      if(resumeStart==null)resumeStart=setupComplete?4:0;
      if(resumeSlide==null){
        if(!helperInstalled)resumeSlide=setupComplete?4:resumeStart;
        else resumeSlide=Math.max(4,resumeStart);
      }
      await openOnboarding({initialInfo:info,requireZotero:true,startSlide:resumeStart,currentSlide:resumeSlide});
      return true;
    }

    // During a genuine first-run setup, missing/incompatible components remain
    // part of the wizard. Completed users have already returned above and will
    // instead receive the small repair prompts from checkExistingInstallationComponents().
    if(!helperInstalled || !helperCompatible){
      await openOnboarding({initialInfo:info,requireZotero:true,startSlide:null,currentSlide:null});
      return true;
    }

    let zr=null;
    try{zr=await send({cmd:'zotero_status'});}catch(_){}
    const zoteroVersion=String(zr?.helperVersion||'').trim();
    const zoteroConnected=!!zr?.connected;
    const zoteroIncompatible=!!zoteroVersion&&!zoteroConnected;

    // If Zotero is running and we can positively see that the StashLibrary endpoint
    // is absent, or an installed helper answers with an incompatible protocol,
    // route completed installations directly to setup page 6. If Zotero is
    // merely closed, do not mistake that for an uninstalled helper.
    const zoteroDefinitelyMissing=!!(zr?.zoteroReachable && !zoteroConnected && !zoteroVersion);
    // Only route to the Zotero-helper repair page when StashLibrary has positive
    // evidence that repair is actually needed. Zotero simply being closed is
    // a normal state and must never look like a missing-helper problem.
    if(zoteroIncompatible || zoteroDefinitelyMissing){
      await openOnboarding({initialInfo:info,requireZotero:true,startSlide:null,currentSlide:null});
      return true;
    }

    // Reinstalling the Firefox extension should not force setup again when both
    // companion components are already present and compatible.
    if(!setupComplete && zoteroConnected && zoteroVersion){
      browser.storage.local.set({[STASHLIBRARY_ONBOARDING_KEY]:true}).catch(()=>{});
    }
    return false;
  }catch(_){return false}
}

function setBackupRunning(active){
  const create=document.querySelector('#backupNow'),cancel=document.querySelector('#cancelBackup');
  if(create){create.disabled=!!active;create.textContent=active?'Backup in progress…':'Create Manual Backup…';}
  if(cancel){cancel.hidden=!active;cancel.disabled=false;}
}
function formatCloudBackupTime(value){
  if(!value)return 'Never';
  const d=new Date(value);
  if(Number.isNaN(d.getTime()))return String(value);
  try{return d.toLocaleString(undefined,{dateStyle:'long',timeStyle:'short'});}catch(_){return d.toLocaleString();}
}
function webdavProviderDefaults(provider){
  const p=String(provider||'');
  const map={
    'koofr':{url:'https://app.koofr.net/dav/Koofr',help:'Use your Koofr email address and a Koofr app password.',custom:false},
    'pcloud-eu':{url:'https://ewebdav.pcloud.com',help:'Use your pCloud email address and password.',custom:false},
    'pcloud-us':{url:'https://webdav.pcloud.com',help:'Use your pCloud email address and password.',custom:false},
    'nextcloud':{url:'',help:'Paste the WebDAV address shown by your Nextcloud account. An app password is recommended.',custom:true},
    'owncloud':{url:'',help:'Paste the WebDAV address for your ownCloud account.',custom:true},
    'other':{url:'',help:'Enter the HTTPS WebDAV address supplied by your provider.',custom:true}
  };
  return map[p]||{url:'',help:'Choose a provider to continue.',custom:false};
}
function updateWebdavProviderForm({preserveUrl=true}={}){
  const provider=document.querySelector('#webdavProvider')?.value||'';
  const preset=webdavProviderDefaults(provider);
  const row=document.querySelector('#webdavServerRow'),url=document.querySelector('#webdavUrl'),help=document.querySelector('#webdavProviderHelp');
  if(row)row.hidden=!preset.custom;
  if(url && (!preserveUrl || !url.value || !preset.custom))url.value=preset.url;
  if(help)help.textContent=preset.help;
  const btn=document.querySelector('#webdavConnect');if(btn)btn.disabled=!provider;
}
function formatCloudProgressStage(kind,stage){
  const k=String(kind||'').toLowerCase();
  const op=k.includes('recovery')?'Recovery':(k.includes('restore')?'Restore':'Backup');
  const raw=String(stage||'').trim();
  if(!raw)return `Cloud ${op}`;
  const friendly=({
    'verifying local catalogue':'Checking your library','comparing cloud backup':'Checking the existing backup',
    'uploading files':'Uploading saved items','uploading catalogue':'Finishing the upload',
    'verifying cloud catalogue':'Checking the uploaded backup','publishing verified backup':'Finalising the backup',
    'confirming cloud changes':'Confirming the backup','downloading catalogue':'Opening the backup',
    'comparing local files':'Comparing saved items','downloading changed files':'Downloading saved items',
    'verifying downloaded backup':'Checking the downloaded backup','finished':'Complete','failed':'Needs attention'
  })[raw.toLowerCase()]||raw.charAt(0).toUpperCase()+raw.slice(1);
  return `Cloud ${op} · ${friendly}`;
}
function friendlyCloudError(value){
  const text=String(value||'');
  if(/(?:http\s*)?401|unauthori[sz]ed/i.test(text))return 'Could not sign in. Check your Koofr email and app password.';
  if(/(?:http\s*)?403|forbidden/i.test(text))return 'Koofr would not allow this action. Check the connected account.';
  if(/timed?\s*out|timeout/i.test(text))return 'The cloud service took too long to respond. Please try again.';
  return text;
}
function renderWebdavWorkerProgress(workers=[]){
  const box=document.querySelector('#webdavWorkerProgress');if(!box)return;
  const rows=Array.isArray(workers)?workers.filter(w=>w&&typeof w==='object'):[];
  box.replaceChildren();box.hidden=!rows.length;
  if(!rows.length)return;
  for(const worker of rows){
    const row=document.createElement('div');row.className='cloud-transfer-worker';
    const head=document.createElement('div');head.className='cloud-transfer-worker-head';
    const slot=document.createElement('span');slot.className='cloud-transfer-worker-slot';slot.textContent=String(worker.id||'');
    const label=document.createElement('span');label.className='cloud-transfer-worker-label';
    const status=String(worker.status||'').toLowerCase();
    const prefix=status==='retrying'?`Retry ${Number(worker.attempt||2)} · `:(status==='failed'?'Failed · ':(status==='complete'?'Complete · ':''));
    label.textContent=prefix+String(worker.label||'Waiting…');label.title=label.textContent;
    const sent=Number(worker.sent||0),total=Number(worker.total||0),pct=Number(worker.percent);
    const determinate=Number.isFinite(pct)&&total>0;
    const clamped=determinate?Math.max(0,Math.min(100,pct)):0;
    const pctEl=document.createElement('span');pctEl.className='cloud-transfer-worker-percent';
    pctEl.textContent=determinate?`${Math.round(clamped)}%`:(worker.active?'…':'0%');
    head.append(slot,label,pctEl);
    const track=document.createElement('div');track.className='cloud-transfer-worker-track';
    track.setAttribute('role','progressbar');track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax','100');
    track.setAttribute('aria-label',`Transfer ${worker.id||''}: ${worker.label||'Waiting'}`);
    const fill=document.createElement('div');fill.className='cloud-transfer-worker-fill';
    if(determinate){fill.style.width=`${clamped}%`;track.setAttribute('aria-valuenow',String(Math.round(clamped)));}
    else if(worker.active){row.classList.add('is-indeterminate');track.removeAttribute('aria-valuenow');}
    track.append(fill);row.append(head,track);box.append(row);
  }
}
function renderWebdavProgress({active=false,kind='',stage='',detail='',percent=null,error='',workers=[]}={}){
  const box=document.querySelector('#webdavProgress');if(!box)return;
  const terminal=['finished','failed','cancelled'].includes(String(stage||'').toLowerCase());
  const show=!!active && !terminal;
  box.hidden=!show;
  if(!show){renderWebdavWorkerProgress([]);return;}
  const pct=Number(percent);const determinate=Number.isFinite(pct);
  const clamped=determinate?Math.max(0,Math.min(100,pct)):null;
  box.classList.toggle('is-indeterminate',!determinate);
  const stageEl=document.querySelector('#webdavProgressStage');if(stageEl)stageEl.textContent=formatCloudProgressStage(kind,stage);
  const pctEl=document.querySelector('#webdavProgressPercent');if(pctEl)pctEl.textContent=determinate?`${Math.round(clamped)}%`:'Working…';
  const detailEl=document.querySelector('#webdavProgressDetail');if(detailEl){detailEl.textContent=friendlyCloudError(detail||error||'Working in the background…');detailEl.title=detailEl.textContent;}
  const fill=document.querySelector('#webdavProgressFill');if(fill && determinate)fill.style.width=`${clamped}%`;
  const track=box.querySelector('.cloud-backup-progress-track');if(track){
    track.setAttribute('aria-valuetext',`${formatCloudProgressStage(kind,stage)}${detail?` — ${detail}`:''}`);
    if(determinate){track.setAttribute('aria-valuenow',String(Math.round(clamped)));}
    else{track.removeAttribute('aria-valuenow');}
  }
  renderWebdavWorkerProgress(workers);
}

function renderManualBackupProgress({active=false,stage='',detail='',percent=null,error=''}={}){
  const box=document.querySelector('#manualBackupProgress');if(!box)return;
  const rawStage=String(stage||'').trim();const normalized=rawStage.toLowerCase();
  const show=!!active&&!['finished','failed','cancelled'].includes(normalized);
  box.hidden=!show;if(!show)return;
  const pct=Number(percent);const determinate=Number.isFinite(pct);const clamped=determinate?Math.max(0,Math.min(100,pct)):null;
  box.classList.toggle('is-indeterminate',!determinate);
  const label=rawStage?`Manual Backup · ${rawStage.charAt(0).toUpperCase()+rawStage.slice(1)}`:'Manual Backup';
  const stageEl=document.querySelector('#manualBackupProgressStage');if(stageEl)stageEl.textContent=label;
  const pctEl=document.querySelector('#manualBackupProgressPercent');if(pctEl)pctEl.textContent=determinate?`${Math.round(clamped)}%`:'Working…';
  const detailEl=document.querySelector('#manualBackupProgressDetail');if(detailEl){detailEl.textContent=String(detail||error||'Creating backup…');detailEl.title=detailEl.textContent;}
  const fill=document.querySelector('#manualBackupProgressFill');if(fill&&determinate)fill.style.width=`${clamped}%`;
  const track=box.querySelector('.cloud-backup-progress-track');if(track){track.setAttribute('aria-valuetext',`${label}${detail?` — ${detail}`:''}`);if(determinate)track.setAttribute('aria-valuenow',String(Math.round(clamped)));else track.removeAttribute('aria-valuenow');}
}
function setManualBackupRunning(active,{cancelling=false}={}){
  manualBackupActive=!!active;
  const button=document.querySelector('#manualBackupCreate'),label=button?.querySelector('.cloud-backup-action-label');
  if(label)label.textContent=manualBackupActive?(cancelling?'Cancelling Backup…':'Cancel Backup'):'Create Manual Backup…';
  if(button){button.setAttribute('aria-label',manualBackupActive?'Cancel Manual Backup':'Create Manual Backup');button.disabled=!!cancelling;}
  const more=document.querySelector('#manualBackupMoreActions');if(more)more.disabled=manualBackupActive;
}
function renderWebdavBackupInfo(info={}){
  const connected=!!info.connected;
  // v0.10.181: only block a manual overwrite when the helper says the
  // current local library cannot safely replace the cloud copy. Older
  // helpers/configs can retain a stale safetyHold flag after upgrading, so
  // safetyHold by itself must not make Back Up Now permanently inert.
  const canReplace=info.canReplaceWithCurrent!==false;
  const replacementBlocked=connected&&!canReplace;
  const disconnected=document.querySelector('#webdavDisconnectedView'),connectedView=document.querySelector('#webdavConnectedView');
  if(disconnected)disconnected.hidden=connected;
  if(connectedView)connectedView.hidden=!connected;
  const provider=document.querySelector('#webdavProvider');
  if(provider && !connected && info.provider)provider.value=info.provider;
  const username=document.querySelector('#webdavUsername');if(username && !connected && info.username)username.value=info.username;
  const url=document.querySelector('#webdavUrl');if(url && !connected && info.url)url.value=info.url;
  if(!connected)updateWebdavProviderForm({preserveUrl:true});
  const connectButton=document.querySelector('#webdavConnect');if(connectButton)connectButton.textContent='Configure Cloud Backup';
  const providerName=document.querySelector('#webdavConnectedProvider');if(providerName)providerName.textContent=info.providerLabel||'WebDAV';
  const account=document.querySelector('#webdavConnectedAccount');if(account)account.textContent=info.username?` · ${info.username}`:'';
  const last=document.querySelector('#webdavLastBackup');if(last)last.textContent=`Last backup: ${formatCloudBackupTime(info.lastBackupAt)}`;
  const lastRun=document.querySelector('#webdavLastRunSummary');
  if(lastRun){
    const haveRun=!!info.lastBackupAt && info.lastRunProviderConfirmed===true;
    if(haveRun){
      const changed=Number(info.lastRunNew||0)+Number(info.lastRunUpdated||0)+Number(info.lastRunDeleted||0);
      const checked=changed+Number(info.lastRunUnchanged||0);
      lastRun.textContent=changed?`${changed} change${changed===1?'':'s'} backed up · ${checked} item${checked===1?'':'s'} checked`:`Up to date · ${checked} item${checked===1?'':'s'} checked`;
      lastRun.hidden=false;
    }else{lastRun.textContent='';lastRun.hidden=true;}
  }
  const hold=document.querySelector('#webdavSafetyHold');if(hold)hold.hidden=!replacementBlocked;
  const holdText=document.querySelector('#webdavSafetyHoldText');
  if(holdText && replacementBlocked){
    const remote=Number(info.remoteFileCount||0);
    holdText.textContent=remote>0
      ?`This computer's StashLibrary library is empty, while the cloud backup contains ${remote} archived file${remote===1?'':'s'}. Back Up Now is blocked so the cloud backup cannot be erased.`
      :`StashLibrary is protecting the existing cloud backup because this local library is empty.`;
  }
  const error=document.querySelector('#webdavBackupError');if(error){error.textContent=info.lastError?`Cloud backup needs attention: ${friendlyCloudError(info.lastError)}`:'';error.hidden=!info.lastError;}
  renderWebdavProgress({active:!!info.cloudOperationActive,kind:info.cloudOperation,stage:info.cloudOperationStage,detail:info.cloudOperationDetail,percent:info.cloudOperationPercent,error:info.cloudOperationError,workers:info.cloudOperationWorkers||[]});
  const backup=document.querySelector('#webdavBackupNow');
  if(backup){
    const active=!!info.backupActive;const op=String(info.cloudOperation||'').toLowerCase();
    backup.disabled=!connected||replacementBlocked||active;
    const label=document.querySelector('#webdavBackupActionLabel');
    const activeLabel=op.includes('recovery')?'Recovering…':(op.includes('restore')?'Restoring…':'Backing Up…');
    const activeAria=op.includes('recovery')?'Recovering cloud backup':(op.includes('restore')?'Restoring cloud backup':'Backing up to the cloud');
    if(label)label.textContent=active?activeLabel:'Back Up Now…';
    backup.setAttribute('aria-label',active?activeAria:'Back Up Now');
  }
  const restore=document.querySelector('#webdavRestore');if(restore){restore.disabled=!connected||!info.hasCloudBackup||!!info.backupActive;restore.textContent='Restore from Cloud Backup…';}
  const recoverKoofr=document.querySelector('#webdavRecoverKoofrZip');
  if(recoverKoofr){
    const show=connected&&String(info.provider||'').toLowerCase()==='koofr'&&!!info.legacyKoofrRecoveryNeeded;
    recoverKoofr.hidden=!show;recoverKoofr.disabled=!show||!!info.backupActive;
  }
  const disconnect=document.querySelector('#webdavDisconnect');if(disconnect)disconnect.disabled=!connected||!!info.backupActive;
  const more=document.querySelector('#webdavMoreActions');if(more)more.disabled=!connected||!!info.backupActive;
  if(!connected||info.backupActive)closeWebdavMoreMenu();
  renderOnboardingCloudStatus(info);
}
function renderOnboardingCloudStatus(info={}){
  const row=document.querySelector('#onboardingCloudSummary');if(!row)return;
  const connected=!!info.connected;row.classList.toggle('done',connected);row.classList.remove('problem');
  const icon=row.querySelector('.onboarding-check-icon');if(icon)icon.textContent=connected?'✓':'○';
  const statusEl=document.querySelector('#onboardingCloudStatus');
  if(statusEl)statusEl.textContent=connected?`${info.providerLabel||'Cloud backup'} connected${info.lastBackupAt?` · Last backup ${formatCloudBackupTime(info.lastBackupAt)}`:''}`:'Not connected — optional';
  const button=document.querySelector('#onboardingOpenCloudSettings');if(button)button.textContent=connected?'Manage Cloud Backup…':'Set Up Cloud Backup…';
}
function renderStashLibraryUpdateComponent(name,build,state,detail=''){
  const icon=state==='ok'?'✓':state==='waiting'?'○':'!';
  const cls=state==='problem'?' problem':'';
  const shown=build?`Build ${build}`:(detail||'Not detected');
  return `<div class="stashlibrary-update-component${cls}"><span class="stashlibrary-update-icon" aria-hidden="true">${icon}</span><span>${name}</span><span class="stashlibrary-update-version">${escapeHtml(shown)}</span></div>`;
}
async function fetchStashLibraryReleaseManifest(force=false){
  if(stashlibraryLatestRelease&&!force)return stashlibraryLatestRelease;
  try{
    const url=STASHLIBRARY_RELEASE_MANIFEST_URL+(STASHLIBRARY_RELEASE_MANIFEST_URL.includes('?')?'&':'?')+'t='+Date.now();
    const response=await fetch(url,{cache:'no-store'});
    if(!response.ok)throw new Error(`Update server returned ${response.status}`);
    const data=await response.json();
    const version=String(data?.version||data?.component_version||'').trim();
    if(!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(version))throw new Error('Update manifest has no valid semantic version.');
    stashlibraryLatestRelease={...data,version};
    return stashlibraryLatestRelease;
  }catch(error){
    return {offline:true,error:String(error?.message||error||'Unknown update-check error')};
  }
}
function releaseComponentVersion(release){return String(release?.component_version||release?.version||'').trim();}
async function stageWindowsUpdateIfNeeded(release){
  const releaseVersion=String(release?.version||release?.component_version||'').trim();
  if(!releaseVersion||compareStashLibraryVersions(releaseVersion,STASHLIBRARY_VERSION)<=0)return null;
  const url=String(release.windows_setup_url||'').trim();
  if(!url)return null;
  try{
    const reply=await browser.runtime.sendMessage({type:'stashlibrary-stage-windows-update',url,version:releaseVersion});
    if(reply?.ok&&Number.isInteger(reply.downloadId)){stashlibraryStagedWindowsDownloadId=reply.downloadId;return reply.downloadId;}
  }catch(_){ }
  return null;
}
async function refreshStashLibraryUpdateStatus(nativeInfo=null,{forceReleaseCheck=false}={}){
  const summary=document.querySelector('#stashlibraryUpdateSummary');
  const button=document.querySelector('#updateStashLibraryBtn');
  if(!summary||!button)return;
  if(forceReleaseCheck){summary.textContent='Checking for updates…';button.hidden=true;}
  const release=await fetchStashLibraryReleaseManifest(forceReleaseCheck);
  if(release?.offline){
    summary.textContent='Could not check for updates';
    summary.title=release.error||'The update manifest could not be reached or parsed.';
    button.hidden=false;
    button.disabled=false;
    button.dataset.action='retry';
    button.textContent='Check again';
    return;
  }
  summary.title='';
  const releaseVersion=String(release?.version||'').trim();
  if(!releaseVersion){
    summary.textContent='Update check failed';
    button.hidden=false;button.disabled=false;button.dataset.action='retry';button.textContent='Check again';
    return;
  }
  const updateAvailable=compareStashLibraryVersions(releaseVersion,STASHLIBRARY_VERSION)>0;
  if(updateAvailable){
    // A newer release without its expected download URLs is not "available" in
    // a usable sense. Surface that as an error instead of claiming success.
    const missing=[];
    if(!String(release.windows_setup_url||'').trim())missing.push('Windows installer');
    if(!String(release.firefox||release.firefox_url||'').trim())missing.push('Firefox package');
    if(!String(release.zotero||release.zotero_url||'').trim())missing.push('Zotero package');
    if(missing.length){
      summary.textContent='Update found, but download unavailable';
      summary.title=`Missing from update manifest: ${missing.join(', ')}`;
      button.hidden=false;button.disabled=false;button.dataset.action='retry';button.textContent='Check again';
      return;
    }
    summary.textContent='Update available';
    button.hidden=false;
    button.disabled=false;
    button.dataset.action='install';
    button.textContent='Install latest update';
    if(stashlibraryStagedWindowsDownloadId==null && !stashlibraryWindowsStagePromise){
      stashlibraryWindowsStagePromise=stageWindowsUpdateIfNeeded(release).finally(()=>{stashlibraryWindowsStagePromise=null;});
      stashlibraryWindowsStagePromise.catch(()=>{});
    }
  }else{
    // "Up to date" is reserved for a completed, valid manifest check.
    summary.textContent='Up to date';
    button.hidden=true;
    button.disabled=false;
    button.dataset.action='install';
  }
}

async function waitForZoteroReady(timeoutMs=90000){
  const deadline=Date.now()+timeoutMs;
  while(Date.now()<deadline){
    try{const zr=await send({cmd:'zotero_status'});if(zr?.ok&&zr.connected)return zr;}catch(_){ }
    await new Promise(resolve=>setTimeout(resolve,1200));
  }
  throw new Error('Zotero did not open in time. Open Zotero and try Update StashLibrary again.');
}
async function beginCoordinatedStashLibraryUpdate(){
  const button=document.querySelector('#updateStashLibraryBtn');
  const summary=document.querySelector('#stashlibraryUpdateSummary');
  // downloads.open() must be invoked directly from the user's click handler.
  // Settings pre-stages the Windows installer when an update is discovered.
  if(stashlibraryStagedWindowsDownloadId!=null){
    try{browser.downloads.open(stashlibraryStagedWindowsDownloadId);}catch(_){ }
  }
  if(button){button.disabled=true;button.textContent='Preparing update…';}
  try{
    const release=await fetchStashLibraryReleaseManifest(true);
    if(compareStashLibraryVersions(String(release.version||''),STASHLIBRARY_VERSION)<=0){
      if(summary)summary.textContent=`Version ${STASHLIBRARY_VERSION} · Up to date`;
      return;
    }
    const wantedVersion=releaseComponentVersion(release);
    if(wantedVersion&&wantedVersion===STASHLIBRARY_COMPONENT_VERSION){
      // Valid: StashLibrary build numbers can advance while host-required manifest versions stay separate.
    }
    let zr=null;
    try{zr=await send({cmd:'zotero_status'});}catch(_){ }
    if(!(zr?.ok&&zr.connected)){
      const ok=await stashlibraryConfirm({
        title:'Open Zotero to Update StashLibrary',
        message:'StashLibrary updates Firefox, the Windows helper and the Zotero integration as one tested build. Open Zotero now, wait until it has finished starting, then click Continue.',
        confirmLabel:'Continue',danger:false
      });
      if(!ok)return;
      if(summary)summary.textContent='Waiting for Zotero…';
      zr=await waitForZoteroReady();
    }
    await browser.storage.local.set({stashlibraryUpdatePendingVersion:String(release.version),stashlibraryUpdateStartedAt:new Date().toISOString()}).catch(()=>{});
    if(summary)summary.textContent=`Updating StashLibrary to Version ${release.version}…`;

    // Zotero applies its own XPI through AddonManager while Zotero is running.
    const zoteroResult=await browser.runtime.sendMessage({type:'stashlibrary-zotero-update',expectedVersion:String(release.version)}).catch(e=>({ok:false,error:String(e?.message||e)}));
    if(!zoteroResult?.ok)throw new Error(zoteroResult?.error||'Zotero could not start its StashLibrary update.');

    // Stage/open Windows installer. If it was not already staged, download it now;
    // Firefox may require the user to click the downloaded installer because the
    // downloads.open privilege only survives the original user gesture.
    if(stashlibraryStagedWindowsDownloadId==null){
      const staged=await stageWindowsUpdateIfNeeded(release);
      if(staged==null)throw new Error('The Windows update could not be staged.');
      if(summary)summary.textContent=`Version ${release.version}: Windows installer downloaded. Open it from Firefox Downloads to continue.`;
    }

    // Ask Firefox to fetch its XPI from update_url. The release uses the stable asset name stashlibrary-firefox.xpi; test releases may use an unsigned XPI where Firefox permits it, while normal Firefox releases must use a Mozilla-signed XPI under the same filename. onUpdateAvailable keeps
    // it pending until the coordinated transaction is ready to reload.
    const firefoxResult=await browser.runtime.sendMessage({type:'stashlibrary-firefox-update-check',expectedVersion:String(release.firefox_version||wantedVersion||'')}).catch(e=>({ok:false,error:String(e?.message||e)}));
    if(!firefoxResult?.ok)throw new Error(firefoxResult?.error||'Firefox could not check for the StashLibrary extension update.');

    if(summary)summary.textContent=`Version ${release.version} is being applied. Keep Zotero open and complete the Windows installer if Firefox shows it.`;
    button.textContent='Verify update';
  }finally{
    if(button)button.disabled=false;
  }
}

let settingsHelperConnected=false;
let settingsLastStoragePath='';

async function refreshSettingsInfo(){
  const localPath=document.querySelector('#settingsLocalStoragePath');
  const openLocal=document.querySelector('#settingsOpenLocalFolder');
  let info=null;

  // Only a failure of the helper request itself may mark the Windows helper as
  // disconnected. Optional Settings refreshes below (updates, Zotero, cloud,
  // history, etc.) must never overwrite a path we have already confirmed.
  try{
    info=await send({cmd:'backup_info'});
    settingsHelperConnected=!!info?.ok;
    const confirmedPath=String(info?.bookmarks||info?.bookmarksPath||'').trim();
    if(confirmedPath)settingsLastStoragePath=confirmedPath;
    if(localPath)localPath.textContent=settingsLastStoragePath||'No StashLibrary storage folder selected.';
    if(openLocal)openLocal.disabled=!settingsLastStoragePath;
  }catch(e){
    if(!settingsHelperConnected){
      if(localPath)localPath.textContent='StashLibrary Helper not connected.';
      if(openLocal)openLocal.disabled=true;
    }else{
      // A transient later request failure does not undo a previously confirmed
      // connection. Keep the last known-good path visible.
      if(localPath)localPath.textContent=settingsLastStoragePath||'StashLibrary storage folder path unavailable.';
      if(openLocal)openLocal.disabled=!settingsLastStoragePath;
    }
    setBackupRunning(false);
    try{await refreshStashLibraryUpdateStatus(null);}catch(_){ }
    return;
  }

  const manualActive=!!info.backupActive,cloudActive=!!info.webdav?.backupActive;
  setManualBackupRunning(manualActive);
  const manualCreate=document.querySelector('#manualBackupCreate');if(manualCreate)manualCreate.disabled=!settingsLastStoragePath||cloudActive;
  const manualMore=document.querySelector('#manualBackupMoreActions');if(manualMore)manualMore.disabled=!settingsLastStoragePath||manualActive||cloudActive;

  try{renderWebdavBackupInfo(await send({cmd:'webdav_info'}));}
  catch(_){
    const error=document.querySelector('#webdavBackupError');if(error){error.hidden=false;error.textContent='Cloud Backup requires StashLibrary Helper 0.1.0.';}
  }

  setBackupRunning(!!info.backupActive||!!info.webdav?.backupActive);
  const hv=document.querySelector('#nativeHelperVersionWarning');
  if(hv){const actual=String(info.hostVersion||'');const compatible=Number(info.protocolVersion||0)===STASHLIBRARY_PROTOCOL_VERSION && actual===REQUIRED_WINDOWS_HELPER_VERSION;hv.hidden=compatible;hv.textContent=compatible?'':`StashLibrary update incomplete${actual?` — Windows integration is Build ${actual}`:''}. Install the latest StashLibrary release to continue.`;}

  try{
    const jobReply=await browser.runtime.sendMessage({type:'webdav-job-status'});
    const job=jobReply?.job;if(job?.active)status(`${job.kind==='restore'?'Cloud Restore':'Cloud Backup'} — running in the background. You can close StashLibrary.`,{busy:true});
  }catch(_){ }

  // These are optional Settings panels. Their failures must stay local and must
  // never be interpreted as loss of the native-helper connection.
  try{await refreshHistoryState(info);}catch(_){ }
  try{await refreshStashLibraryUpdateStatus(info);}catch(_){ }
}
async function openSettings(){
  collapseAll(); ctx.hidden=true; settingsPanel.hidden=false;
  const prefs=await browser.storage.local.get(['theme']);
  const theme=prefs.theme||'system';
  document.querySelectorAll('[data-theme]').forEach(b=>b.classList.toggle('selected',b.dataset.theme===theme));
  await refreshSettingsInfo();
}
function closeSettings(){closeWebdavMoreMenu();settingsPanel.hidden=true}
document.querySelector('#updateStashLibraryBtn')?.addEventListener('click',e=>{
  const button=e.currentTarget;
  if(button?.dataset?.action==='retry')refreshStashLibraryUpdateStatus(null,{forceReleaseCheck:true}).catch(errorStatus);
  else beginCoordinatedStashLibraryUpdate().catch(errorStatus);
});


document.addEventListener('contextmenu',e=>{if(e.target.closest('#settingsPanel,#settingsBtn'))return;const t=contextTarget(e);if(!t)return;e.preventDefault();e.stopPropagation();if(t.node&&!selectedPaths.has(t.node.path)&&selectedPaths.size)clearSelection({quiet:true});showCtx(e.clientX,e.clientY,t.node,t.folder);clearKeyboardCurrent(ctx)});
document.addEventListener('dragend',clearDropMarks,true);document.addEventListener('drop',()=>setTimeout(clearDropMarks,0),true);
document.body.addEventListener('mousedown',e=>{
  // Settings is deliberately not a modal: clicking the gear toggles it, and
  // clicking genuinely empty bookmark-area space closes it. Bookmark/folder
  // interactions themselves do not count as click-off.
  if(!settingsPanel.hidden && e.target.closest('#root') && !e.target.closest('.item,.flyout,.ctx'))closeSettings();
  if(!e.target.closest('.item,.flyout,#settingsPanel,#settingsBtn,.ctx,#zoteroDestinationModal,#stashlibraryDestinationModal')){collapseAll();if(!e.ctrlKey&&!e.metaKey)clearSelection({quiet:true})}
  if(!e.target.closest('.ctx'))ctx.hidden=true
});

async function getCurrentPageMetadata(tab){
  const fallback={title:tab?.title||'',authors:[]};
  if(!tab?.id || !/^https?:/i.test(tab.url||'')) return fallback;
  try{
    const results=await browser.tabs.executeScript(tab.id,{code:`(()=>{
      const pick=(sel,attr='content')=>{const e=document.querySelector(sel);return e?(e.getAttribute(attr)||e.textContent||'').trim():''};
      const picks=(sels)=>{for(const s of sels){const v=pick(s);if(v)return v}return ''};
      let title=picks(['meta[name="citation_title"]','meta[property="og:title"]','meta[name="dc.title"]','meta[name="DC.title"]'])||document.title||'';
      const authors=[]; const seen=new Set();
      const add=a=>{a=String(a||'').replace(/\\s+/g,' ').trim();if(a&&!seen.has(a)){seen.add(a);authors.push(a)}};
      document.querySelectorAll('meta[name="citation_author"],meta[name="author"],meta[name="dc.creator"],meta[name="DC.creator"]').forEach(e=>add(e.getAttribute('content')));
      if(!authors.length){
        document.querySelectorAll('script[type="application/ld+json"]').forEach(sc=>{try{
          const raw=JSON.parse(sc.textContent||'null'); const nodes=Array.isArray(raw)?raw:(raw&&raw['@graph']?raw['@graph']:[raw]);
          for(const n of nodes||[]){if(!n||typeof n!=='object')continue; if(!title&&n.headline)title=String(n.headline); let a=n.author;if(!a)continue;a=Array.isArray(a)?a:[a];for(const x of a){if(typeof x==='string')add(x);else if(x&&x.name)add(x.name)}}
        }catch(_){}})
      }
      return {title:String(title||'').trim(),authors};
    })()`});
    return results?.[0]||fallback;
  }catch(_){return fallback}
}

let pendingStashLibrarySave=null,ldestNodes=[],ldestSelectedPath='',ldestSelectedName='',ldestLastPath='';
let currentStashLibrarySaveAuditId='';
function renderStashLibraryDestinations(){
  const box=document.querySelector('#ldestTree');box.innerHTML='';ldestNodes=[];
  if(!tree?.path){box.innerHTML='<div class="empty-zotero">Choose a StashLibrary storage folder in Settings first.</div>';return}
  let index=0;
  const makeNode=(src,parent=null,level=0)=>{
    const node={index:index++,src,path:src.path,name:src.name||PathLabel(src.path)||'StashLibrary root',parent,level,children:[],expanded:level===0};
    ldestNodes.push(node);
    for(const child of (src.children||[])){if(child.type==='folder')node.children.push(makeNode(child,node,level+1))}
    return node;
  };
  const rootNode=makeNode(tree,null,0);
  rootNode.name='StashLibrary root';
  const visible=n=>{let p=n.parent;while(p){if(!p.expanded)return false;p=p.parent}return true};
  const update=()=>{for(const n of ldestNodes){const row=box.querySelector(`[data-lidx="${n.index}"]`);if(!row)continue;row.classList.toggle('hiddenByParent',!visible(n));const tog=row.querySelector('.zdest-toggle');if(tog)tog.textContent=n.children.length?(n.expanded?'▾':'▸'):''}};
  const select=(n,scroll=false)=>{ldestSelectedPath=n.path;ldestSelectedName=n.name;box.querySelectorAll('.zdest-row').forEach(x=>x.classList.remove('selected'));const row=box.querySelector(`[data-lidx="${n.index}"]`);row?.classList.add('selected');document.querySelector('#ldestSelected').textContent=n.name;document.querySelector('#ldestSave').disabled=false;if(scroll&&row)requestAnimationFrame(()=>row.scrollIntoView({block:'nearest'}))};
  for(const n of ldestNodes){
    const row=document.createElement('div');row.className='zdest-row';row.dataset.lidx=n.index;row.style.paddingLeft=`${5+n.level*16}px`;
    const tog=document.createElement('span');tog.className='zdest-toggle';
    const icon=document.createElement('span');icon.className='folder-icon';
    const name=document.createElement('span');name.className='zdest-name';name.textContent=n.name;
    row.append(tog,icon,name);
    tog.onclick=e=>{e.stopPropagation();if(n.children.length){n.expanded=!n.expanded;update()}};
    row.onclick=()=>select(n);box.append(row);
  }

  // Restore the last folder that was successfully used for Save to StashLibrary.
  // Expand only its ancestor chain so the tree opens no further than needed.
  const remembered=ldestLastPath?ldestNodes.find(n=>String(n.path)===String(ldestLastPath)):null;
  const initial=remembered||rootNode;
  if(remembered){
    let a=remembered.parent;
    while(a){a.expanded=true;a=a.parent}
  }
  update();
  select(initial,true);

  document.querySelector('#ldestExpand').onclick=()=>{ldestNodes.forEach(n=>n.expanded=true);update()};
  document.querySelector('#ldestCollapse').onclick=()=>{ldestNodes.forEach(n=>n.expanded=false);rootNode.expanded=true;update()};
}
async function openStashLibraryDestinationPicker(){
  try{
    if(!tree?.path)throw new Error('Choose a StashLibrary storage folder in Settings first.');
    const tab=await getCurrentTab();
    if(!/^(https?:|file:)/i.test(tab.url))throw new Error(`This Firefox page cannot be archived: ${tab.url.split(':')[0]}: pages are protected by Firefox.`);
    pendingStashLibrarySave=tab;ldestSelectedPath='';ldestSelectedName='';
    try{const saved=await browser.storage.local.get('stashlibraryLastSaveFolder');ldestLastPath=String(saved?.stashlibraryLastSaveFolder||'')}catch(_){ldestLastPath=''}
    document.querySelector('#stashlibraryDestinationModal').hidden=false;
    document.querySelector('#ldestStatus').textContent='Choose where to save the current page or PDF, then click Save.';
    document.querySelector('#ldestSelected').textContent='None';document.querySelector('#ldestSave').disabled=true;
    renderStashLibraryDestinations();
  }catch(e){errorStatus(e)}
}
function closeStashLibraryDestinationPicker(){document.querySelector('#stashlibraryDestinationModal').hidden=true;pendingStashLibrarySave=null}
async function confirmStashLibraryDestination(){
  if(!pendingStashLibrarySave||!ldestSelectedPath)return;
  const tab=pendingStashLibrarySave,parent=ldestSelectedPath,targetName=ldestSelectedName||PathLabel(parent);
  document.querySelector('#ldestSave').disabled=true;

  currentStashLibrarySaveAuditId=await addSaveAudit({
    kind:'save',
    status:'started',
    title:tab.title||tab.url,
    url:tab.url,
    destination:parent,
    destinationName:targetName,
    tabId:tab.id
  });

  try{
    document.querySelector('#stashlibraryDestinationModal').hidden=true;
    beginOperation('Saving to StashLibrary',`${tab.title||tab.url} → ${targetName}`);

    let r;
    if(/^https?:/i.test(tab.url)){
      r=await browser.runtime.sendMessage({type:'smart-save-current',parent,tabId:tab.id,url:tab.url,title:tab.title||''});
    }else{
      status('Saving local file — you can leave this tab.',{busy:true});
      r=await send({cmd:'add',parent,url:tab.url,name:tab.title||''});
      if(r?.ok)r={...r,saveKind:'local-file'};
    }

    if(!r?.ok)throw new Error(r?.error||'Could not save the current page to StashLibrary.');

    ldestLastPath=String(parent);
    try{await browser.storage.local.set({stashlibraryLastSaveFolder:ldestLastPath})}catch(_){}

    await updateSaveAudit(currentStashLibrarySaveAuditId,{
      status:'completed',
      saveKind:r.saveKind||'unknown',
      physicalName:r.physicalName||'',
      path:r.path||''
    });

    await refreshTree('change');
    finishOperation(`Saved to StashLibrary — “${r.physicalName||PathLabel(r.path)}”`);
    pendingStashLibrarySave=null;
    currentStashLibrarySaveAuditId='';
  }catch(e){
    await updateSaveAudit(currentStashLibrarySaveAuditId,{
      status:'failed',
      error:String(e?.message||e)
    });
    currentStashLibrarySaveAuditId='';
    errorStatus(e);
    document.querySelector('#stashlibraryDestinationModal').hidden=false;
    document.querySelector('#ldestSave').disabled=false;
  }
}
document.querySelector('#ldestSave').onclick=confirmStashLibraryDestination;
document.querySelector('#ldestCancel').onclick=closeStashLibraryDestinationPicker;
document.querySelector('#ldestClose').onclick=closeStashLibraryDestinationPicker;

let pendingZoteroSend=null,zdestNodes=[],zdestSelectedID=null,zdestSelectedName='';
function zdestNormalID(t){if(typeof t.id==='string')return t.id;if(t.objectType==='collection'||t.type==='collection')return 'C'+t.id;if(t.libraryID!=null)return 'L'+t.libraryID;return String(t.id??'')}
function renderZoteroDestinations(targets,initial){
 const tree=document.querySelector('#zdestTree');tree.innerHTML='';zdestNodes=[];const stack=[];
 (targets||[]).forEach((t,index)=>{const level=Number(t.level||0),node={index,t,id:zdestNormalID(t),name:t.name||'Unnamed',level,parent:null,children:[],expanded:false};while(stack.length&&stack.at(-1).level>=level)stack.pop();if(stack.length){node.parent=stack.at(-1);node.parent.children.push(node)}zdestNodes.push(node);stack.push(node)});
 const visible=n=>{let p=n.parent;while(p){if(!p.expanded)return false;p=p.parent}return true};
 const update=()=>{for(const n of zdestNodes){const row=tree.querySelector(`[data-zidx="${n.index}"]`);if(!row)continue;row.classList.toggle('hiddenByParent',!visible(n));const tog=row.querySelector('.zdest-toggle');if(tog)tog.textContent=n.children.length?(n.expanded?'▾':'▸'):''}};
 const select=n=>{zdestSelectedID=n.id;zdestSelectedName=n.name;tree.querySelectorAll('.zdest-row').forEach(x=>x.classList.remove('selected'));tree.querySelector(`[data-zidx="${n.index}"]`)?.classList.add('selected');document.querySelector('#zdestSelected').textContent=n.name;document.querySelector('#zdestSave').disabled=false};
 for(const n of zdestNodes){const row=document.createElement('div');row.className='zdest-row';row.dataset.zidx=n.index;const tog=document.createElement('span');tog.className='zdest-toggle';const icon=document.createElement('span');icon.className='zdest-icon';icon.textContent=n.id.startsWith('L')?'▣':'▰';const name=document.createElement('span');name.className='zdest-name';name.textContent=n.name;row.append(tog,icon,name);tog.onclick=e=>{e.stopPropagation();if(n.children.length){n.expanded=!n.expanded;update()}};row.onclick=()=>select(n);tree.append(row)}
 const init=zdestNodes.find(n=>n.id===initial);if(init){let p=init.parent;while(p){p.expanded=true;p=p.parent}select(init)}update();
 document.querySelector('#zdestExpand').onclick=()=>{zdestNodes.forEach(n=>n.expanded=true);update()};document.querySelector('#zdestCollapse').onclick=()=>{zdestNodes.forEach(n=>n.expanded=false);update()};
}
async function openZoteroDestinationPicker(pending){
 pendingZoteroSend=pending;zdestSelectedID=null;zdestSelectedName='';const modal=document.querySelector('#zoteroDestinationModal');modal.hidden=false;document.querySelector('#zdestTree').innerHTML='';document.querySelector('#zdestSelected').textContent='None';document.querySelector('#zdestSave').disabled=true;document.querySelector('#zdestStatus').textContent='Loading Zotero folders…';
 try{const r=await send({cmd:'zotero_targets'});if(!r.ok)throw new Error(r.error);if(!r.targets?.length)throw new Error('Zotero did not return any writable folders.');renderZoteroDestinations(r.targets,r.selectedTargetID);document.querySelector('#zdestStatus').textContent='Choose a destination, then click Save. Nothing is sent until you click Save.'}catch(e){
   const routedToSetup=await maybeShowOnboarding().catch(()=>false);
   if(routedToSetup){closeZoteroDestinationPicker();status('Zotero Helper setup required — continue on page 6');return}
   document.querySelector('#zdestStatus').textContent=e.message||String(e)
 }
}
function closeZoteroDestinationPicker(){document.querySelector('#zoteroDestinationModal').hidden=true;pendingZoteroSend=null}
async function confirmZoteroDestination(){
 if(!pendingZoteroSend||!zdestSelectedID)return;const pending=pendingZoteroSend,target=zdestSelectedID;document.querySelector('#zdestSave').disabled=true;
 try{if(pending.kind==='bookmark'||pending.kind==='bookmarks'){
   const items=pending.kind==='bookmarks'?(pending.items||[]):[{path:pending.path,name:pending.name}];
   beginOperation('Sending to Zotero',`${items.length} archived item${items.length===1?'':'s'} → ${zdestSelectedName}`);
   let last=null;for(let i=0;i<items.length;i++){const item=items[i];status(`Sending to Zotero — ${i+1}/${items.length} — ${item.name}`,{busy:true});last=await send({cmd:'send_to_zotero',path:item.path,target});if(!last.ok)throw new Error(last.error)}
   closeZoteroDestinationPicker();clearSelection({quiet:true});finishOperation(`Sent ${items.length} item${items.length===1?'':'s'} to ${zdestSelectedName}`)
  }else{const tabs=await browser.tabs.query({active:true,currentWindow:true}),tab=tabs[0];if(!tab?.url)throw new Error('No current page or file was found.');closeZoteroDestinationPicker();beginOperation('Sending direct to Zotero',`${tab.title||tab.url} → ${zdestSelectedName}`);const r=await browser.runtime.sendMessage({type:'direct-zotero-current',tabId:tab.id,url:tab.url,title:tab.title||'',target});if(!r?.ok)throw new Error(r?.error||'Zotero direct send failed.');finishOperation(`Sent direct to Zotero — “${r.parentTitle||r.title||r.fileName||tab.title||'item'}”`)} }catch(e){
   const routedToSetup=await maybeShowOnboarding().catch(()=>false);
   if(routedToSetup){closeZoteroDestinationPicker();status('Zotero Helper setup required — continue on page 6');return}
   errorStatus(e);document.querySelector('#zoteroDestinationModal').hidden=false;document.querySelector('#zdestSave').disabled=false
 }
}
async function sendCurrentPageToZotero(){
 try{const tabs=await browser.tabs.query({active:true,currentWindow:true}),tab=tabs[0];if(!tab?.url)throw new Error('No current page or file was found.');if(!/^(https?|file):/i.test(tab.url))throw new Error('Send direct to Zotero supports webpages and PDF/HTML files opened in Firefox.');await openZoteroDestinationPicker({kind:'direct'})}catch(e){errorStatus(e)}
}
document.querySelector('#zdestSave').onclick=confirmZoteroDestination;document.querySelector('#zdestCancel').onclick=closeZoteroDestinationPicker;document.querySelector('#zdestClose').onclick=closeZoteroDestinationPicker;

async function doUndo(){try{beginOperation('Undoing','last bookmark change');const r=await send({cmd:'undo'});if(!r.ok)throw new Error(r.error);await refreshTree('change');await refreshHistoryState(r);finishOperation(r.changed?`Undid ${r.label||'last change'}`:'Nothing to undo')}catch(e){errorStatus(e)}}
async function doRedo(){try{beginOperation('Redoing','last bookmark change');const r=await send({cmd:'redo'});if(!r.ok)throw new Error(r.error);await refreshTree('change');await refreshHistoryState(r);finishOperation(r.changed?`Redid ${r.label||'last change'}`:'Nothing to redo')}catch(e){errorStatus(e)}}


document.querySelector('#saveCurrentStashLibraryBtn').onclick=openStashLibraryDestinationPicker;
document.querySelector('#sendCurrentZoteroBtn').onclick=sendCurrentPageToZotero;
document.querySelector('#undoBtn').onclick=doUndo;
document.querySelector('#redoBtn').onclick=doRedo;

function visibleAndEnabled(selector){
  const el=document.querySelector(selector);
  return !!(el && !el.hidden && !el.disabled && el.offsetParent!==null);
}

function topVisibleOverlay(){
  const ids=['#stashlibraryDialogModal','#onboardingModal','#keyboardShortcutsModal','#stashlibraryDestinationModal','#zoteroDestinationModal','#zoteroMigrationModal','#repairModal','#faviconsDialog','#uninstallComponentsModal'];
  return ids.map(s=>document.querySelector(s)).find(el=>el && !el.hidden) || null;
}

function modalDefaultButton(){
  if(!document.querySelector('#stashlibraryDialogModal')?.hidden) return document.querySelector('#stashlibraryDialogConfirm');
  if(!document.querySelector('#onboardingModal')?.hidden){
    if(onboardingSlideIndex<4)return document.querySelector('#onboardingNext');
    if(onboardingSlideIndex===4 && !(onboardingState.helper&&onboardingState.folder))return document.querySelector('#onboardingInstallHelper');
    if(onboardingSlideIndex===5 && !onboardingState.zotero)return document.querySelector('#onboardingInstallZotero');
    return document.querySelector('#onboardingNext');
  }
  if(!document.querySelector('#stashlibraryDestinationModal')?.hidden) return document.querySelector('#ldestSave');
  if(!document.querySelector('#zoteroDestinationModal')?.hidden) return document.querySelector('#zdestSave');
  if(!document.querySelector('#zoteroMigrationModal')?.hidden){
    const destTree=document.querySelector('#zmigDestinationTree');
    if(destTree && !destTree.hidden) return document.querySelector('#zmigDestinationSelect');
    return document.querySelector('#zoteroMigrate');
  }
  if(!document.querySelector('#repairModal')?.hidden) return document.querySelector('#repairCloseX');
  if(!document.querySelector('#faviconsDialog')?.hidden) return document.querySelector('#refreshFavicons');
  return null;
}

function cancelTopDialog(){
  if(!document.querySelector('#stashlibraryDialogModal')?.hidden){document.querySelector('#stashlibraryDialogCancel')?.click();return true}
  if(!document.querySelector('#uninstallComponentsModal')?.hidden){document.querySelector('#uninstallComponentsCloseX')?.click();return true}
  if(!document.querySelector('#onboardingModal')?.hidden){return true}
  if(!document.querySelector('#keyboardShortcutsModal')?.hidden){closeShortcutManager();return true}
  if(!document.querySelector('#stashlibraryDestinationModal')?.hidden){document.querySelector('#ldestCancel')?.click();return true}
  if(!document.querySelector('#zoteroDestinationModal')?.hidden){document.querySelector('#zdestCancel')?.click();return true}
  if(!document.querySelector('#zoteroMigrationModal')?.hidden){
    const destTree=document.querySelector('#zmigDestinationTree');
    if(destTree && !destTree.hidden){document.querySelector('#zmigDestinationClose')?.click();return true}
    document.querySelector('#zmigCancel')?.click();return true
  }
  if(!document.querySelector('#faviconsDialog')?.hidden){document.querySelector('#faviconsDialogCloseX')?.click();return true}
  if(!document.querySelector('#repairModal')?.hidden){document.querySelector('#repairCloseX')?.click();return true}
  return false;
}
function keyboardBack(){
  if(cancelTopDialog())return true;
  if(!settingsPanel?.hidden){
    closeSettings();
    return true;
  }
  const live=openPanels.filter(Boolean);
  if(live.length){
    closeFrom(live.length);
    const cur=currentKeyboardItem();
    if(cur)setKeyboardCurrent(cur);
    return true;
  }
  return false;
}




function clearKeyboardCurrent(scope=document){
  scope.querySelectorAll?.('.keyboard-current').forEach(x=>x.classList.remove('keyboard-current'));
}

function setKeyboardCurrent(el,{focus=false,scroll=true}={}){
  if(!el)return false;
  clearKeyboardCurrent(document);
  el.classList.add('keyboard-current');
  if(focus && typeof el.focus==='function'){
    try{el.focus({preventScroll:true})}catch(_){try{el.focus()}catch(__){}}
  }
  if(scroll)requestAnimationFrame(()=>{try{el.scrollIntoView({block:'nearest',inline:'nearest'})}catch(_){}});
  return true;
}

function topActionButtons(){
  return [...document.querySelectorAll('#setupBtn,#topActions button')]
    .filter(b=>!b.hidden && b.offsetParent!==null);
}

function selectTopAction(btn){
  if(!btn)return false;
  clearBookmarkUrlPreview();
  keyboardTreePath='';
  const focused=document.activeElement;
  if(focused && focused!==btn){try{focused.blur()}catch(_){}}
  // Disabled buttons cannot receive DOM focus, but remain keyboard-selectable.
  return setKeyboardCurrent(btn,{focus:!btn.disabled});
}

function currentTopAction(){
  const buttons=topActionButtons();
  return buttons.find(b=>b.classList.contains('keyboard-current')) ||
         buttons.find(b=>b===document.activeElement) || null;
}

function moveTopAction(direction){
  const buttons=topActionButtons();
  if(!buttons.length)return false;
  const current=currentTopAction();
  let i=current?buttons.indexOf(current):-1;
  if(i<0)i=direction>0?-1:buttons.length;
  i=(i+direction+buttons.length)%buttons.length;
  return selectTopAction(buttons[i]);
}

function activateTopAction(){
  const btn=currentTopAction();
  if(!btn || btn.disabled)return false;
  btn.click();
  return true;
}

function focusTopActionFromTree(){
  const buttons=topActionButtons();
  if(!buttons.length)return false;
  return selectTopAction(buttons[0]);
}

function visibleItemsInPanel(panel){
  if(!panel)return [];
  return [...panel.querySelectorAll(':scope > .item')].filter(el=>el.offsetParent!==null);
}

function itemPanel(el){
  return el?.parentElement?.matches?.('.root,.flyout') ? el.parentElement : null;
}

function nodeForItem(el){
  return el?.dataset?.path ? findNodeByPath(el.dataset.path,tree) : null;
}

function keyboardSelectItem(el,{autoExpand=false}={}){
  if(!el)return false;
  const n=nodeForItem(el); if(!n)return false;
  const focused=document.activeElement;
  if(focused?.closest?.('#topActions') || focused?.closest?.('#settingsPanel')){
    try{focused.blur()}catch(_){}
  }
  keyboardTreePath=n.path;
  setKeyboardCurrent(el,{focus:false});
  if(n.type==='bookmark')showBookmarkUrlPreview(n);
  else clearBookmarkUrlPreview();
  document.querySelectorAll('.item.keyboard-selected').forEach(x=>x.classList.remove('keyboard-selected'));
  // Keyboard navigation is a cursor, not Ctrl multi-selection.
  const panel=itemPanel(el);
  const depth=panel?.classList.contains('root')?1:(openPanels.indexOf(panel)+2);

  if(autoExpand && n.type==='folder'){
    openFolder(el,n,depth);
  }else{
    // When keyboard navigation moves back onto a folder without explicitly
    // entering it, close every flyout below that folder. This keeps the
    // visible hierarchy in sync with the single keyboard highlight.
    closeFrom(depth);
    document.querySelectorAll('.item.active').forEach(x=>x.classList.remove('active'));
  }
  return true;
}

function initialiseMainTreeCursor(){
  const first=root.querySelector(':scope > .item');
  if(!first)return false;
  return keyboardSelectItem(first,{autoExpand:false});
}

function currentKeyboardItem(){
  if(keyboardTreePath){
    const exact=[...document.querySelectorAll('.item[data-path]')]
      .find(el=>el.dataset.path===keyboardTreePath && el.offsetParent!==null);
    if(exact)return exact;
  }
  return document.querySelector('.item.keyboard-current') || null;
}

function currentFlyoutOpenAll(){
  return [...document.querySelectorAll('.flyout-open-all.keyboard-current')]
    .find(btn=>btn.offsetParent!==null) || null;
}

function selectFlyoutOpenAll(btn){
  if(!btn || btn.offsetParent===null)return false;
  keyboardTreePath='';
  setKeyboardCurrent(btn,{focus:false,scroll:true});
  return true;
}

function moveMainTreeVertical(direction){
  const openAll=currentFlyoutOpenAll();

  // The Open All Bookmarks footer is the final keyboard stop in a flyout.
  // Up returns to the last bookmark/folder in that same panel; Down stays put.
  if(openAll){
    const panel=openAll.closest('.flyout');
    const rows=visibleItemsInPanel(panel);
    if(direction<0 && rows.length){
      return keyboardSelectItem(rows.at(-1),{autoExpand:false});
    }
    return true;
  }

  let cur=currentKeyboardItem();
  if(!cur){
    const first=root.querySelector(':scope > .item');
    return first ? keyboardSelectItem(first,{autoExpand:false}) : false;
  }

  const panel=itemPanel(cur);
  const rows=visibleItemsInPanel(panel);
  let i=rows.indexOf(cur);
  if(i<0)return false;

  if(direction<0 && i===0){
    // At the first root bookmark, hand navigation to the complete top action bar.
    if(panel===root)return focusTopActionFromTree();

    // At the first child, move back to its parent folder.
    const parentPath=panel?.dataset?.folder;
    const parent=parentPath ? [...document.querySelectorAll('.item[data-path]')]
      .find(el=>el.dataset.path===parentPath && el.offsetParent!==null) : null;
    if(parent)return keyboardSelectItem(parent,{autoExpand:false});
    return false;
  }

  if(direction>0 && i===rows.length-1){
    const footer=panel?.querySelector(':scope > .flyout-open-all');
    if(footer && footer.offsetParent!==null){
      return selectFlyoutOpenAll(footer);
    }
    return false;
  }

  return keyboardSelectItem(rows[i+direction],{autoExpand:false});
}

function expectedChildDirection(anchor,depth){
  // Two levels LEFT, then two levels RIGHT, repeating:
  // 1 L, 2 L, 3 R, 4 R, 5 L, 6 L, 7 R, 8 R...
  return (Math.floor((depth-1)/2)%2===0) ? -1 : 1;
}

function moveMainTreeHorizontal(direction){
  const cur=currentKeyboardItem();
  if(!cur)return false;

  const n=nodeForItem(cur);
  if(!n)return false;

  const panel=itemPanel(cur);
  const isRoot=panel?.classList.contains('root');
  const depth=isRoot ? 1 : (openPanels.indexOf(panel)+2);
  const childDirection=expectedChildDirection(cur,depth);

  // ROOT EXCEPTION:
  // Right on a root folder does nothing. Root folders only open to the left.
  if(isRoot && direction===1){
    setKeyboardCurrent(cur);
    return true;
  }

  // Arrow toward the configured child direction opens/enters that folder.
  if(direction===childDirection){
    if(n.type!=='folder')return false;

    let childPanel=openPanels[depth-1];
    let ownsChild=!!(childPanel && childPanel.dataset.folder===n.path);

    if(!ownsChild){
      openFolder(cur,n,depth);
      childPanel=openPanels[depth-1];
      ownsChild=!!(childPanel && childPanel.dataset.folder===n.path);
    }

    if(!ownsChild)return false;

    const first=childPanel.querySelector(':scope > .item');
    if(first)return keyboardSelectItem(first,{autoExpand:false});

    setKeyboardCurrent(cur);
    return true;
  }

  // On nested panels, the opposite arrow goes back to the parent.
  if(!isRoot && direction===-childDirection){
    const parentPath=panel?.dataset?.folder;
    const parent=[...document.querySelectorAll('.item[data-path]')]
      .find(el=>el.dataset.path===parentPath && el.offsetParent!==null);

    if(parent){
      closeFrom(depth-1);
      return keyboardSelectItem(parent,{autoExpand:false});
    }

    setKeyboardCurrent(cur);
    return true;
  }

  setKeyboardCurrent(cur);
  return true;
}
function activateKeyboardTreeItem(){
  const cur=currentKeyboardItem();
  if(!cur)return false;

  const n=nodeForItem(cur);
  if(!n)return false;

  if(n.type==='folder'){
    const panel=itemPanel(cur);
    const depth=panel?.classList.contains('root') ? 1 : (openPanels.indexOf(panel)+2);
    return moveMainTreeHorizontal(expectedChildDirection(cur,depth));
  }

  openBookmark(n);
  return true;
}

function dialogTreeContext(){
  if(!document.querySelector('#stashlibraryDestinationModal')?.hidden){
    const box=document.querySelector('#ldestTree');
    return {
      box,
      rows:()=>[...(box?.querySelectorAll('.zdest-row:not(.hiddenByParent)')||[])].filter(r=>r.offsetParent!==null),
      selected:()=>box?.querySelector('.zdest-row.selected')||box?.querySelector('.zdest-row.keyboard-current'),
      expand(row,want){
        const idx=Number(row?.dataset?.lidx); const n=ldestNodes[idx];
        if(!n||!n.children.length)return false;
        n.expanded=want;
        const tog=row.querySelector('.zdest-toggle');
        if(tog)tog.textContent=want?'▾':'▸';
        for(const x of ldestNodes){
          const r=box.querySelector(`[data-lidx="${x.index}"]`);
          if(!r)continue;
          let visible=true,p=x.parent;
          while(p){if(!p.expanded){visible=false;break}p=p.parent}
          r.classList.toggle('hiddenByParent',!visible);
        }
        return true;
      }
    };
  }
  if(!document.querySelector('#zoteroDestinationModal')?.hidden){
    const box=document.querySelector('#zdestTree');
    return {
      box,
      rows:()=>[...(box?.querySelectorAll('.zdest-row:not(.hiddenByParent)')||[])].filter(r=>r.offsetParent!==null),
      selected:()=>box?.querySelector('.zdest-row.selected')||box?.querySelector('.zdest-row.keyboard-current'),
      expand(row,want){
        const idx=Number(row?.dataset?.zidx); const n=zdestNodes[idx];
        if(!n||!n.children.length)return false;
        n.expanded=want;
        const visible=x=>{let q=x.parent;while(q){if(!q.expanded)return false;q=q.parent}return true};
        for(const x of zdestNodes){
          const r=box.querySelector(`[data-zidx="${x.index}"]`);if(!r)continue;
          r.classList.toggle('hiddenByParent',!visible(x));
          const tog=r.querySelector('.zdest-toggle');if(tog)tog.textContent=x.children.length?(x.expanded?'▾':'▸'):'';
        }
        return true;
      }
    };
  }
  const mig=document.querySelector('#zmigDestinationTree');
  if(mig && !mig.hidden){
    const box=document.querySelector('#zmigDestinationTreeBody');
    return {
      box,
      rows:()=>[...(box?.querySelectorAll('.zmig-dest-row')||[])].filter(r=>r.offsetParent!==null),
      selected:()=>box?.querySelector('.zmig-dest-row.pending')||box?.querySelector('.zmig-dest-row.keyboard-current'),
      expand(row,want){
        const node=row?.closest('.zmig-dest-node');
        const kids=node?.querySelector(':scope > .zmig-dest-children');
        if(!node||!kids||!kids.children.length)return false;
        setDestNodeExpanded(node,want);return true;
      }
    };
  }
  return null;
}

function dialogBoundaryControls(which){
  if(!document.querySelector('#stashlibraryDestinationModal')?.hidden){
    return which==='top'?[document.querySelector('#ldestExpand'),document.querySelector('#ldestCollapse')]:
      [document.querySelector('#ldestCancel'),document.querySelector('#ldestSave')];
  }
  if(!document.querySelector('#zoteroDestinationModal')?.hidden){
    return which==='top'?[document.querySelector('#zdestExpand'),document.querySelector('#zdestCollapse')]:
      [document.querySelector('#zdestCancel'),document.querySelector('#zdestSave')];
  }
  const mig=document.querySelector('#zmigDestinationTree');
  if(mig && !mig.hidden){
    return which==='top'?[document.querySelector('#zmigDestExpandAll'),document.querySelector('#zmigDestCollapseAll')]:
      [document.querySelector('#zmigDestinationClose'),document.querySelector('#zmigDestinationSelect')];
  }
  return [];
}
function usableControls(items){return (items||[]).filter(el=>el&&!el.hidden&&el.offsetParent!==null)}
function currentDialogBoundaryControl(){
  const all=[...usableControls(dialogBoundaryControls('top')),...usableControls(dialogBoundaryControls('bottom'))];
  return all.find(el=>el.classList.contains('keyboard-current'))||all.find(el=>el===document.activeElement)||null;
}
function selectDialogControl(el){return el?setKeyboardCurrent(el,{focus:!el.disabled}):false}
function moveDialogControlHorizontal(direction){
  const current=currentDialogBoundaryControl();if(!current)return false;
  let row=usableControls(dialogBoundaryControls('top'));
  if(!row.includes(current))row=usableControls(dialogBoundaryControls('bottom'));
  if(!row.length)return false;
  let i=row.indexOf(current);i=(i+direction+row.length)%row.length;
  return selectDialogControl(row[i]);
}
function moveFolderDialogSelection(direction){
  const ctx=dialogTreeContext();if(!ctx)return false;
  const rows=ctx.rows();if(!rows.length)return false;
  const boundary=currentDialogBoundaryControl();

  // Boundary controls and tree rows are separate keyboard states.
  // When moving back into the tree, explicitly blur the button so its DOM
  // focus cannot keep trapping subsequent Up/Down presses on that boundary.
  if(boundary){
    const top=usableControls(dialogBoundaryControls('top'));
    const bottom=usableControls(dialogBoundaryControls('bottom'));

    if(top.includes(boundary)&&direction>0){
      try{boundary.blur()}catch(_){}
      const r=rows[0];
      r.click();
      setKeyboardCurrent(r,{focus:false});
      return true;
    }

    if(bottom.includes(boundary)&&direction<0){
      try{boundary.blur()}catch(_){}
      const r=rows.at(-1);
      r.click();
      setKeyboardCurrent(r,{focus:false});
      return true;
    }

    // At a boundary, pressing farther outward stays there.
    return true;
  }

  const selected=ctx.selected();
  let i=selected?rows.indexOf(selected):-1;
  if(i<0)i=direction>0?-1:rows.length;

  // Only leave the tree after the user is already on its first/last visible row.
  if(direction<0&&i<=0){
    const top=usableControls(dialogBoundaryControls('top'));
    return top.length?selectDialogControl(top[0]):true;
  }

  if(direction>0&&i>=rows.length-1){
    const bottom=usableControls(dialogBoundaryControls('bottom'));
    return bottom.length?selectDialogControl(bottom[0]):true;
  }

  i=Math.max(0,Math.min(rows.length-1,i+direction));
  const r=rows[i];
  r.click();
  setKeyboardCurrent(r,{focus:false});
  return true;
}
function expandCollapseFolderDialog(expand){
  const ctx=dialogTreeContext(); if(!ctx)return false;
  const row=ctx.selected(); if(!row)return false;
  if(ctx.expand(row,expand)){
    setKeyboardCurrent(row); return true;
  }
  return false;
}

function moveRepairSpatial(direction){
  const modal=document.querySelector('#repairModal');
  if(!modal || modal.hidden)return false;

  const closeX=document.querySelector('#repairCloseX');
  const rescan=document.querySelector('#repairRescanResync');
  const current=[closeX,rescan].find(el=>el && (el===document.activeElement || el.classList.contains('keyboard-current')));

  if(direction==='towardX'){
    return closeX ? setKeyboardCurrent(closeX,{focus:true,scroll:false}) : false;
  }

  if(direction==='towardBody'){
    return rescan ? setKeyboardCurrent(rescan,{focus:true,scroll:false}) : false;
  }

  if(!current){
    return rescan ? setKeyboardCurrent(rescan,{focus:true,scroll:false}) : false;
  }

  return false;
}

function visibleDialogButtons(){
  const overlay=topVisibleOverlay();if(!overlay)return [];
  return [...overlay.querySelectorAll('button')].filter(b=>!b.hidden&&b.offsetParent!==null);
}
function moveGenericDialogButton(direction){
  const buttons=visibleDialogButtons();if(!buttons.length)return false;
  const current=buttons.find(b=>b.classList.contains('keyboard-current'))||buttons.find(b=>b===document.activeElement);
  let i=current?buttons.indexOf(current):-1;
  if(i<0)i=direction>0?-1:buttons.length;

  const repairOpen=!document.querySelector('#repairModal')?.hidden;
  if(repairOpen){
    // Repair/Debug does not wrap. Reaching either end simply stays there.
    const next=Math.max(0,Math.min(buttons.length-1,i+direction));
    return setKeyboardCurrent(buttons[next],{focus:!buttons[next].disabled});
  }

  i=(i+direction+buttons.length)%buttons.length;
  return setKeyboardCurrent(buttons[i],{focus:!buttons[i].disabled});
}
function activateKeyboardCurrentControl(){
  const el=document.querySelector('.keyboard-current');
  if(!el||el.offsetParent===null)return false;
  if(el.matches('button')){if(!el.disabled)el.click();return true}
  return false;
}
function settingsNavigables(){
  return [...settingsPanel.querySelectorAll('button:not([hidden]),select:not([hidden]),input:not([type="hidden"]):not([hidden])')]
    .filter(el=>el.offsetParent!==null && !el.disabled);
}


function adjustSettingsSelect(select,direction){
  if(!select || select.tagName!=='SELECT')return false;
  const opts=[...select.options].filter(o=>!o.disabled);
  if(!opts.length)return false;

  let i=opts.findIndex(o=>o.value===select.value);
  if(i<0)i=0;
  const next=Math.max(0,Math.min(opts.length-1,i+direction));
  if(next===i)return true;

  select.value=opts[next].value;
  select.dispatchEvent(new Event('change',{bubbles:true}));
  setKeyboardCurrent(select,{focus:true,scroll:false});
  return true;
}

function moveSettingsFocus(direction){
  const items=settingsNavigables(); if(!items.length)return false;
  const active=document.activeElement;
  let i=items.indexOf(active);
  if(i<0){
    const marked=items.findIndex(x=>x.classList.contains('keyboard-current'));
    i=marked>=0?marked:(direction>0?-1:items.length);
  }
  i=(i+direction+items.length)%items.length;
  const target=items[i];
  const ok=setKeyboardCurrent(target,{focus:true});
  if(ok && (target?.id==='donateBtn' || target?.id==='aboutVersionEasterEgg')){
    requestAnimationFrame(()=>{
      const scroller=document.querySelector('.settings-scroll');
      if(!scroller)return;
      if(target.id==='donateBtn')scroller.scrollTop=scroller.scrollHeight;
      else scroller.scrollTop=0;
    });
  }
  return ok;
}

function keyboardActionBlocked(){
  const a=document.activeElement;
  if(a && (a.matches?.('input,textarea,select,[contenteditable="true"]') || a.closest?.('[contenteditable="true"]'))) return true;
  if(!document.querySelector('#zoteroDestinationModal')?.hidden) return true;
  if(!document.querySelector('#stashlibraryDestinationModal')?.hidden) return true;
  return false;
}
function selectedPasteFolder(){
  // Paste destinations follow the folder the user is currently navigating,
  // rather than the Ctrl/Shift multi-selection. The multi-selection normally
  // still contains the source items after Copy/Cut, so requiring it to contain
  // exactly one folder made Ctrl+V fail (or target the source) in nested
  // flyouts.
  //
  // Mouse navigation marks the currently hovered/open folder as `.active`.
  const active=[...document.querySelectorAll('.item.active[data-path]')]
    .filter(el=>el.offsetParent!==null)
    .at(-1);
  const activeNode=nodeForItem(active);
  if(activeNode?.type==='folder')return activeNode;

  // Keyboard navigation has its own cursor and does not use selectedPaths.
  const keyboardNode=nodeForItem(currentKeyboardItem());
  if(keyboardNode?.type==='folder')return keyboardNode;

  // A normal click records an anchor even though it deliberately does not add
  // the folder to the Ctrl multi-selection. Keep that as a final fallback.
  const anchorNode=selectionAnchorPath?findNodeByPath(selectionAnchorPath,tree):null;
  return anchorNode?.type==='folder' ? anchorNode : null;
}
async function runSelectionShortcut(action){
  const nodes=topLevelSelectedNodes();
  if(action==='paste'){
    const dest=selectedPasteFolder();
    if(!dest || !clipboard){
      status(!clipboard?'Nothing to paste — copy or cut something first':'Choose or highlight a destination folder, then press Ctrl+V',{hold:2600});
      return;
    }
    await doAction('paste',dest,dest.path,[dest]);
    return;
  }
  if(!nodes.length)return;
  const first=nodes[0];
  const folder=first.type==='folder'?first.path:PathParent(first.path);
  await doAction(action,first,folder,nodes);
}

let keyboardContextOrigin=null;

function keyboardContextMenuButtons(){
  const menu=document.querySelector('#ctx');
  if(!menu || menu.hidden)return [];
  return [...menu.querySelectorAll('button,[role="menuitem"]')].filter(el=>{
    if(el.hidden)return false;
    if(el.disabled)return false;
    const style=getComputedStyle(el);
    return style.display!=='none' && style.visibility!=='hidden';
  });
}

function clearKeyboardContextHighlight(){
  const menu=document.querySelector('#ctx');
  if(!menu)return;
  menu.querySelectorAll('.keyboard-current,.keyboard-selected').forEach(el=>{
    el.classList.remove('keyboard-current','keyboard-selected');
  });
}

function selectKeyboardContextButton(button){
  if(!button)return;

  // Context-menu focus must be exclusive: once the menu owns the keyboard
  // cursor, remove keyboard highlighting from the bookmark/folder tree.
  document.querySelectorAll('.keyboard-current,.keyboard-selected').forEach(el=>{
    if(!el.closest('#ctx'))el.classList.remove('keyboard-current','keyboard-selected');
  });

  clearKeyboardContextHighlight();
  button.classList.add('keyboard-current');
  try{button.focus({preventScroll:true})}catch(_){try{button.focus()}catch(_){}}
}

function currentKeyboardContextButton(){
  const menu=document.querySelector('#ctx');
  if(!menu || menu.hidden)return null;
  const active=document.activeElement;
  if(active && menu.contains(active))return active;
  return menu.querySelector('.keyboard-current');
}

function closeKeyboardContextMenu(){
  const menu=document.querySelector('#ctx');
  if(!menu)return;

  clearKeyboardContextHighlight();
  menu.hidden=true;

  // Return the keyboard cursor to the bookmark/folder that opened the menu.
  if(keyboardContextOrigin && document.contains(keyboardContextOrigin)){
    document.querySelectorAll('.keyboard-current,.keyboard-selected').forEach(el=>{
      if(el!==keyboardContextOrigin)el.classList.remove('keyboard-current','keyboard-selected');
    });
    keyboardContextOrigin.classList.add('keyboard-current');
    try{keyboardContextOrigin.focus({preventScroll:true})}catch(_){}
  }
}

function keyboardContextAnchor(){
  // Prefer the actual keyboard-highlighted bookmark/folder.
  const flyouts=[...document.querySelectorAll('.flyout')].filter(el=>!el.hidden);
  for(let i=flyouts.length-1;i>=0;i--){
    const hit=flyouts[i].querySelector('.keyboard-current,.keyboard-selected');
    if(hit)return hit;
  }
  return document.querySelector('.bookmark-row.keyboard-current,.bookmark-row.keyboard-selected,.folder-row.keyboard-current,.folder-row.keyboard-selected,[data-node-id].keyboard-current,[data-node-id].keyboard-selected');
}

function openKeyboardContextMenuForCurrent(){
  const menu=document.querySelector('#ctx');
  const anchor=keyboardContextAnchor();
  if(!menu || !anchor)return false;

  keyboardContextOrigin=anchor;

  // Reuse the same context-menu setup as a real right click whenever possible.
  try{
    const rect=anchor.getBoundingClientRect();
    anchor.dispatchEvent(new MouseEvent('contextmenu',{
      bubbles:true,
      cancelable:true,
      clientX:Math.round(rect.right-4),
      clientY:Math.round(rect.top+Math.min(rect.height/2,12))
    }));
  }catch(_){}

  if(menu.hidden)return false;

  // Root-level keyboard context menus sit 200px lower than the normal anchor
  // when opened with Shift. Nested flyout items keep the regular alignment.
  const isRoot = !!anchor.closest('.root-panel,.root-list,#rootPanel,#bookmarksRoot') &&
                 !anchor.closest('.flyout:not(.root-panel):not(#rootPanel)');
  if(isRoot){
    const top=parseFloat(menu.style.top||'');
    if(Number.isFinite(top))menu.style.top=`${top+200}px`;
  }

  const buttons=keyboardContextMenuButtons();
  if(buttons.length)selectKeyboardContextButton(buttons[0]);
  return true;
}

document.addEventListener('keydown',e=>{
  const _ctx=document.querySelector('#ctx');

  // Reserve bare Alt for StashLibrary while its window has focus. Do not stop
  // propagation here: the context-menu handler below still needs the event.
  if(String(e.key||'')==='Alt' && !e.ctrlKey && !e.shiftKey && !e.metaKey)e.preventDefault();

  // Keep Ctrl+C available for bookmark records when no text is highlighted,
  // but let the browser copy a real text selection to the system clipboard.
  const selectedText=String(window.getSelection?.()?.toString()||'');
  if((e.ctrlKey||e.metaKey) && String(e.key||'').toLowerCase()==='c' && selectedText)return;

  // When the context menu is open, its shortcut or Enter toggles it closed.
  // Up/Down navigate directly through enabled menu entries.
  if(_ctx && !_ctx.hidden){
    if(matchesLocalShortcut(e,'contextMenu') || matchesLocalShortcut(e,'activate')){
      e.preventDefault();
      e.stopPropagation();
      closeKeyboardContextMenu();
      return;
    }

    // Left/Right are deliberately disabled while the right-click menu is open.
    if(matchesLocalShortcut(e,'collapse') || matchesLocalShortcut(e,'expand')){
      e.preventDefault();
      e.stopPropagation();
      return;
    }

    if(matchesLocalShortcut(e,'moveDown') || matchesLocalShortcut(e,'moveUp')){
      e.preventDefault();
      e.stopPropagation();
      const buttons=keyboardContextMenuButtons();
      if(!buttons.length)return;
      const current=currentKeyboardContextButton();
      let index=Math.max(0,buttons.indexOf(current));
      index=(index+(matchesLocalShortcut(e,'moveDown')?1:-1)+buttons.length)%buttons.length;
      selectKeyboardContextButton(buttons[index]);
      return;
    }
  }

  const k=String(e.key||'').toLowerCase();

  // Ctrl+W closes the active page tab through the background script, then
  // reopens/refocuses StashLibrary so keyboard shortcuts work immediately on the next tab.
  if(matchesLocalShortcut(e,'closeTab')){
    e.preventDefault();
    e.stopPropagation();

    browser.runtime.sendMessage({type:'close-active-tab-keep-stashlibrary'})
      .then(r=>{
        if(!r?.ok)throw new Error(r?.error||'Could not close the active tab.');
        scheduleStashLibraryKeyboardFocusRestore();
      })
      .catch(errorStatus);
    return;
  }
  const a=document.activeElement;
  const typing=!!(a && (a.matches?.('input[type="text"],input[type="search"],input[type="number"],textarea,[contenteditable="true"]') || a.closest?.('[contenteditable="true"]')));
  const overlay=topVisibleOverlay();
  const settingsOpen=!settingsPanel?.hidden;

  const shortcutsOpen=!document.querySelector('#keyboardShortcutsModal')?.hidden;
  const shortcutEditing=shortcutsOpen && !document.querySelector('#shortcutEditArea')?.hidden;

  // Alt (or the dedicated Menu key) opens the same context menu as right-click.
  // It does not fire while typing or inside settings/dialogs.
  if(matchesLocalShortcut(e,'contextMenu') && !e.repeat && !typing && !overlay && !settingsOpen){
    if(openKeyboardContextMenu()){
      e.preventDefault();e.stopPropagation();return;
    }
  }

  // Once the context menu is open it becomes its own keyboard surface.
  if(!ctx.hidden && !typing){
    if(matchesLocalShortcut(e,'moveDown')){
      e.preventDefault();e.stopPropagation();moveContextMenuSelection(1);return;
    }
    if(matchesLocalShortcut(e,'moveUp')){
      e.preventDefault();e.stopPropagation();moveContextMenuSelection(-1);return;
    }
    if(matchesLocalShortcut(e,'activate')){
      const btn=contextMenuButtons().find(b=>b.classList.contains('keyboard-current')) ||
                contextMenuButtons().find(b=>b===document.activeElement);
      if(btn){e.preventDefault();e.stopPropagation();btn.click();return;}
    }
    if(matchesLocalShortcut(e,'backClose')){
      e.preventDefault();e.stopPropagation();closeKeyboardContextMenu();return;
    }
  }

  if(shortcutEditing && !typing){
    const active=document.activeElement;
    const onButton=!!shortcutEditCurrentButton();

    if(onButton && (matchesLocalShortcut(e,'collapse')||matchesLocalShortcut(e,'expand'))){
      if(moveShortcutEditButton(matchesLocalShortcut(e,'expand')?1:-1)){
        e.preventDefault();e.stopPropagation();return;
      }
    }
    if(onButton && matchesLocalShortcut(e,'moveUp')){
      const input=document.querySelector('#shortcutCaptureInput');
      if(input){
        clearKeyboardCurrent(document);
        input.focus();
        e.preventDefault();e.stopPropagation();return;
      }
    }
    if(onButton && matchesLocalShortcut(e,'activate')){
      const btn=shortcutEditCurrentButton();
      if(btn && !btn.disabled){
        e.preventDefault();e.stopPropagation();btn.click();return;
      }
    }
  }

  if(shortcutsOpen && !shortcutEditing && !typing){
    const closeX=document.querySelector('#keyboardShortcutsCloseX');
    const active=document.activeElement;
    const boundary=shortcutBoundarySelection();

    const activeEdit=active?.classList?.contains('shortcut-row-edit') ? active : null;
    if(activeEdit){
      const row=activeEdit.closest('.shortcut-row');
      const idx=Number(row?.dataset?.shortcutIndex);
      if(Number.isInteger(idx))shortcutRowIndex=idx;
    }

    const editBtn=currentShortcutEditButton();
    const onEdit=!!activeEdit || !!(editBtn && editBtn.classList.contains('keyboard-current'));

    // Spatial model:
    //   Up / Right  -> toward the X
    //   Left / Down -> toward the shortcut value/list
    if(boundary==='close'){
      if(matchesLocalShortcut(e,'collapse') || matchesLocalShortcut(e,'moveDown')){
        e.preventDefault();e.stopPropagation();
        try{closeX?.blur()}catch(_){}
        closeX?.classList.remove('keyboard-current');
        selectShortcutRow(0,false);
        focusCurrentShortcutEdit();
        return;
      }
      if(matchesLocalShortcut(e,'moveUp') || matchesLocalShortcut(e,'expand')){
        e.preventDefault();e.stopPropagation();
        return;
      }
      if(matchesLocalShortcut(e,'activate') || matchesLocalShortcut(e,'backClose')){
        e.preventDefault();e.stopPropagation();
        closeX?.click();
        return;
      }
    }

    if(onEdit){
      if(matchesLocalShortcut(e,'moveUp') || matchesLocalShortcut(e,'expand')){
        e.preventDefault();e.stopPropagation();
        selectShortcutCloseX();
        return;
      }
      if(matchesLocalShortcut(e,'moveDown')){
        e.preventDefault();e.stopPropagation();
        if(shortcutRowIndex<shortcutRowsState.length-1){
          selectShortcutRow(shortcutRowIndex+1,false);
          focusCurrentShortcutEdit();
        }
        return;
      }
      if(matchesLocalShortcut(e,'collapse')){
        e.preventDefault();e.stopPropagation();
        // Left means stay in the Edit/list side; do not jump to X.
        if(shortcutRowIndex>0){
          selectShortcutRow(shortcutRowIndex-1,false);
          focusCurrentShortcutEdit();
        }
        return;
      }
      if(matchesLocalShortcut(e,'activate')){
        e.preventDefault();e.stopPropagation();
        editBtn?.click();
        return;
      }
    }

    // If nothing is focused yet, Down/Left starts at the shortcut value button.
    if(matchesLocalShortcut(e,'moveDown') || matchesLocalShortcut(e,'collapse')){
      e.preventDefault();e.stopPropagation();
      selectShortcutRow(Math.max(0,shortcutRowIndex),false);
      focusCurrentShortcutEdit();
      return;
    }

    // Up/Right from the list side heads toward the X.
    if(matchesLocalShortcut(e,'moveUp') || matchesLocalShortcut(e,'expand')){
      e.preventDefault();e.stopPropagation();
      selectShortcutCloseX();
      return;
    }

    if(matchesLocalShortcut(e,'activate')){
      e.preventDefault();e.stopPropagation();
      editBtn?.click();
      return;
    }
  }
  // Repair/Debug uses the same spatial rule:
  // Up -> X, Down -> Rescan/body.
  if(!typing && overlay && !document.querySelector('#repairModal')?.hidden &&
     (matchesLocalShortcut(e,'moveUp')||matchesLocalShortcut(e,'moveDown'))){
    e.preventDefault();e.stopPropagation();
    if(matchesLocalShortcut(e,'moveUp'))moveRepairSpatial('towardX');
    else moveRepairSpatial('towardBody');
    return;
  }

  // Dialog arrows form one continuous keyboard surface: top controls -> tree -> bottom controls.
  if(!typing && overlay && (matchesLocalShortcut(e,'moveUp')||matchesLocalShortcut(e,'moveDown'))){
    if(dialogTreeContext()){
      if(moveFolderDialogSelection(matchesLocalShortcut(e,'moveDown')?1:-1)){e.preventDefault();e.stopPropagation();return}
    }else if(moveGenericDialogButton(matchesLocalShortcut(e,'moveDown')?1:-1)){
      e.preventDefault();e.stopPropagation();return;
    }
  }
  if(!typing && overlay && (matchesLocalShortcut(e,'collapse')||matchesLocalShortcut(e,'expand'))){
    if(!document.querySelector('#repairModal')?.hidden){
      e.preventDefault();e.stopPropagation();
      if(matchesLocalShortcut(e,'expand'))moveRepairSpatial('towardX');
      else moveRepairSpatial('towardBody');
      return;
    }
    if(currentDialogBoundaryControl()){
      if(moveDialogControlHorizontal(matchesLocalShortcut(e,'expand')?1:-1)){e.preventDefault();e.stopPropagation();return}
    }else if(dialogTreeContext()){
      if(expandCollapseFolderDialog(matchesLocalShortcut(e,'expand'))){e.preventDefault();e.stopPropagation();return}
    }else if(moveGenericDialogButton(matchesLocalShortcut(e,'expand')?1:-1)){
      e.preventDefault();e.stopPropagation();return;
    }
  }

  // The configurable Back / Close command defaults to Backspace only.
  // It is never intercepted while typing/editing text.
  if(matchesLocalShortcut(e,'backClose') && !typing){
    if(keyboardBack()){
      e.preventDefault();e.stopPropagation();return;
    }
  }

  // In folder picker dialogs, Enter on the currently selected folder confirms it.
  // This includes a folder restored from the previous Save-to-StashLibrary choice:
  // it is a real selected row even if the user has not clicked it again.
  if(matchesLocalShortcut(e,'activate') && !typing && overlay && dialogTreeContext()){
    const ctx=dialogTreeContext();
    const selected=ctx?.selected?.();

    if(selected){
      let saveBtn=null;

      if(!document.querySelector('#stashlibraryDestinationModal')?.hidden){
        saveBtn=document.querySelector('#ldestSave');
      }else if(!document.querySelector('#zoteroDestinationModal')?.hidden){
        saveBtn=document.querySelector('#zdestSave');
      }else{
        const mig=document.querySelector('#zmigDestinationTree');
        if(mig && !mig.hidden)saveBtn=document.querySelector('#zmigDestinationSelect');
      }

      if(saveBtn && !saveBtn.disabled && saveBtn.offsetParent!==null){
        e.preventDefault();
        e.stopPropagation();
        saveBtn.click();
        return;
      }
    }
  }

  // Enter activates a specifically highlighted dialog button first; otherwise use the normal default.
  if(matchesLocalShortcut(e,'activate') && !typing && overlay){
    if(activateKeyboardCurrentControl()){e.preventDefault();e.stopPropagation();return}
    const btn=modalDefaultButton();
    if(btn && !btn.disabled && btn.offsetParent!==null){
      e.preventDefault();e.stopPropagation();btn.click();return;
    }
  }

  // Settings keyboard model:
  // Up/Down moves between controls and never changes a dropdown value.
  // Left/Right changes a focused dropdown; on other controls it moves focus.
  if(!typing && settingsOpen && !overlay){
    const active=document.activeElement;

    if(matchesLocalShortcut(e,'moveDown')){
      if(moveSettingsFocus(1)){e.preventDefault();e.stopPropagation();return}
    }
    if(matchesLocalShortcut(e,'moveUp')){
      if(moveSettingsFocus(-1)){e.preventDefault();e.stopPropagation();return}
    }

    if(matchesLocalShortcut(e,'expand')){
      if(active?.tagName==='SELECT'){
        if(adjustSettingsSelect(active,1)){e.preventDefault();e.stopPropagation();return}
      }else if(moveSettingsFocus(1)){
        e.preventDefault();e.stopPropagation();return;
      }
    }
    if(matchesLocalShortcut(e,'collapse')){
      if(active?.tagName==='SELECT'){
        if(adjustSettingsSelect(active,-1)){e.preventDefault();e.stopPropagation();return}
      }else if(moveSettingsFocus(-1)){
        e.preventDefault();e.stopPropagation();return;
      }
    }
  }

  if(keyboardActionBlocked())return;

  if(!typing && !settingsOpen && !overlay){
    // Configurable main-screen shortcuts focus the matching top action.
    if(eventMatchesShortcut(e,localShortcuts.saveStashLibrary)){
      e.preventDefault();e.stopPropagation();selectTopAction(document.querySelector('#saveCurrentStashLibraryBtn'));return;
    }
    if(eventMatchesShortcut(e,localShortcuts.sendZotero)){
      e.preventDefault();e.stopPropagation();selectTopAction(document.querySelector('#sendCurrentZoteroBtn'));return;
    }

    const inTop=!!a?.closest?.('#topActions') || !!document.querySelector('#topActions .keyboard-current');
    if(inTop && (matchesLocalShortcut(e,'collapse')||matchesLocalShortcut(e,'expand'))){
      if(moveTopAction(matchesLocalShortcut(e,'expand')?1:-1)){
        e.preventDefault();e.stopPropagation();return;
      }
    }
    if(inTop && matchesLocalShortcut(e,'moveUp')){
      if(moveTopAction(1)){
        e.preventDefault();e.stopPropagation();return;
      }
    }
    if(inTop && matchesLocalShortcut(e,'moveDown')){
      const first=root.querySelector(':scope > .item');
      if(first){
        e.preventDefault();e.stopPropagation();keyboardSelectItem(first);return;
      }
    }
    if(inTop && matchesLocalShortcut(e,'activate')){
      if(activateTopAction()){
        e.preventDefault();e.stopPropagation();return;
      }
    }

    // Main bookmark tree navigation.
    if(matchesLocalShortcut(e,'moveUp')||matchesLocalShortcut(e,'moveDown')){
      if(moveMainTreeVertical(matchesLocalShortcut(e,'moveDown')?1:-1)){
        e.preventDefault();e.stopPropagation();return;
      }
    }
    if(matchesLocalShortcut(e,'collapse')||matchesLocalShortcut(e,'expand')){
      if(!currentKeyboardItem() && !currentTopAction()){
        if(initialiseMainTreeCursor()){
          e.preventDefault();e.stopPropagation();return;
        }
      }
      if(moveMainTreeHorizontal(matchesLocalShortcut(e,'expand')?1:-1)){
        e.preventDefault();e.stopPropagation();return;
      }
    }
    if(matchesLocalShortcut(e,'activate') && currentFlyoutOpenAll()){
      e.preventDefault();e.stopPropagation();
      currentFlyoutOpenAll().click();
      return;
    }
    if(matchesLocalShortcut(e,'activate') && currentKeyboardItem()){
      if(activateKeyboardTreeItem()){
        e.preventDefault();e.stopPropagation();return;
      }
    }
  }

  if(eventMatchesShortcut(e,localShortcuts.delete)){
    // Delete follows the visible keyboard cursor when there is no explicit
    // Ctrl/Shift multi-selection. This is especially important in deep flyouts,
    // where arrow-key navigation highlights an item without adding it to
    // selectedPaths.
    if(selectedPaths.size){
      e.preventDefault();e.stopPropagation();
      runSelectionShortcut('delete');
      return;
    }

    const keyboardItem=currentKeyboardItem();
    const keyboardNode=nodeForItem(keyboardItem);
    if(keyboardNode){
      e.preventDefault();e.stopPropagation();
      const folder=keyboardNode.type==='folder'
        ? keyboardNode.path
        : PathParent(keyboardNode.path);
      doAction('delete',keyboardNode,folder,[keyboardNode]);
      return;
    }
  }
  if(eventMatchesShortcut(e,localShortcuts.selectAll)){e.preventDefault();e.stopPropagation();selectAllTreeItems();return}
  if(eventMatchesShortcut(e,localShortcuts.undo)){e.preventDefault();e.stopPropagation();doUndo();return}
  if(eventMatchesShortcut(e,localShortcuts.redo)){e.preventDefault();e.stopPropagation();doRedo();return}
  if(eventMatchesShortcut(e,localShortcuts.copy)){e.preventDefault();e.stopPropagation();runSelectionShortcut('copy');return}
  if(eventMatchesShortcut(e,localShortcuts.cut)){e.preventDefault();e.stopPropagation();runSelectionShortcut('cut');return}
  if(eventMatchesShortcut(e,localShortcuts.paste)){e.preventDefault();e.stopPropagation();runSelectionShortcut('paste');return}
  if(matchesLocalShortcut(e,'escapeApp') && !typing){e.preventDefault();e.stopPropagation();try{window.close()}catch(_){};return}
});

// Firefox commonly toggles its menu bar when a bare Alt key is released.
// Consume that matching release while StashLibrary has focus so Alt remains an
// app-local context-menu shortcut. Modified Alt combinations are untouched.
document.addEventListener('keyup',e=>{
  if(String(e.key||'')==='Alt' && !e.ctrlKey && !e.shiftKey && !e.metaKey){
    e.preventDefault();
    e.stopPropagation();
  }
},true);

const LOCAL_SHORTCUT_DEFAULTS={
  saveStashLibrary:'S',
  sendZotero:'Z',
  undo:'Ctrl+Z',
  redo:'Ctrl+Y',
  copy:'Ctrl+C',
  cut:'Ctrl+X',
  paste:'Ctrl+V',
  selectAll:'Ctrl+A',
  delete:'Delete',
  backClose:'Backspace',
  escapeApp:'Esc',
  activate:'Enter',
  moveUp:'Up',
  moveDown:'Down',
  collapse:'Left',
  expand:'Right',
  contextMenu:'Alt',
  closeTab:'Ctrl+W'
};
let localShortcuts={...LOCAL_SHORTCUT_DEFAULTS};
let shortcutRowsState=[],shortcutRowIndex=0,shortcutEditingKey='',shortcutPendingValue='';
const shortcutPressedKeys=new Set();

function shortcutEventString(e,{allowBare=true}={}){
  const modifierOnly={Control:'Ctrl',Shift:'Shift',Alt:'Alt',Meta:'Command',OS:'Command'};
  let key=String(e.key||'');
  if(modifierOnly[key])return modifierOnly[key];
  const parts=[];
  if(e.ctrlKey)parts.push('Ctrl');
  if(e.altKey)parts.push('Alt');
  if(e.shiftKey)parts.push('Shift');
  if(e.metaKey)parts.push('Command');
  if(key===' ')key='Space';
  else if(key==='Escape')key='Esc';
  else if(key==='ArrowUp')key='Up';
  else if(key==='ArrowDown')key='Down';
  else if(key==='ArrowLeft')key='Left';
  else if(key==='ArrowRight')key='Right';
  else if(key.length===1)key=key.toUpperCase();
  if(!allowBare && key.length===1 && !e.ctrlKey && !e.altKey && !e.metaKey)return '';
  if(!key)return '';
  parts.push(key);
  return parts.join('+');
}
function eventMatchesShortcut(e,shortcut){
  const wanted=String(shortcut||'').trim();
  if(!wanted)return false;
  const actual=shortcutEventString(e,{allowBare:true});
  return !!actual && actual.toLowerCase()===wanted.toLowerCase();
}
function matchesLocalShortcut(e,key){
  if(key==='contextMenu' && String(e.key||'')==='ContextMenu')return true;
  return eventMatchesShortcut(e,localShortcuts?.[key]);
}
async function loadLocalShortcuts(){
  localShortcuts={...LOCAL_SHORTCUT_DEFAULTS};
  try{
    const stored=(await browser.storage.local.get('stashlibraryLocalShortcuts'))?.stashlibraryLocalShortcuts;
    let migrated=false;
    if(stored && typeof stored==='object'){
      for(const key of Object.keys(LOCAL_SHORTCUT_DEFAULTS)){
        if(typeof stored[key]==='string')localShortcuts[key]=stored[key];
      }
      if(String(stored.saveStashLibrary||'').toUpperCase()==='L'){
        localShortcuts.saveStashLibrary='S';
        migrated=true;
      }
      if(['shift','shift+f10'].includes(String(stored.contextMenu||'').toLowerCase())){
        localShortcuts.contextMenu='Alt';
        migrated=true;
      }
    }
    if(migrated)await browser.storage.local.set({stashlibraryLocalShortcuts:localShortcuts});
  }catch(_){}
}
loadLocalShortcuts();

async function getGlobalStashLibraryShortcut(){
  try{
    const commands=await browser.commands.getAll();
    return commands.find(x=>x.name==='toggle-stashlibrary')?.shortcut||'';
  }catch(_){return ''}
}

async function buildShortcutRows(){
  const globalShortcut=await getGlobalStashLibraryShortcut();
  shortcutRowsState=[
    {key:'toggle-stashlibrary',label:'Open / Close StashLibrary',value:globalShortcut,global:true,editable:true},
    {key:'saveStashLibrary',label:'Add to StashLibrary',value:localShortcuts.saveStashLibrary,editable:true},
    {key:'sendZotero',label:'Send to Zotero',value:localShortcuts.sendZotero,editable:true},
    {key:'undo',label:'Undo',value:localShortcuts.undo,editable:true},
    {key:'redo',label:'Redo',value:localShortcuts.redo,editable:true},
    {key:'copy',label:'Copy',value:localShortcuts.copy,editable:true},
    {key:'cut',label:'Cut',value:localShortcuts.cut,editable:true},
    {key:'paste',label:'Paste',value:localShortcuts.paste,editable:true},
    {key:'selectAll',label:'Select All',value:localShortcuts.selectAll,editable:true},
    {key:'delete',label:'Delete',value:localShortcuts.delete,editable:true},
    {key:'backClose',label:'Back / Close',value:localShortcuts.backClose,editable:true},
    {key:'escapeApp',label:'Exit StashLibrary',value:localShortcuts.escapeApp,editable:true},
    {key:'activate',label:'Activate / Confirm',value:localShortcuts.activate,editable:true},
    {key:'moveUp',label:'Move Up',value:localShortcuts.moveUp,editable:true},
    {key:'moveDown',label:'Move Down',value:localShortcuts.moveDown,editable:true},
    {key:'collapse',label:'Collapse / Move Left',value:localShortcuts.collapse,editable:true},
    {key:'expand',label:'Expand / Move Right',value:localShortcuts.expand,editable:true},
    {key:'contextMenu',label:'Open Context Menu (Menu key also works)',value:localShortcuts.contextMenu,editable:true},
    {key:'closeTab',label:'Close Active Browser Tab',value:localShortcuts.closeTab,editable:true}
  ];
}
function renderShortcutRows(){
  const box=document.querySelector('#shortcutRows');
  box.innerHTML='';

  shortcutRowsState.forEach((row,i)=>{
    const el=document.createElement('div');
    el.className='shortcut-row';
    el.dataset.shortcutIndex=String(i);
    el.setAttribute('role','option');

    const label=document.createElement('div');
    label.className='shortcut-row-label';
    label.textContent=row.label;

    let value;
    if(row.editable){
      value=document.createElement('button');
      value.type='button';
      value.className='shortcut-row-value shortcut-row-edit';
      value.textContent=row.value||'Not assigned';
      value.title='Edit this shortcut';
      value.setAttribute('aria-label',`${row.label}: ${row.value||'Not assigned'}. Edit shortcut`);
      value.onclick=ev=>{
        ev.stopPropagation();
        selectShortcutRow(i,false);
        beginShortcutEdit();
      };
    }else{
      value=document.createElement('div');
      value.className='shortcut-row-value';
      value.textContent=row.value||'Not assigned';
    }

    el.append(label,value);
    el.onclick=()=>selectShortcutRow(i);
    box.append(el);
  });

  selectShortcutRow(Math.min(shortcutRowIndex,Math.max(0,shortcutRowsState.length-1)),false);
}
async function revertShortcutRow(index=shortcutRowIndex){
  const row=shortcutRowsState[index];
  if(!row?.editable)return false;
  try{
    if(row.global){
      await browser.commands.reset('toggle-stashlibrary');
    }else{
      localShortcuts[row.key]=LOCAL_SHORTCUT_DEFAULTS[row.key]||'';
      await browser.storage.local.set({stashlibraryLocalShortcuts:localShortcuts});
    }
    await buildShortcutRows();
    renderShortcutRows();
    selectShortcutRow(index);
    return true;
  }catch(_){
    return false;
  }
}


async function revertAllShortcuts(){
  try{
    try{await browser.commands.reset('toggle-stashlibrary')}catch(_){}
    localShortcuts={...LOCAL_SHORTCUT_DEFAULTS};
    await browser.storage.local.set({stashlibraryLocalShortcuts:localShortcuts});
    await buildShortcutRows();
    shortcutRowIndex=0;
    renderShortcutRows();
    status('Keyboard shortcuts reverted to defaults',{hold:1800});
    return true;
  }catch(e){
    errorStatus(e);
    return false;
  }
}



function focusCurrentShortcutEdit(){
  const row=document.querySelector(`#shortcutRows [data-shortcut-index="${shortcutRowIndex}"]`);
  const edit=row?.querySelector('.shortcut-row-edit');
  if(!edit)return false;

  clearKeyboardCurrent(document.querySelector('#keyboardShortcutsModal')||document);
  return setKeyboardCurrent(edit,{focus:true,scroll:false});
}

function currentShortcutEditButton(){
  return document.querySelector(`#shortcutRows [data-shortcut-index="${shortcutRowIndex}"] .shortcut-row-edit`);
}

function shortcutBoundarySelection(){
  const close=document.querySelector('#keyboardShortcutsCloseX');
  if(!close)return '';
  if(close===document.activeElement || close.classList.contains('keyboard-current'))return 'close';
  return '';
}

function selectShortcutCloseX(){
  const target=document.querySelector('#keyboardShortcutsCloseX');
  if(!target)return false;

  document.querySelectorAll('#shortcutRows .shortcut-row').forEach(el=>{
    el.classList.remove('keyboard-current');
    el.setAttribute('aria-selected','false');
  });
  document.querySelectorAll('#shortcutRows .shortcut-row-edit').forEach(btn=>{
    btn.classList.remove('keyboard-current','shortcut-edit-paired');
  });

  const modal=document.querySelector('#keyboardShortcutsModal');
  if(modal?.contains(document.activeElement) && document.activeElement!==target){
    try{document.activeElement.blur()}catch(_){}
  }
  clearKeyboardCurrent(modal||document);
  return setKeyboardCurrent(target,{focus:true,scroll:false});
}



function selectShortcutRow(i,scroll=true){
  if(!shortcutRowsState.length)return false;
  shortcutRowIndex=Math.max(0,Math.min(shortcutRowsState.length-1,i));

  const modal=document.querySelector('#keyboardShortcutsModal');
  const closeX=document.querySelector('#keyboardShortcutsCloseX');
  if(modal?.contains(document.activeElement)){
    try{document.activeElement.blur()}catch(_){}
  }
  closeX?.classList.remove('keyboard-current');
  clearKeyboardCurrent(modal||document);

  // Rows are not visually highlighted. The index is only used for navigation.
  document.querySelectorAll('#shortcutRows .shortcut-row').forEach(el=>{
    el.classList.remove('keyboard-current');
    el.removeAttribute('aria-selected');
  });

  const current=document.querySelector(`#shortcutRows [data-shortcut-index="${shortcutRowIndex}"]`);
  if(scroll&&current)current.scrollIntoView({block:'nearest'});
  return true;
}

async function openShortcutManager(){
  await loadLocalShortcuts();
  await buildShortcutRows();
  shortcutRowIndex=0;
  document.querySelector('#shortcutEditArea').hidden=true;
  document.querySelector('#keyboardShortcutsModal').hidden=false;
  renderShortcutRows();
}

function closeShortcutManager(){
  document.querySelector('#keyboardShortcutsModal').hidden=true;
  document.querySelector('#shortcutEditArea').hidden=true;
  shortcutEditingKey='';
  shortcutPendingValue='';
  shortcutPressedKeys.clear();
}

function shortcutEditButtons(){
  return [
    document.querySelector('#shortcutEditClear'),
    document.querySelector('#shortcutEditReset'),
    document.querySelector('#shortcutEditSave')
  ].filter(b=>b && !b.hidden && b.offsetParent!==null);
}

function shortcutEditCurrentButton(){
  const buttons=shortcutEditButtons();
  return buttons.find(b=>b.classList.contains('keyboard-current')) ||
         buttons.find(b=>b===document.activeElement) || null;
}

function selectShortcutEditButton(index){
  const buttons=shortcutEditButtons();
  if(!buttons.length)return false;
  index=Math.max(0,Math.min(buttons.length-1,index));
  return setKeyboardCurrent(buttons[index],{focus:true});
}

function moveShortcutEditButton(direction){
  const buttons=shortcutEditButtons();
  if(!buttons.length)return false;
  const current=shortcutEditCurrentButton();
  let i=current?buttons.indexOf(current):-1;
  if(i<0)i=direction>0?-1:buttons.length;
  i=(i+direction+buttons.length)%buttons.length;
  return setKeyboardCurrent(buttons[i],{focus:true});
}

function exitShortcutEdit(){
  document.querySelector('#shortcutEditArea').hidden=true;
  shortcutEditingKey='';
  shortcutPendingValue='';
  const row=document.querySelector(`#shortcutRows [data-shortcut-index="${shortcutRowIndex}"]`);
  if(row)setKeyboardCurrent(row,{focus:false});
}

function beginShortcutEdit(){
  const row=shortcutRowsState[shortcutRowIndex];
  if(!row?.editable)return false;
  shortcutEditingKey=row.key;
  shortcutPendingValue=row.value||'';
  shortcutPressedKeys.clear();
  document.querySelector('#shortcutEditLabel').textContent='Change: '+row.label;
  document.querySelector('#shortcutCaptureInput').value=shortcutPendingValue;
  document.querySelector('#shortcutEditStatus').textContent='Press and hold the keys you want in the shortcut. Click Save when finished.';
  document.querySelector('#shortcutEditArea').hidden=false;
  document.querySelector('#shortcutCaptureInput').focus();
  return true;
}
async function saveShortcutEdit(){
  const row=shortcutRowsState.find(x=>x.key===shortcutEditingKey);
  if(!row)return;
  const statusEl=document.querySelector('#shortcutEditStatus');
  try{
    if(row.global){
      await browser.commands.update({name:'toggle-stashlibrary',shortcut:shortcutPendingValue});
    }else{
      localShortcuts[row.key]=shortcutPendingValue;
      await browser.storage.local.set({stashlibraryLocalShortcuts:localShortcuts});
    }
    await buildShortcutRows();
    renderShortcutRows();
    exitShortcutEdit();
    statusEl.textContent='';
  }catch(_){
    statusEl.textContent='Firefox could not use that shortcut. Try a different combination.';
  }
}
async function resetShortcutEdit(){
  const row=shortcutRowsState.find(x=>x.key===shortcutEditingKey);
  if(!row)return;
  if(row.global){
    await browser.commands.reset('toggle-stashlibrary');
    shortcutPendingValue=await getGlobalStashLibraryShortcut();
  }else{
    shortcutPendingValue=LOCAL_SHORTCUT_DEFAULTS[row.key]||'';
  }
  document.querySelector('#shortcutCaptureInput').value=shortcutPendingValue;
  document.querySelector('#shortcutEditStatus').textContent='Default restored in the editor. Choose Save to apply.';
}

document.querySelector('#configureKeyboardShortcuts')?.addEventListener('click',openShortcutManager);
document.querySelector('#keyboardShortcutsCloseX')?.addEventListener('click',closeShortcutManager);
document.querySelector('#shortcutRevertAll')?.addEventListener('click',revertAllShortcuts);
document.querySelector('#shortcutEditClear')?.addEventListener('click',()=>{
  shortcutPendingValue='';
  shortcutPressedKeys.clear();
  const input=document.querySelector('#shortcutCaptureInput');
  if(input){
    input.value='';
    input.focus();
  }
  document.querySelector('#shortcutEditStatus').textContent=
    'Shortcut cleared. Press the keys you want, or click Revert/Save.';
});
document.querySelector('#shortcutEditSave')?.addEventListener('click',saveShortcutEdit);
document.querySelector('#shortcutEditReset')?.addEventListener('click',resetShortcutEdit);
function normalizeShortcutCaptureKey(key){
  if(key==='Control')return 'Ctrl';
  if(key==='Alt')return 'Alt';
  if(key==='Shift')return 'Shift';
  if(key==='Meta'||key==='OS')return 'Command';
  if(key===' ')return 'Space';
  if(key==='Escape')return 'Esc';
  if(key==='ArrowUp')return 'Up';
  if(key==='ArrowDown')return 'Down';
  if(key==='ArrowLeft')return 'Left';
  if(key==='ArrowRight')return 'Right';
  if(String(key).length===1)return String(key).toUpperCase();
  return String(key||'');
}

function capturedShortcutFromPressedKeys(e){
  const ordered=[];
  const add=(k)=>{if(k && !ordered.includes(k))ordered.push(k)};

  // Standard modifier order first.
  if(e.ctrlKey || shortcutPressedKeys.has('Ctrl'))add('Ctrl');
  if(e.altKey || shortcutPressedKeys.has('Alt'))add('Alt');
  if(e.shiftKey || shortcutPressedKeys.has('Shift'))add('Shift');
  if(e.metaKey || shortcutPressedKeys.has('Command'))add('Command');

  // Then every other key currently held down.
  for(const key of shortcutPressedKeys){
    if(!['Ctrl','Alt','Shift','Command'].includes(key))add(key);
  }

  return ordered.join('+');
}

document.querySelector('#shortcutCaptureInput')?.addEventListener('keydown',e=>{
  e.preventDefault();
  e.stopPropagation();

  const key=normalizeShortcutCaptureKey(e.key);
  if(!key)return;

  shortcutPressedKeys.add(key);

  const combo=capturedShortcutFromPressedKeys(e);
  if(!combo)return;

  shortcutPendingValue=combo;
  e.currentTarget.value=combo;
  document.querySelector('#shortcutEditStatus').textContent=
    'Captured: '+combo+' — keep holding/pressing keys to build the combination, then click Save.';
});

document.querySelector('#shortcutCaptureInput')?.addEventListener('keyup',e=>{
  e.preventDefault();
  e.stopPropagation();
  shortcutPressedKeys.delete(normalizeShortcutCaptureKey(e.key));
});

window.addEventListener('blur',()=>shortcutPressedKeys.clear());

document.querySelector('#settingsBtn').onclick=()=>settingsPanel.hidden?openSettings():closeSettings();
document.querySelector('#onboardingCloseX')?.addEventListener('click',()=>{
  closeOnboarding();
  browser.storage.local.remove(STASHLIBRARY_ONBOARDING_RESUME_KEY).catch(()=>{});
  status(onboardingReturnStatus||'Ready');
});
document.querySelector('#welcomeSettings').onclick=()=>openSettings();
document.querySelector('#closeSettings')?.addEventListener('click',closeSettings);
document.querySelectorAll('[data-theme]').forEach(b=>b.onclick=()=>applyTheme(b.dataset.theme));

let stashlibraryDialogResolver=null;
let stashlibraryDialogReturnFocus=null;
let stashlibraryDialogBlocking=false;
let stashlibraryDialogConfirmOverride=null;
function finishStashLibraryDialog(confirmed){
  const modal=document.querySelector('#stashlibraryDialogModal');
  if(!modal||modal.hidden)return;
  // Mandatory component-repair dialogs cannot be dismissed with X, Escape,
  // Backspace or Cancel. They reappear on every popup open until detection succeeds.
  if(stashlibraryDialogBlocking && !confirmed)return;
  const input=document.querySelector('#stashlibraryDialogInput');
  const value=input?.value??'';
  modal.hidden=true;
  const resolve=stashlibraryDialogResolver;
  stashlibraryDialogResolver=null;
  stashlibraryDialogBlocking=false;
  stashlibraryDialogConfirmOverride=null;
  modal.dataset.blocking='false';
  const returnFocus=stashlibraryDialogReturnFocus;
  stashlibraryDialogReturnFocus=null;
  if(returnFocus && typeof returnFocus.focus==='function')requestAnimationFrame(()=>{try{returnFocus.focus({preventScroll:true})}catch(_){try{returnFocus.focus()}catch(__){}}});
  resolve?.({confirmed:!!confirmed,value});
}
function showStashLibraryDialog({title='StashLibrary',message='',confirmLabel='OK',cancelLabel='Cancel',danger=false,input=false,inputLabel='',value='',placeholder='',blocking=false,onConfirm=null}={}){
  const modal=document.querySelector('#stashlibraryDialogModal');
  if(!modal)return Promise.resolve({confirmed:false,value:''});
  if(stashlibraryDialogResolver)finishStashLibraryDialog(false);
  stashlibraryDialogReturnFocus=document.activeElement instanceof HTMLElement?document.activeElement:null;
  stashlibraryDialogBlocking=!!blocking;
  stashlibraryDialogConfirmOverride=typeof onConfirm==='function'?onConfirm:null;
  modal.dataset.blocking=stashlibraryDialogBlocking?'true':'false';
  const titleEl=document.querySelector('#stashlibraryDialogTitle');if(titleEl)titleEl.textContent=title;
  const messageEl=document.querySelector('#stashlibraryDialogMessage');if(messageEl)messageEl.textContent=message;
  const wrap=document.querySelector('#stashlibraryDialogInputWrap');if(wrap)wrap.hidden=!input;
  const label=document.querySelector('#stashlibraryDialogInputLabel');if(label)label.textContent=inputLabel||'';
  const field=document.querySelector('#stashlibraryDialogInput');
  if(field){field.value=String(value??'');field.placeholder=placeholder||'';}
  const confirm=document.querySelector('#stashlibraryDialogConfirm');
  if(confirm){confirm.textContent=confirmLabel||'OK';confirm.classList.toggle('dialog-danger-action',!!danger);}
  const cancel=document.querySelector('#stashlibraryDialogCancel');
  if(cancel){cancel.textContent=cancelLabel||'Cancel';cancel.hidden=stashlibraryDialogBlocking||!cancelLabel;}
  const closeX=document.querySelector('#stashlibraryDialogCloseX');
  if(closeX)closeX.hidden=stashlibraryDialogBlocking;
  modal.hidden=false;
  return new Promise(resolve=>{
    stashlibraryDialogResolver=resolve;
    requestAnimationFrame(()=>{
      if(input&&field){field.focus();field.select();}
      else confirm?.focus();
    });
  });
}
async function stashlibraryConfirm(options={}){
  const result=await showStashLibraryDialog({...options,input:false});
  return !!result.confirmed;
}
async function stashlibraryPrompt(options={}){
  const result=await showStashLibraryDialog({...options,input:true});
  return result.confirmed?result.value:null;
}
document.querySelector('#stashlibraryDialogConfirm')?.addEventListener('click',async()=>{
  if(stashlibraryDialogConfirmOverride){
    const fn=stashlibraryDialogConfirmOverride;
    const button=document.querySelector('#stashlibraryDialogConfirm');
    if(button)button.disabled=true;
    try{await fn();}finally{if(button && !document.querySelector('#stashlibraryDialogModal')?.hidden)button.disabled=false;}
    return;
  }
  finishStashLibraryDialog(true);
});
document.querySelector('#stashlibraryDialogCancel')?.addEventListener('click',()=>finishStashLibraryDialog(false));
document.querySelector('#stashlibraryDialogCloseX')?.addEventListener('click',()=>finishStashLibraryDialog(false));
document.querySelector('#stashlibraryDialogInput')?.addEventListener('keydown',e=>{
  if(e.key==='Enter'){e.preventDefault();e.stopPropagation();document.querySelector('#stashlibraryDialogConfirm')?.click();}
  else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();document.querySelector('#stashlibraryDialogCancel')?.click();}
});

function closeManualBackupMoreMenu(){
  const menu=document.querySelector('#manualBackupMoreMenu'),button=document.querySelector('#manualBackupMoreActions');
  if(menu)menu.hidden=true;
  if(button)button.setAttribute('aria-expanded','false');
}
function toggleManualBackupMoreMenu(){
  const menu=document.querySelector('#manualBackupMoreMenu'),button=document.querySelector('#manualBackupMoreActions');
  if(!menu||!button||button.disabled)return;
  const opening=menu.hidden;
  closeWebdavMoreMenu();
  menu.hidden=!opening;
  button.setAttribute('aria-expanded',opening?'true':'false');
  if(opening)requestAnimationFrame(()=>menu.querySelector('button:not(:disabled)')?.focus());
}
document.querySelector('#manualBackupMoreActions')?.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();toggleManualBackupMoreMenu();});
document.querySelector('#manualBackupMoreMenu')?.addEventListener('click',e=>e.stopPropagation());
document.addEventListener('click',e=>{if(!e.target.closest('.manual-backup-menu-wrap'))closeManualBackupMoreMenu();});
document.querySelector('#manualBackupMoreMenu')?.addEventListener('keydown',e=>{
  if(matchesLocalShortcut(e,'backClose')){e.preventDefault();e.stopPropagation();closeManualBackupMoreMenu();document.querySelector('#manualBackupMoreActions')?.focus();}
});
function closeWebdavMoreMenu(){
  const menu=document.querySelector('#webdavMoreMenu'),button=document.querySelector('#webdavMoreActions');
  if(menu)menu.hidden=true;
  if(button)button.setAttribute('aria-expanded','false');
}
function toggleWebdavMoreMenu(){
  const menu=document.querySelector('#webdavMoreMenu'),button=document.querySelector('#webdavMoreActions');
  if(!menu||!button||button.disabled)return;
  const opening=menu.hidden;
  menu.hidden=!opening;
  button.setAttribute('aria-expanded',opening?'true':'false');
  if(opening)requestAnimationFrame(()=>menu.querySelector('button:not(:disabled)')?.focus());
}
document.querySelector('#webdavMoreActions')?.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();toggleWebdavMoreMenu();});
document.querySelector('#webdavMoreMenu')?.addEventListener('click',e=>e.stopPropagation());
document.addEventListener('click',e=>{if(!e.target.closest('.cloud-backup-menu-wrap'))closeWebdavMoreMenu();});
document.querySelector('#webdavMoreMenu')?.addEventListener('keydown',e=>{
  if(matchesLocalShortcut(e,'backClose')){e.preventDefault();e.stopPropagation();closeWebdavMoreMenu();document.querySelector('#webdavMoreActions')?.focus();return;}
  if(matchesLocalShortcut(e,'moveDown')||matchesLocalShortcut(e,'moveUp')){
    const buttons=[...e.currentTarget.querySelectorAll('button:not(:disabled)')];if(!buttons.length)return;
    e.preventDefault();e.stopPropagation();const i=Math.max(0,buttons.indexOf(document.activeElement));const step=matchesLocalShortcut(e,'moveDown')?1:-1;buttons[(i+step+buttons.length)%buttons.length].focus();
  }
});


document.querySelector('#settingsOpenLocalFolder')?.addEventListener('click',async()=>{
  try{const r=await send({cmd:'open_bookmarks'});if(!r?.ok)throw new Error(r?.error||'Could not open the StashLibrary storage folder.');status('Opened StashLibrary storage folder');}catch(e){errorStatus(e)}
});
document.querySelector('#manualBackupCreate')?.addEventListener('click',async()=>{
  closeManualBackupMoreMenu();
  if(manualBackupActive){
    setManualBackupRunning(true,{cancelling:true});
    try{
      const r=await send({cmd:'cancel_backup'});if(!r?.ok)throw new Error(r?.error||'Could not cancel the manual backup.');
      if(r.cancelRequested)status('Cancelling Manual Backup…',{busy:true});
      else{setManualBackupRunning(false);finishOperation('No manual backup is running');}
    }catch(e){setManualBackupRunning(true);errorStatus(e)}
    return;
  }
  try{
    const result=document.querySelector('#manualBackupResult');if(result)result.hidden=true;
    beginOperation('Creating Manual Backup','choose a folder for the backup');
    const r=await send({cmd:'manual_backup_create'});
    if(!r?.ok)throw new Error(r?.error||'Could not create the manual backup.');
    if(r.cancelled){setManualBackupRunning(false);finishOperation('Manual backup cancelled');return;}
    if(result){result.hidden=false;result.textContent='Done. Backup stored in “'+r.path+'”.';}
    setManualBackupRunning(false);finishOperation('Manual backup created and verified — '+r.path);
  }catch(e){setManualBackupRunning(false);errorStatus(e)}
});document.querySelector('#manualBackupRestore')?.addEventListener('click',async()=>{
  closeManualBackupMoreMenu();
  if(!await stashlibraryConfirm({title:'Restore Manual Backup?',message:'Choose a StashLibrary Manual Backup ZIP. StashLibrary will verify the backup before replacing the current local library. The readable files, folder structure and catalogue data will all be restored.',confirmLabel:'Choose Backup ZIP',danger:false}))return;
  try{
    beginOperation('Restoring Manual Backup','waiting for ZIP picker');
    const r=await send({cmd:'import_backup'});
    if(!r?.ok)throw new Error(r?.error||'Could not restore the manual backup.');
    if(r.cancelled){finishOperation('Manual backup restore cancelled');return;}
    await refreshTree('change');await refreshSettingsInfo();
    finishOperation('Manual backup restored and verified — '+(r.path||'complete'));
  }catch(e){errorStatus(e)}
});
document.querySelector('#webdavProvider')?.addEventListener('change',()=>updateWebdavProviderForm({preserveUrl:false}));
document.querySelector('#webdavConnect')?.addEventListener('click',async()=>{
  const provider=document.querySelector('#webdavProvider')?.value||'';
  const preset=webdavProviderDefaults(provider);
  const url=preset.custom?(document.querySelector('#webdavUrl')?.value||''):preset.url;
  const username=document.querySelector('#webdavUsername')?.value||'';
  const password=document.querySelector('#webdavPassword')?.value||'';
  try{
    beginOperation('Connecting Cloud Backup','checking the WebDAV connection');
    const r=await send({cmd:'webdav_connect',provider,url,username,password});if(!r?.ok)throw new Error(r?.error||'Could not connect Cloud Backup.');
    const pw=document.querySelector('#webdavPassword');if(pw)pw.value='';
    renderWebdavBackupInfo(r);finishOperation(r.safetyHold?'Existing cloud backup found — replacement blocked for safety':`Cloud Backup connected — ${r.providerLabel||'WebDAV'}`);
  }catch(e){errorStatus(e);}
});
document.querySelector('#webdavBackupNow')?.addEventListener('click',async()=>{
  const ok=await stashlibraryConfirm({title:'Back Up Now?',message:'Your cloud backup will be updated to match your current local StashLibrary library. Existing cloud backup data may be overwritten or removed.\n\nStashLibrary verifies the local catalogue and the uploaded cloud catalogue before marking the backup complete.',confirmLabel:'Back Up Now',danger:false});
  if(!ok)return;
  try{
    beginOperation('Cloud Backup','starting in the background');
    const r=await browser.runtime.sendMessage({type:'webdav-job-start',kind:'backup'});
    if(!r?.ok)throw new Error(r?.error||'Cloud backup could not start.');
    await refreshSettingsInfo();
    status('Cloud Backup — running in the background. You can close StashLibrary.',{busy:true});
  }catch(e){errorStatus(e);await refreshSettingsInfo();}
});
document.querySelector('#webdavDisconnect')?.addEventListener('click',async()=>{
  closeWebdavMoreMenu();
  if(!await stashlibraryConfirm({title:'Disconnect Cloud Backup?',message:'Your existing cloud backup will stay online and will not be deleted. StashLibrary will forget the saved password for this connection.',confirmLabel:'Disconnect',danger:false}))return;
  try{const r=await send({cmd:'webdav_disconnect'});if(!r?.ok)throw new Error(r?.error||'Could not disconnect Cloud Backup.');renderWebdavBackupInfo(r);status('Cloud Backup disconnected');}catch(e){errorStatus(e)}
});
document.querySelector('#webdavRestore')?.addEventListener('click',async()=>{
  closeWebdavMoreMenu();
  if(!await stashlibraryConfirm({title:'Restore from Cloud Backup?',message:'StashLibrary will compare the cloud backup with your current local library, download only missing or changed files, and verify the cloud catalogue before applying the restored state.',confirmLabel:'Restore',danger:false}))return;
  try{
    beginOperation('Restoring Cloud Backup','starting in the background');
    const r=await browser.runtime.sendMessage({type:'webdav-job-start',kind:'restore'});
    if(!r?.ok)throw new Error(r?.error||'Cloud restore could not start.');
    await refreshSettingsInfo();
    status('Cloud Restore — running in the background. You can close StashLibrary.',{busy:true});
  }catch(e){errorStatus(e);await refreshSettingsInfo();}
});

document.querySelector('#webdavRecoverKoofrZip')?.addEventListener('click',async()=>{
  closeWebdavMoreMenu();
  const ok=await stashlibraryConfirm({
    title:'Recover Legacy Koofr Backup?',
    message:'Koofr can block legacy .html files over WebDAV. In the Koofr web app, download the entire StashLibrary folder as a ZIP. Then choose that ZIP here. StashLibrary will verify the catalogue and restore the original HTML/PDF files into local storage.',
    confirmLabel:'Choose Koofr ZIP',danger:false
  });
  if(!ok)return;
  try{
    beginOperation('Recovering Legacy Koofr Backup','waiting for ZIP picker');
    const r=await send({cmd:'import_webdav_backup_zip'});
    if(!r?.ok)throw new Error(r?.error||'Could not recover the downloaded Koofr backup.');
    if(r.cancelled){finishOperation('Koofr recovery cancelled');return;}
    await refreshTree('change');await refreshSettingsInfo();
    finishOperation(`Recovered ${Number(r.files||0)} archived files from Koofr ZIP`);
  }catch(e){errorStatus(e);await refreshSettingsInfo();}
});

let zoteroInventory=null;
let zmigDestinationPath='';
let zmigDestinationLabel='StashLibrary root';
let zmigPendingDestinationPath='';
let zmigPendingDestinationLabel='StashLibrary root';

function zoteroAttachmentID(a){
  const id=a?.id;
  if(id!==undefined&&id!==null&&String(id)!=='')return String(id);
  return String(a?.key||'');
}

function setZoteroNodeExpanded(node,expanded){
  if(!node)return;
  const kids=node.querySelector(':scope > .zotero-children');
  const toggle=node.querySelector(':scope > .zotero-row .zotero-toggle');
  if(!kids||!toggle||toggle.disabled)return;

  const isExpanded=!!expanded;
  node.dataset.expanded=isExpanded?'1':'0';
  node.classList.toggle('collapsed',!isExpanded);
  kids.hidden=!isExpanded;
  toggle.textContent=isExpanded?'▾':'▸';
  toggle.setAttribute('aria-expanded',String(isExpanded));
}

function expandAllZotero(){
  [...document.querySelectorAll('#zoteroPicker .zotero-node')].forEach(
    node=>setZoteroNodeExpanded(node,true)
  );
}

function collapseAllZotero(){
  // Collapse deepest nodes first so no intermediate reflow/event can leave
  // descendants visually exposed.
  [...document.querySelectorAll('#zoteroPicker .zotero-node')]
    .reverse()
    .forEach(node=>setZoteroNodeExpanded(node,false));
}

function renderZoteroPicker(){
 const box=document.querySelector('#zoteroPicker');
 box.innerHTML='';

 const cs=zoteroInventory?.collections||[];
 const as=zoteroInventory?.attachments||[];

 // IMPORTANT: do not sort. Preserve Zotero's collection order as returned by
 // the helper so this view reflects Zotero rather than imposing alphabetic order.
 const byParent=new Map();
 for(const c of cs){
   const p=c.parentID?String(c.parentID):'root';
   if(!byParent.has(p))byParent.set(p,[]);
   byParent.get(p).push(c);
 }

 const attByColl=new Map();
 const unfiled=[];
 for(const a of as){
   const ids=(a.collectionIDs||[]).map(String);
   if(!ids.length)unfiled.push(a);
   for(const id of ids){
     if(!attByColl.has(id))attByColl.set(id,[]);
     attByColl.get(id).push(a);
   }
 }

 const makeRow=(type,id,label,count='',hasChildren=false)=>{
   const row=document.createElement('div');
   row.className='zotero-row '+(type==='attachment'?'zotero-file':'');

   let toggle=null;
   if(type==='collection'){
     toggle=document.createElement('button');
     toggle.type='button';
     toggle.className='zotero-toggle';
     toggle.textContent=hasChildren?'▾':'';
     toggle.disabled=!hasChildren;
     toggle.setAttribute('aria-expanded',String(hasChildren));
     row.append(toggle);
   }else{
     const spacer=document.createElement('span');
     spacer.className='zotero-toggle-spacer';
     row.append(spacer);
   }

   const cb=document.createElement('input');
   cb.type='checkbox';
   cb.dataset.ztype=type;
   cb.dataset.zid=String(id);

   const lab=document.createElement('label');
   lab.textContent=label;

   row.append(cb,lab);

   if(count!==''){
     const n=document.createElement('span');
     n.className='zotero-count';
     n.textContent=count;
     row.append(n);
   }

   return {row,cb,toggle,lab};
 };

 const wireNode=(wrap,cb,toggle,lab,kids)=>{
   if(toggle&&!toggle.disabled){
     const flip=()=>{
       const opening=kids.hidden;
       setZoteroNodeExpanded(wrap,opening);
     };
     toggle.onclick=e=>{e.preventDefault();e.stopPropagation();flip()};
     // Folder name also expands/collapses, while the checkbox remains purely
     // a selection control.
     lab.onclick=e=>{e.preventDefault();e.stopPropagation();flip()};
   }else{
     lab.onclick=e=>{
       e.preventDefault();e.stopPropagation();
       cb.checked=!cb.checked;
       cb.dispatchEvent(new Event('change',{bubbles:true}));
     };
   }

   cb.addEventListener('change',e=>{
     if(cb.dataset.ztype==='collection'){
       kids.querySelectorAll('input[data-ztype]').forEach(x=>{
         x.checked=cb.checked;
         x.indeterminate=false;
       });
     }
     updateZoteroParentCheckboxes();
     updateZoteroSelectionState();
   });
 };

 const buildCollection=(c)=>{
   const wrap=document.createElement('div');
   wrap.className='zotero-node';

   const childCs=byParent.get(String(c.id))||[];
   const atts=attByColl.get(String(c.id))||[];
   const hasChildren=childCs.length+atts.length>0;
   const {row,cb,toggle,lab}=makeRow(
     'collection',
     c.id,
     c.name||'Untitled collection',
     atts.length?`${atts.length} file${atts.length===1?'':'s'}`:'',
     hasChildren
   );
   wrap.append(row);

   const kids=document.createElement('div');
   kids.className='zotero-children';
   kids.hidden=false;

   for(const cc of childCs)kids.append(buildCollection(cc));

   for(const a of atts){
     const r=makeRow(
       'attachment',
       zoteroAttachmentID(a),
       a.parentTitle||a.title||((a.path||'').split(/[\\/]/).pop())||'Attachment'
     );
     r.cb.dataset.zkey=String(a.key||'');
     r.cb.dataset.ownerCollection=String(c.id);
     r.lab.onclick=e=>{
       e.preventDefault();e.stopPropagation();
       r.cb.checked=!r.cb.checked;
       r.cb.dispatchEvent(new Event('change',{bubbles:true}));
     };
     r.cb.addEventListener('change',()=>{
       updateZoteroParentCheckboxes();
       updateZoteroSelectionState();
     });
     kids.append(r.row);
   }

   wrap.append(kids);
   wireNode(wrap,cb,toggle,lab,kids);
   if(hasChildren)setZoteroNodeExpanded(wrap,true);
   return wrap;
 };

 // A dedicated Unfiled Items pseudo-folder. These are real Zotero attachments
 // whose owning item belongs to no collection.
 if(unfiled.length){
   const wrap=document.createElement('div');
   wrap.className='zotero-node zotero-unfiled';

   const {row,cb,toggle,lab}=makeRow(
     'collection','__unfiled','Unfiled Items',
     `${unfiled.length} file${unfiled.length===1?'':'s'}`,true
   );
   wrap.append(row);

   const kids=document.createElement('div');
   kids.className='zotero-children';
   kids.hidden=false;

   for(const a of unfiled){
     const r=makeRow(
       'attachment',
       zoteroAttachmentID(a),
       a.parentTitle||a.title||((a.path||'').split(/[\\/]/).pop())||'Attachment'
     );
     r.cb.dataset.zkey=String(a.key||'');
     r.cb.dataset.unfiled='1';
     r.lab.onclick=e=>{
       e.preventDefault();e.stopPropagation();
       r.cb.checked=!r.cb.checked;
       r.cb.dispatchEvent(new Event('change',{bubbles:true}));
     };
     // This handler was missing before and was the reason visibly ticked
     // Unfiled files did not reliably enable/run migration.
     r.cb.addEventListener('change',()=>{
       updateZoteroParentCheckboxes();
       updateZoteroSelectionState();
     });
     kids.append(r.row);
   }

   wrap.append(kids);
   wireNode(wrap,cb,toggle,lab,kids);
   setZoteroNodeExpanded(wrap,true);
   box.append(wrap);
 }

 for(const c of byParent.get('root')||[])box.append(buildCollection(c));

 if(!box.children.length){
   box.innerHTML='<div class="empty-zotero">No Zotero collections or local attachments were returned.</div>';
 }

 box.hidden=false;
 ['zoteroSelectAll','zoteroSelectNone','zoteroExpandAll','zoteroCollapseAll'].forEach(id=>{
   const el=document.querySelector('#'+id);
   if(el)el.disabled=false;
 });
 updateZoteroParentCheckboxes();
 updateZoteroSelectionState();
}

function updateZoteroParentCheckboxes(){
 const nodes=[...document.querySelectorAll('#zoteroPicker .zotero-node')].reverse();

 for(const node of nodes){
   const own=node.querySelector(':scope > .zotero-row input[data-ztype="collection"]');
   const kids=node.querySelector(':scope > .zotero-children');
   if(!own||!kids)continue;

   const descendantBoxes=[
     ...kids.querySelectorAll('input[data-ztype="attachment"], input[data-ztype="collection"]')
   ];

   if(!descendantBoxes.length){
     own.indeterminate=false;
     continue;
   }

   const checked=descendantBoxes.filter(x=>x.checked).length;
   own.indeterminate=checked>0 && checked<descendantBoxes.length;

   // If no descendants are selected, clear the folder checkbox too.
   // If every descendant is manually selected, leave the folder checkbox
   // unchanged so "select files" does not silently become "recreate folder".
   if(checked===0)own.checked=false;
 }
}

function updateZoteroSelectionState(){
 const boxes=[...document.querySelectorAll('#zoteroPicker input[data-ztype]')];
 const files=boxes.filter(x=>x.checked&&x.dataset.ztype==='attachment').length;
 const folders=boxes.filter(x=>x.checked&&x.dataset.ztype==='collection'&&x.dataset.zid!=='__unfiled').length;
 const unfiledFolder=boxes.some(x=>x.checked&&x.dataset.ztype==='collection'&&x.dataset.zid==='__unfiled');

 const btn=document.querySelector('#zoteroMigrate');
 btn.disabled=(files+folders+(unfiledFolder?1:0))===0;

 const bits=[];
 if(folders)bits.push(`${folders} folder${folders===1?'':'s'}`);
 if(unfiledFolder && !files)bits.push('Unfiled Items');
 if(files)bits.push(`${files} file${files===1?'':'s'}`);

 btn.textContent=bits.length?`Migrate selected items (${bits.join(', ')})`:'Migrate selected items';

 const st=document.querySelector('#zmigStatus');
 if(zoteroInventory&&st&&!st.dataset.busy){
   st.textContent=bits.length
     ? `${bits.join(', ')} selected — destination: ${zmigDestinationLabel}`
     : `Loaded ${zoteroInventory.collections?.length||0} collections and ${zoteroInventory.attachments?.length||0} attachments. Choose what you want to copy.`;
 }
}

// ---------------- StashLibrary destination tree ----------------

function setDestNodeExpanded(node,expanded){
 const kids=node?.querySelector(':scope > .zmig-dest-children');
 const toggle=node?.querySelector(':scope > .zmig-dest-row .zmig-dest-toggle');
 if(!kids||!toggle||toggle.disabled)return;
 kids.hidden=!expanded;
 toggle.textContent=expanded?'▾':'▸';
 toggle.setAttribute('aria-expanded',String(expanded));
}

function expandAllDest(){
 document.querySelectorAll('#zmigDestinationTreeBody .zmig-dest-node').forEach(n=>setDestNodeExpanded(n,true));
}
function collapseAllDest(){
 document.querySelectorAll('#zmigDestinationTreeBody .zmig-dest-node').forEach(n=>setDestNodeExpanded(n,false));
}

function populateZoteroMigrationDestinations(){
 const panel=document.querySelector('#zmigDestinationTree');
 const body=document.querySelector('#zmigDestinationTreeBody');
 const summary=document.querySelector('#zmigDestinationSummary');
 const pending=document.querySelector('#zmigDestinationPending');
 if(!panel||!body)return;

 body.innerHTML='';

 const buildNode=(name,path,children=[])=>{
   const node=document.createElement('div');
   node.className='zmig-dest-node';

   const folderChildren=(children||[]).filter(c=>c.type==='folder');

   const row=document.createElement('div');
   row.className='zmig-dest-row';
   row.dataset.path=path||'';

   const tog=document.createElement('button');
   tog.type='button';
   tog.className='zmig-dest-toggle';
   tog.textContent=folderChildren.length?'▾':'';
   tog.disabled=!folderChildren.length;
   tog.setAttribute('aria-expanded',String(!!folderChildren.length));

   const icon=document.createElement('span');
   icon.className='folder-icon zmig-dest-icon';

   const label=document.createElement('span');
   label.className='zmig-dest-name';
   label.textContent=name;

   row.append(tog,icon,label);
   node.append(row);

   const kids=document.createElement('div');
   kids.className='zmig-dest-children';
   kids.hidden=false;

   // Preserve the exact current StashLibrary bookmark order.
   for(const c of folderChildren){
     kids.append(buildNode(c.name,c.path,c.children||[]));
   }
   node.append(kids);

   if(folderChildren.length){
     const flip=()=>setDestNodeExpanded(node,kids.hidden);
     tog.onclick=e=>{e.preventDefault();e.stopPropagation();flip()};
     icon.onclick=e=>{e.preventDefault();e.stopPropagation();flip()};
   }

   const choosePending=()=>{
     zmigPendingDestinationPath=path||tree?.path||'';
     zmigPendingDestinationLabel=name;
     body.querySelectorAll('.zmig-dest-row').forEach(x=>x.classList.toggle('pending',x===row));
     if(pending)pending.textContent=name;
   };

   // Clicking a folder only highlights it. It does NOT close the selector and
   // does NOT immediately change the confirmed destination.
   label.onclick=e=>{e.preventDefault();e.stopPropagation();choosePending()};
   row.onclick=e=>{
     if(e.target===tog||e.target===icon||e.target===label)return;
     choosePending();
   };

   return node;
 };

 if(!tree?.path){
   body.append(buildNode('StashLibrary root','',[]));
 }else{
   body.append(buildNode('StashLibrary root',tree.path,tree.children||[]));
 }

 if(!zmigDestinationPath)zmigDestinationPath=tree?.path||'';
 if(!zmigPendingDestinationPath)zmigPendingDestinationPath=zmigDestinationPath;

 const confirmed=[...body.querySelectorAll('.zmig-dest-row')].find(r=>r.dataset.path===zmigDestinationPath);
 const pendingRow=[...body.querySelectorAll('.zmig-dest-row')].find(r=>r.dataset.path===zmigPendingDestinationPath);

 if(confirmed)confirmed.classList.add('selected');
 if(pendingRow)pendingRow.classList.add('pending');

 if(summary)summary.textContent=zmigDestinationLabel;
 if(pending)pending.textContent=zmigPendingDestinationLabel;
}


function closeZoteroMigrationModal(){
 document.querySelector('#zoteroMigrationModal').hidden=true;
}

document.querySelector('#zmigClose').onclick=closeZoteroMigrationModal;
document.querySelector('#zmigCancel').onclick=closeZoteroMigrationModal;

document.querySelector('#zmigDestinationButton').onclick=e=>{
 e.preventDefault();e.stopPropagation();
 const panel=document.querySelector('#zmigDestinationTree');
 const btn=document.querySelector('#zmigDestinationButton');
 const opening=panel.hidden;
 panel.hidden=!opening;
 btn.setAttribute('aria-expanded',String(opening));
};

document.querySelector('#zmigDestinationClose').onclick=e=>{
  stashlibraryClickFeedback(e?.currentTarget||document.querySelector('#zmigDestinationClose'));
 e.preventDefault();e.stopPropagation();
 const panel=document.querySelector('#zmigDestinationTree');
 const btn=document.querySelector('#zmigDestinationButton');
 if(panel)panel.hidden=true;
 if(btn)btn.setAttribute('aria-expanded','false');
};

document.querySelector('#zmigDestinationSelect').onclick=e=>{
  stashlibraryClickFeedback(e?.currentTarget||document.querySelector('#zmigDestinationSelect'));
 e.preventDefault();e.stopPropagation();
 zmigDestinationPath=zmigPendingDestinationPath||tree?.path||'';
 zmigDestinationLabel=zmigPendingDestinationLabel||'StashLibrary root';
 const body=document.querySelector('#zmigDestinationTreeBody');
 body?.querySelectorAll('.zmig-dest-row').forEach(row=>{
   row.classList.toggle('selected',row.dataset.path===zmigDestinationPath);
 });
 const summary=document.querySelector('#zmigDestinationSummary');
 if(summary)summary.textContent=zmigDestinationLabel;
 const panel=document.querySelector('#zmigDestinationTree');
 const btn=document.querySelector('#zmigDestinationButton');
 if(panel)panel.hidden=true;
 if(btn)btn.setAttribute('aria-expanded','false');
 updateZoteroSelectionState();
};

document.querySelector('#zmigDestExpandAll').onclick=e=>{e.preventDefault();e.stopPropagation();expandAllDest()};
document.querySelector('#zmigDestCollapseAll').onclick=e=>{e.preventDefault();e.stopPropagation();collapseAllDest()};

async function loadZoteroInventory({showModal=false}={}){
 const modal=document.querySelector('#zoteroMigrationModal');
 const st=document.querySelector('#zmigStatus');
 const info=document.querySelector('#zoteroMigrationInfo');
 const refresh=document.querySelector('#zoteroRefresh');

 if(showModal){
   modal.hidden=false;
   if(!zmigDestinationPath)zmigDestinationPath=tree?.path||'';
   zmigPendingDestinationPath=zmigDestinationPath;
   zmigPendingDestinationLabel=zmigDestinationLabel;
   populateZoteroMigrationDestinations();
   document.querySelector('#zmigDestinationTree').hidden=true;
   document.querySelector('#zmigDestinationButton').setAttribute('aria-expanded','false');
 }

 document.querySelector('#zoteroPicker').hidden=true;
 document.querySelector('#zoteroPicker').innerHTML='';
 ['zoteroSelectAll','zoteroSelectNone','zoteroExpandAll','zoteroCollapseAll'].forEach(id=>{
   const el=document.querySelector('#'+id);if(el)el.disabled=true;
 });
 document.querySelector('#zoteroMigrate').disabled=true;
 if(refresh)refresh.disabled=true;

 st.dataset.busy='1';
 st.textContent='Loading Zotero library…';

 try{
   beginOperation('Zotero migration','reading collection tree');
   const r=await send({cmd:'zotero_inventory'});
   if(!r.ok)throw new Error(r.error);
   zoteroInventory=r;
   renderZoteroPicker();
   requestAnimationFrame(()=>{
     updateZoteroParentCheckboxes();
     updateZoteroSelectionState();
   });
   delete st.dataset.busy;
   if(refresh)refresh.disabled=false;
   st.textContent=`Loaded ${r.collections?.length||0} collections and ${r.attachments?.length||0} attachments. Choose what you want to copy.`;
   info.textContent=`Zotero library loaded: ${r.collections?.length||0} collections, ${r.attachments?.length||0} attachments.`;
   finishOperation('Zotero library loaded');
 }catch(e){
   delete st.dataset.busy;
   if(refresh)refresh.disabled=false;
   st.textContent=`Could not load Zotero: ${e.message||e}`;
   info.textContent=`Could not load Zotero: ${e.message||e}`;
   errorStatus(e);
 }
}

document.querySelector('#zoteroLoad').onclick=()=>loadZoteroInventory({showModal:true});
document.querySelector('#zoteroRefresh').onclick=e=>{e.preventDefault();e.stopPropagation();loadZoteroInventory({showModal:false})};

document.querySelector('#zoteroSelectAll').onclick=()=>{
  stashlibraryClickFeedback(e?.currentTarget||document.querySelector('#zoteroSelectAll'));
 document.querySelectorAll('#zoteroPicker input[data-ztype]').forEach(x=>{
   x.checked=true;x.indeterminate=false;
 });
 updateZoteroParentCheckboxes();
 updateZoteroSelectionState();
};
document.querySelector('#zoteroSelectNone').onclick=()=>{
  stashlibraryClickFeedback(e?.currentTarget||document.querySelector('#zoteroSelectNone'));
 document.querySelectorAll('#zoteroPicker input[data-ztype]').forEach(x=>{
   x.checked=false;x.indeterminate=false;
 });
 updateZoteroParentCheckboxes();
 updateZoteroSelectionState();
};
document.querySelector('#zoteroExpandAll').onclick=expandAllZotero;
document.querySelector('#zoteroCollapseAll').onclick=collapseAllZotero;

document.querySelector('#zoteroMigrate').onclick=async(e)=>{
  const btn=e?.currentTarget||document.querySelector('#zoteroMigrate');
  stashlibraryClickFeedback(btn,{working:true});

  // Close immediately so the migration click feels responsive.
  closeZoteroMigrationModal();
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));

  try{
const btn=document.querySelector('#zoteroMigrate');
 const info=document.querySelector('#zoteroMigrationInfo');
 const st=document.querySelector('#zmigStatus');

 const selected=[...document.querySelectorAll('#zoteroPicker input[data-ztype]:checked')];
 const collectionIDs=[...new Set(
   selected
     .filter(x=>x.dataset.ztype==='collection'&&x.dataset.zid!=='__unfiled')
     .map(x=>x.dataset.zid)
 )];

 const attachmentBoxes=selected.filter(x=>x.dataset.ztype==='attachment');
 const attachmentIDs=[...new Set(attachmentBoxes.map(x=>x.dataset.zid).filter(Boolean))];

 if(!collectionIDs.length&&!attachmentIDs.length){
   st.textContent='Select at least one Zotero folder or file first.';
   updateZoteroSelectionState();
   return;
 }

 // A manually selected attachment is direct unless its real parent collection
 // was explicitly selected. Unfiled attachments are always direct.
 const collectionSet=new Set(collectionIDs.map(String));
 const directAttachmentIDs=[...new Set(
   attachmentBoxes
     .filter(x=>x.dataset.unfiled==='1'||!x.dataset.ownerCollection||!collectionSet.has(String(x.dataset.ownerCollection)))
     .map(x=>x.dataset.zid)
     .filter(Boolean)
 )];

 const destinationPath=zmigDestinationPath||tree?.path||'';
 const migrationAuditId=await addSaveAudit({
   kind:'migration',
   status:'started',
   title:'Zotero migration',
   destination:destinationPath,
   detail:`${collectionIDs.length} folder(s), ${attachmentIDs.length} file(s)`
 });

 try{
   btn.disabled=true;
   st.dataset.busy='1';

   const bits=[];
   if(collectionIDs.length)bits.push(`${collectionIDs.length} folder${collectionIDs.length===1?'':'s'}`);
   if(attachmentIDs.length)bits.push(`${attachmentIDs.length} file${attachmentIDs.length===1?'':'s'}`);

   st.textContent=`Migrating ${bits.join(', ')} into ${zmigDestinationLabel}…`;
   beginOperation('Zotero migration',attachmentIDs.length?'copying selected items':'creating selected folders');

   const r=await send({
     cmd:'zotero_migrate',
     collectionIDs,
     attachmentIDs,
     directAttachmentIDs,
     destinationPath
   });

   if(!r.ok)throw new Error(r.error);

   // If the native side received no attachments despite checked files, surface
   // that explicitly rather than making the button appear to do nothing.
   if(attachmentIDs.length && Number(r.attachments||0)===0){
     throw new Error(
       `StashLibrary sent ${attachmentIDs.length} selected Zotero file${attachmentIDs.length===1?'':'s'}, `+
       `but Zotero returned no matching attachment records. Reload the Zotero picker and try again.`
     );
   }

   await refreshTree('change');
   populateZoteroMigrationDestinations();

   const parts=[
     `${r.copied||0} copied`,
     `${r.updated||0} updated`,
     `${r.skipped||0} unchanged`,
     `${r.foldersCreated||0} folders created`
   ];
   if(r.missing)parts.push(`${r.missing} item${r.missing===1?'':'s'} skipped`);
   if(r.failed)parts.push(`${r.failed} failed`);

   const summary=`Finished: ${parts.join(' · ')}`+
     (r.errors?.length?`\n${r.errors.slice(0,8).join('\n')}`:'');

   st.textContent=summary;
   info.textContent=summary;
   await updateSaveAudit(migrationAuditId,{
     status:(r.failed||r.missing)?'completed-with-warnings':'completed',
     detail:parts.join(' · '),
     error:r.errors?.slice(0,8).join(' | ')||''
   });
   finishOperation(`Zotero migration finished — ${parts.join(', ')}`);
 }catch(e){
   await updateSaveAudit(migrationAuditId,{status:'failed',error:String(e?.message||e)});
   st.textContent=`Migration failed: ${e.message||e}`;
   info.textContent=`Migration failed: ${e.message||e}`;
   errorStatus(e);
 }finally{
   delete st.dataset.busy;
   updateZoteroSelectionState();
 }
    stashlibraryClickFeedback(btn,{success:true});
  }finally{
    stashlibraryClearWorking(btn);
  }
};

browser.runtime.onMessage.addListener(m=>{
 if(m?.type==='archive-progress'&&m.payload){
   const p=m.payload;
   const elapsed=p.elapsed!=null?` — ${Number(p.elapsed).toFixed(1)}s`:'';
   status(`${p.stage||'Saving'}${elapsed}`,{busy:true});
   if(operationActive)showActivity(p.stage||operationLabel||'Saving',p.detail||'');

   if(currentStashLibrarySaveAuditId){
     const stage=String(p.stage||'').toLowerCase();
     let auditStatus='started',saveKind='';
     if(stage.includes('html')){auditStatus='capturing';saveKind='html'}
     else if(stage.includes('pdf')){auditStatus='saving';saveKind='pdf'}
     updateSaveAudit(currentStashLibrarySaveAuditId,{
       status:auditStatus,
       saveKind,
       progressStage:p.stage||'',
       progressDetail:p.detail||''
     });
   }
   return;
 }
 if(m?.type==='webdav-job'&&m.payload){
   const job=m.payload;
   if(job.active){
     setBackupRunning(true);
     renderWebdavProgress({active:true,kind:job.kind,stage:'starting',detail:'Running in the background. You can close StashLibrary.',percent:null});
     status(`${job.kind==='restore'?'Cloud Restore':'Cloud Backup'} — running in the background. You can close StashLibrary.`,{busy:true});
   }else{
     setBackupRunning(false);
     renderWebdavProgress({active:false,kind:job.kind,stage:job.error?'failed':'finished',detail:job.error||'',percent:job.error?null:100,error:job.error||''});
     if(job.error){
       errorStatus(new Error(job.error));
       refreshSettingsInfo().catch(()=>{});
     }else if(job.result){
       if(job.kind==='restore'){
         refreshTree('change').catch(()=>{});refreshSettingsInfo().catch(()=>{});
         const warnings=Number(job.result.legacyWarnings||0);
         const skipped=Number(job.result.skippedFiles||0);
         if(skipped){
           finishOperation(`Cloud backup restored — ${Number(job.result.files||0)-skipped}/${Number(job.result.files||0)} saved items recovered · ${skipped} item${skipped===1?'':'s'} need attention in Check & Repair Library`);
         }else{
           finishOperation(warnings?`Cloud backup restored — ${warnings} old backup metadata warning${warnings===1?'':'s'} recovered`:'Cloud backup restored and verified');
         }
       }else{
         refreshSettingsInfo().catch(()=>{});
         const r=job.result;const changed=Number(r.newFiles||0)+Number(r.updatedFiles||0)+Number(r.deletedFiles||0);
         const provider=r.providerLabel||'cloud provider';
         finishOperation(`Cloud backup verified by ${provider} — ${changed} change${changed===1?'':'s'} · ${Number(r.unchangedFiles||0)} unchanged`);
       }
     }
   }
   return;
 }
 if(m?.type!=='native'||!m.payload)return;
 const p=m.payload;
 if(p.event==='progress'){
   const cloudOp=String(p.operation||'').trim().toLowerCase();
   if(['cloud backup','cloud restore','cloud recovery'].includes(cloudOp)){
     const stage=String(p.stage||'').toLowerCase();
     const active=!['finished','failed','cancelled'].includes(stage);
     const kind=cloudOp.includes('recovery')?'recovery':(cloudOp.includes('restore')?'restore':'backup');
     setBackupRunning(active);
     renderWebdavProgress({active,kind,stage:p.stage||'',detail:p.detail||'',percent:p.percent,error:stage==='failed'?(p.detail||''):'',workers:p.workers||[]});
   }else if(cloudOp==='backup'){
     const stage=String(p.stage||'').toLowerCase();
     const active=!['finished','failed','cancelled'].includes(stage);
     renderManualBackupProgress({active,stage:p.stage||'',detail:p.detail||'',percent:p.percent,error:stage==='failed'?(p.detail||''):''});
     setManualBackupRunning(active);
   }
   // Background catalogue checks are intentionally invisible. A 30–80 ms
   // SQLite tree read should never make the status bar flicker.
   if(silentNativeProgress>0)return;
   const pct=Number.isFinite(p.percent)?` ${Math.round(p.percent)}%`:'';
   const elapsed=p.elapsed!=null?` — ${Number(p.elapsed).toFixed(1)}s`:'';
   status(`${p.operation||'Working'}${pct}`,{busy:true});
   if(operationActive)showActivity(`${p.operation||operationLabel||'Working'}${pct}`,p.detail||p.stage||'');
 }else if(p.event==='changed'){
   // The native watcher may emit noisy/repeated notifications. Do not debounce
   // forever and do not redraw every 500 ms: schedule at most one quiet check
   // per background interval, and hold it while a user edit is running.
   scheduleBackgroundRefresh();
 }
});
document.querySelector('#copyDebugLog').onclick=async()=>{
  try{
    const payload={
      generatedAt:new Date().toISOString(),
      extension:{
        name:browser.runtime.getManifest().name,
        version:browser.runtime.getManifest().version
      },
      currentStatus:document.querySelector('#status')?.textContent||'',
      selectedCount:selectedPaths?.size||0,
      native:await send({cmd:'debug_info'}),
      events:debugEvents
    };
    await navigator.clipboard.writeText(JSON.stringify(payload,null,2));
    status('Support information copied',{hold:1800});
  }catch(e){errorStatus(e)}
};



let currentRepairScan=null;

function closeRepairModal(){
  document.querySelector('#repairModal').hidden=true;
}

function repairButton(label,handler,danger=false){
  const b=document.createElement('button');
  b.textContent=label;
  if(danger)b.classList.add('danger-subtle');
  b.onclick=handler;
  return b;
}

function repairItemShell(title,subtitle){
  const wrap=document.createElement('div');
  wrap.className='repair-card';

  const head=document.createElement('div');
  head.className='repair-card-title';
  head.textContent=title||'Unnamed Item';

  const sub=document.createElement('div');
  sub.className='repair-card-subtitle';
  sub.textContent=subtitle||'';

  const actions=document.createElement('div');
  actions.className='repair-card-actions';

  wrap.append(head,sub,actions);
  return {wrap,actions};
}

async function applyRepairAction(issue,action,extra={}){
  const r=await send({cmd:'repair_apply',issue:{...issue,action,...extra}});
  if(!r?.ok)throw new Error(r?.error||'Repair failed');
  return r;
}

function populateMatchSelect(filesWithoutRecords){
  const sel=document.createElement('select');
  sel.className='repair-match-select';

  const ph=document.createElement('option');
  ph.value='';
  ph.textContent='Choose Physical File…';
  sel.append(ph);

  const seen=new Set();
  for(const issue of filesWithoutRecords||[]){
    const name=issue.physicalName||'';
    if(!name||seen.has(name))continue;
    seen.add(name);
    const opt=document.createElement('option');
    opt.value=name;
    opt.textContent=name;
    sel.append(opt);
  }
  return sel;
}

async function openRepairPhysicalFile(issue){
  const r=await send({cmd:'open_archive_file',physicalName:issue?.physicalName||''});
  if(!r?.ok)throw new Error(r?.error||'Could not open the physical file.');
  if(r.url)await browser.tabs.create({url:r.url,active:true});
}

function revealRepairRecord(issue){
  const path=String(issue?.path||'');
  if(!path)return false;
  const node=findNodeByPath(path,tree);
  if(!node)return false;
  document.querySelector('#repairModal').hidden=true;
  settingsPanel.hidden=true;
  collapseAll();
  clearSelection({quiet:true});
  const parts=path.split('/');
  const prefixes=[];
  for(let i=2;i<parts.length;i++)prefixes.push(parts.slice(0,i).join('/'));
  let depth=1;
  for(const folderPath of prefixes){
    const folder=findNodeByPath(folderPath,tree);
    const anchor=itemElementByPath(folderPath);
    if(folder?.type==='folder'&&anchor){openFolder(anchor,folder,depth);depth++;}
  }
  selectedPaths.add(path);selectionAnchorPath=path;syncSelectionClasses();updateSelectionUI(true);
  const target=itemElementByPath(path);
  if(target){
    target.classList.add('repair-highlight');
    target.scrollIntoView({block:'nearest',inline:'nearest'});
    setTimeout(()=>target.classList.remove('repair-highlight'),3500);
  }
  status(`Showing record — ${issue.bookmarkTitle||issue.label||'missing-file record'}`,{hold:3500});
  return !!target;
}

function renderRepairScan(scan){
  currentRepairScan=scan;

  const recordsBox=document.querySelector('#repairRecordsWithoutFiles');
  const filesBox=document.querySelector('#repairFilesWithoutRecords');
  const metadataArea=document.querySelector('#repairMetadataIssues');
  const metadataBox=document.querySelector('#repairMetadataList');
  const summary=document.querySelector('#repairScanSummary');

  recordsBox.innerHTML='';
  filesBox.innerHTML='';
  metadataBox.innerHTML='';

  const records=scan?.recordsWithoutFiles||[];
  const files=scan?.filesWithoutRecords||[];
  const metadata=scan?.metadataIssues||[];

  const totalIssues=records.length+files.length+metadata.length;
  summary.textContent=totalIssues
    ?`${totalIssues} item${totalIssues===1?'':'s'} need attention · ${records.length} missing saved file${records.length===1?'':'s'} · ${files.length} missing bookmark${files.length===1?'':'s'}`+(metadata.length?` · ${metadata.length} detail${metadata.length===1?'':'s'} to update`:'')
    :'Everything looks good';

  if(!records.length){
    recordsBox.innerHTML='<div class="repair-empty-small">None</div>';
  }else{
    for(const issue of records){
      const {wrap,actions}=repairItemShell(
        issue.bookmarkTitle||issue.label,
        issue.reason||issue.physicalName||''
      );

      if(issue.path){
        actions.append(repairButton('Show Record',async()=>{
          try{if(!revealRepairRecord(issue))throw new Error('That StashLibrary record could not be shown in the current library view.');}
          catch(e){errorStatus(e)}
        }));
      }

      const select=populateMatchSelect(files);
      actions.append(select);

      actions.append(repairButton('Match',async()=>{
        if(!select.value)return;
        try{
          await applyRepairAction(issue,'matchRecordToFile',{matchPhysicalName:select.value});
          await runRepairScan('Matched record to file.');
        }catch(e){errorStatus(e)}
      }));

      if(issue.kind==='fileRecordWithoutPhysical'){
        actions.append(repairButton('Remove Broken Entry',async()=>{
          try{
            await applyRepairAction(issue,'deleteStaleFileRecord');
            await runRepairScan('Removed the broken entry.');
          }catch(e){errorStatus(e)}
        }));
      }else{
        actions.append(repairButton('Remove Bookmark',async()=>{
          try{
            await applyRepairAction(issue,'deleteRecord');
            await runRepairScan('Deleted orphan bookmark record.');
          }catch(e){errorStatus(e)}
        }));
      }

      recordsBox.append(wrap);
    }
  }

  if(!files.length){
    filesBox.innerHTML='<div class="repair-empty-small">None</div>';
  }else{
    for(const issue of files){
      const {wrap,actions}=repairItemShell(
        issue.physicalName||issue.label,
        issue.reason||''
      );

      if(issue.physicalName&&issue.physicalExists!==false){
        actions.append(repairButton('Open File',async()=>{
          try{await openRepairPhysicalFile(issue);}
          catch(e){errorStatus(e)}
        }));
      }

      if(issue.physicalName){
        actions.append(repairButton('Create Bookmark',async()=>{
          try{
            await applyRepairAction(issue,'createBookmarkForFile');
            await runRepairScan('Created bookmark for physical file.');
          }catch(e){errorStatus(e)}
        }));
      }

      if(issue.physicalExists===false && issue.fileId){
        actions.append(repairButton('Remove Broken Entry',async()=>{
          try{
            await applyRepairAction(issue,'deleteFileRecord');
            await runRepairScan('Deleted orphan file record.');
          }catch(e){errorStatus(e)}
        }));
      }else if(issue.physicalName){
        actions.append(repairButton('Delete File',async()=>{
          try{
            await applyRepairAction(issue,'deletePhysicalFile');
            await runRepairScan('Deleted physical file.');
          }catch(e){errorStatus(e)}
        }));
      }

      filesBox.append(wrap);
    }
  }

  metadataArea.hidden=!metadata.length;
  for(const issue of metadata){
    const {wrap,actions}=repairItemShell(
      issue.bookmarkTitle||issue.label,
      `${issue.physicalName||''} → ${issue.storageName||''}`
    );
    actions.append(repairButton('Update Details',async()=>{
      try{
        await applyRepairAction(issue,'syncMetadata');
        await runRepairScan('Synchronized filename metadata.');
      }catch(e){errorStatus(e)}
    }));
    metadataBox.append(wrap);
  }
}


async function renderSaveAudit(){
  const box=document.querySelector('#repairSaveAudit');
  if(!box)return;
  const rows=await readSaveAudit();
  box.innerHTML='';

  if(!rows.length){
    box.innerHTML='<div class="repair-empty-small">No save or migration history yet.</div>';
    return;
  }

  const table=document.createElement('table');
  table.className='repair-audit-table';
  const thead=document.createElement('thead');
  const hr=document.createElement('tr');
  for(const [label,cls] of [
    ['File / operation','audit-name'],['Type','audit-type'],['Saved to','audit-destination'],
    ['Status','audit-status'],['Source link','audit-source'],['Details','audit-details']
  ]){
    const th=document.createElement('th');th.textContent=label;th.className=cls;hr.append(th);
  }
  thead.append(hr);table.append(thead);
  const tbody=document.createElement('tbody');

  const statusLabelFor=row=>({
    completed:'Completed','completed-with-warnings':'Completed with warnings',failed:'Failed',
    interrupted:'Interrupted',capturing:'Capturing',saving:'Saving',started:'Started'
  }[row.status]||row.status||'Unknown');
  const typeLabelFor=row=>{
    if(row.kind==='migration')return 'Migration';
    if(row.saveKind)return String(row.saveKind).replace(/-/g,' ').toUpperCase();
    const n=String(row.physicalName||row.title||'');
    const m=n.match(/\.([a-z0-9]{1,8})$/i);
    return m?m[1].toUpperCase():'Save';
  };

  for(const row of rows.slice(0,80)){
    const tr=document.createElement('tr');
    tr.classList.add(`audit-${String(row.status||'').replace(/[^a-z-]/g,'')}`);
    const values=[
      row.physicalName||row.title||row.kind||'StashLibrary operation',
      typeLabelFor(row),
      row.destinationName||row.destination||'—',
      statusLabelFor(row)
    ];
    values.forEach((value,index)=>{
      const td=document.createElement('td');
      td.className=['audit-name','audit-type','audit-destination','audit-status'][index];
      td.textContent=String(value||'—');td.title=String(value||'');tr.append(td);
    });

    const sourceTd=document.createElement('td');sourceTd.className='audit-source';
    if(row.url){
      const a=document.createElement('a');
      a.href=row.url;a.className='audit-source-link';a.textContent=row.url;a.title=row.url;
      a.addEventListener('click',async e=>{e.preventDefault();try{await browser.tabs.create({url:row.url,active:true})}catch(err){errorStatus(err)}});
      sourceTd.append(a);
    }else sourceTd.textContent='—';
    tr.append(sourceTd);

    const detailTd=document.createElement('td');detailTd.className='audit-details';
    const detail=document.createElement('span');detail.className='audit-cell-detail';
    const when=row.updatedAt||row.time||'';
    const detailText=[row.error||row.detail||'',when?new Date(when).toLocaleString():''].filter(Boolean).join(' · ');
    detail.textContent=detailText||'—';detail.title=detailText;detailTd.append(detail);
    if(row.url && ['failed','interrupted'].includes(row.status)){
      const retry=document.createElement('button');retry.type='button';retry.className='audit-inline-btn';retry.textContent='Retry';
      retry.addEventListener('click',async()=>{try{await browser.tabs.create({url:row.url,active:true});status('Retry opened — wait for the page to finish loading, then press S to save it to StashLibrary.',{hold:5000});document.querySelector('#repairModal').hidden=true}catch(err){errorStatus(err)}});
      detailTd.append(retry);
    }
    tr.append(detailTd);
    tbody.append(tr);
  }
  table.append(tbody);
  const tfoot=document.createElement('tfoot');
  const footRow=document.createElement('tr');
  const footCell=document.createElement('td');
  footCell.colSpan=6;footCell.className='audit-bottom-rule';
  footRow.append(footCell);tfoot.append(footRow);table.append(tfoot);
  box.append(table);
}

document.querySelector('#repairClearAudit')?.addEventListener('click',async()=>{
  await writeSaveAudit([]);
  await renderSaveAudit();
  status('Save and migration history cleared',{hold:1800});
});

async function runRepairScan(message=''){
  const r=await send({cmd:'repair_scan'});
  if(!r?.ok)throw new Error(r?.error||'Repair scan failed.');
  renderRepairScan(r);
  await renderSaveAudit();

  return r;
}

document.querySelector('#scanRepair').onclick=async(e)=>{
  const btn=e.currentTarget;
  const modal=document.querySelector('#repairModal');
  if(modal)modal.hidden=false;
  const summary=document.querySelector('#repairScanSummary');
  if(summary)summary.textContent='Checking your library…';
  try{
    if(typeof stashlibraryClickFeedback==='function')stashlibraryClickFeedback(btn,{working:true});
    await runRepairScan();
  }catch(err){errorStatus(err);if(summary)summary.textContent='The check could not finish. Copy Support Information if you need to report this.'}
  finally{if(typeof stashlibraryClearWorking==='function')stashlibraryClearWorking(btn)}
};

document.querySelector('#repairCloseX')?.addEventListener('click',closeRepairModal);
document.querySelector('#repairRescanResync').onclick=async(e)=>{
  const btn=e.currentTarget;
  try{
    if(typeof stashlibraryClickFeedback==='function')stashlibraryClickFeedback(btn,{working:true});
    beginOperation('Checking library','Finding and repairing missing links');

    const r=await send({cmd:'repair_rescan_resync'});
    if(!r?.ok)throw new Error(r?.error||'The library check could not finish.');

    // Re-read the tree from the native backend rather than trusting the returned
    // snapshot, so the normal extension refresh path and UI state are exercised.
    await refreshTree('change');
    await runRepairScan(`Library checked${r.synced?` · ${r.synced} item${r.synced===1?'':'s'} updated.`:'.'}`);
    finishOperation('Library checked and repaired');
  }catch(err){errorStatus(err)}
  finally{if(typeof stashlibraryClearWorking==='function')stashlibraryClearWorking(btn)}
};




document.querySelector('#openFaviconsDialog')?.addEventListener('click',()=>{
  const modal=document.querySelector('#faviconsDialog');
  if(!modal)return;
  modal.hidden=false;
  browser.storage.local.get('stashlibraryFaviconRefreshJob').then(result=>{
    const q=result?.stashlibraryFaviconRefreshJob;
    const s=document.querySelector('#thumbnailGenerationStatus');
    if(!q||!s)return;
    s.textContent=q.error
      ? 'Favicon error — '+q.error
      : q.finished
        ? 'Favicon refresh complete — '+(q.updated||0)+' updated, '+(q.unchanged||0)+' unchanged, '+(q.kept||0)+' existing icons kept because their websites were unavailable'+(q.missing?', '+q.missing+' websites had no favicon available':'')+'.'
        : 'Refreshing favicons in the background — '+(q.done||0)+'/'+(q.total||0)+' websites checked.';
  }).catch(()=>{});
  requestAnimationFrame(()=>document.querySelector('#refreshFavicons')?.focus());
});
document.querySelector('#faviconsDialogCloseX')?.addEventListener('click',()=>{
  const modal=document.querySelector('#faviconsDialog');
  if(modal)modal.hidden=true;
});

document.querySelector('#refreshFavicons')?.addEventListener('click',()=>runThumbnailGeneration());

browser.storage.onChanged.addListener((changes,area)=>{
  if(area!=='local')return;
  if(changes[STASHLIBRARY_THUMBNAIL_KEY]){
    bookmarkThumbnailCache=changes[STASHLIBRARY_THUMBNAIL_KEY].newValue||{};
    document.querySelectorAll('.item.bookmark[data-path]').forEach(el=>{
      const n=findNodeByPath(el.dataset.path,tree);
      if(n)applyBookmarkThumbnail(el,n);
    });
  }
  if(changes.stashlibraryFaviconRefreshJob){
    const q=changes.stashlibraryFaviconRefreshJob.newValue||{};
    const s=document.querySelector('#thumbnailGenerationStatus');
    if(s)s.textContent=q.error
      ? 'Favicon error — '+q.error
      : q.finished
        ? 'Favicon refresh complete — '+(q.updated||0)+' updated, '+(q.unchanged||0)+' unchanged, '+(q.kept||0)+' existing icons kept because their websites were unavailable'+(q.missing?', '+q.missing+' websites had no favicon available':'')+'.'
        : 'Refreshing favicons in the background — '+(q.done||0)+'/'+(q.total||0)+' websites checked.';
  }
});

browser.runtime.onMessage.addListener(m=>{
  if(m?.type!=='thumbnail-progress'||!m.payload)return;
  const s=document.querySelector('#thumbnailGenerationStatus'),q=m.payload;
  if(!s)return;
  s.textContent=q.finished
    ? 'Favicon refresh complete — '+(q.updated||0)+' updated, '+(q.unchanged||0)+' unchanged, '+(q.kept||0)+' existing icons kept because their websites were unavailable'+(q.missing?', '+q.missing+' websites had no favicon available':'')+'.'
    : 'Refreshing favicons — '+q.done+'/'+q.total+' websites checked'+(q.updated?' · '+q.updated+' updated':'')+(q.kept?' · '+q.kept+' existing kept':'');
});

document.querySelector('#onboardingInstallHelper')?.addEventListener('click',async()=>{
  helperInstallAttempted=true;
  await persistOnboardingProgress();
  // Release any currently-running helper before the user starts the installer.
  // The installer uses side-by-side version folders, so Firefox and all tabs can
  // stay open while the native helper is replaced.
  try{await browser.runtime.sendMessage({type:'native-pause'});}catch(_){}
  helperMismatchPaused=true;
  setOnboardingCheck('#onboardingHelperCheck',false,'Waiting for StashLibrary Helper…',false);
  startOnboardingHelperPolling({immediate:false});
  openSupportLink(STASHLIBRARY_WINDOWS_HELPER_URL,'StashLibrary Helper');
});
document.querySelector('#onboardingInstallZotero')?.addEventListener('click',async()=>{
  zoteroInstallAttempted=true;
  await persistOnboardingProgress();
  setOnboardingCheck('#onboardingZoteroCheck',false,'Ready to install — follow the instructions below, restart Zotero, then return here.',false);
  openSupportLink(STASHLIBRARY_ZOTERO_HELPER_URL,'Zotero Helper');
});

// Installing either companion temporarily takes focus away from Firefox.
// Always re-check BOTH components when the user returns. Previously the
// Windows-helper install flag could suppress the Zotero re-check, leaving a
// freshly-installed Zotero Helper shown as "Not connected" until the setup
// screen was reopened. A few short Zotero retries also cover Zotero finishing
// startup after the plugin has just been installed.
window.addEventListener('focus',async()=>{
  const modal=document.querySelector('#onboardingModal');
  if(!modal || modal.hidden)return;

  if(!onboardingState.helper){
    setOnboardingCheck('#onboardingHelperCheck',false,'Waiting for StashLibrary Helper…',false);
    try{await detectInstalledHelperFast(900);}catch(_){}
    if(!onboardingState.helper)startOnboardingHelperPolling({immediate:false});
  }else{
    try{await refreshOnboardingStatus({forceNative:true});}catch(_){}
  }
  if(onboardingState.helper)helperInstallAttempted=false;

  const tries=zoteroInstallAttempted?4:1;
  for(let attempt=0;attempt<tries;attempt++){
    if(modal.hidden)break;
    try{
      if(await refreshOnboardingZoteroStatus()){
        zoteroInstallAttempted=false;
        break;
      }
    }catch(_){}
    if(!zoteroInstallAttempted)break;
    await new Promise(r=>setTimeout(r,350+attempt*120));
  }
});


document.addEventListener('visibilitychange',()=>{
  const modal=document.querySelector('#onboardingModal');
  if(document.visibilityState==='visible'&&modal&&!modal.hidden&&!onboardingState.helper){
    startOnboardingHelperPolling({immediate:true});
  }
});

document.querySelector('#onboardingPrev')?.addEventListener('click',async()=>{
  if(onboardingSlideIndex>onboardingStartSlide){onboardingSlideIndex--;renderOnboardingSlide();await persistOnboardingProgress();}
});
async function finishOnboardingWizard(){
  if(!(onboardingState.helper&&onboardingState.folder&&onboardingState.zotero))return;
  closeOnboarding();
  await browser.storage.local.set({[STASHLIBRARY_ONBOARDING_KEY]:true}).catch(()=>{});
  browser.storage.local.remove([STASHLIBRARY_ONBOARDING_SKIPPED_VERSION_KEY,STASHLIBRARY_ONBOARDING_RESUME_KEY,'stashlibraryAutomaticUpdates']).catch(()=>{});
  requestAnimationFrame(async()=>{
    if(onboardingTreePreloadPromise)await onboardingTreePreloadPromise.catch(()=>{});
    if(!applyOnboardingPrefetchedTree('onboarding-done'))await refreshTree('startup').catch(()=>{});
    status('Setup complete — StashLibrary is ready',{hold:2200});
  });
}
document.querySelector('#onboardingNext')?.addEventListener('click',async()=>{
  if(onboardingSlideIndex===4 && !(onboardingState.helper&&onboardingState.folder))return;
  if(onboardingSlideIndex===5 && !onboardingState.zotero)return;
  if(onboardingSlideIndex===5){await finishOnboardingWizard();return;}
  onboardingSlideIndex=Math.min(5,onboardingSlideIndex+1);
  renderOnboardingSlide();
  await persistOnboardingProgress();
});

document.querySelector('#openUninstallComponents')?.addEventListener('click',()=>{
  const modal=document.querySelector('#uninstallComponentsModal');
  if(modal)modal.hidden=false;
  const statusEl=document.querySelector('#uninstallComponentsStatus');
  if(statusEl)statusEl.textContent='';
});
document.querySelector('#uninstallComponentsCloseX')?.addEventListener('click',()=>{document.querySelector('#uninstallComponentsModal').hidden=true;});

async function uninstallStashLibraryHelper(btn,statusEl=null){
  if(!await stashlibraryConfirm({title:'Uninstall StashLibrary Helper?',message:'Your StashLibrary library, saved webpages/PDFs, settings, and backups will be kept.',confirmLabel:'Uninstall',danger:false}))return;
  const old=btn?.textContent||'Uninstall…';
  try{
    if(btn){btn.disabled=true;btn.textContent='Uninstalling…';}
    const r=await send({cmd:'uninstall_helper'});
    if(!r?.ok)throw new Error(r?.error||'Could not uninstall the StashLibrary Helper.');
    try{await browser.runtime.sendMessage({type:'native-pause'});}catch(_){ }
    helperInstallAttempted=false;helperMismatchPaused=true;
    onboardingTreePreloaded=false;onboardingTreePreloadPromise=null;onboardingPrefetchedTree=null;
    onboardingState={helper:false,folder:false,zotero:false};
    setOnboardingCheck('#onboardingHelperCheck',false,'Not installed',false);
    setOnboardingCheck('#onboardingZoteroCheck',false,'Not connected',false);
    const install=document.querySelector('#onboardingInstallHelper');if(install){install.hidden=false;install.textContent='Install StashLibrary Helper';}
    updateOnboardingFinish();
    if(statusEl)statusEl.textContent='StashLibrary Helper uninstalled. Your StashLibrary library was kept.';
    status('StashLibrary Helper uninstalled — your StashLibrary library was kept',{hold:2600});

    // This is an intentional uninstall flow, so do not immediately reopen
    // Setup. If the Firefox extension is kept, Setup will appear normally the
    // next time StashLibrary opens because the Helper is genuinely absent.
    if(btn){btn.textContent='Removed';btn.disabled=true;}
  }catch(err){if(statusEl)statusEl.textContent=String(err?.message||err);errorStatus(err)}
  finally{if(btn && btn.textContent!=='Removed'){btn.disabled=false;btn.textContent=old;}}
}

document.querySelector('#uninstallWindowsHelperBtn')?.addEventListener('click',e=>uninstallStashLibraryHelper(e.currentTarget,document.querySelector('#uninstallComponentsStatus')));
document.querySelector('#uninstallFirefoxExtensionBtn')?.addEventListener('click',async()=>{
  const statusEl=document.querySelector('#uninstallComponentsStatus');
  try{
    if(browser.management?.uninstallSelf){
      const ok=await stashlibraryConfirm({title:'Remove StashLibrary from Firefox?',message:'Your saved StashLibrary files will not be deleted.',confirmLabel:'Remove',danger:false});
      if(!ok)return;
      await browser.management.uninstallSelf({showConfirmDialog:false});
    }else{
      if(statusEl)statusEl.textContent='Open Firefox Add-ons Manager and remove StashLibrary from Extensions.';
    }
  }catch(err){if(statusEl)statusEl.textContent='Firefox did not remove the extension. You can remove StashLibrary from Firefox Add-ons Manager.';}
});


init();


async function openSupportLink(url,label){
  const target=String(url||'').trim();
  if(!target){
    finishOperation(`${label} link has not been configured yet.`);
    return;
  }
  try{
    await browser.tabs.create({url:target});
  }catch(e){
    errorStatus(e);
  }
}


const reportBugBtn=document.querySelector('#reportBugBtn');
if(reportBugBtn) reportBugBtn.onclick=()=>openSupportLink(REPORT_BUG_URL,'Report a Bug');


function revealSettingsTop(){
  requestAnimationFrame(()=>{
    const scroller=document.querySelector('.settings-scroll');
    if(scroller)scroller.scrollTop=0;
  });
}
document.querySelector('#aboutVersionEasterEgg')?.addEventListener('focus',revealSettingsTop);

function revealDonateBottom(){
  requestAnimationFrame(()=>{
    const scroller=document.querySelector('.settings-scroll');
    if(scroller)scroller.scrollTop=scroller.scrollHeight;
  });
}
document.querySelector('#donateBtn')?.addEventListener('focus',revealDonateBottom);
document.querySelector('#donateBtn')?.addEventListener('mouseenter',()=>{});

const donateBtn=document.querySelector('#donateBtn');
if(donateBtn) donateBtn.onclick=()=>openSupportLink(SUPPORT_PROJECT_URL,'Support the Project');
