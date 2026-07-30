const state={authenticated:false,auth:null,config:null,connections:null,serviceStatus:null,runtime:null,recovery:null,recoveryDiagnoses:new Map(),operations:[],queue:[],reconcileQueue:[],queueSummary:{move:{total:0,terminal:0},reconcile:{total:0,terminal:0}},operationEvents:new Map(),selectedHistory:new Set(),securityEvents:[],sessions:[],plan:null,movePlan:null,movePlanGeneration:0,moveTorrent:null,qbitCatalog:null,routingAudit:null,hiddenMoveColumns:new Set(),syncApp:'radarr',sync:{},safeSyncPlans:{},safeWorkflow:null,syncExpanded:new Set(),syncHiddenStatuses:{radarr:new Set(),sonarr:new Set()},operationSections:{completed:false,remaining:false},operationTracking:false,operationTrackingGeneration:0,operationHidden:false,currentOperation:null};
const $=(s,r=document)=>r.querySelector(s);const $$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const fmtTime=v=>v?new Date(v*1000).toLocaleString(): '—';
const fmtBytes=v=>{const n=Number(v||0);if(!n)return '0 B';const units=['B','KiB','MiB','GiB','TiB'];const i=Math.min(Math.floor(Math.log(n)/Math.log(1024)),units.length-1);return `${(n/1024**i).toFixed(i?2:0)} ${units[i]}`};
const badge=v=>`<span class="badge ${esc(String(v).toLowerCase())}">${esc(String(v).replaceAll('_',' '))}</span>`;
const poolForPath=path=>state.config?.pools.find(p=>String(path||'').startsWith(p.prefix))?.name||'unknown pool';
const RECONCILE_UNCHANGED_PAIR_STATUSES=new Set(['linked','already-on-target','verified-derived']);
const RECONCILE_ACTIONABLE_AUXILIARY_STATUSES=new Set(['torrent-sidecar','missing-target','target-exists-same-size']);
const reconcilePrimaryNeedsRepair=plan=>{
  const current=String(plan?.current_item_path||'').replace(/\/+$/,'');
  const target=String(plan?.target_item_path||'').replace(/\/+$/,'');
  return Boolean(current&&target&&current!==target)||(plan?.pairs||[]).some(pair=>!RECONCILE_UNCHANGED_PAIR_STATUSES.has(pair.status));
};
const reconcileAuxiliaryNeedsRepair=item=>RECONCILE_ACTIONABLE_AUXILIARY_STATUSES.has(item.status);
const reconcileHasSelectedWork=plan=>reconcilePrimaryNeedsRepair(plan)||$$('.aux-file:checked').length>0;
function renderBuildVersion(source){
  const version=source?.version||state.config?.version||state.serviceStatus?.version||'—';
  const commit=source?.commit||state.config?.commit||state.serviceStatus?.commit||'unknown';
  const shortCommit=commit==='unknown'?'unknown':commit.slice(0,12);
  const node=$('#side-version');
  node.textContent=`Version ${version} · ${shortCommit}`;
  node.title=commit==='unknown'?'Build commit was not embedded in this image':`Git commit ${commit}`;
}
async function api(path,options={}){const method=(options.method||'GET').toUpperCase();const headers=new Headers(options.headers||{});if(method!=='GET'&&method!=='HEAD')headers.set('X-Stowarr-CSRF','1');const response=await fetch(path,{...options,headers,credentials:'same-origin'});if(response.status===401&&path!=='/api/auth/login'){state.authenticated=false;showLogin()}if(!response.ok)throw new Error((await response.json().catch(()=>({}))).error||`HTTP ${response.status}`);return response.json()}
async function streamApi(path,options={},onProgress=()=>{}){
  const method=(options.method||'GET').toUpperCase();
  const headers=new Headers(options.headers||{});
  if(method!=='GET'&&method!=='HEAD')headers.set('X-Stowarr-CSRF','1');
  const response=await fetch(path,{...options,headers,credentials:'same-origin'});
  if(response.status===401){state.authenticated=false;showLogin()}
  if(!response.ok)throw new Error((await response.json().catch(()=>({}))).error||`HTTP ${response.status}`);
  if(!response.body)throw new Error('Progress stream is unavailable');
  const reader=response.body.getReader();
  const decoder=new TextDecoder();
  let buffer='';
  let result;
  const consume=line=>{
    if(!line.trim())return;
    const event=JSON.parse(line);
    if(event.type==='progress')onProgress(event);
    else if(event.type==='result')result=event.result;
    else if(event.type==='error')throw new Error(event.error||'Safe workflow failed');
  };
  while(true){
    const {value,done}=await reader.read();
    buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});
    const lines=buffer.split('\n');
    buffer=lines.pop()||'';
    lines.forEach(consume);
    if(done)break;
  }
  consume(buffer);
  if(result===undefined)throw new Error('Safe workflow ended without a result');
  return result;
}
function showLogin(message=''){const dialog=$('#login-dialog');$('#login-error').textContent=message;$('#login-error').classList.toggle('hidden',!message);if(!dialog.open)dialog.showModal()}
async function bootstrap(){try{const auth=await api('/api/auth/status');state.auth=auth;state.authenticated=auth.authenticated;if(auth.authenticated)load();else showLogin(auth.method==='external'?'External authentication did not provide a trusted username. Access Stowarr through the configured authentication proxy.':'')}catch(e){showLogin(e.message)}}
async function login(event){event.preventDefault();const form=event.currentTarget;const button=form.querySelector('button');button.disabled=true;button.textContent='Signing in…';try{await api('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:form.elements.username.value,password:form.elements.password.value})});state.authenticated=true;form.reset();form.elements.username.value='admin';$('#login-dialog').close();await load()}catch(e){showLogin(e.message)}finally{button.disabled=false;button.textContent='Sign in'}}
async function logout(){try{await api('/api/auth/logout',{method:'POST'});}finally{state.authenticated=false;state.config=null;showLogin();}}
async function changePassword(event){event.preventDefault();const form=event.currentTarget;const current=form.elements['current-password'].value;const password=form.elements['new-password'].value;const confirmation=form.elements['confirm-password'].value;if(password!==confirmation){toast('New passwords do not match');return}const button=form.querySelector('button');button.disabled=true;try{await api('/api/auth/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({currentPassword:current,newPassword:password})});form.reset();state.authenticated=false;showLogin('Password changed. Sign in again.')}catch(e){toast(`Password was not changed: ${e.message}`)}finally{button.disabled=false}}
function toast(message){const node=$('#toast');node.textContent=message;node.classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>node.classList.remove('show'),2600)}
function confirmAction({title,message='',details=[],confirmLabel='Confirm',danger=false}){const dialog=$('#confirm-dialog');$('#confirm-title').textContent=title;$('#confirm-message').textContent=message;$('#confirm-details').innerHTML=details.map(([label,value])=>`<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('');const accept=$('#confirm-accept');accept.textContent=confirmLabel;accept.className=danger?'danger':'primary';if(dialog.open)dialog.close();dialog.showModal();return new Promise(resolve=>{let settled=false;const finish=result=>{if(settled)return;settled=true;dialog.removeEventListener('cancel',onCancel);dialog.removeEventListener('close',onClose);$('#confirm-cancel').removeEventListener('click',onCancel);$('#confirm-close').removeEventListener('click',onCancel);accept.removeEventListener('click',onAccept);if(dialog.open)dialog.close();resolve(result)};const onCancel=event=>{event?.preventDefault();finish(false)};const onClose=()=>finish(false);const onAccept=()=>finish(true);dialog.addEventListener('cancel',onCancel);dialog.addEventListener('close',onClose);$('#confirm-cancel').addEventListener('click',onCancel);$('#confirm-close').addEventListener('click',onCancel);accept.addEventListener('click',onAccept)})}
function startSafeWorkflow(title,summary,steps){
  state.safeWorkflow={title,summary,steps:steps.map(step=>({...step,current:0,total:1,message:step.description,status:'remaining'})),terminal:false,error:'',confirmation:null,hidden:false};
  state.safeWorkflow.steps[0].status='active';
  renderSafeWorkflow();
  const dialog=$('#safe-workflow-dialog');
  if(!dialog.open)dialog.showModal();
}
function updateSafeWorkflow(event){
  const workflow=state.safeWorkflow;
  if(!workflow)return;
  const index=workflow.steps.findIndex(step=>step.id===event.stage);
  if(index<0)return;
  workflow.steps.forEach((step,stepIndex)=>{
    if(stepIndex<index)step.status='complete';
    else if(stepIndex===index)step.status='active';
    else if(step.status!=='complete')step.status='remaining';
  });
  const step=workflow.steps[index];
  step.current=Math.max(0,Number(event.current||0));
  step.total=Math.max(0,Number(event.total||0));
  step.message=event.message||step.description;
  if(step.total===0||step.current>=step.total){
    step.status='complete';
    if(workflow.steps[index+1])workflow.steps[index+1].status='active';
  }
  renderSafeWorkflow();
}
function finishSafeWorkflow(summary){
  if(!state.safeWorkflow)return;
  state.safeWorkflow.steps.forEach(step=>{step.status='complete';step.current=step.total||1;step.total=step.total||1});
  state.safeWorkflow.summary=summary;
  state.safeWorkflow.confirmation=null;
  state.safeWorkflow.terminal=true;
  renderSafeWorkflow();
}
function confirmSafeWorkflowPhase({summary,title,message,details,confirmLabel,onConfirm}){
  if(!state.safeWorkflow)return;
  state.safeWorkflow.steps.forEach(step=>{step.status='complete';step.current=step.total||1;step.total=step.total||1});
  state.safeWorkflow.summary=summary;
  state.safeWorkflow.confirmation={title,message,details,confirmLabel,onConfirm};
  state.safeWorkflow.terminal=true;
  renderSafeWorkflow();
}
function failSafeWorkflow(error){
  if(!state.safeWorkflow)return;
  const active=state.safeWorkflow.steps.find(step=>step.status==='active');
  if(active)active.status='failed';
  state.safeWorkflow.error=error;
  state.safeWorkflow.confirmation=null;
  state.safeWorkflow.summary='The workflow stopped safely';
  state.safeWorkflow.terminal=true;
  renderSafeWorkflow();
}
function renderSafeWorkflow(){
  const workflow=state.safeWorkflow;
  if(!workflow)return;
  $('#safe-workflow-title').textContent=workflow.title;
  $('#safe-workflow-summary').textContent=workflow.summary;
  const fractions=workflow.steps.map(step=>step.status==='complete'?1:step.status==='remaining'?0:step.total?Math.min(1,step.current/step.total):0);
  const overall=Math.round(100*fractions.reduce((sum,value)=>sum+value,0)/Math.max(1,fractions.length));
  $('#safe-workflow-overall').innerHTML=`<div class="operation-step-title"><strong>${workflow.terminal?(workflow.error?'Stopped':'All steps complete'):'Workflow progress'}</strong><b>${overall}% overall</b></div><div class="operation-progress"><i style="width:${overall}%"></i></div>`;
  $('#safe-workflow-steps').innerHTML=workflow.steps.map((step,index)=>{
    const fraction=step.status==='complete'?1:step.total?Math.min(1,step.current/step.total):0;
    const count=step.status==='remaining'?'Waiting':step.status==='failed'?'Stopped':step.total>1?`${Math.min(step.current,step.total)} / ${step.total}`:step.status==='complete'?'Complete':'Working';
    return `<li class="${esc(step.status)}"><span class="safe-step-index">${step.status==='complete'?'✓':index+1}</span><div><div class="operation-step-title"><strong>${esc(step.title)}</strong><b>${esc(count)}</b></div><div class="operation-progress"><i style="width:${Math.round(fraction*100)}%"></i></div><small>${esc(step.message)}</small></div></li>`;
  }).join('');
  $('#safe-workflow-error').textContent=workflow.error;
  $('#safe-workflow-error').classList.toggle('hidden',!workflow.error);
  const confirmation=$('#safe-workflow-confirmation');
  confirmation.classList.toggle('hidden',!workflow.confirmation);
  if(workflow.confirmation){
    $('#safe-workflow-confirm-title').textContent=workflow.confirmation.title;
    $('#safe-workflow-confirm-message').textContent=workflow.confirmation.message;
    $('#safe-workflow-confirm-details').innerHTML=workflow.confirmation.details.map(([label,value])=>`<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('');
  }else{
    $('#safe-workflow-confirm-details').innerHTML='';
  }
  const cancel=$('#safe-workflow-cancel');
  const done=$('#safe-workflow-done');
  cancel.classList.toggle('hidden',!workflow.confirmation);
  done.textContent=!workflow.terminal?'Hide':workflow.confirmation?.confirmLabel||'Close';
  done.disabled=false;
  $('#safe-workflow-close').disabled=false;
  $('#safe-workflow-close').setAttribute('aria-label',workflow.terminal&&!workflow.confirmation?'Close Safe workflow':'Hide Safe workflow');
  renderSafeWorkflowMinimized();
}
function renderSafeWorkflowMinimized(){
  const launcher=$('#safe-workflow-minimized');
  const workflow=state.safeWorkflow;
  const visible=Boolean(workflow?.hidden);
  launcher.classList.toggle('hidden',!visible);
  if(!visible)return;
  const failed=Boolean(workflow.error);
  const complete=workflow.terminal&&!workflow.confirmation&&!failed;
  const active=workflow.steps.find(step=>step.status==='active');
  const title=failed?'Safe workflow needs attention':workflow.confirmation?'Safe workflow awaits confirmation':complete?'Safe workflow complete':'Safe workflow in progress…';
  const summary=workflow.confirmation?.title||active?.message||workflow.summary;
  $('#safe-workflow-minimized-title').textContent=title;
  $('#safe-workflow-minimized-summary').textContent=summary;
  $('#safe-workflow-minimized-dot').className=`dot ${failed?'failed':complete?'ok':'warn'}`;
  launcher.classList.toggle('failed',failed);
  launcher.classList.toggle('complete',complete);
}
function hideSafeWorkflow(){
  if(!state.safeWorkflow)return;
  state.safeWorkflow.hidden=true;
  const dialog=$('#safe-workflow-dialog');
  if(dialog.open)dialog.close();
  renderSafeWorkflowMinimized();
}
function showSafeWorkflow(){
  if(!state.safeWorkflow)return;
  state.safeWorkflow.hidden=false;
  renderSafeWorkflow();
  const dialog=$('#safe-workflow-dialog');
  if(!dialog.open)dialog.showModal();
}
function closeSafeWorkflow(){
  if(!state.safeWorkflow?.terminal)return;
  const dialog=$('#safe-workflow-dialog');
  if(dialog.open)dialog.close();
  state.safeWorkflow=null;
  renderSafeWorkflowMinimized();
}
async function continueSafeWorkflow(){
  const confirmation=state.safeWorkflow?.confirmation;
  if(!state.safeWorkflow?.terminal){hideSafeWorkflow();return}
  if(!confirmation){closeSafeWorkflow();return}
  state.safeWorkflow.confirmation=null;
  state.safeWorkflow.terminal=false;
  renderSafeWorkflow();
  await confirmation.onConfirm();
}
const MOVE_PROGRESS=[['MOVE_PLANNED','Plan accepted','The operation is bound to the reviewed torrent and destination.'],['MOVE_PAUSED','Torrent paused','Writes are stopped before the storage path changes.'],['MOVE_ISOLATED','Torrent isolated','A temporary Stowarr category prevents *Arr cleanup during the transaction.'],['MOVE_RELOCATING','Torrent data relocating','qBittorrent moves its tracked files to the destination pool.'],['MOVE_RECHECKING','qBittorrent rechecking','Stowarr observes qBittorrent enter checking and follows its reported progress.'],['MOVE_QBIT_COMPLETE','Torrent content verified','The recheck finished and every selected qBittorrent file is visible.'],['MOVE_ARCHIVE_VERIFYING','Archive integrity verifying','Every required archive set is tested at the destination before extraction.'],['MOVE_ARCHIVE_VERIFIED','Archive integrity verified','Every required archive set passed its integrity and manifest checks.'],['MOVE_EXTRACTING','Archive media extracting','Extraction, hashing, and publication progress are reported separately.'],['MOVE_EXTRACTED','Archive media verified','Archive-derived media has been extracted and hash-verified.'],['MOVE_ADDITIONAL_VERIFYING','Additional files verifying','Selected subtitles, metadata, and artwork are being verified.'],['MOVE_ADDITIONAL_VERIFIED','Additional files verified','Selected subtitles, metadata, and artwork are copied and hash-verified.'],['MOVE_LIBRARY_VERIFYING','Library files verifying','Media hashes and hardlink destinations are being verified.'],['MOVE_LIBRARY_LINKED','Library files created','Verified hardlinks or extracted media are created on the destination pool.'],['MOVE_LIBRARY_AUXILIARY','Library sidecars copied','Selected subtitles, metadata, and artwork are present in the new library.'],['MOVE_ARR_UPDATED','*Arr route updated','The movie or series root folder and pool tag now point at the destination.'],['MOVE_ARR_RESCANNING','*Arr library rescanning','Stowarr waits for the Radarr or Sonarr rescan command to finish.'],['MOVE_ARR_RESCANNED','*Arr rescan verified','Radarr or Sonarr confirms every managed file at its new path.'],['MOVE_OLD_LIBRARY_REMOVED','Source library finalized','A distinct stale source folder is removed after verification; the destination folder is always kept.'],['MOVE_DERIVATIVE_CLEANUP','Derived output cleanup','Recognized Unpackerr output is removed only after its media hash matches the published library file.'],['MOVE_ROUTE_COMMITTED','Final route committed','The destination category is assigned only after the library update succeeds.'],['MOVE_RESUMING','Torrent resuming','qBittorrent is instructed to continue from the verified destination.'],['MOVE_SEEDING','Seeding verified','qBittorrent confirms an active or queued upload state at 100% progress.'],['COMPLETE','Move complete','qBittorrent is seeding and the *Arr library agrees.']];
const RECONCILE_PROGRESS=[['PLANNED','Plan accepted','The reviewed repair plan has been registered.'],['RECONCILE_VERIFYING','Library files verifying','Source content is hash-verified before links or copies are created.'],['LINKED','Library files created','Verified media is present on the authoritative pool.'],['AUXILIARY_COPIED','Sidecars copied','Selected subtitles, artwork, and metadata have been verified.'],['ARR_UPDATED','*Arr route updated','The movie or series points at the authoritative pool.'],['ARR_RESCANNING','*Arr library rescanning','Stowarr waits for Radarr or Sonarr to finish.'],['ARR_RESCANNED','*Arr rescan verified','Managed files are confirmed at their expected paths.'],['SOURCE_UNLINKED','Stale source removed','Verified obsolete files are removed last.'],['COMPLETE','Reconcile complete','The library and qBittorrent agree.']];
const CATEGORY_PROGRESS=[['CATEGORY_APPLYING','Categories applying','Each freshly validated qBittorrent category is changed and recorded.'],['COMPLETE','Category repair complete','Every category in the confirmed batch was processed.']];
const ARCHIVE_PROGRESS_STATES=new Set(['MOVE_ARCHIVE_VERIFYING','MOVE_ARCHIVE_VERIFIED','MOVE_EXTRACTING','MOVE_EXTRACTED']);
const INDETERMINATE_PROGRESS_STATES=new Set(['MOVE_RELOCATING','MOVE_ARR_RESCANNING','ARR_RESCANNING','MOVE_RESUMING','MOVE_SEEDING']);
const operationKindLabel=kind=>kind==='reconcile'?'Reconcile':kind==='category'?'Category repair':'Move';
function resetOperationSections(){state.operationSections={completed:false,remaining:false}}
const terminalOperation=operation=>Boolean(operation&&['COMPLETE','FAILED','BLOCKED','DRY_RUN','RECOVERY_REQUIRED'].includes(operation.state));
const QUEUE_TERMINAL_STATES=new Set(['COMPLETE','FAILED','CANCELLED','INTERRUPTED']);
function resetOperationLog(){const panel=$('#operation-log-panel');panel.open=false}
function renderOperationMinimized(){
  const launcher=$('#operation-minimized');
  const operation=state.currentOperation;
  const visible=state.operationTracking&&state.operationHidden;
  launcher.classList.toggle('hidden',!visible);
  if(!visible)return;
  const terminal=terminalOperation(operation);
  const failed=['FAILED','BLOCKED','RECOVERY_REQUIRED'].includes(operation?.state);
  const live=operation?.detail?.progress||{};
  const label=operationKindLabel(operation?.kind);
  const title=terminal?(failed?`${label} needs attention`:`${label} complete`):`${label} in progress…`;
  const summary=operation?.detail?.torrent_name||operation?.torrent_hash||(live.message||'Waiting for the operation to start');
  $('#operation-minimized-title').textContent=title;
  $('#operation-minimized-summary').textContent=summary;
  $('#operation-minimized-dot').className=`dot ${failed?'failed':terminal?'ok':'warn'}`;
  launcher.classList.toggle('failed',failed);
  launcher.classList.toggle('complete',terminal&&!failed);
}
function operationLogRows(operation,events){
  const rows=[];
  const indexes=new Map();
  for(const event of events){
    const eventState=String(event.state||'UNKNOWN');
    if(eventState==='FAILED')continue;
    const detail=event.detail||{};
    const existing=indexes.get(eventState);
    if(existing===undefined){
      indexes.set(eventState,rows.length);
      rows.push({...event,detail:{...detail}});
    }else{
      const previous=rows[existing];
      rows[existing]={...previous,...event,detail:{...previous.detail,...detail}};
    }
  }
  if(operation?.state==='FAILED'){
    const failedEvent=[...events].reverse().find(event=>String(event.state)==='FAILED');
    const failedState=String(operation.detail?.failed_after||failedEvent?.detail?.failed_after||'');
    const failedIndex=indexes.has(failedState)?indexes.get(failedState):rows.length-1;
    if(failedIndex>=0){
      const row=rows[failedIndex];
      const failureDetail=failedEvent?.detail||{};
      rows[failedIndex]={...row,failed:true,detail:{...row.detail,error:operation.detail?.error||failureDetail.error||'Operation failed',recovery:operation.detail?.recovery||failureDetail.recovery||row.detail.recovery}};
    }else if(failedEvent){
      rows.push({...failedEvent,failed:true});
    }
  }
  return rows;
}
function renderOperationLog(operation){
  const panel=$('#operation-log-panel');
  const log=$('#operation-log');
  const events=state.operationEvents.get(operation?.id)||[];
  panel.classList.toggle('hidden',!operation);
  if(!operation)return;
  const rows=operationLogRows(operation,events);
  const previousTop=log.scrollTop;
  const followTail=log.scrollHeight-log.scrollTop-log.clientHeight<24;
  $('#operation-log-count').textContent=events.length?`${rows.length} stages · ${events.length} events`:'Loading…';
  log.innerHTML=rows.length?rows.map(event=>{
    const detail=event.detail||{};
    const percent=detail.percent!==undefined?`${Math.round(Number(detail.percent))}%`:'';
    const message=detail.error||detail.message||detail.current||'State recorded';
    const recovery=typeof detail.recovery==='string'?detail.recovery:detail.recovery?.reason||detail.recovery?.note||'';
    return `<li class="${event.failed||detail.error?'failed':''}"><time>${esc(fmtTime(event.created_at))}</time><strong>${esc(String(event.state).replaceAll('_',' '))}</strong>${percent?`<b>${esc(percent)}</b>`:''}<span>${esc(message)}</span>${recovery?`<small>${esc(recovery)}</small>`:''}</li>`
  }).join(''):'<li class="empty-log"><span>No detailed events were recorded for this operation.</span></li>';
  log.scrollTop=followTail?log.scrollHeight:previousTop;
}
async function loadOperationEvents(operationId){try{const result=await api(`/api/operations/${encodeURIComponent(operationId)}/events`);state.operationEvents.set(Number(operationId),result.events||[])}catch(e){state.operationEvents.set(Number(operationId),[{state:'LOG_UNAVAILABLE',created_at:Math.floor(Date.now()/1000),detail:{error:e.message}}])}}
async function openOperationDetails(operationId){
  await refreshOperations();
  const operation=state.operations.find(item=>String(item.id)===String(operationId));
  if(!operation)return;
  if(!terminalOperation(operation)){
    if(state.operationTracking&&String(state.currentOperation?.id)===String(operation.id)){
      showOperationTracking();
      return;
    }
    await trackOperationById(operation.id);
    return;
  }
  const trackingLiveOperation=state.operationTracking&&!terminalOperation(state.currentOperation);
  if(trackingLiveOperation){
    state.operationHidden=true;
    renderOperationMinimized();
  }else{
    if(state.operationTracking)finishOperationTracking();
    state.currentOperation=operation;
    renderOperationMinimized();
  }
  resetOperationSections();
  resetOperationLog();
  renderOperationDialog(operation,false,!trackingLiveOperation);
  const dialog=$('#operation-dialog');
  if(!dialog.open)dialog.showModal();
  await loadOperationEvents(operation.id);
  renderOperationDialog(operation,false,!trackingLiveOperation);
}
function operationStepMarkup(step,status,percent,detail='',indeterminate=false){const [,title,description]=step;return `<li class="${status}${indeterminate?' indeterminate':''}"><div class="operation-step-title"><strong>${esc(title)}</strong><b>${indeterminate?'Working':`${Math.round(percent)}%`}</b></div><div class="operation-progress"><i style="width:${indeterminate?100:Math.max(0,Math.min(100,percent))}%"></i></div><small>${esc(detail||description)}</small></li>`}
function operationGroupMarkup(kind,label,steps,open){if(!steps.length)return '';const body=kind==='remaining'?`<ol class="operation-upcoming-list">${steps.map(([,title,description])=>`<li><strong>${esc(title)}</strong><small>${esc(description)}</small></li>`).join('')}</ol>`:`<ol class="operation-group-list">${steps.map(step=>operationStepMarkup(step,'complete',100)).join('')}</ol>`;const hint=kind==='remaining'&&steps[0]?`Next: ${steps[0][1]}`:`${steps.length} step${steps.length===1?'':'s'}`;return `<details class="operation-group operation-group-${kind}" data-operation-section="${kind}"${open?' open':''}><summary><span>${esc(label)}</span><small>${esc(hint)}</small></summary>${body}</details>`}
function renderOperationDialog(operation,waiting=false,updateTrackedOperation=true){
  if(updateTrackedOperation)state.currentOperation=operation;
  const terminal=terminalOperation(operation);
  const stateName=operation?.state||'WAITING';
  const recoveryRequired=stateName==='RECOVERY_REQUIRED';
  const progressState=stateName==='FAILED'||recoveryRequired?(operation?.detail?.recovery?.previous_state||operation?.detail?.failed_after||(operation?.kind==='reconcile'?'PLANNED':operation?.kind==='category'?'CATEGORY_APPLYING':'MOVE_PLANNED')):stateName;
  const live=operation?.detail?.progress||{};
  const isReconcile=operation?.kind==='reconcile';
  const isCategory=operation?.kind==='category';
  const hasArchive=Boolean(operation?.detail?.extraction_required)||ARCHIVE_PROGRESS_STATES.has(progressState)||ARCHIVE_PROGRESS_STATES.has(live.state);
  const progressSteps=isReconcile?RECONCILE_PROGRESS:isCategory?CATEGORY_PROGRESS:MOVE_PROGRESS.filter(([name])=>hasArchive||!ARCHIVE_PROGRESS_STATES.has(name));
  const currentIndex=progressSteps.findIndex(([name])=>name===progressState);
  const complete=stateName==='COMPLETE';
  const completedSteps=complete?[...progressSteps]:currentIndex>0?progressSteps.slice(0,currentIndex):[];
  const activeStep=!complete&&currentIndex>=0?progressSteps[currentIndex]:null;
  const remainingSteps=currentIndex>=0&&!complete?progressSteps.slice(currentIndex+1):[];
  const currentLive=activeStep&&live.state===activeStep[0];
  const reportedPercent=Number(live.percent||0);
  const activePercent=Number.isFinite(reportedPercent)&&(stateName==='FAILED'||recoveryRequired||currentLive)?reportedPercent:0;
  const overallPercent=complete?100:Math.round(((completedSteps.length+(activePercent/100))/progressSteps.length)*100);
  const stepNumber=complete?progressSteps.length:currentIndex>=0?currentIndex+1:0;
  const byteProgress=live.total_bytes?`${fmtBytes(Number(live.completed_bytes||0))} of ${fmtBytes(Number(live.total_bytes))}`:'';
  const torrentSize=Number(operation?.detail?.torrent_size||0);
  const largeTorrent=torrentSize>=20*1024**3;
  const waitingForRecheck=activeStep?.[0]==='MOVE_RECHECKING'&&!String(live.qbit_state||'').toLowerCase().includes('checking');
  const indeterminate=Boolean(activeStep&&stateName!=='FAILED'&&activePercent<100&&(INDETERMINATE_PROGRESS_STATES.has(activeStep[0])||waitingForRecheck));
  const sizeNotice=largeTorrent&&['MOVE_RELOCATING','MOVE_RECHECKING'].includes(activeStep?.[0])?`${fmtBytes(torrentSize)} torrent; relocation and verification may take a while`:'';
  const activeDetail=currentLive?[live.message,sizeNotice,live.current,byteProgress,live.qbit_state].filter(Boolean).join(' · '):sizeNotice;
  const failureContext=(stateName==='FAILED'||recoveryRequired)&&completedSteps.length?`<div class="operation-failure-context"><small>Previous completed: ${completedSteps.slice(-2).map(([,title])=>esc(title)).join(' · ')}</small></div>`:'';
  const kindLabel=operationKindLabel(operation?.kind);
  $('#operation-title').textContent=terminal?`${kindLabel} details`:`${kindLabel} in progress`;
  $('#operation-summary').textContent=waiting?(operation?.public_id?`${operation.public_id} · ${operation.detail?.torrent_name||operation.torrent_hash||'Queued job'} · waiting to start`:'Waiting for the API to register the operation'):operation?`${operation.public_id?`${operation.public_id} · `:''}${operation.detail?.torrent_name||operation.torrent_hash} · ${stateName.replaceAll('_',' ')}`:'Operation details unavailable';
  const activeMarkup=activeStep?`<ol class="operation-active-list">${operationStepMarkup(activeStep,stateName==='FAILED'||recoveryRequired?'failed':'active',activePercent,activeDetail,indeterminate)}</ol>${failureContext}`:'';
  const categoryResults=isCategory&&operation?.detail?.results?.length?`<details class="category-operation-results"><summary>Category results <span>${operation.detail.results.length} ${operation.detail.results.length===1?'torrent':'torrents'}</span></summary><ol>${operation.detail.results.map(item=>`<li><strong>${esc(item.torrent_name||item.hash)}</strong><small>${esc(item.previous_category||'none')} → ${esc(item.category)} · ${esc(item.pool||'unknown pool')}</small></li>`).join('')}</ol></details>`:'';
  $('#operation-steps').innerHTML=`<div class="operation-overall"><div class="operation-step-title"><strong>${complete?'All steps complete':stepNumber?`Step ${stepNumber} of ${progressSteps.length}`:'Preparing operation'}</strong><b>${overallPercent}% overall</b></div><div class="operation-progress"><i style="width:${overallPercent}%"></i></div></div>${operationGroupMarkup('completed','Completed',completedSteps,state.operationSections.completed)}${activeMarkup}${operationGroupMarkup('remaining','Remaining',remainingSteps,state.operationSections.remaining)}${categoryResults}`;
  $$('.operation-group',$('#operation-steps')).forEach(group=>group.addEventListener('toggle',()=>{state.operationSections[group.dataset.operationSection]=group.open}));
  const recoveryReason=typeof operation?.detail?.recovery==='string'?operation.detail.recovery:operation?.detail?.recovery?.reason||'';
  const error=operation?.detail?.error||(recoveryRequired?'Operation interrupted; all writes are paused until Recovery is reviewed.':'');
  $('#operation-error').textContent=error?`${error}${recoveryReason?` ${recoveryReason}`:''}`:'';
  $('#operation-error').classList.toggle('hidden',!error);
  const canHide=state.operationTracking&&!terminal;
  $('#operation-close').disabled=!terminal&&!canHide;
  $('#operation-close').setAttribute('aria-label',canHide?'Hide operation details':'Close operation details');
  $('#operation-done').disabled=!terminal&&!canHide;
  $('#operation-done').textContent=canHide?'Hide':stateName==='FAILED'?'Close failure details':recoveryRequired?'Close recovery details':'Close';
  renderOperationLog(waiting?null:operation);
  renderOperationMinimized();
}
async function refreshOperations(){if(!state.authenticated)return;try{state.operations=await api('/api/operations');renderOperations()}catch{/* Keep the last successfully loaded operation list during transient refresh failures. */}}
async function startOperationTracking(findOperation,kind='move',queueContext=null,startHidden=false){
  const generation=++state.operationTrackingGeneration;
  const dialog=$('#operation-dialog');
  state.operationTracking=true;
  state.operationHidden=startHidden;
  state.currentOperation=null;
  resetOperationSections();
  resetOperationLog();
  const renderTrackedOperation=(operation,waiting=false)=>{
    if(state.operationHidden){
      state.currentOperation=operation;
      renderOperationMinimized();
    }else{
      renderOperationDialog(operation,waiting);
    }
  };
  const queueRows=()=>[
    ...(state.queue||[]).map(item=>({...item,kind:'move'})),
    ...(state.reconcileQueue||[]).map(item=>({...item,kind:'reconcile'})),
  ].sort((left,right)=>{
    const stateOrder={RUNNING:0,QUEUED:1};
    return (stateOrder[left.state]??2)-(stateOrder[right.state]??2)||(left.position||Number.MAX_SAFE_INTEGER)-(right.position||Number.MAX_SAFE_INTEGER);
  });
  const initialQueueJob=queueContext?queueRows().find(item=>item.public_id===queueContext.publicId&&item.kind===queueContext.kind):null;
  renderTrackedOperation(initialQueueJob?{kind:initialQueueJob.kind,public_id:initialQueueJob.public_id,torrent_hash:initialQueueJob.torrent_hash,state:'WAITING',detail:initialQueueJob.detail||{}}:{kind,state:'WAITING',detail:{}},true);
  if(!startHidden&&!dialog.open)dialog.showModal();
  clearInterval(state.operationTimer);
  state.operationTimer=null;
  let updating=false;
  let keepTracking=true;
  const showQueueWaiting=job=>{
    resetOperationSections();
    resetOperationLog();
    renderTrackedOperation({kind:job.kind,public_id:job.public_id,torrent_hash:job.torrent_hash,state:'WAITING',detail:job.detail||{}},true);
  };
  const showQueueTerminal=job=>{
    const complete=job.state==='COMPLETE';
    renderTrackedOperation({
      kind:job.kind,
      public_id:job.public_id,
      torrent_hash:job.torrent_hash,
      state:complete?'COMPLETE':'FAILED',
      detail:{
        ...(job.detail||{}),
        error:complete?'':job.error||`Queued ${job.kind} ended in state ${job.state}`,
        recovery:complete?'':'Review the Queue error, correct the plan or files, and submit the job again.',
      },
    });
  };
  const advanceQueue=async()=>{
    await refreshQueue(true);
    if(generation!==state.operationTrackingGeneration)return false;
    const rows=queueRows()||[];
    const current=rows.find(item=>item.public_id===queueContext.publicId&&item.kind===queueContext.kind);
    if(current&&['RUNNING','QUEUED'].includes(current.state))return true;
    const next=rows.find(item=>(item.public_id!==queueContext.publicId||item.kind!==queueContext.kind)&&['RUNNING','QUEUED'].includes(item.state));
    if(!next)return false;
    queueContext.publicId=next.public_id;
    queueContext.kind=next.kind;
    showQueueWaiting(next);
    return true;
  };
  const update=async()=>{
    if(generation!==state.operationTrackingGeneration||updating)return state.currentOperation;
    updating=true;
    try{
      await refreshOperations();
      if(generation!==state.operationTrackingGeneration)return state.currentOperation;
      const operation=findOperation(state.operations);
      if(operation){
        await loadOperationEvents(operation.id);
        if(generation!==state.operationTrackingGeneration)return state.currentOperation;
        renderTrackedOperation(operation);
        if(terminalOperation(operation))keepTracking=queueContext?await advanceQueue():false;
      }else if(queueContext){
        keepTracking=await advanceQueue();
        if(!keepTracking&&generation===state.operationTrackingGeneration){
          const finished=queueRows().find(item=>item.public_id===queueContext.publicId&&item.kind===queueContext.kind);
          if(finished&&['COMPLETE','FAILED','CANCELLED','INTERRUPTED'].includes(finished.state)){
            showQueueTerminal(finished);
          }else{
            finishOperationTracking();
          }
        }
      }
      if(!keepTracking&&generation===state.operationTrackingGeneration){
        clearInterval(state.operationTimer);
        state.operationTimer=null;
      }
      return operation;
    }finally{
      updating=false;
    }
  };
  await update();
  if(keepTracking&&generation===state.operationTrackingGeneration)state.operationTimer=setInterval(update,1000);
}
async function trackOperationById(operationId){const kind=state.operations.find(item=>String(item.id)===String(operationId))?.kind||'move';return startOperationTracking(operations=>operations.find(item=>String(item.id)===String(operationId)),kind)}
async function trackOperationByPublicId(publicId,kind,startHidden=false){
  const queueContext={publicId,kind:kind||'move'};
  return startOperationTracking(operations=>operations.find(item=>item.public_id===queueContext.publicId&&item.kind===queueContext.kind),queueContext.kind,queueContext,startHidden);
}
function watchOperationRegistration(torrentHash,kind,afterId=0,startHidden=false){
  let active=true;
  let timer=null;
  const watcher={
    registeredOperationId:null,
    stop(){
      active=false;
      clearTimeout(timer);
    },
  };
  const update=async()=>{
    if(!active)return;
    await refreshOperations();
    if(!active)return;
    const operation=state.operations.find(item=>item.id>afterId&&item.torrent_hash.toLowerCase()===torrentHash.toLowerCase()&&item.kind===kind);
    if(operation){
      active=false;
      watcher.registeredOperationId=operation.id;
      startOperationTracking(operations=>operations.find(item=>item.id===operation.id),kind,null,startHidden);
      return;
    }
    timer=setTimeout(update,250);
  };
  update();
  return watcher;
}
async function ensureDirectOperationTracking(watcher,result,kind){
  if(result?.disposition!=='direct'||!result.operation_id||watcher.registeredOperationId)return;
  watcher.stop();
  await refreshOperations();
  await startOperationTracking(
    operations=>operations.find(item=>String(item.id)===String(result.operation_id)),
    kind,
  );
}
function hideOperationTracking(){if(!state.operationTracking||terminalOperation(state.currentOperation))return finishOperationTracking();state.operationHidden=true;const dialog=$('#operation-dialog');if(dialog.open)dialog.close();renderOperationMinimized()}
function showOperationTracking(){if(!state.operationTracking)return;state.operationHidden=false;renderOperationDialog(state.currentOperation,state.currentOperation?.state==='WAITING');const dialog=$('#operation-dialog');if(!dialog.open)dialog.showModal()}
function finishOperationTracking(){state.operationTrackingGeneration++;clearInterval(state.operationTimer);state.operationTimer=null;state.operationTracking=false;state.operationHidden=false;state.currentOperation=null;renderOperationMinimized();const dialog=$('#operation-dialog');if(dialog.open)dialog.close()}
function rejectOperationTracking(torrentHash,kind,message,afterId=0){
  const registered=state.operations.find(item=>item.id>afterId&&item.torrent_hash.toLowerCase()===torrentHash.toLowerCase()&&item.kind===kind);
  if(registered){
    startOperationTracking(operations=>operations.find(item=>item.id===registered.id),kind);
    return;
  }
  const trackingLiveOperation=state.operationTracking&&!terminalOperation(state.currentOperation);
  if(trackingLiveOperation){
    state.operationHidden=true;
    renderOperationMinimized();
  }else{
    state.operationTrackingGeneration++;
    clearInterval(state.operationTimer);
    state.operationTimer=null;
    state.operationTracking=false;
    state.operationHidden=false;
  }
  state.operationEvents=[];
  const failedAfter=kind==='move'?'MOVE_PLANNED':kind==='category'?'CATEGORY_APPLYING':'PLANNED';
  const planName=(kind==='move'?state.movePlan:kind==='reconcile'?state.plan:null)?.torrent_name;
  renderOperationDialog({id:0,kind,torrent_hash:torrentHash,state:'FAILED',detail:{torrent_name:planName||torrentHash,error:`Operation was rejected before it started: ${message}. No files were changed by this request.`,failed_after:failedAfter,progress:{state:failedAfter,percent:0,message:'The API did not register the operation'}}},false,!trackingLiveOperation);
  const dialog=$('#operation-dialog');
  if(!dialog.open)dialog.showModal();
}
function navigate(page){$$('.page').forEach(x=>x.classList.toggle('active',x.id===page));$$('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===page));$('.sidebar').classList.remove('open');location.hash=page;if(page==='move')loadQbitCatalog();if(page==='queue')refreshQueue()}
function renderConfig(){const c=state.config;$('#mode-pill').textContent=c.apply?'Write mode':'Dry run';$('#mode-pill').className=`pill ${c.apply?'live':'dry'}`;$('#side-mode').textContent=c.apply?'Write mode':'Dry run';$('#side-dot').className=`dot ${c.apply?'ok':'warn'}`;renderBuildVersion(c);$('#stat-pools').textContent=c.pools.length;$('#routing-head').innerHTML=`<tr><th>Service</th>${c.pools.map(p=>`<th colspan="2" class="pool-heading">${esc(p.name)}</th>`).join('')}</tr><tr class="subhead"><th></th>${c.pools.map(()=>'<th>Category</th><th>Root folders</th>').join('')}</tr>`;const roots=(p,app)=>app==='sonarr'?(p.sonarr_roots||[p.sonarr_root]):[p.radarr_root];const services=[['Radarr','radarr_category','radarr'],['Sonarr','sonarr_category','sonarr']];$('#pool-rows').innerHTML=services.map(([name,categoryKey,app])=>`<tr><td class="service-name">${esc(name)}</td>${c.pools.map(p=>`<td><span class="category">${esc(p[categoryKey])}</span></td><td class="path">${roots(p,app).map(esc).join('<br>')}</td>`).join('')}</tr>`).join('');$('#settings-pools').innerHTML=c.pools.map(p=>`<article class="settings-card"><h2>${esc(p.name)}</h2><dl class="settings-grid"><dt>Pool prefix</dt><dd>${esc(p.prefix)}</dd><dt>Download roots</dt><dd>${p.download_roots.map(esc).join('<br>')}</dd><dt>Radarr root / tag</dt><dd>${esc(p.radarr_root)} · ${esc(p.radarr_tag)}</dd><dt>Sonarr roots / tag</dt><dd>${roots(p,'sonarr').map(esc).join('<br>')}<br>${esc(p.sonarr_tag)}</dd><dt>Categories</dt><dd>${esc(p.radarr_category)} · ${esc(p.sonarr_category)}</dd></dl></article>`).join('');$('#move-target').innerHTML=c.pools.map(p=>`<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('')}
function renderServiceStatus(){const status=state.serviceStatus;if(!status)return;const ids={stowarr_api:'api',qbittorrent:'qbit',radarr:'radarr',sonarr:'sonarr'};Object.entries(ids).forEach(([name,id])=>{const service=status.services?.[name]||{};const node=$(`#side-${id}-dot`);const mode=service.status==='connected'?'ok':service.status==='unavailable'?'failed':'warn';node.className=`dot ${mode}`;node.title=service.status==='connected'?`${name==='stowarr_api'?'Stowarr API':name} connected${service.version?` · ${service.version}`:''}${service.commit&&service.commit!=='unknown'?` · ${service.commit.slice(0,12)}`:''}`:service.error||'Not configured'});renderBuildVersion(status)}
function renderConnections(){const services=state.connections?.services;if(!services)return;const form=$('#connections-form');form.elements['qbittorrent-url'].value=services.qbittorrent.url||'';form.elements['qbittorrent-api-key'].placeholder=services.qbittorrent.api_key_set?'Saved API key · preferred authentication':'API key recommended for qBittorrent 5.2+';form.elements['qbittorrent-username'].value=services.qbittorrent.username||'';form.elements['qbittorrent-password'].placeholder=services.qbittorrent.password_set?'Saved password · login fallback':'Password for login fallback';form.elements['radarr-url'].value=services.radarr.url||'';form.elements['radarr-api-key'].placeholder=services.radarr.api_key_set?'Saved API key · leave blank to keep':'API key required when Radarr is configured';form.elements['sonarr-url'].value=services.sonarr.url||'';form.elements['sonarr-api-key'].placeholder=services.sonarr.api_key_set?'Saved API key · leave blank to keep':'API key required when Sonarr is configured';const configured=state.connections.configured||{};const count=Object.values(configured).filter(Boolean).length;const complete=state.connections.status==='ready';$('#connections-status').textContent=complete?'All connected':count?`${count} of 3 connected`:'Not configured';$('#connections-status').className=`badge ${complete?'complete':count?'partial':'blocked'}`;$('#connection-error').textContent=state.connections.error||'';$('#connection-error').classList.toggle('hidden',!state.connections.error);$('#setup-error').textContent=state.connections.error||'';$('#setup-error').classList.toggle('hidden',!state.connections.error);const states={qbit:Boolean(configured.qbittorrent),radarr:Boolean(configured.radarr),sonarr:Boolean(configured.sonarr)};Object.entries(states).forEach(([name,connected])=>{const node=$(`#${name}-connection-state`);node.textContent=connected?'Connected':'Optional';node.className=`badge ${connected?'complete':'partial'}`;const dot=$(`#${name}-summary-dot`);dot.className=`dot ${connected?'ok':'warn'}`});const methods={qbit:services.qbittorrent.api_key_set?'API key':services.qbittorrent.password_set?'Username/password fallback':'No credentials',radarr:services.radarr.api_key_set?'API key':'No API key',sonarr:services.sonarr.api_key_set?'API key':'No API key'};Object.entries(states).forEach(([name,connected])=>{$(`#${name}-auth-summary`).textContent=connected?`Connected · ${methods[name]}`:`Not configured · ${methods[name]}`})}
function renderRuntime(){if(!state.runtime)return;$('#runtime-apply').checked=state.runtime.apply;$('#runtime-status').textContent=state.runtime.apply?'Write mode':'Dry run';$('#runtime-status').className=`badge ${state.runtime.apply?'complete':'dry_run'}`;const deployment=state.runtime.deployment;$('#deployment-settings').innerHTML=`<div class="deployment-note"><strong>Docker deployment settings</strong><span>These values define the container boundary and require a Compose recreate to change safely.</span></div>${deployment.running_as_root?'<div class="alert inline-alert">The API process is running as root. Configure PUID and PGID or a non-root container user before enabling writes.</div>':''}<dl><dt>Configured identity</dt><dd>${esc(deployment.configured_puid)}:${esc(deployment.configured_pgid)}</dd><dt>Effective identity</dt><dd>${esc(deployment.process_uid)}:${esc(deployment.process_gid)}</dd><dt>File creation umask</dt><dd>${esc(deployment.umask)}</dd><dt>Media mount mode</dt><dd>${esc(deployment.media_mount_mode)}</dd><dt>API token</dt><dd>${deployment.api_token_set?'Configured':'Not configured'}</dd><dt>API listener</dt><dd>${esc(deployment.listen)}:${esc(deployment.port)}</dd><dt>API-only service</dt><dd>${deployment.api_only?'Enabled':'Disabled'}</dd><dt>Timezone</dt><dd>${esc(deployment.timezone)}</dd>${deployment.pool_mounts.map(pool=>`<dt>${esc(pool.name)} mount</dt><dd>${esc(pool.prefix)} · ${pool.writable?'writable':'read-only'}</dd>`).join('')}</dl>`}
function renderSecurity(){const method=state.auth?.method||state.runtime?.deployment?.auth_method||'forms';$('#password-panel').classList.toggle('hidden',method!=='forms');$('#security-summary').innerHTML=`<div><small>Authentication method</small><strong>${esc(method==='external'?'External proxy':'Forms')}</strong></div><div><small>Active sessions</small><strong>${state.sessions.length}</strong></div><div><small>API authentication</small><strong>Bearer or X-Api-Key</strong></div>`;$('#revoke-sessions').disabled=method!=='forms'||!state.sessions.length;$('#security-events').innerHTML=state.securityEvents.length?state.securityEvents.map(item=>`<tr><td>${badge(item.event)}</td><td>${esc(item.username||'—')}</td><td>${esc(item.client||'—')}</td><td>${fmtTime(item.created_at)}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No security events recorded</td></tr>'}
function renderOperations(){const ops=state.operations;const terminal=op=>['COMPLETE','FAILED','BLOCKED','DRY_RUN'].includes(op.state);const available=new Set(ops.filter(terminal).map(op=>op.id));state.selectedHistory=new Set([...state.selectedHistory].filter(id=>available.has(id)));$('#history-count').textContent=ops.length||'';$('#stat-operations').textContent=ops.length;$('#stat-complete').textContent=ops.filter(x=>x.state==='COMPLETE').length;$('#stat-blocked').textContent=ops.filter(x=>['BLOCKED','FAILED','RECOVERY_REQUIRED'].includes(x.state)).length;$('#recent-list').innerHTML=ops.slice(0,5).map(op=>`<div class="activity">${badge(op.state)}<div><strong>${esc(op.detail?.torrent_name||op.torrent_hash)}</strong><small>${esc(op.public_id)} · ${esc(op.kind||'reconcile')} · ${esc(op.app||'unknown')} · ${fmtTime(op.updated_at)}</small></div><button class="link-button inspect-operation" data-operation-id="${op.id}">Details</button></div>`).join('');$('#operation-rows').innerHTML=ops.length?ops.map(op=>`<tr><td><input class="history-select" type="checkbox" data-operation-id="${op.id}" aria-label="Select job ${esc(op.public_id)}" ${state.selectedHistory.has(op.id)?'checked':''} ${terminal(op)?'':'disabled'}></td><td><code>${esc(op.public_id)}</code></td><td>${badge(op.kind||'reconcile')}</td><td>${esc(op.app||'—')}</td><td>${esc(op.detail?.torrent_name||op.torrent_hash)}</td><td>${badge(op.state)}</td><td>${fmtTime(op.updated_at)}</td><td><button class="link-button inspect-operation" data-operation-id="${op.id}">Details</button></td></tr>`).join(''):`<tr><td colspan="8" class="empty">No operations recorded</td></tr>`;const selected=state.selectedHistory.size;const selectable=available.size;$('#history-selection').textContent=`${selected} selected`;$('#delete-history-selected').disabled=!selected;const selectAll=$('#select-all-history');selectAll.disabled=!selectable;selectAll.checked=Boolean(selectable)&&selected===selectable;selectAll.indeterminate=selected>0&&selected<selectable;$('#clear-history').disabled=!selectable}
async function deleteHistory(all=false){const ids=all?[]:[...state.selectedHistory];const count=ids.length;if(!all&&!count)return;const confirmed=await confirmAction({title:all?'Clear History?':`Delete ${count} selected History ${count===1?'entry':'entries'}?`,message:'The operation records and their saved execution logs will be permanently removed. Active operations are always kept.',details:[[all?'Scope':'Selected entries',all?'All terminal History entries':String(count)]],confirmLabel:all?'Clear history':'Delete selected',danger:true});if(!confirmed)return;try{const result=await api('/api/operations',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify(all?{all:true}:{operationIds:ids})});state.selectedHistory.clear();state.operationEvents.clear();await refreshOperations();toast(`${result.deleted} History ${result.deleted===1?'entry':'entries'} deleted`)}catch(e){toast(`History was not changed: ${e.message}`)}}
function renderSonarrScopeBlocker(plan,context='reconcile'){
  const details=plan.error_details||{};
  const others=details.other_managed_files||[];
  const current=details.current_pool||'the current series pool';
  const target=details.target_pool||plan.target_pool||'the torrent pool';
  const selected=details.selected_file_count??0;
  const otherCount=details.other_managed_file_count??0;
  const total=details.scope_verified?selected+otherCount:null;
  const canRecommendMove=context==='reconcile'&&details.recommended_move_pool;
  const moveButton=canRecommendMove?`<button type="button" class="primary" id="open-recommended-sonarr-move" data-hash="${esc(plan.torrent_hash)}" data-pool="${esc(details.recommended_move_pool)}">Open Move to ${esc(details.recommended_move_pool)}</button>`:'';
  const inventory=others.length?`<details><summary>${details.other_managed_file_count} other Sonarr-managed episode file${details.other_managed_file_count===1?'':'s'} would be affected</summary><div class="table-wrap"><table><thead><tr><th>Existing series file</th><th>Episode IDs</th></tr></thead><tbody>${others.map(item=>`<tr><td class="path">${esc(item.relativePath||item.path||'Unknown')}</td><td>${(item.episodeIds||[]).map(id=>`#${esc(id)}`).join(', ')||'—'}</td></tr>`).join('')}</tbody></table></div></details>`:'';
  const moveContext=context==='move';
  const title=moveContext?'This release cannot move the whole Sonarr series':'A partial Sonarr release cannot relocate the whole series';
  const guidanceTitle=moveContext?`Keep this release on ${current}`:'Keep the release with its series';
  const guidance=moveContext
    ?`This torrent contains only part of the series. Moving it to ${target} would require changing the root for all Sonarr-managed episodes, so Stowarr stopped before changing anything.`
    :`Move this qBittorrent release to ${current}, then Reconcile can repair only the episodes owned by this release. Missing destination folders are created after the resulting plan is proven safe.`;
  return `<article class="panel release-conflict sonarr-scope-blocker"><div class="panel-head"><div><h2>${esc(title)}</h2><p>Blocked before qBittorrent, Sonarr, or the filesystem changed.</p></div>${badge('blocked')}</div><div class="sonarr-scope-summary"><div class="scope-card"><small>Release coverage</small><strong>${total===null?`${selected} proven files`:`${selected} of ${total} managed files`}</strong><span>${details.selected_episode_count??0} mapped episode${details.selected_episode_count===1?'':'s'}${otherCount?` · ${otherCount} other files stay with the series`:''}</span></div><div class="scope-card"><small>${moveContext?'Requested route':'Safe route'}</small><div class="scope-route"><span><b>qBittorrent</b><strong>${esc(target)}</strong></span><i>→</i><span><b>Sonarr series</b><strong>${esc(current)}</strong></span></div><code>${esc(plan.current_item_path||plan.current_item||'Unknown')}</code></div></div>${inventory}<div class="recovery-guidance"><div><strong>${esc(guidanceTitle)}</strong><p>${esc(guidance)}</p><small>Moving the whole series needs one coordinated plan covering every managed episode and associated release.</small></div>${moveButton}</div></article>`;
}
function renderReconcileBlocker(plan){
  if(plan.status!=='blocked')return '';
  const details=plan.error_details||{};
  if(plan.error_code==='SONARR_PARTIAL_SERIES_POOL_CHANGE')return renderSonarrScopeBlocker(plan);
  const issues=details.issues||[];
  const related=details.related_torrents||[];
  const candidates=details.candidates||[];
  const affected=details.affected_files||[];
  const issueMarkup=issues.length?`<ol class="reconcile-prerequisites">${issues.map(issue=>{const repeated=issue.summary===plan.reason&&issue.code===plan.error_code;return `<li><div>${repeated?'':`<strong>${esc(issue.summary)}</strong>`}<code>${esc(issue.code)}</code></div><p><b>Manual fix:</b> ${esc(issue.action)}</p></li>`}).join('')}</ol>`:`<div class="alert inline-alert"><p>${esc(plan.reason)}</p></div>`;
  const relatedMarkup=related.length?`<div class="table-wrap"><table><thead><tr><th>Competing qBittorrent release</th><th>Hash</th><th>Category</th><th>Save path</th></tr></thead><tbody>${related.map(item=>`<tr><td>${esc(item.name)}</td><td><span class="hash-short" title="${esc(item.hash)}">${esc(String(item.hash||'').slice(0,12))}…</span></td><td><span class="category">${esc(item.category||'none')}</span></td><td class="path">${esc(item.save_path||'—')}</td></tr>`).join('')}</tbody></table></div>`:'';
  const candidateMarkup=candidates.length?`<div class="release-evidence">${candidates.map(item=>`<div><small>Possible ${plan.app==='sonarr'?'Sonarr series':'Radarr movie'} — not automatically trusted</small><strong>${esc(item.title||'Unknown')} ${item.id?`(#${esc(item.id)})`:''}</strong><code>${esc(item.path||'No library path')}</code></div>`).join('')}</div>`:'';
  const affectedMarkup=affected.length?`<div class="table-wrap"><table><thead><tr><th>Blocked media file</th><th>Torrent file</th><th>Reason</th></tr></thead><tbody>${affected.map(item=>`<tr><td class="path">${esc(item.source_library||'—')}</td><td class="path">${esc(item.torrent_file||'No unique file')}</td><td>${badge(item.status)}</td></tr>`).join('')}</tbody></table></div>`:'';
  const eligibleVideos=details.eligible_feature_videos||[];
  const rejectedVideos=details.rejected_non_feature_videos||[];
  const videoEvidenceMarkup=Number.isInteger(details.torrent_video_count)?`<div class="release-evidence"><div><small>Selected direct videos</small><strong>${details.torrent_video_count}</strong><span>${details.contains_archives?'Archive content is also selected':'No archive content detected'}</span></div><div><small>Eligible feature candidates</small><strong>${details.eligible_feature_video_count??'—'}</strong>${eligibleVideos.map(path=>`<code>${esc(path)}</code>`).join('')||'<span>No unambiguous feature candidate</span>'}</div>${rejectedVideos.length?`<div><small>Ignored samples, trailers, extras, or unproven videos</small><strong>${rejectedVideos.length}</strong>${rejectedVideos.map(path=>`<code>${esc(path)}</code>`).join('')}</div>`:''}</div>`:'';
  return `<article class="panel release-conflict"><div class="panel-head"><div><h2>Reconcile needs manual resolution</h2><p>Stowarr made no changes and will not enable Reconcile until the identity and storage route are safe.</p></div>${badge('blocked')}</div><div class="reconcile-block-summary"><strong>${esc(plan.reason)}</strong>${plan.error_code?`<code>${esc(plan.error_code)}</code>`:''}</div>${videoEvidenceMarkup}${issueMarkup}${candidateMarkup}${relatedMarkup}${affectedMarkup}${details.action?`<div class="recovery-guidance"><div><strong>Before trying again</strong><p>${esc(details.action)}</p></div></div>`:''}</article>`;
}
function reconcileActionCopy(plan){
  const isSonarr=plan.app==='sonarr';
  const managedName=isSonarr?'episode files':'movie file';
  const secondaryName=isSonarr?'series files':'sidecars';
  const restoresMissing=plan.pairs?.some(pair=>pair.status==='missing-library');
  const mediaAlreadyValid=Boolean(plan.pairs?.length)&&plan.pairs.every(pair=>['linked','already-on-target','verified-derived'].includes(pair.status));
  const samePath=Boolean(plan.pairs?.length)&&plan.pairs.every(pair=>pair.source_library===pair.target_library);
  if(restoresMissing)return{
    title:`Ready to restore missing ${isSonarr?'Sonarr episodes':'Radarr media'}`,
    message:`Stowarr will force-recheck qBittorrent, create the missing ${managedName} and selected secondary files, rescan ${isSonarr?'Sonarr':'Radarr'}, and require *Arr to manage the exact paths. Missing parent folders are created automatically. No verified duplicate is removed.`,
    button:'Restore missing media hardlink',
    confirmTitle:`Restore ${plan.item_title} from qBittorrent?`,
    confirmMessage:'The qBittorrent video is retained as the source of truth. Stowarr creates library hardlinks only after a successful force recheck.',
    detailLabel:'Missing library media',
  };
  if(mediaAlreadyValid)return{
    title:`Ready to repair selected ${secondaryName} only`,
    message:`The managed ${managedName} ${isSonarr?'are':'is'} already valid and remain${isSonarr?'':'s'} untouched. Stowarr processes only the selected subtitles, NFO, artwork, or other secondary files.`,
    button:`Repair selected ${secondaryName}`,
    confirmTitle:`Repair selected ${secondaryName} for ${plan.item_title}?`,
    confirmMessage:`No managed ${managedName} ${isSonarr?'are':'is'} hashed, replaced, moved, or deleted by this plan.`,
    detailLabel:`Managed ${managedName} changes`,
    mediaUntouched:true,
  };
  if(samePath)return{
    title:'Ready to repair missing hardlink identity',
    message:'Stowarr will hash-verify the existing Radarr media against qBittorrent and replace the same library entry with a hardlink only when the content is identical.',
    button:'Replace with verified hardlink',
    confirmTitle:`Repair the ${plan.item_title} hardlink?`,
    confirmMessage:'The existing library entry is replaced only after exact content verification.',
    detailLabel:'Library entry repaired',
  };
  return{
    title:'Ready for verified reconciliation',
    message:'Stowarr will hash-verify, create the new hardlink, update *Arr, and only then remove the verified old duplicate.',
    button:'Reconcile & remove verified duplicate',
    confirmTitle:`Reconcile ${plan.item_title}?`,
    confirmMessage:'Suspected duplicates are removed only after content verification.',
    detailLabel:'Suspected duplicates',
  };
}
function renderReconcilePair(pair,app){
  const isSonarr=app==='sonarr';
  const mediaName=isSonarr?'episode':'movie';
  const packed=pair.strategy==='archive-reextract'||pair.strategy==='verified-copy';
  const already=pair.status==='already-on-target';
  const mediaAlreadyValid=['linked','already-on-target','verified-derived'].includes(pair.status);
  const restore=pair.status==='missing-library';
  const sourceTitle=packed
    ?'qBittorrent archive set'
    :`qBittorrent download on ${esc(poolForPath(pair.torrent_file))}`;
  const sourceText=packed
    ?'The archive manifest and save path are authoritative. Extracted files are disposable derived artifacts.'
    :'This save path determines the authoritative pool. The download is kept.';
  const sourcePath=packed
    ?'Archive content must pass a qBittorrent recheck and extractor test'
    :esc(pair.torrent_file);
  const targetStep=packed?(already?'KEEP':'RE-EXTRACT'):'CREATE';
  const targetTitle=packed
    ?(already
      ?'Imported media already on the authoritative pool'
      :`Regenerate media with Stowarr on ${esc(poolForPath(pair.target_library))}`)
    :`New library hardlink on ${esc(poolForPath(pair.target_library))}`;
  const targetText=restore
    ?`Created from the selected qBittorrent ${mediaName} only after a successful force recheck. Missing parent folders are created automatically.`
    :packed
      ?(already
        ?'No media move is required.'
        :'Stowarr extracts the qBittorrent-owned archives into isolated staging and verifies the result before import.')
      :'Created as a hardlink to the qBittorrent file after verification.';
  const previous=restore?'':`<div class="flow-arrow">→</div><div class="flow-card obsolete"><span class="flow-step">3 · ${already?'CURRENT LIBRARY':'SUSPECTED STALE DERIVATIVE'}</span><h3>${already?'Current imported media':`Old derived media on ${esc(poolForPath(pair.source_library))}`}</h3><p>${already?'This file is already on the pool selected by qBittorrent.':'It is removed only after Stowarr has extracted, verified, and imported the media on the authoritative pool.'}</p><code class="path">${esc(pair.source_library)}</code></div>`;
  const mediaState=restore
    ?`The managed ${mediaName} is missing from the library.`
    :mediaAlreadyValid
      ?`The managed ${mediaName} is already valid and requires no repair.`
      :pair.source_library===pair.target_library&&!packed
      ?`The managed ${mediaName} already exists, but it is a separate copy rather than a hardlink to qBittorrent.`
      :`The managed ${mediaName} exists at an old or non-authoritative library path.`;
  const mediaAction=restore
    ?'After qBittorrent recheck, create the missing library hardlink.'
    :mediaAlreadyValid
      ?'Skip media hashing and leave this file unchanged.'
      :pair.source_library===pair.target_library&&!packed
      ?'Hash both files; only if identical, replace this library copy with a hardlink at the same path.'
      :`Verify and publish the managed ${mediaName} on the authoritative pool before removing the old derivative. Missing parent folders are created automatically.`;
  return `<section class="primary-media"><div class="primary-media-head"><div><span class="section-kicker">${isSonarr?'Managed episode file':'Primary movie file'}</span><strong>${esc(mediaState)}</strong><small>${esc(mediaAction)}</small></div><div class="pair-summary">${badge(pair.status)}${badge(pair.strategy)}<strong>${(pair.size/1073741824).toFixed(2)} GiB</strong></div></div><div class="pair"><div class="file-flow"><div class="flow-card canonical"><span class="flow-step">1 · SOURCE OF TRUTH / KEEP</span><h3>${sourceTitle}</h3><p>${sourceText}</p><code class="path">${sourcePath}</code></div><div class="flow-arrow">→</div><div class="flow-card target"><span class="flow-step">2 · ${targetStep}</span><h3>${targetTitle}</h3><p>${targetText}</p><code class="path">${esc(pair.target_library)}</code></div>${previous}</div></div></section>`;
}
function renderSonarrEpisodePairs(pairs){
  if(!pairs?.length)return '<div class="empty">No managed episode files are part of this release</div>';
  const actionable=pairs.filter(pair=>!['linked','already-on-target','verified-derived'].includes(pair.status)).length;
  return `<details class="move-manifest" open><summary><strong>${pairs.length} managed episode file${pairs.length===1?'':'s'}</strong><span>${actionable} require repair · missing series and season folders are created automatically</span></summary><div class="table-wrap"><table><thead><tr><th>State</th><th>qBittorrent source</th><th>Sonarr destination</th><th>Size</th></tr></thead><tbody>${pairs.map(pair=>`<tr><td>${badge(pair.status)} ${badge(pair.strategy)}</td><td class="path">${esc(pair.torrent_file)}</td><td class="path">${esc(pair.target_library)}</td><td>${fmtBytes(pair.size)}</td></tr>`).join('')}</tbody></table></div></details>`;
}
function renderPlan(plan){
  const error=renderReconcileBlocker(plan);
  const service=plan.app==='sonarr'?'Sonarr':'Radarr';
  const itemType=plan.app==='sonarr'?'Series':'Movie';
  const details=[['Status',badge(plan.status)],['Application',esc(service)],['Authoritative pool',esc(plan.target_pool||'—')],[`${service} ${itemType.toLowerCase()}`,plan.item_id?`${esc(plan.item_title)} (#${plan.item_id})`:'—'],['Torrent',esc(plan.torrent_name||'—')],[`Current ${service} ${itemType.toLowerCase()} folder`,`<span class="path">${esc(plan.current_item_path||'—')}</span>`],[`New ${service} ${itemType.toLowerCase()} folder`,`<span class="path">${esc(plan.target_item_path||'—')}</span>`],['Torrent hash',`<span class="path">${esc(plan.torrent_hash)}</span>`]];
  const pairs=plan.app==='sonarr'?renderSonarrEpisodePairs(plan.pairs):(plan.pairs?.length?plan.pairs.map(pair=>renderReconcilePair(pair,plan.app)).join(''):'<div class="empty">No file pairs available</div>');
  const missingAux=(plan.auxiliary_files||[]).filter(x=>['missing-target','torrent-sidecar'].includes(x.status));
  const conflicts=(plan.auxiliary_files||[]).filter(x=>['target-conflict','torrent-name-conflict','source-missing'].includes(x.status));
  const eligibleAux=(plan.auxiliary_files||[]).filter(reconcileAuxiliaryNeedsRepair);
  const linkedAux=(plan.auxiliary_files||[]).filter(x=>x.status==='linked');
  const auxCounts=(plan.auxiliary_files||[]).reduce((counts,item)=>({...counts,[item.kind]:(counts[item.kind]||0)+1}),{});
  const auxKinds=Object.entries(auxCounts).map(([kind,count])=>`${count} ${kind}`).join(' · ');
  const auxiliary=plan.auxiliary_files?.length?`<div class="auxiliary"><div class="auxiliary-head"><span class="section-kicker">${plan.app==='sonarr'?'Optional series files':'Optional secondary files'}</span><strong>Subtitles, NFO, artwork, and other non-video files</strong><small>Video and episode files are never treated as optional secondary files.</small></div><label class="check-option select-all"><input id="select-all-auxiliary" type="checkbox" ${eligibleAux.length?'checked':'disabled'}><span><strong>${eligibleAux.length?`Select all ${eligibleAux.length} secondary files requiring repair`:'No secondary files require repair'}</strong><small>qBittorrent-owned files are hardlinked. Files found only in the old library are copied and verified.</small></span></label><details ${plan.auxiliary_files.length<=40?'open':''}><summary>${plan.auxiliary_files.length} secondary files · ${esc(auxKinds)} · ${missingAux.length} missing${linkedAux.length?` · ${linkedAux.length} already linked`:''}${conflicts.length?` · ${conflicts.length} conflict`:''}</summary><div class="aux-list">${plan.auxiliary_files.map(x=>{const selectable=reconcileAuxiliaryNeedsRepair(x);const origin=x.origin==='qbittorrent'?(x.status==='linked'?'qBittorrent · already linked':'qBittorrent · hardlink'):'Old library · copy';return `<label class="aux-row ${selectable?'':'disabled'}"><input class="aux-file" type="checkbox" data-source="${esc(x.source)}" ${selectable?'checked':'disabled'}><span class="origin">${esc(origin)}</span><span>${badge(x.status)}</span><code class="path">${esc(x.source)}</code><span>→</span><code class="path">${esc(x.target)}</code></label>`}).join('')}</div></details>${conflicts.length?`<div class="aux-warning">${conflicts.length} secondary file(s) already exist with different content or compete for the same destination. They are skipped and will not be overwritten automatically.</div>`:''}</div>`:'';
  const ready=plan.status==='ready';
  const apply=Boolean(state.config?.apply);
  const copy=reconcileActionCopy(plan);
  const hasWork=reconcilePrimaryNeedsRepair(plan)||eligibleAux.length>0;
  const enabled=apply&&hasWork;
  const action=ready?`<div id="verification-result"></div><div class="reconcile-action"><div><strong>${hasWork?(apply?esc(copy.title):'Changes are locked in dry-run mode'):'No repairs are needed'}</strong><p>${hasWork?(apply?esc(copy.message):'Enable Write mode and provide writable media mounts only after reviewing the plan.'):'Every managed media file and discovered secondary file already has the expected hardlink identity.'}</p></div><div class="action-buttons"><button id="verify-plan" class="secondary" ${hasWork?'':'disabled'}>Verify content hashes</button><button id="queue-reconcile" class="secondary" ${enabled?'':'disabled'}>Add to queue</button><button id="apply-plan" class="danger" ${enabled?'':'disabled'}>${hasWork?esc(copy.button):'Nothing to repair'}</button></div></div>`:'';
  const repairTitle=plan.app==='sonarr'?'Managed episode repair':'Primary media repair';
  const repairHelp=plan.app==='sonarr'?'Only episodes proven to belong to this release are shown. Series artwork and other non-video files are handled separately below.':'The movie action is shown first. Optional subtitles and metadata are handled separately below.';
  const repairPanel=plan.error_code==='SONARR_PARTIAL_SERIES_POOL_CHANGE'?'':`<article class="panel"><div class="panel-head"><div><h2>${repairTitle}</h2><p>${repairHelp}</p></div></div>${pairs}${auxiliary}${action}</article>`;
  const detailPanel=plan.error_code==='SONARR_PARTIAL_SERIES_POOL_CHANGE'?'':`<article class="panel"><div class="panel-head"><div><h2>Plan details</h2><p>No changes have been made</p></div></div><div class="detail-grid">${details.map(([k,v])=>`<div class="detail"><small>${k}</small><strong>${v}</strong></div>`).join('')}</div></article>`;
  $('#plan-result').innerHTML=`${error}${detailPanel}${repairPanel}`
}
async function applyPlan(){
  const plan=state.plan;
  if(!plan||plan.status!=='ready'||!state.config?.apply||!reconcileHasSelectedWork(plan))return;
  const copy=reconcileActionCopy(plan);
  const oldFiles=plan.pairs.map(p=>p.source_library).join(', ');
  const auxiliaryFiles=$$('.aux-file:checked').map(input=>input.dataset.source);
  if(!await confirmAction({title:copy.confirmTitle,message:copy.confirmMessage,details:[[copy.detailLabel,copy.mediaUntouched?'None — existing file kept':oldFiles],['Selected sidecar files',String(auxiliaryFiles.length)]],confirmLabel:copy.button,danger:true}))return;
  const button=$('#apply-plan');
  button.disabled=true;
  button.textContent='Authorizing…';
  await refreshOperations();
  const afterId=Math.max(0,...state.operations.map(item=>item.id));
  let registrationWatcher=null;
  try{
    const confirmation=await api('/api/confirmations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'reconcile',torrentHash:plan.torrent_hash,payload:{auxiliaryFiles}})});
    button.textContent='Reconciling…';
    registrationWatcher=watchOperationRegistration(plan.torrent_hash,'reconcile',afterId);
    const result=await api(`/api/reconcile/${encodeURIComponent(plan.torrent_hash)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auxiliaryFiles,confirmationToken:confirmation.token})});
    if(result.disposition==='queued'){
      registrationWatcher.stop();
      if(registrationWatcher.registeredOperationId&&state.currentOperation?.id===registrationWatcher.registeredOperationId)finishOperationTracking();
      await refreshQueue();
      navigate('queue');
      toast(`Another operation has the shared slot; Reconcile queued last as ${result.public_id}`);
      return;
    }
    await ensureDirectOperationTracking(registrationWatcher,result,'reconcile');
    toast(`Reconcile result: ${result.state}`);
    await inspect(plan.torrent_hash);
    await load();
  }catch(e){
    registrationWatcher?.stop();
    await refreshOperations();
    rejectOperationTracking(plan.torrent_hash,'reconcile',e.message,afterId);
    toast(`Reconcile failed: ${e.message}`);
    button.disabled=false;
    button.textContent=copy.button;
  }
}
async function enqueueReconcile(){const plan=state.plan;if(!plan||plan.status!=='ready'||!state.config?.apply||!reconcileHasSelectedWork(plan))return;const auxiliaryFiles=$$('.aux-file:checked').map(input=>input.dataset.source);if(!await confirmAction({title:`Add ${plan.item_title} to the Reconcile queue?`,message:'The plan is revalidated immediately before execution.',details:[['Selected sidecar files',String(auxiliaryFiles.length)]],confirmLabel:'Add to queue'}))return;const button=$('#queue-reconcile');button.disabled=true;button.textContent='Authorizing…';try{const confirmation=await api('/api/confirmations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'reconcile',torrentHash:plan.torrent_hash,payload:{auxiliaryFiles}})});const queued=await api('/api/reconcile-queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({torrentHash:plan.torrent_hash,auxiliaryFiles,confirmationToken:confirmation.token})});toast(`Reconcile queued as ${queued.public_id}`);await refreshQueue();navigate('queue')}catch(error){toast(`Reconcile was not queued: ${error.message}`);button.disabled=false;button.textContent='Add to queue'}}
function renderSubtitleInventory(plan){const subtitles=plan.subtitle_files||[];if(!subtitles.length)return '<div class="move-no-extras">No subtitles were found in the torrent folders or archive manifests.</div>';const archiveCount=subtitles.filter(file=>file.location==='archive').length;const subfolderCount=subtitles.filter(file=>file.location==='subfolder').length;const torrentCount=subtitles.filter(file=>file.location==='torrent').length;return `<details class="move-manifest subtitle-inventory"><summary><strong>Subtitle discovery</strong><span>${subtitles.length} found · ${archiveCount} in archives · ${subfolderCount} in subfolders · ${torrentCount} beside media</span></summary><div class="table-wrap"><table><thead><tr><th>Location</th><th>Subtitle path</th><th>Archive source</th></tr></thead><tbody>${subtitles.map(file=>`<tr><td>${badge(file.location)}</td><td class="path">${esc(file.path)}</td><td class="path">${esc(file.archive||'—')}</td></tr>`).join('')}</tbody></table></div></details>`}
function renderMoveBlocker(plan){if(plan.status!=='blocked')return '';if(plan.error_code==='SONARR_PARTIAL_SERIES_POOL_CHANGE')return renderSonarrScopeBlocker(plan,'move');if(plan.error_code==='LIBRARY_SEEDED_MAPPING_REQUIRED'){const details=plan.error_details||{};const service=plan.app==='sonarr'?'Sonarr':'Radarr';return `<article class="panel release-conflict"><div class="panel-head"><div><h2>No exact ${service} media match</h2><p>The torrent is safely left untouched. Its files are already inside the ${service} library, but ${service} does not currently report these exact paths as managed media.</p></div>${badge('blocked')}</div><div class="release-evidence"><div><small>qBittorrent torrent</small><strong>${esc(details.torrent_name||plan.torrent_name)}</strong><code>${esc(details.torrent_hash||plan.torrent_hash)}</code></div><div><small>qBittorrent content path${(details.media_paths||[]).length===1?'':'s'}</small>${(details.media_paths||[]).map(path=>`<code>${esc(path)}</code>`).join('')||`<code>${esc(details.save_path||'Unknown')}</code>`}</div></div><div class="recovery-guidance"><div><strong>How to resolve this</strong><p>${esc(details.action||`Verify the current media file in ${service}, then build the Move plan again.`)}</p></div><button type="button" class="secondary" id="recheck-release-identity">Build plan again</button></div></article>`}if(plan.error_code!=='ARR_CURRENT_RELEASE_MISMATCH')return `<div class="alert"><div class="alert-head"><span>✕</span> Move blocked</div><p>${esc(plan.reason)}</p>${plan.error_details?.action?`<p><strong>How to resolve this:</strong> ${esc(plan.error_details.action)}</p><button type="button" class="secondary" id="recheck-release-identity">Build plan again</button>`:''}</div>`;const details=plan.error_details||{};const files=details.files||[];return `<article class="panel release-conflict"><div class="panel-head"><div><h2>Release identity conflict</h2><p>Stowarr stopped before changing qBittorrent, files, or *Arr.</p></div>${badge('blocked')}</div><div class="release-evidence"><div><small>Selected qBittorrent release</small><strong>${esc(details.torrent_name||plan.torrent_name)}</strong><code>${esc(details.torrent_hash||plan.torrent_hash)}</code></div><div><small>Current ${plan.app==='sonarr'?'Sonarr':'Radarr'} item</small><strong>${esc(details.arr_item_title||plan.item_title||'Unknown')} ${details.arr_item_id?`(#${esc(details.arr_item_id)})`:''}</strong><span>The item matches, but its current media release does not.</span></div></div><div class="table-wrap"><table><thead><tr><th>Current *Arr media file</th><th>Matching torrent file</th><th>Result</th></tr></thead><tbody>${files.map(file=>`<tr><td class="path">${esc(file.arr_file)}</td><td class="path">${esc(file.torrent_file||`${file.candidate_count||0} same-size candidate(s), none verified`)}</td><td>${badge(file.status)}</td></tr>`).join('')}</tbody></table></div><div class="recovery-guidance"><div><strong>Safe recovery</strong><p>${esc(details.action||'Import the intended release in *Arr, then check again.')}</p></div><button type="button" class="secondary" id="recheck-release-identity">Recheck release identity</button></div></article>`}
function renderMoveWarnings(plan){return(plan.warnings||[]).map(warning=>`<aside class="move-advisory"><div class="alert-head"><span>!</span><strong>${esc(warning.title)}</strong></div><p>${esc(warning.message)}</p><dl><dt>Current Radarr path</dt><dd class="path">${esc(warning.currentPath||'—')}</dd><dt>Selected qBittorrent release</dt><dd class="path">${esc(warning.selectedRelease||'—')}</dd>${warning.suggestedPath?`<dt>Conventional Radarr path</dt><dd class="path">${esc(warning.suggestedPath)}</dd>`:''}</dl><p><strong>Recommended before Move:</strong> ${esc(warning.action)}</p><p class="move-advisory-safe">This is advisory only. Move remains available and will preserve the current Radarr folder name.</p></aside>`).join('')}
function renderMovePlan(plan){const blocked=renderMoveBlocker(plan);const warnings=renderMoveWarnings(plan);const service=plan.app==='sonarr'?'Sonarr':'Radarr';const itemType=plan.app==='sonarr'?'series':'movie';const extractionIds=new Set((plan.extraction_files||[]).map(file=>file.id));const files=plan.managed_files?.length?`<div class="table-wrap"><table><thead><tr><th>${service} file ID</th><th>Episodes</th><th>Current library file</th><th>New library file</th><th>Strategy</th><th>Size</th></tr></thead><tbody>${plan.managed_files.map(file=>`<tr><td>${esc(file.id??'—')}</td><td>${file.episodeIds?.length?file.episodeIds.map(id=>`#${esc(id)}`).join(', '):'—'}</td><td class="path">${esc(file.path)}</td><td class="path">${esc(file.targetPath)}</td><td>${extractionIds.has(file.id)?badge('verified re-extraction'):badge('hardlink')}</td><td>${fmtBytes(file.size)}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">No managed library files were mapped</div>';const tracked=plan.tracked_files?.length?`<details class="move-manifest" open><summary><strong>Torrent manifest — tracked by qBittorrent</strong><span>${plan.tracked_files.length} tracked files · ${fmtBytes(plan.tracked_files.reduce((sum,file)=>sum+file.size,0))}</span></summary><p class="manifest-help">These files belong to the torrent. qBittorrent moves and rechecks them; they cannot be individually deleted by this plan.</p><div class="table-wrap"><table><thead><tr><th>Type</th><th>Current qBittorrent file</th><th>Size</th><th>Action</th></tr></thead><tbody>${plan.tracked_files.map(file=>`<tr><td>${badge(file.kind)}</td><td class="path">${esc(file.path)}</td><td>${fmtBytes(file.size)}</td><td>Move with qBittorrent and recheck</td></tr>`).join('')}</tbody></table></div></details>`:'';const extras=plan.additional_files?.length?`<section class="move-extras"><div class="move-extras-head"><div><h3>Files outside the torrent manifest</h3><p>These files are not tracked by qBittorrent. Their scope shows whether Stowarr found them beside the download or in the ${service} library.</p></div><div><button type="button" class="secondary compact set-extra-action" data-action="move">Move all</button><button type="button" class="secondary compact set-extra-action" data-action="delete">Delete all</button></div></div><div class="table-wrap"><table><thead><tr><th>Found in</th><th>Type</th><th>Current file</th><th>Destination</th><th>Size</th><th>After verification</th></tr></thead><tbody>${plan.additional_files.map(file=>`<tr><td>${badge(file.scope)}</td><td>${badge(file.kind)}</td><td class="path">${esc(file.source)}</td><td class="path">${esc(file.target)}</td><td>${fmtBytes(file.size)}</td><td><select class="move-extra-action" data-source="${esc(file.source)}"><option value="move" selected>Move and verify</option><option value="delete">Delete after verification</option></select></td></tr>`).join('')}</tbody></table></div></section>`:'<div class="move-no-extras">No files were found outside qBittorrent and *Arr ownership.</div>';const apply=Boolean(state.config?.apply);const action=plan.status==='ready'?`<div class="reconcile-action"><div><strong>${apply?'Ready for a verified Move transaction':'Move is locked in dry-run mode'}</strong><p>qBittorrent moves and rechecks tracked content first. ${plan.extraction_required?'Stowarr then tests and re-extracts archive-derived media in isolated staging. ':''}Stowarr verifies additional files, rebuilds the ${itemType} library, waits for the ${service} rescan, and removes the empty old folder last.</p></div><button id="apply-move" class="danger" ${apply?'':'disabled'}>Review &amp; move to ${esc(plan.target_pool)}</button></div>`:'';$('#move-result').innerHTML=`${blocked}<article class="panel"><div class="panel-head"><div><h2>Move plan</h2><p>No changes have been made</p></div></div><div class="detail-grid"><div class="detail"><small>Status</small><strong>${badge(plan.status)}</strong></div><div class="detail"><small>${service} ${itemType}</small><strong>${esc(plan.item_title||'—')}${plan.item_id?` (#${esc(plan.item_id)})`:''}</strong></div><div class="detail"><small>Current authoritative pool</small><strong>${esc(plan.current_pool||'—')}</strong></div><div class="detail"><small>Selected destination pool</small><strong>${esc(plan.target_pool)}</strong></div><div class="detail"><small>Current qBittorrent save path</small><strong class="path">${esc(plan.current_save_path||'—')}</strong></div><div class="detail"><small>New qBittorrent save path</small><strong class="path">${esc(plan.target_save_path||'—')}</strong></div><div class="detail"><small>New qBittorrent category</small><strong>${esc(plan.target_category||'—')}</strong></div><div class="detail"><small>Content</small><strong>${badge(plan.content_mode)} ${plan.archive_files?`· ${esc(plan.archive_files)} archive files · ${plan.archive_verified?'integrity verified':'verification required'}`:''}</strong></div><div class="detail"><small>Required / available space</small><strong>${fmtBytes(plan.torrent_size+(plan.extraction_space||0))} / ${plan.free_space===null?'unknown':fmtBytes(plan.free_space)}</strong><span>${plan.extraction_space?`${fmtBytes(plan.extraction_space)} reserved for verified extraction`:''}</span></div></div></article>${warnings}<article class="panel"><div class="panel-head"><div><h2>Move manifest</h2><p>Ownership inventory from qBittorrent and ${service}; every physical file is listed once</p></div></div>${tracked}${renderSubtitleInventory(plan)}<div class="managed-section"><h3>Managed by ${service}</h3><p>The main media files ${service} expects in its library. A file may also be tracked by qBittorrent above.</p></div>${files}${extras}${action}</article>`}
function renderTorrentMembership(plan){const trackedPaths=new Set((plan.tracked_files||[]).map(file=>file.path));const yes='<span class="badge complete">YES — TRACKED</span>';const no='<span class="badge failed">NO — NOT TRACKED</span>';const membershipHead='<th class="torrent-membership-column">In torrent?</th>';const membershipCell=(included)=>`<td class="torrent-membership-column torrent-membership-${included?'yes':'no'}">${included?yes:no}</td>`;const trackedTable=$('.move-manifest table');if(trackedTable){trackedTable.querySelector('thead tr').insertAdjacentHTML('afterbegin',membershipHead);trackedTable.querySelectorAll('tbody tr').forEach(row=>row.insertAdjacentHTML('afterbegin',membershipCell(true)))}const managedTable=$('.managed-section + .table-wrap table');if(managedTable){managedTable.querySelector('thead tr').insertAdjacentHTML('afterbegin',membershipHead);managedTable.querySelectorAll('tbody tr').forEach((row,index)=>{const file=plan.managed_files[index];row.insertAdjacentHTML('afterbegin',membershipCell(Boolean(file&&trackedPaths.has(file.path))))})}const extras=$('.move-extras');if(extras){const description=extras.querySelector('.move-extras-head p');if(description)description.textContent='These files do not belong to the torrent and are not rechecked by qBittorrent. Choose whether Stowarr should move or delete each file after verification.';const table=extras.querySelector('table');const head=table.querySelector('thead th');head.textContent='In torrent?';head.classList.add('torrent-membership-column');table.querySelectorAll('tbody tr').forEach(row=>{row.cells[0].outerHTML=membershipCell(false)})}}
function initializeMoveActions(plan){renderTorrentMembership(plan);(plan.additional_files||[]).filter(file=>file.status==='target-conflict').forEach(file=>{const input=$(`.move-extra-action[data-source="${CSS.escape(file.source)}"]`);if(!input)return;input.value='delete';input.options[0].disabled=true;input.title='The destination already contains different data. Resolve it manually or delete this source after verification.'});const apply=$('#apply-move');if(apply&&!$('#queue-move')){const actions=document.createElement('div');actions.className='move-action-buttons';const queue=document.createElement('button');queue.id='queue-move';queue.type='button';queue.className='secondary';queue.textContent='Add to queue';queue.disabled=!state.config?.apply;apply.before(actions);actions.append(queue,apply)}}

function moveSubmission(plan=state.movePlan){
  const additionalFiles=Object.fromEntries($$('.move-extra-action').map(input=>[input.dataset.source,input.value]));
  const moving=Object.values(additionalFiles).filter(action=>action==='move').length;
  const deleting=Object.values(additionalFiles).filter(action=>action==='delete').length;
  const tracked=plan.tracked_files||[];
  const trackedKinds=Object.entries(tracked.reduce((counts,file)=>({...counts,[file.kind]:(counts[file.kind]||0)+1}),{})).map(([kind,count])=>`${count} ${kind}`).join(', ');
  const details=[
    ['Pool route',`${plan.current_pool} → ${plan.target_pool}`],
    ['Torrent manifest',`${tracked.length} files${trackedKinds?` (${trackedKinds})`:''}`],
    ['Library rebuild',`${plan.managed_files?.length||0} managed files and ${moving} additional files`],
    ['Delete after verification',`${deleting} explicitly selected files`],
    ['Final category',plan.target_category||'No category'],
  ];
  const warning=plan.warnings?.[0];
  if(warning)details.push(['Advisory',`Radarr folder will remain ${warning.currentPath}`]);
  return {additionalFiles,payload:{targetPool:plan.target_pool,additionalFiles},details,warning};
}

async function enqueueMove(){
  const plan=state.movePlan;
  if(!plan||plan.status!=='ready'||!state.config?.apply)return;
  const submission=moveSubmission(plan);
  if(!await confirmAction({
    title:`Add ${plan.torrent_name} to the Move queue?`,
    message:'The confirmed plan will be stored persistently and revalidated immediately before execution. Queued Moves run one at a time.',
    details:submission.details,
    confirmLabel:'Add to queue',
    danger:true,
  }))return;
  const button=$('#queue-move');
  button.disabled=true;
  button.textContent='Authorizing…';
  try{
    const confirmation=await api('/api/confirmations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'move',torrentHash:plan.torrent_hash,payload:submission.payload})});
    const queued=await api('/api/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...submission.payload,torrentHash:plan.torrent_hash,confirmationToken:confirmation.token})});
    toast(`Move queued as ${queued.public_id}`);
    clearMoveSelection();
    await refreshQueue();
    navigate('queue');
  }catch(error){
    toast(`Move was not queued: ${error.message}`);
    button.disabled=false;
    button.textContent='Add to queue';
  }
}

function renderQueue(){
  const render=(rows,kind,target)=>{
    $(target).innerHTML=rows.length?rows.map(item=>{
      const detail=item.detail||{};
      const linkedOperation=state.operations.find(operation=>operation.public_id===item.public_id);
      const operationId=item.operation_id||linkedOperation?.id;
      const operation=item.state==='RUNNING'?`<button class="link-button track-operation" data-public-id="${esc(item.public_id)}" data-kind="${kind}">Live progress</button>`:operationId?`<button class="link-button inspect-operation" data-operation-id="${operationId}">Details</button>`:'—';
      const action=item.state==='QUEUED'?`<button class="secondary compact cancel-queue" data-kind="${kind}" data-queue-id="${esc(item.public_id)}">Cancel</button>`:'';
      const positionLabel=item.state==='RUNNING'?'Running':item.state==='QUEUED'?`#${item.position||'—'} global`:'—';
      const error=item.error?`<small class="queue-error">${esc(item.error)}</small>`:'';
      const route=kind==='move'?`${esc(detail.current_pool||'—')} → ${esc(item.target_pool)}`:esc(detail.target_pool||'—');
      return `<tr data-queue-public-id="${esc(item.public_id)}"><td><span class="queue-position"><code>${esc(item.public_id)}</code><small>${esc(positionLabel)}</small></span></td><td class="queue-torrent"><strong>${esc(detail.torrent_name||item.torrent_hash)}</strong><code>${esc(item.torrent_hash)}</code>${error}</td><td>${route}</td><td>${badge(item.state)}</td><td>${operation}</td><td>${fmtTime(item.updated_at)}</td><td>${action}</td></tr>`;
    }).join(''):`<tr><td colspan="7" class="empty">The ${kind==='move'?'Move':'Reconcile'} queue is empty</td></tr>`;
    const removable=state.queueSummary?.[kind]?.terminal??rows.filter(item=>QUEUE_TERMINAL_STATES.has(item.state)).length;
    const clearButton=$(`.clear-queue[data-kind="${kind}"]`);
    if(clearButton)clearButton.disabled=!removable;
  };
  render(state.queue||[],'move','#queue-rows');
  render(state.reconcileQueue||[],'reconcile','#reconcile-queue-rows');
  const summaryKinds=['move','reconcile'];
  const hasCompleteSummary=summaryKinds.every(kind=>Number.isInteger(state.queueSummary?.[kind]?.total)&&Number.isInteger(state.queueSummary?.[kind]?.terminal));
  const active=hasCompleteSummary
    ?summaryKinds.reduce((count,kind)=>count+state.queueSummary[kind].total-state.queueSummary[kind].terminal,0)
    :[...(state.queue||[]),...(state.reconcileQueue||[])].filter(item=>['QUEUED','RUNNING'].includes(item.state)).length;
  $('#queue-count').textContent=(active+(state.recovery?.count||0))||'';
}

function renderRecovery(){
  const panel=$('#recovery-panel');
  const recovery=state.recovery;
  const notes=new Map($$('.recovery-note',panel).map(input=>[input.dataset.publicId,input.value]));
  panel.classList.toggle('hidden',!recovery?.required);
  if(!recovery?.required){
    $('#recovery-operations').innerHTML='';
    state.recoveryDiagnoses.clear();
    return;
  }
  const activeIds=new Set(recovery.operations.map(operation=>operation.public_id));
  for(const publicId of state.recoveryDiagnoses.keys()){
    if(!activeIds.has(publicId))state.recoveryDiagnoses.delete(publicId);
  }
  $('#recovery-operations').innerHTML=recovery.operations.map(operation=>{
    const detail=operation.detail||{};
    const prior=detail.recovery?.previous_state||detail.failed_after||'Unknown stage';
    const diagnosis=state.recoveryDiagnoses.get(operation.public_id)?.diagnosis;
    const recommendation=diagnosis?.recommendation;
    const qbit=diagnosis?.qbittorrent||{};
    const arr=diagnosis?.arr||{};
    const qbitSummary=diagnosis
      ?qbit.repairs
        ?`${qbit.matching||0}/${qbit.total||0} selected categories match their confirmed routes`
        :qbit.found
          ?`${qbit.state||'unknown state'} · ${qbit.files?.visible||0}/${qbit.files?.count||0} files visible with ${qbit.files?.size_matches||0} matching sizes`
          :qbit.error||'Torrent not found in qBittorrent'
      :'Not checked';
    const arrSummary=diagnosis
      ?arr.mapping_found
        ?`${arr.app||'*Arr'} item #${arr.item_id||'—'} · ${arr.item_path||'path unavailable'}`
        :arr.error||'No exact *Arr mapping found'
      :'Not checked';
    const diagnosisMarkup=diagnosis?`<div class="recovery-diagnosis ${recommendation?.safe_action==='manual'?'manual':'candidate'}"><strong>${esc(recommendation?.summary||'Inspection complete')}</strong><dl><dt>qBittorrent</dt><dd>${esc(qbitSummary)}</dd><dt>*Arr</dt><dd>${esc(arrSummary)}</dd><dt>Result</dt><dd>${esc(String(recommendation?.code||'manual review').replaceAll('_',' '))}</dd></dl><p>This diagnosis is read-only and does not replace a qBittorrent force recheck or a manual file-content inspection.</p></div>`:'';
    return `<section class="recovery-operation" data-recovery-public-id="${esc(operation.public_id)}"><header><div><span>${badge(operation.kind)}</span><strong>${esc(operation.public_id)} · ${esc(detail.torrent_name||operation.torrent_hash)}</strong><small>Interrupted after ${esc(String(prior).replaceAll('_',' '))}</small></div><button type="button" class="secondary compact diagnose-recovery" data-public-id="${esc(operation.public_id)}">${diagnosis?'Diagnose again':'Diagnose current state'}</button></header>${diagnosisMarkup}<div class="recovery-resolution ${diagnosis?'':'hidden'}"><label><span>Manual inspection / repair note</span><input class="recovery-note" data-public-id="${esc(operation.public_id)}" value="${esc(notes.get(operation.public_id)||'')}" placeholder="Briefly describe what you inspected or repaired"><small>No fixed phrase is required.</small></label><div class="recovery-resolution-actions"><button type="button" class="secondary compact recovery-note-preset" data-public-id="${esc(operation.public_id)}">Force recheck passed</button><button type="button" class="danger compact resolve-recovery" data-public-id="${esc(operation.public_id)}">Acknowledge and resume when clear</button></div></div></section>`;
  }).join('');
}

async function diagnoseRecovery(publicId){
  const button=$(`.diagnose-recovery[data-public-id="${CSS.escape(publicId)}"]`);
  if(button){button.disabled=true;button.textContent='Inspecting…'}
  try{
    const result=await api(`/api/recovery/${encodeURIComponent(publicId)}/diagnose`,{method:'POST'});
    state.recoveryDiagnoses.set(publicId,result);
    renderRecovery();
  }catch(error){
    toast(`Recovery diagnosis failed: ${error.message}`);
    if(button){button.disabled=false;button.textContent='Diagnose current state'}
  }
}

async function resolveRecovery(publicId){
  const input=$(`.recovery-note[data-public-id="${CSS.escape(publicId)}"]`);
  const note=input?.value.trim()||'';
  if(note.length<3){toast('Describe what you inspected or repaired before resuming');input?.focus();return}
  const operation=state.recovery?.operations.find(item=>item.public_id===publicId);
  if(!await confirmAction({title:`Acknowledge interrupted job ${publicId}?`,message:'This marks the old operation failed and may resume later queued work. It does not move, delete, recheck, or repair external data.',details:[['Torrent',operation?.detail?.torrent_name||operation?.torrent_hash||'—'],['Inspection note',note]],confirmLabel:'Acknowledge interruption',danger:true}))return;
  try{
    const result=await api(`/api/recovery/${encodeURIComponent(publicId)}/resolve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:true,note})});
    state.recovery=result.recovery;
    state.recoveryDiagnoses.delete(publicId);
    await Promise.all([refreshOperations(),refreshQueue(true)]);
    renderRecovery();
    toast(state.recovery.required?'Interruption acknowledged; queue remains paused for other recovery work':'Recovery cleared; queued work may resume');
  }catch(error){toast(`Recovery was not cleared: ${error.message}`)}
}

function automaticallyTrackRunningQueue(){
  if(state.recovery?.required){
    if(state.operationTracking)finishOperationTracking();
    return;
  }
  const runningMove=(state.queue||[]).find(item=>item.state==='RUNNING');
  const runningReconcile=(state.reconcileQueue||[]).find(item=>item.state==='RUNNING');
  const running=runningMove||runningReconcile;
  if(!running)return;
  let startHidden=true;
  if(state.operationTracking){
    if(!terminalOperation(state.currentOperation))return;
    startHidden=state.operationHidden;
    finishOperationTracking();
  }
  trackOperationByPublicId(running.public_id,runningMove?'move':'reconcile',startHidden);
}

async function refreshQueue(quiet=false,throwOnError=false){
  if(!state.authenticated)return;
  const previousActiveReconcile=activeReconcileQueueSignature();
  try{
    [state.queue,state.reconcileQueue,state.queueSummary,state.recovery]=await Promise.all([api('/api/queue'),api('/api/reconcile-queue'),api('/api/queue-summary'),api('/api/recovery')]);
    renderQueue();
    renderRecovery();
    automaticallyTrackRunningQueue();
    if(previousActiveReconcile!==activeReconcileQueueSignature()&&state.sync[state.syncApp])renderSync(state.sync[state.syncApp]);
  }catch(error){if(!quiet||location.hash==='#queue')toast(`Could not load queues: ${error.message}`);if(throwOnError)throw error}
}

async function cancelQueue(id,kind='move'){
  const label=kind==='reconcile'?'Reconcile':'Move';
  const endpoint=kind==='reconcile'?'reconcile-queue':'queue';
  if(!await confirmAction({title:`Cancel queued ${label} ${id}?`,message:'Only work that has not started can be cancelled.',confirmLabel:`Cancel queued ${label}`,danger:true}))return;
  try{await api(`/api/${endpoint}/${encodeURIComponent(id)}/cancel`,{method:'POST'});await refreshQueue();toast(`Queued ${label} ${id} cancelled`)}catch(error){toast(`Queued ${label} was not cancelled: ${error.message}`)}
}
async function clearQueue(kind){
  const label=kind==='reconcile'?'Reconcile':'Move';
  const rows=kind==='reconcile'?state.reconcileQueue:state.queue;
  const finished=state.queueSummary?.[kind]?.terminal??rows.filter(item=>QUEUE_TERMINAL_STATES.has(item.state)).length;
  if(!finished)return;
  const message=`This removes ${finished} finished queue ${finished===1?'entry':'entries'}. Waiting and running work and Operation History are always kept.`;
  if(!await confirmAction({title:`Clear finished ${label} jobs?`,message,details:[['Queue',label],['Finished entries removed',String(finished)]],confirmLabel:'Clear finished',danger:true}))return;
  const endpoint=kind==='reconcile'?'reconcile-queue':'queue';
  try{const result=await api(`/api/${endpoint}`,{method:'DELETE'});await refreshQueue();toast(`${result.deleted} ${label} queue ${result.deleted===1?'entry':'entries'} removed`)}catch(error){toast(`${label} queue was not cleared: ${error.message}`)}
}
function enhanceMoveRecovery(plan){
  if(plan.error_code!=='QBITTORRENT_ALREADY_ON_TARGET')return;
  const blocker=$('.alert',$('#move-result'));
  if(!blocker)return;
  blocker.querySelector('.alert-head').innerHTML=`<span>✓</span> qBittorrent data is already on ${esc(plan.target_pool)}`;
  blocker.querySelector('p').textContent='Move has nothing left to relocate. An earlier transaction may have moved and rechecked the torrent before its library update was interrupted.';
  const button=blocker.querySelector('#recheck-release-identity');
  if(button){
    button.textContent='Open Reconcile';
    button.id='open-reconcile-recovery';
    button.addEventListener('click',()=>inspect(plan.torrent_hash));
  }
}
async function inspectMove(hash,targetPool){
  const requestedHash=String(hash||'').trim();
  const requestedTarget=String(targetPool||'').trim();
  const generation=++state.movePlanGeneration;
  navigate('move');
  state.movePlan=null;
  $('#move-hash').value=requestedHash;
  $('#move-target').value=requestedTarget;
  $('#move-loading').classList.remove('hidden');
  $('#move-result').innerHTML='';
  try{
    const plan=await api(`/api/move/plan/${encodeURIComponent(requestedHash)}?targetPool=${encodeURIComponent(requestedTarget)}`);
    const currentHash=$('#move-hash').value.trim();
    const currentTarget=$('#move-target').value;
    if(generation!==state.movePlanGeneration||currentHash.toLowerCase()!==requestedHash.toLowerCase()||currentTarget!==requestedTarget)return;
    if(String(plan.torrent_hash||'').toLowerCase()!==requestedHash.toLowerCase()){
      throw new Error('The API returned a Move plan for a different torrent. Nothing was changed; refresh the catalog and try again.');
    }
    state.movePlan=plan;
    renderMovePlan(plan);
    initializeMoveActions(plan);
    enhanceMoveRecovery(plan);
  }catch(e){
    if(generation!==state.movePlanGeneration)return;
    $('#move-result').innerHTML=`<div class="alert"><div class="alert-head">Request failed</div><p>${esc(e.message)}</p></div>`;
  }finally{
    if(generation===state.movePlanGeneration)$('#move-loading').classList.add('hidden');
  }
}
async function applyMove(){
  const plan=state.movePlan;
  if(!plan||plan.status!=='ready'||!state.config?.apply)return;
  if(
    String(plan.torrent_hash||'').toLowerCase()!==$('#move-hash').value.trim().toLowerCase()
    ||String(plan.target_pool||'')!==$('#move-target').value
  ){
    invalidateMovePlan();
    toast('The selected torrent or destination changed. Build a fresh Move plan.');
    return;
  }
  const additionalFiles=Object.fromEntries($$('.move-extra-action').map(input=>[input.dataset.source,input.value]));
  const moving=Object.values(additionalFiles).filter(action=>action==='move').length;
  const deleting=Object.values(additionalFiles).filter(action=>action==='delete').length;
  const tracked=plan.tracked_files||[];
  const trackedKinds=Object.entries(tracked.reduce((counts,file)=>({...counts,[file.kind]:(counts[file.kind]||0)+1}),{})).map(([kind,count])=>`${count} ${kind}`).join(', ');
  const warning=plan.warnings?.[0];
  const details=[['Pool route',`${plan.current_pool} → ${plan.target_pool}`],['1 · Pause and isolate','Temporary Stowarr category on the destination pool'],['2 · Relocate and recheck',`${tracked.length} files in the torrent manifest${trackedKinds?` (${trackedKinds})`:''}`],['3 · Rebuild library',`${plan.managed_files?.length||0} managed files and ${moving} additional files`],['4 · Delete after verification',`${deleting} explicitly selected files`],['5 · Commit final route',plan.target_category||'No category']];
  if(warning)details.push(['Advisory',`Radarr folder will remain ${warning.currentPath}`]);
  if(!await confirmAction({title:`Move ${plan.torrent_name}?`,message:warning?'Review the transaction and the Radarr folder advisory. Move will preserve the existing library folder name.':'Review the complete transaction. The torrent is isolated from *Arr cleanup until every verification and library update succeeds.',details,confirmLabel:`Confirm move to ${plan.target_pool}`,danger:true}))return;
  const button=$('#apply-move');
  button.disabled=true;
  button.textContent='Authorizing…';
  const payload={targetPool:plan.target_pool,additionalFiles};
  await refreshOperations();
  const afterId=Math.max(0,...state.operations.map(item=>item.id));
  let registrationWatcher=null;
  try{
    const confirmation=await api('/api/confirmations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'move',torrentHash:plan.torrent_hash,payload})});
    button.textContent='Moving and verifying…';
    registrationWatcher=watchOperationRegistration(plan.torrent_hash,'move',afterId);
    const result=await api(`/api/move/apply/${encodeURIComponent(plan.torrent_hash)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,confirmationToken:confirmation.token})});
    if(result.disposition==='queued'){
      registrationWatcher.stop();
      if(registrationWatcher.registeredOperationId&&state.currentOperation?.id===registrationWatcher.registeredOperationId)finishOperationTracking();
      await refreshQueue();
      navigate('queue');
      toast(`Another operation has the shared slot; Move queued last as ${result.public_id}`);
      return;
    }
    await ensureDirectOperationTracking(registrationWatcher,result,'move');
    toast(`Move result: ${result.state}`);
    clearMoveSelection();
    state.qbitCatalog=null;
    await loadQbitCatalog(true);
    await load();
  }catch(e){
    registrationWatcher?.stop();
    await refreshOperations();
    rejectOperationTracking(plan.torrent_hash,'move',e.message,afterId);
    toast(`Move failed: ${e.message}`);
    button.disabled=false;
    button.textContent=`Review & move to ${plan.target_pool}`;
  }
}
function catalogRows(){if(!state.qbitCatalog)return [];return [...state.qbitCatalog.routes,...(state.qbitCatalog.library_seeded||[]),...state.qbitCatalog.unmanaged].flatMap(group=>group.paths.flatMap(path=>path.torrents))}
function torrentCard(row){return `<button class="torrent-card select-move ${esc(row.route_status)}" data-hash="${esc(row.hash)}" type="button"><strong>${esc(row.name)}</strong><span><i>${esc(row.category||'uncategorized')}</i>${badge(row.route_status)}${badge(row.state)}</span><small>${fmtBytes(row.size)} · ${(row.progress*100).toFixed(1)}%</small><code>${esc(row.hash)}</code></button>`}
function renderCatalogPaths(paths,terms,primary){return paths.map(path=>{const torrents=path.torrents.filter(row=>{if(!terms.length)return true;const searchable=[row.name,row.hash,row.category,row.save_path,path.path].map(value=>String(value||'').toLowerCase()).join(' ');return terms.every(term=>searchable.includes(term))});if(!torrents.length)return {html:'',count:0};const expanded=terms.length>0||primary||path.route==='download';return {count:torrents.length,html:`<details class="path-group ${esc(path.route||'route')}" ${expanded?'open':''}><summary><span><em>${esc(path.route||'actual path')}</em>${esc(path.path)}</span><b>${torrents.length}</b></summary><div class="torrent-cards">${torrents.map(torrentCard).join('')}</div></details>`}})}
function renderQbitCatalog(){if(!state.qbitCatalog)return;const terms=$('#move-search').value.trim().toLowerCase().split(/\s+/).filter(Boolean);const routeGroups=state.qbitCatalog.routes.map((route,index)=>({kind:'route',key:`route:${route.app}:${route.pool}:${route.category}:${index}`,label:`${route.app[0].toUpperCase()+route.app.slice(1)} · ${route.pool}`,group:route}));const libraryGroups=(state.qbitCatalog.library_seeded||[]).map((group,index)=>({kind:'library',key:`library:${group.app}:${group.pool}:${group.root_family||index}`,label:`Library-seeded · ${group.app[0].toUpperCase()+group.app.slice(1)} · ${group.pool} · ${group.root_family||'library'}`,group}));const unmanagedGroups=state.qbitCatalog.unmanaged.map((group,index)=>({kind:'unmanaged',key:`unmanaged:${group.pool||'outside'}:${index}`,label:`Unmanaged · ${group.pool||'outside pools'}`,group}));const columns=[...routeGroups,...libraryGroups,...unmanagedGroups];const availableKeys=new Set(columns.map(column=>column.key));state.hiddenMoveColumns.forEach(key=>{if(!availableKeys.has(key))state.hiddenMoveColumns.delete(key)});const shown=columns.filter(column=>!state.hiddenMoveColumns.has(column.key));$('#move-column-filters').innerHTML=`<span>Columns</span><div>${columns.map(column=>`<button type="button" class="column-toggle ${state.hiddenMoveColumns.has(column.key)?'':'active'}" data-column-key="${esc(column.key)}" aria-pressed="${state.hiddenMoveColumns.has(column.key)?'false':'true'}"><i></i>${esc(column.label)}<b>${column.group.count}</b></button>`).join('')}</div><button type="button" id="show-all-columns" class="text-button" ${state.hiddenMoveColumns.size?'':'disabled'}>Show all</button>`;let visible=0;const renderedColumns=shown.map(column=>{const primary=column.kind==='route';const rendered=renderCatalogPaths(column.group.paths,terms,primary);visible+=rendered.reduce((sum,item)=>sum+item.count,0);if(primary){const route=column.group;return `<section class="pool-column route-column" data-column="${esc(column.key)}"><header><div><h3>${esc(column.label)}</h3><small>${esc(route.category)} · ${esc(route.tag)}</small><code>${(route.roots||[route.root]).map(esc).join('<br>')}</code></div><strong>${route.count}</strong></header><div class="pool-column-body">${rendered.map(item=>item.html).join('')||'<div class="empty">No matching routed torrents</div>'}</div></section>`}const group=column.group;if(column.kind==='library')return `<section class="pool-column library-column" data-column="${esc(column.key)}"><header><div><h3>${esc(column.label)}</h3><small>qBittorrent is seeding directly from the managed library</small><code>${esc(group.root)}</code></div><strong>${group.count}</strong></header><div class="pool-column-body">${rendered.map(item=>item.html).join('')||'<div class="empty">No matching library-seeded torrents</div>'}</div></section>`;return `<section class="pool-column unmanaged-column" data-column="${esc(column.key)}"><header><div><h3>${esc(column.label)}</h3><small>Category does not match a configured *Arr route</small></div><strong>${group.count}</strong></header><div class="pool-column-body">${rendered.map(item=>item.html).join('')||'<div class="empty">No matching unmanaged torrents</div>'}</div></section>`}).join('');$('#move-visible-count').textContent=`${visible} visible · ${state.qbitCatalog.total} total`;$('#move-search-results').innerHTML=shown.length?`<div class="pool-board">${renderedColumns}</div>`:'<div class="empty column-empty">All torrent columns are hidden. Use the column filter to show one or more.</div>'}
function renderRoutingAudit(){if(!state.routingAudit)return;const audit=state.routingAudit;$('#move-routing-status').innerHTML=`<div class="routing-status ${esc(audit.status)}"><div>${badge(audit.status)}<span>${audit.status==='ready'?'All configured *Arr category routes are complete':`${audit.issue_count} routing issue(s) need attention`}</span></div><button id="open-routing-guide" class="secondary">Routing diagnostics</button></div>`;const routes=audit.services.flatMap(service=>service.routes);$('#routing-guide-content').innerHTML=routes.map(route=>`<article class="guide-route ${esc(route.status)}"><header><div><h3>${esc(route.app[0].toUpperCase()+route.app.slice(1))} · ${esc(route.pool)}</h3><span>${badge(route.status)}</span></div><code>${esc(route.category)}</code></header><div class="guide-chain"><div><small>1 · *ARR DOWNLOAD CLIENT</small><strong>${esc(route.download_clients[0]?.name||'Missing')}</strong></div><b>→</b><div><small>2 · ROUTE CATEGORY</small><strong>${esc(route.category)}</strong></div><b>→</b><div><small>3 · QBITTORRENT SAVE PATH</small><strong>${esc(route.qbit_save_path||'missing')}</strong></div></div><div class="guide-guards"><div><small>CLIENT SELECTION TAG</small><strong>${esc(route.tag)}</strong><span>${route.download_clients[0]?.tags.includes(route.tag)?'Attached to this client':'Not attached to this client'}</span></div><div><small>LIBRARY DESTINATION</small><strong>${esc(route.root)}</strong><span>Root folder for the selected movie or series</span></div></div>${route.issues.length?`<ul>${route.issues.map(issue=>`<li>${esc(issue)}</li>`).join('')}</ul>`:'<p class="route-ready">Category route and client restriction are complete.</p>'}</article>`).join('')}
async function refreshRoutingGuide(){const content=$('#routing-guide-content');content.innerHTML='<div class="loading"><span></span>Refreshing Radarr, Sonarr, and qBittorrent routing…</div>';try{state.routingAudit=await api('/api/routing/audit');renderRoutingAudit()}catch(error){content.innerHTML=`<div class="alert inline-alert"><div class="alert-head">Routing diagnostics could not be refreshed</div><p>${esc(error.message)}</p></div>`}}
function invalidateMovePlan(){state.movePlanGeneration+=1;state.movePlan=null;$('#move-loading').classList.add('hidden');$('#move-result').innerHTML=''}
function clearMoveSelection(){invalidateMovePlan();state.moveTorrent=null;$('#move-hash').value='';$('#move-selected-name').textContent='';$('#move-selected-hash').textContent='';$('.move-selection').classList.add('hidden')}
async function loadQbitCatalog(force=false){if(state.qbitCatalog&&state.routingAudit&&!force){renderRoutingAudit();renderQbitCatalog();return}clearMoveSelection();$('#move-search-loading').classList.remove('hidden');$('#move-search-results').innerHTML='';try{const [catalog,audit]=await Promise.all([api('/api/qbittorrent/torrents'),api('/api/routing/audit')]);state.qbitCatalog=catalog;state.routingAudit=audit;renderRoutingAudit();renderQbitCatalog()}catch(e){$('#move-search-results').innerHTML=`<div class="alert inline-alert"><div class="alert-head">qBittorrent catalog failed</div><p>${esc(e.message)}</p></div>`}finally{$('#move-search-loading').classList.add('hidden')}}
function selectMoveTorrent(hash){const torrent=catalogRows().find(row=>row.hash===hash);if(!torrent)return;invalidateMovePlan();state.moveTorrent=torrent;$('#move-hash').value=hash;$('#move-selected-name').textContent=torrent.name;$('#move-selected-hash').textContent=hash;$('.move-selection').classList.remove('hidden');const destination=state.config.pools.find(pool=>pool.name!==torrent.pool)||state.config.pools[0];if(destination)$('#move-target').value=destination.name;$('.move-selection').scrollIntoView({behavior:'smooth',block:'nearest'})}
function hashCell(file,label){if(!file)return `<div><small>${esc(label)}</small><strong>Not applicable</strong><span>Packed torrent: media is not part of the torrent manifest</span></div>`;if(!file.exists)return `<div><small>${esc(label)}</small><strong>Not present</strong><code class="path">${esc(file.path)}</code></div>`;return `<div><small>${esc(label)}</small><strong title="${esc(file.sha256)}">SHA-256 ${esc(file.sha256.slice(0,16))}…</strong><span>inode ${file.inode} · links ${file.links}</span><code class="path">${esc(file.path)}</code></div>`}
async function verifyPlan(){const plan=state.plan;if(!plan||plan.status!=='ready'||!reconcileHasSelectedWork(plan))return;const button=$('#verify-plan');button.disabled=true;button.textContent='Hashing files…';$('#verification-result').innerHTML='<div class="loading"><span></span>Hashing torrent, old library, and new library paths. This may take several minutes…</div>';try{const result=await api(`/api/verify/${encodeURIComponent(plan.torrent_hash)}`,{method:'POST'});const videos=result.video_files.map(file=>{const packed=file.strategy==='archive-reextract'||file.strategy==='verified-copy';const restore=file.status==='missing-library';const valid=restore?file.torrent?.exists&&file.old_library?.exists===false:packed?file.new_matches_torrent!==false:file.old_matches_torrent&&file.new_matches_torrent!==false;const comparison=restore?'Missing Radarr media restoration':packed?'Extracted media comparison':'Direct torrent media comparison';const explanation=restore?'The selected qBittorrent source is present. Its pieces are force-rechecked during execution before the missing library hardlink is created.':packed?'The archive set remains authoritative; extracted media is verified independently.':`Old matches torrent: ${file.old_matches_torrent?'Yes':'No'} · New matches expected content: ${file.new_matches_torrent===null?'Not present yet':file.new_matches_torrent?'Yes':'No'}`;return `<div class="verification-file"><div class="verification-head">${badge(restore&&valid?'ready-to-restore':valid?'verified':'mismatch')}<strong>${comparison}</strong></div><div class="hash-grid">${hashCell(file.torrent,'qBittorrent media file')}${hashCell(file.old_library,restore?'Missing library path':'Old library path')}${hashCell(file.new_library,'New library path')}</div><p>${explanation}</p></div>`}).join('');const sidecarMatched=result.sidecar_files.filter(x=>x.matches_target===true).length;const sidecarMissing=result.sidecar_files.filter(x=>x.matches_target===null).length;const sidecarDifferent=result.sidecar_files.filter(x=>x.matches_target===false).length;$('#verification-result').innerHTML=`<div class="verification ${result.status}"><div class="verification-title"><strong>Hash verification: ${esc(result.status)}</strong><span>${result.sidecar_files.length} sidecars · ${sidecarMatched} matching · ${sidecarMissing} destination missing · ${sidecarDifferent} different</span></div>${videos}</div>`}catch(e){$('#verification-result').innerHTML=`<div class="alert"><div class="alert-head">Hash verification failed</div><p>${esc(e.message)}</p></div>`}finally{button.disabled=false;button.textContent='Verify content hashes'}}
function setSyncApp(app){
  state.syncApp=app;
  $$('.service-tab').forEach(x=>x.classList.toggle('active',x.dataset.app===app));
  $('#sync-title').textContent=`${app[0].toUpperCase()+app.slice(1)} hash audit`;
  const cached=state.sync[app];
  if(cached)renderSync(cached);else{
    $('#sync-summary').innerHTML='';
    $('#sync-filters').innerHTML='';
    $('#sync-safe-actions').innerHTML='';
    $('#sync-rows').innerHTML=`<tr><td colspan="7" class="empty">Run the ${esc(app[0].toUpperCase()+app.slice(1))} audit to compare hashes</td></tr>`;
  }
}
function activeReconcileQueueItem(hash){
  const normalized=String(hash||'').toLowerCase();
  return (state.reconcileQueue||[]).find(item=>['QUEUED','RUNNING'].includes(item.state)&&String(item.torrent_hash||'').toLowerCase()===normalized);
}
function activeReconcileQueueSignature(){
  return (state.reconcileQueue||[])
    .filter(item=>['QUEUED','RUNNING'].includes(item.state))
    .map(item=>`${String(item.torrent_hash||'').toLowerCase()}:${item.state}`)
    .sort()
    .join('|');
}
function renderSync(result){
  const missingLabel=result.app==='sonarr'?'No matching Sonarr series':'No matching Radarr movie';
  const hidden=state.syncHiddenStatuses[result.app]||(state.syncHiddenStatuses[result.app]=new Set());
  const statusCounts=new Map();
  result.rows.forEach(row=>statusCounts.set(row.status,(statusCounts.get(row.status)||0)+1));
  const visibleRows=result.rows.filter(row=>!hidden.has(row.status));
  const rowMarkup=row=>{
    const issue=row.healthy===undefined?row.status!=='in-sync':row.healthy!==true;
    const queued=activeReconcileQueueItem(row.hash);
    const canRepairCategory=row.status==='category-unconfigured'&&row.category_repairable;
    const safePlanCandidate=row.safe_plan_candidate===true;
    const actionLabel=canRepairCategory?'Stowarr fix available':safePlanCandidate?'Stowarr safe-plan candidate':'Manual fix';
    const categoryAction=canRepairCategory?`<button type="button" class="secondary compact repair-sync-category" data-app="${esc(result.app)}" data-hash="${esc(row.hash)}" data-name="${esc(row.torrent_name)}" data-current-category="${esc(row.category||'none')}" data-category="${esc(row.expected_category)}" data-pool="${esc(row.qbit_pool)}" ${state.config?.apply?'':'disabled'}>Set category</button>`:'';
    const primaryAction=!issue&&row.status==='packed-media'
      ?''
      :queued
      ?`<button class="link-button view-sync-queue" data-public-id="${esc(queued.public_id)}" title="Open this Reconcile job in Queue">${queued.state==='RUNNING'?'Running':'In queue'}</button>`
      :`<button class="link-button inspect" data-hash="${esc(row.hash)}">${issue?'Diagnose':'Reconcile'}</button>`;
    return `<tr title="${esc(row.reason)}"><td>${badge(row.status)}</td><td class="sync-title-cell"><strong>${esc(row.torrent_name)}</strong><small>${esc(row.item_title||missingLabel)}</small>${issue?`<small class="sync-diagnosis">${esc(row.reason)}</small>`:''}</td><td><span class="hash-short" title="${esc(row.hash)}">${esc(row.hash.slice(0,12))}…</span></td><td><span class="category">${esc(row.category||'none')}</span>${row.expected_category&&row.category!==row.expected_category?`<small class="sync-expected">Expected: ${esc(row.expected_category)}</small>`:''}</td><td>${esc(row.qbit_pool||'—')}</td><td class="path">${esc(row.arr_path||row.reason)}${issue&&row.action?`<small class="sync-action"><b>${actionLabel}:</b> ${esc(row.action)}</small>`:''}</td><td><div class="sync-row-actions">${categoryAction}${primaryAction}</div></td></tr>`;
  };
  const issues=visibleRows.filter(row=>row.healthy===undefined?row.status!=='in-sync':row.healthy!==true);
  const healthy=visibleRows.filter(row=>row.healthy===undefined?row.status==='in-sync':row.healthy===true);
  const expanded=state.syncExpanded.has(result.app);
  const healthyGroup=healthy.length?`<tr class="sync-group-row"><td colspan="7"><button type="button" class="sync-group-toggle" data-sync-group="${esc(result.app)}" aria-expanded="${expanded}"><span><i>${expanded?'▾':'▸'}</i><strong>In sync</strong><small>${healthy.length} ${healthy.length===1?'title':'titles'} without detected issues</small></span><b>${expanded?'Collapse':'Show'}</b></button></td></tr>${expanded?healthy.map(rowMarkup).join(''):''}`:'';
  $('#sync-summary').innerHTML=`<span><strong>${result.scanned}</strong><small>qBit torrents</small></span><span><strong>${result.matched_history}</strong><small>hash matches</small></span><span><strong>${result.in_sync}</strong><small>in sync</small></span><span><strong>${result.issues}</strong><small>issues</small></span><span><strong>${visibleRows.length}</strong><small>visible</small></span>`;
  const hasSafePossibilities=result.rows.some(row=>row.safe_plan_candidate===true);
  $('#sync-filters').innerHTML=`<span>Status</span><div>${[...statusCounts].map(([status,count])=>`<button type="button" class="sync-status-filter ${hidden.has(status)?'':'active'}" data-status="${esc(status)}" aria-pressed="${hidden.has(status)?'false':'true'}">${badge(status)}<b>${count}</b></button>`).join('')}</div><button type="button" class="text-button" id="sync-issues-only">Issues only</button><button type="button" class="text-button" id="sync-show-all">Show all</button>${hasSafePossibilities?'<button type="button" class="secondary compact" id="plan-safe-sync">Plan safe fixes</button>':''}`;
  $('#sync-rows').innerHTML=!result.rows.length?`<tr><td colspan="7" class="empty">No ${esc(result.app)} torrents found in qBittorrent</td></tr>`:visibleRows.length?`${issues.map(rowMarkup).join('')}${healthyGroup}`:'<tr><td colspan="7" class="empty">No audit rows match the selected status filters</td></tr>';
  renderSafeSyncPlan(state.safeSyncPlans[result.app]);
}
async function repairSyncCategory(button){
  const app=button.dataset.app;
  const hash=button.dataset.hash;
  const category=button.dataset.category;
  if(!await confirmAction({title:`Set qBittorrent category to ${category}?`,message:'Stowarr will revalidate the exact *Arr hash association, torrent save path, and configured qBittorrent category route before changing anything.',details:[['Torrent',button.dataset.name],['Current category',button.dataset.currentCategory],['New category',category],['Authoritative pool',button.dataset.pool]],confirmLabel:'Set category'}))return;
  button.disabled=true;
  button.textContent='Validating…';
  await refreshOperations();
  const afterId=Math.max(0,...state.operations.map(item=>item.id));
  const registrationWatcher=watchOperationRegistration(hash,'category',afterId);
  try{
    const result=await api(`/api/sync/${encodeURIComponent(app)}/${encodeURIComponent(hash)}/category`,{method:'POST'});
    await refreshOperations();
    if(!registrationWatcher.registeredOperationId&&result.operation_id){
      registrationWatcher.stop();
      startOperationTracking(operations=>operations.find(item=>item.id===result.operation_id),'category');
    }
    toast(result.changed?`Category changed to ${result.category}`:`Category is already ${result.category}`);
    await runSync({app,throwOnError:true});
  }catch(error){
    registrationWatcher.stop();
    await refreshOperations();
    rejectOperationTracking(hash,'category',error.message,afterId);
    toast(`Category was not changed: ${error.message}`);
    button.disabled=false;
    button.textContent='Set category';
  }
}
function renderSafeSyncPlan(plan){
  const target=$('#sync-safe-actions');
  if(!plan){target.innerHTML='';return}
  const category=plan.category_repairs||[];
  const reconcile=plan.reconcile_candidates||[];
  const queued=plan.queued_reconciles||[];
  const manual=plan.manual||[];
  const preview=(items,empty)=>items.length?`<ul>${items.slice(0,8).map(item=>`<li><strong>${esc(item.torrent_name)}</strong><small>${esc(item.category?`${item.current_category||'none'} → ${item.category}`:item.target_pool?`Reconcile to ${item.target_pool}`:item.reason||item.status)}</small></li>`).join('')}${items.length>8?`<li><strong>+ ${items.length-8} more</strong></li>`:''}</ul>`:`<p>${esc(empty)}</p>`;
  const reconcileDisabled=!reconcile.length||!state.config?.apply||category.length>0;
  target.innerHTML=`<section class="sync-safe-plan"><header><div><h3>Safe assisted repair</h3><p>Fresh plans only. Category fixes are applied first; Reconcile candidates enter the shared FIFO queue. Ambiguous items remain manual.</p></div><span>${plan.safe_count} safe</span></header><div class="sync-safe-columns"><article><h4>1 · Category fixes <b>${category.length}</b></h4>${preview(category,'No safely repairable categories.')}<button type="button" id="apply-safe-categories" class="primary compact" ${category.length&&state.config?.apply?'':'disabled'}>Apply safe category fixes</button></article><article><h4>2 · Reconcile queue <b>${reconcile.length}</b></h4>${preview(reconcile,'No repair candidates have a ready Reconcile plan.')}<button type="button" id="queue-safe-reconciles" class="primary compact" ${reconcileDisabled?'disabled':''}>${category.length?'Apply category fixes first':'Queue safe reconciles'}</button></article><article><h4>Manual review <b>${manual.length}</b></h4>${preview(manual,'No remaining manual issues in this plan.')}${queued.length?`<p>${queued.length} Reconcile ${queued.length===1?'job is':'jobs are'} already active.</p>`:''}</article></div></section>`;
}
const safePlanSteps=()=>[
  {id:'audit',title:'Read current audit',description:'Reading qBittorrent and exact *Arr history associations.'},
  {id:'categories',title:'Classify category fixes',description:'Checking save paths, pool routes, and configured categories.'},
  {id:'reconciles',title:'Build fresh Reconcile plans',description:'Testing each repair candidate against the complete Reconcile safety plan.'},
  {id:'manual',title:'Separate manual issues',description:'Keeping ambiguous or incomplete evidence outside automation.'},
];
async function fetchSafeSyncPlan(app,onProgress){
  return streamApi(`/api/sync/${encodeURIComponent(app)}/safe-plan/progress`,{},onProgress);
}
function offerSafePlanConfirmation(app,plan,prefix=''){
  const repairs=plan.category_repairs||[];
  const candidates=plan.reconcile_candidates||[];
  const manual=plan.manual||[];
  const summary=[prefix,`${plan.safe_count} safe actions found; ${manual.length} remain manual`].filter(Boolean).join(' · ');
  if(repairs.length){
    confirmSafeWorkflowPhase({
      summary,
      title:`Review ${repairs.length} safe category ${repairs.length===1?'fix':'fixes'}`,
      message:'No changes have been made. Confirm to revalidate the complete batch, then process each torrent one at a time. Reconcile will be planned again after categories are current.',
      details:[
        ['Next phase',`${repairs.length} qBittorrent category ${repairs.length===1?'fix':'fixes'}`],
        ['Afterwards',`${candidates.length} current Reconcile ${candidates.length===1?'candidate':'candidates'} will be discarded and rebuilt`],
        ['Manual review',`${manual.length} ${manual.length===1?'item remains':'items remain'} excluded`],
        ...repairs.slice(0,8).map(item=>[item.torrent_name,`${item.current_category||'none'} → ${item.category}`]),
        ...(repairs.length>8?[['Additional category fixes',String(repairs.length-8)]]:[]),
      ],
      confirmLabel:`Confirm ${repairs.length} category ${repairs.length===1?'fix':'fixes'}`,
      onConfirm:()=>applySafeCategories({app,plan,confirmed:true}),
    });
    return;
  }
  if(candidates.length){
    confirmSafeWorkflowPhase({
      summary,
      title:`Review ${candidates.length} safe Reconcile ${candidates.length===1?'job':'jobs'}`,
      message:'No jobs have been queued. Confirm to freshly authorize each problem one at a time; changed or ambiguous titles are skipped and accepted jobs are appended to the shared FIFO queue.',
      details:[
        ['Next phase',`${candidates.length} Reconcile ${candidates.length===1?'candidate':'candidates'}`],
        ['Queue behavior','Each accepted title is added after existing Move and Reconcile work'],
        ['Manual review',`${manual.length} ${manual.length===1?'item remains':'items remain'} excluded`],
        ...candidates.slice(0,8).map(item=>[item.torrent_name,`${item.target_pool} · ${item.auxiliary_count} auxiliary files`]),
        ...(candidates.length>8?[['Additional Reconcile candidates',String(candidates.length-8)]]:[]),
      ],
      confirmLabel:`Confirm ${candidates.length} Reconcile ${candidates.length===1?'job':'jobs'}`,
      onConfirm:()=>queueSafeReconciles({app,plan,confirmed:true}),
    });
    return;
  }
  finishSafeWorkflow(summary||'No safe automated actions are currently available');
}
async function planSafeSyncFixes(){
  const app=state.syncApp;
  const button=$('#plan-safe-sync');
  if(button)button.disabled=true;
  startSafeWorkflow(`Plan safe ${app==='radarr'?'Radarr':'Sonarr'} repairs`,'Building a read-only safe action plan',safePlanSteps());
  try{
    const plan=await fetchSafeSyncPlan(app,updateSafeWorkflow);
    state.safeSyncPlans[app]=plan;
    if(state.syncApp===app)renderSafeSyncPlan(plan);
    offerSafePlanConfirmation(app,plan);
    toast(`${plan.safe_count} safe assisted action${plan.safe_count===1?'':'s'} found`);
  }catch(error){
    failSafeWorkflow(error.message);
    toast(`Safe plan failed: ${error.message}`);
  }finally{
    if(button?.isConnected)button.disabled=false;
  }
}
async function applySafeCategories(options={}){
  const app=options.app||state.syncApp;
  const plan=options.plan||state.safeSyncPlans[app];
  const repairs=plan?.category_repairs||[];
  if(!repairs.length)return;
  if(!options.confirmed&&!await confirmAction({title:`Apply ${repairs.length} safe category ${repairs.length===1?'fix':'fixes'}?`,message:'Before changing qBittorrent, Stowarr will rebuild the audit and verify every exact hash association, download path, pool route, and category destination. If any item changed, the entire batch is rejected.',details:repairs.slice(0,10).map(item=>[item.torrent_name,`${item.current_category||'none'} → ${item.category}`]),confirmLabel:'Apply safe fixes'}))return;
  const hashes=repairs.map(item=>item.hash);
  const button=$('#apply-safe-categories');
  button.disabled=true;
  startSafeWorkflow(`Apply ${repairs.length} safe category ${repairs.length===1?'fix':'fixes'}`,'Revalidating the exact batch before the first change',[
    {id:'confirmation',title:'Bind exact confirmation',description:'Binding the selected hashes and current safe plan to one single-use token.'},
    {id:'validation',title:'Revalidate complete batch',description:'Every selected torrent must remain safe before any category changes.'},
    {id:'apply',title:'Apply qBittorrent categories',description:'Changing only the previously validated category routes.'},
    {id:'audit',title:'Refresh Sync audit',description:'Reading qBittorrent and *Arr again after the batch.'},
    {id:'rebuild',title:'Rebuild safe action plan',description:'Discovering which repair candidates are now ready for Reconcile.'},
  ]);
  try{
    const confirmation=await api(`/api/sync/${encodeURIComponent(app)}/safe-category/confirmation`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({torrentHashes:hashes})});
    updateSafeWorkflow({stage:'confirmation',current:1,total:1,message:`Confirmation expires at ${new Date(confirmation.expires_at*1000).toLocaleTimeString()}`});
    const result=await streamApi(`/api/sync/${encodeURIComponent(app)}/safe-category/apply-progress`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({torrentHashes:hashes,confirmationToken:confirmation.token})},updateSafeWorkflow);
    toast(`${result.changed} qBittorrent ${result.changed===1?'category':'categories'} changed`);
    await refreshOperations();
    updateSafeWorkflow({stage:'audit',current:0,total:1,message:'Refreshing the complete Sync audit'});
    await runSync({app,throwOnError:true});
    updateSafeWorkflow({stage:'audit',current:1,total:1,message:'Sync audit refreshed'});
    updateSafeWorkflow({stage:'rebuild',current:0,total:1,message:'Building the next safe action plan'});
    const nextPlan=await fetchSafeSyncPlan(app,event=>updateSafeWorkflow({stage:'rebuild',current:0,total:1,message:event.message}));
    state.safeSyncPlans[app]=nextPlan;
    if(state.syncApp===app)renderSafeSyncPlan(nextPlan);
    updateSafeWorkflow({stage:'rebuild',current:1,total:1,message:`${nextPlan.safe_count} safe actions remain after category repair`});
    offerSafePlanConfirmation(app,nextPlan,`${result.changed} ${result.changed===1?'category':'categories'} changed`);
  }catch(error){
    failSafeWorkflow(`${error.message}. Any category changes already shown as complete are retained; rerun the audit before continuing.`);
    toast(`Category workflow stopped: ${error.message}`);
    button.disabled=false;
  }
}
async function queueSafeReconciles(options={}){
  const app=options.app||state.syncApp;
  const plan=options.plan||state.safeSyncPlans[app];
  const candidates=plan?.reconcile_candidates||[];
  if(!candidates.length)return;
  if(!options.confirmed&&!await confirmAction({title:`Queue ${candidates.length} safe Reconcile ${candidates.length===1?'job':'jobs'}?`,message:'Each candidate gets a fresh exact plan and confirmation. Changed or ambiguous candidates are skipped; accepted jobs enter the existing shared Move/Reconcile FIFO queue.',details:candidates.slice(0,10).map(item=>[item.torrent_name,`${item.target_pool} · ${item.auxiliary_count} auxiliary files`]),confirmLabel:'Queue safe reconciles'}))return;
  const button=$('#queue-safe-reconciles');
  button.disabled=true;
  startSafeWorkflow(`Queue ${candidates.length} safe Reconcile ${candidates.length===1?'job':'jobs'}`,'Every candidate is freshly authorized before it enters the shared FIFO',[
    {id:'authorize',title:'Revalidate and authorize plans',description:'Issuing a fresh exact single-use confirmation for each candidate.'},
    {id:'enqueue',title:'Add jobs to shared FIFO',description:'Appending authorized Reconcile jobs after all existing Move and Reconcile work.'},
    {id:'refresh',title:'Refresh Queue and Sync',description:'Confirming accepted jobs and updating the audit view.'},
  ]);
  try{
    let queued=0;
    const failures=[];
    const authorized=[];
    for(const [index,candidate] of candidates.entries()){
      const payload={auxiliaryFiles:candidate.auxiliary_files};
      try{
        const confirmation=await api('/api/confirmations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'reconcile',torrentHash:candidate.hash,payload})});
        authorized.push({candidate,payload,confirmation});
      }catch(error){
        failures.push(`${candidate.torrent_name}: ${error.message}`);
      }
      updateSafeWorkflow({stage:'authorize',current:index+1,total:candidates.length,message:`Authorized ${authorized.length}; ${failures.length} skipped · ${candidate.torrent_name}`});
    }
    for(const [index,item] of authorized.entries()){
      try{
        await api('/api/reconcile-queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({torrentHash:item.candidate.hash,...item.payload,confirmationToken:item.confirmation.token})});
        queued+=1;
      }catch(error){
        failures.push(`${item.candidate.torrent_name}: ${error.message}`);
      }
      updateSafeWorkflow({stage:'enqueue',current:index+1,total:authorized.length,message:`Queued ${queued} of ${authorized.length} authorized jobs · ${item.candidate.torrent_name}`});
    }
    updateSafeWorkflow({stage:'refresh',current:0,total:1,message:'Refreshing the shared Queue and Sync audit'});
    await refreshQueue(true,true);
    await runSync({app,throwOnError:true});
    updateSafeWorkflow({stage:'refresh',current:1,total:1,message:`${queued} jobs confirmed in the shared Queue`});
    finishSafeWorkflow(failures.length?`${queued} queued; ${failures.length} skipped safely`:`${queued} safe Reconcile ${queued===1?'job':'jobs'} queued`);
    if(failures.length)toast(`${queued} queued; ${failures.length} skipped after fresh validation`);
    else toast(`${queued} safe Reconcile ${queued===1?'job':'jobs'} added to the shared queue`);
  }catch(error){
    failSafeWorkflow(`${error.message}. Jobs already shown as queued remain in the shared FIFO.`);
    toast(`Queue workflow stopped: ${error.message}`);
    if(button?.isConnected)button.disabled=false;
  }
}
async function runSync(options={}){
  const app=typeof options?.app==='string'?options.app:state.syncApp;
  const active=()=>state.syncApp===app;
  delete state.safeSyncPlans[app];
  if(active()){
    $('#sync-safe-actions').innerHTML='';
    $('#sync-loading').classList.remove('hidden');
    $('#run-sync').disabled=true;
  }
  try{
    const result=await api(`/api/sync/${app}`);
    state.sync[app]=result;
    state.syncExpanded.delete(app);
    if(active())renderSync(result);
    return result;
  }catch(error){
    toast(`Audit failed: ${error.message}`);
    if(options?.throwOnError)throw error;
    return null;
  }finally{
    if(active()){
      $('#sync-loading').classList.add('hidden');
      $('#run-sync').disabled=false;
    }
  }
}
async function viewSyncQueue(publicId){
  navigate('queue');
  await refreshQueue(true);
  const row=document.querySelector(`[data-queue-public-id="${CSS.escape(publicId)}"]`);
  if(row)row.scrollIntoView({behavior:'smooth',block:'center'});
}
async function inspect(hash){navigate('reconcile');$('#torrent-hash').value=hash;$('#global-hash').value=hash;$('#plan-loading').classList.remove('hidden');$('#plan-result').innerHTML='';try{state.plan=await api(`/api/plan/${encodeURIComponent(hash.trim())}`);renderPlan(state.plan)}catch(e){$('#plan-result').innerHTML=`<div class="alert"><div class="alert-head">Request failed</div><p>${esc(e.message)}</p></div>`}finally{$('#plan-loading').classList.add('hidden')}}
async function refreshServiceStatus(){if(!state.authenticated)return;try{state.serviceStatus=await api('/api/status');renderServiceStatus()}catch(e){state.serviceStatus={version:state.config?.version,services:{stowarr_api:{status:'unavailable',error:e.message},qbittorrent:{status:'unavailable',error:e.message},radarr:{status:'unavailable',error:e.message},sonarr:{status:'unavailable',error:e.message}}};renderServiceStatus()}}
async function load(){try{const [config,connections,status,runtime,recovery,operations,queue,reconcileQueue,queueSummary,security,sessions]=await Promise.all([api('/api/config'),api('/api/settings/connections'),api('/api/status'),api('/api/settings/runtime'),api('/api/recovery'),api('/api/operations'),api('/api/queue'),api('/api/reconcile-queue'),api('/api/queue-summary'),api('/api/security/events'),api('/api/auth/sessions')]);state.config=config;state.connections=connections;state.serviceStatus=status;state.runtime=runtime;state.recovery=recovery;state.operations=operations;state.queue=queue;state.reconcileQueue=reconcileQueue;state.queueSummary=queueSummary;state.securityEvents=security.events;state.sessions=sessions.sessions;renderConfig();renderConnections();renderServiceStatus();renderRuntime();renderOperations();renderQueue();renderRecovery();automaticallyTrackRunningQueue();renderSecurity()}catch(e){toast(`Could not load Stowarr: ${e.message}`)}}
async function revokeSessions(){if(!await confirmAction({title:'Sign out all sessions?',message:'Every active WebUI session, including this one, will be revoked.',confirmLabel:'Sign out all',danger:true}))return;try{await api('/api/auth/sessions/revoke',{method:'POST'});state.authenticated=false;state.config=null;showLogin('All WebUI sessions were signed out.')}catch(e){toast(`Sessions were not revoked: ${e.message}`)}}
async function saveRuntime(event){event.preventDefault();const apply=$('#runtime-apply').checked;if(!await confirmAction({title:`${apply?'Enable':'Disable'} confirmed write operations?`,message:'Move and Reconcile still require an explicit plan-bound confirmation.',details:[['Execution mode',apply?'Write mode':'Dry run']],confirmLabel:apply?'Enable writes':'Enable dry run',danger:apply}))return;const button=event.currentTarget.querySelector('button');button.disabled=true;button.textContent='Validating mounts…';try{state.runtime=await api('/api/settings/runtime',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({apply})});state.config.apply=state.runtime.apply;renderRuntime();renderConfig();toast(`Execution mode: ${apply?'Write mode':'Dry run'}`)}catch(e){$('#runtime-apply').checked=state.runtime.apply;toast(`Execution mode was not changed: ${e.message}`)}finally{button.disabled=false;button.textContent='Save execution mode'}}
async function saveConnections(event){event.preventDefault();const form=event.currentTarget;const button=$('#save-connections');const services={qbittorrent:{url:form.elements['qbittorrent-url'].value.trim(),api_key:form.elements['qbittorrent-api-key'].value.trim(),username:form.elements['qbittorrent-username'].value.trim(),password:form.elements['qbittorrent-password'].value},radarr:{url:form.elements['radarr-url'].value.trim(),api_key:form.elements['radarr-api-key'].value.trim()},sonarr:{url:form.elements['sonarr-url'].value.trim(),api_key:form.elements['sonarr-api-key'].value.trim()}};const selected=Object.entries(services).filter(([,service])=>service.url);if(!selected.length){$('#setup-error').textContent='Enter the URL and credentials for at least one service';$('#setup-error').classList.remove('hidden');return}if(!await confirmAction({title:'Test and save configured services?',message:'Existing credentials are kept for blank secret fields. Only services with a URL are tested.',details:selected.map(([name,service])=>[name==='qbittorrent'?'qBittorrent':name[0].toUpperCase()+name.slice(1),service.url]),confirmLabel:'Test and save'}))return;button.disabled=true;button.textContent='Testing connections…';$('#setup-error').classList.add('hidden');try{const result=await api('/api/settings/connections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({services})});state.connections=result;renderConnections();form.elements['qbittorrent-api-key'].value='';form.elements['qbittorrent-password'].value='';form.elements['radarr-api-key'].value='';form.elements['sonarr-api-key'].value='';state.qbitCatalog=null;state.routingAudit=null;await refreshServiceStatus();$('#connection-setup').close();toast(`${selected.length} configured service${selected.length===1?'':'s'} tested and saved`)}catch(e){$('#setup-error').textContent=e.message;$('#setup-error').classList.remove('hidden')}finally{button.disabled=false;button.textContent='Test and save configured services'}}
async function discoverRouting(){const button=$('#discover-routing');button.disabled=true;button.textContent='Discovering…';const target=$('#discovery-result');target.className='loading';target.innerHTML='<span></span>Reading qBittorrent first, then Radarr and Sonarr…';try{const result=await api('/api/settings/discovery');const clients=result.services.flatMap(service=>service.download_clients.map(client=>({...client,app:service.app})));target.className='discovery-grid';target.innerHTML=`<section><header><h3>1 · qBittorrent destinations</h3><span>${result.qbit_categories.length} categories</span></header>${result.qbit_categories.map(item=>`<div class="discovery-row"><strong>${esc(item.category)}</strong><code>${esc(item.save_path||'Default save path')}</code></div>`).join('')||'<div class="empty">No qBittorrent categories found</div>'}</section><section><header><h3>2 · *Arr category senders</h3><span>${clients.length} download clients</span></header>${clients.map(item=>`<div class="discovery-row"><strong>${esc(item.app[0].toUpperCase()+item.app.slice(1))} · ${esc(item.name)}</strong><code>${esc(item.category)}</code><small>Tags: ${item.tags.length?item.tags.map(esc).join(', '):'none'}</small></div>`).join('')||'<div class="empty">No categorized *Arr download clients found</div>'}</section><section><header><h3>3 · Library destinations</h3></header>${result.services.flatMap(service=>service.root_folders.map(path=>`<div class="discovery-row"><strong>${esc(service.app[0].toUpperCase()+service.app.slice(1))}</strong><code>${esc(path)}</code></div>`)).join('')}</section>`}catch(e){target.className='alert inline-alert';target.textContent=e.message}finally{button.disabled=false;button.textContent='Discover routes'}}
function updateAuxiliarySelection(){const selectAll=$('#select-all-auxiliary');if(!selectAll)return;const files=$$('.aux-file:not(:disabled)');const selected=files.filter(input=>input.checked).length;selectAll.checked=Boolean(files.length)&&selected===files.length;selectAll.indeterminate=selected>0&&selected<files.length;const hasWork=reconcilePrimaryNeedsRepair(state.plan)||selected>0;const apply=Boolean(state.config?.apply);const verify=$('#verify-plan');const queue=$('#queue-reconcile');const execute=$('#apply-plan');if(verify)verify.disabled=!hasWork;if(queue)queue.disabled=!apply||!hasWork;if(execute)execute.disabled=!apply||!hasWork}
document.addEventListener('click',e=>{const nav=e.target.closest('[data-page]');if(nav)navigate(nav.dataset.page);const go=e.target.closest('[data-go]');if(go)navigate(go.dataset.go);const tab=e.target.closest('.service-tab');if(tab)setSyncApp(tab.dataset.app);const inspectButton=e.target.closest('.inspect');if(inspectButton)inspect(inspectButton.dataset.hash);const recommendedMove=e.target.closest('#open-recommended-sonarr-move');if(recommendedMove)inspectMove(recommendedMove.dataset.hash,recommendedMove.dataset.pool);const moveButton=e.target.closest('.select-move');if(moveButton)selectMoveTorrent(moveButton.dataset.hash);const columnButton=e.target.closest('.column-toggle');if(columnButton){const key=columnButton.dataset.columnKey;if(state.hiddenMoveColumns.has(key))state.hiddenMoveColumns.delete(key);else state.hiddenMoveColumns.add(key);renderQbitCatalog()}if(e.target.closest('#show-all-columns')){state.hiddenMoveColumns.clear();renderQbitCatalog()}const bulkAction=e.target.closest('.set-extra-action');if(bulkAction)$$('.move-extra-action').forEach(input=>input.value=bulkAction.dataset.action);if(e.target.closest('#verify-plan'))verifyPlan();if(e.target.closest('#apply-plan'))applyPlan();if(e.target.closest('#apply-move'))applyMove();if(e.target.closest('#recheck-release-identity')&&state.movePlan)inspectMove(state.movePlan.torrent_hash,state.movePlan.target_pool);if(e.target.closest('#refresh-qbit'))loadQbitCatalog(true);if(e.target.closest('#discover-routing'))discoverRouting();if(e.target.closest('#open-routing-guide'))$('#routing-guide').showModal();if(e.target.closest('#close-routing-guide'))$('#routing-guide').close();if(e.target.closest('#open-connection-setup'))$('#connection-setup').showModal();if(e.target.closest('#close-connection-setup'))$('#connection-setup').close()});$('#login-dialog').addEventListener('cancel',e=>e.preventDefault());$('#login-form').addEventListener('submit',login);$('#logout').addEventListener('click',logout);$('#password-form').addEventListener('submit',changePassword);$('#run-sync').addEventListener('click',runSync);$('#plan-form').addEventListener('submit',e=>{e.preventDefault();const hash=$('#torrent-hash').value.trim();if(hash)inspect(hash);else toast('Enter a torrent hash')});$('#move-search').addEventListener('input',renderQbitCatalog);$('#move-form').addEventListener('submit',e=>{e.preventDefault();const hash=$('#move-hash').value.trim();const target=$('#move-target').value;if(hash&&target)inspectMove(hash,target);else toast('Select a torrent and destination pool')});$('#global-hash').addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.value.trim())inspect(e.target.value)});$('#refresh').addEventListener('click',()=>{load();if(location.hash==='#move')loadQbitCatalog(true);toast('Refreshed')});$('#menu').addEventListener('click',()=>$('.sidebar').classList.toggle('open'));$('#sidebar-backdrop').addEventListener('click',()=>$('.sidebar').classList.remove('open'));navigate(location.hash.slice(1)||'sync');setSyncApp('radarr');bootstrap();
document.addEventListener('change',e=>{if(e.target.id==='move-target')invalidateMovePlan();else if(e.target.id==='select-all-auxiliary'){$$('.aux-file:not(:disabled)').forEach(input=>input.checked=e.target.checked);updateAuxiliarySelection()}else if(e.target.classList.contains('aux-file'))updateAuxiliarySelection();else if(e.target.id==='select-all-history'){const terminal=state.operations.filter(op=>['COMPLETE','FAILED','BLOCKED','DRY_RUN'].includes(op.state));state.selectedHistory=e.target.checked?new Set(terminal.map(op=>op.id)):new Set();renderOperations()}else if(e.target.classList.contains('history-select')){const id=Number(e.target.dataset.operationId);if(e.target.checked)state.selectedHistory.add(id);else state.selectedHistory.delete(id);renderOperations()}});
document.addEventListener('click',e=>{const details=e.target.closest('.inspect-operation');if(details)openOperationDetails(details.dataset.operationId);if(e.target.closest('#delete-history-selected'))deleteHistory(false);if(e.target.closest('#clear-history'))deleteHistory(true);if(e.target.closest('#operation-minimized'))showOperationTracking();if(e.target.closest('#operation-done')||e.target.closest('#operation-close')){if(state.operationTracking&&!terminalOperation(state.currentOperation))hideOperationTracking();else finishOperationTracking()}});
$('#operation-dialog').addEventListener('cancel',e=>{e.preventDefault();if(state.operationTracking&&!terminalOperation(state.currentOperation))hideOperationTracking();else if(!$('#operation-done').disabled)finishOperationTracking()});
$('#safe-workflow-dialog').addEventListener('cancel',e=>{e.preventDefault();if(state.safeWorkflow?.terminal&&!state.safeWorkflow?.confirmation)closeSafeWorkflow();else hideSafeWorkflow()});
$('#safe-workflow-close').addEventListener('click',()=>{if(state.safeWorkflow?.terminal&&!state.safeWorkflow?.confirmation)closeSafeWorkflow();else hideSafeWorkflow()});
$('#safe-workflow-cancel').addEventListener('click',closeSafeWorkflow);
$('#safe-workflow-done').addEventListener('click',continueSafeWorkflow);
$('#safe-workflow-minimized').addEventListener('click',showSafeWorkflow);
$('#connections-form').addEventListener('submit',saveConnections);
document.addEventListener('click',event=>{
  if(event.target.closest('#open-routing-guide'))refreshRoutingGuide()
});
$('#routing-guide').addEventListener('click',event=>{
  const dialog=event.currentTarget;
  const bounds=dialog.getBoundingClientRect();
  const outside=event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom;
  if(outside)dialog.close()
});
$('#runtime-form').addEventListener('submit',saveRuntime);
$('#revoke-sessions').addEventListener('click',revokeSessions);
document.addEventListener('click',event=>{const button=event.target.closest('.set-extra-action');if(button?.dataset.action==='move')$$('.move-extra-action').filter(input=>input.options[0].disabled).forEach(input=>input.value='delete')});
document.addEventListener('click',event=>{
  if(event.target.closest('#plan-safe-sync'))planSafeSyncFixes();
  if(event.target.closest('#apply-safe-categories'))applySafeCategories();
  if(event.target.closest('#queue-safe-reconciles'))queueSafeReconciles();
  const statusFilter=event.target.closest('.sync-status-filter');
  if(statusFilter){
    const hidden=state.syncHiddenStatuses[state.syncApp];
    const status=statusFilter.dataset.status;
    if(hidden.has(status))hidden.delete(status);else hidden.add(status);
    if(state.sync[state.syncApp])renderSync(state.sync[state.syncApp]);
  }
  if(event.target.closest('#sync-issues-only')){
    const hidden=state.syncHiddenStatuses[state.syncApp];
    hidden.clear();
    hidden.add('in-sync');
    if(state.sync[state.syncApp])renderSync(state.sync[state.syncApp]);
  }
  if(event.target.closest('#sync-show-all')){
    state.syncHiddenStatuses[state.syncApp].clear();
    if(state.sync[state.syncApp])renderSync(state.sync[state.syncApp]);
  }
  const categoryRepair=event.target.closest('.repair-sync-category');
  if(categoryRepair)repairSyncCategory(categoryRepair);
  const queuedSync=event.target.closest('.view-sync-queue');
  if(queuedSync)viewSyncQueue(queuedSync.dataset.publicId);
  const syncGroup=event.target.closest('.sync-group-toggle');
  if(syncGroup){
    const app=syncGroup.dataset.syncGroup;
    if(state.syncExpanded.has(app))state.syncExpanded.delete(app);else state.syncExpanded.add(app);
    if(state.sync[app])renderSync(state.sync[app]);
  }
  const liveOperation=event.target.closest('.track-operation');
  if(liveOperation)trackOperationByPublicId(liveOperation.dataset.publicId,liveOperation.dataset.kind);
  const clear=event.target.closest('.clear-queue');
  if(clear)clearQueue(clear.dataset.kind);
  if(event.target.closest('#queue-reconcile'))enqueueReconcile();
  if(event.target.closest('#queue-move'))enqueueMove();
  if(event.target.closest('#refresh-queue'))refreshQueue();
  const diagnose=event.target.closest('.diagnose-recovery');
  if(diagnose)diagnoseRecovery(diagnose.dataset.publicId);
  const resolve=event.target.closest('.resolve-recovery');
  if(resolve)resolveRecovery(resolve.dataset.publicId);
  const recoveryPreset=event.target.closest('.recovery-note-preset');
  if(recoveryPreset){
    const input=$(`.recovery-note[data-public-id="${CSS.escape(recoveryPreset.dataset.publicId)}"]`);
    if(input){
      input.value='qBittorrent force recheck passed';
      input.focus();
    }
  }
  const cancel=event.target.closest('.cancel-queue');
  if(cancel)cancelQueue(cancel.dataset.queueId,cancel.dataset.kind);
});
setInterval(refreshOperations,5000);
setInterval(()=>refreshQueue(true),3000);
setInterval(refreshServiceStatus,30000);
