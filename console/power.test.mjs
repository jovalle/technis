import { test } from 'node:test';
import assert from 'node:assert/strict';
import https from 'node:https';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { Duplex } from 'node:stream';
import { once } from 'node:events';
import { WebSocket } from 'ws';
import { power, powerActions } from './power.mjs';
import { createConsole } from './server.mjs';

const md5 = value => createHash('md5').update(value).digest('hex');
const cim = 'http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/';
const enumeration = 'http://schemas.xmlsoap.org/ws/2004/09/enumeration';
const soap = content => `<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>${content}</s:Body></s:Envelope>`;

test('pinned AMT WS-Man and IPMI Redfish send every power action, report status and reject firmware failures', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'power-test-'));
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=fixture', '-days', '1', '-keyout', join(directory, 'key.pem'), '-out', join(directory, 'cert.pem')], { stdio: 'ignore' });
  const certificate = readFileSync(join(directory, 'cert.pem'), 'utf8');
  const commands = [], authenticated = [];
  let fail = false, allowCycle = true, graceful = true, actionInfo = false, systemPath = '/redfish/v1/Systems/Fixture';
  const allowable = () => ['On', 'GracefulShutdown', 'ForceOff', 'ForceRestart', ...(allowCycle ? ['PowerCycle'] : [])];
  const server = https.createServer({ key: readFileSync(join(directory, 'key.pem')), cert: certificate }, async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks).toString();
    if (req.url === '/wsman') {
      if (!req.headers.authorization) { res.writeHead(401, { 'WWW-Authenticate': 'Digest realm="fixture", nonce="nonce", qop="auth", algorithm=MD5' }).end(); return; }
      const fields = Object.fromEntries([...req.headers.authorization.matchAll(/(\w+)=(?:"([^"]*)"|([^,\s]+))/g)].map(([, key, quoted, plain]) => [key, quoted ?? plain]));
      assert.equal(fields.username, 'admin'); assert.equal(fields.uri, '/wsman');
      assert.equal(fields.response, md5(`${md5('admin:fixture:secret &+')}:nonce:${fields.nc}:${fields.cnonce}:auth:${md5('POST:/wsman')}`));
      authenticated.push(req.url);
      if (body.includes('RequestPowerStateChange_INPUT')) {
        const code = Number(/<p:PowerState>(\d+)<\/p:PowerState>/.exec(body)?.[1]);
        commands.push(['amt', code]);
        assert.match(body, /<w:Selector Name="Name">Intel\(r\) AMT Power Management Service/);
        assert.match(body, /<w:Selector Name="Name">ManagedSystem/);
        res.end(soap(`<p:RequestPowerStateChange_OUTPUT xmlns:p="${cim}CIM_PowerManagementService"><p:ReturnValue>${fail ? 2 : 0}</p:ReturnValue></p:RequestPowerStateChange_OUTPUT>`));
      } else if (body.includes('<n:Enumerate/>')) {
        res.end(soap(`<n:EnumerateResponse xmlns:n="${enumeration}"><n:EnumerationContext>fixture</n:EnumerationContext></n:EnumerateResponse>`));
      } else {
        assert.match(body, /<n:Pull>/);
        res.end(soap(`<n:PullResponse xmlns:n="${enumeration}"><n:Items><p:CIM_AssociatedPowerManagementService xmlns:p="${cim}CIM_AssociatedPowerManagementService"><p:PowerState>2</p:PowerState><p:AvailableRequestedPowerStates>10</p:AvailableRequestedPowerStates>${graceful ? '<p:AvailableRequestedPowerStates>12</p:AvailableRequestedPowerStates>' : ''}<p:AvailableRequestedPowerStates>11</p:AvailableRequestedPowerStates></p:CIM_AssociatedPowerManagementService></n:Items><n:EndOfSequence/></n:PullResponse>`));
      }
      return;
    }
    assert.equal(req.headers.authorization, `Basic ${Buffer.from('admin:secret &+').toString('base64')}`);
    authenticated.push(req.url);
    if (req.url === '/redfish/v1/Systems') { res.end(JSON.stringify({ Members: [{ '@odata.id': systemPath }] })); return; }
    if (req.url === '/redfish/v1/Systems/Fixture') {
      res.end(JSON.stringify({ PowerState: 'On', Actions: { '#ComputerSystem.Reset': { target: `${systemPath}/Actions/ComputerSystem.Reset`, ...(actionInfo ? { '@Redfish.ActionInfo': `${systemPath}/ResetActionInfo` } : { 'ResetType@Redfish.AllowableValues': allowable() }) } } })); return;
    }
    if (req.url === '/redfish/v1/Systems/Fixture/ResetActionInfo') { res.end(JSON.stringify({ Parameters: [{ Name: 'ResetType', AllowableValues: allowable() }] })); return; }
    assert.equal(req.url, '/redfish/v1/Systems/Fixture/Actions/ComputerSystem.Reset');
    assert.equal(req.method, 'POST'); assert.match(req.headers['content-type'], /application\/json/);
    commands.push(['ipmi', JSON.parse(body).ResetType]);
    res.writeHead(fail ? 403 : 204).end();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const config = { name: 'fixture', host: '127.0.0.1', port: server.address().port, powerPort: server.address().port, certificate, username: 'admin', password: 'secret &+' };
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); rmSync(directory, { recursive: true, force: true }); });
  for (const protocol of ['amt', 'ipmi']) {
    const target = { ...config, protocol };
    assert.deepEqual(await power(target), { state: 'on', actions: Object.keys(protocol === 'amt' ? { on: 0, off: 0, restart: 0, cycle: 0, shutdown: 0 } : powerActions) });
    for (const action of Object.keys(powerActions)) assert.deepEqual(await power(target, action), { accepted: true });
    fail = true;
    await assert.rejects(power(target, 'restart'));
    fail = false;
    const wrongPin = readFileSync(new URL('../ansible/inventory/group_vars/ipmi/certificates/nexus.pem', import.meta.url), 'utf8');
    const before = authenticated.length;
    await assert.rejects(power({ ...target, certificate: wrongPin }));
    assert.equal(authenticated.length, before, 'wrong pins send no credentials');
  }
  assert.deepEqual(commands, [
    ['amt', 2], ['amt', 12], ['amt', 10], ['amt', 8], ['amt', 5], ['amt', 10],
    ['ipmi', 'On'], ['ipmi', 'GracefulShutdown'], ['ipmi', 'ForceRestart'], ['ipmi', 'ForceOff'], ['ipmi', 'PowerCycle'], ['ipmi', 'ForceRestart'],
  ]);
  graceful = false;
  assert.equal((await power({ ...config, protocol: 'amt' })).actions.includes('shutdown'), false);
  const beforeUnsupported = commands.length;
  await assert.rejects(power({ ...config, protocol: 'amt' }, 'shutdown'));
  assert.equal(commands.length, beforeUnsupported, 'unavailable graceful shutdown never sends a command');
  allowCycle = false;
  assert.equal((await power({ ...config, protocol: 'ipmi' })).actions.includes('cycle'), false);
  await assert.rejects(power({ ...config, protocol: 'ipmi' }, 'cycle'), /unavailable/);
  actionInfo = true;
  assert.deepEqual((await power({ ...config, protocol: 'ipmi' })).actions, ['on', 'shutdown', 'restart', 'off']);
  assert.deepEqual(await power({ ...config, protocol: 'ipmi' }, 'restart'), { accepted: true });
  systemPath = 'https://other.example/redfish/v1/Systems/Other';
  await assert.rejects(power({ ...config, protocol: 'ipmi' }), /Expected one/);
});

