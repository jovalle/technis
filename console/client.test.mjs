import { test } from 'node:test';
import assert from 'node:assert/strict';

for (const pathname of ['/', '/console/ms-01/']) {
test(`reconnects recover reboots and network loss; explicit stops cancel retries (${pathname})`, async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const elements = new Map();
  for (const id of ['screen', 'viewport', 'status', 'notice', 'disconnect', 'reconnect', 'focus', 'fullscreen', 'host', 'resolution']) {
    elements.set(id, Object.assign(new EventTarget(), { style: {}, width: 640, height: 400 }));
  }
  const document = Object.assign(new EventTarget(), { getElementById: id => elements.get(id), querySelectorAll: () => [] });
  const renderers = [], sent = [], outbound = [];
  let relayAvailable = true;
  const window = Object.assign(new EventTarget(), { CreateAmtRemoteDesktop: () => {
    const renderer = { Start() {}, sendkey: (...args) => sent.push(args) };
    renderers.push(renderer);
    return renderer;
  } });
  elements.get('reconnect').focus = () => {};
  const sockets = [];
  class FakeSocket extends EventTarget {
    static OPEN = 1;
    static CLOSED = 3;
    readyState = 1;
    constructor(url) {
      super();
      assert.equal(url, `ws://127.0.0.1:1234${pathname}kvm`);
      assert.ok(sockets.every(socket => socket.readyState === 3), 'replacement opened before prior close');
      sockets.push(this);
      setImmediate(() => this.onopen?.());
    }
    close() {
      this.readyState = 2;
      setImmediate(() => this.finish());
    }
    finish(code = 1000) { this.readyState = 3; const closed = this.onclose?.({ code }); this.dispatchEvent(new Event('close')); return closed; }
    send(data) { outbound.push([this, data]); }
  }
  Object.assign(globalThis, {
    window, document, WebSocket: FakeSocket,
    location: { origin: 'http://127.0.0.1:1234', pathname },
    ResizeObserver: class { observe() {} },
    cancelAnimationFrame() {},
    fetch: async url => {
      assert.equal(url, `${pathname}session`);
      if (!relayAvailable) throw new TypeError('Failed to fetch');
      return { ok: true, json: async () => ({ host: 'ms-01' }) };
    },
  });
  // Browser global properties also become bare identifiers; emulate that binding here.
  globalThis.Q = id => elements.get(id);
  await import(`./client.mjs?path=${encodeURIComponent(pathname)}`);
  t.after(() => elements.get('disconnect').onclick());
  const flush = () => new Promise(resolve => setImmediate(resolve));
  async function tick(milliseconds) { t.mock.timers.tick(milliseconds); await flush(); }
  assert.equal(sockets.length, 1);
  await elements.get('reconnect').onclick();
  assert.equal(sockets.length, 2);
  assert.equal(sockets[0].readyState, 3);
  assert.equal(elements.get('disconnect').disabled, false);
  sockets[1].readyState = 0;
  elements.get('disconnect').onclick();
  assert.equal(elements.get('reconnect').disabled, false);
  await elements.get('reconnect').onclick();
  assert.equal(sockets.length, 3);
  assert.equal(sockets[1].readyState, 3);
  renderers.at(-1).parent.xxStateChange(3);
  function key(type, code, value, shiftKey = false, target = elements.get('screen')) {
    const event = Object.assign(new Event(type, { cancelable: true }), { code, key: value, shiftKey });
    target.dispatchEvent(event);
    return event;
  }
  assert.equal(key('keydown', 'ControlLeft', 'Control').defaultPrevented, true);
  assert.equal(key('keydown', 'KeyC', 'c').defaultPrevented, true);
  key('keyup', 'KeyC', 'c'); key('keyup', 'ControlLeft', 'Control');
  key('keydown', 'Backspace', 'Backspace'); key('keyup', 'Backspace', 'Backspace');
  key('keydown', 'ShiftLeft', 'Shift', true);
  key('keydown', 'Escape', 'Escape', true);
  key('keydown', 'KeyA', 'a', false, document);
  assert.deepEqual(sent, [[0xffe3, 1], [99, 1], [99, 0], [0xffe3, 0], [0xff08, 1], [0xff08, 0], [0xffe1, 1], [0xffe1, 0]]);

  const screen = elements.get('screen');
  Object.assign(elements.get('viewport'), { clientWidth: 1040, clientHeight: 784 });
  window.dispatchEvent(new Event('resize'));
  assert.equal(screen.style.width, '1024px', 'a selected embedded viewer refits after being hidden');
  assert.equal(screen.style.height, '640px');
  const beforeBoot = renderers.at(-1);
  function size(renderer, width, height) {
    screen.width = width; screen.height = height; renderer.onScreenSizeChange();
    renderer.onFrameReceived?.();
  }
  size(beforeBoot, 1024, 768);
  size(beforeBoot, 1280, 720);
  await tick(3000);
  assert.equal(sockets.length, 3, 'ordinary resolution changes do not restart KVM');
  key('keydown', 'ControlLeft', 'Control');
  size(beforeBoot, 800, 600);
  size(beforeBoot, 800, 600);
  await tick(1999);
  assert.equal(sockets.length, 3);
  await tick(1);
  assert.deepEqual(sent.at(-1), [0xffe3, 0], 'reboot recovery releases held keys');
  assert.equal(elements.get('status').textContent, 'Reconnecting…');
  await tick(1000);
  assert.equal(sockets.length, 4, 'firmware mode gets a fresh KVM handshake');
  renderers.at(-1).parent.xxStateChange(3);
  size(renderers.at(-1), 800, 600);
  await tick(30000);
  assert.equal(sockets.length, 4, 'initial firmware resolution and static screens do not loop');
  beforeBoot.parent.send('stale framebuffer request');
  beforeBoot.parent.Stop();
  assert.equal(outbound.length, 0, 'old renderer timers cannot write into a replacement session');
  renderers.at(-1).parent.send('current framebuffer request');
  assert.equal(outbound.length, 1);

  await sockets.at(-1).finish();
  await tick(1000);
  assert.equal(sockets.length, 5, 'a connected session resets retry backoff');
  sockets.at(-1).onmessage({ data: JSON.stringify({ error: 'Console is busy', retryable: true }) });
  await sockets.at(-1).finish();
  await tick(1999);
  assert.equal(sockets.length, 5);
  await tick(1);
  assert.equal(sockets.length, 6, 'transient firmware busy is retried with backoff');
  for (const delay of [4000, 8000, 10000, 10000]) {
    const count = sockets.length;
    await sockets.at(-1).finish();
    await tick(delay - 1);
    assert.equal(sockets.length, count);
    await tick(1);
    assert.equal(sockets.length, count + 1, 'retry delay is capped at ten seconds');
  }
  renderers.at(-1).parent.xxStateChange(3);
  sockets.at(-1).onmessage({ data: JSON.stringify({ error: 'Certificate pin mismatch', retryable: false }) });
  await sockets.at(-1).finish();
  const count = sockets.length;
  await tick(60000);
  assert.equal(sockets.length, count, 'certificate/authentication failures do not retry');
  assert.equal(elements.get('notice').textContent, 'Certificate pin mismatch');

  await elements.get('reconnect').onclick();
  await tick(20000);
  assert.match(elements.get('notice').textContent, /did not finish connecting/);
  await tick(1000);
  assert.equal(sockets.length, count + 2, 'a stalled handshake is replaced');
  await sockets.at(-1).finish();
  elements.get('disconnect').onclick();
  await tick(60000);
  assert.equal(sockets.length, count + 2, 'Disconnect cancels pending recovery');
  await elements.get('reconnect').onclick();
  await sockets.at(-1).finish();
  window.dispatchEvent(new Event('pagehide'));
  await tick(60000);
  assert.equal(sockets.length, count + 3, 'leaving the page cancels recovery');
  await elements.get('reconnect').onclick();
  renderers.at(-1).parent.xxStateChange(3);
  renderers.at(-1).parent.Stop();
  await tick(60000);
  assert.equal(sockets.length, count + 4, 'unsupported video data stops rather than looping');
  assert.match(elements.get('notice').textContent, /unsupported video data/);
  await elements.get('reconnect').onclick();
  const beforeRelease = sockets.length;
  await sockets.at(-1).finish(4001);
  await tick(60000);
  assert.equal(sockets.length, beforeRelease, 'a released session never retries, even if its final message was lost');
  assert.match(elements.get('notice').textContent, /Session released/);

  await elements.get('reconnect').onclick();
  await sockets.at(-1).finish(4002);
  const beforePowerPause = sockets.length;
  await tick(60000);
  assert.equal(sockets.length, beforePowerPause, 'power pauses do not automatically reclaim KVM');
  assert.match(elements.get('notice').textContent, /paused for a power action/);

  await elements.get('reconnect').onclick();
  renderers.at(-1).parent.xxStateChange(3);
  key('keydown', 'ControlLeft', 'Control');
  await sockets.at(-1).finish(1001);
  const beforeShutdown = sockets.length;
  await tick(60000);
  assert.equal(sockets.length, beforeShutdown, 'graceful local shutdown never retries');
  assert.equal(elements.get('status').textContent, 'Disconnected');
  assert.match(elements.get('notice').textContent, /Local console stopped/);
  assert.deepEqual(sent.at(-1), [0xffe3, 0], 'shutdown releases held keys');
  assert.equal(elements.get('disconnect').disabled, true);
  assert.equal(elements.get('reconnect').disabled, false);

  await elements.get('reconnect').onclick();
  relayAvailable = false;
  await sockets.at(-1).finish(1006);
  const beforeLoss = sockets.length;
  await tick(60000);
  assert.equal(sockets.length, beforeLoss, 'abrupt local relay loss never retries');
  assert.equal(elements.get('status').textContent, 'Disconnected');
  assert.match(elements.get('notice').textContent, /Local relay disconnected/);

  await elements.get('reconnect').onclick();
  sockets.at(-1).onerror();
  await sockets.at(-1).finish(1006);
  const beforeRefused = sockets.length;
  await tick(60000);
  assert.equal(sockets.length, beforeRefused, 'manual reconnect to a stopped relay does not loop');

  relayAvailable = true;
  await elements.get('reconnect').onclick();
  renderers.at(-1).parent.xxStateChange(3);
  size(renderers.at(-1), 1024, 768);
  await sockets.at(-1).finish(1006);
  await tick(1000);
  const beforeFirmware = sockets.length;
  renderers.at(-1).parent.xxStateChange(3);
  size(renderers.at(-1), 800, 600);
  await tick(2000);
  await tick(1000);
  assert.equal(sockets.length, beforeFirmware + 1, 'a transport reconnect retains the prior OS mode and releases the firmware KVM wait');
  renderers.at(-1).parent.xxStateChange(3);
  size(renderers.at(-1), 800, 600);
  await tick(30000);
  assert.equal(sockets.length, beforeFirmware + 1, 'the firmware reconnect runs once rather than looping');

  const beforeSameModeReset = sockets.length;
  size(renderers.at(-1), 800, 600);
  size(renderers.at(-1), 800, 600);
  await tick(30000);
  assert.equal(sockets.length, beforeSameModeReset, 'repeated initial DesktopSize notices at the same firmware resolution do not start a reconnect loop');

  await elements.get('reconnect').onclick();
  renderers.at(-1).parent.xxStateChange(3);
  const beforeStaleInit = sockets.length;
  screen.width = 1024; screen.height = 768;
  renderers.at(-1).onScreenSizeChange();
  size(renderers.at(-1), 800, 600);
  await tick(2000);
  await tick(1000);
  assert.equal(sockets.length, beforeStaleInit, 'stale ServerInit dimensions followed by the unchanged firmware display do not cause repeated handshakes');
});
}
