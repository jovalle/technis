import https from 'node:https';
import tls from 'node:tls';
import { X509Certificate } from 'node:crypto';
import { WebSocket } from 'ws';

export function fingerbotSettings(config) {
  const duration = config.fingerbot_press_time, strength = config.fingerbot_strength;
  if (!Number.isInteger(duration) || duration < 100 || duration > 60000 || ![1, 2].includes(strength)) {
    throw new Error('Invalid GLKVM Fingerbot calibration');
  }
  return { duration, strength };
}

// GLKVM's factory certificate can be dated 1970. Authenticate its exact enrolled
// DER before giving HTTPS a socket, including on resumed sessions.
function pinnedAgent(config) {
  const pin = new X509Certificate(config.certificate);
  const agent = new https.Agent({ keepAlive: true, maxCachedSessions: 0 });
  agent.createConnection = (options, callback) => {
    const socket = tls.connect({ ...options, rejectUnauthorized: false, minVersion: 'TLSv1.2' });
    const fail = error => { socket.destroy(); callback(error); };
    socket.setTimeout(15000, () => socket.destroy(new Error('GLKVM TLS timed out')));
    socket.once('error', fail);
    socket.once('secureConnect', () => {
      socket.removeListener('error', fail);
      if (!socket.getPeerCertificate().raw?.equals(pin.raw)) {
        socket.destroy(); callback(new Error('GLKVM certificate does not match the enrolled pin'));
      } else callback(null, socket);
    });
  };
  return agent;
}

export function createGlkvm(config) {
  const agent = pinnedAgent(config);
  let cookie = '', session, closed = false;
  function request(path, method = 'GET', body, timeout = 15000) {
    return new Promise((resolve, reject) => {
      const req = https.request({ host: config.host, port: config.port ?? 443, agent, path, method, headers: {
        ...(cookie ? { Cookie: cookie } : {}),
        ...(body ? { 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': Buffer.byteLength(body) } : {}),
      } }, resolve);
      req.setTimeout(timeout, () => req.destroy(new Error('GLKVM request timed out')));
      req.on('error', reject); req.end(body);
    });
  }
  async function json(path, method, body, timeout) {
    const response = await request(path, method, body, timeout);
    let text = '', size = 0;
    for await (const chunk of response) {
      size += chunk.length;
      if (size > 1024 * 1024) { response.destroy(); throw new Error('GLKVM response too large'); }
      text += chunk;
    }
    if ([401, 403].includes(response.statusCode)) { session = undefined; cookie = ''; }
    if (response.statusCode !== 200) throw new Error('GLKVM request rejected');
    const value = JSON.parse(text);
    if (value.ok !== true) throw new Error('GLKVM request rejected');
    return { result: value.result, cookies: response.headers['set-cookie'] };
  }
  async function login() {
    if (closed) throw new Error('GLKVM session closed');
    session ??= json('/api/auth/login', 'POST', new URLSearchParams({ user: config.username, passwd: config.password }).toString())
      .then(value => {
        cookie = (value.cookies ?? []).map(item => item.split(';')[0]).join('; ');
        if (!cookie) throw new Error('GLKVM login requires a session cookie');
      }).catch(error => { session = undefined; throw error; });
    await session;
    if (closed) throw new Error('GLKVM session closed');
  }
  const self = {
    async status(path) { await login(); return (await json(path)).result; },
    async open(path) { await login(); return request(path); },
    async socket(media = false) {
      await login();
      return new WebSocket(`wss://${config.host}:${config.port ?? 443}/api/${media ? 'media/ws' : 'ws?stream=true'}`, {
        agent, headers: { Cookie: cookie, Origin: `https://${config.host}` },
        handshakeTimeout: 15000, maxPayload: 16 * 1024 * 1024, perMessageDeflate: false,
      });
    },
    async improveQuality() {
      const { params, limits } = await self.status('/api/streamer');
      const updates = new URLSearchParams();
      const bitrate = Math.min(8000, limits?.h264_bitrate?.max ?? 8000);
      if (params.h264_bitrate > 0 && params.h264_bitrate < bitrate) updates.set('h264_bitrate', bitrate);
      if (params.quality < 95) updates.set('quality', 95);
      if (updates.size) await json(`/api/streamer/set_params?${updates}`, 'POST');
    },
    async fingerbot(press = false, expected) {
      const present = await self.status('/api/fingerbot/exist');
      if (present.exist !== true) {
        if (press) throw new Error('Fingerbot is unavailable');
        return { enabled: false };
      }
      const settings = fingerbotSettings((await self.status('/api/system/get_config')).config);
      if (press) {
        if (settings.duration !== expected?.duration || settings.strength !== expected?.strength) throw new Error('Fingerbot calibration changed; refresh before pressing');
        await json(`/api/fingerbot/click?press_time=${settings.duration}&angle_enum=${settings.strength}`, 'GET', undefined, settings.duration + 10000);
        return { accepted: true };
      }
      return { enabled: true, ...settings };
    },
    async bridge(local, current) {
      const remotes = [];
      const stop = () => { remotes.forEach(socket => socket.terminate()); };
      current.remote = { destroy: stop };
      local.on('error', stop); local.once('close', stop);
      try {
        await self.improveQuality();
        for (const media of [false, true]) {
          const remote = await self.socket(media);
          remotes.push(remote);
          remote.on('error', () => local.close(1011, 'GLKVM connection failed'));
          remote.once('close', () => local.close(1011, 'GLKVM disconnected'));
          if (local.readyState !== WebSocket.OPEN) { stop(); return; }
          remote.on('message', (data, binary) => {
            if (local.readyState !== WebSocket.OPEN) return;
            if (local.bufferedAmount > 16 * 1024 * 1024) { local.close(1013, 'GLKVM video queue full'); return; }
            if (!binary) {
              try {
                const { event_type } = JSON.parse(data);
                if (!['loop', 'streamer', 'hid', 'fingerbot', 'media'].includes(event_type)) return;
              } catch { return; }
            } else if (!media) return;
            local.send(data, { binary });
          });
        }
        local.on('message', (data, binary) => {
          try {
            let destinations;
            if (binary) {
              if (data.length !== 1 || data[0] !== 0) throw new Error();
              destinations = remotes;
            } else {
              const value = JSON.parse(data);
              if (['key', 'mouse_button', 'mouse_move', 'mouse_wheel'].includes(value.event_type)) destinations = [remotes[0]];
              else if (value.event_type === 'start' && value.event?.type === 'video' && value.event?.format === 'h264') destinations = [remotes[1]];
              else throw new Error();
            }
            for (const remote of destinations) if (remote.readyState === WebSocket.OPEN) {
              if (remote.bufferedAmount > 65536) throw new Error();
              remote.send(data, { binary });
            }
          } catch { local.close(1003, 'Unsupported GLKVM input'); }
        });
        if (local.readyState === WebSocket.OPEN) await new Promise(resolve => local.once('close', resolve));
      } finally { stop(); }
    },
    close() {
      closed = true;
      if (cookie) json('/api/auth/logout', 'POST').catch(() => {}).finally(() => agent.destroy());
      else agent.destroy();
    },
  };
  return self;
}
