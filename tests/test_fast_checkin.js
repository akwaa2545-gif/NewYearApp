const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
const employee = (id, checked = false, reason = null) => ({employee_id:id,
  name:`Employee ${id}`, department:'Production', checked_in_at:checked ? '2026-11-06T08:00:00Z' : null,
  checkin_wristband_reason:reason});

function fixture(respond = (path, data) => path === '/api/lookup'
  ? {person:employee(data.code || data.employee_id), lookup:data}
  : {person:employee(data.code || data.employee_id, true), state:'done'}) {
  let clock = 10000, onScan;
  const requests = [], portraits = [], elements = new Map(), fields = new Map(), timers = new Map(), documentListeners = new Map();
  function documentEvent(type, target) {
    const event = {target, defaultPrevented:false, propagationStopped:false,
      preventDefault() {this.defaultPrevented = true;},
      stopPropagation() {this.propagationStopped = true;}};
    for (const callback of documentListeners.get(type) || []) callback(event);
    return event;
  }
  const document = {activeElement:null, body:{dataset:{}}, hidden:false,
    getElementById:id => element(id), querySelectorAll:() => [], querySelector:() => null,
    createElement:() => node(), addEventListener(type, callback) {
      if (!documentListeners.has(type)) documentListeners.set(type, []);
      documentListeners.get(type).push(callback);
    }};
  function node(tagName = 'DIV') {
    const listeners = new Map(), attributes = new Map(), descendants = new Map();
    const value = {tagName, hidden:false, disabled:false, open:false, value:'', textContent:'',
      dataset:{}, style:{zIndex:''}, className:'', classList:{toggle() {}, add() {}, remove() {}},
      addEventListener(type, callback) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(callback);
      },
      emit(type, init = {}) {
        const event = {target:value, preventDefault() {this.defaultPrevented = true;},
          stopPropagation() {}, defaultPrevented:false, ...init};
        for (const callback of listeners.get(type) || []) callback(event);
        if (type === 'cancel' && !event.defaultPrevented) value.close();
        return event;
      },
      setAttribute(name, text) {attributes.set(name, String(text));},
      getAttribute:name => attributes.get(name) ?? null,
      hasAttribute:name => attributes.has(name),
      removeAttribute:name => attributes.delete(name),
      focus() {document.activeElement = value; documentEvent('focusin', value);}, append() {}, replaceChildren() {},
      remove() {}, closest:() => null,
      querySelector(selector) {
        if (!descendants.has(selector)) descendants.set(selector, node());
        return descendants.get(selector);
      },
      showModal() {value.open = true; value.showCount = (value.showCount || 0) + 1;},
      close() {value.open = false;},
    };
    return value;
  }
  function field(selector) {
    if (!fields.has(selector)) fields.set(selector, node(/input|reason$|detail$/.test(selector) ? 'INPUT' :
      /button|submit|cancel|close/.test(selector) ? 'BUTTON' : 'DIV'));
    return fields.get(selector);
  }
  function element(id) {
    if (!elements.has(id)) elements.set(id, node());
    return elements.get(id);
  }
  const host = element('checkinWorkspace'); host.querySelector = field;
  element('scannerTemplate').content = {cloneNode:() => ({})};
  const popup = field('.employee-popup'); popup.tagName = 'DIALOG';
  popup.closest = selector => selector === '.view' ? element('view-checkin') : null;
  field('.scan-receipt').hidden = true;
  field('.checkin-success').hidden = true;
  class Scanner {
    static async getCameras() {return [{id:'rear', label:'rear'}];}
    async start(_id, _options, callback) {this.isScanning = true; onScan = callback;}
    async stop() {this.isScanning = false;}
  }
  class ClockDate extends Date {static now() {return clock;}}
  const window = {Html5Qrcode:Scanner, isSecureContext:true, scrollTo() {},
    EventDeskPortrait:{show(_node, person) {portraits.push(person);}, reset() {}},
    clearTimeout:id => timers.delete(id),
    setTimeout(callback, delay) {const id = Symbol('timer'); timers.set(id, {callback, delay}); return id;}};
  const context = vm.createContext({window, document, Date:ClockDate, console,
    Html5Qrcode:Scanner, Html5QrcodeSupportedFormats:{QR_CODE:0},
    location:{hostname:'localhost', hash:'#/checkin'}, history:{replaceState() {}},
    matchMedia:() => ({matches:false}), setTimeout:window.setTimeout,
    request:async (path, options) => {requests.push({path, data:options?.data}); return respond(path, options?.data);},
  });
  vm.runInContext(source, context);
  vm.runInContext(`api = request; refreshStats = async () => request('/api/stats');
    createWorkspace('checkinWorkspace', 'checkin');
    state.profile = {role:'staff', csrf:'test'}; state.view = 'checkin';
    globalThis.workspace = state.workspaces.checkin;`, context);
  const run = code => vm.runInContext(code, context);
  popup.contains = target => target === popup || [context.workspace.modal, context.workspace.modalSubmit,
    context.workspace.modalCancel, context.workspace.popupClose, context.workspace.modalReasonSelect,
    context.workspace.modalDetail].includes(target);
  const flush = async () => {for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();};
  return {requests, portraits, fields, popup, document, documentEvent, timers, run, flush,
    workspace:context.workspace, field,
    lookup:(id, manual = false) => run(`findEmployee(workspace, ${JSON.stringify(manual ? {employee_id:id} : {code:id})})`),
    start:() => run('startCamera(workspace)'), scan:code => onScan(code),
    key(key, extra = {}) {
      clock += 20;
      const event = {key, ctrlKey:false, metaKey:false, altKey:false,
        preventDefault() {this.defaultPrevented = true;}, stopPropagation() {}, defaultPrevented:false, ...extra};
      context.keyEvent = event; run('captureScanner(keyEvent)'); return event;
    },
  };
}

