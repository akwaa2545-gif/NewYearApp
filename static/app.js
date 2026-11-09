'use strict';

const $ = (id) => document.getElementById(id);
const state = {csrf:null, profile:null, view:'home', workspaces:{}, wheelTurn:0, drawing:false, prizeImageUploading:false, keyBuffer:'', keyAt:0, projectorSource:null, adminChecked:null, adminDrawCount:null, adminLoading:false, adminRefreshPending:false, adminEmployeeOperation:null, adminAttendanceOperation:null, adminDrawOperation:null, adminDialog:null, attendancePage:1, attendanceRequestId:0, wristbandPerson:null, wristbandRequestId:0, wristbandLoading:false, wristbandSaving:false, wristbandOperation:null};

async function api(path, options={}) {
  const method = options.method || 'GET';
  const headers = {...(options.headers || {})};
  if (state.csrf && method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  if (options.data !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  const lookupMayRetry = path === '/api/lookup';
  for (let attempt = 0; attempt < (lookupMayRetry ? 2 : 1); attempt += 1) {
    try {
      response = await fetch(path, {method, headers, credentials:'same-origin', body:options.data === undefined ? undefined : JSON.stringify(options.data)});
    } catch (_) {
      if (lookupMayRetry && attempt === 0) {
        await new Promise(resolve => setTimeout(resolve, 350));
        continue;
      }
      throw new Error('Cannot connect. Check your network.');
    }
    if (lookupMayRetry && attempt === 0 && [502,503,504].includes(response.status)) {
      await new Promise(resolve => setTimeout(resolve, 350));
      continue;
    }
    break;
  }
  const type = response.headers.get('Content-Type') || '';
  const payload = type.includes('json') ? await response.json().catch(() => ({})) : await response.text();
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login') showLogin();
    const gatewayMessage = [502,503,504].includes(response.status)
      ? 'Server is offline. Try again.'
      : '';
    const error = new Error(payload?.error || payload?.message || gatewayMessage || `Request failed (${response.status}).`);
    error.code = payload?.code;
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function toast(message, type='', {checkinComplete=false}={}) {
  const node = document.createElement('div');
  node.className = `toast ${type}`;
  if (checkinComplete) {
    document.querySelectorAll('.toast.checkin-complete').forEach(item => item.remove());
    node.classList.add('checkin-complete');
  }
  node.textContent = message;
  $('toastRegion').append(node);
  setTimeout(() => node.remove(), checkinComplete ? 2400 : 4200);
}

function showLogin(message='', preserveStaff=false) {
  resetAdminDialogs();
  if (state.view === 'wristband') resetWristbandSupport();
  Object.values(state.workspaces).forEach(workspace => {
    if (workspace.reviewPending || workspace.lookupPending || workspace.actionPending || employeePopupOpen(workspace)) resetWorkspace(workspace, false);
    stopCamera(workspace);
  });
  state.projectorSource?.close(); state.projectorSource = null;
  if (!preserveStaff) { state.csrf = null; state.profile = null; }
  state.adminChecked = null; state.adminDrawCount = null;
  $('appView').hidden = true; $('loginView').hidden = false;
  $('loginError').textContent = message;
  setTimeout(() => $('loginPassword').focus(), 0);
}

function showApp(profile) {
  resetAdminDialogs();
  state.profile = profile; state.csrf = profile.csrf;
  $('loginView').hidden = true; $('appView').hidden = false;
  $('eventName').textContent = profile.event_name || 'Company Party';
  $('adminLoginButton').hidden = profile.role === 'admin';
  $('logoutButton').hidden = profile.role !== 'admin';
  $('staffName').textContent = profile.display_name || 'Event Staff';
  document.querySelectorAll('[data-roles]').forEach(card => {
    card.hidden = !card.dataset.roles.split(' ').includes(profile.role);
  });
  document.title = `${profile.event_name || 'Company Party'} · Staff`;
  const requestedView = location.hash === '#/' ? 'home' : /^#\/(checkin|verify|draw|admin|attendance|wristband)$/.exec(location.hash)?.[1];
  goView(requestedView || (profile.role === 'admin' ? 'admin' : 'home'));
  const projectorSettings = $('projectorSettingsForm');
  projectorSettings.hidden = profile.role !== 'admin';
  if (profile.role === 'admin') loadProjectorSettings().catch(() => {});
  refreshStats().catch(error => toast(error.message, 'error'));
}

async function login(event) {
  event.preventDefault();
  const button = $('loginButton');
  button.disabled = true; button.textContent = 'Signing in…'; $('loginError').textContent = '';
  try {
    const profile = await api('/api/login', {method:'POST', data:{password:$('loginPassword').value, admin_only:true}});
    const me = await api('/api/me');
    if (!me.authenticated || me.role !== profile.role || me.csrf !== profile.csrf) {
      throw new Error('Your browser did not keep your login. Allow cookies and try again.');
    }
    showApp({...me, ...profile});
    goView('admin');
    $('loginForm').reset();
  } catch (error) {
    $('loginError').textContent = error.message;
  } finally {
    button.disabled = false; button.textContent = 'Sign in';
  }
}

function goView(name) {
  if (!['home','checkin','verify','draw','admin','attendance','wristband'].includes(name)) name = 'home';
  if (name === 'verify' && state.profile?.role !== 'verification_staff') name = 'checkin';
  const permitted = {checkin:['admin','staff','checkin_staff'], attendance:['admin','staff','checkin_staff'], wristband:['admin','staff','checkin_staff'], verify:['admin','staff','verification_staff'], draw:['admin','staff','lucky_draw_operator'], admin:['admin']};
  if (permitted[name] && !permitted[name].includes(state.profile?.role)) name = 'home';
  if (name !== 'admin') resetAdminDialogs();
  if (state.view !== name && (state.view === 'wristband' || name === 'wristband')) resetWristbandSupport();
  Object.values(state.workspaces).forEach(workspace => {
    if (workspace.mode === name) return;
    if (workspace.reviewPending || workspace.lookupPending || workspace.actionPending || employeePopupOpen(workspace)) resetWorkspace(workspace, false);
    stopCamera(workspace);
  });
  document.querySelectorAll('.view').forEach(view => view.classList.toggle('active', view.id === `view-${name}`));
  state.view = name;
  document.body.dataset.view = name;
  $('adminLoginButton').hidden = name !== 'home' || state.profile?.role === 'admin';
  $('headerBackButton').hidden = name === 'home';
  history.replaceState(null, '', name === 'home' ? '#/' : `#/${name}`);
  window.scrollTo({top:0, behavior:'smooth'});
  if (name === 'home' || name === 'draw') refreshStats().catch(() => {});
  if (name === 'attendance') loadAttendance();
  if (name === 'admin') refreshAdmin().catch(() => {});
  if (name === 'draw') {
    loadPrizeList().catch(error => { $('drawMessage').textContent = error.message; });
    loadProjectorState().catch(() => {});
    loadWinnerHistory().catch(() => {});
    connectProjectorState();
  } else if (state.projectorSource) {
    state.projectorSource.close(); state.projectorSource = null;
  }
}

async function refreshStats() {
  const stats = await api('/api/stats');
  $('totalCount').textContent = Number(stats.total_employees ?? stats.total ?? 0).toLocaleString();
  $('checkedCount').textContent = Number(stats.checked_in ?? 0).toLocaleString();
  $('eligibleCount').textContent = Number(stats.eligible ?? 0).toLocaleString();
  $('drawEligible').textContent = Number(stats.eligible ?? 0).toLocaleString();
  $('drawEligibleSummary').textContent = Number(stats.eligible ?? 0).toLocaleString();
  $('drawParticipantCount').textContent = Number(stats.lucky_participants ?? 0).toLocaleString();
  $('winnerCount').textContent = Number(stats.winners ?? 0).toLocaleString();
}

function attendanceCell(row, label, value) {
  const cell = document.createElement('td');
  cell.dataset.label = label;
  cell.textContent = value;
  row.append(cell);
}

function attendanceStaffName(value) {
  if (value === 'legacy-staff') return 'Shared staff';
  if (value === 'legacy-admin') return 'Administrator';
  return value || '—';
}

async function loadAttendance(silent=false) {
  const requestId = ++state.attendanceRequestId;
  const params = new URLSearchParams({employee_id:$('attendanceSearch').value.trim(),
    status:$('attendanceStatus').value, page:String(state.attendancePage)});
  if (!silent) $('attendanceSummary').textContent = 'Loading employees…';
  try {
    const result = await api(`/api/attendance?${params}`);
    if (requestId !== state.attendanceRequestId) return;
    const rows = $('attendanceRows'); rows.replaceChildren();
    for (const person of result.items) {
      const row = document.createElement('tr');
      const wristband = {lost:'Lost wristband', unreadable:'QR cannot scan', other:'Other'}[person.wristband_reason] || 'No issue';
      const checkin = person.checked_in_at ? new Date(person.checked_in_at).toLocaleString() : 'Not checked in';
      const recordedBy = attendanceStaffName(person.checked_in_by);
      const wristbandBy = person.wristband_recorded_by && person.wristband_recorded_by !== person.checked_in_by
        ? ` · Wristband: ${attendanceStaffName(person.wristband_recorded_by)}` : '';
      attendanceCell(row, 'Employee ID', person.employee_id);
      attendanceCell(row, 'Name', person.name || '—');
      attendanceCell(row, 'Department', person.department || '—');
      attendanceCell(row, 'Check-in', checkin);
      attendanceCell(row, 'Wristband', person.checked_in_at ? wristband : '—');
      attendanceCell(row, 'Recorded by', person.checked_in_at ? recordedBy + wristbandBy : '—');
      rows.append(row);
    }
    if (!result.items.length) {
      const row = document.createElement('tr'); row.className = 'attendance-empty';
      const cell = document.createElement('td'); cell.colSpan = 6;
      cell.textContent = 'No employees found.'; row.append(cell); rows.append(row);
    }
    const totalPages = Math.max(1, Math.ceil(result.total / result.page_size));
    $('attendanceSummary').textContent = `${Number(result.total).toLocaleString()} employee${result.total === 1 ? '' : 's'}`;
    $('attendancePage').textContent = `Page ${result.page} of ${totalPages}`;
    $('attendancePrevious').disabled = result.page <= 1;
    $('attendanceNext').disabled = result.page >= totalPages;
  } catch (error) {
    if (requestId === state.attendanceRequestId) $('attendanceSummary').textContent = error.message;
  }
}

function adminMessage(id, message, type='') {
  const node = $(id);
  node.className = `form-message ${type}`;
  node.textContent = message;
}

const adminDialogFields = {
  adminEmployeeDialog:['adminAddEmployeeId', 'adminEmployeeOperation'],
  adminActivityDialog:[null, null],
  adminAttendanceDialog:['adminAttendanceScope', 'adminAttendanceOperation'],
  adminDrawDialog:['adminDrawReason', 'adminDrawOperation'],
};

function adminDialogBusy(id) {
  return Boolean(state[adminDialogFields[id]?.[1]]);
}

function adminDialogFocusable(dialog) {
  return [...dialog.querySelectorAll('button,input,select,textarea,a[href],[tabindex]')].filter(field =>
    !field.disabled && !field.hidden && field.type !== 'hidden' && field.tabIndex !== -1 &&
    !field.closest?.('[hidden]') && (!field.getClientRects || field.getClientRects().length));
}

function focusAdminDialog(dialog) {
  const id = adminDialogFields[dialog.id][0];
  const first = id ? $(id) : null;
  (first || adminDialogFocusable(dialog)[0] || dialog).focus();
}

function excludeAdminBackground(dialog) {
  const excluded = [];
  for (let branch = dialog; branch.parentElement; branch = branch.parentElement) {
    for (const sibling of branch.parentElement.children) {
      if (sibling === branch) continue;
      excluded.push({node:sibling, hidden:sibling.getAttribute('aria-hidden'), inert:sibling.getAttribute('inert')});
      sibling.setAttribute('aria-hidden', 'true'); sibling.setAttribute('inert', '');
    }
  }
  return excluded;
}

function closeAdminDialog(force=false, restoreFocus=true) {
  const current = state.adminDialog;
  if (!current || (!force && adminDialogBusy(current.dialog.id))) return false;
  state.adminDialog = null;
  if (current.dialog.close && current.dialog.open) current.dialog.close();
  else current.dialog.removeAttribute('open');
  current.dialog.classList.remove('popup-fallback');
  const scrim = $('adminDialogScrim'); if (scrim) scrim.hidden = true;
  if (current.layer) current.layer.node.style.zIndex = current.layer.zIndex;
  for (const entry of current.excluded || []) {
    for (const [name, value] of [['aria-hidden', entry.hidden], ['inert', entry.inert]]) {
      if (value === null) entry.node.removeAttribute(name); else entry.node.setAttribute(name, value);
    }
  }
  const opener = current.opener;
  if (restoreFocus && state.view === 'admin' && state.profile?.role === 'admin' &&
      opener?.isConnected !== false && opener && !opener.hidden && !opener.closest?.('[hidden]') &&
      (!opener.getClientRects || opener.getClientRects().length)) opener.focus();
  return true;
}

function openAdminDialog(id, opener=document.activeElement) {
  if (state.profile?.role !== 'admin' || state.view !== 'admin' || !adminDialogFields[id]) return false;
  if (state.adminDialog?.dialog.id === id) return true;
  if (state.adminDialog && !closeAdminDialog(false, false)) return false;
  const dialog = $(id); if (!dialog) return false;
  if (id === 'adminDrawDialog') $('adminDrawConfirm').value = '';
  const current = {dialog, opener}; state.adminDialog = current;
  dialog.setAttribute('aria-modal', 'true');
  if (dialog.showModal) dialog.showModal();
  else {
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('open', ''); dialog.classList.add('popup-fallback');
    const scrim = $('adminDialogScrim'); if (scrim) scrim.hidden = false;
    const view = $('view-admin');
    if (view?.style) { current.layer = {node:view, zIndex:view.style.zIndex}; view.style.zIndex = '10001'; }
    current.excluded = excludeAdminBackground(dialog);
  }
  focusAdminDialog(dialog);
  return true;
}

function setAdminDialogBusy(id, pending) {
  const dialog = $(id);
  dialog?.setAttribute?.('aria-busy', String(pending));
  dialog?.querySelector?.('[data-admin-close]')?.setAttribute('aria-disabled', String(pending));
}

function handleAdminDialogKey(event) {
  const dialog = state.adminDialog?.dialog;
  if (!dialog || !dialog.classList.contains('popup-fallback')) return;
  if (event.key === 'Escape') { event.preventDefault(); closeAdminDialog(); }
  if (event.key !== 'Tab') return;
  const fields = adminDialogFocusable(dialog);
  const last = fields[fields.length - 1];
  const next = event.shiftKey ? last : fields[0];
  if (!fields.length || !dialog.contains(document.activeElement) ||
      document.activeElement === (event.shiftKey ? fields[0] : last)) {
    event.preventDefault(); (next || dialog).focus();
  }
}

function initializeAdminDialogs() {
  document.querySelectorAll('[data-admin-dialog]').forEach(button =>
    button.addEventListener('click', () => openAdminDialog(button.dataset.adminDialog, button)));
  document.querySelectorAll('.admin-dialog').forEach(dialog => {
    dialog.querySelector('[data-admin-close]')?.addEventListener('click', () => closeAdminDialog());
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeAdminDialog(); });
    dialog.addEventListener('close', () => {
      if (state.adminDialog?.dialog === dialog && !dialog.open && !dialog.hasAttribute('open')) closeAdminDialog(true);
    });
  });
  document.addEventListener('keydown', handleAdminDialogKey, true);
  document.addEventListener('focusin', event => {
    const dialog = state.adminDialog?.dialog;
    if (dialog?.classList.contains('popup-fallback') && !dialog.contains(event.target)) focusAdminDialog(dialog);
  }, true);
  document.addEventListener('click', event => {
    const dialog = state.adminDialog?.dialog;
    if (dialog?.classList.contains('popup-fallback') && !dialog.contains(event.target)) {
      event.preventDefault(); event.stopPropagation(); focusAdminDialog(dialog);
    }
  }, true);
}

function resetAdminDialogs() {
  closeAdminDialog(true, false);
  resetAdminEmployeePending();
  for (const kind of ['attendance', 'draw']) {
    const key = kind === 'attendance' ? 'adminAttendanceOperation' : 'adminDrawOperation';
    if (!state[key]) continue;
    state[key] = null; setAdminRemovalBusy(kind, false);
  }
}

function adminRow(title, detail, button) {
  const row = document.createElement('div'); row.className = 'admin-row';
  const text = document.createElement('div');
  const name = document.createElement('strong'); name.textContent = title;
  const meta = document.createElement('span'); meta.textContent = detail;
  text.append(name, meta); row.append(text);
  if (button) row.append(button);
  return row;
}

async function refreshAdmin() {
  if (state.profile?.role !== 'admin') return;
  if (state.adminLoading) { state.adminRefreshPending = true; return; }
  state.adminLoading = true;
  try {
    const [stats, activity, drawHistory] = await Promise.all([api('/api/stats'), api('/api/admin/activity'), api('/api/admin/draw-history')]);
    const newCount = Number(stats.checked_in ?? 0);
    state.adminChecked = newCount;
    const newDrawCount = Number(drawHistory.count);
    if (state.adminDrawCount !== null && state.adminDrawCount !== newDrawCount && $('adminDrawConfirm').value) {
      $('adminDrawConfirm').value = '';
      adminMessage('adminDrawMessage', 'The winner count changed. Check it again.', 'warning');
    }
    state.adminDrawCount = newDrawCount;
    $('adminDrawCount').textContent = newDrawCount.toLocaleString();
    $('adminTotal').textContent = Number(stats.total_employees ?? 0).toLocaleString();
    $('adminChecked').textContent = state.adminChecked.toLocaleString();
    $('adminActive').textContent = activity.items.reduce((total, item) => total + Number(item.sessions || 0), 0).toLocaleString();
    $('adminHealth').textContent = 'Online';
    $('adminStatus').className = 'admin-status';
    $('adminStatus').textContent = `Updated ${new Date().toLocaleTimeString()}`;
    const activityHost = $('adminActivity'); activityHost.replaceChildren();
    if (!activity.items.length) activityHost.textContent = 'No active staff.';
    activity.items.forEach(item => {
      const title = item.username === 'legacy-admin' ? 'Administrator' :
        item.username === 'legacy-staff' ? 'Shared staff access' : item.display_name || item.username;
      const count = Number(item.sessions || 0);
      activityHost.append(adminRow(title,
        `${count} browser session${count === 1 ? '' : 's'} · Last active ${new Date(item.last_seen).toLocaleTimeString()}`));
    });
  } catch (error) {
    state.adminChecked = null; state.adminDrawCount = null; $('adminHealth').textContent = 'Unavailable';
    $('adminStatus').className = 'admin-status error';
    $('adminStatus').textContent = 'Cannot load data. Try again.';
    throw error;
  } finally {
    state.adminLoading = false;
    if (state.adminRefreshPending) {
      state.adminRefreshPending = false;
      if (state.profile?.role === 'admin' && state.view === 'admin') refreshAdmin().catch(() => {});
    }
  }
}

function updateAttendanceScope() {
  const all = $('adminAttendanceScope').value === 'all';
  $('adminEmployeeLabel').hidden = all; $('adminEmployeeId').required = !all;
  $('adminAttendanceSubmit').textContent = all ? 'Remove all check-ins' : 'Remove check-in';
}

function updateAdminEmployeeDraw() {
  const manager = /\bmanager\b|\bmanaging\s+director\b/i.test($('adminAddEmployeePosition').value);
  const draw = $('adminAddEmployeeDraw');
  if (manager) draw.value = 'no';
  draw.disabled = manager || Boolean(state.adminEmployeeOperation);
}

function setAdminEmployeeBusy(pending) {
  setAdminDialogBusy('adminEmployeeDialog', pending);
  $('adminAddEmployeeForm').setAttribute('aria-busy', String(pending));
  for (const field of ['Id','Name','Department','Position']) {
    $('adminAddEmployee' + field).readOnly = pending;
  }
  const button = $('adminAddEmployeeSubmit');
  button.disabled = false;
  button.setAttribute('aria-disabled', String(pending));
  button.textContent = pending ? 'Adding...' : 'Add employee';
  updateAdminEmployeeDraw();
}

function resetAdminEmployeePending() {
  if (!state.adminEmployeeOperation) return;
  state.adminEmployeeOperation = null;
  setAdminEmployeeBusy(false);
  adminMessage('adminAddEmployeeMessage', '');
}

async function addAdminEmployee(event) {
  event.preventDefault();
  if (state.profile?.role !== 'admin' || state.adminEmployeeOperation) return;
  const form = event.currentTarget;
  if (form.reportValidity && !form.reportValidity()) return;
  const profile = state.profile;
  const operation = {};
  const current = () => state.adminEmployeeOperation === operation && state.profile === profile;
  const data = {
    employee_id:$('adminAddEmployeeId').value.trim(),
    name:$('adminAddEmployeeName').value.trim(),
    department:$('adminAddEmployeeDepartment').value.trim(),
    position:$('adminAddEmployeePosition').value.trim(),
    eligible_for_draw:$('adminAddEmployeeDraw').value === 'yes',
  };
  state.adminEmployeeOperation = operation;
  setAdminEmployeeBusy(true);
  adminMessage('adminAddEmployeeMessage', 'Adding...');
  try {
    const result = await api('/api/admin/employees', {method:'POST', data});
    if (!current()) return;
    form.reset();
    adminMessage('adminAddEmployeeMessage', result.eligible_for_draw ? 'Employee added.' :
      'Employee added. Cannot win.', 'success');
    if (state.view === 'admin') $('adminAddEmployeeId').focus();
    refreshAdmin().catch(() => {});
  } catch (error) {
    if (current()) adminMessage('adminAddEmployeeMessage', error.message, 'error');
  } finally {
    if (state.adminEmployeeOperation === operation) {
      state.adminEmployeeOperation = null;
      setAdminEmployeeBusy(false);
    }
  }
}

async function removeAdminAttendance(event) {
  return removeAdminRecords(event, 'attendance');
}

async function removeAdminDrawHistory(event) {
  return removeAdminRecords(event, 'draw');
}

function setAdminRemovalBusy(kind, pending) {
  const attendance = kind === 'attendance';
  const prefix = attendance ? 'adminAttendance' : 'adminDraw';
  const fields = attendance ? ['adminAttendanceScope','adminEmployeeId','adminAttendanceReason'] :
    ['adminDrawReason','adminDrawConfirm'];
  for (const id of fields) {
    if (id === 'adminAttendanceScope') $(id).disabled = pending;
    else $(id).readOnly = pending;
  }
  $(prefix + 'Form').setAttribute('aria-busy', String(pending));
  const button = $(prefix + 'Submit');
  button.disabled = false; button.setAttribute('aria-disabled', String(pending));
  button.textContent = pending ? 'Removing...' : attendance ?
    ($('adminAttendanceScope').value === 'all' ? 'Remove all check-ins' : 'Remove check-in') : 'Clear winners';
  setAdminDialogBusy(attendance ? 'adminAttendanceDialog' : 'adminDrawDialog', pending);
}

async function removeAdminRecords(event, kind) {
  event.preventDefault();
  const attendance = kind === 'attendance';
  const key = attendance ? 'adminAttendanceOperation' : 'adminDrawOperation';
  if (state.profile?.role !== 'admin' || state[key]) return;
  const form = event.currentTarget;
  if (form.reportValidity && !form.reportValidity()) return;
  const count = attendance ? state.adminChecked : state.adminDrawCount;
  const messageId = attendance ? 'adminAttendanceMessage' : 'adminDrawMessage';
  if (count === null) return adminMessage(messageId, 'Refresh the count first.', 'error');
  const data = attendance ? {
    scope:$('adminAttendanceScope').value, employee_id:$('adminEmployeeId').value.trim(),
    reason:$('adminAttendanceReason').value.trim(),
    confirmation:$('adminAttendanceScope').value === 'all' ? 'RESET ALL' : 'REMOVE', expected_count:count,
  } : {reason:$('adminDrawReason').value.trim(), confirmation:$('adminDrawConfirm').value.trim(), expected_count:count};
  const profile = state.profile, operation = {};
  const current = () => state[key] === operation && state.profile === profile;
  state[key] = operation; setAdminRemovalBusy(kind, true); adminMessage(messageId, 'Removing...');
  try {
    const result = await api(attendance ? '/api/admin/attendance/remove' : '/api/admin/draw-history/remove', {method:'POST', data});
    if (!current()) return;
    form.reset();
    if (attendance) updateAttendanceScope();
    const noun = attendance ? 'check-in' : 'winner record';
    adminMessage(messageId, `${result.removed} ${noun}${result.removed === 1 ? '' : 's'} removed.`, 'success');
    refreshAdmin().catch(() => {}); refreshStats().catch(() => {});
  } catch (error) { if (current()) adminMessage(messageId, error.message, 'error'); }
  finally {
    if (state[key] === operation) { state[key] = null; setAdminRemovalBusy(kind, false); }
  }
}

async function loadPrizeList() {
  const select = $('prizeSelect');
  const previous = select.value;
  const config = await api('/api/draw/config');
  select.replaceChildren();
  const placeholder = document.createElement('option'); placeholder.value = '';
  placeholder.textContent = 'Select a reward'; select.append(placeholder);
  config.prizes.filter(prize => prize.active).forEach(prize => {
    const option = document.createElement('option'); option.value = String(prize.id);
    option.textContent = `${prize.name} (${prize.remaining} remaining)`;
    option.dataset.prizeName = prize.name; option.disabled = prize.remaining <= 0;
    select.append(option);
  });
  if ([...select.options].some(option => option.value === previous && !option.disabled)) select.value = previous;
  if (select.options.length === 1) placeholder.textContent = 'No rewards yet — ask the admin to add one';
  await loadRewards();
}

let rewardData = {drafts:[], prizes:[]};
function resetRewardForm() {
  $('rewardForm').reset(); $('rewardDraftId').value = ''; $('rewardSourceId').value = '';
  $('rewardQuantity').value = 1; $('rewardAdd').textContent = 'Add to list'; $('rewardCancel').hidden = true;
}
function rewardEdit(item, isDraft) {
  $('rewardSetup').open = true;
  $('rewardDraftId').value = isDraft ? item.id : '';
  $('rewardSourceId').value = isDraft ? (item.source_id || '') : item.id;
  $('rewardName').value = item.name; $('rewardQuantity').value = item.quantity;
  $('rewardImage').value = ''; $('rewardRemoveImage').checked = false;
  $('rewardAdd').textContent = 'Save change'; $('rewardCancel').hidden = false; $('rewardName').focus();
}
function rewardRow(item, isDraft) {
  const row = document.createElement('div'); row.className = 'reward-row';
  const detail = document.createElement('span');
  detail.textContent = `${item.name} · ${item.quantity}${item.action === 'remove' ? ' · To delete' : ''}${item.drawn ? ` · ${item.drawn} awarded` : ''}`;
  if (item.has_image) { const img = document.createElement('img'); img.className = 'reward-thumbnail'; img.alt = ''; img.src = `/api/rewards/image?${isDraft ? 'draft' : 'id'}=${encodeURIComponent(item.id)}&v=${Date.now()}`; detail.prepend(img); }
  const actions = document.createElement('div');
  const pending = !isDraft && rewardData.drafts.find(draft => Number(draft.source_id) === Number(item.id));
  const edit = document.createElement('button'); edit.type = 'button'; edit.textContent = pending ? 'Edit draft' : 'Edit';
  edit.addEventListener('click', () => rewardEdit(pending || item, Boolean(pending) || isDraft));
  const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = isDraft ? 'Remove' : 'Delete';
  if (!isDraft && (item.drawn || pending)) { remove.disabled = true; remove.title = pending ? 'Remove or submit the existing draft first.' : 'Rewards with winners cannot be deleted.'; }
  remove.addEventListener('click', async () => {
    if (!isDraft && !confirm(`Remove ${item.name} from the projector when you submit?`)) return;
    try {
      if (isDraft) await api('/api/rewards/draft/delete', {method:'POST', data:{id:item.id}});
      else await api('/api/rewards/draft', {method:'POST', data:{source_id:item.id,name:item.name,quantity:item.quantity,action:'remove'}});
      resetRewardForm(); await loadRewards();
    } catch (error) { $('rewardMessage').textContent = error.message; }
  });
  actions.append(edit, remove); row.append(detail, actions); return row;
}
async function loadRewards() {
  rewardData = await api('/api/rewards');
  const draft = $('rewardDraftList'), published = $('rewardPublishedList');
  draft.replaceChildren(); published.replaceChildren();
  rewardData.drafts.forEach(item => draft.append(rewardRow(item, true)));
  rewardData.prizes.forEach(item => published.append(rewardRow(item, false)));
  if (!rewardData.prizes.length) published.textContent = 'No rewards yet.';
  $('rewardCount').textContent = `${rewardData.prizes.length} reward${rewardData.prizes.length === 1 ? '' : 's'}`;
  $('rewardSetupSummary').textContent = rewardData.drafts.length ? `${rewardData.drafts.length} pending` : '';
  $('rewardPublish').disabled = !rewardData.drafts.length;
}
async function addRewardDraft(event) {
  event.preventDefault(); const button = $('rewardAdd'); button.disabled = true;
  try {
    const file = $('rewardImage').files[0];
    if (file && (file.size > 4 * 1024 * 1024 || !['image/png','image/jpeg','image/webp'].includes(file.type))) throw new Error('Use PNG, JPG or WebP, up to 4 MB.');
    const dataUrl = file ? await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('Cannot read image.')); reader.readAsDataURL(file); }) : undefined;
    await api('/api/rewards/draft', {method:'POST', data:{id:$('rewardDraftId').value || undefined, source_id:$('rewardSourceId').value || undefined, name:$('rewardName').value.trim(), quantity:Number($('rewardQuantity').value), data_url:dataUrl, remove_image:$('rewardRemoveImage').checked}});
    resetRewardForm(); await loadRewards(); $('rewardMessage').textContent = 'Draft saved.';
  } catch (error) { $('rewardMessage').textContent = error.message; }
  finally { button.disabled = false; }
}
async function publishRewards() {
  const button = $('rewardPublish'); button.disabled = true;
  try {
    await api('/api/rewards/publish', {method:'POST', data:{ids:rewardData.drafts.map(item => item.id)}});
    resetRewardForm(); await loadPrizeList(); $('rewardMessage').textContent = 'Rewards saved.';
  } catch (error) { $('rewardMessage').textContent = error.message; await loadRewards().catch(() => {}); }
}

