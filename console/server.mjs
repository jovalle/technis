import http from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, resolve } from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { tmpdir } from "node:os";
import { WebSocketServer, WebSocket } from "ws";
import { authenticate, connect } from "./amt.mjs";
import { registerSession, releaseSessions } from "./sessions.mjs";
import { killConsoles } from "./kill.mjs";
import { createBmc, bmcPath } from "./bmc.mjs";
import { createGlkvm } from "./glkvm.mjs";
import { power, powerActions } from "./power.mjs";

const directory = dirname(fileURLToPath(import.meta.url));
const root = resolve(directory, "..");
const assets = new Map([
  ["/", ["index.html", "text/html"]],
  ["/client.mjs", ["client.mjs", "text/javascript"]],
  ["/power-client.mjs", ["power-client.mjs", "text/javascript"]],
  ["/asmb-client.mjs", ["asmb-client.mjs", "text/javascript"]],
  ["/glkvm-client.mjs", ["glkvm-client.mjs", "text/javascript"]],
  ["/asmb-bootstrap.mjs", ["asmb-bootstrap.mjs", "text/javascript"]],
  ["/asmb-viewer.css", ["asmb-viewer.css", "text/css"]],
  ["/input.mjs", ["input.mjs", "text/javascript"]],
  ["/console.css", ["console.css", "text/css"]],
  ["/workspace.mjs", ["workspace.mjs", "text/javascript"]],
  ["/logo.svg", ["../assets/logo.svg", "image/svg+xml"]],
  ["/favicon.ico", ["../assets/favicon.ico", "image/x-icon"]],
  ["/vendor/LUCIDE-LICENSE", ["vendor/LUCIDE-LICENSE", "text/plain"]],
  [
    "/vendor/amt-desktop-0.0.2.js",
    ["vendor/amt-desktop-0.0.2.js", "text/javascript"],
  ],
  ["/vendor/zlib-inflate.js", ["vendor/zlib-inflate.js", "text/javascript"]],
  ["/vendor/zlib.js", ["vendor/zlib.js", "text/javascript"]],
  ["/vendor/zlib-adler32.js", ["vendor/zlib-adler32.js", "text/javascript"]],
  ["/vendor/zlib-crc32.js", ["vendor/zlib-crc32.js", "text/javascript"]],
]);

export function loadInventory() {
  try {
    return JSON.parse(
      execFileSync("ansible-inventory", ["--list"], {
        cwd: root,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
        env: {
          ...process.env,
          ANSIBLE_LOCAL_TEMP: process.env.ANSIBLE_LOCAL_TEMP ?? tmpdir(),
        },
      })
    );
  } catch {
    throw new Error(
      "Cannot read Ansible inventory; check ansible-inventory --list"
    );
  }
}

export function amtHosts(inventory, group = "amt") {
  function hosts(group, visited = new Set()) {
    if (visited.has(group)) return [];
    visited.add(group);
    const value = inventory[group] ?? {};
    return [
      ...(value.hosts ?? []),
      ...(value.children ?? []).flatMap(child => hosts(child, visited)),
    ];
  }
  return [...new Set(hosts(group))].sort();
}

export function consoleHosts(inventory) {
  return [
    ...new Set(
      ["amt", "ipmi", "glkvm"].flatMap(group => amtHosts(inventory, group))
    ),
  ].sort();
}

export function selectHosts(inventory, name) {
  const hosts = consoleHosts(inventory);
  if (
    name !== undefined &&
    (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(name) || !hosts.includes(name))
  ) {
    throw new Error(
      `Host ${name} is not in the Ansible amt, ipmi or glkvm group`
    );
  }
  if (!hosts.length)
    throw new Error("No hosts found in the Ansible amt, ipmi or glkvm group");
  return name === undefined ? hosts : [name];
}

