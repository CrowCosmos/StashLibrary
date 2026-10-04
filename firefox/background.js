const HOST='stashlibrary.host';
const EXPECTED_STASHLIBRARY_VERSION=browser.runtime.getManifest().version;
const STASHLIBRARY_PROTOCOL_VERSION=1;
const REQUIRED_WINDOWS_HELPER_VERSION='0.1.1';
let port=null, listeners=new Set(), verifiedNativeProtocol=null, nativeAutoReconnect=true, manualReconnectInProgress=false;
let webdavJob={active:false,kind:'',startedAt:0,finishedAt:0,result:null,error:''};

function broadcast(msg){
  browser.runtime.sendMessage(msg).catch(()=>{});
}
function connect(){
  if(port)return port;
  try{
    const next=browser.runtime.connectNative(HOST);
    port=next;
    next.onMessage.addListener(m=>{ for(const fn of [...listeners]) fn(m); broadcast({type:'native',payload:m}); });
    next.onDisconnect.addListener(()=>{
      if(port===next)port=null;
      verifiedNativeProtocol=null;
      if(nativeAutoReconnect && !manualReconnectInProgress){
        setTimeout(()=>{if(nativeAutoReconnect&&!port)connect()},1200);
      }
    });
    return next;
  }catch(e){ port=null; return null; }
}
// Native messaging is connected lazily when StashLibrary actually needs it.
// This avoids launching legacy helpers merely because the extension loaded.

function pauseNativeReconnect(){
  nativeAutoReconnect=false;
  verifiedNativeProtocol=null;
  try{
    if(port){
      const old=port;
      port=null;
      try{old.disconnect()}catch(_){}
    }
  }catch(_){}
  return {ok:true};
}

function reconnectNative(){
  verifiedNativeProtocol=null;
  nativeAutoReconnect=true;
  manualReconnectInProgress=true;
  try{
    if(port){
      const old=port;
      port=null;
      try{old.disconnect()}catch(_){}
    }
  }catch(_){}
  return new Promise(resolve=>{
    setTimeout(()=>{
      manualReconnectInProgress=false;
      connect();
      resolve({ok:true});
    },60);
  });
}

async function probeNative(timeout=3000){
  // Force one fresh connection to whatever manifest the installer has most
  // recently registered, then immediately ask for its version/folder state.
  // This is used only by Configuration after an install/update attempt.
  await reconnectNative();
  try{
    const info=await rawNativeSend({cmd:'backup_info'},timeout);
    return {ok:true,info};
  }catch(e){
    pauseNativeReconnect();
    return {ok:false,error:e?.message||String(e)};
  }
}

function rawNativeSend(payload, timeout=1800000){
  return new Promise((resolve,reject)=>{
    let requestPort=null, timer=null, disconnectHandler=null, handler=null, settled=false;
    const cleanup=()=>{
      if(timer){clearTimeout(timer);timer=null}
      if(handler)listeners.delete(handler);
      if(requestPort&&disconnectHandler){
        try{requestPort.onDisconnect.removeListener(disconnectHandler)}catch(_){}
      }
    };
    const fail=(error)=>{
      if(settled)return;
      settled=true;cleanup();reject(error instanceof Error?error:new Error(String(error||'StashLibrary Helper disconnected.')));
    };
    try{
      if(!port) connect();
      if(!port) return fail(new Error('StashLibrary Helper is not installed or connected.'));
      requestPort=port;
      const id=crypto.randomUUID();
      handler=(m)=>{
        if(m.replyTo===id){
          if(settled)return;
          settled=true;cleanup();resolve(m);
        }
      };
      disconnectHandler=()=>fail(new Error('StashLibrary Helper is not installed or has disconnected.'));
      listeners.add(handler);
      requestPort.onDisconnect.addListener(disconnectHandler);
      requestPort.postMessage({...payload,id});
      timer=setTimeout(()=>fail(new Error('Native helper timed out')),timeout);
    }catch(e){fail(e)}
  });
}

function compareStashLibraryVersions(a,b){
  const pa=String(a||'').split('.').map(n=>Number(n)||0), pb=String(b||'').split('.').map(n=>Number(n)||0);
  for(let i=0;i<Math.max(pa.length,pb.length);i++){const x=pa[i]||0,y=pb[i]||0;if(x!==y)return x<y?-1:1;}
  return 0;
}

async function ensureCompatibleNative(){
  if(verifiedNativeProtocol===STASHLIBRARY_PROTOCOL_VERSION)return verifiedNativeProtocol;
  const info=await rawNativeSend({cmd:'backup_info'},5000);
  const actual=String(info?.hostVersion||'').trim();
  const protocol=Number(info?.protocolVersion||0);
  if(protocol!==STASHLIBRARY_PROTOCOL_VERSION || actual!==REQUIRED_WINDOWS_HELPER_VERSION){
    verifiedNativeProtocol=null;
    const err=new Error(`StashLibrary update incomplete. StashLibrary ${EXPECTED_STASHLIBRARY_VERSION} requires Windows helper ${REQUIRED_WINDOWS_HELPER_VERSION}, but ${actual||'no compatible helper'} was detected. Install the latest StashLibrary release to continue.`);
    err.actualVersion=actual;
    err.actualProtocol=protocol;
    err.expectedProtocol=STASHLIBRARY_PROTOCOL_VERSION;
    throw err;
  }
  verifiedNativeProtocol=protocol;
  return protocol;
}