function initials(name) {
  return String(name || 'Employee').trim().split(/\s+/).slice(0,2).map(part => part[0]).join('').toUpperCase();
}

function setMessage(workspace, message, type='') {
  workspace.message.textContent = message || '';
  workspace.message.className = `scan-message form-message ${type}`;
}

function setOutcome(workspace, type='', title='', detail='') {
  workspace.outcome.hidden = !type;
  workspace.outcome.className = `scan-outcome ${type}`;
  workspace.outcome.querySelector('strong').textContent = title;
  workspace.outcome.querySelector('span').textContent = detail;
}

function employeePopupOpen(workspace) {
  return Boolean(workspace.popup && (workspace.popup.open || workspace.popup.hasAttribute?.('open')));
}

function clearCheckinFeedback(workspace) {
  if (workspace.successFeedbackTimer != null) window.clearTimeout(workspace.successFeedbackTimer);
  workspace.successFeedbackTimer = null;
  workspace.successFeedbackRun = null;
  if (workspace.successFeedback) workspace.successFeedback.hidden = true;
}

function showCheckinFeedback(workspace) {
  clearCheckinFeedback(workspace);
  const feedback = workspace.successFeedback;
  if (!feedback) return;
  const run = {};
  workspace.successFeedbackRun = run;
  // Resolve the hidden state so each confirmed save starts a fresh CSS animation.
  void feedback.offsetWidth;
  feedback.hidden = false;
  workspace.successFeedbackTimer = window.setTimeout(() => {
    if (workspace.successFeedbackRun !== run) return;
    clearCheckinFeedback(workspace);
  }, 900);
}