test('AMT power-off pauses every local KVM without revoking the power session', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'power-pause-test-'));
  const streams = [];
  const dial = () => { const stream = new Duplex({ read() {}, write(_d, _e, callback) { callback(); } }); streams.push(stream); return stream; };
  let sent = 0;
  const manage = async (_config, action) => {
    if (action) { assert.ok(streams.every(stream => stream.destroyed), 'KVM closes before the power command'); sent++; }
    return action ? { accepted: true } : { state: 'off', actions: ['on'] };
  };
  const apps = [createConsole({ name: 'ms-01' }, dial, directory, manage), createConsole([{ name: 'ms-01' }], dial, directory, manage)];
  t.after(() => { apps.forEach(app => app.close()); rmSync(directory, { recursive: true, force: true }); });
  const viewers = [];
  for (const [i, app] of apps.entries()) {
    const launch = await app.listen(), origin = new URL(launch).origin;
    const redirect = await fetch(launch, { redirect: 'manual' });
    const headers = { Cookie: redirect.headers.get('set-cookie').split(';')[0], Origin: origin };
    const path = i ? '/console/ms-01/' : '/';
    const ws = new WebSocket(`${origin.replace('http', 'ws')}${path}kvm`, { headers });
    await once(ws, 'open'); viewers.push({ origin, headers, path, ws });
  }
  const closed = viewers.map(viewer => once(viewer.ws, 'close'));
  const viewer = viewers[0];
  assert.equal((await fetch(`${viewer.origin}/power/off`, { method: 'POST', headers: viewer.headers })).status, 200);
  for (const [code] of await Promise.all(closed)) assert.equal(code, 4002);
  assert.equal(sent, 1);
  for (const viewer of viewers) {
    assert.equal((await fetch(`${viewer.origin}${viewer.path}power`, { headers: viewer.headers })).status, 200, 'power controls remain authorized');
    const retry = new WebSocket(`${viewer.origin.replace('http', 'ws')}${viewer.path}kvm`, { headers: viewer.headers });
    const close = once(retry, 'close'); await once(retry, 'open'); assert.equal((await close)[0], 4002, 'KVM cannot reclaim during a power transition');
  }
});

