import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

test('AMI bootstrap clears reload guards and keeps requests on the selected local relay', async () => {
  const storage = new Map([['reloaded', '1'], ['status', 'stop_session']]);
  const appended = [];
  class Request { open(method, url) { this.url = url; } }
  class Socket extends EventTarget { constructor(url) { super(); this.url = url; } }
  const context = {
    URL, XMLHttpRequest: Request, WebSocket: Socket,
    location: { href: 'http://127.0.0.1:1234/asmb/nexus/viewer.html', host: '127.0.0.1:1234', origin: 'http://127.0.0.1:1234', pathname: '/asmb/nexus/viewer.html' },
    sessionStorage: { setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) },
    document: { createElement: () => ({ dataset: {} }), body: { append: script => appended.push(script) } },
    fetch: async path => { assert.equal(path, '/asmb/nexus/api/configuration/project'); return { ok: true, json: async () => [{ feature: 'HTML5_KVM' }] }; },
    parent: { postMessage() { assert.fail('Bootstrap unexpectedly failed'); } },
  };
  context.window = context;
  await vm.runInNewContext(readFileSync(new URL('./asmb-bootstrap.mjs', import.meta.url), 'utf8'), context);
  assert.equal(storage.has('reloaded'), false); assert.equal(storage.has('status'), false);
  assert.equal(storage.get('garc'), 'local-relay');
  assert.match(appended[0].src, /^\/asmb\/nexus\/viewer\.min\.js\?v=\d+$/);
  const request = new Request(); request.open('GET', '/api/kvm/token');
  assert.equal(request.url, 'http://127.0.0.1:1234/asmb/nexus/api/kvm/token');
  assert.equal(new context.WebSocket('ws://127.0.0.1:1234/kvm').url, 'ws://127.0.0.1:1234/asmb/nexus/kvm');
  assert.throws(() => new context.WebSocket('wss://evil.example/kvm'), /Unexpected ASMB10 destination/);
});
