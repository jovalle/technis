import { test } from "node:test";
import assert from "node:assert/strict";
import { Duplex } from "node:stream";
import { readFileSync } from "node:fs";
import { createHash, X509Certificate } from "node:crypto";
import { authenticate, tlsOptions } from "./amt.mjs";
import { createConsole } from "./server.mjs";
import { WebSocket } from "ws";
import { once } from "node:events";

const md5 = value => createHash("md5").update(value).digest("hex");
function authReply(status, data, type = 4) {
  const header = Buffer.from([0x14, status, 0, 0, type, 0, 0, 0, 0]);
  header.writeUInt32LE(data.length, 5);
  return Buffer.concat([header, data]);
}
const fields = values =>
  Buffer.concat(
    values.map(value =>
      Buffer.concat([Buffer.from([value.length]), Buffer.from(value)])
    )
  );

// A firmware-shaped peer verifies the Digest rather than accepting any response.
export function firmware(options = {}) {
  let step = 0;
  const writes = [];
  const socket = new Duplex({
    read() {},
    write(data, _encoding, callback) {
      writes.push(Buffer.from(data));
      let response;
      try {
        if (step === 0) {
          assert.equal(data.toString("hex"), "100100004b564d52");
          response = options.busy
            ? Buffer.from([0x11, 2, 0, 0])
            : Buffer.from([0x11, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
        } else if (step === 1) {
          response = authReply(0, Buffer.from([4]), 0);
        } else if (step === 2) {
          response = authReply(
            1,
            options.malformed
              ? Buffer.from([255])
              : fields(["realm", "nonce", "auth"])
          );
        } else if (step === 3) {
          const values = [];
          for (let offset = 9; offset < data.length; ) {
            const size = data[offset++];
            values.push(data.toString("latin1", offset, (offset += size)));
          }
          const [user, realm, nonce, uri, cnonce, nc, digest, qop] = values;
          assert.deepEqual(
            [user, realm, nonce, uri, nc, qop],
            [
              "admin",
              "realm",
              "nonce",
              "/RedirectionService",
              "00000002",
              "auth",
            ]
          );
          assert.equal(
            digest,
            md5(
              `${md5("admin:realm:secret")}:nonce:${nc}:${cnonce}:auth:${md5("POST:/RedirectionService")}`
            )
          );
          response = authReply(options.reject ? 1 : 0, Buffer.alloc(0));
        } else if (step === 4) {
          assert.equal(data.toString("hex"), "4000000000000000");
          response = Buffer.concat([
            Buffer.from([0x41, 0, 0, 0, 0, 0, 0, 0]),
            Buffer.from("RFB 003.008\n"),
          ]);
        }
        step++;
        if (response) {
          // Split headers and coalesce the KVM-start reply with RFB data.
          this.push(response.subarray(0, 2));
          setImmediate(() => this.push(response.subarray(2)));
        }
        callback();
      } catch (error) {
        callback(error);
      }
    },
  });
  socket.setTimeout = () => {};
  socket.writes = writes;
  return socket;
}

test("fragmented AMT Digest handshake preserves coalesced RFB bytes", async () => {
  const socket = firmware();
  const stream = authenticate(socket, {
    username: "admin",
    password: "secret",
  });
  const received = [];
  while (Buffer.concat(received).length < 12)
    received.push((await stream.next()).value);
  assert.equal(Buffer.concat(received).toString(), "RFB 003.008\n");
  socket.destroy();
});

for (const [options, expected] of [
  [{ busy: true }, /busy/],
  [{ malformed: true }, /Truncated/],
  [{ reject: true }, /rejected/],
]) {
  test(`AMT failure closes cleanly: ${expected}`, async () => {
    const socket = firmware(options);
    await assert.rejects(
      authenticate(socket, { username: "admin", password: "secret" }).next(),
      expected
    );
    socket.destroy();
  });
}

test("TLS requires CA verification and the exact enrolled certificate before authentication", () => {
  const certificate = readFileSync(
    new URL(
      "../ansible/inventory/group_vars/amt/certificates/ms-01.pem",
      import.meta.url
    )
  );
  const other = readFileSync(
    new URL(
      "../ansible/inventory/group_vars/amt/certificates/ms-02.pem",
      import.meta.url
    )
  );
  const options = tlsOptions({
    host: "192.168.10.21",
    certificate,
    legacy: true,
  });
  assert.equal(options.rejectUnauthorized, true);
  assert.equal(options.minVersion, "TLSv1.2");
  assert.equal(
    options.checkServerIdentity("generic-intel-name", {
      raw: new X509Certificate(certificate).raw,
    }),
    undefined
  );
  assert.match(
    options.checkServerIdentity("generic-intel-name", {
      raw: new X509Certificate(other).raw,
    }).message,
    /pin/
  );
});

test("the authenticated browser relay forwards RFB unchanged and reconnects after close", async t => {
  const peers = [];
  const app = createConsole(
    { name: "ms-01", username: "admin", password: "secret" },
    () => {
      const peer = firmware();
      peers.push(peer);
      return peer;
    }
  );
  t.after(() => app.close());
  const launch = await app.listen();
  const origin = new URL(launch).origin;
  const response = await fetch(launch, { redirect: "manual" });
  const headers = {
    Origin: origin,
    Cookie: response.headers.get("set-cookie").split(";")[0],
  };
  for (let index = 0; index < 2; index++) {
    const ws = new WebSocket(`${origin.replace("http", "ws")}/kvm`, {
      headers,
    });
    const [banner, binary] = await once(ws, "message");
    assert.equal(binary, true);
    assert.equal(banner.toString(), "RFB 003.008\n");
    ws.send(Buffer.from("RFB 003.008\n"));
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(peers[index].writes.at(-1).toString(), "RFB 003.008\n");
    ws.send(Buffer.from([3, 1, 0, 0, 0, 0, 4, 0, 3, 0]));
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(
      peers[index].writes.at(-1).toString("hex"),
      "03010000000004000300"
    );
    const remoteClosed = once(peers[index], "close");
    ws.close();
    await once(ws, "close");
    await remoteClosed;
    assert.equal(peers[index].destroyed, true);
  }
});

for (const [options, retryable] of [
  [{ busy: true }, true],
  [{ reject: true }, false],
  [{ malformed: true }, false],
]) {
  test(`relay classifies firmware failure ${JSON.stringify(options)} for automatic recovery`, async t => {
    const app = createConsole(
      { name: "ms-01", username: "admin", password: "secret" },
      () => firmware(options)
    );
    t.after(() => app.close());
    const launch = await app.listen();
    const origin = new URL(launch).origin;
    const response = await fetch(launch, { redirect: "manual" });
    const headers = {
      Origin: origin,
      Cookie: response.headers.get("set-cookie").split(";")[0],
    };
    const ws = new WebSocket(`${origin.replace("http", "ws")}/kvm`, {
      headers,
    });
    const [data, binary] = await once(ws, "message");
    assert.equal(binary, false);
    assert.equal(JSON.parse(data).retryable, retryable);
    await once(ws, "close");
  });
}
