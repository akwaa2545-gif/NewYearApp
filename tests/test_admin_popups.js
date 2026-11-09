const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
const ids = ['adminEmployeeDialog','adminActivityDialog','adminAttendanceDialog','adminDrawDialog'];
const removedCheckinIds = new Set(['adminAttendanceConfirm','adminConfirmWord']);

function fixture(respond = async () => ({removed:1}), native = true, initialize = true) {
  const nodes = new Map(), requests = [], listeners = new Map();
  const document = {activeElement:null, body:{dataset:{},style:{}}, hidden:false,
    getElementById:id => removedCheckinIds.has(id) ? null : node(id),
    querySelectorAll(selector) {
      if (selector === '[data-admin-dialog]') return ids.map(id => node(id+'Open'));
      if (selector === '.admin-dialog') return ids.map(node);
      if (selector === '[data-admin-close]') return ids.map(id => node(id+'Close'));
      return [];
    },
    addEventListener(type, fn, capture) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    }};
  function node(id) {
    assert.equal(removedCheckinIds.has(id), false, 'the check-in confirmation field no longer exists');
    if (nodes.has(id)) return nodes.get(id);
    const attrs = new Map(), events = new Map(), classes = new Set();
    const value = {id, value:'', textContent:'', hidden:false, disabled:false, readOnly:false,
      dataset:{}, style:{}, tagName:'INPUT', open:false,
      classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),toggle() {}},
      setAttribute(name, value) {attrs.set(name, String(value)); if(name==='open') this.open=true;},
      getAttribute:name => attrs.get(name) ?? null, hasAttribute:name=>attrs.has(name),
      removeAttribute(name) {attrs.delete(name); if(name==='open') this.open=false;},
      focus() {document.activeElement=this; this.focusCount=(this.focusCount||0)+1;},
      addEventListener(type,fn) {events.set(type,fn);},
      emit(type,extra={}) {
        const e={target:this,currentTarget:this,preventDefault(){this.defaultPrevented=true;},stopPropagation(){this.stopped=true;},...extra};
        const result=events.get(type)?.(e); return {event:e,result};
      },
      querySelector(selector) {
        if (selector === '[data-admin-close]') return node(id+'Close');
        return null;
      },
      querySelectorAll() {return this.fields || [];},
      closest(selector) {return selector === '.admin-dialog' ? this.dialog || null : null;},
      contains(target) {return target===this || this.fields?.includes(target);},
      reportValidity:()=>true,
      reset() {this.resets=(this.resets||0)+1; for(const field of this.fields || []) field.value='';},
      append() {}, replaceChildren() {},
    };
    nodes.set(id,value); return value;
  }
  const groups = {
    adminEmployeeDialog:['adminAddEmployeeId','adminAddEmployeeName','adminAddEmployeeDepartment','adminAddEmployeePosition','adminAddEmployeeDraw','adminAddEmployeeSubmit'],
    adminActivityDialog:[],
    adminAttendanceDialog:['adminAttendanceScope','adminEmployeeId','adminAttendanceReason','adminAttendanceSubmit'],
    adminDrawDialog:['adminDrawReason','adminDrawConfirm','adminDrawSubmit'],
  };
  for(const id of ids) {
    const dialog=node(id); dialog.tagName='DIALOG'; dialog.fields=[node(id+'Close'),...groups[id].map(node)];
    for(const field of dialog.fields) field.dialog=dialog;
    node(id+'Close').tagName='BUTTON';
    node(id+'Open').dataset.adminDialog=id; node(id+'Open').tagName='BUTTON';
    node(id+'Title').tagName='H2'; node(id+'Title').dialog=dialog;
    if(native) {
      dialog.showModal=()=>{dialog.open=true; dialog.shows=(dialog.shows||0)+1;};
      dialog.close=()=>{dialog.open=false; dialog.emit('close');};
    }
  }
  for(const [form,id] of [['adminAttendanceForm','adminAttendanceDialog'],['adminDrawForm','adminDrawDialog'],['adminAddEmployeeForm','adminEmployeeDialog']]) {
    node(form).fields=node(id).fields.filter(n=>!n.id.endsWith('Close')); node(form).dialog=node(id);
  }
  node('adminAttendanceScope').value='all'; node('adminAttendanceScope').tagName='SELECT';
  node('adminAddEmployeeDraw').value='yes'; node('adminAddEmployeeDraw').tagName='SELECT';
  for(const id of ['adminAttendanceSubmit','adminDrawSubmit','adminAddEmployeeSubmit']) node(id).tagName='BUTTON';
  node('adminDialogScrim').hidden=true;
  const context=vm.createContext({document,window:{scrollTo(){},addEventListener(){}},console,
    history:{replaceState(){}},location:{hash:'#/admin'},setTimeout:()=>0,setInterval:()=>0,
    request:async(path,options)=>{requests.push({path,data:options?.data}); return respond(path,options?.data);}});
  vm.runInContext(fs.readFileSync('static/wristband-support.js', 'utf8'),context);
  vm.runInContext(source,context);
  vm.runInContext("globalThis.refreshAdminFromSource=refreshAdmin; api=request; refreshAdmin=async()=>{}; refreshStats=async()=>{}; state.profile={role:'admin'}; state.view='admin'; state.adminChecked=4; state.adminDrawCount=2;",context);
  if(initialize) vm.runInContext('initializeAdminDialogs()',context);
  const run=code=>vm.runInContext(code,context);
  function docEvent(type,target,extra={}) {
    const event={type,target,preventDefault(){this.defaultPrevented=true;},stopPropagation(){this.stopped=true;},...extra};
    for(const fn of listeners.get(type)||[]) fn(event); return event;
  }
  return {node,run,document,requests,docEvent,
    open(id){node(id+'Open').focus(); return node(id+'Open').emit('click');},
    submit(kind) {context.submitEvent={preventDefault(){},currentTarget:node(kind==='attendance'?'adminAttendanceForm':'adminDrawForm')}; return run(kind==='attendance'?'removeAdminAttendance(submitEvent)':'removeAdminDrawHistory(submitEvent)');},
    flush:async()=>{for(let i=0;i<8;i++) await Promise.resolve();}};
}

