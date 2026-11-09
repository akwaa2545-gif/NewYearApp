const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');

function appAt(hash, authenticated = true) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        id, hidden: false, value: '', textContent: '', dataset: {},
        classList: {
          toggle(name, active) { active ? classes.add(name) : classes.delete(name); },
          contains(name) { return classes.has(name); },
        },
        reset() { this.resetCalled = true; },
        replaceChildren() {},
      });
    }
    return elements.get(id);
  };
  const views = ['home', 'admin', 'checkin'].map(name => element(`view-${name}`));
  const scanCard = element('scan-card');
  scanCard.dataset.roles = 'admin staff checkin_staff';
  const adminCard = element('admin-card');
  adminCard.dataset.roles = 'admin';
  element('appView').hidden = true;
  const profile = {authenticated: true, role: 'admin', csrf: 'test-csrf', display_name: 'Administrator'};
  const document = {
    body: {dataset: {}},
    getElementById: element,
    querySelectorAll: selector => selector === '.view' ? views : selector === '[data-roles]' ? [scanCard, adminCard] : [],
  };
  const context = vm.createContext({
    document, profile, location: {hash},
    history: {replaceState(_state, _title, nextHash) { context.location.hash = nextHash; }},
    window: {scrollTo() {}}, console,
    fetch: async path => {
      const payloads = {
        '/api/login': profile,
        '/api/me': typeof authenticated === 'object' ? authenticated : authenticated ? profile : {authenticated: false, role: null},
        '/api/stats': {total_employees: 1709},
        '/api/admin/activity': {items: []},
        '/api/admin/draw-history': {count: 0},
        '/api/settings': {},
      };
      assert.ok(Object.hasOwn(payloads, path), `Unexpected request: ${path}`);
      return {ok: true, headers: {get: () => 'application/json'}, json: async () => payloads[path]};
    },
  });
  vm.runInContext(source, context);
  return {context, document, element, scanCard, adminCard};
}

test('restoring an admin session on the first-page URL keeps the home page', () => {
  const {context, document, element} = appAt('#/');
  vm.runInContext('showApp(profile)', context);
  assert.equal(document.body.dataset.view, 'home');
  assert.equal(element('view-home').classList.contains('active'), true);
  assert.equal(context.location.hash, '#/');
});

test('restoring an admin session on the scan URL keeps the scan page', () => {
  const {context, document, element} = appAt('#/checkin');
  vm.runInContext('showApp(profile)', context);
  assert.equal(document.body.dataset.view, 'checkin');
  assert.equal(element('view-checkin').classList.contains('active'), true);
});

test('Administration exposes Back and returning home keeps admin scan access', () => {
  const {context, document, element, scanCard, adminCard} = appAt('#/admin');
  vm.runInContext('showApp(profile)', context);
  assert.equal(element('headerBackButton').hidden, false);
  vm.runInContext("goView('home')", context);
  assert.equal(document.body.dataset.view, 'home');
  assert.equal(element('logoutButton').hidden, false);
  assert.equal(scanCard.hidden, false);
  assert.equal(adminCard.hidden, false);
  vm.runInContext("goView('checkin')", context);
  assert.equal(document.body.dataset.view, 'checkin');
});

test('explicit admin sign-in still opens Administration', async () => {
  const {context, document, element} = appAt('#/');
  await vm.runInContext('login({preventDefault() {}})', context);
  assert.equal(document.body.dataset.view, 'admin');
  assert.equal(element('loginView').hidden, true);
  assert.equal(element('loginForm').resetCalled, true);
});

test('sign-in without a retained session explains the cookie failure on the login page', async () => {
  const {context, element} = appAt('#/', false);
  await vm.runInContext('login({preventDefault() {}})', context);
  assert.equal(element('appView').hidden, true);
  assert.equal(element('loginView').hidden, false);
  assert.match(element('loginError').textContent, /browser did not keep/i);
  assert.equal(element('loginButton').disabled, false);
});

for (const [description, retainedProfile] of [
  ['the previous staff session', {authenticated: true, role: 'staff', csrf: 'old-staff-csrf'}],
  ['a previous admin session', {authenticated: true, role: 'admin', csrf: 'old-admin-csrf'}],
]) {
  test(`sign-in rejects ${description} when the new admin cookie was not retained`, async () => {
    const {context, element} = appAt('#/', retainedProfile);
    await vm.runInContext('login({preventDefault() {}})', context);
    assert.equal(element('appView').hidden, true);
    assert.equal(element('loginView').hidden, false);
    assert.match(element('loginError').textContent, /browser did not keep/i);
    assert.equal(element('loginButton').disabled, false);
    assert.notEqual(element('loginForm').resetCalled, true);
  });
}
