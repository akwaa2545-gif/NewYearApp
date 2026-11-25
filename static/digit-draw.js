'use strict';

// The draw service supplies the ID. These settings control only its presentation.
window.EventDeskDigitDraw = (() => {
  const WIDTH = 7;
  const ROWS_PER_TURN = 10;
  const config = Object.freeze({
    firstStopMs: 5000,
    firstRotations: 2,
    accelerationMs: 500,
    firstDecelerationMs: 3000,
    cruiseRowMs: 80,
    decelerationMs: 1400,
    finalDecelerationMs: 1900
  });

  function normalize(value) {
    const id = String(value ?? '').trim();
    return /^\d{1,7}$/.test(id) ? id.padStart(WIDTH, '0') : null;
  }

  // Distance is the integral of a half-cosine velocity ramp at either end
  // of a constant-speed cruise. Velocity and acceleration meet at the joins.
  function distance(t, accel, decel) {
    const area = 1 - (accel + decel) / 2;
    const ramp = (u, span) => (u - span * Math.sin(Math.PI * u / span) / Math.PI) / 2;
    if (t < accel) return ramp(t, accel) / area;
    if (t > 1 - decel) return 1 - ramp(1 - t, decel) / area;
    return (t - accel / 2) / area;
  }

  function planWheels(id) {
    let previousStop = 0;
    return Array.from(id, (digit, index) => {
      const value = Number(digit);
      const decelerationMs = index === 0 ? config.firstDecelerationMs
        : index === WIDTH - 1 ? config.finalDecelerationMs : config.decelerationMs;
      let duration = config.firstStopMs;
      let target = ROWS_PER_TURN * config.firstRotations + value;
      if (index > 0) {
        // Cruise for at least one turn after the previous lock. Choose the first
        // matching row that leaves room for a full brake at the same entry speed.
        const earliestBrake = previousStop + ROWS_PER_TURN * config.cruiseRowMs;
        const earliestTarget = (earliestBrake + (decelerationMs - config.accelerationMs) / 2)
          / config.cruiseRowMs;
        const turns = Math.ceil((earliestTarget - value) / ROWS_PER_TURN);
        target = turns * ROWS_PER_TURN + value;
        duration = target * config.cruiseRowMs + (config.accelerationMs + decelerationMs) / 2;
      }
      previousStop = duration;
      return {duration, target, lastTick:0, acceleration:config.accelerationMs / duration,
        deceleration:decelerationMs / duration};
    });
  }

  function create(host, options = {}) {
    if (!host) return null;
    const reels = Array.from({length:WIDTH}, (_, index) => {
      const cell = document.createElement('span');
      cell.className = 'id-digit';
      cell.style.setProperty('--digit-index', index);
      const viewport = document.createElement('span');
      viewport.className = 'digit-window';
      viewport.setAttribute('aria-hidden', 'true');
      const strip = document.createElement('span');
      strip.className = 'digit-strip';
      // Repeat the edge digits so both sides of a wrap display identical rows.
      for (let i = -1; i <= ROWS_PER_TURN + 1; i++) {
        const row = document.createElement('span');
        row.textContent = String((i + ROWS_PER_TURN) % ROWS_PER_TURN);
        strip.append(row);
      }
      viewport.append(strip);
      cell.append(viewport);
      return {cell, strip};
    });
    host.replaceChildren(...reels.map(reel => reel.cell));
    let frame = null, activeId = null;
    function position(reel, rows) {
      reel.strip.style.transform = 'translateY(' + (-100 * (rows % ROWS_PER_TURN + 1)) + '%)';
    }
    function settle(id) {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      activeId = id;
      host.classList.remove('is-rolling', 'is-pending');
      host.setAttribute('aria-label', id ? 'Employee ID ' + id : 'Seven-digit employee ID');
      reels.forEach((reel, index) => {
        position(reel, id ? Number(id[index]) : 0);
        reel.cell.classList.remove('is-active');
        reel.cell.classList.toggle('is-locked', Boolean(id));
      });
    }
    function reset() { settle(null); }
    function show(value) {
      const id = normalize(value);
      settle(id);
      return Boolean(id);
    }
    function pending() {
      if (!activeId) host.classList.add('is-pending');
    }
    function play(value, onComplete = () => {}) {
      const id = normalize(value);
      if (!id) return false;
      if (activeId === id) return true;
      if (frame !== null) return false;
      reset();
      activeId = id;
      if (options.reducedMotion) {
        show(id);
        onComplete();
        return true;
      }

      host.classList.add('is-rolling');
      const startedAt = performance.now();
      const wheels = planWheels(id);
      reels.forEach(reel => reel.cell.classList.add('is-active'));
      let locked = 0;
      function update(now) {
        const elapsed = Math.max(0, now - startedAt);
        const lead = locked;
        for (let index = lead; index < WIDTH; index++) {
          const reel = reels[index], wheel = wheels[index];
          if (elapsed >= wheel.duration) {
            position(reel, wheel.target);
            reel.cell.classList.remove('is-active');
            reel.cell.classList.add('is-locked');
            locked++;
            if (elapsed - wheel.duration < 100) options.onTick?.({index, phase:'stop', progress:1});
            continue;
          }
          const progress = elapsed / wheel.duration;
          const rows = wheel.target * distance(progress, wheel.acceleration, wheel.deceleration);
          position(reel, rows);
          const row = Math.floor(rows);
          // Keep one audible tick stream while all of the remaining wheels move.
          if (index === lead && row > wheel.lastTick) {
            options.onTick?.({index, phase:row === wheel.target - 1 ? 'almost' : 'spin', progress});
          }
          wheel.lastTick = row;
        }
        if (locked < WIDTH) {
          frame = requestAnimationFrame(update);
        } else {
          frame = null;
          host.classList.remove('is-rolling');
          host.setAttribute('aria-label', 'Employee ID ' + id);
          onComplete();
        }
      }
      frame = requestAnimationFrame(update);
      return true;
    }
    reset();
    return {reset, pending, show, play, isSpinning:() => frame !== null};
  }
  return {create, normalize, config};
})();
