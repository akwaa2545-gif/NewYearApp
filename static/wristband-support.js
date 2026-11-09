'use strict';

function wristbandSupportAllowed() {
  return state.view === 'wristband' && ['admin','staff','checkin_staff'].includes(state.profile?.role);
}

function setWristbandBusy(pending) {
  for (const button of [$('wristbandFind'), $('wristbandLost')]) {
    button.disabled = false;
    button.setAttribute('aria-disabled', String(pending));
  }
  $('wristbandEmployeeId').disabled = false;
  $('wristbandEmployeeId').readOnly = pending;
  $('wristbandLookupForm').setAttribute('aria-busy', String(pending));
}

function resetWristbandSupport(clearInput=true) {
  state.wristbandRequestId = (state.wristbandRequestId || 0) + 1;
  state.wristbandPerson = null;
  state.wristbandLoading = false;
  state.wristbandSaving = false;
  state.wristbandOperation = null;
  $('wristbandResult').hidden = true;
  $('wristbandLost').hidden = true;
  $('wristbandLost').textContent = 'Mark wristband lost';
  setWristbandBusy(false);
  if (clearInput) $('wristbandEmployeeId').value = '';
  $('wristbandMessage').textContent = '';
}

function renderWristbandPerson(person) {
  $('wristbandEmployeeName').textContent = person.name || 'Employee';
  $('wristbandEmployeeMeta').textContent = `ID ${person.employee_id} · ${person.department || 'No department'}`;
  const checked = Boolean(person.checked_in_at);
  const lost = person.checkin_wristband_reason === 'lost';
  $('wristbandStatus').textContent = !checked ? 'Not checked in.' :
    lost ? 'Wristband is lost.' : 'Checked in.';
  $('wristbandLost').hidden = !checked || lost;
  $('wristbandResult').hidden = false;
}

async function loadWristbandEmployee(event) {
  event?.preventDefault();
  if (!wristbandSupportAllowed() || state.wristbandLoading || state.wristbandSaving) return;
  const employeeId = $('wristbandEmployeeId').value.trim();
  resetWristbandSupport(false);
  if (!employeeId) {
    $('wristbandMessage').textContent = 'Enter an employee ID.';
    return;
  }
  const requestId = state.wristbandRequestId;
  const current = () => requestId === state.wristbandRequestId && wristbandSupportAllowed();
  state.wristbandLoading = true;
  setWristbandBusy(true);
  $('wristbandMessage').textContent = 'Finding employee…';
  try {
    const payload = await api('/api/lookup', {method:'POST', data:{employee_id:employeeId}});
    if (!current()) return;
    $('wristbandEmployeeId').value = String(payload.person.employee_id);
    state.wristbandPerson = payload.person;
    renderWristbandPerson(payload.person);
    $('wristbandMessage').textContent = 'Employee found.';
  } catch (error) {
    if (current()) $('wristbandMessage').textContent = error.message;
  } finally {
    if (current()) {
      state.wristbandLoading = false;
      setWristbandBusy(false);
    }
  }
}

async function markWristbandLost() {
  const person = state.wristbandPerson;
  if (!wristbandSupportAllowed() || state.wristbandLoading || state.wristbandSaving ||
      !person?.checked_in_at || person.checkin_wristband_reason === 'lost') return;
  if ($('wristbandEmployeeId').value.trim() !== String(person.employee_id)) {
    resetWristbandSupport(false);
    $('wristbandMessage').textContent = 'Find the employee again.';
    return;
  }
  const requestId = state.wristbandRequestId;
  const operation = {};
  state.wristbandOperation = operation;
  state.wristbandSaving = true;
  const current = () => state.wristbandOperation === operation &&
    requestId === state.wristbandRequestId && wristbandSupportAllowed();
  setWristbandBusy(true);
  $('wristbandLost').textContent = 'Saving…';
  $('wristbandMessage').textContent = 'Updating wristband…';
  let saved = false;
  try {
    const payload = await api('/api/checkin/wristband-lost', {method:'POST', data:{employee_id:person.employee_id}});
    if (!current()) return;
    state.wristbandPerson = {...payload.person, checkin_wristband_reason:'lost'};
    renderWristbandPerson(state.wristbandPerson);
    saved = true;
    $('wristbandMessage').textContent = 'Wristband is lost. Not in draw.';
  } catch (error) {
    if (current()) $('wristbandMessage').textContent = error.message;
  } finally {
    if (state.wristbandOperation === operation) {
      state.wristbandOperation = null;
      state.wristbandSaving = false;
      $('wristbandLost').textContent = 'Mark wristband lost';
      setWristbandBusy(false);
      if (saved && wristbandSupportAllowed()) $('wristbandFind').focus();
    }
  }
}
