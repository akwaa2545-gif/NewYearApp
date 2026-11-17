'use strict';

const byId = id => document.getElementById(id);
if (new URLSearchParams(location.search).get('visual') === 'childhood') document.body.dataset.visual = 'childhood';
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let stream = null, activeSequence = -1, wheelAnimation = null, audioContext = null;
let soundEnabled = localStorage.getItem('eventdesk-projector-sound') === 'on';
let winnerSoundReady = false, winnerSoundPlaying = false;
let boardPrizes = [], boardPage = 0, boardWinnerIndex = 0, boardSelected = '';
let refreshInFlight = false, lastRenderedSequence = -1, lastRenderedPhase = -1;
let projectorCsrf = '', projectorDrawing = false, currentPhase = 'waiting', pendingPrize = null;
let currentView = 'overview', latestState = null, selectedPrizeId = null;
let wheelIds = [], wheelIdOffset = 0, wheelIdsLoading = false;
const phaseOrder = {waiting:0, preparing:1, spinning:2, winner_selected:3, winner_announced:4, ready_for_next:5};
const shortRewardNames = [
  [/Yamaha Grand Filano/i, 'Grand Filano 2026'],
  [/iPhone 18 Pro Max/i, 'iPhone 18 Pro Max'],
  [/iPad Pro/i, 'iPad Pro 11-inch'],
  [/Samsung Galaxy S26 Ultra/i, 'Galaxy S26 Ultra'],
  [/Bosch.*Dryer/i, 'Bosch 10 kg Dryer'],
  [/Bosch.*Washing Machine/i, 'Bosch 10 kg Washer'],
  [/Gold Necklace/i, 'Gold Necklace ¼ Baht'],
  [/LG.*Refrigerator/i, 'LG Refrigerator'],
  [/Samsung.*Smart TV/i, 'Samsung 55-inch TV'],
  [/Apple Watch Series 12/i, 'Apple Watch Series 12'],
];
function projectorRewardName(name) {
  return shortRewardNames.find(([pattern]) => pattern.test(name || ''))?.[1] || name;
}