test('a found employee opens a native review popup and cannot be replaced before confirmation', async () => {
  const f = fixture();
  await f.lookup('first');
  assert.equal(f.popup.open, true);
  assert.equal(f.document.activeElement, f.workspace.modalSubmit);
  await f.lookup('second');
  assert.equal(f.workspace.person.employee_id, 'first');
  assert.equal(f.requests.length, 1, 'manual and handheld entry points must respect the open review');
});

test('one confirmation saves once, closes immediately, leaves a receipt and accepts the next QR', async () => {
  const f = fixture();
  await f.start(); await f.scan('first');
  await f.run('confirmAction(workspace)');
  assert.equal(f.popup.open, false);
  assert.equal(f.workspace.scanBusy, false);
  assert.equal(f.workspace.person, null);
  assert.equal(f.workspace.receipt.hidden, false);
  assert.match(f.workspace.receipt.querySelector('.receipt-name').textContent, /first/);
  assert.match(f.workspace.receipt.querySelector('.receipt-status').textContent, /checked in|saved/i);
  assert.deepEqual(f.requests.map(r => r.path), ['/api/lookup', '/api/scan/checkin'], 'no per-employee stats request');
  await f.scan('second');
  assert.equal(f.workspace.person.employee_id, 'second');
  assert.equal(f.popup.open, true);
  assert.equal(f.workspace.scanner.isScanning, true);
});

test('already-checked-in QR shows a read-only photo popup and accepts the next scan without a click', async () => {
  const f = fixture((_path, data) => ({person:employee(data.code, data.code === 'first'), lookup:data}));
  await f.start(); await f.scan('first');
  assert.equal(f.popup.open, true);
  assert.equal(f.workspace.scanBusy, false);
  assert.equal(f.workspace.reviewPending, false);
  assert.equal(f.portraits.at(-1).employee_id, 'first');
  assert.match(f.workspace.modal.querySelector('.modal-name').textContent, /first/);
  assert.match(f.workspace.modal.querySelector('.modal-status').textContent, /already/i);
  assert.equal(f.workspace.modalSubmit.hidden, true);
  assert.equal(f.workspace.modalReasonLabel.hidden, true);
  assert.equal(f.workspace.modalCancel.textContent, 'Close');
  assert.equal(f.workspace.popupClose.getAttribute('aria-label'), 'Close employee information');
  await f.scan('first');
  await f.lookup('first');
  assert.equal(f.popup.showCount, 1, 'repeated QR must not reopen or flicker the popup');
  assert.equal(f.portraits.length, 1, 'repeated QR must not reload the portrait');
  await f.scan('second');
  assert.equal(f.workspace.person.employee_id, 'second');
  assert.equal(f.workspace.reviewPending, true);
  assert.equal(f.workspace.modalSubmit.hidden, false);
  assert.equal(f.popup.showCount, 1, 'new QR replaces the contents of the open popup');
  assert.deepEqual(f.requests.map(r => r.path), ['/api/lookup', '/api/lookup']);
});

