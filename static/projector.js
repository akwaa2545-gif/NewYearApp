'use strict';

const byId = id => document.getElementById(id);
if (new URLSearchParams(location.search).get('visual') === 'childhood') document.body.dataset.visual = 'childhood';
let openingWelcome = new URLSearchParams(location.search).get('intro') === '1';
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let digitSequence = -1, localDrawWinner = null, initialSequence = null;
let lastRevealedDraw = null;
let soundStatus = 'blocked';
const projectorAudio = window.EventDeskProjectorAudio?.create(status => {
  soundStatus = status;
  const button = byId('soundToggle');
  button.hidden = status === 'enabled';
  button.textContent = status === 'muted' ? 'Unmute sound' : 'Enable sound';
});
let boardPrizes = [], boardPage = 0, boardWinnerIndex = 0, boardSelected = '';
let posterIntroPlayed = false;
let refreshInFlight = false, lastRenderedSequence = -1, lastRenderedPhase = -1;
let queuedSelectionSequence = -1, queuedStateRefresh = false;
let requestedPrize = null, displayedPrize = null, prizeAnimation = null;
let deferredProjectorState = null, pendingReelState = null;
const WINNER_HOLD_MS = 5000;
let winnerHoldTimer = null;
let boardSignature = '', boardCards = [], lastRenderedSignature = '';
let projectorCsrf = '', projectorDrawing = false, currentPhase = 'waiting', pendingPrize = null;
let currentView = 'overview', latestState = null, selectedPrizeId = null, localPrizePreviewId = null;
let projectorActivity = null, projectorReady = false, statePollTimer = null;
let statePollingEnabled = false, statePollFailures = 0, stateRead = null;
const projectorDigits = window.EventDeskDigitDraw?.create(byId('projectorDigits'),
  {reducedMotion:reducedMotion.matches, onTick:projectorAudio?.reelTick});
const projectorWinnerDigits = window.EventDeskDigitDraw?.create(byId('projectorWinnerId'), {reducedMotion:true});

function projectorBackgroundAllowed() { return projectorReady && projectorActivity?.isActive() === true; }

// The audience display can be visible on another monitor while staff use the draw window.
function projectorStatePollingAllowed() { return projectorReady && statePollingEnabled && !document.hidden; }

function pauseProjectorBackground() {
  statePollingEnabled = false;
  clearTimeout(statePollTimer); statePollTimer = null;
  queuedSelectionSequence = -1;
  queuedStateRefresh = false;
  if (projectorReady) setConnection('offline', 'Paused');
}

function projectorPollDelay(state) {
  if (statePollFailures) return statePollFailures === 1 ? 30000 : 60000;
  const phase = state?.phase;
  const active = ['preparing','spinning','winner_selected'].includes(phase)
    || (phase === 'ready_for_next' && state.prize)
    || projectorDigits?.isSpinning() || winnerHoldTimer !== null;
  return active ? 3000 : 15000;
}

async function refreshProjectorState() {
  if (!projectorStatePollingAllowed() || refreshInFlight) return;
  clearTimeout(statePollTimer); statePollTimer = null;
  if (projectorBackgroundAllowed()) void window.EventDeskStorage?.confirmBackup?.(true);
  let state;
  refreshInFlight = true;
  try { state = await loadState(); statePollFailures = 0; }
  catch (_) {
    statePollFailures = Math.min(statePollFailures + 1, 2);
    if (projectorStatePollingAllowed()) setConnection('offline', 'Reconnecting');
  }
  finally {
    refreshInFlight = false;
    const needsRefresh = queuedStateRefresh || queuedSelectionSequence > lastRenderedSequence;
    queuedSelectionSequence = -1;
    queuedStateRefresh = false;
    if (projectorStatePollingAllowed()) {
      if (needsRefresh) void refreshProjectorState();
      else statePollTimer = setTimeout(refreshProjectorState, projectorPollDelay(state));
    }
  }
}