function showCheckinDialog(workspace, kind, person=null, note='', title='') {
  clearCheckinFeedback(workspace);
  const modal = workspace.modal;
  if (!modal) return;
  modal.className = `checkin-modal ${kind}`;
  modal.querySelector('.modal-status').textContent = {
    found:'READY TO CHECK IN', checked:'ALREADY CHECKED IN', success:'CHECK-IN SAVED', notfound:'NOT FOUND', error:'ERROR'
  }[kind];
  modal.querySelector('.modal-name').textContent = title || person?.name || 'Employee';
  window.EventDeskPortrait?.show(modal.querySelector('.employee-portrait'),
    kind === 'found' || kind === 'checked' ? person : null);
  const id = modal.querySelector('.modal-id');
  id.textContent = person?.employee_id ? `ID ${person.employee_id}` : '';
  id.hidden = !person?.employee_id;
  const department = modal.querySelector('.modal-department');
  department.textContent = person?.department || 'No department';
  department.hidden = !person;
  modal.querySelector('.employee-detail-meta').hidden = !person;
  const noteField = modal.querySelector('.modal-note');
  noteField.textContent = note;
  noteField.hidden = !note;
  workspace.modalMessage.textContent = '';
  workspace.modalReasonLabel.hidden = kind !== 'found' || !workspace.lookup?.employee_id;
  workspace.modalReasonSelect.value = kind === 'found' && workspace.lookup?.employee_id ? 'unreadable' : '';
  workspace.modalDetail.value = '';
  workspace.modalDetailLabel.hidden = true;
  workspace.modalSubmit.hidden = kind !== 'found';
  workspace.modalSubmit.textContent = 'Check in';
  workspace.modalSubmit.disabled = false;
  workspace.modalCancel.textContent = kind === 'found' ? 'Skip' : kind === 'checked' ? 'Close' : 'Try another scan';
  workspace.reviewPending = kind !== 'checked';
  workspace.checkedPreview = kind === 'checked';
  workspace.popup?.setAttribute?.('aria-label', kind === 'checked' ? 'Employee information' :
    kind === 'found' ? 'Confirm employee check-in' : 'Scan needs attention');
  workspace.popupClose?.setAttribute?.('aria-label', kind === 'found' ? 'Skip employee' :
    kind === 'checked' ? 'Close employee information' : 'Close scan result');
  if (workspace.popupTitle) workspace.popupTitle.textContent = kind === 'found' ? 'Employee found' :
    kind === 'checked' ? 'Employee information' : 'Scan needs attention';
  modal.hidden = false;
  if (workspace.controlPlaceholder) workspace.controlPlaceholder.hidden = true;
  workspace.controlLive.className = `checkin-controls-live ${kind}`;
  workspace.controlLive.hidden = false;
  setCheckinSaving(workspace, false);
  if (workspace.popup && !employeePopupOpen(workspace)) {
    if (workspace.popup.showModal) workspace.popup.showModal();
    else {
      workspace.popup.setAttribute('open', '');
      workspace.popup.classList?.add('popup-fallback');
      if (workspace.popupScrim) workspace.popupScrim.hidden = false;
      const view = workspace.popup.closest?.('.view') || workspace.root.closest?.('.view');
      if (view?.style) {
        workspace.popupLayer = {view, zIndex:view.style.zIndex};
        view.style.zIndex = '10001';
      }
    }
  }
  // Alternating names restarts CSS motion for a new result in an already-open dialog.
  workspace.popupEntrance = workspace.popupEntrance === 'a' ? 'b' : 'a';
  workspace.popup?.setAttribute?.('data-entrance', workspace.popupEntrance);
  workspace.popupScrim?.setAttribute?.('data-entrance', workspace.popupEntrance);
  (kind === 'found' ? workspace.modalSubmit : workspace.modalCancel).focus();
}