test('each Admin action opens only its popup without making a mutation', () => {
  const f=fixture();
  for(const id of ids) {
    f.open(id);
    assert.equal(f.node(id).open,true);
    assert.equal(ids.filter(other=>f.node(other).open).length,1);
  }
  assert.equal(f.requests.length,0);
});

test('popups require Admin view and role and return focus on Close and Escape', () => {
  const f=fixture(); f.run("state.profile={role:'staff'}"); f.open(ids[0]); assert.equal(f.node(ids[0]).open,false);
  f.run("state.profile={role:'admin'}; state.view='home'"); f.open(ids[0]); assert.equal(f.node(ids[0]).open,false);
  f.run("state.view='admin'"); f.open(ids[0]);
  assert.equal(f.document.activeElement,f.node('adminAddEmployeeId'));
  f.node(ids[0]+'Close').emit('click');
  assert.equal(f.node(ids[0]).open,false); assert.equal(f.document.activeElement,f.node(ids[0]+'Open'));
  f.open(ids[2]); f.node(ids[2]).emit('cancel');
  assert.equal(f.node(ids[2]).open,false); assert.equal(f.document.activeElement,f.node(ids[2]+'Open'));
});

test('check-in removal has no typed confirmation, while winner clearing still requires it', () => {
  const html=fs.readFileSync('static/index.html','utf8');
  assert.equal(/id=["'](?:adminAttendanceConfirm|adminConfirmWord)["']/.test(html),false,
    'check-in reset must not show a typed confirmation field');
  assert.match(html,/id=["']adminDrawConfirm["']/);
  const f=fixture(); f.open(ids[2]);
  assert.equal(f.document.activeElement,f.node('adminAttendanceScope'));
  f.node('adminDrawConfirm').value='RESET DRAW'; f.open(ids[3]);
  assert.equal(f.node('adminDrawConfirm').value,'');
  assert.equal(f.document.activeElement,f.node('adminDrawReason'));
});

test('recent logins opens with a focusable Close button in native and fallback dialogs',()=>{
  for(const native of [true,false]) {
    const f=fixture(undefined,native); f.open(ids[1]);
    assert.equal(f.document.activeElement,f.node(ids[1]+'Close'));
    assert.equal(f.document.activeElement.tagName,'BUTTON');
    if(!native) assert.equal(f.node(ids[1]).getAttribute('role'),'dialog');
  }
});

test('attendance save is single flight, blocks Close and Escape, and leaves success visible',async()=>{
  let finish; const f=fixture(()=>new Promise(resolve=>{finish=resolve;})); f.open(ids[2]);
  f.node('adminAttendanceReason').value='Correction';
  f.node('adminAttendanceSubmit').focus(); const saving=f.submit('attendance'); await f.submit('attendance');
  assert.equal(f.requests.length,1);
  assert.deepEqual(JSON.parse(JSON.stringify(f.requests[0].data)),{scope:'all',employee_id:'',reason:'Correction',confirmation:'RESET ALL',expected_count:4});
  assert.equal(f.node('adminAttendanceSubmit').disabled,false);
  assert.equal(f.node('adminAttendanceSubmit').getAttribute('aria-disabled'),'true');
  assert.equal(f.node('adminAttendanceReason').readOnly,true);
  f.node(ids[2]+'Close').emit('click'); const escape=f.node(ids[2]).emit('cancel');
  assert.equal(escape.event.defaultPrevented,true); assert.equal(f.node(ids[2]).open,true);
  f.open(ids[0]); assert.equal(f.node(ids[0]).open,false);
  finish({removed:4}); await saving;
  assert.equal(f.node(ids[2]).open,true); assert.equal(f.node('adminAttendanceReason').readOnly,false);
  assert.match(f.node('adminAttendanceMessage').textContent,/4 check-ins removed/);
});

test('one employee check-in can be removed with a reason and no typed confirmation',async()=>{
  const f=fixture(); f.open(ids[2]); f.node('adminAttendanceScope').value='one';
  f.run('updateAttendanceScope()');
  assert.equal(f.node('adminEmployeeId').required,true);
  f.node('adminEmployeeId').value='2205529'; f.node('adminAttendanceReason').value='Correction';
  await f.submit('attendance');
  assert.deepEqual(JSON.parse(JSON.stringify(f.requests[0].data)),{
    scope:'one',employee_id:'2205529',reason:'Correction',confirmation:'REMOVE',expected_count:4,
  });
  assert.match(f.node('adminAttendanceMessage').textContent,/1 check-in removed/);
});

test('changing attendance counts still updates the removal snapshot without a confirmation field',async()=>{
  const f=fixture(async path=>{
    if(path==='/api/stats') return {checked_in:5,total_employees:10};
    if(path==='/api/admin/activity') return {items:[]};
    if(path==='/api/admin/draw-history') return {count:2};
    return {removed:5};
  });
  f.open(ids[2]); f.run('refreshAdmin=refreshAdminFromSource'); await f.run('refreshAdmin()');
  assert.equal(f.run('state.adminChecked'),5);
  f.node('adminAttendanceReason').value='Test reset'; await f.submit('attendance'); await f.flush();
  const requests=f.requests.filter(request=>request.path==='/api/admin/attendance/remove');
  assert.equal(requests.length,1);
  assert.equal(requests[0].data.expected_count,5);
  assert.equal(requests[0].data.confirmation,'RESET ALL');
  assert.match(f.node('adminAttendanceMessage').textContent,/5 check-ins removed/);
});

test('winner clear keeps confirmation rules, preserves failures, and permits explicit retry',async()=>{
  const f=fixture(async()=>{throw new Error('Type RESET DRAW');}); f.open(ids[3]);
  f.node('adminDrawReason').value='Rehearsal'; f.node('adminDrawConfirm').value='wrong';
  await f.submit('draw');
  assert.equal(f.requests[0].data.confirmation,'wrong'); assert.equal(f.requests[0].data.expected_count,2);
  assert.equal(f.node('adminDrawReason').value,'Rehearsal'); assert.equal(f.node(ids[3]).open,true);
  assert.equal(f.node('adminDrawMessage').textContent,'Type RESET DRAW');
  await f.submit('draw'); assert.equal(f.requests.length,2);
});

test('saved removal remains a success when totals refresh fails',async()=>{
  const f=fixture(); f.open(ids[3]); f.run("refreshAdmin=async()=>{throw new Error('Offline');}; refreshStats=async()=>{throw new Error('Offline');}");
  await f.submit('draw'); await f.flush();
  assert.match(f.node('adminDrawMessage').textContent,/removed/); assert.equal(f.node('adminDrawForm').resets,1);
});

test('leaving Admin closes and detaches a pending removal from the next popup',async()=>{
  let finish; const f=fixture(()=>new Promise(resolve=>{finish=resolve;})); f.open(ids[2]);
  const old=f.submit('attendance'); f.run("goView('home')");
  assert.equal(f.node(ids[2]).open,false); assert.equal(f.node('adminAttendanceReason').readOnly,false);
  f.run("state.view='admin'"); f.open(ids[2]); f.node('adminAttendanceMessage').textContent='New review';
  finish({removed:4}); await old;
  assert.equal(f.node('adminAttendanceMessage').textContent,'New review'); assert.equal(f.node('adminAttendanceForm').resets,undefined);
});

test('logging out prevents a late removal error from replacing a new session message',async()=>{
  let fail; const f=fixture(()=>new Promise((_r,reject)=>{fail=reject;})); f.open(ids[3]);
  const old=f.submit('draw'); f.run('showLogin()'); f.node('adminDrawMessage').textContent='New session';
  fail(new Error('Old response')); await old;
  assert.equal(f.node(ids[3]).open,false); assert.equal(f.node('adminDrawMessage').textContent,'New session');
});

test('adding an employee also prevents popup dismissal until its explicit save finishes',()=>{
  const f=fixture(); f.open(ids[0]); f.run('state.adminEmployeeOperation={}');
  f.node(ids[0]+'Close').emit('click'); f.node(ids[0]).emit('cancel'); assert.equal(f.node(ids[0]).open,true);
  f.run('state.adminEmployeeOperation=null'); f.node(ids[0]+'Close').emit('click'); assert.equal(f.node(ids[0]).open,false);
});

test('fallback popups show scrim, trap focus, block outside clicks, and restore their layer',()=>{
  const f=fixture(undefined,false); f.node('view-admin').style.zIndex='4'; f.open(ids[0]);
  assert.equal(f.node('adminDialogScrim').hidden,false); assert.equal(f.node(ids[0]).hasAttribute('open'),true);
  const fields=f.node(ids[0]).fields;
  fields.at(-1).focus(); let key=f.docEvent('keydown',fields.at(-1),{key:'Tab',shiftKey:false});
  assert.equal(key.defaultPrevented,true); assert.equal(f.document.activeElement,fields[0]);
  fields[0].focus(); f.docEvent('keydown',fields[0],{key:'Tab',shiftKey:true}); assert.equal(f.document.activeElement,fields.at(-1));
  const outside=f.docEvent('click',f.node('dangerOutside'));
  assert.equal(outside.defaultPrevented,true); assert.equal(outside.stopped,true);
  f.docEvent('focusin',f.node('dangerOutside')); assert.equal(f.node(ids[0]).contains(f.document.activeElement),true);
  f.docEvent('keydown',f.document.activeElement,{key:'Escape'});
  assert.equal(f.node(ids[0]).open,false); assert.equal(f.node('adminDialogScrim').hidden,true);
  assert.equal(f.node('view-admin').style.zIndex,'4');
});

test('an old completion cannot unlock a newer removal after forced navigation',async()=>{
  const finishes=[]; const f=fixture(()=>new Promise(resolve=>finishes.push(resolve)));
  f.open(ids[2]); const old=f.submit('attendance'); f.run("goView('home'); state.view='admin'");
  f.open(ids[2]); const newer=f.submit('attendance');
  finishes[0]({removed:4}); await old;
  assert.equal(f.node('adminAttendanceReason').readOnly,true);
  assert.equal(f.node('adminAttendanceSubmit').getAttribute('aria-disabled'),'true');
  assert.equal(f.node(ids[2]+'Close').getAttribute('aria-disabled'),'true');
  f.node(ids[2]).emit('cancel'); assert.equal(f.node(ids[2]).open,true);
  finishes[1]({removed:1}); await newer;
  assert.equal(f.node('adminAttendanceReason').readOnly,false);
  assert.equal(f.node(ids[2]+'Close').getAttribute('aria-disabled'),'false');
});

test('direct non-admin removals never submit API requests',async()=>{
  const f=fixture(); f.run("state.profile={role:'staff'}");
  await f.submit('attendance'); await f.submit('draw'); assert.equal(f.requests.length,0);
});

test('native close events restore opener focus once, and disconnected triggers are ignored',()=>{
  const f=fixture(); f.open(ids[1]); const opener=f.node(ids[1]+'Open'); const before=opener.focusCount;
  f.node(ids[1]).close(); assert.equal(opener.focusCount,before+1);
  f.node(ids[1]).emit('close'); assert.equal(opener.focusCount,before+1);
  f.open(ids[0]); f.node(ids[0]+'Open').isConnected=false; f.node(ids[0]+'Close').emit('click');
  assert.notEqual(f.document.activeElement,f.node(ids[0]+'Open'));
});

test('a queued native close event cannot close a popup that has already reopened',()=>{
  const f=fixture(); f.open(ids[0]); f.node(ids[0]+'Close').emit('click'); f.open(ids[0]);
  f.node(ids[0]).emit('close');
  assert.equal(f.node(ids[0]).open,true); assert.equal(f.document.activeElement,f.node('adminAddEmployeeId'));
});

test('fallback background semantics and original attributes are restored exactly',()=>{
  const f=fixture(undefined,false), view=f.node('view-admin'), app=f.node('appView'), header=f.node('header');
  const menu=f.node('menu'), footer=f.node('footer');
  menu.setAttribute('aria-hidden','false'); header.setAttribute('inert','kept');
  view.children=[menu,f.node('adminDialogScrim'),...ids.map(f.node)];
  for(const dialog of ids.map(f.node)) dialog.parentElement=view;
  view.parentElement=app; app.children=[header,view]; app.parentElement=f.document.body;
  f.document.body.children=[app,footer];
  f.open(ids[0]);
  for(const sibling of [menu,header,footer]) {
    assert.equal(sibling.getAttribute('aria-hidden'),'true'); assert.equal(sibling.hasAttribute('inert'),true);
  }
  assert.equal(f.node(ids[0]).getAttribute('aria-modal'),'true');
  f.node(ids[0]+'Close').emit('click');
  assert.equal(menu.getAttribute('aria-hidden'),'false'); assert.equal(menu.hasAttribute('inert'),false);
  assert.equal(header.getAttribute('aria-hidden'),null); assert.equal(header.getAttribute('inert'),'kept');
  assert.equal(footer.getAttribute('aria-hidden'),null); assert.equal(footer.hasAttribute('inert'),false);
});

test('fallback Tab ignores hidden and disabled controls',()=>{
  const f=fixture(undefined,false); f.open(ids[0]);
  const fields=f.node(ids[0]).fields; fields.at(-1).disabled=true; fields[1].hidden=true;
  fields.at(-2).focus(); f.docEvent('keydown',fields.at(-2),{key:'Tab'});
  assert.equal(f.document.activeElement,fields[0]);
  fields[0].focus(); f.docEvent('keydown',fields[0],{key:'Tab',shiftKey:true});
  assert.equal(f.document.activeElement,fields.at(-2));
});

test('fallback keyboard navigation works in browsers without Array.at',()=>{
  const f=fixture(undefined,false); f.run('Array.prototype.at=undefined'); f.open(ids[0]);
  const fields=f.node(ids[0]).fields, last=fields[fields.length-1];
  last.focus(); f.docEvent('keydown',last,{key:'Tab'});
  assert.equal(f.document.activeElement,fields[0]);
  fields[0].focus(); f.docEvent('keydown',fields[0],{key:'Tab',shiftKey:true});
  assert.equal(f.document.activeElement,last);
});

test('application initialization wires popup launchers and their Close controls',async()=>{
  const f=fixture(undefined,true,false);
  f.run("createWorkspace=()=>{}; updateNetwork=()=>{}; showApp=()=>{}; api=async()=>({authenticated:true,role:'admin'});");
  await f.run('initialize()'); f.open(ids[0]);
  assert.equal(f.node(ids[0]).open,true); f.node(ids[0]+'Close').emit('click');
  assert.equal(f.node(ids[0]).open,false);
});
