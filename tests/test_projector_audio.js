const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
  const sources = [], gains = [], statuses = [], saved = new Map(), requests = [];
  let allowPlayback = false;
  const parameter = () => ({value:0, setTargetAtTime(value, _now, duration) {
    this.value = value; this.duration = duration;
  }});
  class AudioContext {
    state = 'suspended';
    addEventListener() {}
    async resume() { if (allowPlayback) this.state = 'running'; }
    decodeAudioData(bytes) { return bytes; }
    createGain() {
      const node = {gain:parameter(), connect(destination) { this.output = destination; return destination; }};
      gains.push(node);
      return node;
    }
    createBufferSource() {
      const node = {playbackRate:parameter(), connect(destination) { this.output = destination; },
        start() { sources.push(this); }, stop() { this.stopped = true; }, disconnect() {}};
      return node;
    }
  }
  const window = {AudioContext, localStorage:{getItem:key => saved.get(key) ?? null,
    setItem:(key, value) => saved.set(key, value)}};
  vm.runInNewContext(fs.readFileSync('static/projector-audio.js', 'utf8'), {
    window, fetch:async url => {
      requests.push(url);
      return {ok:true, arrayBuffer:async () => url.split('/').pop().replace('.wav', '')};
    }
  });
  const preferences = window.EventDeskProjectorAudio;
  const audio = preferences.create(status => statuses.push(status));
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return {audio, preferences, sources, gains, statuses, requests, settle,
    async unlock() { allowPlayback = true; audio.unlock(); await settle(); }};
}

test('projector preloads locally, waits for autoplay permission and mixes each draw once', async () => {
  const f = fixture(), {audio, gains, sources} = f;
  assert.equal(f.requests.length, 0);
  audio.open();
  await f.settle();
  assert.equal(f.requests.length, 4);
  assert.ok(f.requests.every(path => path.startsWith('/static/assets/audio/')));
  assert.equal(f.statuses.at(-1), 'blocked');
  assert.equal(sources.length, 0);
  await f.unlock();
  assert.equal(f.statuses.at(-1), 'enabled');
  assert.deepEqual(sources.map(source => source.buffer), ['festival-loop']);
  f.preferences.saveVolumes({music:.25, effects:.6});
  audio.reloadVolumes();
  assert.equal(gains[0].gain.value, .25);
  assert.equal(gains[1].gain.value, .6);

  audio.startDraw();
  assert.equal(gains[2].gain.value, .18);
  const roll = sources.at(-1);
  assert.equal(roll.buffer, 'drum-roll');
  assert.equal(roll.loop, true);
  audio.reelTick({index:0, phase:'spin'});
  for (let index = 0; index < 7; index++) audio.reelTick({index, phase:'stop'});
  assert.equal(sources.filter(source => source.buffer === 'digit-lock').length, 7);
  assert.equal(roll.playbackRate.value, 1.29);
  audio.finishDraw();
  assert.equal(roll.stopped, true);
  audio.celebrate();
  audio.celebrate();
  assert.equal(sources.filter(source => source.buffer === 'victory').length, 1);
  assert.equal(gains[2].gain.value, .1);
  sources.at(-1).onended();
  assert.equal(gains[2].gain.value, 1);
  assert.equal(sources.filter(source => source.buffer === 'festival-loop').length, 1);
});

test('reset and page close stop their active sounds and reopening uses the decoded clips', async () => {
  const f = fixture(), {audio, sources} = f;
  audio.open();
  await f.settle();
  await f.unlock();
  const music = sources[0];
  audio.startDraw();
  const roll = sources.at(-1);
  audio.reset();
  assert.equal(roll.stopped, true);
  assert.equal(music.stopped, undefined);
  audio.celebrate();
  const fanfare = sources.at(-1);
  audio.close();
  assert.equal(fanfare.stopped, true);
  assert.equal(music.stopped, true);
  audio.open();
  assert.equal(f.requests.length, 4);
  assert.equal(sources.at(-1).buffer, 'festival-loop');
});
