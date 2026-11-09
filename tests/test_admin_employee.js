const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');

function fixture(respond = async () => ({eligible_for_draw:true})) {
  const elements = new Map(), calls = [];
  const document = {body:{dataset:{}}, activeElement:null, querySelectorAll:() => [], addEventListener() {},
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        value:'', disabled:false, readOnly:false, textContent:'', hidden:false, className:'',
        listeners:new Map(),
        addEventListener(type, callback) {this.listeners.set(type, callback);},
        emit(type) {return this.listeners.get(type)?.({preventDefault() {}, currentTarget:this});},
        setAttribute(name, value) {this[name] = value;},
        focus() {document.activeElement = this;},
      });
      return elements.get(id);
    }};
  const get = id => document.getElementById(id);
  const form = get('adminAddEmployeeForm');
  form.reportValidity = () => true;
  form.reset = () => {
    form.resetCount = (form.resetCount || 0) + 1;
    for (const field of ['Id','Name','Department','Position']) get('adminAddEmployee' + field).value = '';
    get('adminAddEmployeeDraw').value = 'yes';
  };
  form.reset(); form.resetCount = 0;
  const context = vm.createContext({document, window:{addEventListener() {}}, console, setInterval() {},
    setTimeout:callback => callback(),
    request:async (path, options = {}) => {calls.push({path, data:options.data}); return respond(path, options.data);}});
  vm.runInContext(fs.readFileSync('static/wristband-support.js', 'utf8'), context);
  vm.runInContext(source, context);
  vm.runInContext("api = request; refreshAdmin = async () => {}; state.profile = {role:'admin', csrf:'test'}; state.view = 'admin';", context);
  const run = code => vm.runInContext(code, context);
  const fill = (position = 'Operator') => {
    for (const [field, value] of Object.entries({Id:' 12345 ', Name:' Test Person ', Department:' IT ', Position:position})) {
      get('adminAddEmployee' + field).value = value;
    }
  };
  context.event = {preventDefault() {}, currentTarget:form};
  return {get, form, document, context, run, calls, fill,
    submit:() => run('addAdminEmployee(event)'),
    flush:async () => {for (let i=0; i<6; i++) await Promise.resolve();}};
}

test('manual employee creation sends trimmed fields and waits for explicit submission', async () => {
  const f = fixture(); f.fill();
  assert.equal(f.calls.length, 0);
  await f.submit();
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].path, '/api/admin/employees');
  assert.deepEqual(JSON.parse(JSON.stringify(f.calls[0].data)), {
    employee_id:'12345', name:'Test Person', department:'IT', position:'Operator', eligible_for_draw:true,
  });
  assert.equal(f.form.resetCount, 1);
  assert.equal(f.document.activeElement, f.get('adminAddEmployeeId'));
  assert.equal(f.get('adminAddEmployeeMessage').textContent, 'Employee added.');
});

test('managers cannot select lucky wheel entry and other employees can be excluded', async () => {
  for (const position of ['SR.MANAGER - HR','Technical Manager I','MANAGING DIRECTOR']) {
    const f = fixture(async () => ({eligible_for_draw:false})); f.fill(position);
    f.run('updateAdminEmployeeDraw()');
    assert.equal(f.get('adminAddEmployeeDraw').value, 'no');
    assert.equal(f.get('adminAddEmployeeDraw').disabled, true);
    await f.submit();
    assert.equal(f.calls[0].data.eligible_for_draw, false);
    assert.equal(f.get('adminAddEmployeeMessage').textContent, 'Employee added. Cannot win.');
    assert.equal(f.get('adminAddEmployeeDraw').value, 'yes');
    assert.equal(f.get('adminAddEmployeeDraw').disabled, false);
  }
  const f = fixture(async () => ({eligible_for_draw:false})); f.fill();
  f.get('adminAddEmployeeDraw').value = 'no';
  await f.submit();
  assert.equal(f.calls[0].data.eligible_for_draw, false);
});

test('changing a manager title allows editing the draw choice without silently opting in', () => {
  const f = fixture(); f.fill('Manager I');
  f.run('updateAdminEmployeeDraw()');
  f.get('adminAddEmployeePosition').value = 'Operator';
  f.run('updateAdminEmployeeDraw()');
  assert.equal(f.get('adminAddEmployeeDraw').disabled, false);
  assert.equal(f.get('adminAddEmployeeDraw').value, 'no');
});

