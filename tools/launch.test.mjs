import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startServer } from './launch.mjs';

test('background launcher waits for HTTP readiness, reuses a live server and rejects failed startup', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'technis-launch-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const script = join(directory, 'server.mjs'), record = join(directory, 'server.json');
  writeFileSync(script, `import http from 'node:http';
const server = http.createServer((req,res) => res.end('ready'));
server.listen(0,'127.0.0.1',() => process.send({url:'http://127.0.0.1:'+server.address().port}));`);
  const started = await startServer(script, [], record);
  t.after(() => { try { process.kill(started.pid); } catch {} });
  assert.equal(await (await fetch(started.url)).text(), 'ready');
  assert.deepEqual(await startServer(script, [], record), started);
  assert.equal(statSync(record).mode & 0o777, 0o600);
  assert.equal(statSync(`${record}.log`).mode & 0o777, 0o600);
  const broken = join(directory, 'broken.mjs');
  writeFileSync(broken, 'process.exit(1)');
  await assert.rejects(startServer(broken, [], join(directory, 'broken.json')), /failed to start/);
  writeFileSync(broken, "process.send({error:'Set real AMT credentials'}); setInterval(()=>{},1000);");
  await assert.rejects(startServer(broken, [], join(directory, 'rejected.json')), /Set real AMT credentials/);
});