for (const fallback of [false, true]) {
  test(`popup entrance replays only for a new result without delaying scans (${fallback ? 'fallback' : 'native'})`, async () => {
    const f = fixture((_path, data) => ({person:employee(data.code, data.code !== 'third'), lookup:data}));
    if (fallback) {f.popup.showModal = undefined; f.popup.close = undefined;}
    await f.start(); await f.scan('first');
    const firstEntrance = f.popup.getAttribute('data-entrance');
    assert.ok(firstEntrance, 'the initial result has an entrance animation');
    assert.equal(f.workspace.popupScrim.getAttribute('data-entrance'), firstEntrance);
    await f.scan('first');
    assert.equal(f.popup.getAttribute('data-entrance'), firstEntrance, 'the same QR does not replay motion');
    await f.scan('second');
    assert.notEqual(f.popup.getAttribute('data-entrance'), firstEntrance, 'a replacement replays without reopening');
    await f.scan('third');
    assert.equal(f.popup.getAttribute('data-entrance'), firstEntrance, 'consecutive replacements keep replaying');
    assert.equal(f.workspace.person.employee_id, 'third');
    assert.equal(f.document.activeElement, f.workspace.modalSubmit);
    assert.equal(f.timers.size, 0, 'entrance motion adds no timers or scan delays');
    if (!fallback) assert.equal(f.popup.showCount, 1);
    f.run('dismissCheckin(workspace)');
    assert.equal(f.run('employeePopupOpen(workspace)'), false, 'closing never waits for animation');
  });
}

test('a failed save keeps the same employee available for retry and does not show a saved receipt', async () => {
  const f = fixture((path, data) => {
    if (path === '/api/lookup') return {person:employee(data.code), lookup:data};
    throw new Error('Network interrupted');
  });
  await f.lookup('first');
  await f.run('confirmAction(workspace)');
  assert.equal(f.popup.open, true);
  assert.equal(f.workspace.person.employee_id, 'first');
  assert.equal(f.workspace.actionPending, false);
  assert.match(f.workspace.modalMessage.textContent, /Network interrupted/);
  assert.equal(f.workspace.receipt.hidden, true);
});

test('saving is single-flight, preserves keyboard focus and cannot be skipped with Escape', async () => {
  let finish;
  const f = fixture((path, data) => path === '/api/lookup' ? {person:employee(data.code), lookup:data} :
    new Promise(resolve => {finish = resolve;}));
  await f.lookup('first');
  const saving = f.run('confirmAction(workspace)');
  await f.run('confirmAction(workspace)');
  assert.equal(f.requests.filter(r => r.path === '/api/scan/checkin').length, 1);
  assert.notEqual(f.workspace.modalSubmit.disabled, true, 'keep the focused action reachable while aria-disabled');
  const escape = f.popup.emit('cancel');
  assert.equal(escape.defaultPrevented, true);
  assert.equal(f.popup.open, true);
  assert.equal(f.workspace.person.employee_id, 'first');
  f.workspace.modalCancel.emit('click');
  assert.equal(f.popup.open, true);
  finish({person:employee('first', true), state:'done'});
  await saving;
  assert.equal(f.popup.open, false);
});

test('Escape skips an unsubmitted employee without creating attendance', async () => {
  const f = fixture(); await f.lookup('first');
  f.popup.emit('cancel');
  assert.equal(f.popup.open, false);
  assert.equal(f.workspace.person, null);
  assert.equal(f.workspace.scanBusy, false);
  assert.deepEqual(f.requests.map(r => r.path), ['/api/lookup']);
});

test('a late saved response after reset cannot replace a newer employee', async () => {
  let finish;
  const f = fixture((path, data) => path === '/api/lookup' ? {person:employee(data.code), lookup:data} :
    new Promise(resolve => {finish = resolve;}));
  await f.lookup('first');
  const saving = f.run('confirmAction(workspace)');
  f.run('resetWorkspace(workspace)');
  f.workspace.actionPending = false; // A forced navigation reset has detached the old request.
  await f.lookup('second');
  finish({person:employee('first', true), state:'done'});
  await saving;
  assert.equal(f.workspace.person.employee_id, 'second');
  assert.equal(f.popup.open, true);
  assert.equal(f.workspace.receipt.hidden, true, 'old completion must not announce a new saved result');
});

