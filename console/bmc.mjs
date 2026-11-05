import https from 'node:https';
import { WebSocket } from 'ws';
import { tlsOptions } from './amt.mjs';
import { gunzipSync } from 'node:zlib';

const api = new Set(['/api/configuration/project', '/api/configuration/runtime', '/api/firmware-info', '/api/kvm/token', '/api/settings/media/adviser', '/api/settings/media/h5viewercfg', '/api/host_inventory/host_interface_system_info']);
export function bmcPath(path) {
  return api.has(path) || /^\/(viewer\.html|viewer\.min\.(js|css)|styles\.min\.css|app\/templates\/layouts\/main\.html)$/.test(path) || /^\/(images|fonts|locales|lang|libs)\/[a-zA-Z0-9_./-]+\.(png|jpg|gif|svg|ico|woff2?|ttf|eot|json|js|css)$/.test(path) && !path.includes('..');
}

export function createBmc(config) {
  let session, cookie = '', csrf = '', closed = false;
  const agent = new https.Agent({ ...tlsOptions(config), keepAlive: true });
  function request(path, method = 'GET', body) {
    return new Promise((resolve, reject) => {
      const req = https.request({ ...tlsOptions(config), agent, path, method, headers: {
        ...(cookie ? { Cookie: cookie, 'X-CSRFTOKEN': csrf } : {}),
        ...(body ? { 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': Buffer.byteLength(body) } : {}),
      } }, res => {
        const chunks = []; let size = 0;
        res.on('data', chunk => {
          size += chunk.length;
          if (size > 16 * 1024 * 1024) res.destroy(new Error('ASMB10 response too large'));
          else chunks.push(chunk);
        });
        res.on('error', reject);
        res.on('end', () => {
          try {
            let data = Buffer.concat(chunks);
            if (res.headers['content-encoding'] === 'gzip') data = gunzipSync(data, { maxOutputLength: 16 * 1024 * 1024 });
            resolve({ status: res.statusCode, type: res.headers['content-type'] ?? 'application/octet-stream', cookies: res.headers['set-cookie'] ?? [], body: data });
          } catch { reject(new Error('Invalid ASMB10 compressed response')); }
        });
      });
      req.setTimeout(15000, () => req.destroy(new Error('ASMB10 request timed out')));
      req.on('error', reject);
      req.end(body);
    });
  }
  async function login() {
    if (closed) throw new Error('ASMB10 session closed');
    if (!session) session = (async () => {
      const result = await request('/api/session', 'POST', new URLSearchParams({ username: config.username, password: config.password }).toString());
      let data;
      try { data = JSON.parse(result.body); } catch { throw new Error('Invalid ASMB10 login response'); }
      if (result.status !== 200 || data.ok !== 0 || !data.CSRFToken || !(Number(data.extendedpriv) & 1) || !result.cookies.length) throw new Error('ASMB10 login rejected or KVM access unavailable');
      cookie = result.cookies.map(value => value.split(';')[0]).join('; ');
      csrf = data.CSRFToken;
    })().catch(error => { session = undefined; throw error; });
    await session;
    if (closed) { await logout(); throw new Error('ASMB10 session closed'); }
  }
  async function logout() {
    if (cookie) {
      try { await request('/api/session', 'DELETE'); } catch {}
      cookie = ''; csrf = ''; session = undefined;
    }
  }
  return {
    async get(path) {
      if (!bmcPath(new URL(path, 'https://bmc').pathname)) throw new Error('ASMB10 path unavailable');
      await login();
      const result = await request(path);
      if ([401, 403].includes(result.status)) { await logout(); throw new Error('ASMB10 session expired; reconnect'); }
      return result;
    },
    async bridge(local, current) {
      let remote, stopping;
      local.on('error', () => local.terminate());
      function stopRemote() {
        if (stopping) return stopping;
        if (!remote || remote.readyState === WebSocket.CLOSED) return Promise.resolve();
        stopping = new Promise(resolve => {
          const timer = setTimeout(() => { remote.terminate(); resolve(); }, 1000);
          timer.unref();
          remote.once('close', () => { clearTimeout(timer); resolve(); });
          if (remote.readyState === WebSocket.OPEN) {
            // AMI command 8 releases KVM; a dropped socket reserves master control for reconnect.
            remote.send(Buffer.from([8, 0, 0, 0, 0, 0, 0, 0]), { binary: true }, () => remote.close());
          } else remote.terminate();
        });
        return stopping;
      }
      const pending = [];
      const incoming = (data, binary) => {
        if (!binary) { local.close(1003, 'Expected console data'); return; }
        if (remote?.readyState === WebSocket.OPEN) {
          if (remote.bufferedAmount > 1024 * 1024) { local.close(1009, 'Console input queue full'); return; }
          remote.send(data, { binary });
        }
        else if (pending.length < 16) pending.push(data);
        else local.close(1009, 'Too much pending console data');
      };
      local.on('message', incoming);
      try {
        await login();
        if (local.readyState !== WebSocket.OPEN) return;
        remote = new WebSocket(`wss://${config.host}:${config.port ?? 443}/kvm`, ['binary', 'base64'], {
          ...tlsOptions(config), headers: { Cookie: cookie, Origin: `https://${config.host}`, 'X-CSRFTOKEN': csrf },
          handshakeTimeout: 15000, maxPayload: 16 * 1024 * 1024, perMessageDeflate: false,
        });
        current.remote = { destroy: stopRemote };
        await new Promise(resolve => {
          local.once('close', resolve);
          remote.on('open', () => { for (const data of pending) remote.send(data, { binary: true }); pending.length = 0; });
          remote.on('message', (data, binary) => {
            if (local.bufferedAmount > 32 * 1024 * 1024) { local.close(1013, 'Console video queue full'); return; }
            if (local.readyState === WebSocket.OPEN) local.send(data, { binary });
          });
          remote.once('error', () => { if (local.readyState === WebSocket.OPEN) local.close(1011, 'ASMB10 connection failed'); resolve(); });
          remote.once('close', () => { if (local.readyState === WebSocket.OPEN) local.close(1000, 'ASMB10 disconnected'); resolve(); });
        });
      } finally { local.off('message', incoming); await stopRemote(); await logout(); }
    },
    close() { closed = true; logout().finally(() => agent.destroy()); },
  };
}