function resetWorkspace(workspace, focus=true) {
  clearCheckinFeedback(workspace);
  window.clearTimeout(workspace.nextScanTimer);
  workspace.nextScanTimer = null;
  workspace.scanSequence += 1;
  workspace.lookupRequestId = (workspace.lookupRequestId || 0) + 1;
  workspace.lookupPending = false;
  workspace.actionPending = false;
  workspace.saveOperation = null;
  workspace.scanBusy = false;
  workspace.reviewPending = false;
  workspace.checkedPreview = false;
  workspace.person = null; workspace.lookup = null;
  workspace.empty.hidden = false; workspace.result.hidden = true;
  workspace.codeInput.value = ''; workspace.idInput.value = '';
  workspace.mobileActions.hidden = true;
  workspace.mobileClear.disabled = false;
  workspace.mobileSubmit.disabled = false;
  if (workspace.modal) workspace.modal.hidden = true;
  window.EventDeskPortrait?.reset(workspace.modal?.querySelector('.employee-portrait'));
  if (workspace.controlLive) workspace.controlLive.hidden = true;
  if (workspace.controlPlaceholder) workspace.controlPlaceholder.hidden = false;
  if (workspace.modalMessage) workspace.modalMessage.textContent = '';
  setCheckinSaving(workspace, false);
  if (employeePopupOpen(workspace)) {
    if (workspace.popup.close) workspace.popup.close();
    else workspace.popup.removeAttribute('open');
  }
  if (workspace.popupScrim) workspace.popupScrim.hidden = true;
  if (workspace.popupLayer) {
    workspace.popupLayer.view.style.zIndex = workspace.popupLayer.zIndex;
    workspace.popupLayer = null;
  }
  workspace.confirm.disabled = false;
  setMessage(workspace, '');
  setOutcome(workspace);
  if (focus) {
    if (!matchMedia('(pointer:coarse)').matches) workspace.codeInput.focus();
    else workspace.cameraButton?.focus?.();
  }
}