test('handheld barcode bursts cannot replace or confirm an employee while the popup is open', async () => {
  const f = fixture(); await f.lookup('first');
  for (const key of 'PARTY:second') f.key(key);
  const enter = f.key('Enter');
  await f.flush();
  assert.equal(enter.defaultPrevented, true, 'consume the scanner Enter before it activates the focused Check in button');
  assert.equal(f.workspace.person.employee_id, 'first');
  assert.deepEqual(f.requests.map(r => r.path), ['/api/lookup']);
  const deliberateEnter = f.key('Enter');
  assert.equal(deliberateEnter.defaultPrevented, false, 'a deliberate Enter still activates the focused button normally');
});

test('manual ID defaults to an unreadable QR and waits for Check in before saving', async () => {
  const f = fixture(); await f.lookup('manual', true);
  assert.equal(f.popup.open, true);
  assert.equal(f.workspace.modalReasonLabel.hidden, false);
  assert.equal(f.workspace.modalReasonSelect.value, 'unreadable');
  assert.equal(f.document.activeElement, f.workspace.modalSubmit, 'manual entry is ready for Check in');
  assert.deepEqual(f.requests.map(r => r.path), ['/api/lookup'], 'lookup must not save attendance');
  await f.run('confirmAction(workspace)');
  assert.equal(f.popup.open, false);
  assert.equal(f.requests[1].path, '/api/scan/checkin');
  assert.equal(f.requests[1].data.employee_id, 'manual');
  assert.equal(f.requests[1].data.wristband_reason, 'unreadable');
});

test('each manual employee starts with the unreadable default and QR check-in has no manual reason', async () => {
  const f = fixture(); await f.lookup('first', true);
  f.workspace.modalReasonSelect.value = 'lost';
  await f.run('confirmAction(workspace)');
  assert.equal(f.requests[1].data.wristband_reason, 'lost', 'staff may change the default');
  await f.lookup('second', true);
  assert.equal(f.workspace.modalReasonSelect.value, 'unreadable');
  f.workspace.modalReasonSelect.value = 'other';
  f.workspace.modalDetail.value = 'Old note';
  f.run('dismissCheckin(workspace)');
  await f.lookup('third', true);
  assert.equal(f.workspace.modalReasonSelect.value, 'unreadable');
  assert.equal(f.workspace.modalDetail.value, '');
  assert.equal(f.workspace.modalDetailLabel.hidden, true);
  f.run('dismissCheckin(workspace)');
  await f.lookup('qr');
  assert.equal(f.workspace.modalReasonLabel.hidden, true);
  await f.run('confirmAction(workspace)');
  assert.equal(f.requests.at(-1).data.code, 'qr');
  assert.equal(Object.hasOwn(f.requests.at(-1).data, 'wristband_reason'), false);
});

test('manual ID reason validation remains inside the popup before saving', async () => {
  const f = fixture(); await f.lookup('manual', true);
  assert.equal(f.popup.open, true);
  assert.equal(f.workspace.modalReasonLabel.hidden, false);
  f.workspace.modalReasonSelect.value = '';
  await f.run('confirmAction(workspace)');
  assert.equal(f.requests.length, 1);
  f.workspace.modalReasonSelect.value = 'other';
  f.workspace.modalDetail.value = '';
  await f.run('confirmAction(workspace)');
  assert.equal(f.requests.length, 1);
  f.workspace.modalDetail.value = 'Damaged badge';
  await f.run('confirmAction(workspace)');
  assert.equal(f.popup.open, false);
  assert.equal(f.requests[1].data.wristband_reason, 'other');
  assert.equal(f.requests[1].data.wristband_detail, 'Damaged badge');
});

test('leaving the scanning view closes its popup without saving the employee', async () => {
  const f = fixture(); await f.lookup('first');
  f.run("goView('home')");
  assert.equal(f.popup.open, false);
  assert.equal(f.workspace.person, null);
  assert.equal(f.requests.filter(r => r.path === '/api/scan/checkin').length, 0);
});