function startProjectorBackground() {
  if (projectorActivity || !window.EventDeskActivity) return;
  projectorActivity = window.EventDeskActivity.create();
  window.addEventListener('storage', event => {
    if (!event.newValue || !projectorStatePollingAllowed()) return;
    if (event.key === 'eventdesk-projector-draw') {
      if (refreshInFlight) queuedStateRefresh = true;
      else void refreshProjectorState();
      return;
    }
    if (event.key !== 'eventdesk-projector-selection') return;
    const sequence = Number(event.newValue);
    if (!Number.isSafeInteger(sequence) || sequence <= lastRenderedSequence) return;
    if (refreshInFlight) queuedSelectionSequence = Math.max(queuedSelectionSequence, sequence);
    else void refreshProjectorState();
  });
  const syncVisibility = () => {
    if (document.hidden) return pauseProjectorBackground();
    if (statePollingEnabled) return;
    statePollingEnabled = true;
    if (refreshInFlight) queuedStateRefresh = true;
    else void refreshProjectorState();
  };
  document.addEventListener('visibilitychange', syncVisibility);
  window.addEventListener('pagehide', pauseProjectorBackground);
  window.addEventListener('pageshow', syncVisibility);
  syncVisibility();
}
const phaseOrder = {waiting:0, preparing:1, spinning:2, winner_selected:3, winner_announced:4, ready_for_next:5};
const shortRewardNames = [
  [/Special Gift (?:from|form) MD/i, 'Special Gift from MD'],
  [/Yamaha Grand Filano/i, 'Grand Filano 2026'],
  [/iPhone 18 Pro Max/i, 'iPhone 18 Pro Max'],
  [/iPad Pro/i, 'iPad Pro 11 inch'],
  [/Samsung Galaxy S26 Ultra/i, 'Galaxy S26 Ultra'],
  [/Bosch.*9 kg.*Dryer/i, 'Bosch 9 kg Dryer'],
  [/Bosch.*Dryer/i, 'Bosch Dryer'],
  [/Bosch.*Washing Machine/i, 'Bosch 10 kg Washer'],
  [/Gold Necklace 1 salueng/i, 'Gold Necklace 1 salueng'],
  [/Gold Necklace/i, 'Gold Necklace ¼ Baht'],
  [/TOSHIBA.*Refrigerator/i, 'TOSHIBA 16.2Q Refrigerator'],
  [/LG.*Refrigerator/i, 'LG Refrigerator'],
  [/Samsung.*Mini LED.*Smart TV/i, 'Samsung Mini LED 55-inch TV'],
  [/Samsung.*Smart TV/i, 'Samsung 55-inch TV'],
  [/Apple Watch Series 12 GPS/i, 'Apple Watch Series 12 GPS'],
  [/Apple Watch Series 12/i, 'Apple Watch Series 12'],
];
const showPrizeNames = [
  /Special Gift (?:from|form) MD/i, /Yamaha Grand Filano/i,
  /iPhone 18 Pro Max/i, /iPad Pro/i, /Samsung Galaxy S26 Ultra/i,
  /Bosch.*Dryer/i, /Bosch.*Washing Machine/i, /Gold Necklace/i,
  /TOSHIBA.*Refrigerator/i, /Samsung.*Mini LED.*Smart TV/i,
  /Apple Watch Series 12/i,
];
const boardPageSize = 11;
const posterSlots = {
  1:[56,80,12,23], 2:[18,21,17,25], 3:[34,20,9,20],
  4:[49,20,13,21], 5:[65,20,10,21], 6:[81,21,13,23],
  7:[82,79,13,22], 8:[69,79,10,20], 9:[17,79,10,24],
  10:[43,79,14,18], 11:[30,79,9,18],
};
function prizeNumber(prize) {
  const savedNumber = Number(prize.number);
  if (Number.isInteger(savedNumber) && savedNumber > 0) return savedNumber;
  const index = showPrizeNames.findIndex(pattern => pattern.test(prize.name || ''));
  return index < 0 ? 0 : index + 1;
}
function nextUnfinishedPrize() {
  const index = boardPrizes.findIndex(prize => prize.id === selectedPrizeId);
  return boardPrizes[index + 1] || boardPrizes[0];
}
function projectorRewardName(name) {
  return shortRewardNames.find(([pattern]) => pattern.test(name || ''))?.[1] || name;
}

function text(id, value) { const node = byId(id); if (node) node.textContent = value ?? ''; }
function phaseCopy(phase) {
  return ({
    waiting: ['Waiting', ''],
    preparing: ['Get ready', ''],
    spinning: ['Revealing employee ID', 'Digits stop from left to right'],
    winner_selected: ['Final digits', ''],
    winner_announced: ['Congratulations', ''],
    ready_for_next: ['Ready', ''],
  }[phase] || ['Waiting', '']);
}
function setConnection(mode, label) {
  const badge = byId('connectionBadge');
  badge.className = `connection-badge is-${mode}`;
  badge.querySelector('span').textContent = label;
}
function createConfetti() {
  const host = byId('celebration');
  if (reducedMotion.matches) return;
  host.replaceChildren();
  for (let index = 0; index < 26; index += 1) {
    const item = document.createElement('i');
    item.className = 'confetti';
    item.style.left = `${2 + Math.random() * 96}%`;
    item.style.setProperty('--delay', `${Math.random() * .4}s`);
    item.style.setProperty('--fall', `${1.8 + Math.random() * .9}s`);
    item.style.setProperty('--drift', `${-8 + Math.random() * 16}vw`);
    host.append(item);
  }
}
function applyDeferredProjectorState() {
  if (winnerHoldTimer !== null || !deferredProjectorState) return;
  const next = deferredProjectorState;
  deferredProjectorState = null;
  render(next);
}

function winnerFromNextSnapshot(state, id, next) {
  if (next.show_employee_id === false) return null;
  // The server may move on between polls. New saved winners for this prize
  // still carry the exact employee ID that the current reels landed on.
  const winnersFor = snapshot => snapshot.prizes?.find(prize => prize.name === state.prize)?.winners || [];
  const matchId = winner => winner.employee_id === id;
  const latest = winnersFor(next).filter(matchId);
  return latest.length > winnersFor(state).filter(matchId).length ? latest.at(-1) : null;
}

