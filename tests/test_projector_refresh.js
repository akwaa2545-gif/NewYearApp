const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

function fixture({includeStorage = true, reducedMotion = true, audio} = {}) {
  const nodes = new Map(), requests = [];
  const entries = new Map();
  const localStorage = {get length() { return entries.size; }, key:index => [...entries.keys()][index] ?? null,
    getItem:key => entries.get(key) ?? null, setItem:(key, value) => entries.set(key, value), removeItem:key => entries.delete(key)};
  function element(id = '') {
    const attributes = new Map(), classes = new Set();
    return {id, children:[], dataset:{}, style:{setProperty() {}}, hidden:false, listeners:{},
      complete:id === 'img', naturalWidth:id === 'img' ? 100 : 0,
      classList:{add:(...names) => names.forEach(name => classes.add(name)), remove:(...names) => names.forEach(name => classes.delete(name)),
        contains:name => classes.has(name),
        toggle(name, force) { if (force ?? !classes.has(name)) classes.add(name); else classes.delete(name); }},
      addEventListener(name, callback) { this.listeners[name] = callback; }, setAttribute:(name, value) => attributes.set(name, value),
      getAttribute:name => attributes.get(name) ?? null, removeAttribute:name => attributes.delete(name),
      hasAttribute:name => attributes.has(name),
      set src(value) { attributes.set('src', value); }, get src() { return attributes.get('src'); },
      replaceWith(next) { nodes.set(this.id, next); },
      append(...items) { for (const item of items) { this.children.push(item); item.parentElement = this; } },
      remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); },
      replaceChildren(...items) { this.children = []; this.append(...items); },
      get firstElementChild() { return this.children[0] || null; },
      querySelector() { return this.children[0] ||= element(); },
      get childElementCount() { return this.children.length; },
    };
  }
  const node = id => {
    if (id === 'prizePlaqueLayer') return {content:{cloneNode:() => element('svg')}};
    if (id === 'mysteryPrizeLayer') return {content:{cloneNode:() => element('mystery-svg')}};
    for (const group of nodes.get('prizeTrack')?.children || []) {
      for (const part of group.children) {
        if (part.id === id) return part;
        if (part.firstElementChild?.id === id) return part.firstElementChild;
      }
    }
    if (!nodes.has(id)) nodes.set(id, element(id));
    return nodes.get(id);
  };
  const context = vm.createContext({document:{getElementById:node, createElement:element,
    body:element(), addEventListener() {}}, window:{addEventListener() {}, crypto:webcrypto, TextEncoder, localStorage},
    location:{search:''}, URLSearchParams, matchMedia:() => ({matches:reducedMotion}),
    localStorage, setInterval() {}, clearInterval() {}, setTimeout() { return 1; },
    fetch:async path => { requests.push(path); return {ok:true, status:200, json:async () => ({items:[]})}; },
  });
  const run = code => vm.runInContext(code, context);
  if (audio) context.window.EventDeskProjectorAudio = {create:() => audio};
  if (includeStorage) run(fs.readFileSync('static/storage-status.js', 'utf8'));
  run(fs.readFileSync('static/digit-draw.js', 'utf8'));
  run(fs.readFileSync('static/projector.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n'));
  run("projectorReady = true; projectorCsrf = 'fixture-csrf'; projectorActivity = {isActive:() => true, touch() {}};");
  const state = extra => ({sequence:1, phase:'waiting', eligible:10, event_name:'Test event',
    server_time:'2026-11-18T00:00:00Z', prize:'', winner:null,
    prizes:[{id:1, name:'Prize one', quantity:2, has_image:true,
      winners:[{employee_id:'0000001', name:'First'}, {employee_id:'0000002', name:'Second'}]}], ...extra});
  return {node, requests, context, run, state, entries,
    render:value => run(`render(${JSON.stringify(value)})`),
    async settle() { for (let index = 0; index < 20; index++) await Promise.resolve(); },
  };
}

function visibleDigit(cell) {
  const strip = cell.children[0].children[0];
  const row = Math.round(-parseFloat(strip.style.transform.slice(11)) / 100);
  return strip.children[row].textContent;
}

test('the projector finishes seven mechanical reels before showing an announced winner', () => {
  const f = fixture({reducedMotion:false});
  let clock = 0, tick;
  f.context.performance = {now:() => clock};
  f.context.cancelAnimationFrame = () => {};
  f.context.requestAnimationFrame = callback => { tick = callback; return 1; };
  const advance = time => { clock = time; tick(time); };
  const winner = {employee_id:'2606801', name:'Test guest', department:'Operations'};
  const state = {sequence:2, prize:'Prize one', draw_employee_id:winner.employee_id};
  f.render(f.state({...state, phase:'spinning'}));
  const digits = f.node('projectorDigits').children;
  advance(5000);
  assert.equal(visibleDigit(digits[0]), '2');
  f.render(f.state({...state, phase:'winner_announced', winner}));
  advance(21279);
  assert.equal(f.node('projectorDrawWinnerName').hidden, true);
  assert.equal(f.node('projectorWinnerMeta').hidden, true);
  assert.equal(f.node('drawStage').classList.contains('is-revealed'), false);
  advance(21280);
  assert.equal(digits.map(visibleDigit).join(''), winner.employee_id);
  assert.equal(f.node('projectorDrawWinnerName').hidden, false);
  assert.equal(f.node('drawStage').classList.contains('is-revealed'), true);
  assert.equal(f.node('projectorDrawWinnerName').textContent, winner.name);
  assert.equal(f.node('projectorWinnerMeta').hidden, false);
  assert.equal(f.node('projectorWinnerMeta').textContent, winner.department);
});

