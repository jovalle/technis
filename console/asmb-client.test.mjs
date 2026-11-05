import { test } from 'node:test';
import assert from 'node:assert/strict';

test('ASMB10 controls release held input, fit video, and stop retrying after session release', async t => {
  t.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  class Element extends EventTarget {
    style = {}; focus() { this.focused = true; } setAttribute() {}
  }
  class Key extends Event { constructor(type, values) { const { bubbles, cancelable, ...fields } = values; super(type, { bubbles, cancelable: true }); Object.assign(this, fields); } }
  const elements = new Map(['viewer', 'status', 'notice', 'disconnect', 'reconnect', 'focus', 'fullscreen', 'host', 'resolution', 'take-control'].map(id => [id, new Element()]));
  const viewer = elements.get('viewer'), sent = [];
  const input = new Element();
  input.onkeyup = event => sent.push(['up', event.keyCode]);
  input.onkeydown = event => sent.push(['down', event.keyCode]);
  const canvas = { width: 1024, height: 768, style: {} }, cursor = { ...canvas, style: {}, onmouseup() {} };
  const video = { running: true, sock: { validated: true }, textbox: input, canvas, cursor_canvas: cursor };
  viewer.contentWindow = Object.assign(new EventTarget(), { _video: video, master: true });
  viewer.contentDocument = new EventTarget(); viewer.clientWidth = 528; viewer.clientHeight = 400;
  const scroll = { style: { setProperty(name, value) { this[name] = value; } }, scrollLeft: 23, scrollTop: 37 };
  viewer.contentDocument.getElementById = id => id === 'scroll_div' ? scroll : undefined;
  const button = Object.assign(new Element(), { dataset: { key: 'ctrlc' } });
  const window = new EventTarget(); window.self = window.top = window;
  const document = Object.assign(new EventTarget(), { getElementById: id => elements.get(id), querySelectorAll: () => [button] });
  let requests = 0;
  Object.assign(globalThis, { window, document, KeyboardEvent: Key, location: { origin: 'http://127.0.0.1:1234', pathname: '/console/nexus/' }, fetch: async path => {
    requests++;
    assert.ok(path === '/console/nexus/session' || path.startsWith('/asmb/nexus/viewer.html?generation='));
    return { ok: true, json: async () => ({ host: 'nexus' }) };
  } });
  await import('./asmb-client.mjs');
  t.after(() => elements.get('disconnect').onclick());
  viewer.dispatchEvent(new Event('load')); t.mock.timers.tick(500);
  assert.equal(elements.get('status').textContent, 'Connected');
  assert.equal(canvas.style.width, '512px'); assert.equal(cursor.style.height, '384px');
  assert.equal(scroll.style['--console-width'], '512px'); assert.equal(scroll.style['--console-height'], '384px');
  assert.equal(scroll.scrollLeft, 0); assert.equal(scroll.scrollTop, 0);
  viewer.contentWindow.master = false; t.mock.timers.tick(500);
  assert.equal(elements.get('status').textContent, 'View only'); assert.equal(button.disabled, true); assert.equal(elements.get('take-control').hidden, false);
  viewer.contentWindow.master = true; t.mock.timers.tick(500);
  button.dispatchEvent(new Event('click'));
  assert.deepEqual(sent.slice(-4), [['down', 17], ['down', 67], ['up', 67], ['up', 17]]);
  viewer.contentDocument.dispatchEvent(new Key('keydown', { keyCode: 65, code: 'KeyA' }));
  document.hidden = true; document.dispatchEvent(new Event('visibilitychange'));
  assert.ok(sent.some(([type, code]) => type === 'up' && code === 65), 'tab changes release non-modifier keys too');
  const escape = new Key('keydown', { keyCode: 27, code: 'Escape', shiftKey: true });
  viewer.contentDocument.dispatchEvent(escape);
  assert.equal(escape.defaultPrevented, true); assert.equal(elements.get('reconnect').focused, true);
  const stale = Object.assign(new Event('message'), { origin: location.origin, source: viewer.contentWindow, data: { type: 'asmb-close', code: 4001, generation: '0' } });
  window.dispatchEvent(stale); assert.equal(elements.get('status').textContent, 'Connected', 'old iframe close events cannot stop a replacement viewer');
  const released = Object.assign(new Event('message'), { origin: location.origin, source: viewer.contentWindow, data: { type: 'asmb-close', code: 4001, generation: new URL(viewer.src, location.origin).searchParams.get('generation') } });
  window.dispatchEvent(released);
  assert.match(elements.get('status').textContent, /released/);
  const before = requests; t.mock.timers.tick(30000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests, before); assert.equal(viewer.src, 'about:blank');
  assert.equal(viewer.hidden, true); assert.equal(elements.get('disconnect').disabled, true);
});