function startDigitDraw(state) {
  if (digitSequence !== state.sequence) {
    digitSequence = state.sequence;
    projectorDigits?.reset();
    projectorAudio?.startDraw();
  }
  const id = state.show_employee_id === false ? '' : state.draw_employee_id || state.winner?.employee_id
    || (localDrawWinner?.prizeId === selectedPrizeId && localDrawWinner.sequence === state.sequence
      ? localDrawWinner.employeeId : '');
  if (!id) { projectorDigits?.pending(); return; }
  projectorDigits?.play(id, () => {
    projectorAudio?.finishDraw();
    if (latestState?.sequence === state.sequence) {
      const next = deferredProjectorState;
      if (latestState.phase === 'winner_announced') {
        if (next?.show_employee_id === false && latestState.show_employee_id !== false) {
          render({...latestState, show_employee_id:false});
        } else renderView();
      } else if (next) {
        const winner = latestState.winner || winnerFromNextSnapshot(latestState, id, next);
        if (winner) render({...latestState, phase:'winner_announced', winner,
          show_employee_id:latestState.show_employee_id !== false && next.show_employee_id !== false});
      }
    }
    applyDeferredProjectorState();
  });
}
function winnerIdentity(state) {
  if (state.spin_started_at) return JSON.stringify([state.spin_started_at, state.prize, state.round_name]);
  return state.winner.winner_id == null ? String(state.sequence) : 'winner:' + state.winner.winner_id;
}

function showWinner(state) {
  const winner = state.winner; if (!winner) return;
  if (state.show_employee_id !== false && digitSequence === state.sequence && projectorDigits?.isSpinning()) return;
  const id = state.show_employee_id === false ? '' : winner.employee_id || '';
  const boxesShown = projectorDigits?.show(id) === true;
  projectorWinnerDigits?.reset();
  text('projectorDrawIdFallback', boxesShown ? '' : id);
  byId('projectorDrawIdFallback').hidden = boxesShown || !id;
  text('projectorDrawWinnerName', winner.name || 'Winner');
  byId('projectorDrawWinnerName').hidden = false;
  text('winnerPrize', projectorRewardName(winner.prize || state.prize) || 'Prize');
  text('projectorWinnerName', winner.name || 'Winner');
  text('projectorWinnerMeta', winner.department || '');
  byId('projectorWinnerMeta').hidden = !winner.department;
  byId('drawStage').classList.add('is-revealed');
  text('winnerRound', winner.round_name || state.round_name || '');
  byId('winnerStage').hidden = true; byId('drawStage').hidden = false;
  const draw = winnerIdentity(state);
  if (lastRevealedDraw !== draw) {
    lastRevealedDraw = draw;
    createConfetti();
    // Opening a saved result should not replay its fanfare.
    if (digitSequence === state.sequence || Number(state.sequence) !== initialSequence) projectorAudio?.celebrate();
    if (digitSequence === state.sequence && !reducedMotion.matches) {
      // Give the reveal and the five-second victory cue time before moving on.
      winnerHoldTimer = setTimeout(() => {
        winnerHoldTimer = null;
        if (deferredProjectorState) applyDeferredProjectorState();
        else renderView();
      }, WINNER_HOLD_MS);
    }
  }
}
function showDraw(state) {
  byId('winnerStage').hidden = true; byId('drawStage').hidden = false;
  byId('drawStage').classList.remove('is-revealed');
  byId('projectorWinnerMeta').hidden = true;
  byId('projectorDrawWinnerName').hidden = true;
  byId('projectorDrawIdFallback').hidden = true;
  const copy = phaseCopy(state.phase); text('phaseTitle', copy[0]); text('phaseDetail', copy[1]);
  if (state.phase === 'spinning' || state.phase === 'winner_selected') startDigitDraw(state);
  else { projectorAudio?.reset(); projectorDigits?.reset(); projectorWinnerDigits?.reset(); }
}
function fitPrizeNameplate(label) {
  if (!label.clientWidth || !label.firstChild) return;
  label.classList.remove('can-wrap');
  label.style.removeProperty('--letter-scale');
  const range = document.createRange();
  range.selectNodeContents(label);
  const fits = () => {
    const ink = range.getBoundingClientRect();
    return ink.width <= label.clientWidth * .83 + 1 && ink.height <= label.clientHeight * .72;
  };
  if (fits()) return;
  // Try one line down to a projector-readable size before allowing a second line.
  let small = .044, large = .06;
  label.style.setProperty('--letter-scale', small);
  if (!fits()) {
    label.classList.add('can-wrap');
    label.style.removeProperty('--letter-scale');
    if (fits()) return;
    small = .016;
  }
  for (let i = 0; i < 7; i++) {
    const middle = (small + large) / 2;
    label.style.setProperty('--letter-scale', middle);
    if (fits()) small = middle;
    else large = middle;
  }
  label.style.setProperty('--letter-scale', small);
}

