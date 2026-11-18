'use strict';

// Activity stays in memory. Background connections stop even if a browser delays timers.
(function (global) {
  function create() {
    const idleMs = 120000;
    const listeners = new Set();
    let lastActivity = Date.now(), focused = document.hasFocus?.() !== false;
    let active = !document.hidden && focused, deadline = null;

    function update() {
      const next = !document.hidden && focused && Date.now() - lastActivity < idleMs;
      if (next !== active) {
        active = next;
        for (const listener of listeners) listener(active);
      }
      return active;
    }
    function schedule() {
      clearTimeout(deadline);
      deadline = null;
      if (active) deadline = setTimeout(update, Math.max(0, idleMs - (Date.now() - lastActivity)));
    }
    function touch() {
      lastActivity = Date.now();
      update(); schedule();
    }
    for (const name of ['keydown', 'pointerdown', 'pointermove', 'touchstart', 'input', 'click', 'submit', 'wheel']) {
      document.addEventListener(name, touch, {capture:true, passive:true});
    }
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) touch(); else { update(); schedule(); }
    });
    global.addEventListener('focus', () => { focused = true; touch(); });
    global.addEventListener('blur', () => { focused = false; update(); schedule(); });
    global.addEventListener('pagehide', () => { focused = false; update(); schedule(); });
    global.addEventListener('pageshow', () => { focused = document.hasFocus?.() !== false; touch(); });
    schedule();
    return Object.freeze({isActive:update, touch,
      subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }});
  }
  global.EventDeskActivity = Object.freeze({create});
})(window);