for (const multiple of [false, true]) test(`power routes enforce host selection, origin, cookie, release and duplicate protection (${multiple ? 'workspace' : 'single'})`, async t => {
  t.mock.timers.enable({ apis: ['Date'] });
  const directory = mkdtempSync(join(tmpdir(), 'power-route-test-'));
  let calls = [], fail = false;
  const configs = [{ name: 'ms-01' }, { name: 'nexus', protocol: 'ipmi', certificate: readFileSync(new URL('../ansible/inventory/group_vars/ipmi/certificates/nexus.pem', import.meta.url), 'utf8') }];
  const app = createConsole(multiple ? configs : configs[0], undefined, directory, async (config, action) => {
    calls.push([config.name, action]);
    if (fail) throw new Error('never expose credentials');
    return action ? { accepted: true } : { state: 'on', actions: Object.keys(powerActions) };
  });
  t.after(() => { app.close(); rmSync(directory, { recursive: true, force: true }); });
  const launch = await app.listen(), origin = new URL(launch).origin;
  const redirect = await fetch(launch, { redirect: 'manual' });
  const headers = { Cookie: redirect.headers.get('set-cookie').split(';')[0], Origin: origin };
  for (const config of multiple ? configs : [configs[0]]) {
    const path = `${origin}${multiple ? `/console/${config.name}/` : '/'}power`;
    assert.equal((await fetch(path)).status, 403);
    assert.deepEqual(await (await fetch(path, { headers })).json(), { state: 'on', actions: Object.keys(powerActions) });
    for (const auth of [{}, { Cookie: headers.Cookie }, { ...headers, Origin: 'https://evil.example' }, { Origin: origin }]) {
      assert.equal((await fetch(`${path}/restart`, { method: 'POST', headers: auth })).status, 403);
    }
    assert.equal((await fetch(`${path}/restart`, { headers })).status, 405);
    assert.equal((await fetch(path, { method: 'POST', headers })).status, 405);
    assert.equal((await fetch(`${path}/erase`, { method: 'POST', headers })).status, 400);
    assert.equal(calls.filter(([, action]) => action).length, 0);
    assert.equal((await fetch(`${path}/restart`, { method: 'POST', headers })).status, 200);
    assert.deepEqual(calls.at(-1), [config.name, 'restart']);
    assert.equal((await fetch(`${path}/off`, { method: 'POST', headers })).status, 409);
    t.mock.timers.tick(10001); fail = true;
    const failure = await fetch(`${path}/off`, { method: 'POST', headers });
    assert.equal(failure.status, 502); assert.doesNotMatch(await failure.text(), /never expose/);
    assert.equal((await fetch(`${path}/off`, { method: 'POST', headers })).status, 409, 'uncertain commands are not retried');
    fail = false; calls = [];
  }
  assert.equal((await fetch(`${origin}/console/unknown/power`, { headers })).status, 404);
  await fetch(`${origin}/release/ms-01`, { method: 'POST', headers: { Authorization: `Bearer ${new URL(launch).searchParams.get('token')}` } });
  assert.equal((await fetch(`${origin}${multiple ? '/console/ms-01/' : '/'}power/on`, { method: 'POST', headers })).status, 410);
});