function makePrizeGroup(request) {
  const group = document.createElement('div');
  group.className = 'prize-block';
  if (request.id === null) {
    group.setAttribute('role', 'img');
    group.setAttribute('aria-label', 'Mystery prize. Waiting for staff to select a prize.');
    group.append(byId('mysteryPrizeLayer').content.cloneNode(true));
    return {group};
  }
  const visual = document.createElement('div');
  const label = document.createElement('strong'), number = document.createElement('span');
  const round = document.createElement('small');
  const image = request.image || document.createElement('img');
  visual.className = 'prize-visual';
  if (/^(?:Grand Filano 2026|Motorcycle)$/i.test(request.label)) visual.classList.add('prize-visual--motorcycle');
  if (/^(?:Samsung (?:Mini LED )?55-inch TV|TV)$/i.test(request.label)) visual.classList.add('prize-visual--tv');
  visual.hidden = !request.image;
  label.className = 'prize-nameplate';
  label.lang = /[\u0e00-\u0e7f]/u.test(request.label) ? 'th' : 'en';
  label.textContent = request.label;
  number.hidden = round.hidden = true;
  number.textContent = request.number;
  round.textContent = request.round;
  visual.append(image);
  group.append(visual, number, label, round, byId('prizePlaqueLayer').content.cloneNode(true));
  return {group, visual, image, label, number, round};
}

function presentPrize(request) {
  if (request !== requestedPrize || !request.ready || prizeAnimation || displayedPrize === request) return;
  const track = byId('prizeTrack'), parts = makePrizeGroup(request);
  const outgoing = track.firstElementChild;
  const slide = outgoing && displayedPrize.id !== request.id && !reducedMotion.matches && track.animate;
  const commit = () => {
    outgoing?.remove();
    parts.group.classList.remove('is-incoming');
    parts.group.removeAttribute('aria-hidden');
    if (parts.label) {
      parts.visual.id = 'projectorPrizeVisual';
      parts.image.id = 'projectorPrizeImage';
      parts.label.id = 'projectorPrize';
      parts.number.id = 'projectorPrizeNumber';
      parts.round.id = 'projectorRound';
      selectedPrizeId = request.id;
    }
    displayedPrize = request;
    const winnerImage = byId('winnerPrizeImage');
    if (request.image) { winnerImage.src = request.url; winnerImage.alt = parts.image.alt; }
    else winnerImage.removeAttribute('src');
    winnerImage.hidden = !request.image;
    byId('drawStage').classList.toggle('has-prize-image', Boolean(request.image));
    byId('winnerStage').classList.toggle('has-prize-image', Boolean(request.image));
  };
  if (slide) {
    parts.group.classList.add('is-incoming');
    parts.group.setAttribute('aria-hidden', 'true');
  }
  track.append(parts.group);
  if (parts.label) fitPrizeNameplate(parts.label);
  if (!slide) { commit(); return; }
  track.classList.add('is-sliding');
  // The track fills the viewport. Percentage translations follow its live width,
  // including if the operator enters fullscreen halfway through a slide.
  const animation = track.animate([{transform:'translateX(0%)'}, {transform:'translateX(100%)'}],
    {duration:1200, easing:'cubic-bezier(0.65, 0, 0.35, 1)', fill:'forwards'});
  prizeAnimation = animation;
  animation.onfinish = () => {
    if (prizeAnimation !== animation) return;
    commit();
    animation.cancel();
    prizeAnimation = null;
    track.classList.remove('is-sliding');
    renderView();
  };
}

function renderPrizeGroup(id, state, number) {
  const url = state.prize_image_url || '', round = state.round_name || '';
  const label = projectorRewardName(state.prize) || 'Prize';
  if (requestedPrize?.id === id && requestedPrize.url === url && requestedPrize.label === label &&
      requestedPrize.number === number && requestedPrize.round === round) {
    presentPrize(requestedPrize);
    return;
  }
  const request = {id, url, label, number, round, ready:false, image:null};
  requestedPrize = request;
  if (url) {
    const image = document.createElement('img');
    image.alt = `Prize: ${label}`;
    image.fetchPriority = 'high';
    const loaded = decoded => {
      if (requestedPrize !== request) return;
      request.image = decoded;
      request.ready = true;
      presentPrize(request);
      renderView();
    };
    if (image.decode) {
      image.src = url;
      image.decode().then(() => loaded(image), () => loaded(null));
      return;
    }
    image.onload = () => loaded(image);
    image.onerror = () => loaded(null);
    image.src = url;
    if (!image.complete || !image.naturalWidth) return;
    image.onload = image.onerror = null;
    request.image = image;
  }
  request.ready = true;
  presentPrize(request);
}

function createRewardCard(prize, index) {
    const row = document.createElement('button'); row.type = 'button'; row.className = 'reward-card';
    row.title = prize.name;
    if (/Yamaha Grand Filano/i.test(prize.name)) row.classList.add('reward-card--scooter');
    if (/(?:LG|TOSHIBA).*Refrigerator/i.test(prize.name)) row.classList.add('reward-card--fridge');
    row.disabled = projectorDrawing || Boolean(pendingPrize) || ['spinning','winner_selected'].includes(currentPhase);
    row.addEventListener('click', () => openPrize(prize.id));
    const reward = document.createElement('div'); reward.className = 'reward-card-name';
    const name = document.createElement('strong'); name.textContent = projectorRewardName(prize.name);
    const progress = document.createElement('small'); progress.textContent = `${prize.winners.length} winner${prize.winners.length === 1 ? '' : 's'}`;
    const number = document.createElement('span'); number.className = 'reward-card-number'; number.textContent = `#${String(prizeNumber(prize) || boardPrizes.length - boardPage * boardPageSize - index).padStart(2, '0')}`;
    row.style.setProperty('--card-index', String(index));
    const visual = document.createElement('div'); visual.className = 'reward-card-visual';
    if (prize.has_image) { const picture = document.createElement('img'); picture.loading = 'lazy'; picture.src = `/api/rewards/image?id=${encodeURIComponent(prize.id)}&v=${lastRenderedSequence}`; picture.alt = ''; visual.append(picture); row.classList.add('has-image'); }
    else row.classList.add('text-only');
    reward.append(name, progress);
    const winner = document.createElement('div'); winner.className = 'reward-card-winner';
    row.append(number);
    if (prize.has_image) row.append(visual);
    row.append(reward);
    row.append(winner);
    return {row, winner};
}

