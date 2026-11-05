import { test } from 'node:test';
import assert from 'node:assert/strict';

for (const pathname of ['/', '/console/nexus/']) test(`power controls work without video, confirm the host and never retry commands (${pathname})`, async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const elements = new Map(['power-controls', 'power-state', 'power-feedback', 'power-refresh'].map(id => [id, Object.assign(new EventTarget(), { textContent: '' })]));
  const buttons = ['on', 'shutdown', 'restart', 'off', 'cycle'].map(action => Object.assign(new EventTarget(), { dataset: { power: action }, textContent: action === 'restart' ? 'Reset (force)' : action, disabled: true }));
  elements.get('power-controls').querySelectorAll = () => buttons;
  const requests = [], confirmations = [];
  let confirmed = false, fail = false, expired = false;
  Object.assign(globalThis, {
    document: Object.assign(new EventTarget(), { getElementById: id => elements.get(id), hidden: false }),
    location: { pathname },
    window: Object.assign(new EventTarget(), { confirm: message => { confirmations.push(message); return confirmed; } }),
    fetch: async (url, options) => {
      requests.push([url, options?.method ?? 'GET']);
      if (url.endsWith('session')) return { ok: true, json: async () => ({ host: 'nexus' }) };
      return { ok: !fail && !expired, status: expired ? 410 : fail ? 502 : 200, json: async () => ({ state: 'on', actions: ['on', 'shutdown', 'restart', 'off'] }) };
    },
  });
  await import(`./power-client.mjs?path=${encodeURIComponent(pathname)}`);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const reset = buttons[2];
  assert.equal(elements.get('power-state').textContent, 'on');
  assert.equal(reset.disabled, false, 'power is independent of KVM input ownership');
  assert.equal(buttons[4].hidden, true, 'unsupported firmware actions are hidden');
  reset.dispatchEvent(new Event('click')); await flush();
  assert.equal(requests.filter(([, method]) => method === 'POST').length, 0);
  assert.match(confirmations.at(-1), /Reset \(force\) nexus\?/);
  assert.match(confirmations.at(-1), /lose unsaved data/);
  confirmed = true;
  reset.dispatchEvent(new Event('click')); reset.dispatchEvent(new Event('click')); await flush();
  assert.deepEqual(requests.filter(([, method]) => method === 'POST'), [[`${pathname}power/restart`, 'POST']]);
  assert.ok(buttons.every(button => button.disabled));
  assert.match(elements.get('power-feedback').textContent, /accepted/);
  t.mock.timers.tick(10000); await flush();
  assert.equal(reset.disabled, false);
  fail = true;
  reset.dispatchEvent(new Event('click')); await flush();
  assert.match(elements.get('power-feedback').textContent, /may have reached/);
  const count = requests.filter(([, method]) => method === 'POST').length;
  t.mock.timers.tick(30000); await flush();
  assert.equal(requests.filter(([, method]) => method === 'POST').length, count, 'failed commands are never retried');
  assert.equal(elements.get('power-state').textContent, 'unknown');
  assert.ok(buttons.every(button => button.disabled));
  fail = false;
  elements.get('power-refresh').dispatchEvent(new Event('click')); await flush();
  assert.equal(reset.disabled, false);
  expired = true;
  elements.get('power-refresh').dispatchEvent(new Event('click')); await flush();
  assert.ok(buttons.every(button => button.disabled));
  reset.dispatchEvent(new Event('click')); await flush();
  assert.equal(requests.filter(([, method]) => method === 'POST').length, count);
  window.dispatchEvent(new Event('pagehide'));
});