function dismissCheckin(workspace) {
  if (workspace.actionPending) return false;
  resetWorkspace(workspace);
  return true;
}

function setCheckinSaving(workspace, pending) {
  workspace.popup?.setAttribute?.('aria-busy', String(pending));
  if (workspace.modalReasonSelect) workspace.modalReasonSelect.disabled = pending;
  if (workspace.modalDetail) workspace.modalDetail.disabled = pending;
  for (const button of [workspace.modalSubmit, workspace.modalCancel, workspace.popupClose]) {
    if (!button) continue;
    button.disabled = false;
    button.setAttribute?.('aria-disabled', String(pending));
  }
  if (pending) {
    workspace.modalSubmit.textContent = 'Saving…';
    workspace.modalMessage.textContent = 'Saving...';
  }
}

function finishCheckin(workspace, person, kind='success', detail='') {
  const restoreFocus = Boolean(employeePopupOpen(workspace) || workspace.reviewPending);
  if (kind === 'checked') {
    workspace.actionPending = false;
    workspace.lookupPending = false;
    workspace.saveOperation = null;
    workspace.scanBusy = false;
    workspace.codeInput.value = ''; workspace.idInput.value = '';
    workspace.mobileActions.hidden = true;
    if (workspace.receipt) workspace.receipt.hidden = true;
    renderPerson(workspace, person);
    showCheckinDialog(workspace, 'checked', person, detail);
    setMessage(workspace, '');
  } else resetWorkspace(workspace, restoreFocus);
  if (workspace.announcement) {
    workspace.announcement.textContent = `${person.name || 'Employee'}, ID ${person.employee_id}, ${kind === 'checked' ? 'already checked in' : 'check-in saved'}.${detail ? ` ${detail}` : ''}`;
  }
  if (kind === 'checked') return;
  if (kind === 'success') showCheckinFeedback(workspace);
  const receipt = workspace.receipt;
  if (!receipt) return setMessage(workspace, `${person.name || 'Employee'} · ${person.employee_id}: ${detail || (kind === 'checked' ? 'Already checked in' : 'Check-in saved')}`, 'success');
  receipt.className = `scan-receipt ${kind}`;
  receipt.querySelector('.receipt-status').textContent = kind === 'checked' ? 'Already checked in' : 'Check-in saved';
  receipt.querySelector('.receipt-name').textContent = person.name || 'Employee';
  receipt.querySelector('.receipt-detail').textContent = `ID ${person.employee_id} · ${person.department || 'No department'}${detail ? ` · ${detail}` : ''}`;
  receipt.hidden = false;
}

function scheduleNextScan(workspace, delay=2400) {
  window.clearTimeout(workspace.nextScanTimer);
  const sequence = workspace.scanSequence;
  workspace.nextScanTimer = window.setTimeout(() => {
    if (sequence !== workspace.scanSequence) return;
    workspace.nextScanTimer = null;
    resetWorkspace(workspace);
    workspace.scanBusy = false;
    if (workspace.scanner?.isScanning) setMessage(workspace, '');
  }, delay);
}

function renderPerson(workspace, person) {
  workspace.person = person;
  workspace.empty.hidden = true; workspace.result.hidden = false;
  workspace.root.querySelector('.avatar').textContent = initials(person.name);
  workspace.root.querySelector('.employee-id').textContent = `Employee ID ${person.employee_id}`;
  workspace.root.querySelector('.employee-name').textContent = person.name || 'Employee';
  workspace.root.querySelector('.department').textContent = person.department || 'No department';
  const checked = workspace.root.querySelector('.checkin-state');
  checked.textContent = person.checked_in_at ? 'Checked in' : 'Not checked in';
  checked.classList.toggle('done', Boolean(person.checked_in_at));
  workspace.confirm.textContent = person.checked_in_at ? 'Already checked in' : 'Submit check-in';
  workspace.confirm.disabled = Boolean(person.checked_in_at);
}

async function findEmployee(workspace, lookup) {
  if (workspace.reviewPending || workspace.lookupPending || workspace.actionPending) return null;
  if (workspace.checkedPreview && workspace.lookup &&
      ((lookup.code && lookup.code === workspace.lookup.code) ||
       (lookup.employee_id && lookup.employee_id === workspace.person?.employee_id))) {
    workspace.scanBusy = false;
    return null;
  }
  workspace.lookupPending = true;
  const lookupRequestId = workspace.lookupRequestId = (workspace.lookupRequestId || 0) + 1;
  window.clearTimeout(workspace.nextScanTimer);
  workspace.nextScanTimer = null;
  workspace.scanSequence += 1;
  workspace.person = null; workspace.lookup = null;
  workspace.empty.hidden = false; workspace.result.hidden = true;
  workspace.mobileActions.hidden = true;
  setOutcome(workspace);
  setMessage(workspace, 'Looking up employee…');
  try {
    const payload = await api('/api/lookup', {method:'POST', data:lookup});
    if (workspace.lookupRequestId !== lookupRequestId) return null;
    workspace.lookup = payload.lookup || lookup;
    renderPerson(workspace, payload.person);
    if (workspace.mode === 'checkin') {
      const checked = Boolean(payload.person.checked_in_at);
      const noDraw = ['lost','other'].includes(payload.person.checkin_wristband_reason);
      if (checked) {
        finishCheckin(workspace, payload.person, 'checked', noDraw ?
          'Not in draw.' : '');
        return payload;
      }
      workspace.scanBusy = true;
      showCheckinDialog(workspace, 'found', payload.person);
      workspace.mobileActions.hidden = true;
      workspace.mobileSubmit.disabled = false;
      setMessage(workspace, '');
      return payload;
    }
    if (!payload.person.checked_in_at) {
      setOutcome(workspace, 'warning', 'NOT CHECKED IN',
        `${payload.person.name || 'Employee'} · ${payload.person.employee_id} · ${payload.person.department || 'No department'}`);
      setMessage(workspace, 'Not checked in.', 'warning');
      return payload;
    }
    const drawNote = ['lost','other'].includes(payload.person.checkin_wristband_reason)
      ? ' · Not eligible for lucky wheel' : '';
    setOutcome(workspace, 'pass', 'CHECKED IN',
      `${payload.person.name || 'Employee'} · ${payload.person.employee_id} · ${payload.person.department || 'No department'}${drawNote}`);
    setMessage(workspace, '', 'success');
    return payload;
  } catch (error) {
    if (workspace.lookupRequestId !== lookupRequestId) return null;
    resetWorkspace(workspace);
    if (workspace.mode === 'checkin') {
      const missing = ['unknown_token','employee_not_found'].includes(error.code);
      const invalidQr = Boolean(lookup.code && error.status === 400);
      showCheckinDialog(workspace, missing || invalidQr ? 'notfound' : 'error', null,
        missing ? (lookup.employee_id ? 'Check the employee ID.' : 'Scan again.') : error.message,
        missing ? 'Employee not found' : (invalidQr ? 'Invalid QR code' : 'Unable to continue'));
      workspace.scanBusy = true;
      return null;
    }
    if (lookup.code && (error.status === 400 || ['unknown_token', 'employee_not_found', 'revoked_wristband', 'replaced_wristband'].includes(error.code))) {
      if (typeof matchMedia === 'function' && matchMedia('(max-width:640px)').matches) {
        setOutcome(workspace);
        setMessage(workspace, error.message || 'Employee not found.', 'error');
      } else {
        setOutcome(workspace, 'error', 'INVALID QR CODE', error.message);
        setMessage(workspace, '');
      }
    } else if (error.code === 'employee_not_found') {
      if (typeof matchMedia === 'function' && matchMedia('(max-width:640px)').matches) {
        setOutcome(workspace);
        setMessage(workspace, error.message || 'Employee not found.', 'error');
      } else {
        setOutcome(workspace, 'error', 'EMPLOYEE NOT FOUND', 'Check the employee ID.');
        setMessage(workspace, '');
      }
    } else {
      setMessage(workspace, error.message, 'error');
    }
    if (lookup.code && workspace.scanner?.isScanning && workspace.mode === 'verify') {
      workspace.scanBusy = true;
      scheduleNextScan(workspace, 3200);
    }
    return null;
  } finally {
    if (workspace.lookupRequestId === lookupRequestId) workspace.lookupPending = false;
  }
}

