const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const portrait = require('../static/employee-portrait.js');

function fixture() {
  const images = [];
  const slot = {children:[], replaceChildren(...children) {this.children = children;}};
  const initials = {textContent:''}, label = {textContent:''}, fallback = {hidden:false};
  const fields = {'.portrait-image':slot, '.portrait-initials':initials,
    '.portrait-label':label, '.portrait-fallback':fallback};
  const root = {hidden:true, dataset:{}, attributes:new Map(),
    querySelector:selector => fields[selector],
    setAttribute(name, value) {this.attributes.set(name, value);},
    removeAttribute(name) {this.attributes.delete(name);},
    ownerDocument:{createElement(tag) {
      assert.equal(tag, 'img');
      const image = {attributes:new Map(), removeAttribute(name) {this.attributes.delete(name); if (name === 'src') delete this.src;}};
      images.push(image); return image;
    }},
  };
  return {api:portrait, root, images, slot, initials, label, fallback};
}

test('loads one same-origin photo without waiting before returning', () => {
  const f = fixture();
  assert.equal(f.api.show(f.root, {employee_id:'1001519', name:'Alice Example'}), undefined);
  assert.equal(f.root.hidden, false);
  assert.equal(f.images[0].src, '/api/employee-photo/1001519');
  assert.equal(f.images[0].alt, 'Photo of Alice Example');
  assert.equal(f.initials.textContent, 'AE');
  assert.equal(f.label.textContent, 'Loading photo');
  assert.equal(f.slot.children.length, 0);
  assert.equal(f.fallback.hidden, false);
  f.images[0].onload();
  assert.equal(f.slot.children[0], f.images[0]);
  assert.equal(f.fallback.hidden, true);
  assert.equal(f.root.dataset.state, 'ready');
  assert.equal(f.root.attributes.has('aria-busy'), false);
});

test('a missing photo leaves readable initials and no broken image', () => {
  const f = fixture(); f.api.show(f.root, {employee_id:'1001519', name:'Alice Example'});
  f.images[0].onerror();
  assert.equal(f.label.textContent, 'No photo');
  assert.equal(f.fallback.hidden, false);
  assert.equal(f.slot.children.length, 0);
  assert.equal(f.root.dataset.state, 'missing');
  assert.equal(f.images[0].src, undefined);
});

test('a previous employee cannot replace a new employee photo', () => {
  const f = fixture(); f.api.show(f.root, {employee_id:'1001519', name:'First Employee'});
  const staleLoad = f.images[0].onload, staleError = f.images[0].onerror;
  f.api.show(f.root, {employee_id:'1001801', name:'Second Employee'});
  f.images[1].onload(); staleLoad(); staleError();
  assert.equal(f.slot.children[0], f.images[1]);
  assert.equal(f.images[1].alt, 'Photo of Second Employee');
  assert.equal(f.root.dataset.state, 'ready');
});

test('reset clears the photo and ignores delayed load or error events', () => {
  const f = fixture(); f.api.show(f.root, {employee_id:'1001519', name:'Alice'});
  const image = f.images[0], staleLoad = image.onload, staleError = image.onerror;
  f.api.reset(f.root); staleLoad(); staleError();
  assert.equal(f.root.hidden, true);
  assert.equal(f.slot.children.length, 0);
  assert.equal(image.src, undefined);
  assert.equal(image.onload, null);
  assert.equal(image.onerror, null);
  assert.equal(f.label.textContent, 'No photo');
});

test('loading a new employee removes the old visible portrait immediately', () => {
  const f = fixture(); f.api.show(f.root, {employee_id:'1001519', name:'First Employee'});
  f.images[0].onload();
  f.api.show(f.root, {employee_id:'1001801', name:'Second Employee'});
  assert.equal(f.slot.children.length, 0);
  assert.equal(f.fallback.hidden, false);
  assert.equal(f.initials.textContent, 'SE');
});

test('invalid IDs never become image URLs', () => {
  for (const employee_id of ['123', '../photo', '1001519?token=secret', '１２３４５６７', 1001519, null, '']) {
    const f = fixture(); f.api.show(f.root, {employee_id, name:'Employee'});
    assert.equal(f.images.length, 0);
    assert.equal(f.label.textContent, 'No photo');
  }
});

test('missing employee information hides and resets the portrait', () => {
  const f = fixture(); f.api.show(f.root, {employee_id:'1001519', name:'Alice'});
  f.api.show(f.root, null);
  assert.equal(f.root.hidden, true);
  assert.equal(f.images[0].src, undefined);
  f.api.show(null, null); f.api.reset(null);
});

test('names are plain text, initials support Thai, and no name is required', () => {
  const f = fixture(); f.api.show(f.root, {employee_id:'1001519', name:'\u0e1e\u0e19\u0e31\u0e01\u0e07\u0e32\u0e19 \u0e17\u0e14\u0e2a\u0e2d\u0e1a'});
  assert.equal(f.initials.textContent, '\u0e1e\u0e17');
  f.api.show(f.root, {employee_id:'1001801', name:'<img src=x>'});
  assert.equal(f.images[1].alt, 'Photo of <img src=x>');
  f.api.show(f.root, {employee_id:'1003585'});
  assert.equal(f.images[2].alt, 'Photo of Employee');
});

test('the browser receives the portrait module before the application starts', () => {
  const context = vm.createContext({window:{}});
  vm.runInContext(fs.readFileSync('static/employee-portrait.js', 'utf8'), context);
  assert.equal(typeof context.window.EventDeskPortrait.show, 'function');
  const html = fs.readFileSync('static/index.html', 'utf8');
  assert.ok(html.indexOf('src="/static/employee-portrait.js"') < html.indexOf('src="/static/app.js"'));
  assert.match(html, /<div class="employee-portrait" hidden>/);
});

test('employee popups show portraits for found/manual checked employees and reset on close', () => {
  const calls = [];
  const fields = new Map();
  const node = () => ({hidden:false, value:'', textContent:'', focus() {}, setAttribute() {},
    querySelector(selector) {
      if (!fields.has(selector)) fields.set(selector, node());
      return fields.get(selector);
    }});
  const workspace = {scanSequence:0};
  for (const key of ['modal','modalMessage','modalReasonLabel','modalReasonSelect','modalDetail',
    'modalDetailLabel','modalSubmit','modalCancel','controlLive','empty','result','codeInput',
    'idInput','mobileActions','mobileClear','mobileSubmit','confirm','message','outcome']) workspace[key] = node();
  const person = {employee_id:'1001519', name:'Alice Example'};
  const context = vm.createContext({workspace, person, window:{clearTimeout() {}, EventDeskPortrait:{
    show:(root, employee) => calls.push({action:'show', root, employee}),
    reset:root => calls.push({action:'reset', root}),
  }}});
  const app = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
  vm.runInContext(app, context);
  vm.runInContext("showCheckinDialog(workspace, 'found', person);", context);
  vm.runInContext("showCheckinDialog(workspace, 'checked', person);", context);
  vm.runInContext("showCheckinDialog(workspace, 'notfound');", context);
  vm.runInContext('resetWorkspace(workspace, false);', context);
  assert.equal(calls[0].employee, person);
  assert.equal(calls[1].employee, person);
  assert.equal(calls[2].employee, null);
  assert.equal(calls[3].action, 'reset');
  assert.equal(calls[3].root, calls[0].root);
});
