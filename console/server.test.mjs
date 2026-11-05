import { test } from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import http from 'node:http';
import { Duplex } from 'node:stream';
import { mkdtempSync, readdirSync, statSync, rmSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { WebSocket } from 'ws';
import { amtHosts, createConsole } from './server.mjs';
import { registerSession, releaseSessions } from './sessions.mjs';

test('all-host launch uses only distinct AMT inventory hosts, including child groups', () => {
  assert.deepEqual(amtHosts({
    amt: { hosts: ['ms-03'], children: ['cluster'] },
    cluster: { hosts: ['ms-02', 'ms-01', 'ms-03'], children: ['amt'] },
    unrelated: { hosts: ['nexus'] },
  }), ['ms-01', 'ms-02', 'ms-03']);
});

test('loopback app requires its launch cookie; rejects cross-origin upgrades and arbitrary paths', async t => {
  let dialed = 0;
  const app = createConsole({ name: 'ms-01' }, () => { dialed++; return new Duplex({ read() {}, write(_d, _e, cb) { cb(); } }); });
  const launch = await app.listen();
  t.after(() => app.close());
  const origin = new URL(launch).origin;
  assert.equal(app.server.address().address, '127.0.0.1');
  assert.equal((await fetch(origin)).status, 403);
  assert.equal((await fetch(`${origin}/session`)).status, 403);
  const redirect = await fetch(launch, { redirect: 'manual' });
  assert.equal(redirect.status, 303);
  assert.equal(redirect.headers.get('location'), '/');
  assert.match(redirect.headers.get('set-cookie'), /HttpOnly; SameSite=Strict/);
  assert.equal(redirect.headers.get('referrer-policy'), 'no-referrer');
  const cookie = redirect.headers.get('set-cookie').split(';')[0];
  assert.equal((await fetch(`${origin}/session`, { headers: { Cookie: cookie } })).status, 200);
  const page = await fetch(origin, { headers: { Cookie: cookie } });
  assert.match(page.headers.get('content-security-policy'), /frame-ancestors 'none'/);
  assert.match(await page.text(), /Remote console/);
  for (const [path, type] of [['/logo.svg', 'image/svg+xml'], ['/favicon.ico', 'image/x-icon']]) {
    const asset = await fetch(`${origin}${path}`, { headers: { Cookie: cookie } });
    assert.equal(asset.status, 200);
    assert.ok(asset.headers.get('content-type').startsWith(type));
    assert.ok((await asset.arrayBuffer()).byteLength > 0);
  }
  assert.equal((await fetch(`${origin}/server.mjs`, { headers: { Cookie: cookie } })).status, 404);
  const wrongHost = await new Promise((resolve, reject) => {
    const request = http.get(origin, { headers: { Cookie: cookie, Host: 'evil.example' } }, response => { response.resume(); resolve(response.statusCode); });
    request.on('error', reject);
  });
  assert.equal(wrongHost, 403);
  for (const headers of [{ Origin: 'https://evil.example', Cookie: cookie }, { Origin: origin }]) {
    const ws = new WebSocket(`${origin.replace('http', 'ws')}/kvm`, { headers });
    await assert.rejects(once(ws, 'open'), /403/);
  }
  assert.equal(dialed, 0);
});

test('local shutdown announces a terminal close and destroys the AMT connection', async () => {
  const remote = new Duplex({ read() {}, write(_d, _e, cb) { cb(); } });
  const app = createConsole({ name: 'ms-01' }, () => remote);
  const launch = await app.listen(), origin = new URL(launch).origin;
  const redirect = await fetch(launch, { redirect: 'manual' });
  const ws = new WebSocket(`${origin.replace('http', 'ws')}/kvm`, {
    headers: { Cookie: redirect.headers.get('set-cookie').split(';')[0], Origin: origin },
  });
  try {
    await once(ws, 'open');
    const closed = once(ws, 'close');
    app.close();
    const [code, reason] = await closed;
    assert.equal(code, 1001);
    assert.equal(reason.toString(), 'Local console stopped');
    assert.equal(remote.destroyed, true);
  } finally { app.close(); }
});

test('separate launches use separate cookies so consoles for different hosts can coexist', async t => {
  const one = createConsole({ name: 'ms-01' }), two = createConsole({ name: 'ms-02' });
  t.after(() => { one.close(); two.close(); });
  const response1 = await fetch(await one.listen(), { redirect: 'manual' });
  const response2 = await fetch(await two.listen(), { redirect: 'manual' });
  assert.notEqual(response1.headers.get('set-cookie').split('=')[0], response2.headers.get('set-cookie').split('=')[0]);
});

test('tabbed workspace routes independent sessions and retains per-host access controls', async t => {
  const dialed = [];
  const app = createConsole(['ms-01', 'ms-02', 'ms-03'].map(name => ({ name, password: 'never exposed' })), config => {
    dialed.push(config.name);
    return new Duplex({ read() {}, write(_d, _e, cb) { cb(); } });
  });
  t.after(() => app.close());
  const launch = await app.listen(), origin = new URL(launch).origin;
  const redirect = await fetch(launch, { redirect: 'manual' });
  const cookie = redirect.headers.get('set-cookie').split(';')[0];
  const headers = { Cookie: cookie };
  const page = await fetch(origin, { headers });
  assert.match(await page.text(), /role="tablist"/);
  assert.equal(page.headers.get('x-frame-options'), 'DENY');
  assert.deepEqual(await (await fetch(`${origin}/session`, { headers })).json(), { hosts: ['ms-01', 'ms-02', 'ms-03'] });
  for (const host of ['ms-01', 'ms-02', 'ms-03']) {
    assert.equal((await fetch(`${origin}/console/${host}/`)).status, 403);
    const viewer = await fetch(`${origin}/console/${host}/`, { headers });
    assert.match(await viewer.text(), /Remote console/);
    assert.equal(viewer.headers.get('x-frame-options'), 'SAMEORIGIN');
    assert.match(viewer.headers.get('content-security-policy'), /frame-ancestors 'self'/);
    assert.deepEqual(await (await fetch(`${origin}/console/${host}/session`, { headers })).json(), { host });
  }
  assert.equal((await fetch(`${origin}/console/unknown/`, { headers })).status, 404);
  for (const [path, wsHeaders] of [
    ['/kvm', { ...headers, Origin: origin }],
    ['/console/unknown/kvm', { ...headers, Origin: origin }],
    ['/console/ms-01/kvm', { ...headers, Origin: 'https://evil.example' }],
    ['/console/ms-01/kvm', { Origin: origin }],
  ]) {
    await assert.rejects(once(new WebSocket(`${origin.replace('http', 'ws')}${path}`, { headers: wsHeaders }), 'open'), /403/);
  }
  const sockets = [];
  for (const host of ['ms-01', 'ms-02', 'ms-03']) {
    const ws = new WebSocket(`${origin.replace('http', 'ws')}/console/${host}/kvm`, { headers: { ...headers, Origin: origin } });
    sockets.push(ws); await once(ws, 'open');
  }
  assert.deepEqual(dialed, ['ms-01', 'ms-02', 'ms-03']);
  const duplicate = new WebSocket(`${origin.replace('http', 'ws')}/console/ms-01/kvm`, { headers: { ...headers, Origin: origin } });
  await assert.rejects(once(duplicate, 'open'), /403/);
  sockets[0].close(); await once(sockets[0], 'close');
  assert.equal(sockets[1].readyState, WebSocket.OPEN);
  assert.equal(sockets[2].readyState, WebSocket.OPEN);
  const replacement = new WebSocket(`${origin.replace('http', 'ws')}/console/ms-01/kvm`, { headers: { ...headers, Origin: origin } });
  await once(replacement, 'open');
  assert.deepEqual(dialed, ['ms-01', 'ms-02', 'ms-03', 'ms-01']);
});

test('release command stops one host across local instances and prevents automatic reclaim', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'console-release-test-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const streams = [], dialed = [];
  const dial = config => {
    dialed.push(config.name);
    const remote = new Duplex({ read() {}, write(_d, _e, cb) { cb(); } });
    streams.push(remote); return remote;
  };
  const apps = [
    createConsole([{ name: 'ms-01' }, { name: 'ms-02' }], dial, directory),
    createConsole({ name: 'ms-01' }, dial, directory),
  ];
  t.after(() => apps.forEach(app => app.close()));
  const viewers = [];
  const launches = [];
  for (const [index, app] of apps.entries()) {
    const launch = await app.listen(), origin = new URL(launch).origin;
    launches.push(launch);
    const token = new URL(launch).searchParams.get('token');
    const redirect = await fetch(launch, { redirect: 'manual' });
    const cookie = redirect.headers.get('set-cookie').split(';')[0];
    const url = `${origin.replace('http', 'ws')}${index ? '/kvm' : '/console/ms-01/kvm'}`;
    const headers = { Cookie: cookie, Origin: origin };
    for (const auth of [{}, { Authorization: `Bearer ${token}`, Origin: 'https://evil.example' }, { Cookie: cookie }]) {
      assert.equal((await fetch(`${origin}/release/ms-01`, { method: 'POST', headers: auth })).status, 403);
    }
    assert.equal((await fetch(`${origin}/release/unknown`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })).status, 404);
    const ws = new WebSocket(url, { headers }); await once(ws, 'open');
    viewers.push({ ws, url, headers });
    if (!index) {
      const other = new WebSocket(`${origin.replace('http', 'ws')}/console/ms-02/kvm`, { headers });
      await once(other, 'open'); viewers.push({ ws: other, other: true });
    }
  }
  for (const file of readdirSync(directory)) assert.equal(statSync(join(directory, file)).mode & 0o077, 0);
  const owners = viewers.filter(viewer => !viewer.other);
  const messages = owners.map(viewer => once(viewer.ws, 'message'));
  const closed = owners.map(viewer => once(viewer.ws, 'close'));
  assert.equal(await releaseSessions('ms-01', directory), 2);
  for (const message of await Promise.all(messages)) {
    const result = JSON.parse(message[0]);
    assert.equal(result.retryable, false); assert.match(result.error, /Session released/);
  }
  for (const [code] of await Promise.all(closed)) assert.equal(code, 4001);
  assert.equal(streams[0].destroyed, true);
  assert.equal(streams[1].destroyed, false, 'ms-02 is unaffected');
  assert.equal(streams[2].destroyed, true);
  assert.equal(viewers.find(viewer => viewer.other).ws.readyState, WebSocket.OPEN);
  for (const viewer of owners) {
    const retry = new WebSocket(viewer.url, { headers: viewer.headers });
    const message = once(retry, 'message'), close = once(retry, 'close');
    assert.equal(JSON.parse((await message)[0]).retryable, false);
    assert.equal((await close)[0], 4001);
  }
  assert.deepEqual(dialed, ['ms-01', 'ms-02', 'ms-01'], 'blocked reconnects never reach AMT');
  assert.equal(await releaseSessions('ms-01', directory), 0, 'release is idempotent');
  assert.equal(await releaseSessions('unknown', directory), 0);
  await assert.rejects(releaseSessions('../ms-01', directory), /Usage/);
  assert.equal((await fetch(launches[0], { redirect: 'manual' })).status, 303);
  const resumed = new WebSocket(owners[0].url, { headers: owners[0].headers });
  await once(resumed, 'open');
  assert.deepEqual(dialed, ['ms-01', 'ms-02', 'ms-01', 'ms-01'], 'explicitly reopening a launch resumes its released host');
  resumed.close();
  await once(resumed, 'close');
  const fresh = createConsole({ name: 'ms-01' }, dial, directory);
  apps.push(fresh);
  const launch = await fresh.listen(), origin = new URL(launch).origin;
  const redirect = await fetch(launch, { redirect: 'manual' });
  const ws = new WebSocket(`${origin.replace('http', 'ws')}/kvm`, {
    headers: { Cookie: redirect.headers.get('set-cookie').split(';')[0], Origin: origin },
  });
  await once(ws, 'open');
  assert.deepEqual(dialed, ['ms-01', 'ms-02', 'ms-01', 'ms-01', 'ms-01'], 'a new launch can claim the released host');
  apps.forEach(app => app.close());
  assert.deepEqual(readdirSync(directory), [], 'clean shutdown removes registry entries');
});

test('release rejects unsafe registry entries and removes dead local registrations', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'console-registry-test-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const remove = registerSession({ origin: 'https://evil.example', token: 'a'.repeat(64), hosts: ['ms-01'] }, directory);
  await assert.rejects(releaseSessions('ms-01', directory), /Invalid local console registry/);
  remove();
  const server = http.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  await new Promise(resolve => server.close(resolve));
  registerSession({ origin, token: 'b'.repeat(64), hosts: ['ms-01'] }, directory);
  assert.equal(await releaseSessions('ms-01', directory), 0);
  assert.deepEqual(readdirSync(directory), []);
  // A shared registry would expose local control tokens to other users.
  if (process.getuid) {
    chmodSync(directory, 0o755);
    await assert.rejects(releaseSessions('ms-01', directory), /private directory/);
    chmodSync(directory, 0o700);
  }
  await assert.rejects(releaseSessions('', directory), /Usage/);
});
