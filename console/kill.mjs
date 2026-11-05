import { amtPower } from "./power.mjs";
import { releaseSessions } from "./sessions.mjs";

export async function resetKvm(config, request = amtPower) {
  try {
    await request(config, "disconnect");
  } catch (error) {
    throw new Error(
      `AMT KVM disconnect failed for ${config.name}: ${error.message}`
    );
  }
}

export async function killConsoles(
  hosts,
  { release = releaseSessions, reset = resetKvm, report = console.log } = {}
) {
  const failures = [],
    ready = [];
  // Stop every local retry loop before resetting any of the firmware sessions.
  for (const host of hosts) {
    try {
      report(`Released ${await release(host)} local viewer(s) for ${host}.`);
      ready.push(host);
    } catch (error) {
      failures.push(`${host}: ${error.message}`);
    }
  }
  for (const host of ready) {
    try {
      const kind = await reset(host);
      report(
        `${kind ?? "AMT"} KVM disconnected for ${host}; host power and boot settings unchanged.`
      );
    } catch (error) {
      failures.push(`${host}: ${error.message}`);
    }
  }
  if (failures.length) throw new Error(failures.join("\n"));
}