test('a late projector poll still animates a newly announced winner for the armed prize', () => {
  const f = fixture({reducedMotion:false});
  let callback;
  f.context.performance = {now:() => 0};
  f.context.requestAnimationFrame = next => { callback = next; return 1; };
  f.context.cancelAnimationFrame = () => {};
  f.render(f.state({sequence:2, phase:'ready_for_next', prize:'Prize one'}));
  const winner = {employee_id:'2606801', name:'Test guest'};
  f.render(f.state({sequence:3, phase:'winner_announced', prize:'Prize one',
    spin_started_at:'2026-11-26T00:00:00Z', winner}));
  assert.equal(f.node('projectorDrawWinnerName').hidden, true);
  assert.equal(f.run('projectorDigits.isSpinning()'), true);
  callback(30000);
  assert.equal(f.node('projectorDrawWinnerName').textContent, winner.name);
  assert.equal(f.node('projectorDrawWinnerName').hidden, false);
});

test('a next prize arriving during late reels waits for the saved winner reveal', () => {
  const f = fixture({reducedMotion:false});
  let frame, finishHold;
  f.context.performance = {now:() => 0};
  f.context.requestAnimationFrame = callback => { frame = callback; return 1; };
  f.context.cancelAnimationFrame = () => {};
  f.context.setTimeout = callback => { finishHold = callback; return 1; };
  const prizes = [{id:1, name:'Prize one', quantity:1, has_image:false, winners:[]},
    {id:2, name:'Prize two', quantity:1, has_image:false, winners:[]}];
  const winner = {employee_id:'2606801', name:'Test guest'};
  f.render(f.state({sequence:1, phase:'ready_for_next', prize:'Prize one', prizes}));
  f.render(f.state({sequence:2, phase:'winner_announced', prize:'Prize one', prizes, prize_image_url:null,
    spin_started_at:'2026-11-26T00:00:00Z', show_employee_id:true, winner}));
  f.render(f.state({sequence:3, phase:'ready_for_next', prize:'Prize two', prizes, show_employee_id:true}));
  frame(30000);
  assert.equal(f.node('projectorDrawWinnerName').hidden, false);
  assert.equal(f.node('projectorDrawWinnerName').textContent, winner.name);
  assert.equal(f.run('latestState.prize'), 'Prize one');
  finishHold();
  assert.equal(f.run('latestState.prize'), 'Prize two');
});

test('a reward order change cannot replay the previously saved winner', () => {
  let celebrations = 0;
  const f = fixture({reducedMotion:false, audio:{reset() {}, startDraw() {}, celebrate() { celebrations++; }}});
  f.context.performance = {now:() => 0};
  f.context.requestAnimationFrame = () => 1;
  f.context.cancelAnimationFrame = () => {};
  const winner = {winner_id:4, employee_id:'0000021', name:'Test guest'};
  const saved = {phase:'winner_announced', prize:'Prize one', spin_started_at:'2026-11-26T00:00:00Z', winner};
  f.render(f.state({sequence:4, ...saved}));
  f.render(f.state({sequence:5, ...saved}));
  assert.equal(f.run('projectorDigits.isSpinning()'), false);
  assert.equal(f.node('projectorDrawWinnerName').textContent, winner.name);
  assert.equal(celebrations, 0);
});

test('hiding the employee ID does not replay the same winner fanfare', () => {
  let celebrations = 0;
  const f = fixture({audio:{reset() {}, celebrate() { celebrations++; }}});
  f.render(f.state({sequence:1, phase:'ready_for_next', prize:'Prize one'}));
  const saved = {sequence:2, phase:'winner_announced', prize:'Prize one',
    spin_started_at:'2026-11-26T00:00:00Z', show_employee_id:true};
  f.render(f.state({...saved, winner:{employee_id:'0000021', name:'Test guest'}}));
  f.render(f.state({...saved, show_employee_id:false, winner:{name:'Test guest'}}));
  assert.equal(celebrations, 1);
  assert.equal(f.node('projectorDrawIdFallback').hidden, true);
});

