const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {pathToFileURL} = require('node:url');
const {resolve} = require('node:path');

const helperSource = fs.readFileSync('static/wristband-support.js', 'utf8');
const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
const employee = (id='1234567', checked=true, reason=null) => ({
  employee_id:id, name:`Employee ${id}`, department:'IT',
  checked_in_at:checked ? '2026-11-06T08:00:00Z' : null, checkin_wristband_reason:reason,
});

function fixture(respond = (path, data) => ({person:employee(data.employee_id, true,
  path === '/api/checkin/wristband-lost' ? 'lost' : null), state:'updated'})) {
  const elements = new Map(), requests = [];
  const element = id => {
    if (!elements.has(id)) {
      const attributes = new Map();
      const node = {id, value:'', hidden:false, readOnly:false,
        textContent:'', className:'', dataset:{}, style:{}, classList:{toggle() {}},
        setAttribute(name,value) {attributes.set(name,String(value));},
        getAttribute:name => attributes.get(name) ?? null,
        removeAttribute:name => attributes.delete(name),
        focus() {document.activeElement=node;}, reset() {},
        addEventListener() {}, replaceChildren() {}};
      let disabled = false;
      Object.defineProperty(node, 'disabled', {get:()=>disabled, set(value) {
        disabled = value;
        if (value && document.activeElement === node) document.activeElement = null;
      }});
      elements.set(id,node);
    }
    return elements.get(id);
  };
  const document = {body:{dataset:{}}, hidden:false, getElementById:element,
    querySelectorAll:() => [], querySelector:() => null, activeElement:null};
  const context = vm.createContext({document, console, window:{scrollTo() {}},
    location:{hash:'#/wristband'}, history:{replaceState() {}},
    request:async (path, options) => {
      const data = options?.data;
      requests.push({path, data});
      return respond(path, data);
    },
  });
  vm.runInContext(helperSource, context, {filename:pathToFileURL(resolve('static/wristband-support.js')).href});
  vm.runInContext(source, context, {filename:'static/app.js'});
  vm.runInContext(`api=request; refreshStats=async()=>{};
    state.profile={role:'staff',csrf:'test'}; state.view='wristband';
    globalThis.appState=state;`, context);
  element('wristbandEmployeeId').value = '1234567';
  return {element, document, requests, state:context.appState,
    run:code => vm.runInContext(code, context),
    load:() => vm.runInContext('loadWristbandEmployee({preventDefault(){}})', context),
    mark:() => vm.runInContext('markWristbandLost()', context)};
}

test('wristband support looks up an employee without changing attendance', async () => {
  const f = fixture();
  await f.load();
  assert.deepEqual(f.requests.map(r=>r.path), ['/api/lookup']);
  assert.equal(f.requests[0].data.employee_id, '1234567');
  assert.equal(f.element('wristbandResult').hidden, false);
  assert.match(f.element('wristbandEmployeeName').textContent, /Employee 1234567/);
  assert.match(f.element('wristbandEmployeeMeta').textContent, /1234567.*IT|IT.*1234567/);
  assert.equal(f.element('wristbandLost').hidden, false);
});

for (const [description, checked, reason] of [
  ['not yet checked in', false, null], ['already marked lost', true, 'lost'],
]) {
  test(`wristband support cannot mark an employee who is ${description}`, async () => {
    const f = fixture((_path, data) => ({person:employee(data.employee_id, checked, reason)}));
    await f.load();
    assert.equal(f.element('wristbandLost').hidden, true);
    await f.mark();
    assert.deepEqual(f.requests.map(r=>r.path), ['/api/lookup']);
  });
}

test('an empty employee ID never makes a lookup or mutation request', async () => {
  const f = fixture(); f.element('wristbandEmployeeId').value = '  ';
  await f.load(); await f.mark();
  assert.equal(f.requests.length, 0);
  assert.equal(f.state.wristbandPerson, null);
  assert.match(f.element('wristbandMessage').textContent, /employee|ID|enter/i);
});

test('marking a lost wristband requires its separate explicit action and is single-flight', async () => {
  let finish;
  const f = fixture((path,data) => path === '/api/lookup' ? {person:employee(data.employee_id)} :
    new Promise(resolve=>{finish=resolve;}));
  await f.load();
  const saving = f.mark();
  await f.mark();
  assert.equal(f.state.wristbandSaving, true);
  assert.equal(f.element('wristbandEmployeeId').readOnly, true);
  assert.equal(f.element('wristbandEmployeeId').disabled, false);
  assert.deepEqual(f.requests.map(r=>r.path), ['/api/lookup','/api/checkin/wristband-lost']);
  assert.equal(f.requests[1].data.employee_id, '1234567');
  await f.load();
  assert.equal(f.requests.length, 2, 'a lookup cannot replace a pending correction');
  finish({person:employee('1234567',true,'lost'),state:'updated'});
  await saving;
  assert.equal(f.state.wristbandSaving, false);
  assert.equal(f.state.wristbandPerson.checkin_wristband_reason, 'lost');
  assert.equal(f.element('wristbandLost').hidden, true);
  assert.match(f.element('wristbandMessage').textContent, /lost|saved|updated/i);
  assert.equal(f.requests.filter(r=>r.path === '/api/scan/checkin').length, 0);
});

