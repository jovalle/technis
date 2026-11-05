// The firmware viewer runs locally; only the relay owns the BMC login and CSRF token.
(async () => {
  const prefix = location.pathname.replace(/\/viewer\.html$/, '');
  const generation = new URL(location.href).searchParams.get('generation');
  const route = value => {
    const url = new URL(value, location.href);
    if (url.host !== location.host) throw new Error('Unexpected ASMB10 destination');
    if (!url.pathname.startsWith(`${prefix}/`)) url.pathname = prefix + url.pathname;
    return url.href;
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...args) { return open.call(this, method, route(url), ...args); };
  const Socket = WebSocket;
  window.WebSocket = class extends Socket {
    constructor(url, protocols) {
      super(route(url), protocols);
      this.addEventListener('message', event => {
        if (typeof event.data !== 'string' || !event.data.startsWith('{')) return;
        try {
          const reply = JSON.parse(event.data);
          if (reply.error) { event.stopImmediatePropagation(); parent.postMessage({ type: 'asmb-error', error: reply.error, generation }, location.origin); }
        } catch {}
      });
      this.addEventListener('close', event => parent.postMessage({ type: 'asmb-close', code: event.code, generation }, location.origin));
    }
  };
  const values = { garc: 'local-relay', privilege_id: 4, extended_privilege: 1, session_id: 0, username: 'Console', privilege: 'Administrator', id: 0, since: Math.floor(Date.now() / 1000), kvm_access: 1, vmedia_access: 0 };
  for (const [key, value] of Object.entries(values)) sessionStorage.setItem(key, value);
  document.cookie = `asmb_viewer=1; SameSite=Strict; Path=${prefix}/`;
  const response = await fetch(`${prefix}/api/configuration/project`);
  if (!response.ok) throw new Error('Cannot load ASMB10 capabilities');
  sessionStorage.setItem('features', JSON.stringify(await response.json()));
  for (const key of ['reloaded', 'status', 'stop_reason']) sessionStorage.removeItem(key);
  const script = document.createElement('script');
  script.src = `${prefix}/viewer.min.js?v=${Date.now()}`;
  script.dataset.main = '/app/main';
  document.body.append(script);
})().catch(() => parent.postMessage({ type: 'asmb-error', error: 'Cannot initialize ASMB10 viewer. Check LAN/VPN and the BMC firmware.', generation: new URL(location.href).searchParams.get('generation') }, location.origin));