test('unchanged projector snapshots do not rebuild cards, even when server time advances', () => {
  const f = fixture();
  f.run('var renderCount = 0; var originalRenderView = renderView; renderView = () => { renderCount++; originalRenderView(); };');
  f.render(f.state());
  const card = f.node('prizeBoardRows').children[0];
  f.render(f.state({server_time:'2026-11-18T00:00:03Z'}));
  assert.equal(f.run('renderCount'), 1);
  assert.equal(f.node('prizeBoardRows').children[0], card);
});

test('projector stays neutral until staff selects a prize, then shows its draw', () => {
  const f = fixture();
  const names = ['Special Gift form MD', 'Yamaha Grand Filano Hybrid 2026 Motorcycle',
    'iPhone 18 Pro Max 256GB', 'iPad Pro 11 inch', 'Samsung Galaxy S26 Ultra 256GB',
    'Bosch 9 kg Inverter Front-Loading Dryer', 'Bosch 10 kg Front-Loading Washing Machine',
    'Gold Necklace 1 salueng', 'TOSHIBA 16.2Q Two-Door Refrigerator',
    'Samsung Mini LED Smart TV 4K 55"', 'Apple Watch Series 12 GPS'];
  const prizes = names.map((name, index) => ({id:index + 1, name, quantity:1, has_image:true, winners:[]}));
  f.render(f.state({prizes}));
  assert.equal(f.node('prizeTrack').firstElementChild.children[0].id, 'mystery-svg');
  assert.equal(f.node('prizeBoard').hidden, true);
  f.render(f.state({sequence:2, phase:'ready_for_next', prizes}));
  assert.equal(f.node('drawStage').hidden, true);
  assert.equal(f.node('prizeBoard').hidden, true);
  assert.equal(f.node('prizeBoardRows').children.length, 11);
  assert.equal(f.node('prizeBoardRows').children[0].title, names[10]);
  assert.equal(f.node('prizeBoardRows').children[10].title, names[0]);
  f.render(f.state({sequence:3, phase:'spinning', prize:names[10], prizes}));
  assert.equal(f.run('selectedPrizeId'), 11);
  assert.equal(f.node('projectorPrizeNumber').textContent, 'PRIZE #11');
  assert.equal(f.node('drawNavigation').hidden, true);
  prizes[10].winners = [{name:'Winner', employee_id:'0000001'}];
  f.render(f.state({sequence:3, phase:'winner_announced', prize:names[10],
    winner:{name:'Winner', employee_id:'0000001'}, prizes}));
  f.render(f.state({sequence:4, phase:'ready_for_next', prizes}));
  assert.equal(f.node('prizeTrack').firstElementChild.children[0].id, 'mystery-svg');
  assert.equal(f.node('prizeBoard').hidden, true);
  assert.equal(f.run('nextUnfinishedPrize().id'), 10);
});

test('a staff-selected prize replaces the waiting screen', () => {
  const f = fixture();
  f.run('openingWelcome = true');
  f.render(f.state({sequence:2, phase:'ready_for_next', prize:'Prize one'}));
  assert.equal(f.node('drawStage').hidden, false);
  assert.equal(f.node('projectorPrize').textContent, 'Prize one');
});

test('mystery and selected prizes share the slide and clearing cancels an incoming prize', () => {
  const f = fixture({reducedMotion:false}), track = f.node('prizeTrack');
  const slides = [];
  track.animate = (frames, timing) => {
    const slide = {frames, timing, cancel() { this.cancelled = true; }};
    slides.push(slide);
    return slide;
  };
  f.render(f.state());
  assert.equal(track.firstElementChild.children.length, 1);
  assert.equal(track.firstElementChild.children[0].id, 'mystery-svg');
  f.render(f.state({sequence:2, phase:'ready_for_next', prize:'Prize one'}));
  assert.equal(track.children.length, 2);
  assert.deepEqual(Array.from(slides[0].frames, frame => frame.transform), ['translateX(0%)','translateX(100%)']);
  f.render(f.state({sequence:3, phase:'ready_for_next', prize:null}));
  assert.equal(slides[0].cancelled, true);
  slides[0].onfinish();
  assert.equal(track.children.length, 1);
  assert.equal(track.firstElementChild.children[0].id, 'mystery-svg');

  f.render(f.state({sequence:4, phase:'ready_for_next', prize:'Prize one'}));
  slides[1].onfinish();
  assert.equal(f.node('projectorPrize').textContent, 'Prize one');
  f.render(f.state({sequence:5, phase:'ready_for_next', prize:null}));
  slides[2].onfinish();
  assert.equal(track.children.length, 1);
  assert.equal(track.firstElementChild.children[0].id, 'mystery-svg');
  assert.equal(f.node('drawStage').hidden, true);
});

test('a prize image decoded after clearing the selection never reaches the stage', async () => {
  const f = fixture(), track = f.node('prizeTrack');
  const create = f.context.document.createElement;
  let finishDecode;
  f.context.document.createElement = tag => {
    const node = create(tag);
    if (tag === 'img') node.decode = () => new Promise(resolve => { finishDecode = resolve; });
    return node;
  };
  f.render(f.state());
  f.render(f.state({sequence:2, phase:'ready_for_next', prize:'Prize one'}));
  f.render(f.state({sequence:3, phase:'ready_for_next', prize:null}));
  finishDecode();
  await f.settle();
  assert.equal(track.children.length, 1);
  assert.equal(track.firstElementChild.children[0].id, 'mystery-svg');
  assert.equal(f.node('drawStage').hidden, true);
});