export function loadConfig(name, inventory = loadInventory()) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(name ?? ""))
    throw new Error("Usage: just open console [HOST]");
  if (!consoleHosts(inventory).includes(name))
    throw new Error(
      `Host ${name} is not in the Ansible amt, ipmi or glkvm group`
    );
  const vars = inventory._meta.hostvars[name];
  const protocol =
    ["glkvm", "ipmi"].find(group =>
      amtHosts(inventory, group).includes(name)
    ) ?? "amt";
  if (vars.amt_tls === false || vars.amt_validate_certs === false)
    throw new Error("The console requires pinned AMT TLS");
  const certificate = readFileSync(
    resolve(
      root,
      `ansible/inventory/group_vars/${protocol}/certificates`,
      `${name}.pem`
    ),
    "utf8"
  );
  let credentials;
  try {
    credentials = JSON.parse(
      execFileSync(
        "python3",
        [resolve(root, "tools/vault.py"), "section", `${protocol}/${name}`],
        { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }
      )
    );
  } catch {
    throw new Error(
      `Cannot read ${protocol}/${name} from Infisical; check access and the folder`
    );
  }
  if (
    typeof credentials.username !== "string" ||
    typeof credentials.password !== "string" ||
    !credentials.username ||
    !credentials.password ||
    credentials.password === "CHANGEME"
  )
    throw new Error(
      `Set real console credentials with just vault ${protocol}/${name}/username and .../password`
    );
  return {
    name,
    protocol,
    host:
      protocol === "amt"
        ? (vars.amt_host ?? vars.ansible_host ?? name)
        : (vars[`${protocol}_host`] ?? name),
    port:
      protocol === "amt"
        ? (vars.amt_redir_port ?? 16995)
        : (vars[`${protocol}_port`] ?? 443),
    powerPort: vars.amt_port ?? 16993,
    legacy: vars.amt_legacy_server_connect === true,
    certificate,
    username: credentials.username,
    password: credentials.password,
  };
}