async function confirmAction(workspace) {
  if (workspace.mode !== 'checkin' || !workspace.person || !workspace.lookup || workspace.actionPending) return;
  if (workspace.person.checked_in_at) return;
  const sequence = workspace.scanSequence;
  const requestId = workspace.lookupRequestId;
  const lookup = {...workspace.lookup};
  const operation = {};
  const current = () => workspace.saveOperation === operation &&
    workspace.scanSequence === sequence && workspace.lookupRequestId === requestId;
  const manual = Boolean(workspace.lookup.employee_id);
  const reason = manual ? workspace.modalReasonSelect.value : null;
  if (manual && !reason) {
    if (workspace.modalMessage) workspace.modalMessage.textContent = 'Choose a wristband status.';
    workspace.modalReasonSelect.focus();
    return;
  }
  const detail = reason === 'other' ? workspace.modalDetail.value.trim() : '';
  if (reason === 'other' && !detail) {
    if (workspace.modalMessage) workspace.modalMessage.textContent = 'Enter a reason.';
    return;
  }
  window.clearTimeout(workspace.nextScanTimer);
  workspace.nextScanTimer = null;
  workspace.saveOperation = operation;
  workspace.actionPending = true;
  workspace.confirm.disabled = true;
  workspace.mobileSubmit.disabled = true;
  workspace.mobileClear.disabled = true;
  setCheckinSaving(workspace, true);
  try {
    const payload = await api('/api/scan/checkin', {method:'POST', data:{...lookup,
      ...(reason ? {wristband_reason:reason} : {}), ...(detail ? {wristband_detail:detail} : {})}});
    if (!current()) return payload;
    const repeated = payload.state === 'already_done';
    finishCheckin(workspace, payload.person, repeated ? 'checked' : 'success',
      ['lost','other'].includes(reason) ? 'Not in draw.' : '');
    return payload;
  } catch (error) {
    if (!current()) return null;
    setMessage(workspace, '');
    workspace.modalMessage.textContent = error.message;
    workspace.confirm.disabled = false;
    workspace.mobileSubmit.disabled = false;
    workspace.modalSubmit.textContent = 'Check in';
    return null;
  } finally {
    if (workspace.saveOperation === operation) {
      workspace.saveOperation = null;
      workspace.actionPending = false;
      workspace.mobileClear.disabled = false;
      setCheckinSaving(workspace, false);
    }
  }
}

function photoCameraRequired() {
  return !window.isSecureContext && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1';
}

function openQrPhoto(workspace) {
  if (workspace.photoDecoding || workspace.scanBusy || workspace.lookupPending || workspace.actionPending) return;
  workspace.photoRequest = {generation:workspace.cameraGeneration || 0, sequence:workspace.scanSequence || 0};
  workspace.photoInput.value = '';
  workspace.photoInput.click();
}

async function scanQrPhoto(workspace, file) {
  if (!file || workspace.photoDecoding || workspace.scanBusy || workspace.lookupPending || workspace.actionPending) return;
  const request = workspace.photoRequest || {generation:workspace.cameraGeneration || 0, sequence:workspace.scanSequence || 0};
  const isCurrent = () => request.generation === (workspace.cameraGeneration || 0) && request.sequence === (workspace.scanSequence || 0);
  if (!isCurrent()) return;
  if ((file.type && !file.type.startsWith('image/')) || !file.size || file.size > 20 * 1024 * 1024) {
    return setMessage(workspace, 'Use a photo under 20 MB.', 'error');
  }
  workspace.photoDecoding = true;
  workspace.cameraButton.disabled = true;
  setMessage(workspace, 'Reading QR photo…');
  try {
    if (!workspace.scanner) workspace.scanner = new Html5Qrcode(workspace.reader.id, {formatsToSupport:[Html5QrcodeSupportedFormats.QR_CODE]});
    const code = await workspace.scanner.scanFile(file, false);
    if (!isCurrent() || workspace.scanBusy || workspace.lookupPending || workspace.actionPending) return;
    if (typeof code !== 'string' || !code.trim()) throw new Error('No QR code found.');
    workspace.scanBusy = true;
    workspace.codeInput.value = code;
    await findEmployee(workspace, {code});
    if (workspace.mode === 'verify') workspace.scanBusy = false;
  } catch (_) {
    if (isCurrent()) {
      workspace.scanBusy = false;
      setMessage(workspace, 'QR not found. Try another photo.', 'error');
    }
  } finally {
    workspace.photoDecoding = false;
    workspace.photoRequest = null;
    workspace.photoInput.value = '';
    workspace.cameraButton.disabled = false;
  }
}

async function startCamera(workspace) {
  if (!window.Html5Qrcode) return setMessage(workspace, 'Camera is unavailable. Enter the employee ID.', 'error');
  if (photoCameraRequired()) return openQrPhoto(workspace);
  const generation = workspace.cameraGeneration = (workspace.cameraGeneration || 0) + 1;
  workspace.detailScannerStop?.();
  workspace.detailScannerStop = null;
  window.EventDeskCamera?.reset(workspace);
  workspace.cameraButton.disabled = true;
  try {
    if (!workspace.scanner) workspace.scanner = new Html5Qrcode(workspace.reader.id, {
      formatsToSupport: [Html5QrcodeSupportedFormats.QR_CODE],
    });
    const cameras = await Html5Qrcode.getCameras();
    if (generation !== workspace.cameraGeneration) return;
    if (!cameras.length) throw new Error('No camera found.');
    const selected = window.EventDeskCamera?.selectCamera(cameras) ||
      cameras.find(camera => /back|rear|environment/i.test(camera.label)) || cameras[cameras.length - 1];
    const onScan = async code => {
      const now = Date.now();
      if (generation !== workspace.cameraGeneration || workspace.scanBusy || workspace.lookupPending ||
          workspace.actionPending || (workspace.lastScanCode === code && now - workspace.lastScanAt < 5000)) return;
      workspace.scanBusy = true;
      workspace.lastScanCode = code;
      workspace.lastScanAt = now;
      workspace.scanSequence += 1;
      workspace.codeInput.value = code;
      await findEmployee(workspace, {code});
      if (workspace.mode === 'verify') workspace.scanBusy = false;
    };
    const videoConstraints = window.EventDeskCamera?.constraints(selected.id);
    try {
      await workspace.scanner.start(selected.id, {fps:8, ...(videoConstraints ? {videoConstraints} : {})}, onScan, () => {});
    } catch (error) {
      const constraintError = ['OverconstrainedError', 'ConstraintNotSatisfiedError'].includes(error?.name) ||
        (typeof error === 'string' && /^Error getting userMedia, error = (OverconstrainedError|ConstraintNotSatisfiedError)(?::|\b)/.test(error));
      if (!videoConstraints || !constraintError ||
          generation !== workspace.cameraGeneration) throw error;
      await workspace.scanner.start(selected.id, {fps:8}, onScan, () => {});
    }
    if (generation === workspace.cameraGeneration) await window.EventDeskCamera?.configure(workspace, generation);
    if (generation !== workspace.cameraGeneration) {
      if (workspace.scanner.isScanning) await workspace.scanner.stop();
      return;
    }
    workspace.reader.closest('.camera-frame')?.classList.add('camera-active');
    workspace.cameraButton.textContent = 'Stop camera';
    workspace.cameraButton.onclick = () => stopCamera(workspace);
    workspace.detailScannerStop = window.EventDeskDetailScanner?.start(workspace, onScan, generation);
    if (workspace.outcome?.hidden && !workspace.lookupPending) setMessage(workspace, '');
  } catch (error) {
    setMessage(workspace, error.message || 'Cannot start camera.', 'error');
  } finally {
    workspace.cameraButton.disabled = false;
  }
}

async function stopCamera(workspace) {
  clearCheckinFeedback(workspace);
  if (!workspace) return;
  workspace.cameraGeneration = (workspace.cameraGeneration || 0) + 1;
  workspace.detailScannerStop?.();
  workspace.detailScannerStop = null;
  window.EventDeskCamera?.reset(workspace);
  if (!workspace.scanner) return;
  workspace.cameraButton.disabled = true;
  try {
    if (workspace.scanner.isScanning) await workspace.scanner.stop();
    workspace.reader.closest('.camera-frame')?.classList.remove('camera-active');
    workspace.cameraButton.textContent = photoCameraRequired() ? 'Open camera (photo)' : 'Start camera';
    workspace.cameraButton.onclick = () => startCamera(workspace);
  } catch (_) {
    setMessage(workspace, 'Cannot stop camera. Try again.', 'error');
  } finally {
    workspace.cameraButton.disabled = false;
  }
}

