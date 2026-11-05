import { test } from 'node:test';
import assert from 'node:assert/strict';
import https from 'node:https';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { gzipSync } from 'node:zlib';
import { WebSocket, WebSocketServer } from 'ws';
import { createBmc, bmcPath } from './bmc.mjs';
import { createConsole, consoleHosts } from './server.mjs';

test('console inventory includes IPMI without admitting unrelated hosts', () => {
  assert.deepEqual(consoleHosts({ amt: { hosts: ['ms-01'] }, ipmi: { hosts: ['nexus'], children: ['ipmi'] }, other: { hosts: ['gateway'] } }), ['ms-01', 'nexus']);
  for (const path of ['/api/session', '/api/actions/power', '/api/setsession', '/libs/../api/session', '/images/x.html', '//evil.example/a.js']) assert.equal(bmcPath(path), false, path);
});

test('ASMB10 authenticates on pinned TLS, relays binary KVM and releases its session', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'asmb-test-'));
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=fixture', '-days', '1', '-keyout', join(directory, 'key.pem'), '-out', join(directory, 'cert.pem')], { stdio: 'ignore' });
  const certificate = readFileSync(join(directory, 'cert.pem'), 'utf8');
  const keyboardTemplate = String.raw`<input id=\'kvm_textbox\' name=\'password\' autocorrect="off" autocapitalize="off" autocomplete="off"/>`;
  const requests = []; let logouts = 0, stopped = false;
  const firmware = https.createServer({ key: readFileSync(join(directory, 'key.pem')), cert: certificate }, async (req, res) => {
    requests.push(req.url);
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    if (req.url === '/api/session' && req.method === 'POST') {
      assert.deepEqual(Object.fromEntries(new URLSearchParams(Buffer.concat(chunks).toString())), { username: 'admin', password: 'secret &+' });
      res.writeHead(200, { 'Content-Type': 'application/json', 'Set-Cookie': 'QSESSIONID=fixture-secret; HttpOnly' }).end(JSON.stringify({ ok: 0, CSRFToken: 'csrf-secret', extendedpriv: 1 }));
      return;
    }
    assert.equal(req.headers.cookie, 'QSESSIONID=fixture-secret');
    assert.equal(req.headers['x-csrftoken'], 'csrf-secret');
    if (req.method === 'DELETE') { logouts++; res.end('{}'); return; }
    if (req.url.startsWith('/viewer.min.js')) {
      res.writeHead(200, { 'Content-Type': 'text/javascript', 'Content-Encoding': 'gzip' }).end(gzipSync(`globalThis.keyboardTemplate = '${keyboardTemplate}';`));
      return;
    }
    res.writeHead(200, { 'Content-Type': 'text/html', 'Content-Encoding': 'gzip' }).end(gzipSync('<html><head></head><body><script data-main="/app/main" src="/viewer.min.js"></script></body></html>'));
  });
  const wss = new WebSocketServer({ noServer: true });
  firmware.on('upgrade', (req, socket, head) => {
    assert.equal(req.url, '/kvm'); assert.equal(req.headers.cookie, 'QSESSIONID=fixture-secret');
    wss.handleUpgrade(req, socket, head, ws => { ws.send(Buffer.from([1, 2, 3])); ws.on('message', data => { if (data.equals(Buffer.from([8, 0, 0, 0, 0, 0, 0, 0]))) stopped = true; else ws.send(data); }); });
  });
  await new Promise(resolve => firmware.listen(0, '127.0.0.1', resolve));
  const config = { name: 'nexus', protocol: 'ipmi', host: '127.0.0.1', port: firmware.address().port, certificate, username: 'admin', password: 'secret &+' };
  const app = createConsole(config, undefined, directory);
  t.after(async () => { app.close(); wss.clients.forEach(ws => ws.terminate()); wss.close(); firmware.closeAllConnections(); await new Promise(resolve => firmware.close(resolve)); rmSync(directory, { recursive: true, force: true }); });
  const launch = await app.listen(), origin = new URL(launch).origin;
  const redirect = await fetch(launch, { redirect: 'manual' });
  const headers = { Cookie: redirect.headers.get('set-cookie').split(';')[0] };
  const shell = await (await fetch(origin, { headers })).text();
  assert.match(shell, /asmb-client.mjs/); assert.match(shell, /<iframe id="viewer"/); assert.doesNotMatch(shell, /amt-desktop/);
  assert.deepEqual(await (await fetch(`${origin}/session`, { headers })).json(), { host: 'nexus', protocol: 'ipmi' });
  assert.equal((await fetch(`${origin}/asmb/nexus/viewer.html`)).status, 403);
  const page = await fetch(`${origin}/asmb/nexus/viewer.html`, { headers });
  assert.equal(page.status, 200);
  const html = await page.text();
  assert.match(html, /src="\/asmb-bootstrap.mjs\?v=/); assert.doesNotMatch(html, /data-main=/);
  for (const secret of ['secret &+', 'fixture-secret', 'csrf-secret']) assert.equal(html.includes(secret), false);
  const script = await (await fetch(`${origin}/asmb/nexus/viewer.min.js?v=1`, { headers })).text();
  const { runInNewContext } = await import('node:vm');
  const browser = {};
  runInNewContext(script, browser);
  assert.match(browser.keyboardTemplate, /id='kvm_textbox'/);
  assert.match(browser.keyboardTemplate, /type="button"/);
  assert.match(browser.keyboardTemplate, /name='hardware-console-input'/);
  assert.doesNotMatch(browser.keyboardTemplate, /name='password'/);
  for (const path of ['/asmb/nexus/api/actions/power', '/asmb/nexus/api/session', '/asmb/other/viewer.html']) assert.equal((await fetch(origin + path, { headers })).status, 404);
  assert.equal(requests.filter(path => path === '/api/session').length, 1);
  const wrongOrigin = new WebSocket(`${origin.replace('http', 'ws')}/asmb/nexus/kvm`, { headers: { ...headers, Origin: 'https://evil.example' } });
  await assert.rejects(once(wrongOrigin, 'open'), /403/);
  const ws = new WebSocket(`${origin.replace('http', 'ws')}/asmb/nexus/kvm`, { headers: { ...headers, Origin: origin } });
  const first = once(ws, 'message'); await once(ws, 'open');
  assert.deepEqual((await first)[0], Buffer.from([1, 2, 3]));
  const echo = once(ws, 'message'); ws.send(Buffer.from([9, 8])); assert.deepEqual((await echo)[0], Buffer.from([9, 8]));
  const release = await fetch(`${origin}/release/nexus`, { method: 'POST', headers: { Authorization: `Bearer ${new URL(launch).searchParams.get('token')}` } });
  assert.equal(release.status, 200); await once(ws, 'close');
  assert.equal((await fetch(`${origin}/asmb/nexus/viewer.html`, { headers })).status, 410);
  for (let i = 0; i < 20 && !logouts; i++) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(logouts, 1);
  assert.equal(stopped, true, 'session release sends the AMI stop command before closing TLS');
  const wrongPin = createBmc({ ...config, certificate: readFileSync(new URL('../ansible/inventory/group_vars/ipmi/certificates/nexus.pem', import.meta.url), 'utf8') });
  await assert.rejects(wrongPin.get('/viewer.html'));
  wrongPin.close();
});