test('a save finishing after sign-out cannot reopen its popup or announce an old receipt', async () => {
  let finish;
  const f = fixture((path, data) => path === '/api/lookup' ? {person:employee(data.code), lookup:data} :
    new Promise(resolve => {finish = resolve;}));
  await f.lookup('first');
  const saving = f.run('confirmAction(workspace)');
  f.run('showLogin()');
  assert.equal(f.popup.open, false);
  finish({person:employee('first', true), state:'done'});
  await saving;
  assert.equal(f.popup.open, false);
  assert.equal(f.workspace.receipt.hidden, true);
  assert.equal(f.workspace.person, null);
});

test('repeated Enter and text composition never accidentally confirm the popup', async () => {
  const f = fixture(); await f.lookup('first');
  assert.equal(f.key('Enter', {repeat:true}).defaultPrevented, true);
  f.key('Process', {isComposing:true});
  await f.flush();
  assert.deepEqual(f.requests.map(r => r.path), ['/api/lookup']);
});

test('an older save completion cannot unlock a newer in-flight employee save', async () => {
  const finishes = new Map();
  const f = fixture((path, data) => path === '/api/lookup' ? {person:employee(data.code), lookup:data} :
    new Promise(resolve => {finishes.set(data.code, resolve);}));
  await f.lookup('first');
  const firstSave = f.run('confirmAction(workspace)');
  f.run('resetWorkspace(workspace)');
  f.workspace.actionPending = false;
  await f.lookup('second');
  const secondSave = f.run('confirmAction(workspace)');
  finishes.get('first')({person:employee('first', true), state:'done'});
  await firstSave;
  assert.equal(f.workspace.actionPending, true, 'only the owner of the current save may unlock its controls');
  assert.equal(f.workspace.modalSubmit.getAttribute('aria-disabled'), 'true');
  await f.run('confirmAction(workspace)');
  assert.equal(f.requests.filter(r => r.path === '/api/scan/checkin').length, 2);
  finishes.get('second')({person:employee('second', true), state:'done'});
  await secondSave;
  assert.equal(f.workspace.scanBusy, false);
});

test('a barcode begun in the QR input during lookup cannot confirm the popup with its trailing Enter', async () => {
  let finishLookup;
  const f = fixture((_path, data) => new Promise(resolve => {finishLookup = () => resolve({person:employee(data.code), lookup:data});}));
  const lookup = f.lookup('first');
  f.document.activeElement = f.workspace.codeInput;
  for (const key of 'PARTY:second') f.key(key, {target:f.workspace.codeInput});
  finishLookup(); await lookup;
  assert.equal(f.document.activeElement, f.workspace.modalSubmit);
  const enter = f.key('Enter', {target:f.workspace.modalSubmit});
  assert.equal(enter.defaultPrevented, true, 'the Enter belongs to the in-flight barcode, not the newly focused action');
  assert.equal(f.workspace.person.employee_id, 'first');
  assert.deepEqual(f.requests.map(r => r.path), ['/api/lookup']);
});

test('the non-native popup fallback removes its open attribute on reset and Skip', async () => {
  const f = fixture();
  delete f.popup.open; delete f.popup.showModal; delete f.popup.close;
  await f.lookup('first');
  assert.equal(f.popup.hasAttribute('open'), true);
  f.run('resetWorkspace(workspace)');
  assert.equal(f.popup.hasAttribute('open'), false);
  await f.lookup('second');
  assert.equal(f.popup.hasAttribute('open'), true);
  f.workspace.modalCancel.emit('click');
  assert.equal(f.popup.hasAttribute('open'), false);
  assert.equal(f.workspace.person, null);
});

test('an already-checked-in popup focuses its visible Close button', async () => {
  const f = fixture((_path, data) => ({person:employee(data.code, true), lookup:data}));
  await f.start();
  const zoom = f.field('.camera-zoom'); zoom.tagName = 'INPUT'; zoom.focus();
  await f.scan('first');
  assert.equal(f.document.activeElement, f.workspace.modalCancel);
  assert.equal(f.popup.open, true);
  assert.equal(f.workspace.scanBusy, false);
});