test('decoded prize groups share one horizontal animation and coalesce rapid selections', async () => {
  const f = fixture({reducedMotion:false});
  const loads = [], animations = [], track = f.node('prizeTrack');
  track.animate = (frames, timing) => {
    const animation = {frames, timing, cancel() {}};
    animations.push(animation);
    return animation;
  };
  const create = f.context.document.createElement;
  f.context.document.createElement = tag => {
    const element = create(tag);
    if (tag === 'img') element.decode = () => new Promise((resolve, reject) => loads.push({resolve, reject}));
    return element;
  };
  const prizes = ['Tablet','Watch','Scooter','Phone'].map((name,i) =>
    ({id:i+1,name,has_image:true,quantity:1,winners:[]}));
  const select = (sequence, i) => f.render(f.state({sequence,phase:'ready_for_next',prize:prizes[i].name,prizes}));
  select(1,0); loads[0].resolve(); await f.settle();
  select(2,1); select(3,2);
  loads[1].resolve(); await f.settle();
  assert.equal(track.children.length, 1);
  assert.equal(f.node('projectorPrize').textContent, 'Tablet');
  loads[2].resolve(); await f.settle();
  assert.equal(track.children.length, 2);
  assert.equal(track.children[1].children[2].textContent, 'Scooter');
  assert.equal(f.run('selectedPrizeId'), 1);
  assert.equal(animations.length, 1);
  assert.equal(animations[0].timing.duration, 1200);
  assert.deepEqual(Array.from(animations[0].frames, frame => frame.transform), ['translateX(0%)','translateX(100%)']);
  select(4,3); loads[3].reject(); await f.settle();
  assert.equal(animations.length, 1);
  animations[0].onfinish();
  assert.equal(f.node('projectorPrize').textContent, 'Scooter');
  assert.equal(animations.length, 2);
  animations[1].onfinish();
  assert.equal(f.node('projectorPrizeVisual').hidden, true);
  assert.equal(f.node('projectorPrize').textContent, 'Phone');
  assert.equal(f.run('selectedPrizeId'), 4);
  assert.equal(track.children.length, 1);
  select(4,3);
  assert.equal(animations.length, 2);
});

test('a new selection waits for the last spinning digit before moving the prize', () => {
  const f = fixture({reducedMotion:false});
  let tick;
  f.context.performance = {now:() => 0};
  f.context.requestAnimationFrame = callback => { tick=callback; return 1; };
  f.context.cancelAnimationFrame = () => {};
  const prizes = ['Tablet','Watch'].map((name,i) => ({id:i+1,name,quantity:1,winners:[]}));
  f.render(f.state({sequence:1,phase:'spinning',prize:'Tablet',draw_employee_id:'0000042',prizes}));
  tick(5000);
  f.render(f.state({sequence:2,phase:'ready_for_next',prize:'Watch',prizes}));
  f.run('openPrize(2)');
  assert.equal(f.node('projectorPrize').textContent,'Tablet');
  assert.equal(f.run('projectorDigits.isSpinning()'),true);
  tick(20560);
  assert.equal(f.node('projectorPrize').textContent,'Watch');
});

test('an early next-prize selection waits for the current winner and fanfare', () => {
  let fanfares = 0;
  const f = fixture({reducedMotion:false, audio:{startDraw() {}, reelTick() {}, finishDraw() {}, reset() {},
    celebrate() { fanfares++; }}});
  let tick, finishHold;
  f.context.performance = {now:() => 0};
  f.context.requestAnimationFrame = callback => { tick = callback; return 1; };
  f.context.cancelAnimationFrame = () => {};
  f.context.setTimeout = (callback, delay) => { finishHold = callback; assert.equal(delay, 5000); return 1; };
  let celebrations = 0;
  f.run('createConfetti = () => { window.onCelebration(); };');
  f.context.window.onCelebration = () => celebrations++;
  const prizes = ['Tablet','Watch','Phone'].map((name,i) => ({id:i+1,name,quantity:1,winners:[]}));
  const winner = {employee_id:'0000042',name:'Guest',department:'Operations'};
  f.render(f.state({sequence:1,phase:'spinning',prize:'Tablet',draw_employee_id:winner.employee_id,prizes}));
  tick(7000);
  f.render(f.state({sequence:1,phase:'winner_announced',prize:'Tablet',winner,prizes}));
  f.render(f.state({sequence:2,phase:'ready_for_next',prize:'Watch',prizes}));
  tick(20560);
  assert.equal(f.node('projectorPrize').textContent,'Tablet');
  assert.equal(f.node('projectorDrawWinnerName').hidden,false);
  assert.equal(f.node('projectorWinnerMeta').textContent,'Operations');
  assert.equal(celebrations,1);
  assert.equal(fanfares,1);
  f.render(f.state({sequence:3,phase:'ready_for_next',prize:'Phone',prizes}));
  assert.equal(f.node('projectorPrize').textContent,'Tablet');
  finishHold();
  assert.equal(f.node('projectorPrize').textContent,'Phone');
  assert.equal(celebrations,1);
  assert.equal(fanfares,1);
});

