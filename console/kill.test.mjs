import { test } from "node:test";
import assert from "node:assert/strict";
import { killConsoles, resetKvm } from "./kill.mjs";
import { selectHosts } from "./server.mjs";

test("kill selects AMT and IPMI hosts and rejects invalid targets", () => {
  const inventory = {
    amt: { hosts: ["ms-03", "ms-01", "ms-02"] },
    ipmi: { hosts: ["nexus"] },
  };
  assert.deepEqual(selectHosts(inventory), [
    "ms-01",
    "ms-02",
    "ms-03",
    "nexus",
  ]);
  assert.deepEqual(selectHosts(inventory, "nexus"), ["nexus"]);
  assert.deepEqual(selectHosts(inventory, "ms-02"), ["ms-02"]);
  for (const name of ["unknown", "*", "--no-open", ""])
    assert.throws(
      () => selectHosts(inventory, name),
      /not in the Ansible .* group/
    );
  assert.throws(() => selectHosts({}), /No hosts/);
});

test("kill all releases local retries first and resets hosts sequentially", async () => {
  const steps = [];
  let resetting = false;
  await killConsoles(["ms-01", "ms-02", "ms-03"], {
    release: async host => {
      steps.push(`release ${host}`);
      return 1;
    },
    reset: async host => {
      assert.equal(resetting, false);
      resetting = true;
      steps.push(`reset ${host}`);
      await new Promise(resolve => setImmediate(resolve));
      resetting = false;
    },
    report() {},
  });
  assert.deepEqual(steps, [
    "release ms-01",
    "release ms-02",
    "release ms-03",
    "reset ms-01",
    "reset ms-02",
    "reset ms-03",
  ]);
});

test("kill continues unaffected hosts but reports failures and skips firmware if local release fails", async () => {
  const reset = [];
  await assert.rejects(
    killConsoles(["ms-01", "ms-02", "ms-03"], {
      release: async host => {
        if (host === "ms-01") throw new Error("local release failed");
        return 0;
      },
      reset: async host => {
        reset.push(host);
        if (host === "ms-02") throw new Error("restore failed");
      },
      report() {},
    }),
    /ms-01: local release failed\nms-02: restore failed/
  );
  assert.deepEqual(reset, ["ms-02", "ms-03"]);
});

test("firmware request disconnects KVM on the selected host only", async () => {
  const config = { name: "ms-01", host: "192.0.2.1" };
  await resetKvm(config, async (target, action) => {
    assert.equal(target, config);
    assert.equal(action, "disconnect");
  });
  await assert.rejects(
    resetKvm(config, async () => {
      throw new Error("KVM may still be disabled");
    }),
    /^Error: AMT KVM disconnect failed for ms-01: KVM may still be disabled$/
  );
});