async function nativeSend(payload, timeout=1800000){
  // backup_info remains available before compatibility acceptance so the UI can
  // explain which helper is installed. All real operations require the current
  // StashLibrary compatibility protocol.
  if(payload?.cmd==='backup_info')return rawNativeSend(payload,timeout);
  await ensureCompatibleNative();
  return rawNativeSend(payload,timeout);
}

function webdavJobSnapshot(){
  return {...webdavJob};
}

async function startWebdavJob(kind){
  kind=String(kind||'').toLowerCase();
  if(!['backup','restore'].includes(kind))return {ok:false,error:'Unknown Cloud Backup operation.'};
  if(webdavJob.active)return {ok:false,error:`Cloud ${webdavJob.kind||'operation'} is already running.`,job:webdavJobSnapshot()};
  webdavJob={active:true,kind,startedAt:Date.now(),finishedAt:0,result:null,error:''};
  broadcast({type:'webdav-job',payload:webdavJobSnapshot()});
  // Run in the persistent background page, not the popup. Closing/minimising the
  // StashLibrary popup therefore cannot cancel the native request or its helper process.
  (async()=>{
    try{
      const cmd=kind==='backup'?'webdav_backup_now':'webdav_restore';
      const result=await nativeSend({cmd},1800000);
      if(!result?.ok)throw new Error(result?.error||`Cloud ${kind} failed.`);
      webdavJob={active:false,kind,startedAt:webdavJob.startedAt,finishedAt:Date.now(),result,error:''};
    }catch(e){
      webdavJob={active:false,kind,startedAt:webdavJob.startedAt,finishedAt:Date.now(),result:null,error:e?.message||String(e)};
    }
    broadcast({type:'webdav-job',payload:webdavJobSnapshot()});
  })();
  return {ok:true,started:true,job:webdavJobSnapshot()};
}

function archiveProgress(stage, detail='', elapsed=null){
  broadcast({type:'archive-progress',payload:{stage,detail,elapsed}});
}