test('the next snapshot can recover the saved winner when the announcement was missed', () => {
  const f = fixture({reducedMotion:false});
  let tick, finishHold;
  f.context.performance = {now:() => 0};
  f.context.requestAnimationFrame = callback => { tick = callback; return 1; };
  f.context.cancelAnimationFrame = () => {};
  f.context.setTimeout = callback => { finishHold = callback; return 1; };
  const history = {employee_id:'0000041',name:'Previous winner',department:'IT'};
  const winner = {employee_id:'0000042',name:'Guest',department:'Operations'};
  const prizes = ['Tablet','Watch'].map((name,i) => ({id:i+1,name,quantity:1,winners:[]}));
  prizes[0].winners = [history];
  f.render(f.state({sequence:1,phase:'spinning',prize:'Tablet',draw_employee_id:winner.employee_id,prizes}));
  tick(7000);
  f.render(f.state({sequence:2,phase:'ready_for_next',prize:'Watch',
    prizes:[{...prizes[0],winners:[history,winner]},prizes[1]]}));
  tick(20560);
  assert.equal(f.node('projectorPrize').textContent,'Tablet');
  assert.equal(f.node('projectorDrawWinnerName').textContent,'Guest');
  finishHold();
  assert.equal(f.node('projectorPrize').textContent,'Watch');
});

test('a draw arriving mid-slide starts only after its prize reaches the platform', () => {
  const f = fixture({reducedMotion:false});
  let animation, tick;
  f.context.performance = {now:() => 0};
  f.context.requestAnimationFrame = callback => { tick=callback; return 1; };
  f.context.cancelAnimationFrame = () => {};
  const prizes = ['Tablet','Watch'].map((name,i) => ({id:i+1,name,quantity:1,winners:[]}));
  f.node('prizeTrack').animate = () => (animation={cancel() {}});
  f.render(f.state({sequence:1,phase:'ready_for_next',prize:'Tablet',prizes}));
  f.render(f.state({sequence:2,phase:'ready_for_next',prize:'Watch',prizes}));
  f.render(f.state({sequence:3,phase:'spinning',prize:'Watch',draw_employee_id:'0000042',prizes}));
  f.render(f.state({sequence:3,phase:'winner_announced',prize:'Watch',winner:{employee_id:'0000042',name:'Guest'},prizes}));
  assert.equal(f.run('projectorDigits.isSpinning()'),false);
  animation.onfinish();
  assert.equal(f.node('projectorPrize').textContent,'Watch');
  assert.equal(f.run('projectorDigits.isSpinning()'),true);
  tick(20560);
  assert.equal(f.node('projectorDrawWinnerName').textContent,'Guest');
});

test('opening the projector with a saved winner shows the prize instead of the waiting poster', () => {
  const f = fixture();
  f.run('openingWelcome = true');
  f.render(f.state({sequence:2, phase:'winner_announced', prize:'Prize one',
    winner:{name:'First', employee_id:'0000001'}}));
  assert.equal(f.node('drawStage').hidden, false);
  assert.equal(f.node('projectorDrawWinnerName').textContent, 'First');
});

test('clicking a projector prize shows it above seven zero digits', () => {
  const f = fixture();
  f.render(f.state({sequence:2, phase:'ready_for_next'}));
  const posterPrize = f.node('posterPrizes').children[0];
  assert.equal(posterPrize.type, 'button');
  posterPrize.listeners.click();
  assert.equal(f.node('prizeBoard').hidden, true);
  assert.equal(f.node('drawStage').hidden, false);
  assert.equal(f.node('projectorPrize').textContent, 'Prize one');
  assert.equal(f.node('projectorDigits').children.map(visibleDigit).join(''), '0000000');
  f.render(f.state({sequence:3, phase:'ready_for_next'}));
  assert.equal(f.node('drawStage').hidden, false);
});

test('poster uses published reward photos and refreshes them after a change', () => {
  const f = fixture();
  const prize = {id:7, name:'Bosch 10 kg Front-Loading Washing Machine', quantity:1, has_image:true, winners:[]};
  f.render(f.state({sequence:1, prizes:[prize]}));
  assert.equal(f.node('posterPrizes').children.length, 1);
  assert.equal(f.node('posterPrizes').children[0].children[0].src, '/api/rewards/image?id=7&v=1');
  f.render(f.state({sequence:2, prizes:[prize]}));
  assert.equal(f.node('posterPrizes').children[0].children[0].src, '/api/rewards/image?id=7&v=2');
});

