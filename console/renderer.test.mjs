import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

test('shipped scripts initialize the renderer and complete a real RFB-shaped negotiation', () => {
  const screen = { width: 640, height: 400, style: {} };
  const scheduled = [];
  const context = vm.createContext({
    Uint8Array, DataView, setTimeout: callback => scheduled.push(callback), clearTimeout,
    Q: () => screen, QS: () => screen.style,
    ShortToStr: value => String.fromCharCode((value >> 8) & 255, value & 255),
    IntToStr: value => String.fromCharCode(value >>> 24, (value >>> 16) & 255, (value >>> 8) & 255, value & 255),
  });
  let pixels;
  screen.getContext = () => ({ canvas: screen,
    createImageData: (width, height) => ({ data: new Uint8ClampedArray(width * height * 4) }),
    putImageData: image => { pixels = image.data; },
  });
  // Follow the actual HTML script order, so missing zlib dependencies fail here.
  const html = readFileSync(new URL('index.html', import.meta.url), 'utf8');
  for (const [, path] of html.matchAll(/src="\/(vendor\/[^"]+)"/g)) {
    vm.runInContext(readFileSync(new URL(path, import.meta.url), 'utf8'), context);
  }
  const desktop = context.CreateAmtRemoteDesktop('screen');
  const sent = [];
  let ready = false;
  let frames = 0;
  desktop.onFrameReceived = () => { frames++; };
  desktop.parent = { send: data => sent.push(data), xxStateChange: state => { ready = state === 3; } };
  desktop.Start();
  const init = Buffer.alloc(24);
  init.writeUInt16BE(1920); init.writeUInt16BE(1080, 2);
  const wire = Buffer.concat([Buffer.from('RFB 004.000\n'), Buffer.from([1, 1]), Buffer.alloc(4), init]);
  // Exercise fragmented RFB messages as well as the transport's fragmented handshake.
  for (const byte of wire) desktop.ProcessBinaryData(Uint8Array.from([byte]).buffer);
  assert.equal(ready, true);
  assert.equal(screen.width, 1920);
  assert.equal(screen.height, 1080);
  assert.equal(sent[0], 'RFB 003.008\n');
  const tile = Buffer.alloc(12);
  tile.writeUInt16BE(1, 4); tile.writeUInt16BE(1, 6);
  const frame = Buffer.concat([Buffer.from([0, 0, 0, 1]), tile, Buffer.from([0, 0xf8])]);
  desktop.ProcessBinaryData(Uint8Array.from(frame).buffer);
  assert.deepEqual([...pixels], [248, 0, 0, 255]);
  assert.equal(frames, 1);
  desktop.ProcessBinaryData(Uint8Array.from([0, 0, 0, 0]).buffer);
  assert.equal(desktop.state, 4);
  assert.equal(scheduled.length, 1);
  scheduled[0]();
  assert.equal(sent.at(-1).charCodeAt(0), 3);
  desktop.sendkey(0xff08, 1);
  assert.equal(Buffer.from(sent.at(-1), 'latin1').toString('hex'), '040100000000ff08');
  const sizes = [];
  desktop.onScreenSizeChange = () => sizes.push([screen.width, screen.height]);
  const reset = Buffer.alloc(12);
  reset.writeUInt16BE(800, 4); reset.writeUInt16BE(600, 6); reset.writeInt32BE(-223, 8);
  const resize = Uint8Array.from(Buffer.concat([Buffer.from([0, 0, 0, 1]), reset])).buffer;
  desktop.ProcessBinaryData(resize);
  desktop.ProcessBinaryData(resize);
  assert.deepEqual(sizes, [[800, 600], [800, 600]], 'real DesktopSize messages signal resets even with unchanged dimensions');
  let stopped = false;
  desktop.parent.Stop = () => { stopped = true; };
  desktop.ProcessBinaryData(Uint8Array.from(Buffer.concat([Buffer.from([0, 0, 0, 1]), Buffer.alloc(12)])).buffer);
  assert.equal(stopped, false, 'a zero-area raw rectangle during reboot does not terminate the viewer');
  assert.equal(desktop.state, 4);
  assert.equal(scheduled.length, 2, 'empty rectangles back off refresh requests like empty updates');
  assert.equal(frames, 1, 'initialization, resize notices, and empty updates are not actual video frames');
  scheduled.at(-1)();
  desktop.ProcessBinaryData(Uint8Array.from(frame).buffer);
  assert.deepEqual([...pixels], [248, 0, 0, 255], 'pixel decoding resumes after the empty rectangle');
  assert.equal(frames, 2);
  const oversized = Buffer.from(tile); oversized.writeUInt16BE(65, 4);
  desktop.ProcessBinaryData(Uint8Array.from(Buffer.concat([Buffer.from([0, 0, 0, 1]), oversized])).buffer);
  assert.equal(stopped, true, 'oversized rectangles are still rejected before allocation');
});