test('a failed correction keeps the employee available for retry', async () => {
  let attempts = 0;
  const f = fixture((path,data) => {
    if (path === '/api/lookup') return {person:employee(data.employee_id)};
    if (++attempts === 1) throw new Error('Cannot reach the event server.');
    return {person:employee(data.employee_id,true,'lost'),state:'updated'};
  });
  await f.load(); await f.mark();
  assert.equal(f.state.wristbandSaving, false);
  assert.equal(f.state.wristbandPerson.employee_id, '1234567');
  assert.equal(f.element('wristbandLost').hidden, false);
  assert.equal(f.element('wristbandEmployeeId').disabled, false);
  assert.match(f.element('wristbandMessage').textContent, /Cannot reach/);
  await f.mark();
  assert.equal(attempts, 2);
  assert.equal(f.element('wristbandLost').hidden, true);
});

test('changing the ID invalidates the loaded employee before another correction', async () => {
  const f = fixture(); await f.load();
  f.element('wristbandEmployeeId').value = '7654321';
  f.run('resetWristbandSupport(false)');
  assert.equal(f.element('wristbandEmployeeId').value, '7654321');
  assert.equal(f.state.wristbandPerson, null);
  assert.equal(f.element('wristbandResult').hidden, true);
  await f.mark();
  assert.deepEqual(f.requests.map(r=>r.path), ['/api/lookup']);
});

test('a lookup failure clears an earlier employee and offers no stale mutation', async () => {
  let lookups = 0;
  const f = fixture((_path,data) => {
    if (++lookups > 1) throw new Error('Employee not found.');
    return {person:employee(data.employee_id)};
  });
  await f.load();
  f.element('wristbandEmployeeId').value = 'missing';
  await f.load(); await f.mark();
  assert.equal(f.state.wristbandPerson, null);
  assert.equal(f.element('wristbandResult').hidden, true);
  assert.match(f.element('wristbandMessage').textContent, /not found/i);
  assert.equal(f.requests.length, 2);
});

test('a lookup finishing after leaving wristband support cannot display an old employee', async () => {
  let finish;
  const f = fixture(()=>new Promise(resolve=>{finish=resolve;}));
  const pending = f.load();
  f.run("goView('home')");
  finish({person:employee()}); await pending;
  assert.equal(f.state.view, 'home');
  assert.equal(f.state.wristbandPerson, null);
  assert.equal(f.element('wristbandResult').hidden, true);
});

test('the correction API cannot be invoked from a different menu', async () => {
  const f = fixture(); await f.load();
  f.run("state.view='checkin'");
  await f.mark(); await f.load();
  assert.deepEqual(f.requests.map(r=>r.path), ['/api/lookup']);
});

for (const role of ['admin','staff','checkin_staff','verification_staff']) {
  test(`wristband menu access respects the ${role} role`, () => {
    const f = fixture();
    f.run(`state.profile.role=${JSON.stringify(role)};goView('wristband')`);
    assert.equal(f.state.view, role === 'verification_staff' ? 'home' : 'wristband');
  });
}


test('a detached correction cannot unlock or overwrite a newer correction', async () => {
  const completions = new Map();
  const f = fixture((path,data) => path === '/api/lookup' ? {person:employee(data.employee_id)} :
    new Promise(resolve=>completions.set(data.employee_id,resolve)));
  await f.load(); const first = f.mark();
  f.run('resetWristbandSupport(false)');
  f.element('wristbandEmployeeId').value = '7654321';
  await f.load(); const second = f.mark();
  completions.get('1234567')({person:employee('1234567',true,'lost'),state:'updated'});
  await first;
  assert.equal(f.state.wristbandSaving, true);
  assert.equal(f.state.wristbandPerson.employee_id, '7654321');
  await f.mark();
  assert.equal(f.requests.filter(r=>r.path === '/api/checkin/wristband-lost').length, 2);
  completions.get('7654321')({person:employee('7654321',true,'lost'),state:'updated'});
  await second;
  assert.equal(f.state.wristbandSaving, false);
  assert.equal(f.state.wristbandPerson.employee_id, '7654321');
  assert.equal(f.state.wristbandPerson.checkin_wristband_reason, 'lost');
});


test('direct ID edits cannot mark the previously loaded employee', async () => {
  const f = fixture(); await f.load();
  f.element('wristbandEmployeeId').value = '7654321';
  await f.mark();
  assert.equal(f.state.wristbandPerson, null);
  assert.equal(f.element('wristbandEmployeeId').value, '7654321');
  assert.deepEqual(f.requests.map(r=>r.path), ['/api/lookup']);
  assert.match(f.element('wristbandMessage').textContent, /find|again/i);
});