export function createConsole(
  config,
  dial = connect,
  registryDirectory,
  managePower = power
) {
  const multiple = Array.isArray(config);
  const configs = multiple ? config : [config];
  const pages = new Map(assets),
    sessions = new Map(),
    targets = new Map(),
    active = new Map(),
    released = new Set();
  const powerTargets = new Map(),
    powerBusy = new Map(),
    fingerbotBusy = new Map();
  const bmcs = new Map(
    configs
      .filter(value => value.protocol === "ipmi")
      .map(value => [value.name, createBmc(value)])
  );
  const glkvms = new Map(
    configs
      .filter(value => value.protocol === "glkvm")
      .map(value => [value.name, createGlkvm(value)])
  );
  if (multiple) {
    pages.set("/", ["workspace.html", "text/html"]);
    sessions.set("/session", { hosts: configs.map(value => value.name) });
  }
  for (const value of configs) {
    const base = multiple ? `/console/${value.name}/` : "/";
    pages.set(base, assets.get("/"));
    sessions.set(
      `${base}session`,
      value.protocol && value.protocol !== "amt"
        ? { host: value.name, protocol: value.protocol }
        : { host: value.name }
    );
    targets.set(`${base}kvm`, value);
    if (value.protocol !== "glkvm") powerTargets.set(`${base}power`, value);
  }
  const token = randomBytes(32).toString("hex");
  const cookie = `technis_console_${token.slice(0, 12)}=${token}`;
  let origin, unregister;
  const releaseMessage =
    "Session released by another local console. Run just open console again to reconnect.";
  function stopViewer(ws, powerPause = false) {
    ws.send(
      JSON.stringify({
        error: powerPause
          ? "Console paused for a power action. Choose Reconnect after the host returns."
          : releaseMessage,
        retryable: false,
      })
    );
    ws.close(
      powerPause ? 4002 : 4001,
      powerPause ? "Power action" : "Session released"
    );
  }
  function release(host, powerPause = false) {
    if (released.has(host)) return false;
    if (!powerPause) released.add(host);
    else if (powerBusy.get(host) !== Infinity)
      powerBusy.set(host, Date.now() + 10000);
    const current = active.get(host);
    if (current) {
      stopViewer(current.ws, powerPause);
      current.remote?.destroy();
      active.delete(host);
    }
    return true;
  }
  const server = http.createServer(async (req, res) => {
    const headers = {
      "Cache-Control": "no-store",
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options":
        multiple && urlIsConsole(req.url) ? "SAMEORIGIN" : "DENY",
      "Content-Security-Policy": `default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-src 'self'; frame-ancestors ${multiple && urlIsConsole(req.url) ? "'self'" : "'none'"}; base-uri 'none'; form-action 'none'`,
    };
    const url = new URL(req.url, "http://localhost");
    if (req.headers.host !== new URL(origin).host) {
      res.writeHead(403, headers).end();
      return;
    }
    if (req.method === "POST" && url.pathname.startsWith("/release/")) {
      if (
        req.headers.authorization !== `Bearer ${token}` ||
        (req.headers.origin && req.headers.origin !== origin)
      ) {
        res.writeHead(403, headers).end();
        return;
      }
      const host = url.pathname.slice("/release/".length);
      if (!configs.some(value => value.name === host)) {
        res.writeHead(404, headers).end();
        return;
      }
      res
        .writeHead(200, { ...headers, "Content-Type": "application/json" })
        .end(
          JSON.stringify({
            released: release(host, url.searchParams.get("power") === "1"),
          })
        );
      return;
    }
    const fingerbotPath = /^(.*\/)fingerbot(?:\/(press))?$/.exec(url.pathname);
    const glTarget = targets.get(
      fingerbotPath
        ? `${fingerbotPath[1]}kvm`
        : url.pathname.replace(/\/snapshot$/, "/kvm")
    );
    const glkvm = glkvms.get(glTarget?.name);
    if (glkvm && (fingerbotPath || url.pathname.endsWith("/snapshot"))) {
      if (
        !authorized(req) ||
        (req.method === "POST" && req.headers.origin !== origin)
      ) {
        res.writeHead(403, headers).end();
        return;
      }
      if (released.has(glTarget.name)) {
        res.writeHead(410, headers).end();
        return;
      }
      const press = fingerbotPath?.[2] === "press";
      if (req.method !== (press ? "POST" : "GET")) {
        res.writeHead(405, headers).end();
        return;
      }
      if (press && (fingerbotBusy.get(glTarget.name) ?? 0) > Date.now()) {
        res.writeHead(409, headers).end();
        return;
      }
      if (press) fingerbotBusy.set(glTarget.name, Infinity);
      try {
        if (fingerbotPath) {
          const result = await glkvm.fingerbot(press, {
            duration: Number(url.searchParams.get("duration")),
            strength: Number(url.searchParams.get("strength")),
          });
          res
            .writeHead(200, { ...headers, "Content-Type": "application/json" })
            .end(JSON.stringify(result));
        } else {
          const image = await glkvm.open("/api/streamer/snapshot");
          if (
            image.statusCode !== 200 ||
            !image.headers["content-type"]?.startsWith("image/jpeg")
          ) {
            image.destroy();
            throw new Error("No GLKVM frame");
          }
          let size = 0;
          image.on("data", chunk => {
            size += chunk.length;
            if (size > 8 * 1024 * 1024)
              image.destroy(new Error("Frame too large"));
          });
          image.on("error", () => res.destroy());
          res.once("close", () => image.destroy());
          res.writeHead(200, { ...headers, "Content-Type": "image/jpeg" });
          image.pipe(res);
        }
      } catch {
        res
          .writeHead(502, headers)
          .end(
            "GLKVM request failed; check LAN/VPN, credentials and certificate pin."
          );
      } finally {
        if (press) fingerbotBusy.set(glTarget.name, Date.now() + 10000);
      }
      return;
    }
    const powerPath = /^(.*\/power)(?:\/([a-z]+))?$/.exec(url.pathname);
    const powerTarget = powerTargets.get(powerPath?.[1]);
    if (powerTarget) {
      if (
        !authorized(req) ||
        (req.method === "POST" && req.headers.origin !== origin)
      ) {
        res.writeHead(403, headers).end();
        return;
      }
      if (released.has(powerTarget.name)) {
        res.writeHead(410, headers).end("Session released");
        return;
      }
      const action = powerPath[2];
      if (action !== undefined && !Object.hasOwn(powerActions, action)) {
        res.writeHead(400, headers).end();
        return;
      }
      if (
        (action === undefined && req.method !== "GET") ||
        (action !== undefined && req.method !== "POST")
      ) {
        res.writeHead(405, headers).end();
        return;
      }
      if (action && (powerBusy.get(powerTarget.name) ?? 0) > Date.now()) {
        res
          .writeHead(409, headers)
          .end(
            "A power request is already in progress. Wait, then refresh status."
          );
        return;
      }
      if (action) powerBusy.set(powerTarget.name, Infinity);
      try {
        if (
          (powerTarget.protocol ?? "amt") === "amt" &&
          ["off", "cycle", "shutdown"].includes(action)
        ) {
          // AMT rejects power-off and cycle while any local KVM redirection is active.
          await releaseSessions(powerTarget.name, registryDirectory, true);
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
        const result = await managePower(powerTarget, action);
        res
          .writeHead(200, { ...headers, "Content-Type": "application/json" })
          .end(JSON.stringify(result));
      } catch {
        res
          .writeHead(502, { ...headers, "Content-Type": "application/json" })
          .end(
            JSON.stringify({
              error: action
                ? "Power request failed; it may have reached the host. Refresh status before trying again."
                : "Power status unavailable. Check LAN/VPN, credentials and certificate pin.",
            })
          );
      } finally {
        if (action) powerBusy.set(powerTarget.name, Date.now() + 10000);
      }
      return;
    }
    if (req.method !== "GET") {
      res.writeHead(403, headers).end();
      return;
    }
    if (url.pathname === "/launch" && url.searchParams.get("token") === token) {
      released.clear();
      res
        .writeHead(303, {
          ...headers,
          Location: "/",
          "Set-Cookie": `${cookie}; HttpOnly; SameSite=Strict; Path=/`,
        })
        .end();
      return;
    }
    if (!authorized(req)) {
      res
        .writeHead(403, headers)
        .end("Open the launch URL printed by just open console.");
      return;
    }
    if (sessions.has(url.pathname)) {
      res
        .writeHead(200, { ...headers, "Content-Type": "application/json" })
        .end(JSON.stringify(sessions.get(url.pathname)));
      return;
    }
    const bmcMatch = /^\/asmb\/([a-zA-Z0-9_.-]+)(\/.*)$/.exec(url.pathname);
    if (bmcMatch && bmcs.has(bmcMatch[1])) {
      if (released.has(bmcMatch[1])) {
        res.writeHead(410, headers).end("Session released");
        return;
      }
      const bmc = bmcs.get(bmcMatch[1]),
        path = bmcMatch[2];
      if (!bmcPath(path)) {
        res.writeHead(404, headers).end();
        return;
      }
      try {
        const result = await bmc.get(path + url.search);
        let body = result.body;
        const prefix = `/asmb/${bmcMatch[1]}`;
        if (path === "/viewer.html") {
          body = Buffer.from(
            body
              .toString()
              .replace(
                "<head>",
                `<head><script type="module" src="/asmb-bootstrap.mjs?v=${Date.now()}"></script>`
              )
              .replace(/<script data-main=[^>]+><\/script>/, "")
              .replaceAll('href="/', `href="${prefix}/`)
              .replace(
                "</head>",
                '<link rel="stylesheet" href="/asmb-viewer.css"></head>'
              )
          );
        }
        if (path === "/viewer.min.js") {
          // AMI only uses this input for key events; password managers must not treat it as a login.
          body = Buffer.from(
            body
              .toString()
              .replace(
                /(<input id=(\\+)'kvm_textbox\2' )name=\2'password\2'/,
                (_, start, quote) =>
                  `${start}name=${quote}'hardware-console-input${quote}' type="button" aria-label="Remote console keyboard capture"`
              )
          );
        }
        if (result.type.includes("text/css")) {
          body = Buffer.from(
            body
              .toString()
              .replace(/url\((['"]?)([^)'"\s]+)\1\)/g, (match, quote, value) =>
                value.startsWith("data:")
                  ? match
                  : `url("${prefix}${new URL(value, `https://bmc${path}`).pathname}")`
              )
          );
        }
        res
          .writeHead(result.status, {
            ...headers,
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy":
              "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; frame-ancestors 'self'",
            "Content-Type": result.type,
          })
          .end(body);
      } catch {
        res
          .writeHead(502, headers)
          .end("Cannot reach ASMB10; check credentials and certificate pin.");
      }
      return;
    }
    const asset = pages.get(url.pathname);
    if (!asset) {
      res.writeHead(404, headers).end();
      return;
    }
    let body = readFileSync(resolve(directory, asset[0]));
    const target = configs.find(
      value => url.pathname === (multiple ? `/console/${value.name}/` : "/")
    );
    if (target?.protocol === "ipmi" && !(multiple && url.pathname === "/")) {
      body = Buffer.from(
        body
          .toString()
          .replace(/    <script src="\/vendor\/[^\n]+\n/g, "")
          .replace("/client.mjs", `/asmb-client.mjs?v=${Date.now()}`)
          .replace(
            /<canvas[^>]*><\/canvas>/,
            '<iframe id="viewer" title="ASMB10 hardware console" allowfullscreen></iframe>'
          )
          .replaceAll("Connecting to Intel AMT…", "Connecting to ASMB10…")
          .replace(
            '<div class="actions">',
            '<div class="actions"><button id="take-control" type="button" title="Request keyboard and mouse control" hidden>Request control</button>'
          )
      );
    }
    if (target?.protocol === "glkvm" && !(multiple && url.pathname === "/")) {
      body = Buffer.from(
        body
          .toString()
          .replace(/    <script src="\/vendor\/[^\n]+\n/g, "")
          .replace("/client.mjs", `/glkvm-client.mjs?v=${Date.now()}`)
          .replace(/    <script src="\/power-client.mjs"[^\n]+\n/, "")
          .replace(/<details id="power-controls">[\s\S]*?<\/details>/, "")
          .replaceAll("Connecting to Intel AMT…", "Connecting to GLKVM…")
          .replace(
            '<div class="actions">',
            '<div class="actions"><button id="fingerbot" type="button" hidden disabled>Fingerbot</button>'
          )
          .replace(
            '<span id="resolution">',
            '<span id="fingerbot-feedback" role="status" aria-live="polite"></span><span id="resolution">'
          )
      );
    }
    res
      .writeHead(200, {
        ...headers,
        "Content-Type": `${asset[1]}; charset=utf-8`,
      })
      .end(body);
  });
  function urlIsConsole(url) {
    return configs.some(value => url === `/console/${value.name}/`);
  }
  function authorized(req) {
    return (req.headers.cookie ?? "")
      .split(";")
      .some(value => value.trim() === cookie);
  }
  const wss = new WebSocketServer({
    noServer: true,
    maxPayload: 65536,
    perMessageDeflate: false,
  });
  server.on("upgrade", (req, socket, head) => {
    const asmb = /^\/asmb\/([a-zA-Z0-9_.-]+)\/kvm$/.exec(req.url);
    const config = asmb
      ? configs.find(
          value => value.name === asmb[1] && value.protocol === "ipmi"
        )
      : targets.get(req.url);
    if (
      req.headers.host !== new URL(origin).host ||
      req.headers.origin !== origin ||
      !config ||
      !authorized(req) ||
      active.has(config.name)
    ) {
      socket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
      return;
    }
    wss.handleUpgrade(req, socket, head, ws => {
      if (released.has(config.name)) {
        ws.on("error", () => {});
        stopViewer(ws);
        return;
      }
      if ((powerBusy.get(config.name) ?? 0) > Date.now()) {
        ws.on("error", () => {});
        stopViewer(ws, true);
        return;
      }
      const current = { ws };
      active.set(config.name, current);
      if (config.protocol === "glkvm") {
        glkvms
          .get(config.name)
          .bridge(ws, current)
          .catch(() => {
            if (ws.readyState === WebSocket.OPEN)
              ws.close(1011, "GLKVM connection failed");
          })
          .finally(() => {
            if (active.get(config.name) === current) active.delete(config.name);
          });
        return;
      }
      if (config.protocol === "ipmi") {
        bmcs
          .get(config.name)
          .bridge(ws, current)
          .catch(() => {
            if (ws.readyState === WebSocket.OPEN)
              ws.close(1011, "ASMB10 connection failed");
          })
          .finally(() => {
            if (active.get(config.name) === current) active.delete(config.name);
          });
        return;
      }
      let remote;
      try {
        remote = current.remote = dial(config);
      } catch {
        active.delete(config.name);
        ws.send(
          JSON.stringify({
            error:
              "Cannot initialize AMT TLS; check the certificate pin and redirection port.",
            retryable: false,
          })
        );
        ws.close(1011, "AMT configuration error");
        return;
      }
      let ready = false;
      const stop = () => {
        remote.destroy();
        ws.terminate();
      };
      ws.on("error", stop);
      ws.on("close", () => {
        remote.destroy();
        if (active.get(config.name) === current) active.delete(config.name);
      });
      ws.on("message", (data, binary) => {
        if (!binary || !ready) {
          ws.close(1003, "Expected console data");
          return;
        }
        if (!remote.write(data)) {
          ws.pause();
          remote.once("drain", () => ws.resume());
        }
      });
      remote.on("error", () => {}); // The async reader reports the same error to the browser below.
      (async () => {
        try {
          for await (const data of authenticate(remote, config)) {
            ready = true;
            if (ws.readyState !== WebSocket.OPEN) break;
            await new Promise((resolve, reject) =>
              ws.send(data, { binary: true }, err =>
                err ? reject(err) : resolve()
              )
            );
          }
          if (ws.readyState === WebSocket.OPEN)
            ws.close(1000, "AMT closed the session");
        } catch (error) {
          // Never log configuration or authentication frames.
          const message =
            error.code && !error.code.startsWith("AMT_")
              ? ready
                ? `AMT ended the session (${error.code}).`
                : `Cannot reach AMT (${error.code}); check LAN/VPN and certificate pin`
              : error.message;
          const retryable = [
            "ECONNRESET",
            "ECONNREFUSED",
            "ETIMEDOUT",
            "EHOSTUNREACH",
            "ENETUNREACH",
            "EPIPE",
            "EAI_AGAIN",
            "AMT_BUSY",
            "AMT_CLOSED",
          ].includes(error.code);
          if (ws.readyState === WebSocket.OPEN)
            ws.send(JSON.stringify({ error: message, retryable }));
          ws.close(1011, "AMT connection failed");
        } finally {
          remote.destroy();
        }
      })();
    });
  });
  return {
    server,
    async listen() {
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(0, "127.0.0.1", resolve);
      });
      origin = `http://127.0.0.1:${server.address().port}`;
      try {
        unregister = registerSession(
          { origin, token, hosts: configs.map(value => value.name) },
          registryDirectory
        );
      } catch (error) {
        this.close();
        throw error;
      }
      return `${origin}/launch?token=${token}`;
    },
    close() {
      unregister?.();
      for (const current of active.values()) current.remote?.destroy();
      for (const ws of wss.clients) {
        ws.close(1001, "Local console stopped");
        const timeout = setTimeout(() => ws.terminate(), 1000);
        timeout.unref();
        ws.once("close", () => clearTimeout(timeout));
      }
      for (const bmc of bmcs.values()) bmc.close();
      for (const glkvm of glkvms.values()) glkvm.close();
      wss.close();
      server.close();
      server.closeAllConnections();
    },
  };
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  try {
    if (process.argv[2] === "--kill") {
      if (process.argv.length > 4)
        throw new Error("Usage: just stop console [HOST]");
      const inventory = loadInventory();
      const hosts = selectHosts(inventory, process.argv[3]);
      const configs = hosts.map(host => loadConfig(host, inventory));
      await killConsoles(hosts, {
        reset: async host => {
          const config = configs.find(value => value.name === host);
          if (config.protocol === "ipmi") return "Local ASMB10";
          if (config.protocol === "glkvm") return "Local GLKVM";
          const { resetKvm } = await import("./kill.mjs");
          await resetKvm(config);
        },
      });
    } else {
      const names = process.argv.slice(2).filter(arg => arg !== "--no-open");
      if (names.length > 1 || names.some(name => name.startsWith("-")))
        throw new Error(
          "Usage: just open console [HOST] --foreground [--no-open]"
        );
      const inventory = loadInventory();
      const configs = (
        names.length
          ? selectHosts(inventory, names[0])
          : consoleHosts(inventory)
      ).map(name => loadConfig(name, inventory));
      if (!configs.length)
        throw new Error(
          "No hosts found in the Ansible amt, ipmi or glkvm group"
        );
      const app = createConsole(names.length ? configs[0] : configs);
      const url = await app.listen();
      process.send?.({ url });
      console.log(
        process.send
          ? `Console ready for ${configs.map(value => value.name).join(", ")}.`
          : `Console for ${configs.map(value => value.name).join(", ")}: ${url}\nKeep this command running. Ctrl-C stops the local console.`
      );
      if (!process.argv.includes("--no-open")) {
        const opener = spawn(
          process.platform === "darwin"
            ? "open"
            : process.platform === "win32"
              ? "explorer.exe"
              : "xdg-open",
          [url],
          { stdio: "ignore" }
        );
        opener.on("error", () =>
          console.error("Open the launch URL above in your browser.")
        );
        opener.on("exit", code => {
          if (code) console.error("Open the launch URL above in your browser.");
        });
      }
      for (const signal of ["SIGINT", "SIGTERM"])
        process.on(signal, () => {
          app.close();
        });
    }
  } catch (error) {
    process.send?.({ error: error.message });
    console.error(error.message);
    process.exitCode = 1;
  }
}