function text(id, value) { const node = byId(id); if (node) node.textContent = value ?? ''; }
function renderWheelIds() {
  const host = byId('wheelEmployeeIds'); host.replaceChildren();
  const count = wheelIds.length ? 12 : 0;
  for (let i = 0; i < count; i += 1) {
    const segment = document.createElement('div'); segment.className = 'wheel-id-segment';
    segment.style.transform = `rotate(${i * 360 / count}deg)`;
    const label = document.createElement('span'); label.textContent = wheelIds[(wheelIdOffset + i) % wheelIds.length];
    segment.append(label); host.append(segment);
  }
}
async function loadWheelIds() {
  if (wheelIdsLoading) return;
  wheelIdsLoading = true;
  try {
    const response = await fetch('/api/draw/eligible-ids', {credentials:'same-origin'});
    if (!response.ok) throw new Error('Unable to load eligible IDs');
    const data = await response.json(); wheelIds = Array.isArray(data.items) ? data.items : [];
    if (wheelIds.length) wheelIdOffset %= wheelIds.length; else wheelIdOffset = 0;
    renderWheelIds();
  } catch (_) { /* Keep the most recent preview while reconnecting. */ }
  finally { wheelIdsLoading = false; }
}
function phaseCopy(phase) {
  return ({
    waiting: ['Waiting', ''],
    preparing: ['Get ready', ''],
    spinning: ['Spinning', ''],
    winner_selected: ['Winner selected', ''],
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
  if (host.childElementCount || reducedMotion.matches) return;
  for (let index = 0; index < 42; index += 1) {
    const item = document.createElement('i');
    item.className = 'confetti';
    item.style.left = `${2 + Math.random() * 96}%`;
    item.style.setProperty('--delay', `${-Math.random() * 4}s`);
    item.style.setProperty('--fall', `${3.2 + Math.random() * 2.8}s`);
    item.style.setProperty('--drift', `${-8 + Math.random() * 16}vw`);
    host.append(item);
  }
}
function setSoundButton() {
  const button = byId('soundToggle');
  button.setAttribute('aria-pressed', String(soundEnabled));
  button.textContent = soundEnabled ? 'Sound on' : 'Sound off';
  button.title = soundEnabled ? 'Mute celebration sound' : 'Enable celebration sound';
}
function sendWinnerSoundCommand(func, args = []) {
  const player = byId('winnerSoundPlayer');
  if (!winnerSoundReady || !player?.contentWindow) return false;
  player.contentWindow.postMessage(JSON.stringify({event: 'command', func, args}), 'https://www.youtube-nocookie.com');
  return true;
}
function stopWinnerSound() {
  sendWinnerSoundCommand('pauseVideo');
  sendWinnerSoundCommand('seekTo', [0, true]);
  winnerSoundPlaying = false;
}
function primeWinnerSound() {
  if (!sendWinnerSoundCommand('mute')) return;
  sendWinnerSoundCommand('playVideo');
  setTimeout(() => {
    if (winnerSoundPlaying) return;
    sendWinnerSoundCommand('pauseVideo');
    sendWinnerSoundCommand('seekTo', [0, true]);
    if (soundEnabled) sendWinnerSoundCommand('unMute');
  }, 180);
}
function playFallbackCelebration() {
  try {
    audioContext ??= new AudioContext();
    const start = audioContext.currentTime;
    [523.25, 659.25, 783.99, 1046.5].forEach((frequency, index) => {
      const oscillator = audioContext.createOscillator(), gain = audioContext.createGain();
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(.0001, start + index * .1);
      gain.gain.exponentialRampToValueAtTime(.16, start + index * .1 + .02);
      gain.gain.exponentialRampToValueAtTime(.0001, start + index * .1 + .34);
      oscillator.connect(gain).connect(audioContext.destination);
      oscillator.start(start + index * .1); oscillator.stop(start + index * .1 + .36);
    });
  } catch (_) {}
}
function playCelebration() {
  if (!soundEnabled) return;
  sendWinnerSoundCommand('seekTo', [0, true]);
  sendWinnerSoundCommand('unMute');
  sendWinnerSoundCommand('setVolume', [100]);
  if (sendWinnerSoundCommand('playVideo')) winnerSoundPlaying = true;
  else playFallbackCelebration();
}
function startWheel(state) {
  if (activeSequence === state.sequence && wheelAnimation) return;
  activeSequence = state.sequence; wheelAnimation?.cancel();
  const wheel = byId('projectorWheel');
  if (reducedMotion.matches) { wheel.style.transform = 'rotate(18deg)'; return; }
  let duration = 5000;
  if (state.reveal_at && state.server_time) duration = Math.max(300, new Date(state.reveal_at) - new Date(state.server_time));
  const current = Number(wheel.dataset.rotation || 0);
  const finish = current + 360 * 8 + 120 + Math.floor(Math.random() * 200);
  wheel.dataset.rotation = String(finish);
  wheelAnimation = wheel.animate(
    [{transform: `rotate(${current}deg)`}, {transform: `rotate(${finish}deg)`}],
    {duration, easing: 'cubic-bezier(.12,.72,.16,1)', fill: 'forwards'},
  );
}
function showWinner(state) {
  const winner = state.winner; if (!winner) return;
  try { wheelAnimation?.finish(); } catch (_) {}
  text('winnerPrize', projectorRewardName(winner.prize || state.prize) || 'Prize');
  text('projectorWinnerName', winner.name || 'Winner');
  text('projectorWinnerId', winner.employee_id || '');
  text('projectorWinnerMeta', winner.department || '');
  text('winnerRound', winner.round_name || state.round_name || '');
  byId('winnerStage').hidden = false; byId('drawStage').hidden = false;
  text('projectorPrize', projectorRewardName(state.prize) || 'Reward'); text('projectorRound', state.round_name || ''); createConfetti();
  if (byId('winnerStage').dataset.sequence !== String(state.sequence)) {
    byId('winnerStage').dataset.sequence = String(state.sequence); playCelebration();
  }
}
function showDraw(state) {
  stopWinnerSound();
  byId('winnerStage').hidden = true; byId('drawStage').hidden = false;
  text('projectorPrize', projectorRewardName(state.prize) || 'Prize Name'); text('projectorRound', state.round_name || '');
  const copy = phaseCopy(state.phase); text('phaseTitle', copy[0]); text('phaseDetail', copy[1]);
  if (state.phase === 'spinning' || state.phase === 'winner_selected') startWheel(state);
}
function renderPrizeImage(state) {
  const url = state.prize_image_url || '';
  const drawImage = byId('projectorPrizeImage'), winnerImage = byId('winnerPrizeImage');
  const drawVisual = byId('projectorPrizeVisual');
  if (url) {
    if (drawImage.getAttribute('src') !== url) drawImage.src = url;
    if (winnerImage.getAttribute('src') !== url) winnerImage.src = url;
    const alt = state.prize ? `Prize: ${state.prize}` : 'Prize picture';
    drawImage.alt = alt; winnerImage.alt = alt;
    drawVisual.hidden = false; winnerImage.hidden = false;
  } else {
    drawImage.removeAttribute('src'); winnerImage.removeAttribute('src');
    drawVisual.hidden = true; winnerImage.hidden = true;
  }
  byId('drawStage').classList.toggle('has-prize-image', Boolean(url));
  byId('winnerStage').classList.toggle('has-prize-image', Boolean(url));
}
function renderPrizeBoard() {
  const board = byId('prizeBoard');
  const host = byId('prizeBoardRows');
  const count = boardPrizes.length;
  board.hidden = currentView !== 'overview';
  byId('presentation').classList.remove('has-prize-board');
  if (!count) { host.replaceChildren(); text('overviewMessage', 'No rewards yet'); return; }
  text('overviewMessage', '');
  const pages = Math.max(1, Math.ceil(count / 10));
  boardPage %= pages;
  text('prizeBoardPage', pages > 1 ? `${boardPage + 1} / ${pages}` : '');
  host.replaceChildren();
  const pagePrizes = boardPrizes.slice(boardPage * 10, boardPage * 10 + 10);
  pagePrizes.forEach((prize, index) => {
    const row = document.createElement('button'); row.type = 'button'; row.className = 'reward-card';
    row.title = prize.name;
    if (/Yamaha Grand Filano/i.test(prize.name)) row.classList.add('reward-card--scooter');
    if (/LG.*Refrigerator/i.test(prize.name)) row.classList.add('reward-card--fridge');
    row.disabled = projectorDrawing || ['spinning','winner_selected'].includes(currentPhase);
    row.addEventListener('click', () => openPrize(prize.id));
    const reward = document.createElement('div'); reward.className = 'reward-card-name';
    const name = document.createElement('strong'); name.textContent = projectorRewardName(prize.name);
    const progress = document.createElement('small'); progress.textContent = `${prize.winners.length} of ${prize.quantity} drawn`;
    const number = document.createElement('span'); number.className = 'reward-card-number'; number.textContent = String(boardPage * 10 + index + 1).padStart(2, '0');
    const visual = document.createElement('div'); visual.className = 'reward-card-visual';
    if (prize.has_image) { const picture = document.createElement('img'); picture.src = `/api/rewards/image?id=${encodeURIComponent(prize.id)}&v=${lastRenderedSequence}`; picture.alt = ''; visual.append(picture); row.classList.add('has-image'); }
    else row.classList.add('text-only');
    reward.append(name, progress);
    const winner = document.createElement('div'); winner.className = 'reward-card-winner';
    const person = prize.winners[boardWinnerIndex % prize.winners.length];
    winner.textContent = person ? [person.name, person.employee_id, person.department].filter(Boolean).join(' · ') : '';
    row.append(number);
    if (prize.has_image) row.append(visual);
    row.append(reward);
    if (person) row.append(winner);
    host.append(row);
  });
  byId('boardPrevious').hidden = pages <= 1; byId('boardNext').hidden = pages <= 1;
}
function openPrize(id) {
  if (projectorDrawing || ['spinning','winner_selected'].includes(currentPhase)) return;
  selectedPrizeId = id; currentView = 'draw'; text('projectorMessage', ''); renderView();
}
function renderView() {
  if (!latestState) return;
  byId('presentation').scrollTop = 0;
  const selected = boardPrizes.find(prize => prize.id === selectedPrizeId);
  if (!selected && currentView === 'draw') currentView = 'overview';
  byId('presentation').classList.toggle('is-overview', currentView === 'overview');
  byId('presentation').classList.toggle('is-draw-view', currentView === 'draw');
  byId('drawNavigation').hidden = currentView !== 'draw';
  renderPrizeBoard();
  if (currentView === 'overview') { byId('drawStage').hidden = true; byId('winnerStage').hidden = true; return; }
  const active = selected.name === latestState.prize;
  const displayState = active ? latestState : {...latestState, prize:selected.name, round_name:'', phase:'waiting', winner:null};
  displayState.prize_image_url = selected.has_image ? `/api/rewards/image?id=${encodeURIComponent(selected.id)}&v=${lastRenderedSequence}` : null;
  renderPrizeImage(displayState);
  if (displayState.phase === 'winner_announced' && displayState.winner) showWinner(displayState); else showDraw(displayState);
  const busy = projectorDrawing || ['spinning','winner_selected'].includes(currentPhase);
  byId('confirmProjectorDraw').disabled = busy || selected.winners.length >= selected.quantity || !Number(latestState.eligible);
  byId('prizePrevious').disabled = busy; byId('prizeNext').disabled = busy; byId('showRewards').disabled = busy;
}
async function drawProjectorPrize(prize) {
  if (projectorDrawing || !prize) return;
  projectorDrawing = true; renderView(); text('projectorMessage', 'Starting draw…');
  try {
    const response = await fetch('/api/draw', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json', 'X-CSRF-Token':projectorCsrf}, body:JSON.stringify({prize_id:prize.id})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || result.message || 'Could not start draw.');
    text('projectorMessage', 'Draw started'); await loadState();
  } catch (error) { text('projectorMessage', error.message || 'Connection lost. Check the result first.'); }
  finally { projectorDrawing = false; renderView(); }
}
function render(state) {
  const sequence = Number(state.sequence ?? 0);
  const phase = phaseOrder[state.phase] ?? 0;
  if (sequence < lastRenderedSequence || (sequence === lastRenderedSequence && phase < lastRenderedPhase)) return;
  lastRenderedSequence = sequence; lastRenderedPhase = phase;
  const isNewSpin = state.phase === 'spinning' && (currentPhase !== 'spinning' || !latestState || latestState.sequence !== state.sequence);
  currentPhase = state.phase;
  document.body.dataset.theme = ['gold', 'indigo', 'emerald'].includes(state.theme) ? state.theme : 'gold';
  const eligible = Number(state.eligible || 0);
  text('eligibleCount', `${eligible.toLocaleString()} eligible employee${eligible === 1 ? '' : 's'}`);
  document.title = `Projector | ${state.event_name || 'EventDesk'}`;
  boardPrizes = Array.isArray(state.prizes) ? state.prizes : [];
  boardSelected = state.prize || '';
  if (isNewSpin) { const spinningPrize = boardPrizes.find(prize => prize.name === state.prize); if (spinningPrize) { selectedPrizeId = spinningPrize.id; currentView = 'draw'; } }
  latestState = state; renderView();
}
async function loadState() {
  const response = await fetch('/api/projector/state', {credentials: 'same-origin'});
  if (response.status === 401 || response.status === 403) throw Object.assign(new Error('access'), {status: response.status});
  if (!response.ok) throw new Error('state');
  const state = await response.json(); render(state); return state;
}
function connect() {
  stream?.close(); stream = new EventSource('/api/projector/events', {withCredentials: true});
  stream.addEventListener('open', () => setConnection('online', 'Live'));
  stream.addEventListener('draw-state', event => { setConnection('online', 'Live'); try { render(JSON.parse(event.data)); } catch (_) {} });
  stream.onerror = () => { setConnection('offline', 'Reconnecting'); loadState().catch(() => {}); };
}
async function initialize() {
  byId('winnerSoundPlayer').addEventListener('load', () => {
    winnerSoundReady = true;
    if (soundEnabled && byId('winnerStage').hidden) primeWinnerSound();
  });
  setSoundButton();
  try {
    const me = await fetch('/api/me', {credentials: 'same-origin'}).then(response => response.json());
    if (!me.authenticated || !['admin', 'staff', 'lucky_draw_operator'].includes(me.role)) {
      byId('presentation').hidden = true; byId('accessScreen').hidden = false; return;
    }
    projectorCsrf = me.csrf;
    await loadState(); await loadWheelIds(); connect();
  } catch (_) { setConnection('offline', 'Disconnected'); }
}
byId('soundToggle').addEventListener('click', async () => {
  soundEnabled = !soundEnabled; localStorage.setItem('eventdesk-projector-sound', soundEnabled ? 'on' : 'off'); setSoundButton();
  if (soundEnabled) {
    try { audioContext ??= new AudioContext(); await audioContext.resume(); } catch (_) {}
    if (byId('winnerStage').hidden) primeWinnerSound(); else playCelebration();
  }
  else stopWinnerSound();
});
byId('fullscreenToggle').addEventListener('click', async () => {
  try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); } catch (_) {}
});
byId('confirmProjectorDraw').addEventListener('click', () => drawProjectorPrize(boardPrizes.find(prize => prize.id === selectedPrizeId)));
byId('showRewards').addEventListener('click', () => { currentView = 'overview'; renderView(); });
byId('boardPrevious').addEventListener('click', () => { boardPage = (boardPage + Math.ceil(boardPrizes.length / 10) - 1) % Math.ceil(boardPrizes.length / 10); renderPrizeBoard(); });
byId('boardNext').addEventListener('click', () => { boardPage += 1; renderPrizeBoard(); });
byId('prizePrevious').addEventListener('click', () => { const index = boardPrizes.findIndex(item => item.id === selectedPrizeId); if (index <= 0) { currentView = 'overview'; renderView(); } else openPrize(boardPrizes[index - 1].id); });
byId('prizeNext').addEventListener('click', () => { const index = boardPrizes.findIndex(item => item.id === selectedPrizeId); if (index < boardPrizes.length - 1) openPrize(boardPrizes[index + 1].id); });
document.addEventListener('fullscreenchange', () => text('fullscreenToggle', document.fullscreenElement ? 'Exit full screen' : 'Full screen'));
window.addEventListener('online', () => { setConnection('connecting', 'Connecting'); connect(); });
window.addEventListener('offline', () => setConnection('offline', 'Offline'));
window.addEventListener('beforeunload', () => { stopWinnerSound(); stream?.close(); });
setInterval(() => { if (boardPrizes.length) { boardWinnerIndex += 1; renderPrizeBoard(); } }, 8000);
setInterval(() => { if (wheelIds.length > 12 && !['spinning','winner_selected'].includes(currentPhase)) { wheelIdOffset = (wheelIdOffset + 12) % wheelIds.length; renderWheelIds(); } }, 3000);
setInterval(() => { if (!document.hidden) loadWheelIds(); }, 15000);
setInterval(async () => {
  if (refreshInFlight || !byId('accessScreen').hidden) return;
  refreshInFlight = true;
  try { await loadState(); } catch (_) { /* The live stream continues reconnecting. */ }
  finally { refreshInFlight = false; }
}, 3000);
initialize();