function playPosterIntro(entries) {
  if (posterIntroPlayed || reducedMotion.matches ||
      !entries.every(entry => typeof entry.node.animate === 'function')) return false;
  posterIntroPlayed = true;
  entries.forEach(({node}, index) => {
    node.animate([
      {opacity:0, transform:'translate(-50%,calc(-50% + 18px)) scale(.92)'},
      {opacity:1, transform:'translate(-50%,-50%) scale(1)'}
    ], {duration:700, delay:index * 70, easing:'cubic-bezier(.2,.7,.2,1)'});
  });
  return true;
}

function playPosterCycle(entries, afterIntro) {
  if (reducedMotion.matches || entries.length < 2 ||
      !entries.every(entry => typeof entry.node.animate === 'function')) return;
  const aroundCenter = [...entries].sort((a, b) =>
    Math.atan2(a.slot[1] - 50, a.slot[0] - 50) - Math.atan2(b.slot[1] - 50, b.slot[0] - 50));
  entries.forEach(entry => {
    const start = aroundCenter.indexOf(entry);
    const frames = Array.from({length:entries.length + 1}, (_, step) => {
      const slot = aroundCenter[(start + step) % entries.length].slot;
      return {offset:step / entries.length, left:`${slot[0]}%`, top:`${slot[1]}%`};
    });
    entry.node.animate(frames, {
      duration:55000, delay:afterIntro ? 1500 : 0, iterations:Infinity, easing:'ease-in-out'
    });
  });
}

function renderPosterPrizes(prizes) {
  const host = byId('posterPrizes');
  const entries = prizes.map((prize, index) => {
    const slot = posterSlots[prizeNumber(prize)] || posterSlots[index + 1];
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'poster-prize';
    node.title = prize.name;
    node.setAttribute('aria-label', prize.name);
    node.addEventListener('click', () => openPrize(prize.id));
    node.style.setProperty('--poster-x', `${slot[0]}%`);
    node.style.setProperty('--poster-y', `${slot[1]}%`);
    node.style.setProperty('--poster-w', `${slot[2]}%`);
    node.style.setProperty('--poster-h', `${slot[3]}%`);
    if (prize.has_image) {
      const image = document.createElement('img');
      image.loading = 'lazy';
      image.alt = prize.name;
      image.src = `/api/rewards/image?id=${encodeURIComponent(prize.id)}&v=${lastRenderedSequence}`;
      node.append(image);
    } else {
      node.classList.add('text-only');
      node.textContent = projectorRewardName(prize.name);
    }
    return {node, slot};
  });
  host.replaceChildren(...entries.map(entry => entry.node));
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => {
    if (!host.isConnected) return;
    playPosterCycle(entries, playPosterIntro(entries));
  });
}

