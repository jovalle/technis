import https from "node:https";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { tlsOptions } from "./amt.mjs";

export const powerActions = {
  on: "Power on",
  shutdown: "Shut down",
  restart: "Reset (force)",
  off: "Power off (force)",
  cycle: "Power cycle (force)",
};
const resetTypes = {
  on: "On",
  shutdown: "GracefulShutdown",
  restart: "ForceRestart",
  off: "ForceOff",
  cycle: "PowerCycle",
};

export function amtPower(config, action) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      "python3",
      [fileURLToPath(new URL("./power.py", import.meta.url))],
      { stdio: ["pipe", "pipe", "pipe"] }
    );
    const timer = setTimeout(() => child.kill(), 45000);
    let output = "",
      size = 0;
    child.stdout.on("data", data => {
      size += data.length;
      if (size > 65536) child.kill();
      else output += data;
    });
    child.stderr.resume();
    child.stdin.on("error", () => {});
    child.on("error", reject);
    child.on("close", code => {
      clearTimeout(timer);
      let result;
      try {
        result = JSON.parse(output);
      } catch {}
      if (code === 0 && result && !result.error) resolve(result);
      else
        reject(
          new Error(
            typeof result?.error === "string"
              ? result.error
              : "AMT power request failed"
          )
        );
    });
    child.stdin.end(JSON.stringify({ config, action }));
  });
}

function redfish(config, path, body) {
  return new Promise((resolve, reject) => {
    const data = body && JSON.stringify(body);
    const req = https.request(
      {
        ...tlsOptions(config),
        path,
        method: body ? "POST" : "GET",
        headers: {
          Authorization: `Basic ${Buffer.from(`${config.username}:${config.password}`).toString("base64")}`,
          Accept: "application/json",
          ...(data
            ? {
                "Content-Type": "application/json",
                "Content-Length": Buffer.byteLength(data),
              }
            : {}),
        },
      },
      res => {
        let output = "",
          size = 0;
        res.on("data", chunk => {
          size += chunk.length;
          if (size > 1024 * 1024)
            res.destroy(new Error("Redfish response too large"));
          else output += chunk;
        });
        res.on("error", reject);
        res.on("end", () => {
          try {
            if (res.statusCode < 200 || res.statusCode >= 300)
              throw new Error();
            const result = output ? JSON.parse(output) : {};
            if (result.error) throw new Error();
            resolve(result);
          } catch {
            reject(new Error("Redfish power request rejected"));
          }
        });
      }
    );
    req.setTimeout(15000, () =>
      req.destroy(new Error("Redfish request timed out"))
    );
    req.on("error", reject);
    req.end(data);
  });
}

export async function bmcPower(config, action) {
  const systems = await redfish(config, "/redfish/v1/Systems");
  const path = systems.Members?.length === 1 && systems.Members[0]["@odata.id"];
  if (
    typeof path !== "string" ||
    !/^\/redfish\/v1\/Systems\/[a-zA-Z0-9_.-]+$/.test(path)
  )
    throw new Error("Expected one Redfish system");
  const system = await redfish(config, path);
  const reset = system.Actions?.["#ComputerSystem.Reset"];
  let allowed = reset?.["ResetType@Redfish.AllowableValues"];
  if (!allowed && reset?.["@Redfish.ActionInfo"]) {
    const infoPath = reset["@Redfish.ActionInfo"];
    if (infoPath !== `${path}/ResetActionInfo`)
      throw new Error("Unexpected Redfish action metadata");
    const info = await redfish(config, infoPath);
    allowed = info.Parameters?.find(
      parameter => parameter.Name === "ResetType"
    )?.AllowableValues;
  }
  const actions = Object.keys(resetTypes).filter(key =>
    allowed?.includes(resetTypes[key])
  );
  if (action !== undefined) {
    if (
      !actions.includes(action) ||
      reset.target !== `${path}/Actions/ComputerSystem.Reset`
    )
      throw new Error("Power action unavailable");
    await redfish(config, reset.target, { ResetType: resetTypes[action] });
    return { accepted: true };
  }
  return {
    state:
      {
        On: "on",
        Off: "off",
        PoweringOn: "powering on",
        PoweringOff: "powering off",
      }[system.PowerState] ?? "unknown",
    actions,
  };
}

export function power(config, action) {
  if (action !== undefined && !Object.hasOwn(powerActions, action))
    throw new Error("Power action unavailable");
  return config.protocol === "ipmi"
    ? bmcPower(config, action)
    : amtPower(config, action);
}