test('completed scans announce through an always-present status node', async () => {
  const f = fixture((_path, data) => ({person:employee(data.code, true), lookup:data}));
  await f.start();
  assert.ok(f.workspace.announcement, 'the announcement node exists before the first result');
  assert.notEqual(f.workspace.announcement.hidden, true);
  await f.scan('first');
  assert.match(f.workspace.announcement.textContent, /first.*already|already.*first/i);
  await f.scan('second');
  assert.match(f.workspace.announcement.textContent, /second.*already|already.*second/i);
  assert.equal(f.popup.showCount, 1, 'checked employees replace the same open dialog');
  assert.equal(f.portraits.at(-1).employee_id, 'second');
  assert.equal(f.workspace.modalSubmit.hidden, true);
  assert.equal(f.workspace.scanBusy, false);
});

test('a full 1709-employee queue needs no reset clicks and creates exactly one save per employee', async () => {
  const f = fixture(); await f.start();
  for (let number = 1; number <= 1709; number += 1) {
    await f.scan(`EMP:${number}`);
    await f.run('confirmAction(workspace)');
    assert.equal(f.workspace.scanBusy, false);
  }
  const saves = f.requests.filter(request => request.path === '/api/scan/checkin');
  assert.equal(saves.length, 1709);
  assert.equal(new Set(saves.map(request => request.data.code)).size, 1709);
  assert.equal(f.requests.length, 3418);
  assert.equal(f.workspace.nextScanTimer, null);
  assert.equal(f.popup.open, false);
  assert.equal(f.workspace.scanner.isScanning, true);
  assert.match(f.workspace.receipt.querySelector('.receipt-name').textContent, /1709/);
});

test('holding Enter after a mobile confirmation cannot activate the now-focused Stop camera button', async () => {
  const f = fixture();
  f.run('matchMedia = () => ({matches:true})');
  await f.start(); await f.scan('first');
  await f.run('confirmAction(workspace)');
  assert.equal(f.document.activeElement, f.workspace.cameraButton);
  const repeat = f.key('Enter', {repeat:true, target:f.workspace.cameraButton});
  assert.equal(repeat.defaultPrevented, true);
  assert.equal(f.workspace.scanner.isScanning, true);
  assert.deepEqual(f.requests.map(r => r.path), ['/api/lookup', '/api/scan/checkin']);
});

test('fallback popup blocks outside clicks and returns outside focus to its confirmation action', async () => {
  const f = fixture();
  const view = f.document.getElementById('view-checkin'); view.style.zIndex = '10';
  delete f.popup.open; delete f.popup.showModal; delete f.popup.close;
  await f.lookup('first');
  assert.equal(view.style.zIndex, '10001', 'the fallback must sit above the page header');
  const outside = f.field('.outside-header-button'); outside.tagName = 'BUTTON';
  const click = f.documentEvent('click', outside);
  assert.equal(click.defaultPrevented, true);
  assert.equal(click.propagationStopped, true);
  outside.focus();
  assert.equal(f.document.activeElement, f.workspace.modalSubmit);
  f.workspace.modalCancel.emit('click');
  assert.equal(view.style.zIndex, '10', 'closing the fallback restores the original stacking order');
  const afterClose = f.documentEvent('click', outside);
  assert.equal(afterClose.defaultPrevented, false);
  outside.focus();
  assert.equal(f.document.activeElement, outside);
});

test('native dialogs do not activate the fallback click and focus interception', async () => {
  const f = fixture(); await f.lookup('first');
  const outside = f.field('.outside-header-button'); outside.tagName = 'BUTTON';
  const click = f.documentEvent('click', outside);
  assert.equal(click.defaultPrevented, false, 'the native dialog provides its own browser interaction boundary');
  assert.equal(click.propagationStopped, false);
  outside.focus();
  assert.equal(f.document.activeElement, outside, 'the fallback focus guard stays inactive on the native path');
  assert.equal(f.popup.open, true);
});


test('manual lookup of an already-checked-in employee never offers a wristband correction in scanning', async () => {
  const f = fixture((_path, data) => ({person:employee(data.employee_id, true), lookup:data}));
  await f.lookup('checked', true);
  assert.equal(f.popup.open, true);
  assert.equal(f.workspace.scanBusy, false);
  assert.equal(f.workspace.modalSubmit.hidden, true);
  assert.equal(f.workspace.modalReasonLabel.hidden, true);
  assert.match(f.workspace.modal.querySelector('.modal-status').textContent, /already checked in/i);
  await f.run('confirmAction(workspace)');
  assert.deepEqual(f.requests.map(r => r.path), ['/api/lookup']);
});