function renderPrizeBoard() {
  const board = byId('prizeBoard'), host = byId('prizeBoardRows');
  board.hidden = currentView !== 'overview';
  byId('presentation').classList.remove('has-prize-board');
  if (!boardPrizes.length) {
    host.replaceChildren(); boardCards = []; boardSignature = '';
    byId('posterPrizes').replaceChildren();
    byId('startShowDraw').disabled = true;
    text('overviewMessage', 'No rewards yet'); return;
  }
  const next = nextUnfinishedPrize();
  byId('startShowDraw').disabled = !next || projectorDrawing || Boolean(pendingPrize);
  text('startShowDraw', next ? `Start draw · Prize #${prizeNumber(next) || boardPrizes.length}` : 'No prizes yet');
  text('overviewMessage', '');
  const pages = Math.max(1, Math.ceil(boardPrizes.length / boardPageSize));
  boardPage %= pages;
  text('prizeBoardPage', pages > 1 ? `${boardPage + 1} / ${pages}` : '');
  const pagePrizes = boardPrizes.slice(boardPage * boardPageSize, boardPage * boardPageSize + boardPageSize);
  const signature = JSON.stringify({page:boardPage, sequence:lastRenderedSequence, prizes:pagePrizes});
  if (signature !== boardSignature) {
    boardCards = pagePrizes.map(createRewardCard);
    host.replaceChildren(...boardCards.map(card => card.row));
    renderPosterPrizes(pagePrizes);
    boardSignature = signature;
  }
  Array.from(byId('posterPrizes').children).forEach(button => {
    button.disabled = projectorDrawing || Boolean(pendingPrize) || ['spinning','winner_selected'].includes(currentPhase);
  });
  pagePrizes.forEach((prize, index) => {
    const card = boardCards[index], person = prize.winners[boardWinnerIndex % prize.winners.length];
    card.row.disabled = projectorDrawing || Boolean(pendingPrize) || ['spinning','winner_selected'].includes(currentPhase);
    card.winner.hidden = !person;
    card.winner.style.display = person ? '' : 'none';
    card.winner.textContent = person ? [person.name, person.employee_id, person.department].filter(Boolean).join(' · ') : '';
  });
  byId('boardPrevious').hidden = pages <= 1; byId('boardNext').hidden = pages <= 1;
}
function openPrize(id) {
  if (projectorDrawing || pendingPrize || winnerHoldTimer !== null || projectorDigits?.isSpinning() || ['spinning','winner_selected'].includes(currentPhase)) return;
  localPrizePreviewId = id; currentView = 'draw'; text('projectorMessage', '');
  renderView();
}
function startShow() {
  const next = nextUnfinishedPrize();
  if (next) openPrize(next.id);
}
function advancePrize() {
  const index = boardPrizes.findIndex(prize => prize.id === selectedPrizeId);
  if (index < 0 || projectorDrawing || pendingPrize || winnerHoldTimer !== null || projectorDigits?.isSpinning()
      || ['spinning','winner_selected'].includes(currentPhase)) return;
  const next = boardPrizes[index + 1];
  if (next) openPrize(next.id);
  else { currentView = 'overview'; renderView(); }
}
function renderView() {
  if (!latestState) return;
  byId('presentation').scrollTop = 0;
  if (latestState.phase === 'waiting' || latestState.prize) openingWelcome = false;
  const preview = boardPrizes.find(prize => prize.id === localPrizePreviewId);
  const selected = preview || (latestState.phase !== 'waiting' && boardPrizes.find(prize => prize.name === latestState.prize));
  currentView = selected ? 'draw' : 'welcome';
  byId('presentation').classList.toggle('is-welcome', currentView === 'welcome');
  byId('presentation').classList.toggle('is-overview', currentView === 'overview');
  byId('presentation').classList.toggle('is-draw-view', currentView === 'draw');
  byId('drawNavigation').hidden = true;
  renderPrizeBoard();
  if (!selected) {
    if (prizeAnimation && requestedPrize?.id !== null) {
      prizeAnimation.cancel();
      prizeAnimation = null;
      const track = byId('prizeTrack');
      track.children[1].remove();
      track.classList.remove('is-sliding');
    }
    pendingReelState = null;
    byId('drawStage').hidden = true;
    byId('drawStage').classList.remove('is-revealed');
    byId('winnerStage').hidden = true;
    projectorDigits?.reset();
    projectorAudio?.reset();
    renderPrizeGroup(null, {});
    return;
  }
  byId('drawStage').hidden = false;
  const active = !preview && selected.name === latestState.prize;
  const displayState = active ? latestState : {...latestState, prize:selected.name, round_name:'', phase:'waiting', winner:null};
  const number = prizeNumber(selected) || boardPrizes.length - boardPrizes.indexOf(selected);
  displayState.prize_image_url = selected.has_image ? `/api/rewards/image?id=${encodeURIComponent(selected.id)}&v=${lastRenderedSequence}` : null;
  renderPrizeGroup(selected.id, displayState, `PRIZE #${number}`);
  const switching = Boolean(prizeAnimation) || displayedPrize?.id !== selected.id;
  if (switching) {
    if (['spinning','winner_selected'].includes(displayState.phase)) pendingReelState = displayState;
  } else {
    const pending = pendingReelState;
    pendingReelState = null;
    if (displayState.phase === 'winner_announced' && displayState.winner) {
      if (pending?.sequence === displayState.sequence && pending.prize === selected.name) showDraw(pending);
      else if (displayState.spin_started_at && Number(displayState.sequence) !== initialSequence
          && digitSequence !== displayState.sequence && !reducedMotion.matches
          && winnerIdentity(displayState) !== lastRevealedDraw
          && displayState.show_employee_id !== false
          && window.EventDeskDigitDraw?.normalize(displayState.winner.employee_id)) {
        // A projector that was polling slowly can receive the confirmed result first.
        showDraw({...displayState, phase:'winner_selected'});
      }
      showWinner(displayState);
    } else showDraw(displayState);
  }
  const status = window.EventDeskStorage?.current(), retry = Boolean(pendingPrize) || status?.uncertain === true;
  const busy = switching || winnerHoldTimer !== null || projectorDigits?.isSpinning() || projectorDrawing
    || Boolean(pendingPrize) || ['spinning','winner_selected'].includes(currentPhase);
  const nextToDraw = nextUnfinishedPrize();
  text('showProgress', nextToDraw ? `Up next: #${prizeNumber(nextToDraw) || boardPrizes.length}` : 'No prizes yet');
  const helperReady = typeof window.EventDeskStorage?.prepareOperation === 'function';
  byId('confirmProjectorDraw').disabled = !helperReady || switching || projectorDrawing || status?.offline === true
    || Boolean(pendingPrize && pendingPrize.id !== selected.id)
    || (!retry && (busy || !Number(latestState.eligible)));
  byId('confirmProjectorDraw').textContent = !helperReady ? 'Refresh to draw' : retry ? 'Retry draw'
    : selected.winners.length ? 'Draw again' : 'Draw';
  byId('prizeNext').disabled = busy;
  byId('prizeNext').textContent = boardPrizes.indexOf(selected) < boardPrizes.length - 1 ? 'Next prize' : 'Finish show';
  byId('showRewards').disabled = busy;
}