function drawReplies(f, replies) {
  const posts = [];
  f.context.fetch = async (path, options = {}) => {
    if (options.method !== 'POST') return {ok:true, status:200, headers:{get:() => null}, json:async () => f.state()};
    posts.push({path, options});
    const reply = replies.shift();
    if (reply instanceof Error) throw reply;
    return {ok:reply.status < 400, status:reply.status,
      headers:{get:name => ({'content-type':reply.type || 'application/json',
        ...reply.headers,
        'x-eventdesk-operation-resolved':reply.receipt || (reply.resolved ? options.headers['X-EventDesk-Operation-Id'] : null)})[name.toLowerCase()] ?? null},
      json:async () => reply.payload || {winner:{winner_id:1, employee_id:'0000001'}},
    };
  };
  return posts;
}

test('projector draw sends a secure UUID and resolves the confirmed operation', async () => {
  const f = fixture(), posts = drawReplies(f, [{status:200, resolved:true}]);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 1);
  assert.match(posts[0].options.headers['X-EventDesk-Operation-Id'], /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  assert.equal(posts[0].options.headers['X-CSRF-Token'], 'fixture-csrf');
  assert.deepEqual([...f.entries.keys()], ['eventdesk-projector-draw']);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
});

test('a lost projector draw reply blocks another prize and retries the same UUID', async () => {
  const f = fixture(), posts = drawReplies(f, [new Error('reply lost'), {status:200, resolved:true}]);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  await f.run('drawProjectorPrize({id:2})');
  assert.equal(posts.length, 1);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 2);
  assert.equal(posts[0].options.headers['X-EventDesk-Operation-Id'], posts[1].options.headers['X-EventDesk-Operation-Id']);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  assert.deepEqual([...f.entries.keys()], ['eventdesk-projector-draw']);
});

test('a mismatched projector draw receipt remains unresolved and keeps the saved identity', async () => {
  const f = fixture(), posts = drawReplies(f, [{status:200, receipt:'11111111-1111-4111-8111-111111111111'}]);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 1);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  assert.equal(f.entries.size, 1);
  assert.match(f.node('projectorMessage').textContent, /confirm|retry/i);
});

test('an unresolved draw exposes Retry draw and locks other prizes even after the winner appears', async () => {
  const f = fixture(); drawReplies(f, [new Error('reply lost')]);
  f.render(f.state({phase:'ready_for_next', prize:'Prize one'}));
  await f.run('drawProjectorPrize({id:1})');
  f.render(f.state({sequence:2, eligible:0, phase:'winner_announced', prize:'Prize one',
    winner:{name:'First', employee_id:'0000001'}}));
  assert.equal(f.node('confirmProjectorDraw').textContent, 'Retry draw');
  assert.equal(f.node('confirmProjectorDraw').disabled, false);
  assert.equal(f.node('prizeNext').disabled, true);
  assert.equal(f.node('showRewards').disabled, true);
  f.run('openPrize(2)');
  assert.equal(f.run('selectedPrizeId'), 1);
});

test('only a matching receipt resolves a business rejection after an uncertain draw', async () => {
  const f = fixture(), posts = drawReplies(f, [new Error('reply lost'),
    {status:409, payload:{error:'Wait'}}, {status:409, payload:{error:'Wait'}, resolved:true}]);
  await f.run('drawProjectorPrize({id:1})');
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(f.entries.size, 1);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(f.entries.size, 0);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  assert.equal(new Set(posts.map(item => item.options.headers['X-EventDesk-Operation-Id'])).size, 1);
});

test('a failed display refresh after a confirmed draw cannot make the draw uncertain', async () => {
  const f = fixture(), posts = drawReplies(f, [{status:200, resolved:true}]);
  const send = f.context.fetch;
  f.context.fetch = (path, options) => options?.method === 'POST' ? send(path, options) : Promise.reject(new Error('display offline'));
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 1);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  assert.deepEqual([...f.entries.keys()], ['eventdesk-projector-draw']);
  assert.match(f.node('projectorMessage').textContent, /saved/i);
});