test('a duplicate save shows employee information without success motion and accepts a handheld scan', async () => {
  const f = fixture((path, data) => path === '/api/lookup'
    ? {person:employee(data.code), lookup:data}
    : {person:employee(data.code, true), state:'already_done'});
  await f.lookup('first'); await f.run('confirmAction(workspace)');
  assert.equal(f.popup.open, true);
  assert.equal(f.workspace.modalSubmit.hidden, true);
  assert.equal(f.workspace.modalReasonLabel.hidden, true);
  assert.equal(f.field('.checkin-success').hidden, true);
  assert.equal(f.workspace.scanBusy, false);
  assert.equal(f.workspace.actionPending, false);
  for (const key of 'PARTY:second') f.key(key);
  const enter = f.key('Enter');
  assert.equal(enter.defaultPrevented, true, 'scanner Enter must not activate Close');
  await f.flush();
  assert.equal(f.workspace.person.employee_id, 'PARTY:second');
  assert.equal(f.workspace.modalSubmit.hidden, false);
  assert.equal(f.requests.filter(r => r.path === '/api/scan/checkin').length, 1);
});

test('navigation and sign-out close read-only checked employee popups', async () => {
  for (const leave of ["goView('home')", 'showLogin()']) {
    const f = fixture((_path, data) => ({person:employee(data.code, true), lookup:data}));
    await f.lookup('checked');
    assert.equal(f.popup.open, true);
    f.run(leave);
    assert.equal(f.popup.open, false);
    assert.equal(f.workspace.person, null);
    assert.equal(f.requests.filter(r => r.path === '/api/lookup').length, 1);
    assert.equal(f.requests.filter(r => r.path === '/api/scan/checkin').length, 0);
  }
});


test('green success feedback appears only after save confirmation and never blocks the next scan', async () => {
  let finish;
  const f = fixture((path, data) => path === '/api/lookup'
    ? {person:employee(data.code), lookup:data}
    : new Promise(resolve => {finish = resolve;}));
  await f.start(); await f.scan('first');
  const saving = f.run('confirmAction(workspace)');
  assert.equal(f.field('.checkin-success').hidden, true);
  finish({person:employee('first', true), state:'done'}); await saving;
  assert.equal(f.field('.checkin-success').hidden, false);
  assert.equal(f.workspace.scanBusy, false);
  assert.equal(f.popup.open, false);
  assert.equal(f.workspace.scanner.isScanning, true);
  const focus = f.document.activeElement;
  assert.notEqual(focus, f.field('.checkin-success'));
  const timer = [...f.timers.values()].find(value => value.delay === 900);
  assert.ok(timer); timer.callback();
  assert.equal(f.field('.checkin-success').hidden, true);
  assert.equal(f.document.activeElement, focus);
});

test('duplicate and failed saves never show green success feedback', async () => {
  for (const outcome of ['already_done', 'failure']) {
    const f = fixture((path, data) => {
      if (path === '/api/lookup') return {person:employee(data.code), lookup:data};
      if (outcome === 'failure') throw new Error('Save failed');
      return {person:employee(data.code, true), state:outcome};
    });
    await f.lookup('first'); await f.run('confirmAction(workspace)');
    assert.equal(f.field('.checkin-success').hidden, true);
    assert.equal([...f.timers.values()].some(value => value.delay === 900), false);
  }
});

test('new employee clears feedback and stale completion cannot hide a newer success', async () => {
  const f = fixture();
  await f.lookup('first'); await f.run('confirmAction(workspace)');
  const oldTimer = [...f.timers.values()].find(value => value.delay === 900);
  assert.ok(oldTimer);
  await f.lookup('second');
  assert.equal(f.field('.checkin-success').hidden, true);
  await f.run('confirmAction(workspace)');
  assert.equal(f.field('.checkin-success').hidden, false);
  oldTimer.callback();
  assert.equal(f.field('.checkin-success').hidden, false);
  assert.equal([...f.timers.values()].filter(value => value.delay === 900).length, 1);
  f.run('resetWorkspace(workspace, false)');
  assert.equal(f.field('.checkin-success').hidden, true);
});

test('leaving the camera workspace clears success feedback', async () => {
  const f = fixture();
  await f.lookup('first'); await f.run('confirmAction(workspace)');
  await f.run('stopCamera(workspace)');
  assert.equal(f.field('.checkin-success').hidden, true);
  assert.equal([...f.timers.values()].some(value => value.delay === 900), false);
});