test('creation stays single flight with focusable submit and protects submitted fields', async () => {
  let finish;
  const f = fixture(() => new Promise(resolve => {finish = resolve;})); f.fill();
  f.get('adminAddEmployeeSubmit').focus();
  const pending = f.submit(); await f.submit();
  assert.equal(f.calls.length, 1);
  assert.equal(f.get('adminAddEmployeeId').readOnly, true);
  assert.equal(f.get('adminAddEmployeeDraw').disabled, true);
  assert.equal(f.get('adminAddEmployeeSubmit').disabled, false);
  assert.equal(f.get('adminAddEmployeeSubmit')['aria-disabled'], 'true');
  finish({eligible_for_draw:true}); await pending;
  assert.equal(f.get('adminAddEmployeeId').readOnly, false);
  assert.equal(f.get('adminAddEmployeeSubmit')['aria-disabled'], 'false');
});

test('creation failure preserves values and permits an explicit retry', async () => {
  const f = fixture(async () => {throw new Error('Employee ID already exists.');}); f.fill();
  await f.submit();
  assert.equal(f.form.resetCount, 0);
  assert.equal(f.get('adminAddEmployeeId').value, ' 12345 ');
  assert.equal(f.get('adminAddEmployeeMessage').textContent, 'Employee ID already exists.');
  assert.equal(f.get('adminAddEmployeeId').readOnly, false);
  await f.submit();
  assert.equal(f.calls.length, 2);
});

test('a failed totals refresh does not turn a saved employee into a failed creation', async () => {
  const f = fixture(); f.fill();
  f.run("refreshAdmin = async () => {throw new Error('Totals offline');}");
  await f.submit(); await f.flush();
  assert.equal(f.form.resetCount, 1);
  assert.equal(f.calls.length, 1);
  assert.equal(f.get('adminAddEmployeeMessage').textContent, 'Employee added.');
});

test('non-admin submissions and browser-invalid forms never create an employee', async () => {
  const f = fixture(); f.fill();
  f.run("state.profile = {role:'staff'}"); await f.submit();
  f.run("state.profile = {role:'admin'}");
  f.form.reportValidity = () => false; await f.submit();
  assert.equal(f.calls.length, 0);
});

test('sign-out invalidates a pending creation and an old response cannot reset a new admin form', async () => {
  const finishes = [];
  const f = fixture(() => new Promise(resolve => finishes.push(resolve))); f.fill();
  const old = f.submit();
  f.run('showLogin()');
  assert.equal(f.get('adminAddEmployeeId').readOnly, false);
  f.run("state.profile = {role:'admin', csrf:'new-session'}; state.view = 'admin';");
  f.fill(); f.get('adminAddEmployeeId').value = '99999';
  const current = f.submit();
  finishes[0]({eligible_for_draw:true}); await old;
  assert.equal(f.form.resetCount, 0);
  assert.equal(f.get('adminAddEmployeeId').value, '99999');
  assert.equal(f.get('adminAddEmployeeId').readOnly, true);
  finishes[1]({eligible_for_draw:true}); await current;
  assert.equal(f.form.resetCount, 1);
});

test('a late creation error after sign-out cannot replace the new session message', async () => {
  let fail;
  const f = fixture(() => new Promise((_resolve, reject) => {fail = reject;})); f.fill();
  const old = f.submit(); f.run('showLogin()');
  f.get('adminAddEmployeeMessage').textContent = 'New session';
  fail(new Error('Old session failed')); await old;
  assert.equal(f.get('adminAddEmployeeMessage').textContent, 'New session');
});

test('application startup wires manager changes and employee form submissions to the new flow', async () => {
  const f = fixture();
  f.run(`createWorkspace = () => {}; updateNetwork = () => {}; updateAttendanceScope = () => {};
    showApp = () => {};
    api = async (path, options) => path === '/api/me' ? {authenticated:true, role:'admin'} : request(path, options);`);
  await f.run('initialize()');
  f.fill('Manager I');
  await f.get('adminAddEmployeePosition').emit('input');
  assert.equal(f.get('adminAddEmployeeDraw').value, 'no');
  assert.equal(f.get('adminAddEmployeeDraw').disabled, true);
  await f.form.emit('submit');
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].path, '/api/admin/employees');
  assert.equal(f.calls[0].data.eligible_for_draw, false);
});