function createWorkspace(hostId, mode) {
  const host = $(hostId);
  host.append($('scannerTemplate').content.cloneNode(true));
  const workspace = {
    mode, root:host, person:null, lookup:null, scanner:null,
    scanBusy:false, lookupPending:false, actionPending:false, reviewPending:false, saveOperation:null,
    cameraGeneration:0, lastScanCode:'', lastScanAt:0, scanSequence:0, nextScanTimer:null,
    message:host.querySelector('.scan-message'), outcome:host.querySelector('.scan-outcome'), empty:host.querySelector('.empty-result'), result:host.querySelector('.person-result'),
    codeInput:host.querySelector('.code-input'), idInput:host.querySelector('.id-input'), confirm:host.querySelector('.confirm-button'),
    cameraButton:host.querySelector('.camera-button'), reader:host.querySelector('.camera-reader'), photoInput:host.querySelector('.camera-photo-input'),
    torchButton:host.querySelector('.camera-torch'), zoomControl:host.querySelector('.camera-zoom-control'),
    zoomInput:host.querySelector('.camera-zoom'), cameraHint:host.querySelector('.camera-hint'),
    mobileActions:host.querySelector('.mobile-checkin-actions'), mobileSubmit:host.querySelector('.mobile-checkin-submit'),
    mobileClear:host.querySelector('.mobile-checkin-clear'), modal:host.querySelector('.checkin-modal'),
    popup:host.querySelector('.employee-popup'), popupClose:host.querySelector('.popup-close'),
    popupTitle:host.querySelector('.popup-title'), receipt:host.querySelector('.scan-receipt'),
    successFeedback:host.querySelector('.checkin-success'),
    popupScrim:host.querySelector('.employee-popup-scrim'), announcement:host.querySelector('.scan-announcement'),
    controlPlaceholder:host.querySelector('.checkin-controls-placeholder'), controlLive:host.querySelector('.checkin-controls-live'),
    modalSubmit:host.querySelector('.modal-submit'), modalCancel:host.querySelector('.modal-cancel'),
    modalReasonSelect:host.querySelector('.checkin-controls-live .modal-reason'), modalReasonLabel:host.querySelector('.checkin-controls-live .modal-reason-label'),
    modalDetail:host.querySelector('.modal-detail'), modalDetailLabel:host.querySelector('.modal-detail-label'), modalMessage:host.querySelector('.modal-message')
  };
  workspace.reader.id = `${mode}CameraReader`;
  if (workspace.popup) {
    const title = workspace.popupTitle;
    const name = workspace.modal.querySelector('.modal-name');
    const help = workspace.popup.querySelector('.popup-help');
    const id = workspace.modal.querySelector('.modal-id');
    const department = workspace.modal.querySelector('.modal-department');
    title.id = `${mode}PopupTitle`; name.id = `${mode}PopupName`;
    help.id = `${mode}PopupHelp`; id.id = `${mode}PopupEmployeeId`; department.id = `${mode}PopupDepartment`;
    workspace.popup.setAttribute('aria-labelledby', `${title.id} ${name.id}`);
    workspace.popup.setAttribute('aria-describedby', `${id.id} ${department.id} ${help.id}`);
    workspace.popup.setAttribute('role', 'dialog');
    workspace.popup.setAttribute('aria-modal', 'true');
    workspace.popup.addEventListener('cancel', event => { event.preventDefault(); dismissCheckin(workspace); });
    workspace.popup.addEventListener('keydown', event => {
      if (event.key === 'Enter' && (event.repeat || event.isComposing) &&
          !['INPUT','TEXTAREA','SELECT'].includes(event.target?.tagName)) event.preventDefault();
      if (workspace.popup.showModal) return;
      if (event.key === 'Escape') { event.preventDefault(); dismissCheckin(workspace); }
      if (event.key === 'Tab') {
        const controls = Array.from(workspace.popup.querySelectorAll('button,input,select,textarea'))
          .filter(node => !node.disabled && !node.closest('[hidden]'));
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    });
    const outsideFallback = event => !workspace.popup.showModal && employeePopupOpen(workspace) &&
      !workspace.popup.contains(event.target);
    document.addEventListener('click', event => {
      if (!outsideFallback(event)) return;
      event.preventDefault();
      event.stopPropagation();
    }, true);
    document.addEventListener('focusin', event => {
      if (outsideFallback(event)) (workspace.modalSubmit.hidden ? workspace.modalCancel : workspace.modalSubmit).focus();
    });
  }
  workspace.confirm.hidden = mode !== 'checkin';
  host.querySelector('.person-status').hidden = mode === 'verify';
  host.querySelector('.code-form').addEventListener('submit', event => { event.preventDefault(); findEmployee(workspace, {code:workspace.codeInput.value.trim()}); });
  host.querySelector('.id-form').addEventListener('submit', async event => { event.preventDefault(); await stopCamera(workspace); findEmployee(workspace, {employee_id:workspace.idInput.value.trim()}); });
  workspace.cameraButton.onclick = () => startCamera(workspace);
  workspace.torchButton.addEventListener('click', () => window.EventDeskCamera?.toggleTorch(workspace));
  workspace.zoomInput.addEventListener('change', () => window.EventDeskCamera?.setZoom(workspace, Number(workspace.zoomInput.value)));
  workspace.photoInput.addEventListener('change', () => scanQrPhoto(workspace, workspace.photoInput.files?.[0]));
  if (photoCameraRequired()) {
    workspace.cameraButton.textContent = 'Open camera (photo)';
    host.querySelector('.camera-copy strong').textContent = 'QR photo scanner';
    host.querySelector('.camera-copy span').textContent = 'Take a QR photo';
  }
  workspace.confirm.addEventListener('click', () => confirmAction(workspace));
  workspace.mobileSubmit.addEventListener('click', () => confirmAction(workspace));
  workspace.mobileClear.addEventListener('click', () => dismissCheckin(workspace));
  workspace.modalSubmit.addEventListener('click', () => confirmAction(workspace));
  workspace.modalCancel.addEventListener('click', () => dismissCheckin(workspace));
  workspace.popupClose?.addEventListener('click', () => dismissCheckin(workspace));
  workspace.modalReasonSelect.addEventListener('change', () => {
    workspace.modalDetailLabel.hidden = workspace.modalReasonSelect.value !== 'other';
    workspace.modalMessage.textContent = '';
  });
  host.querySelector('.next-button').addEventListener('click', () => dismissCheckin(workspace));
  state.workspaces[mode] = workspace;
}

async function drawWinner(event) {
  event.preventDefault();
  if (state.drawing) return;
  if (state.prizeImageUploading) return toast('Wait for the upload.', 'error');
  const selected = $('prizeSelect').selectedOptions[0];
  const prizeId = $('prizeSelect').value;
  const prize = selected?.dataset.prizeName || '';
  const round = $('roundInput').value.trim();
  if (!prize) return;
  state.drawing = true; $('spinButton').disabled = true; $('winnerReveal').hidden = true; $('drawMessage').textContent = 'Selecting a winner securely…';
  try {
    await setProjectorPhase('preparing', {prize, round_name:round}, false);
    const payload = await api('/api/draw', {method:'POST', data:{prize_id:prizeId, round_name:round}});
    state.wheelTurn += 1800 + Math.floor(Math.random() * 360);
    $('luckyWheel').style.transform = `rotate(${state.wheelTurn}deg)`;
    $('drawMessage').textContent = 'Winner saved. Revealing…';
    setTimeout(() => {
      const winner = payload.winner;
      $('winnerName').textContent = winner.extra?.first_name || winner.name;
      $('winnerMeta').textContent = `Employee ID: ${winner.employee_id} · Department: ${winner.department || 'No department'}`;
      $('winnerPrize').textContent = winner.prize || prize;
      $('winnerReveal').hidden = false;
      $('drawMessage').textContent = 'Winner saved.';
      $('drawMessage').className = 'form-message success';
      state.drawing = false; $('spinButton').disabled = false;
      refreshStats().catch(() => {});
      loadPrizeList().catch(() => {});
      loadProjectorState().catch(() => {});
      loadWinnerHistory().catch(() => {});
    }, 5200);
  } catch (error) {
    $('drawMessage').textContent = error.message; $('drawMessage').className = 'form-message error';
    state.drawing = false; $('spinButton').disabled = false;
    if (error.code !== 'draw_in_progress') setProjectorPhase('ready_for_next', {}, false).catch(() => {});
  }
}

async function addLuckyParticipant(event) {
  event.preventDefault();
  const input = $('participantEmployeeId');
  const message = $('participantMessage');
  const employeeId = input.value.trim();
  if (!employeeId) return;
  message.className = 'form-message';
  message.textContent = 'Adding employee…';
  try {
    const payload = await api('/api/draw/participants', {method:'POST', data:{employee_id:employeeId}});
    const person = payload.person;
    const status = person.eligible_now ? 'eligible now' : 'will be eligible after check-in';
    message.textContent = `${person.employee_id} — ${person.name} added; ${status}.`;
    message.className = 'form-message success';
    input.value = '';
    refreshStats().catch(() => {});
  } catch (error) {
    message.textContent = error.message;
    message.className = 'form-message error';
  }
}

async function loadWinnerHistory() {
  const payload = await api('/api/winners');
  const host = $('winnerHistory');
  host.replaceChildren();
  const winners = (payload.items || []).slice(0, 8);
  if (!winners.length) {
    const empty = document.createElement('p'); empty.className = 'muted';
    empty.textContent = 'No winners yet.'; host.append(empty); return;
  }
  winners.forEach(winner => {
    const item = document.createElement('article'); item.className = 'history-item';
    const person = document.createElement('div');
    const name = document.createElement('strong'); name.textContent = winner.name || 'Employee';
    const meta = document.createElement('span'); meta.textContent = [winner.employee_id, winner.department].filter(Boolean).join(' • ');
    person.append(name, meta);
    const draw = document.createElement('div'); draw.className = 'history-prize';
    const prize = document.createElement('strong'); prize.textContent = winner.prize || 'Prize';
    const round = document.createElement('span'); round.textContent = winner.round_name || 'Draw';
    draw.append(prize, round); item.append(person, draw); host.append(item);
  });
}

const projectorLabels = {
  waiting:'Waiting', preparing:'Preparing', spinning:'Spinning',
  winner_selected:'Winner selected', winner_announced:'Winner announced',
  ready_for_next:'Ready for next draw'
};

function renderPrizeImageControl(payload) {
  const preview = $('prizeImagePreview');
  const image = $('prizeImagePreviewImg');
  const url = payload.prize_image_url || '';
  preview.hidden = !url;
  if (url && image.getAttribute('src') !== url) image.src = url;
  if (!url) image.removeAttribute('src');
  $('prizeImageName').textContent = payload.prize_image_name || 'Prize picture';
}

function readImageDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('Could not read the selected picture.'));
    reader.readAsDataURL(file);
  });
}

