'use strict';

(() => {
  const states = new WeakMap();

  function reset(root) {
    if (!root) return;
    const previous = states.get(root);
    states.delete(root);
    if (previous) {
      previous.image.onload = null;
      previous.image.onerror = null;
      previous.image.removeAttribute('src');
    }
    root.querySelector('.portrait-image').replaceChildren();
    root.querySelector('.portrait-fallback').hidden = false;
    root.querySelector('.portrait-label').textContent = 'No photo';
    root.querySelector('.portrait-initials').textContent = '';
    root.removeAttribute('aria-busy');
    root.dataset.state = 'empty';
    root.hidden = true;
  }

  function show(root, person) {
    if (!root) return;
    reset(root);
    if (!person) return;
    const name = typeof person.name === 'string' && person.name.trim() ? person.name.trim() : 'Employee';
    root.hidden = false;
    root.querySelector('.portrait-initials').textContent = name.split(/\s+/).slice(0, 2)
      .map(part => Array.from(part)[0]).join('').toUpperCase();
    if (typeof person.employee_id !== 'string' || !/^\d{7}$/.test(person.employee_id)) return;

    const image = root.ownerDocument.createElement('img');
    const state = {image};
    states.set(root, state);
    image.alt = `Photo of ${name}`;
    image.decoding = 'async';
    image.referrerPolicy = 'same-origin';
    image.onload = () => {
      if (states.get(root) !== state) return;
      root.querySelector('.portrait-image').replaceChildren(image);
      root.querySelector('.portrait-fallback').hidden = true;
      root.removeAttribute('aria-busy');
      root.dataset.state = 'ready';
    };
    image.onerror = () => {
      if (states.get(root) !== state) return;
      image.onload = null;
      image.onerror = null;
      image.removeAttribute('src');
      root.querySelector('.portrait-label').textContent = 'No photo';
      root.removeAttribute('aria-busy');
      root.dataset.state = 'missing';
    };
    root.querySelector('.portrait-label').textContent = 'Loading photo';
    root.setAttribute('aria-busy', 'true');
    root.dataset.state = 'loading';
    image.src = `/api/employee-photo/${person.employee_id}`;
  }

  const api = {show, reset};
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof window === 'object') window.EventDeskPortrait = api;
})();
