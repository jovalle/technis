import {
  mkdirSync,
  lstatSync,
  readdirSync,
  readFileSync,
  writeFileSync,
  renameSync,
  rmSync,
} from "node:fs";
import { join } from "node:path";
import { tmpdir, userInfo } from "node:os";
import { randomBytes } from "node:crypto";

const directory = join(
  tmpdir(),
  `technis-console-${process.getuid?.() ?? userInfo().username}`
);

function privateDirectory(path) {
  mkdirSync(path, { recursive: true, mode: 0o700 });
  const stat = lstatSync(path);
  if (
    !stat.isDirectory() ||
    (process.getuid && (stat.uid !== process.getuid() || stat.mode & 0o077))
  ) {
    throw new Error(
      "Local console registry must be a private directory owned by your user"
    );
  }
}

export function registerSession(session, path = directory) {
  privateDirectory(path);
  const file = join(path, `${randomBytes(16).toString("hex")}.json`);
  writeFileSync(`${file}.tmp`, JSON.stringify(session), {
    mode: 0o600,
    flag: "wx",
  });
  renameSync(`${file}.tmp`, file);
  return () => rmSync(file, { force: true });
}

export async function releaseSessions(
  host,
  path = directory,
  powerPause = false
) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(host ?? ""))
    throw new Error("Usage: just stop console [HOST]");
  privateDirectory(path);
  let released = 0;
  const failures = [];
  for (const name of readdirSync(path).filter(name =>
    /^[a-f0-9]{32}\.json$/.test(name)
  )) {
    const file = join(path, name);
    try {
      const record = JSON.parse(readFileSync(file, "utf8"));
      if (
        !/^http:\/\/127\.0\.0\.1:[0-9]{1,5}$/.test(record.origin) ||
        !/^[a-f0-9]{64}$/.test(record.token) ||
        !Array.isArray(record.hosts)
      ) {
        throw new Error("Invalid local console registry entry");
      }
      if (!record.hosts.includes(host)) continue;
      const response = await fetch(
        `${record.origin}/release/${encodeURIComponent(host)}${powerPause ? "?power=1" : ""}`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${record.token}` },
          redirect: "error",
          signal: AbortSignal.timeout(2000),
        }
      );
      if (!response.ok)
        throw new Error(`Local console refused release (${response.status})`);
      const result = await response.json();
      if (result.released === true) released++;
      else if (result.released !== false)
        throw new Error("Invalid local console release response");
    } catch (error) {
      const code = error.cause?.code ?? error.code;
      if (code === "ENOENT") continue;
      if (code === "ECONNREFUSED") {
        rmSync(file, { force: true });
        continue;
      }
      failures.push(error.message);
    }
  }
  if (failures.length)
    throw new Error(
      `Released ${released} local viewer(s) for ${host}; could not release all: ${failures.join("; ")}`
    );
  return released;
}