async function uploadPrizeImage(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  const message = $('prizeImageMessage');
  if (!['image/png','image/jpeg','image/webp'].includes(file.type)) {
    event.target.value = '';
    message.className = 'form-message error'; message.textContent = 'Use PNG, JPG or WebP.'; return;
  }
  if (file.size > 4 * 1024 * 1024) {
    event.target.value = '';
    message.className = 'form-message error'; message.textContent = 'Use a picture under 4 MB.'; return;
  }
  state.prizeImageUploading = true; $('spinButton').disabled = true;
  message.className = 'form-message'; message.textContent = 'Uploading prize picture…';
  try {
    const dataUrl = await readImageDataUrl(file);
    const payload = await api('/api/projector/prize-image', {method:'POST', data:{data_url:dataUrl, filename:file.name}});
    renderProjectorState(payload.state);
    message.className = 'form-message success'; message.textContent = 'Picture saved.';
  } catch (error) {
    message.className = 'form-message error'; message.textContent = error.message;
  } finally {
    state.prizeImageUploading = false; $('spinButton').disabled = state.drawing; event.target.value = '';
  }
}

async function removePrizeImage() {
  const message = $('prizeImageMessage');
  state.prizeImageUploading = true; $('spinButton').disabled = true;
  message.className = 'form-message'; message.textContent = 'Removing prize picture…';
  try {
    const payload = await api('/api/projector/prize-image', {method:'POST', data:{clear:true}});
    renderProjectorState(payload.state);
    message.className = 'form-message success'; message.textContent = 'Prize picture removed.';
  } catch (error) {
    message.className = 'form-message error'; message.textContent = error.message;
  } finally {
    state.prizeImageUploading = false; $('spinButton').disabled = state.drawing;
  }
}

function renderProjectorState(payload) {
  const phase = payload.phase || 'waiting';
  $('projectorPhase').textContent = projectorLabels[phase] || 'Connected';
  $('projectorLiveDot').classList.toggle('online', true);
  const details = [];
  if (payload.prize) details.push(payload.prize);
  if (payload.round_name) details.push(payload.round_name);
  details.push(`${Number(payload.eligible || 0).toLocaleString()} eligible`);
  $('projectorDetail').textContent = details.join(' • ');
  $('readyNextDraw').disabled = phase === 'ready_for_next' || state.drawing;
  renderPrizeImageControl(payload);
}

async function loadProjectorState() {
  const payload = await api('/api/projector/state');
  renderProjectorState(payload);
  return payload;
}

function connectProjectorState() {
  if (state.projectorSource || !window.EventSource) return;
  const source = new EventSource('/api/projector/events', {withCredentials:true});
  state.projectorSource = source;
  source.addEventListener('draw-state', event => {
    try { renderProjectorState(JSON.parse(event.data)); } catch (_) {}
  });
  source.onopen = () => $('projectorLiveDot').classList.add('online');
  source.onerror = () => {
    $('projectorLiveDot').classList.remove('online');
    $('projectorPhase').textContent = 'Reconnecting';
    if (state.projectorSource !== source) source.close();
  };
}

async function setProjectorPhase(phase, details={}, notify=true) {
  const payload = await api('/api/projector/state', {method:'POST', data:{phase, ...details}});
  renderProjectorState(payload);
  if (notify) toast('Ready for the next draw.', 'success');
  return payload;
}

function openProjectorWindow() {
  const projector = window.open('/projector', 'eventdesk-projector');
  if (!projector) toast('Allow pop-ups to open the screen.', 'error');
}

async function loadProjectorSettings() {
  const settings = await api('/api/settings');
  $('projectorTitle').value = settings.projector_title || 'Lucky Draw';
  $('projectorTheme').value = settings.projector_theme || 'gold';
  $('projectorShowEmployeeId').checked = settings.projector_show_employee_id !== false;
}

async function saveProjectorSettings(event) {
  event.preventDefault();
  const message = $('projectorSettingsMessage');
  message.className = 'form-message'; message.textContent = 'Saving…';
  try {
    await api('/api/settings', {method:'POST', data:{
      projector_title:$('projectorTitle').value.trim(),
      projector_theme:$('projectorTheme').value,
      projector_show_employee_id:$('projectorShowEmployeeId').checked
    }});
    message.className = 'form-message success'; message.textContent = 'Settings saved.';
    toast('Settings saved.', 'success');
  } catch (error) {
    message.className = 'form-message error'; message.textContent = error.message;
  }
}

function captureScanner(event) {
  if (!['checkin','verify'].includes(state.view) || event.ctrlKey || event.metaKey || event.altKey) return;
  const workspace = state.workspaces[state.view];
  const editing = ['INPUT','TEXTAREA','SELECT'].includes(event.target?.tagName || document.activeElement?.tagName);
  if (event.key === 'Enter' && (event.repeat || event.isComposing) && !editing) {
    event.preventDefault();
    return;
  }
  if (workspace?.reviewPending || workspace?.actionPending || workspace?.lookupPending) {
    if (editing && !workspace.lookupPending) return;
    if (event.isComposing) return;
    const now = Date.now();
    if (now - state.keyAt > 120) state.keyBuffer = '';
    if (event.key === 'Enter') {
      if (state.keyBuffer || event.repeat || workspace.actionPending) event.preventDefault();
      state.keyBuffer = '';
    } else if (event.key.length === 1 && event.key !== ' ') {
      state.keyBuffer += event.key;
      state.keyAt = now;
    }
    return;
  }
  if (event.isComposing || event.repeat) return;
  if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)) return;
  const now = Date.now();
  if (now - state.keyAt > 120) state.keyBuffer = '';
  state.keyAt = now;
  if (event.key === 'Enter') {
    const value = state.keyBuffer.trim(); state.keyBuffer = '';
    if (value.length >= 4) {
      event.preventDefault();
      findEmployee(state.workspaces[state.view], {code:value});
    }
  } else if (event.key.length === 1) state.keyBuffer += event.key;
}

function updateNetwork() {
  $('networkState').textContent = navigator.onLine ? 'Online' : 'Offline';
  $('networkState').classList.toggle('offline', !navigator.onLine);
}

async function initialize() {
  initializeAdminDialogs();
  createWorkspace('checkinWorkspace', 'checkin');
  createWorkspace('verifyWorkspace', 'verify');
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => goView(button.dataset.view)));
  $('loginForm').addEventListener('submit', login);
  $('togglePassword').addEventListener('click', () => { const field=$('loginPassword'); field.type=field.type === 'password' ? 'text' : 'password'; $('togglePassword').textContent=field.type === 'password' ? 'Show' : 'Hide'; });
  $('adminLoginButton').addEventListener('click', () => showLogin('', true));
  $('loginCancel').addEventListener('click', async () => {
    const button = $('loginCancel');
    button.disabled = true;
    try {
      const profile = state.profile || await api('/api/staff-session', {method:'POST', data:{}});
      $('loginForm').reset();
      $('loginError').textContent = '';
      showApp(profile);
      goView('home');
      document.querySelector('.brand-button').focus();
    } catch (error) {
      $('loginError').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
  $('logoutButton').addEventListener('click', async () => { try { await api('/api/logout', {method:'POST', data:{}}); const profile = await api('/api/staff-session', {method:'POST', data:{}}); showApp(profile); } catch (error) { showLogin(error.message); } });
  $('attendanceFilters').addEventListener('submit', event => { event.preventDefault(); state.attendancePage = 1; loadAttendance(); });
  $('attendanceStatus').addEventListener('change', () => { state.attendancePage = 1; loadAttendance(); });
  $('attendancePrevious').addEventListener('click', () => { state.attendancePage -= 1; loadAttendance(); });
  $('attendanceNext').addEventListener('click', () => { state.attendancePage += 1; loadAttendance(); });
  $('wristbandLookupForm').addEventListener('submit', loadWristbandEmployee);
  $('wristbandLost').addEventListener('click', markWristbandLost);
  $('wristbandEmployeeId').addEventListener('input', () => {
    if (!state.wristbandSaving) resetWristbandSupport(false);
  });
  $('adminAttendanceScope').addEventListener('change', updateAttendanceScope);
  $('adminAttendanceForm').addEventListener('submit', removeAdminAttendance);
  $('adminAddEmployeeForm').addEventListener('submit', addAdminEmployee);
  $('adminAddEmployeePosition').addEventListener('input', updateAdminEmployeeDraw);
  updateAdminEmployeeDraw();
  $('adminDrawForm').addEventListener('submit', removeAdminDrawHistory);
  updateAttendanceScope();
  $('drawForm').addEventListener('submit', drawWinner);
  $('rewardForm').addEventListener('submit', addRewardDraft);
  $('rewardCancel').addEventListener('click', resetRewardForm);
  $('rewardPublish').addEventListener('click', publishRewards);
  $('prizeImageInput').addEventListener('change', uploadPrizeImage);
  $('removePrizeImage').addEventListener('click', removePrizeImage);
  $('openProjector').addEventListener('click', openProjectorWindow);
  $('readyNextDraw').addEventListener('click', () => setProjectorPhase('ready_for_next').catch(error => toast(error.message, 'error')));
  $('projectorSettingsForm').addEventListener('submit', saveProjectorSettings);
  $('addParticipantForm').addEventListener('submit', addLuckyParticipant);
  document.addEventListener('keydown', captureScanner);
  window.addEventListener('online', updateNetwork); window.addEventListener('offline', updateNetwork); updateNetwork();
  try {
    const profile = await api('/api/me');
    if (profile.authenticated) showApp(profile);
    else showApp(await api('/api/staff-session', {method:'POST', data:{}}));
  } catch (error) { showLogin(error.message); }
  setInterval(() => {
    if (!state.profile || document.hidden) return;
    if (['checkin','verify','wristband'].includes(state.view)) return;
    if (state.view === 'admin') refreshAdmin().catch(() => {});
    else if (state.view === 'attendance') loadAttendance(true);
    else {
      refreshStats().catch(() => {});
      if (state.view === 'draw') loadWinnerHistory().catch(() => {});
    }
  }, 15000);
}

initialize();