test('repeated Find during a pending lookup stays single-flight', async () => {
  let finish;
  const f = fixture(()=>new Promise(resolve=>{finish=resolve;}));
  const pending = f.load(); await f.load();
  assert.equal(f.requests.length, 1);
  assert.equal(f.state.wristbandLoading, true);
  assert.equal(f.element('wristbandEmployeeId').readOnly, true);
  assert.equal(f.element('wristbandEmployeeId').disabled, false);
  finish({person:employee()}); await pending;
  assert.equal(f.state.wristbandLoading, false);
  assert.equal(f.element('wristbandEmployeeId').disabled, false);
});

test('changing roles invalidates read and mutation access in the support menu', async () => {
  const f = fixture(); await f.load();
  f.run("state.profile.role='verification_staff'");
  await f.load(); await f.mark();
  assert.deepEqual(f.requests.map(r=>r.path), ['/api/lookup']);
});

test('a lookup error arriving after reset does not overwrite the cleared menu', async () => {
  let fail;
  const f = fixture(()=>new Promise((_resolve,reject)=>{fail=reject;}));
  const pending = f.load();
  f.run('resetWristbandSupport()');
  fail(new Error('Old request failed')); await pending;
  assert.equal(f.element('wristbandMessage').textContent, '');
  assert.equal(f.state.wristbandPerson, null);
  assert.equal(f.element('wristbandResult').hidden, true);
});


test('lookup canonicalizes padded employee IDs before an explicit wristband correction', async () => {
  const f = fixture((path,data) => ({person:employee('1234567',true,
    path === '/api/checkin/wristband-lost' ? 'lost' : null),state:'updated'}));
  f.element('wristbandEmployeeId').value = '001234567';
  await f.load();
  assert.equal(f.requests[0].data.employee_id, '001234567');
  assert.equal(f.element('wristbandEmployeeId').value, '1234567');
  await f.mark();
  assert.equal(f.requests.length, 2);
  assert.equal(f.requests[1].data.employee_id, '1234567');
  assert.equal(f.state.wristbandPerson.checkin_wristband_reason, 'lost');
});

test('a pending Find remains focusable while its ID is read-only and duplicate requests are ignored', async () => {
  let finish;
  const f = fixture(()=>new Promise(resolve=>{finish=resolve;}));
  const find = f.element('wristbandFind'); find.focus();
  const pending = f.load();
  assert.equal(find.disabled, false);
  assert.equal(find.getAttribute('aria-disabled'), 'true');
  assert.equal(f.document.activeElement, find);
  assert.equal(f.element('wristbandEmployeeId').readOnly, true);
  assert.equal(f.element('wristbandEmployeeId').disabled, false);
  await f.load(); assert.equal(f.requests.length, 1);
  finish({person:employee()}); await pending;
  assert.equal(find.getAttribute('aria-disabled'), 'false');
  assert.equal(f.element('wristbandEmployeeId').readOnly, false);
  assert.equal(f.document.activeElement, find);
});

test('a pending correction keeps Mark focused and returns focus to Find after hiding the saved action', async () => {
  let finish;
  const f = fixture((path,data)=>path === '/api/lookup' ? {person:employee(data.employee_id)} :
    new Promise(resolve=>{finish=resolve;}));
  await f.load();
  const mark = f.element('wristbandLost'); mark.focus();
  const pending = f.mark();
  assert.equal(mark.disabled, false);
  assert.equal(mark.getAttribute('aria-disabled'), 'true');
  assert.equal(f.document.activeElement, mark);
  await f.mark(); assert.equal(f.requests.length, 2);
  finish({person:employee('1234567',true,'lost'),state:'updated'}); await pending;
  assert.equal(mark.hidden, true);
  assert.equal(mark.getAttribute('aria-disabled'), 'false');
  assert.equal(f.document.activeElement, f.element('wristbandFind'));
  assert.equal(f.element('wristbandEmployeeId').readOnly, false);
});

test('a failed correction preserves focus on the visible retry action', async () => {
  const f = fixture((path,data) => {
    if (path === '/api/lookup') return {person:employee(data.employee_id)};
    throw new Error('Server unavailable');
  });
  await f.load();
  const mark = f.element('wristbandLost'); mark.focus();
  await f.mark();
  assert.equal(mark.hidden, false);
  assert.equal(mark.disabled, false);
  assert.equal(mark.getAttribute('aria-disabled'), 'false');
  assert.equal(f.document.activeElement, mark);
  assert.equal(f.element('wristbandEmployeeId').readOnly, false);
});


test('an already_lost response without exception metadata still finishes the correction exactly once', async () => {
  const f = fixture((path,data)=>path === '/api/lookup' ? {person:employee(data.employee_id)} :
    {person:employee(data.employee_id),state:'already_lost'});
  await f.load(); await f.mark();
  assert.equal(f.state.wristbandPerson.checkin_wristband_reason, 'lost');
  assert.equal(f.element('wristbandLost').hidden, true);
  assert.match(f.element('wristbandStatus').textContent, /lost/i);
  await f.mark();
  assert.deepEqual(f.requests.map(r=>r.path), ['/api/lookup','/api/checkin/wristband-lost']);
});
