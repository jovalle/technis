import { test } from 'node:test';
import assert from 'node:assert/strict';

for (const fallback of [false, true]) test(`GLKVM fits video, releases HID and only confirms available Fingerbot presses (${fallback ? 'JPEG fallback' : 'H264'})`, async t => {
  t.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
  class Element extends EventTarget {
    style = {}; hidden = false; disabled = false; textContent = '';
    focus() { this.focused = true; }
    setPointerCapture() {}
    getBoundingClientRect() { return { left: 0, top: 0, width: 512, height: 288 }; }
  }
  const elements = new Map(['screen', 'viewport', 'host', 'status', 'notice', 'focus', 'resolution', 'disconnect', 'reconnect', 'fullscreen', 'fingerbot', 'fingerbot-feedback'].map(id => [id, new Element()]));
  const screen = elements.get('screen'), draws = [], sent = [], requests = [], confirmations = [];
  const context = { drawImage: (...args) => draws.push(args) };
  screen.width = 640; screen.height = 400; screen.getContext = () => context;
  Object.assign(elements.get('viewport'), { clientWidth: 528, clientHeight: 304 });
  const button = Object.assign(new Element(), { dataset: { key: 'ctrlc' } });
  let confirm = false, enabled = true, fail = false, decoder, closedFrames = 0;
  class Socket {
    static OPEN = 1; static CLOSED = 3;
    readyState = 1;
    constructor(url) { this.url = url; sockets.push(this); }
    send(data) { sent.push(typeof data === 'string' ? JSON.parse(data) : data); }
    close() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
  }
  const sockets = [];
  const frame = () => ({ width: 1920, height: 1080, displayWidth: 1920, displayHeight: 1080, close() { closedFrames++; } });
  class Decoder {
    static async isConfigSupported() { return { supported: true }; }
    state = 'unconfigured'; decodeQueueSize = 0;
    constructor(options) { decoder = this; this.options = options; }
    configure(config) { assert.equal(config.codec, 'avc1.42E01F'); this.state = 'configured'; }
    decode(chunk) { assert.equal(chunk.type, 'key'); this.options.output(frame()); }
    close() { this.state = 'closed'; }
  }
  const window = new EventTarget(); window.self = window.top = window;
  window.confirm = message => { confirmations.push(message); return confirm; };
  const document = Object.assign(new EventTarget(), { hidden: false, getElementById: id => elements.get(id), querySelectorAll: () => [button] });
  Object.assign(globalThis, { window, document, location: { origin: 'http://127.0.0.1:1234', pathname: '/console/stargate/' }, WebSocket: Socket,
    ResizeObserver: class { observe() {} }, VideoDecoder: fallback ? undefined : Decoder, EncodedVideoChunk: class { constructor(value) { Object.assign(this, value); } },
    createImageBitmap: async () => frame(),
    fetch: async (path, options) => {
      requests.push([path, options?.method ?? 'GET']);
      return { ok: !fail, status: fail ? 502 : 200, blob: async () => ({}), json: async () => path.endsWith('session') ? { host: 'stargate', protocol: 'glkvm' } : { enabled, duration: 500, strength: 1 } };
    },
  });
  await import(`./glkvm-client.mjs?fallback=${fallback}`);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const socket = sockets.at(-1); socket.onopen();
  socket.onmessage({ data: JSON.stringify({ event_type: 'media', event: { video: { h264: { profile_level_id: '42E01F' } } } }) });
  await flush();
  if (!fallback) {
    assert.equal(decoder.state, 'configured');
    assert.ok(sent.some(value => value.event_type === 'start'));
    socket.onmessage({ data: new Uint8Array([1, 1, 0, 0, 0, 1]).buffer });
  }
  await flush();
  assert.equal(screen.style.width, '512px'); assert.equal(screen.style.height, '288px');
  assert.equal(screen.width, 512); assert.equal(screen.height, 288);
  assert.equal(context.imageSmoothingEnabled, true); assert.equal(context.imageSmoothingQuality, 'high');
  assert.deepEqual(draws.at(-1).slice(1), [0, 0, 512, 288], 'draw directly into the downsized backing store');
  window.devicePixelRatio = 2; window.dispatchEvent(new Event('resize'));
  assert.equal(screen.style.width, '512px'); assert.equal(screen.width, 1024);
  assert.equal(elements.get('resolution').textContent, '1920 × 1080', 'report the source resolution rather than backing dimensions');
  Object.assign(elements.get('viewport'), { clientWidth: 1296, clientHeight: 736 }); window.dispatchEvent(new Event('resize'));
  assert.equal(screen.width, 2560); assert.equal(screen.height, 1440);
  assert.equal(context.imageSmoothingEnabled, false, 'Retina enlargement preserves bitmap text edges');
  window.devicePixelRatio = 1.25;
  Object.assign(elements.get('viewport'), { clientWidth: 1019, clientHeight: 700 }); window.dispatchEvent(new Event('resize'));
  assert.equal(screen.width, 1254); assert.equal(screen.height, 705);
  assert.equal(Number.parseFloat(screen.style.width) * 1.25, screen.width, 'fractional DPR maps directly onto device pixels');
  window.devicePixelRatio = 1;
  Object.assign(elements.get('viewport'), { clientWidth: 528, clientHeight: 304 }); window.dispatchEvent(new Event('resize'));
  elements.get('viewport').clientWidth = 0; window.dispatchEvent(new Event('resize'));
  assert.equal(screen.style.width, '512px', 'hidden host tabs retain their video size');
  elements.get('viewport').clientWidth = 528; window.dispatchEvent(new Event('resize'));
  assert.equal(elements.get('status').textContent, 'Connected');
  assert.ok(draws.length > 0); assert.ok(closedFrames > 0);
  const key = (type, values) => Object.assign(new Event(type, { cancelable: true }), values);
  screen.dispatchEvent(key('keydown', { code: 'KeyZ' }));
  window.dispatchEvent(new Event('blur'));
  assert.deepEqual(sent.filter(value => value.event_type === 'key').slice(-2), [
    { event_type: 'key', event: { key: 'KeyZ', state: true } }, { event_type: 'key', event: { key: 'KeyZ', state: false } },
  ]);
  screen.dispatchEvent(key('pointerdown', { clientX: 512, clientY: 288, button: 2, pointerId: 1 }));
  assert.deepEqual(sent.find(value => value.event_type === 'mouse_move').event.to, { x: 32767, y: 32767 });
  window.dispatchEvent(new Event('blur'));
  assert.deepEqual(sent.filter(value => value.event_type === 'mouse_button').slice(-2).map(value => value.event), [{ button: 'right', state: true }, { button: 'right', state: false }]);
  button.dispatchEvent(new Event('click'));
  assert.deepEqual(sent.filter(value => value.event_type === 'key').slice(-4).map(value => [value.event.key, value.event.state]), [['ControlLeft', true], ['KeyC', true], ['KeyC', false], ['ControlLeft', false]]);
  const fingerbot = elements.get('fingerbot');
  assert.equal(fingerbot.hidden, false);
  await fingerbot.onclick();
  assert.equal(requests.filter(([, method]) => method === 'POST').length, 0);
  assert.match(confirmations.at(-1), /stargate.*0.5 s/);
  confirm = true; fail = true;
  const press = fingerbot.onclick(); await fingerbot.onclick(); await press;
  assert.equal(requests.filter(([, method]) => method === 'POST').length, 1);
  assert.match(elements.get('fingerbot-feedback').textContent, /may have pressed/);
  t.mock.timers.tick(10000); await flush();
  assert.equal(requests.filter(([, method]) => method === 'POST').length, 1, 'failed presses never retry');
  fail = false; enabled = false;
  document.dispatchEvent(new Event('visibilitychange')); await flush();
  assert.equal(fingerbot.hidden, true);
  await fingerbot.onclick();
  assert.equal(requests.filter(([, method]) => method === 'POST').length, 1);
  elements.get('disconnect').onclick();
  const count = sockets.length; t.mock.timers.tick(30000); await flush();
  assert.equal(sockets.length, count, 'explicit disconnect cancels recovery');
  window.dispatchEvent(new Event('pagehide'));
});