async function requestProjectorDraw(prize) {
  const storage = window.EventDeskStorage;
  if (!storage?.prepareOperation) throw new Error('Refresh this page before drawing.');
  if (storage.current().offline) throw new Error('Draws are unavailable offline.');
  const retryOnly = Boolean(pendingPrize) || storage.current().uncertain;
  const data = retryOnly ? await storage.findPendingDraw([prize.id], undefined, projectorCsrf) : {prize_id:prize.id};
  if (!data) throw new Error('Last save is not confirmed. Retry the original action in the same signed-in session.');
  const body = JSON.stringify(data);
  const key = JSON.stringify(['/api/draw', 'POST', body, projectorCsrf]);
  if (pendingPrize && pendingPrize.key !== key) throw new Error('Retry the same prize before drawing another.');
  const operationId = await storage.prepareOperation(key, {retryOnly}), observation = storage.beginRequest();
  const wasUncertain = storage.operationUncertain?.(operationId) ?? storage.current().uncertain;
  pendingPrize = {id:prize.id, key};
  const uncertain = () => storage.markUncertain(operationId, observation);
  let response, result;
  try {
    response = await fetch('/api/draw', {method:'POST', credentials:'same-origin', body,
      headers:{'Content-Type':'application/json', 'X-CSRF-Token':projectorCsrf, 'X-EventDesk-Operation-Id':operationId}});
  } catch (_) { uncertain(); throw new Error('Connection lost. Retry the same prize.'); }
  try {
    if (!(response.headers.get('Content-Type') || '').includes('json')) throw new Error('Invalid reply');
    result = await response.json();
    if (!result || typeof result !== 'object' || Array.isArray(result)
        || response.ok && (!result.winner || typeof result.winner !== 'object' || Array.isArray(result.winner))
        || !response.ok && typeof result.error !== 'string' && typeof result.message !== 'string') throw new Error('Invalid reply');
  } catch (_) { uncertain(); throw new Error('Cannot read the server reply. Retry the same prize.'); }
  const receipt = response.headers.get('X-EventDesk-Operation-Resolved');
  if (receipt && receipt !== operationId) { uncertain(); throw new Error('Cannot confirm the draw. Retry the same prize.'); }
  if (response.status >= 500) uncertain();
  const resolved = response.ok || receipt === operationId || response.status >= 400 && response.status < 500 && !wasUncertain;
  if (resolved && await storage.completeOperation(operationId) !== false) pendingPrize = null;
  storage.update(response.headers, result, observation);
  if (!response.ok) throw new Error(result.error || result.message || 'Could not start draw.');
  return result;
}

async function drawProjectorPrize(prize) {
  projectorActivity?.touch();
  if (projectorDrawing || !prize || prizeAnimation || winnerHoldTimer !== null
      || (requestedPrize && displayedPrize?.id !== requestedPrize.id)) return;
  projectorDrawing = true; renderView(); text('projectorMessage', 'Starting draw…');
  try {
    const result = await requestProjectorDraw(prize);
    window.EventDeskStorage.notifyProjectorDraw?.(result.winner);
    localDrawWinner = {prizeId:prize.id, employeeId:result.winner.employee_id,
      sequence:Number(latestState?.sequence ?? -1) + 1};
    text('projectorMessage', 'Draw started');
    try { await loadState({fresh:true}); } catch (_) { text('projectorMessage', 'Draw saved. Reconnecting.'); }
  } catch (error) { text('projectorMessage', error.message || 'Connection lost. Check the result first.'); }
  finally { projectorDrawing = false; renderView(); }
}
function render(state) {
  const sequence = Number(state.sequence ?? 0);
  initialSequence ??= sequence;
  const phase = phaseOrder[state.phase] ?? 0;
  if (sequence < lastRenderedSequence || (sequence === lastRenderedSequence && phase < lastRenderedPhase)) return;
  // The server may advance while the audience is still watching the final reel.
  if ((projectorDigits?.isSpinning() || winnerHoldTimer !== null) && sequence > lastRenderedSequence) {
    if (!deferredProjectorState || sequence > deferredProjectorState.sequence ||
        (sequence === deferredProjectorState.sequence && phase >= phaseOrder[deferredProjectorState.phase])) {
      deferredProjectorState = state;
    }
    return;
  }
  if (sequence > lastRenderedSequence && (state.prize === null || (state.prize && state.phase !== 'waiting'))) {
    localPrizePreviewId = null;
  }
  const {server_time, ...displayState} = state;
  const signature = JSON.stringify(displayState);
  if (signature === lastRenderedSignature) { latestState = state; return; }
  lastRenderedSignature = signature;
  lastRenderedSequence = sequence; lastRenderedPhase = phase;
  currentPhase = state.phase;
  document.body.dataset.theme = ['gold', 'indigo', 'emerald'].includes(state.theme) ? state.theme : 'gold';
  const eligible = Number(state.eligible || 0);
  text('eligibleCount', `${eligible.toLocaleString()} eligible employee${eligible === 1 ? '' : 's'}`);
  document.title = `Projector | ${state.event_name || 'EventDesk'}`;
  boardPrizes = Array.isArray(state.prizes) ? [...state.prizes].sort((left, right) => prizeNumber(right) - prizeNumber(left)) : [];
  boardSelected = state.prize || '';
  latestState = state; renderView();
}
function loadState({fresh = false} = {}) {
  if (stateRead) {
    if (fresh) return stateRead.catch(() => {}).then(() => loadState());
    return stateRead;
  }
  stateRead = fetchProjectorState().finally(() => { stateRead = null; });
  return stateRead;
}