async function saveCurrentTabWithSingleFile({parent,tabId,title,url}){
  const startedAt=Date.now();
  let objectURL=null;
  let downloadId=null;
  let changedListener=null;
  let tick=null;
  let done=false;

  const cleanup=()=>{
    done=true;
    if(tick) clearInterval(tick);
    if(changedListener) browser.downloads.onChanged.removeListener(changedListener);
    if(objectURL){try{URL.revokeObjectURL(objectURL)}catch{} objectURL=null}
  };

  try{
    archiveProgress('Complete save','capturing the already-loaded Firefox tab with bundled SingleFile 1.22.98',0);
    tick=setInterval(()=>{
      if(done)return;
      const secs=(Date.now()-startedAt)/1000;
      archiveProgress('Complete save',downloadId==null?'SingleFile is processing the current tab':'SingleFile archive is being written',secs);
    },750);

    let tab=null;
    if(tabId!=null){try{tab=await browser.tabs.get(tabId)}catch{}}
    if(!tab){const tabs=await browser.tabs.query({active:true,currentWindow:true});tab=tabs[0]}
    if(!tab)throw new Error('Could not find the current Firefox tab.');

    const captured=await captureLivePageSnapshot(tab,{repairFailedImages:false});
    if(!captured?.html)throw new Error('Bundled SingleFile did not return an HTML archive.');

    const blob=new Blob([captured.html],{type:'text/html;charset=utf-8'});
    objectURL=URL.createObjectURL(blob);

    let fileName=(captured.filename||'').split(/[\\/]/).pop();
    if(!fileName || !/\.html?$/i.test(fileName)){
      const safe=(captured.title||title||'Saved page').replace(/[\\/:*?"<>|]+/g,'_').trim()||'Saved page';
      const d=new Date();
      const pad=n=>String(n).padStart(2,'0');
      const stamp=`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
      fileName=`${safe} (${stamp}).html`;
    }

    downloadId=await browser.downloads.download({
      url:objectURL,
      filename:fileName,
      saveAs:false,
      conflictAction:'uniquify'
    });

    const result=await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>{
        cleanup();
        reject(new Error('Bundled SingleFile archive did not finish writing within 3 minutes.'));
      },180000);

      changedListener=async delta=>{
        if(delta.id!==downloadId)return;
        if(delta.error?.current){
          clearTimeout(timeout);cleanup();
          reject(new Error(`Firefox could not write the SingleFile archive: ${delta.error.current}`));
          return;
        }
        if(delta.state?.current!=='complete')return;
        try{
          const items=await browser.downloads.search({id:downloadId});
          const item=items[0];
          if(!item?.filename)throw new Error('Firefox did not provide the completed archive path.');
          archiveProgress('Complete save','moving finished HTML into your StashLibrary bookmarks folder',(Date.now()-startedAt)/1000);
          const moved=await nativeSend({
            cmd:'import_download',
            src:item.filename,
            dest:parent,
            displayTitle:title||captured.title||'',
            url:url||captured.url||''
          },120000);
          if(!moved.ok)throw new Error(moved.error);
          clearTimeout(timeout);cleanup();resolve(moved);
        }catch(e){clearTimeout(timeout);cleanup();reject(e)}
      };
      browser.downloads.onChanged.addListener(changedListener);
    });

    archiveProgress('Complete save',`finished — ${result.path?.split(/[\\/]/).pop()||title||'page'}`,(Date.now()-startedAt)/1000);
    return result;
  }finally{
    cleanup();
  }
}


function bytesContainPDFHeader(bytes){
  const limit=Math.min(bytes.length,1024), sig=[0x25,0x50,0x44,0x46,0x2D];
  for(let i=0;i<=limit-sig.length;i++){
    let ok=true;
    for(let j=0;j<sig.length;j++){if(bytes[i+j]!==sig[j]){ok=false;break}}
    if(ok)return true;
  }
  return false;
}
function uint8ToBase64(bytes){
  const chunkSize=0x8000; let binary='';
  for(let i=0;i<bytes.length;i+=chunkSize){
    const chunk=bytes.subarray(i,Math.min(i+chunkSize,bytes.length)); let part='';
    for(let j=0;j<chunk.length;j++)part+=String.fromCharCode(chunk[j]);
    binary+=part;
  }
  return btoa(binary);
}
function cleanPDFTitle(title='document'){
  let t=String(title||'document').replace(/\s*[—-]\s*Mozilla Firefox\s*$/i,'').replace(/[\\/:*?"<>|]+/g,'_').trim();
  if(!t)t='document';
  return t.toLowerCase().endsWith('.pdf')?t:t+'.pdf';
}
function filenameFromContentDisposition(value){
  if(!value)return null;
  let m=value.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  if(m){try{return decodeURIComponent(m[1].trim().replace(/^['"]|['"]$/g,''))}catch{}}
  m=value.match(/filename\s*=\s*(?:"([^"]+)"|([^;]+))/i);
  return (m&&(m[1]||m[2]))?(m[1]||m[2]).trim():null;
}
function hostedPDFFilename(response,url,title){
  const cd=filenameFromContentDisposition(response.headers.get('content-disposition'));
  if(cd)return cd.toLowerCase().endsWith('.pdf')?cd:cd+'.pdf';
  try{
    const leaf=decodeURIComponent(new URL(response.url||url).pathname.split('/').pop()||'');
    if(/\.pdf$/i.test(leaf))return leaf;
  }catch{}
  return cleanPDFTitle(title);
}
async function tryFetchHostedPDF({url,title}){
  let response;
  try{
    response=await fetch(url,{method:'GET',credentials:'include',cache:'no-store',redirect:'follow',headers:{Accept:'application/pdf,application/octet-stream;q=0.9,*/*;q=0.5'}});
  }catch(e){return null}
  if(!response.ok||!response.body)return null;
  const ctype=(response.headers.get('content-type')||'').toLowerCase();
  const reader=response.body.getReader(); const chunks=[]; let total=0, pdfByBytes=false;
  try{
    while(total<1024){
      const {done,value}=await reader.read(); if(done)break;
      if(value?.length){chunks.push(value);total+=value.length; const probe=new Uint8Array(total); let o=0; for(const c of chunks){probe.set(c,o);o+=c.length} pdfByBytes=bytesContainPDFHeader(probe); if(pdfByBytes)break;}
    }
    const looksPDF=pdfByBytes||ctype.includes('application/pdf');
    if(!looksPDF){await reader.cancel().catch(()=>{});return null}
    archiveProgress('Saving hosted PDF','PDF detected — downloading current document',0);
    while(true){const {done,value}=await reader.read();if(done)break;if(value?.length){chunks.push(value);total+=value.length;archiveProgress('Saving hosted PDF',`downloading — ${(total/1024/1024).toFixed(1)} MB`,null)}}
  }finally{try{reader.releaseLock()}catch{}}
  const bytes=new Uint8Array(total);let off=0;for(const c of chunks){bytes.set(c,off);off+=c.length}
  if(!bytes.length||!bytesContainPDFHeader(bytes))throw new Error('The current address looked like a PDF but did not return valid PDF data.');
  return {base64:uint8ToBase64(bytes),fileName:hostedPDFFilename(response,url,title),finalURL:response.url||url,byteLength:bytes.length};
}
async function smartSaveCurrent({parent,tabId,title,url}){
  // Start favicon discovery at the same time as the save. This makes favicons
  // part of the normal save path rather than something that only appears after
  // the manual "Refresh favicons" repair action. The favicon task is allowed
  // to fail without failing the reading-item save itself.
  const automaticFavicon=(async()=>{
    try{return await faviconFromCurrentTab(tabId,url||'')}catch(_){return ''}
  })();

  const finishAutomaticFavicon=async path=>{
    if(!path)return;
    try{
      const image=await automaticFavicon;
      if(!image)return;
      const cache=await readThumbnailCache();
      if(cache[path]===image)return;
      cache[path]=image;
      await writeThumbnailCache(cache);
    }catch(_){ }
  };

  archiveProgress('Checking page type','Looking for a hosted PDF…',0);
  const hosted=await tryFetchHostedPDF({url,title});
  if(hosted){
    archiveProgress('Downloading PDF','You can leave or close this tab while the PDF finishes saving.',null);
    const r=await nativeSend({cmd:'save_pdf_data',parent,name:hosted.fileName,displayTitle:title||'',pdfBase64:hosted.base64,url:hosted.finalURL},240000);
    if(!r.ok)throw new Error(r.error||'Could not save hosted PDF.');
    await finishAutomaticFavicon(r.path);
    archiveProgress('PDF saved',`${r.path?.split(/[\\/]/).pop()||hosted.fileName} — safe to close this tab.`,100);
    return {...r,saveKind:'pdf'};
  }

  archiveProgress(
    'Capturing HTML',
    'Keep this tab open until StashLibrary says the save is complete — SingleFile is reading the live page.',
    0
  );
  const r=await saveCurrentTabWithSingleFile({parent,tabId,title,url});
  await finishAutomaticFavicon(r.path);
  return {...r,saveKind:'html'};
}



// v0.1.30 direct-to-Zotero capture path, restored from the earlier dedicated
// Zotero connector. This uses the bundled SingleFile engine for live webpages.
async function fetchMissingImageAsDataURL(url) {
  if (!url || url.startsWith("data:") || url.startsWith("blob:")) {
    return null;
  }

  try {
    const response = await fetch(url, {
      credentials: "include",
      cache: "force-cache"
    });

    if (!response.ok) {
      return null;
    }

    const blob = await response.blob();

    // Don't unexpectedly turn one failed decorative image into an enormous
    // snapshot. Leave unusually large failed images untouched.
    if (!blob.size || blob.size > 15 * 1024 * 1024) {
      return null;
    }

    return await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error || new Error("Could not embed image"));
      reader.readAsDataURL(blob);
    });
  }
  catch (_) {
    return null;
  }
}

async function repairOnlyFailedSingleFileImages(html, liveImages) {
  if (!html || !html.includes("data:,") || !Array.isArray(liveImages)) {
    return html;
  }

  /*
   * Work on the HTML string itself instead of parsing/serializing the document.
   * This preserves SingleFile's generated markup byte-for-byte except for the
   * src attribute of an image that SingleFile explicitly failed to save.
   */
  const imgRegex = /<img\b[^>]*>/gi;
  const tags = [];
  let match;

  while ((match = imgRegex.exec(html)) !== null) {
    tags.push({
      index: tags.length,
      start: match.index,
      end: match.index + match[0].length,
      tag: match[0]
    });
  }

  const replacements = [];

  for (const entry of tags) {
    const failedSrcPattern = /\bsrc\s*=\s*(?:"data:,"|'data:,'|data:,)(?=\s|\/?>)/i;

    if (!failedSrcPattern.test(entry.tag)) {
      continue;
    }

    const live = liveImages[entry.index];
    const sourceURL = live && (live.currentSrc || live.src);

    if (!sourceURL || sourceURL === "data:,") {
      continue;
    }

    const dataURL = await fetchMissingImageAsDataURL(sourceURL);

    if (!dataURL) {
      continue;
    }

    const repairedTag = entry.tag.replace(
      failedSrcPattern,
      `src="${dataURL}"`
    );

    replacements.push({
      start: entry.start,
      end: entry.end,
      text: repairedTag
    });
  }

  // Replace from the end so original string offsets remain valid.
  replacements.sort((a, b) => b.start - a.start);

  for (const replacement of replacements) {
    html =
      html.slice(0, replacement.start) +
      replacement.text +
      html.slice(replacement.end);
  }

  return html;
}

async function captureLivePageSnapshot(tab,{repairFailedImages=true}={}) {
  const url = tab.url || "";
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    return null;
  }

  /*
   * Webpage capture is delegated completely to SingleFile.
   *
   * Do not post-process SingleFile's generated HTML here. The whole point of
   * this integration is to preserve the same layout/resource handling that
   * makes the standalone SingleFile Firefox extension work reliably.
   *
   * These options are the official integration example's conservative
   * single-file settings. Scripts/video/audio are blocked in the archived
   * copy, while the page's CSS/images/fonts are collected by SingleFile.
   */
  /*
   * Exact SingleFile profile exported by the user on 2026-08-16.
   * Do not add connector-specific capture overrides here.
   */
  const options = {"removeHiddenElements":true,"removedElementsSelector":"","removeUnusedStyles":true,"removeUnusedFonts":true,"removeFrames":false,"compressHTML":true,"compressCSS":false,"loadDeferredImages":true,"loadDeferredImagesMaxIdleTime":1500,"loadDeferredImagesBlockCookies":false,"loadDeferredImagesBlockStorage":false,"loadDeferredImagesKeepZoomLevel":false,"loadDeferredImagesDispatchScrollEvent":false,"loadDeferredImagesBeforeFrames":false,"filenameTemplate":"%if-empty<{page-title}|No title> ({date-locale} {time-locale}).{filename-extension}","infobarTemplate":"","includeInfobar":false,"openInfobar":false,"confirmInfobarContent":false,"autoClose":false,"confirmFilename":false,"filenameConflictAction":"uniquify","filenameMaxLength":192,"filenameMaxLengthUnit":"bytes","filenameReplacedCharacters":["~","+","?","%","*",":","|","\"","<",">","\\\\","\u0000-\u001f",""],"filenameReplacementCharacter":"_","filenameReplacementCharacters":["～","＋","？","％","＊","：","｜","＂","＜","＞","＼"],"replaceEmojisInFilename":false,"saveFilenameTemplateData":false,"contextMenuEnabled":true,"tabMenuEnabled":true,"browserActionMenuEnabled":true,"shadowEnabled":true,"logsEnabled":true,"progressBarEnabled":true,"maxResourceSizeEnabled":false,"maxResourceSize":10,"displayInfobar":true,"displayStats":false,"backgroundSave":true,"defaultEditorMode":"normal","applySystemTheme":true,"contentWidth":70,"autoSaveDelay":1,"autoSaveLoad":false,"autoSaveUnload":false,"autoSaveLoadOrUnload":true,"autoSaveDiscard":false,"autoSaveRemove":false,"autoSaveRepeat":false,"autoSaveRepeatDelay":10,"removeAlternativeFonts":true,"removeAlternativeMedias":true,"removeAlternativeImages":true,"groupDuplicateImages":true,"maxSizeDuplicateImages":524288,"saveRawPage":false,"saveToClipboard":false,"addProof":false,"saveToGDrive":false,"saveToDropbox":false,"saveWithWebDAV":false,"webDAVURL":"","webDAVUser":"","webDAVPassword":"","saveWithMCP":false,"mcpServerUrl":"","mcpAuthToken":"","saveToGitHub":false,"saveToRestFormApi":false,"saveToS3":false,"githubToken":"","githubUser":"","githubRepository":"SingleFile-Archives","githubBranch":"main","saveWithCompanion":false,"sharePage":false,"forceWebAuthFlow":false,"resolveFragmentIdentifierURLs":false,"userScriptEnabled":false,"openEditor":false,"openSavedPage":false,"autoOpenEditor":false,"saveCreatedBookmarks":false,"allowedBookmarkFolders":[],"ignoredBookmarkFolders":[],"replaceBookmarkURL":true,"saveFavicon":true,"includeBOM":false,"warnUnsavedPage":true,"displayInfobarInEditor":false,"compressContent":false,"createRootDirectory":false,"selfExtractingArchive":false,"disableCompression":false,"extractDataFromPage":false,"preventAppendedData":false,"insertEmbeddedImage":false,"insertEmbeddedScreenshotImage":false,"insertTextBody":false,"autoSaveExternalSave":false,"insertMetaNoIndex":false,"insertMetaCSP":true,"passReferrerOnError":false,"password":"","insertSingleFileComment":true,"removeSavedDate":false,"blockMixedContent":false,"saveOriginalURLs":false,"acceptHeaders":{"font":"application/font-woff2;q=1.0,application/font-woff;q=0.9,*/*;q=0.8","image":"image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8","stylesheet":"text/css,*/*;q=0.1","script":"*/*","document":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","video":"video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5","audio":"audio/webm,audio/ogg,audio/wav,audio/*;q=0.9,application/ogg;q=0.7,video/*;q=0.6,*/*;q=0.5"},"moveStylesInHead":false,"networkTimeout":0,"woleetKey":"","blockImages":false,"blockAlternativeImages":true,"blockStylesheets":false,"blockFonts":false,"blockScripts":true,"blockVideos":true,"blockAudios":true,"delayBeforeProcessing":0,"delayAfterProcessing":0,"_migratedTemplateFormat":true,"saveToRestFormApiUrl":"","saveToRestFormApiFileFieldName":"","saveToRestFormApiUrlFieldName":"","saveToRestFormApiToken":"","S3Domain":"s3.amazonaws.com","S3Region":"","S3Bucket":"","S3AccessKey":"","S3SecretKey":"","resolveLinks":true,"groupDuplicateStylesheets":false,"infobarPositionAbsolute":false,"infobarPositionTop":"16px","infobarPositionRight":"16px","infobarPositionBottom":"","infobarPositionLeft":"","removeNoScriptTags":true,"customShortcut":null,"imageReductionFactor":1};

  let results;

  try {
    results = await browser.tabs.executeScript(tab.id, {
      code: `(async () => {
        if (typeof extension === "undefined" ||
            typeof extension.getPageData !== "function") {
          throw new Error("StashLibrary's bundled SingleFile 1.22.98 capture engine is not loaded. Reload StashLibrary and the webpage.");
        }

        // Record the exact image resources Firefox is currently rendering
        // before SingleFile processes the document.
        const liveImages = [...document.images].map(img => ({
          currentSrc: img.currentSrc || "",
          src: img.src || ""
        }));

        const result = await extension.getPageData(${JSON.stringify(options)});
        return {
          html: result.content,
          title: result.title || document.title || "",
          filename: result.filename || "",
          url: location.href,
          singleFile: true,
          liveImages
        };
      })();`
    });
  }
  catch (error) {
    const message = String(error && error.message || error || "");

    if (/SingleFile capture engine is not loaded/i.test(message)) {
      throw error;
    }

    // Built-in Firefox PDF viewer / restricted pages are handled by the
    // hosted-PDF fallback in runSaveOperation().
    throw error;
  }

  const captured = results && results[0] ? results[0] : null;

  if (!captured || !captured.html) {
    throw new Error("SingleFile could not capture this page.");
  }

  if(repairFailedImages){
    captured.html = await repairOnlyFailedSingleFileImages(
      captured.html,
      captured.liveImages
    );
  }

  // liveImages is only capture-time metadata and should not leave the background page.
  delete captured.liveImages;

  return captured;
}



async function directSendCurrentToZotero(msg={}){
  let tab=null;
  if(msg.tabId!=null){ try{tab=await browser.tabs.get(msg.tabId)}catch(_){} }
  if(!tab){ const tabs=await browser.tabs.query({active:true,currentWindow:true}); tab=tabs[0]; }
  const url=tab?.url||msg.url||'';
  const title=tab?.title||msg.title||'';
  if(!url) throw new Error('No current page or file was found.');
  if(!/^(https?|file):/i.test(url)) throw new Error('Send Current Page to Zotero supports webpages and PDF/HTML files opened in Firefox.');
  const accessedAt=new Date().toISOString();
  const target=msg.target||'';

  // A StashLibrary viewer page is an archived bookmark, not a new live-page visit.
  // Route it through the bookmark path so its SQL source/access date win.
  if(/^http:\/\/127\.0\.0\.1:\d+\/view\//i.test(url)){
    const archived=await nativeSend({cmd:'send_friendly_url_to_zotero',url,target},300000);
    if(archived?.ok&&archived?.matched)return archived;
  }

  if(/^file:/i.test(url)){
    archiveProgress('Send Current Page to Zotero','Importing the local file into Zotero',0);
    const r=await nativeSend({cmd:'zotero_direct_save',url,title,accessedAt,target},300000);
    if(!r?.ok) throw new Error(r?.error||'Zotero could not import the local file.');
    return r;
  }

  // Capture live webpages from the browser first. Asking Zotero to download the
  // URL in its own session can return a perfectly valid HTTP page that is only
  // a cookie/consent barrier. Because that looks like a successful request,
  // Zotero cannot know that it should fall back. SingleFile captures the page
  // the user is actually viewing, including its authenticated/consented state,
  // and matches the reliable Save-to-StashLibrary-then-send path.
  let snapshotError=null;
  archiveProgress('Send Current Page to Zotero','Capturing the loaded webpage from Firefox',0);
  try{
    const snapshot=await captureLivePageSnapshot(tab);
    const r=await nativeSend({
      cmd:'zotero_direct_save',
      url,
      originalURL:url,
      title:snapshot?.title||title,
      snapshotHTML:snapshot?.html||'',
      accessedAt,
      target
    },300000);
    if(!r?.ok)throw new Error(r?.error||'Zotero could not import the webpage snapshot.');
    archiveProgress('Send Current Page to Zotero','Saved the Firefox page snapshot to Zotero',100);
    return r;
  }catch(error){
    snapshotError=error;
  }

  // Restricted browser pages such as Firefox's built-in PDF viewer cannot be
  // scripted. Let Zotero fetch those URLs directly after browser capture has
  // proved unavailable.
  archiveProgress('Send Current Page to Zotero','Firefox capture unavailable — asking Zotero to fetch the URL',null);
  const zoteroDirect=await nativeSend(
    {cmd:'zotero_direct_url',url,title,accessedAt,target},
    240000
  );
  if(zoteroDirect?.ok){
    archiveProgress('Send Current Page to Zotero','Saved directly by Zotero',100);
    return zoteroDirect;
  }

  const hosted=await tryFetchHostedPDF({url,title});
  if(hosted){
    archiveProgress('Send Current Page to Zotero',`Sending ${hosted.fileName} to Zotero`,null);
    const r=await nativeSend({
      cmd:'zotero_direct_save',
      url:hosted.finalURL,
      originalURL:url,
      title,
      pdfBase64:hosted.base64,
      pdfFileName:hosted.fileName,
      accessedAt,
      target
    },300000);
    if(!r?.ok) throw new Error(r?.error||'Zotero could not import the PDF.');
    return r;
  }

  const browserError=snapshotError ? ` Firefox capture: ${snapshotError.message||snapshotError}.` : '';
  const directError=zoteroDirect?.error ? ` Zotero capture: ${zoteroDirect.error}` : '';
  throw new Error(`Could not send the current page to Zotero.${browserError}${directError}`);
}



const STASHLIBRARY_THUMBNAIL_KEY='stashlibraryBookmarkThumbnails';
const STASHLIBRARY_FAVICON_JOB_KEY='stashlibraryFaviconRefreshJob';
let faviconBatchRunning=false;

async function publishFaviconProgress(payload){
  const state={...payload,updatedAt:Date.now()};
  await browser.storage.local.set({[STASHLIBRARY_FAVICON_JOB_KEY]:state}).catch(()=>{});
  broadcast({type:'thumbnail-progress',payload:state});
}

async function readThumbnailCache(){
  try{
    const r=await browser.storage.local.get(STASHLIBRARY_THUMBNAIL_KEY);
    return r?.[STASHLIBRARY_THUMBNAIL_KEY] && typeof r[STASHLIBRARY_THUMBNAIL_KEY]==='object'
      ? r[STASHLIBRARY_THUMBNAIL_KEY] : {};
  }catch(_){return {}}
}
async function writeThumbnailCache(cache){
  await browser.storage.local.set({[STASHLIBRARY_THUMBNAIL_KEY]:cache||{}});
}

function absoluteURL(value,base){
  try{return new URL(value,base).href}catch(_){return ''}
}

async function blobToDataURL(blob){
  return new Promise((resolve,reject)=>{
    const fr=new FileReader();
    fr.onload=()=>resolve(String(fr.result||''));
    fr.onerror=()=>reject(fr.error||new Error('Could not read favicon.'));
    fr.readAsDataURL(blob);
  });
}

async function fetchWithTimeout(url,options={},timeoutMs=10000){
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),Math.max(1000,Number(timeoutMs)||10000));
  try{return await fetch(url,{...options,signal:controller.signal})}
  finally{clearTimeout(timer)}
}

async function fetchImageDataURL(url){
  if(!url)return '';
  try{
    const r=await fetchWithTimeout(url,{credentials:'include',redirect:'follow',cache:'force-cache'},8000);
    if(!r.ok)return '';
    const blob=await r.blob();
    if(!blob || !blob.size)return '';
    return await blobToDataURL(blob);
  }catch(_){return ''}
}

async function faviconFromPageURL(pageURL){
  if(!/^https?:/i.test(String(pageURL||'')))return '';

  let parsed;
  try{parsed=new URL(pageURL)}catch(_){return ''}

  // Prefer icon declarations from an HTML page when available.
  try{
    const r=await fetchWithTimeout(pageURL,{credentials:'include',redirect:'follow',cache:'force-cache'},10000);
    const type=String(r.headers.get('content-type')||'').toLowerCase();

    if(r.ok && /html|xhtml/.test(type)){
      const text=await r.text();
      const doc=new DOMParser().parseFromString(text,'text/html');
      const icons=[...doc.querySelectorAll(
        'link[rel~="icon"],link[rel="shortcut icon"],link[rel="apple-touch-icon"],link[rel="apple-touch-icon-precomposed"]'
      )];

      // Prefer normal favicons before large touch icons.
      icons.sort((a,b)=>{
        const ar=String(a.getAttribute('rel')||'').toLowerCase();
        const br=String(b.getAttribute('rel')||'').toLowerCase();
        return Number(/apple-touch/.test(ar))-Number(/apple-touch/.test(br));
      });

      for(const el of icons){
        const href=absoluteURL(el.getAttribute('href')||'',r.url||pageURL);
        const data=await fetchImageDataURL(href);
        if(data)return data;
      }
    }
  }catch(_){}

  // For direct PDFs or pages that block the exact article request, try the
  // site's homepage for its declared icons before falling back to /favicon.ico.
  try{
    const origin=`${parsed.protocol}//${parsed.host}/`;
    const home=await fetchWithTimeout(origin,{credentials:'include',redirect:'follow',cache:'force-cache'},8000);
    const type=String(home.headers.get('content-type')||'').toLowerCase();
    if(home.ok && /html|xhtml/.test(type)){
      const text=await home.text();
      const doc=new DOMParser().parseFromString(text,'text/html');
      const icons=[...doc.querySelectorAll(
        'link[rel~="icon"],link[rel="shortcut icon"],link[rel="apple-touch-icon"],link[rel="apple-touch-icon-precomposed"]'
      )];
      for(const el of icons){
        const href=absoluteURL(el.getAttribute('href')||'',home.url||origin);
        const data=await fetchImageDataURL(href);
        if(data)return data;
      }
    }
  }catch(_){}

  const fallback=`${parsed.protocol}//${parsed.host}/favicon.ico`;
  return await fetchImageDataURL(fallback);
}

async function faviconFromCurrentTab(tabId,pageURL=''){
  try{
    const tab=await browser.tabs.get(tabId);
    if(tab?.favIconUrl){
      const data=await fetchImageDataURL(tab.favIconUrl);
      if(data)return data;
    }
    const url=tab?.url||pageURL||'';
    if(url)return await faviconFromPageURL(url);
  }catch(_){}
  return pageURL ? faviconFromPageURL(pageURL) : '';
}

async function generateFaviconBatch(items=[],force=false){
  if(faviconBatchRunning)return {ok:false,error:'Favicon refresh is already running.'};
  faviconBatchRunning=true;
  try{
    const cache=await readThumbnailCache();
    const jobs=(items||[]).filter(x=>x?.path&&(force||!cache[x.key||x.path]));
    let done=0,updated=0,unchanged=0,kept=0,missing=0;
    await publishFaviconProgress({running:true,done,total:jobs.length,updated,unchanged,kept,missing,finished:false});

    for(const item of jobs){
      const key=item.key||item.path;
      const previous=cache[key]||'';
      try{
        const image=await faviconFromPageURL(item.sourceUrl||'');
        if(image){
          if(image===previous)unchanged++;
          else{cache[key]=image;updated++;await writeThumbnailCache(cache)}
        }else if(previous)kept++;
        else missing++;
      }catch(_){
        if(previous)kept++;
        else missing++;
      }

      done++;
      await publishFaviconProgress({running:true,done,total:jobs.length,updated,unchanged,kept,missing,finished:false});
      await new Promise(r=>setTimeout(r,90));
    }

    await publishFaviconProgress({running:false,done,total:jobs.length,updated,unchanged,kept,missing,finished:true});
    return {ok:true,done,total:jobs.length,updated,unchanged,kept,missing};
  }catch(error){
    await publishFaviconProgress({running:false,finished:true,error:error?.message||String(error)});
    throw error;
  }finally{
    faviconBatchRunning=false;
  }
}

function startFaviconBatch(items=[],force=false){
  if(faviconBatchRunning)return {ok:false,error:'Favicon refresh is already running.'};
  generateFaviconBatch(items,force).catch(()=>{});
  return {ok:true,started:true};
}

async function captureCurrentThumbnail(msg={}){
  if(msg.tabId==null||!msg.key)return {ok:false,error:'Missing bookmark or tab.'};
  try{
    const image=await faviconFromCurrentTab(msg.tabId,msg.sourceUrl||'');
    if(!image)return {ok:false,error:'No favicon was found.'};
    const cache=await readThumbnailCache();
    cache[msg.key]=image;
    await writeThumbnailCache(cache);
    return {ok:true};
  }catch(e){
    return {ok:false,error:e?.message||String(e)};
  }
}


// Coordinated StashLibrary release update state. Firefox downloads its XPI via
// update_url; the Windows installer is staged in Downloads; Zotero updates its
// own XPI through the local companion endpoint.
let pendingStashLibraryFirefoxUpdate=null;
if(browser.runtime.onUpdateAvailable){
  browser.runtime.onUpdateAvailable.addListener(details=>{
    pendingStashLibraryFirefoxUpdate=details||{};
    browser.storage.local.set({stashlibraryPendingFirefoxVersion:String(details?.version||'')}).catch(()=>{});
  });
}
async function stageStashLibraryWindowsUpdate(url,version){
  if(!/^https:\/\//i.test(String(url||'')))return {ok:false,error:'Windows update URL must use HTTPS.'};
  const safeVersion=String(version||'latest').replace(/[^0-9A-Za-z._-]+/g,'_');
  const filename=`StashLibrary Updates/StashLibrary-Windows-Helper-${safeVersion}.exe`;
  const existing=await browser.downloads.search({url:String(url),state:'complete',limit:5}).catch(()=>[]);
  if(existing?.length)return {ok:true,downloadId:existing[0].id,alreadyDownloaded:true};
  const downloadId=await browser.downloads.download({url:String(url),filename,conflictAction:'overwrite',saveAs:false});
  return await new Promise(resolve=>{
    let finished=false;
    const done=result=>{if(finished)return;finished=true;clearTimeout(timer);try{browser.downloads.onChanged.removeListener(listener);}catch(_){}resolve(result);};
    const listener=delta=>{
      if(delta?.id!==downloadId)return;
      if(delta.state?.current==='complete')done({ok:true,downloadId});
      else if(delta.state?.current==='interrupted')done({ok:false,error:`Windows update download was interrupted${delta.error?.current?`: ${delta.error.current}`:''}.`});
    };
    browser.downloads.onChanged.addListener(listener);
    const timer=setTimeout(async()=>{
      const rows=await browser.downloads.search({id:downloadId}).catch(()=>[]);
      if(rows?.[0]?.state==='complete')done({ok:true,downloadId});
      else done({ok:false,error:'Windows update is still downloading. Leave Settings open for a moment, then try again.'});
    },120000);
  });
}
async function triggerZoteroStashLibraryUpdate(expectedVersion){
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),15000);
  try{
    const response=await fetch('http://127.0.0.1:23119/local-file-connector/update',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expectedVersion:String(expectedVersion||'')}),signal:controller.signal
    });
    const text=await response.text();
    let data={};try{data=JSON.parse(text);}catch(_){data={ok:false,error:text||`Zotero returned ${response.status}`};}
    if(!response.ok||!data.ok)return {ok:false,error:data.error||`Zotero returned ${response.status}`};
    return data;
  }catch(e){return {ok:false,error:e?.name==='AbortError'?'Zotero update request timed out.':(e?.message||String(e))};}
  finally{clearTimeout(timer);}
}
async function requestStashLibraryFirefoxUpdate(){
  if(!browser.runtime.requestUpdateCheck)return {ok:false,error:'This Firefox version does not expose requestUpdateCheck().'};
  try{
    const result=await browser.runtime.requestUpdateCheck();
    const status=typeof result==='string'?result:String(result?.status||'');
    return {ok:true,status,pendingVersion:String(pendingStashLibraryFirefoxUpdate?.version||'')};
  }catch(e){return {ok:false,error:e?.message||String(e)};
  }
}

