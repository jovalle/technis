import { test } from 'node:test';
import assert from 'node:assert/strict';
import https from 'node:https';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { WebSocket, WebSocketServer } from 'ws';
import { createGlkvm, fingerbotSettings } from './glkvm.mjs';
import { consoleHosts, createConsole } from './server.mjs';

test('GLKVM discovery and Fingerbot calibration reject unknown hosts and invalid hardware settings', () => {
  assert.deepEqual(consoleHosts({ amt: { hosts: ['ms-01'] }, ipmi: { hosts: ['nexus'] }, glkvm: { hosts: ['stargate'] }, other: { hosts: ['router'] } }), ['ms-01', 'nexus', 'stargate']);
  assert.deepEqual(fingerbotSettings({ fingerbot_press_time: 500, fingerbot_strength: 1 }), { duration: 500, strength: 1 });
  for (const duration of [0, 99, 60001, '500', undefined]) assert.throws(() => fingerbotSettings({ fingerbot_press_time: duration, fingerbot_strength: 1 }));
  for (const strength of [0, 3, '1', undefined]) assert.throws(() => fingerbotSettings({ fingerbot_press_time: 500, fingerbot_strength: strength }));
});

for (const multiple of [false, true]) test(`GLKVM pins TLS before credentials, relays video/HID, gates Fingerbot and releases sessions (${multiple ? 'workspace' : 'single'})`, async t => {
  const directory = mkdtempSync(join(tmpdir(), 'glkvm-test-'));
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=fixture', '-days', '1', '-keyout', join(directory, 'key.pem'), '-out', join(directory, 'cert.pem')], { stdio: 'ignore' });
  const certificate = readFileSync(join(directory, 'cert.pem'), 'utf8');
  let requests = 0, presses = 0, enabled = true;
  const params = { h264_bitrate: 2000, quality: 80 }, qualityWrites = [];
  let maximumBitrate = 20000;
  const hid = [], remoteSockets = [];
  const server = https.createServer({ key: readFileSync(join(directory, 'key.pem')), cert: certificate }, async (req, res) => {
    requests++;
    const result = value => res.setHeader('Content-Type', 'application/json').end(JSON.stringify({ ok: true, result: value }));
    if (req.url === '/api/auth/login') {
      let body = ''; for await (const chunk of req) body += chunk;
      assert.deepEqual(Object.fromEntries(new URLSearchParams(body)), { user: 'admin', passwd: 'fixture password &+' });
      res.setHeader('Set-Cookie', 'auth_token=private-fixture; Secure; HttpOnly'); result({ token: 'private-fixture' }); return;
    }
    assert.equal(req.headers.cookie, 'auth_token=private-fixture');
    if (req.url === '/api/auth/logout') { result({}); return; }
    if (req.url === '/api/fingerbot/exist') { result({ exist: enabled }); return; }
    if (req.url === '/api/system/get_config') { result({ config: { fingerbot_press_time: 500, fingerbot_strength: 1, unrelated_secret: 'hidden-setting' } }); return; }
    if (req.url === '/api/fingerbot/click?press_time=500&angle_enum=1') { presses++; result({ result: 'success' }); return; }
    if (req.url === '/api/streamer/snapshot') { res.setHeader('Content-Type', 'image/jpeg').end(Buffer.from([255, 216, 255, 217])); return; }
    if (req.url === '/api/streamer') { result({ params, limits: { h264_bitrate: { max: maximumBitrate } } }); return; }
    if (req.url.startsWith('/api/streamer/set_params?')) {
      assert.equal(req.method, 'POST');
      const updates = Object.fromEntries(new URL(req.url, 'https://fixture').searchParams);
      qualityWrites.push(updates);
      for (const [key, value] of Object.entries(updates)) { assert.ok(['h264_bitrate', 'quality'].includes(key)); params[key] = Number(value); }
      result({}); return;
    }
    assert.fail(`Unexpected GLKVM request ${req.url}`);
  });
  const wss = new WebSocketServer({ noServer: true });
  server.on('upgrade', (req, socket, head) => {
    assert.equal(req.headers.cookie, 'auth_token=private-fixture');
    assert.ok(['/api/ws?stream=true', '/api/media/ws'].includes(req.url));
    wss.handleUpgrade(req, socket, head, ws => {
      remoteSockets.push(ws);
      if (req.url === '/api/media/ws') {
        ws.send(JSON.stringify({ event_type: 'media', event: { video: { h264: { profile_level_id: '42E01F' } } } }));
        ws.on('message', (data, binary) => { if (!binary && JSON.parse(data).event_type === 'start') ws.send(Buffer.from([1, 1, 0, 0, 0, 1])); });
      } else {
        ws.send(JSON.stringify({ event_type: 'streamer', event: { streamer: { source: { online: true } } } }));
        ws.send(JSON.stringify({ event_type: 'unrelated', event: { secret: 'hidden-setting' } }));
        ws.on('message', (data, binary) => { if (!binary) hid.push(JSON.parse(data)); });
      }
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const config = { name: 'stargate', protocol: 'glkvm', host: '127.0.0.1', port: server.address().port, certificate, username: 'admin', password: 'fixture password &+' };
  const app = createConsole(multiple ? [config] : config, undefined, directory);
  const bad = createGlkvm({ ...config, certificate: readFileSync(new URL('../ansible/inventory/group_vars/ipmi/certificates/nexus.pem', import.meta.url), 'utf8') });
  t.after(async () => {
    app.close(); bad.close(); remoteSockets.forEach(ws => ws.terminate()); wss.close();
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); rmSync(directory, { recursive: true, force: true });
  });
  await assert.rejects(bad.status('/api/fingerbot/exist'), /pin/);
  assert.equal(requests, 0, 'wrong certificate sends no credentials or HTTP requests');
  const launch = await app.listen(), origin = new URL(launch).origin, base = multiple ? '/console/stargate/' : '/';
  const redirect = await fetch(launch, { redirect: 'manual' });
  const headers = { Cookie: redirect.headers.get('set-cookie').split(';')[0] };
  const shell = await (await fetch(origin + base, { headers })).text();
  assert.match(shell, /glkvm-client.mjs/); assert.match(shell, /id="fingerbot"[^>]*hidden/);
  assert.doesNotMatch(shell, /amt-desktop|power-client|power-controls/);
  const get = (path, options = {}) => fetch(origin + base + path + (path === 'fingerbot/press' ? '?duration=500&strength=1' : ''), { headers, ...options });
  assert.equal((await fetch(origin + base + 'fingerbot')).status, 403);
  assert.deepEqual(await (await get('fingerbot')).json(), { enabled: true, duration: 500, strength: 1 });
  enabled = false;
  assert.deepEqual(await (await get('fingerbot')).json(), { enabled: false });
  assert.equal((await get('fingerbot/press', { method: 'POST', headers: { ...headers, Origin: origin } })).status, 502);
  assert.equal(presses, 0);
  // Use the real adapter for successful actions after the server-side cooldown.
  const adapter = createGlkvm(config);
  t.after(() => adapter.close());
  enabled = true;
  await assert.rejects(adapter.fingerbot(true, { duration: 8000, strength: 1 }), /calibration changed/);
  assert.deepEqual(await adapter.fingerbot(true, { duration: 500, strength: 1 }), { accepted: true });
  assert.equal(presses, 1);
  for (const [path, options, status] of [
    ['fingerbot/press', {}, 405],
    ['fingerbot/press', { method: 'POST', headers: { ...headers, Origin: 'https://evil.example' } }, 403],
    ['fingerbot/push', { method: 'POST', headers: { ...headers, Origin: origin } }, 403],
    ['fingerbot/press', { method: 'POST', headers: { ...headers, Origin: origin } }, 409],
    ['power/off', { method: 'POST', headers: { ...headers, Origin: origin } }, 403],
  ]) assert.equal((await get(path, options)).status, status, path);
  assert.equal(presses, 1);
  assert.equal((await get('snapshot')).headers.get('content-type'), 'image/jpeg');
  const ws = new WebSocket(`${origin.replace('http', 'ws')}${base}kvm`, { headers: { ...headers, Origin: origin } });
  const received = [];
  ws.on('message', (data, binary) => {
    received.push(binary ? data : JSON.parse(data));
    if (!binary && JSON.parse(data).event_type === 'media') {
      ws.send(JSON.stringify({ event_type: 'start', event: { type: 'video', format: 'h264' } }));
      ws.send(JSON.stringify({ event_type: 'key', event: { key: 'KeyZ', state: true } }));
    }
  });
  await once(ws, 'open');
  await new Promise((resolve, reject) => { const timer = setTimeout(() => reject(new Error(`No H264 frame: ${JSON.stringify({received,hid,states:remoteSockets.map(ws=>ws.readyState)})}`)), 2000); ws.on('message', (_, binary) => { if (binary) { clearTimeout(timer); resolve(); } }); });
  assert.deepEqual(hid, [{ event_type: 'key', event: { key: 'KeyZ', state: true } }]);
  assert.ok(received.some(value => Buffer.isBuffer(value) && value.equals(Buffer.from([1, 1, 0, 0, 0, 1]))));
  assert.equal(JSON.stringify(received).includes('hidden-setting'), false);
  assert.deepEqual(qualityWrites, [{ h264_bitrate: '8000', quality: '95' }]);
  await adapter.improveQuality();
  assert.equal(qualityWrites.length, 1, 'quality changes are idempotent');
  params.h264_bitrate = 20000; params.quality = 100;
  await adapter.improveQuality();
  assert.equal(qualityWrites.length, 1, 'existing higher quality is preserved');
  maximumBitrate = 4000; params.h264_bitrate = 2000;
  await adapter.improveQuality();
  assert.equal(params.h264_bitrate, 4000, 'quality stays within device limits');
  await fetch(`${origin}/release/stargate`, { method: 'POST', headers: { Authorization: `Bearer ${new URL(launch).searchParams.get('token')}` } });
  await once(ws, 'close');
  assert.equal((await get('fingerbot')).status, 410);
  assert.equal((await get('snapshot')).status, 410);
});
