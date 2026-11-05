import { fork, spawnSync } from "node:child_process";
import {
  mkdirSync,
  readFileSync,
  writeFileSync,
  rmSync,
  openSync,
  closeSync,
} from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const state = resolve(root, ".state/tools");

// Colors follow docker/scripts/stacks: terminal only unless JSH_COLOR says otherwise.
const paint = stream => (code, text) =>
  process.env.JSH_PLAIN_OUTPUT !== "1" &&
  process.env.TERM !== "dumb" &&
  !("NO_COLOR" in process.env) &&
  ({ always: true, never: false }[process.env.JSH_COLOR] ?? stream.isTTY)
    ? `\x1b[${code}m${text}\x1b[0m`
    : text;
const out = paint(process.stdout);
const command = text => out("1;36", text);

export async function startServer(script, args, record, options = {}) {
  try {
    const saved = JSON.parse(readFileSync(record, "utf8"));
    process.kill(saved.pid, 0);
    const response = await fetch(saved.url, {
      redirect: "manual",
      signal: AbortSignal.timeout(2000),
    });
    if (response.ok || response.status === 302 || response.status === 303)
      return saved;
  } catch {}
  const log = openSync(`${record}.log`, "a", 0o600);
  const child = fork(script, args, {
    cwd: root,
    detached: true,
    ...options,
    stdio: ["ignore", log, log, "ipc"],
  });
  closeSync(log);
  try {
    const result = await new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`Server startup timed out; see ${record}.log`)),
        30000
      );
      const finish = (error, value) => {
        clearTimeout(timer);
        error ? reject(error) : resolve(value);
      };
      child.once("error", error => finish(error));
      child.once("exit", () =>
        finish(new Error(`Server failed to start; see ${record}.log`))
      );
      child.once("message", message =>
        finish(message.error ? new Error(message.error) : null, {
          pid: child.pid,
          url: message.url,
        })
      );
    });
    writeFileSync(record, JSON.stringify(result), { mode: 0o600 });
    child.disconnect();
    child.unref();
    return result;
  } catch (error) {
    child.kill();
    throw error;
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    mkdirSync(state, { recursive: true, mode: 0o700 });
    const [tool, host] = process.argv.slice(2);
    if (tool === "stop") {
      const { readdirSync } = await import("node:fs");
      for (const name of readdirSync(state).filter(name =>
        name.endsWith(".json")
      )) {
        const record = resolve(state, name);
        const saved = JSON.parse(readFileSync(record, "utf8"));
        // Verify the recorded listener still exists before signalling its process.
        try {
          const response = await fetch(saved.url, {
            redirect: "manual",
            signal: AbortSignal.timeout(2000),
          });
          if (response.ok || response.status === 302 || response.status === 303)
            process.kill(saved.pid, "SIGTERM");
        } catch {}
        rmSync(record);
      }
      console.log(`${out(32, "✓")} ${out(1, "Stopped")} local console servers`);
    } else {
      if (
        tool !== "console" ||
        (host && !/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(host))
      )
        throw new Error("Usage: just open console [HOST] | just stop");
      const directory = resolve(root, tool);
      const installed = spawnSync("npm", ["ls", "--omit=dev", "--parseable"], {
        cwd: directory,
        stdio: "ignore",
      });
      // npm ls trusts package.json, so also load each dependency to catch missing files.
      const dependencies = Object.keys(
        JSON.parse(readFileSync(resolve(directory, "package.json"), "utf8"))
          .dependencies ?? {}
      );
      const loads = spawnSync(
        process.execPath,
        [
          "--input-type=module",
          "--eval",
          dependencies
            .map(name => `await import(${JSON.stringify(name)});`)
            .join(""),
        ],
        { cwd: directory, stdio: "ignore" }
      );
      if (installed.status !== 0 || loads.status !== 0) {
        const install = spawnSync("npm", ["ci", "--no-audit", "--no-fund"], {
          cwd: directory,
          stdio: "inherit",
        });
        if (install.status !== 0)
          throw new Error(`Cannot install ${tool} dependencies`);
      }
      const { loadInventory, selectHosts } =
        await import("../console/server.mjs");
      const { releaseSessions } = await import("../console/sessions.mjs");
      for (const name of selectHosts(loadInventory(), host || undefined))
        await releaseSessions(name);
      const record = resolve(state, `${tool}-${host || "all"}.json`);
      const result = await startServer(
        resolve(directory, "server.mjs"),
        [...(host ? [host] : []), "--no-open"],
        record
      );
      const opened = spawnSync(
        process.platform === "darwin"
          ? "open"
          : process.platform === "win32"
            ? "explorer.exe"
            : "xdg-open",
        [result.url],
        { stdio: "ignore" }
      );
      if (opened.error || opened.status !== 0)
        throw new Error(
          `Server is running; browser could not open. Retrieve the local URL from ${record}.`
        );
      console.log(
        `${out(32, "✓")} ${out(1, "Console")} opened for ${host || "all hardware consoles"}\n` +
          `  ${out("2;37", "Server runs in the background; stop it with")} ${command("just stop")}`
      );
    }
  } catch (error) {
    console.error(paint(process.stderr)(31, `✗ ${error.message}`));
    process.exitCode = 1;
  }
}