browser.runtime.onMessage.addListener((msg)=>{
  if(msg?.type==='close-active-tab-keep-stashlibrary') return (async()=>{
    const tabs=await browser.tabs.query({active:true,currentWindow:true});
    const tab=tabs&&tabs[0];
    if(tab?.id==null)return {ok:false,error:'No active page tab found.'};
    await browser.tabs.remove(tab.id);
    await new Promise(resolve=>setTimeout(resolve,80));
    try{await browser.browserAction.openPopup()}catch(_){}
    return {ok:true};
  })();
  if(msg?.type==='thumbnail-capture-current') return captureCurrentThumbnail(msg);
  if(msg?.type==='thumbnail-start-batch') return Promise.resolve(startFaviconBatch(msg.items||[],!!msg.force));
  if(msg?.type==='thumbnail-generate-batch') return generateFaviconBatch(msg.items||[],!!msg.force);
  if(msg?.type==='native-send') return nativeSend(msg.payload);
  if(msg?.type==='stashlibrary-stage-windows-update') return stageStashLibraryWindowsUpdate(msg.url,msg.version);
  if(msg?.type==='stashlibrary-zotero-update') return triggerZoteroStashLibraryUpdate(msg.expectedVersion);
  if(msg?.type==='stashlibrary-firefox-update-check') return requestStashLibraryFirefoxUpdate();
  if(msg?.type==='stashlibrary-apply-firefox-update'){ browser.runtime.reload(); return Promise.resolve({ok:true}); }
  if(msg?.type==='webdav-job-start') return startWebdavJob(msg.kind);
  if(msg?.type==='webdav-job-status') return Promise.resolve({ok:true,job:webdavJobSnapshot()});
  if(msg?.type==='native-reconnect') return reconnectNative();
  if(msg?.type==='native-probe') return probeNative(Number(msg.timeout)||3000);
  if(msg?.type==='native-pause') return pauseNativeReconnect();
  if(msg?.type==='singlefile-save-current') return saveCurrentTabWithSingleFile(msg);
  if(msg?.type==='smart-save-current') return smartSaveCurrent(msg);
  if(msg?.type==='direct-zotero-current') return directSendCurrentToZotero(msg);
});


// Backups are intentionally manual-only. They run only from the explicit Create Backup Now action.


// StashLibrary browser-action popup presence and global keyboard toggle.
let stashlibraryPopupOpen=false;
browser.runtime.onConnect.addListener(port=>{
  if(port.name!=='stashlibrary-popup-presence')return;
  stashlibraryPopupOpen=true;
  port.onDisconnect.addListener(()=>{stashlibraryPopupOpen=false});
});
if(browser.commands?.onCommand){
  browser.commands.onCommand.addListener(async command=>{
    if(command!=='toggle-stashlibrary')return;
    try{
      if(stashlibraryPopupOpen){
        await browser.runtime.sendMessage({type:'close-stashlibrary-popup'});
      }else{
        await browser.browserAction.openPopup();
      }
    }catch(e){
      console.warn('StashLibrary popup toggle failed',e);
    }
  });
}