async function fetchProjectorState() {
  const observation = window.EventDeskStorage?.beginRequest();
  const response = await fetch('/api/projector/state', {credentials: 'same-origin'});
  if (response.status === 401 || response.status === 403) throw Object.assign(new Error('access'), {status: response.status});
  if (!response.ok) throw new Error('state');
  const state = await response.json();
  window.EventDeskStorage?.update(response.headers, state, observation);
  render(state);
  if (projectorStatePollingAllowed()) setConnection('online', 'Live');
  return state;
}
async function initialize() {
  startProjectorBackground();
  const nameplateReady = Promise.allSettled([
    document.fonts?.load('700 32px Cinzel', 'AWARD'),
    document.fonts?.load('600 32px Pridi', 'รางวัล')
  ]);
  try {
    let me = await fetch('/api/me', {credentials: 'same-origin'}).then(response => response.json());
    if (!me.authenticated) {
      const session = await fetch('/api/staff-session', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json'}, body:'{}'});
      if (session.ok) me = await session.json();
    }
    if (!me.authenticated || !['admin', 'staff', 'lucky_draw_operator'].includes(me.role)) {
      byId('presentation').hidden = true; byId('accessScreen').hidden = false; return;
    }
    projectorCsrf = me.csrf;
    await nameplateReady;
    // Bootstrap the display once even if automatic connections cannot be started.
    projectorReady = true;
    projectorAudio?.open();
    if (projectorStatePollingAllowed()) await refreshProjectorState();
    else {
      await loadState();
      if (!projectorStatePollingAllowed()) setConnection('offline', 'Paused');
    }
  } catch (_) { setConnection('offline', 'Disconnected'); }
}
byId('soundToggle').addEventListener('click', () => {
  if (soundStatus === 'blocked') projectorAudio?.unlock();
  else projectorAudio?.toggle();
});
document.addEventListener('pointerdown', event => {
  if (event.target !== byId('soundToggle')) projectorAudio?.unlock();
});
document.addEventListener('keydown', event => {
  if (event.key?.toLowerCase() === 'm' && !event.repeat && !event.altKey && !event.ctrlKey && !event.metaKey) {
    event.preventDefault();
    projectorAudio?.toggle();
  } else projectorAudio?.unlock();
});
window.addEventListener('storage', event => {
  if (event.key === window.EventDeskProjectorAudio?.volumeKey) projectorAudio.reloadVolumes();
});
window.addEventListener('pagehide', () => projectorAudio?.close());
window.addEventListener('pageshow', event => {
  if (event.persisted && projectorReady) projectorAudio?.open();
});
byId('fullscreenToggle').addEventListener('click', async () => {
  try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); } catch (_) {}
});
byId('confirmProjectorDraw').addEventListener('click', () => drawProjectorPrize(boardPrizes.find(prize => prize.id === selectedPrizeId)));
byId('startShowDraw').addEventListener('click', startShow);
byId('showRewards').addEventListener('click', () => { currentView = 'overview'; renderView(); });
byId('boardPrevious').addEventListener('click', () => { boardPage = (boardPage + Math.ceil(boardPrizes.length / boardPageSize) - 1) % Math.ceil(boardPrizes.length / boardPageSize); renderPrizeBoard(); });
byId('boardNext').addEventListener('click', () => { boardPage += 1; renderPrizeBoard(); });
byId('prizeNext').addEventListener('click', advancePrize);
document.addEventListener('fullscreenchange', () => text('fullscreenToggle', document.fullscreenElement ? 'Exit full screen' : 'Full screen'));
window.addEventListener('online', () => {
  if (projectorStatePollingAllowed()) { setConnection('connecting', 'Connecting'); refreshProjectorState(); }
});
window.addEventListener('offline', () => {
  if (projectorStatePollingAllowed()) setConnection('offline', 'Offline');
});
window.addEventListener('beforeunload', () => { projectorAudio?.close(); pauseProjectorBackground(); });
window.EventDeskStorage?.subscribe(() => { if (latestState) renderView(); });
// Rotate winner labels in existing cards; keep their images attached.
setInterval(() => { if (projectorBackgroundAllowed() && boardPrizes.length) { boardWinnerIndex += 1; renderPrizeBoard(); } }, 8000);
initialize();