test('a synced SQL state re-enables the original draw retry without resolving its saved UUID', async () => {
  const f = fixture();
  const display = f.state({phase:'preparing', prize:'Prize one', prizes:[{id:1, name:'Prize one', quantity:1, has_image:false, winners:[]}]});
  f.render(display); f.run("selectedPrizeId = 1; currentView = 'draw'; renderView();");
  drawReplies(f, [{status:503, payload:{error:'Draws are unavailable offline.'},
    headers:{'x-eventdesk-storage':'sqlite', 'x-eventdesk-sync':'pending'}}]);
  const send = f.context.fetch;
  let synced = false;
  f.context.fetch = (path, options) => options?.method === 'POST' ? send(path, options) : Promise.resolve({
    ok:true, status:200, headers:{get:name => ({'x-eventdesk-storage':'sql',
      ...(synced ? {'x-eventdesk-sync':'synced'} : {})})[name.toLowerCase()] ?? null}, json:async () => display});
  await f.run('drawProjectorPrize({id:1})');
  const savedId = [...f.entries.values()][0];
  assert.equal(f.node('confirmProjectorDraw').disabled, true);
  await f.run('loadState()');
  assert.equal(f.node('confirmProjectorDraw').disabled, true, 'a cached SQL read without sync proof cannot clear offline status');
  synced = true; await f.run('loadState()');
  assert.equal(f.context.window.EventDeskStorage.current().offline, false);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  assert.equal(f.node('confirmProjectorDraw').disabled, false);
  assert.equal(f.node('confirmProjectorDraw').textContent, 'Retry draw');
  assert.deepEqual([...f.entries.values()], [savedId], 'GET never completes the pending mutation');
});

test('direct SQL online state enables the projector retry while retaining a separate pending queue', async () => {
  const f = fixture();
  const display = f.state({phase:'preparing', prize:'Prize one', prizes:[{id:1, name:'Prize one', quantity:1, has_image:false, winners:[]}]});
  f.render(display); f.run("selectedPrizeId = 1; currentView = 'draw'; renderView();");
  const posts = drawReplies(f, [new Error('Draw reply lost'), {status:200, resolved:true,
    headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'online','x-eventdesk-queue':'unknown'}}]);
  await f.run('drawProjectorPrize({id:1})');
  const savedId = [...f.entries.values()][0];
  const send = f.context.fetch;
  f.context.window.EventDeskStorage.update({get:name => ({'x-eventdesk-storage':'sqlite',
    'x-eventdesk-sync':'pending','x-eventdesk-queue':'pending'})[name.toLowerCase()] ?? null}, {});
  assert.equal(f.node('confirmProjectorDraw').disabled, true);
  f.context.fetch = (path, options) => options?.method === 'POST' ? send(path, options) : Promise.resolve({
    ok:true, status:200, headers:{get:name => ({'x-eventdesk-storage':'sql',
      'x-eventdesk-sync':'online','x-eventdesk-queue':'unknown',
      'x-eventdesk-backup':'unavailable'})[name.toLowerCase()] ?? null}, json:async () => display});
  await f.run('loadState()');
  const status = f.context.window.EventDeskStorage.current();
  assert.equal(status.offline, false);
  assert.equal(status.queue, 'pending');
  assert.equal(status.uncertain, true);
  assert.equal(f.node('confirmProjectorDraw').disabled, false);
  assert.equal(f.node('confirmProjectorDraw').textContent, 'Retry draw');
  assert.deepEqual([...f.entries.values()], [savedId]);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 2);
  assert.equal(posts[0].options.headers['X-EventDesk-Operation-Id'], posts[1].options.headers['X-EventDesk-Operation-Id']);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  assert.equal(f.context.window.EventDeskStorage.current().queue, 'pending', 'one confirmed draw cannot clear a separate outbox');
});

test('the projector can retry an exact staff draw with the same prize and session', async () => {
  const f = fixture();
  const body = JSON.stringify({prize_id:'1', round_name:'Staff round'});
  const storage = f.context.window.EventDeskStorage;
  const id = await storage.prepareOperation(JSON.stringify(['/api/draw', 'POST', body, 'fixture-csrf']));
  storage.markUncertain(id);
  const posts = drawReplies(f, [{status:200, resolved:true}]);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].options.body, body);
  assert.equal(posts[0].options.headers['X-EventDesk-Operation-Id'], id);
  assert.equal(storage.current().uncertain, false);
});

test('an invalid success reply and unconfirmed server failure retain the draw UUID', async () => {
  for (const reply of [{status:200, type:'text/html'}, {status:503, payload:{error:'Offline'}}, {status:200, payload:{}}]) {
    const f = fixture(); drawReplies(f, [reply]);
    await f.run('drawProjectorPrize({id:1})');
    assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
    assert.equal(f.entries.size, 1);
  }
});

test('the projector cannot post a draw without the storage helper', async () => {
  const f = fixture({includeStorage:false}), posts = drawReplies(f, [{status:200, resolved:true}]);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 0);
  assert.match(f.node('projectorMessage').textContent, /refresh/i);
});

test('projector loads deferred storage helper before the draw script', () => {
  const html = fs.readFileSync('static/projector.html', 'utf8');
  assert.match(html, /<script defer src="\/static\/storage-status\.js"><\/script>/);
  assert.ok(html.indexOf('/static/storage-status.js') < html.indexOf('/static/projector.js'));
  assert.ok(html.indexOf('/static/digit-draw.js') < html.indexOf('/static/projector.js'));
});

test('projector draws the stage poster once, outside the SVG effects layer', () => {
  const html = fs.readFileSync('static/projector.html', 'utf8');
  const poster = '/static/assets/tokin-stage-no-title.png';
  const sources = html.match(/<(?:img|image)\b[^>]*>/gi) || [];
  const posterElements = sources.filter(tag => tag.includes(`"${poster}"`));
  assert.equal(posterElements.length, 1);
  assert.match(posterElements[0], /^<img\b/i);
  for (const [svg] of html.matchAll(/<svg\b[\s\S]*?<\/svg>/gi)) {
    assert.ok(!svg.includes(poster), 'SVG effects must not resample the poster');
  }
});

test('each prize includes the gold plaque without a second image request', () => {
  const html = fs.readFileSync('static/projector.html', 'utf8');
  const artwork = fs.readFileSync('static/assets/nameplate/award-plaque.svg', 'utf8');
  const symbol = html.match(/<symbol id="award-plaque"[^>]*>([\s\S]*?)<\/symbol>/);
  const normalize = markup => markup.trim().replace(/>\s+</g, '><').replace(/\s+/g, ' ');
  assert.ok(symbol);
  assert.equal(normalize(symbol[1]), normalize(artwork.match(/<svg[^>]*>([\s\S]*)<\/svg>/)[1]));
  assert.match(html, /<template id="prizePlaqueLayer">\s*<svg[^>]*>\s*<use href="#award-plaque"/);

  const f = fixture();
  const block = f.run("makePrizeGroup({label:'Grand Prize'}).group");
  assert.ok(block.children.some(child => child.id === 'svg'));
});

test('winner label rotation preserves the reward card and image elements', () => {
  const f = fixture(); f.render(f.state());
  const card = f.node('prizeBoardRows').children[0], visual = card.children[1], image = visual.children[0];
  assert.match(card.children[3].textContent, /First/);
  f.run('boardWinnerIndex += 1; renderPrizeBoard();');
  assert.equal(f.node('prizeBoardRows').children[0], card);
  assert.equal(card.children[1].children[0], image);
  assert.match(card.children[3].textContent, /Second/);
});

test('same-sequence eligibility and prize changes refresh the display', async () => {
  const f = fixture(); f.render(f.state());
  f.render(f.state({eligible:11, prizes:[{id:1, name:'Updated prize', quantity:3, has_image:false, winners:[]}]}));
  await f.settle();
  assert.equal(f.node('eligibleCount').textContent, '11 eligible employees');
  assert.equal(f.node('prizeBoardRows').children[0].title, 'Updated prize');
  assert.equal(f.node('prizeBoardRows').children[0].children.at(-1).style.display, 'none');
  assert.equal(f.requests.filter(path => path === '/api/draw/eligible-ids').length, 0);
});

test('projector shows the saved seven-digit ID without polling the eligible ID list', () => {
  const f = fixture(), winner = {name:'Winner', employee_id:'0000042', department:'IT'};
  assert.equal(f.node('projectorDigits').children.map(visibleDigit).join(''), '0000000');
  f.render(f.state({sequence:2, phase:'spinning', prize:'Prize one',
    draw_employee_id:'0000042', show_employee_id:true}));
  assert.equal(f.node('projectorDigits').children.length, 7);
  assert.equal(f.node('projectorDigits').children.map(visibleDigit).join(''), '0000042');
  f.render(f.state({sequence:2, phase:'winner_selected', prize:'Prize one', winner, show_employee_id:true}));
  assert.equal(f.node('projectorDigits').children.map(visibleDigit).join(''), '0000042');
  f.render(f.state({sequence:2, phase:'winner_announced', prize:'Prize one', winner, show_employee_id:true}));
  assert.equal(f.node('drawStage').hidden, false);
  assert.equal(f.node('winnerStage').hidden, true);
  assert.equal(f.node('projectorDigits').children.map(visibleDigit).join(''), '0000042');
  assert.equal(f.node('projectorDrawWinnerName').textContent, 'Winner');
  assert.equal(f.node('projectorDrawWinnerName').hidden, false);
  assert.equal(f.requests.filter(path => path === '/api/draw/eligible-ids').length, 0);
});

test('legacy nonnumeric employee IDs remain readable at the winner reveal', () => {
  const f = fixture();
  f.render(f.state({sequence:2, phase:'spinning', prize:'Prize one', draw_employee_id:'P100'}));
  f.render(f.state({sequence:2, phase:'winner_announced', prize:'Prize one',
    winner:{name:'Winner', employee_id:'P100'}}));
  assert.equal(f.node('projectorDrawIdFallback').textContent, 'P100');
  assert.equal(f.node('projectorDrawIdFallback').hidden, false);
  assert.equal(f.node('projectorDrawWinnerName').textContent, 'Winner');
});

test('an older draw phase cannot replace the saved winner display', () => {
  const f = fixture();
  f.render(f.state({sequence:2, phase:'winner_announced'}));
  f.render(f.state({sequence:2, phase:'spinning'}));
  f.render(f.state({sequence:1, phase:'winner_announced'}));
  assert.equal(f.run('latestState.sequence'), 2);
  assert.equal(f.run('latestState.phase'), 'winner_announced');
});
