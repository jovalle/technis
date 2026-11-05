import { keyboard, pointer } from "./input.mjs";

if (window.self !== window.top) document.body.classList.add("embedded");

// Minimal globals expected by MeshCentral's renderer.
window.Q = id => document.getElementById(id);
window.QS = id => Q(id)?.style;
window.QV = (id, visible) => {
  if (Q(id)) Q(id).hidden = !visible;
};
window.ShortToStr = value =>
  String.fromCharCode((value >> 8) & 255, value & 255);
window.IntToStr = value =>
  String.fromCharCode(
    (value >> 24) & 255,
    (value >> 16) & 255,
    (value >> 8) & 255,
    value & 255
  );

const screen = Q("screen"),
  viewport = Q("viewport");
const keyButtons = [...document.querySelectorAll("[data-key]")];
let desktop,
  socket,
  connected = false,
  lastPointer = { x: 0, y: 0, buttons: 0 },
  motion;
// A transport reconnect must retain the OS mode to recognize the firmware reset.
let previousSize;
let closing = Promise.resolve(),
  generation = 0;
let wanted = false,
  retryTimer,
  connectTimer,
  retryDelay = 1000;
const keys = keyboard((key, down) => {
  if (connected) desktop.sendkey(key, down);
});

function status(message, live = false) {
  Q("status").textContent = live ? "Connected" : "Disconnected";
  Q("notice").textContent = message;
  Q("notice").hidden = live;
  keyButtons.forEach(button => {
    button.disabled = !live;
  });
  Q("disconnect").disabled = !live && !socket;
}
function fit() {
  const scale = Math.min(
    (viewport.clientWidth - 16) / screen.width,
    (viewport.clientHeight - 16) / screen.height
  );
  screen.style.width = `${Math.max(1, screen.width * scale)}px`;
  screen.style.height = `${Math.max(1, screen.height * scale)}px`;
}
new ResizeObserver(fit).observe(viewport);
window.addEventListener("resize", fit);
function send(data) {
  if (socket?.readyState === WebSocket.OPEN)
    socket.send(Uint8Array.from(data, char => char.charCodeAt(0)));
}
function sendPointer(value) {
  if (!connected) return;
  lastPointer = value;
  send(
    String.fromCharCode(5, value.buttons) +
      ShortToStr(value.x) +
      ShortToStr(value.y)
  );
}
function releasePointer() {
  cancelAnimationFrame(motion);
  if (lastPointer.buttons) sendPointer({ ...lastPointer, buttons: 0 });
}
function release() {
  keys.release();
  releasePointer();
  Q("focus").textContent = "Click to control";
}
function disconnect(stop = true) {
  if (stop) wanted = false;
  clearTimeout(retryTimer);
  clearTimeout(connectTimer);
  generation++;
  release();
  connected = false;
  const current = socket;
  socket = null;
  if (current && current.readyState !== WebSocket.CLOSED) {
    closing = new Promise(resolve =>
      current.addEventListener("close", resolve, { once: true })
    );
    current.close();
  }
  Q("reconnect").disabled = false;
  status("Console disconnected. Choose Reconnect to resume.");
}
function recover(message) {
  if (!wanted) return;
  disconnect(false);
  status(`${message} Retrying in ${retryDelay / 1000}s…`);
  Q("status").textContent = "Reconnecting…";
  Q("disconnect").disabled = false;
  retryTimer = setTimeout(start, retryDelay);
  retryDelay = Math.min(retryDelay * 2, 10000);
}
async function start() {
  wanted = true;
  disconnect(false);
  const attempt = generation;
  Q("reconnect").disabled = true;
  await closing;
  if (attempt !== generation) return;
  status("Connecting to Intel AMT…");
  Q("status").textContent = "Connecting…";
  const renderer = (desktop = window.CreateAmtRemoteDesktop("screen"));
  let initialSize = true;
  renderer.parent = {
    send(data) {
      if (socket === current) send(data);
    },
    xxStateChange(state) {
      if (state !== 3 || socket !== current) return;
      clearTimeout(connectTimer);
      retryDelay = 1000;
      connected = true;
      status("", true);
      fit();
      Q("resolution").textContent = `${screen.width} × ${screen.height}`;
    },
    Stop() {
      if (socket !== current) return;
      disconnect();
      status("AMT sent unsupported video data. Choose Reconnect to retry.");
    },
  };
  renderer.onScreenSizeChange = () => {
    if (socket !== current) return;
    fit();
    Q("resolution").textContent = `${screen.width} × ${screen.height}`;
    const size = `${screen.width}x${screen.height}`;
    const initial = initialSize;
    initialSize = false;
    // AMT can advertise the old OS dimensions before sending its firmware size.
    if (initial && previousSize) return;
    if (previousSize === size) return;
    clearTimeout(retryTimer);
    // shortcut: these hosts reset to 800×600 before the firmware KVM wait; calibrate other firmware modes before extending support.
    if (
      previousSize &&
      previousSize !== "800x600" &&
      screen.width === 800 &&
      screen.height === 600
    ) {
      retryTimer = setTimeout(() => recover("Firmware display reset."), 2000);
    }
    previousSize = size;
  };
  renderer.onFrameReceived = () => {
    if (`${screen.width}x${screen.height}` !== previousSize)
      renderer.onScreenSizeChange();
  };
  renderer.Start();
  const current = (socket = new WebSocket(
    `${location.origin.replace("http", "ws")}${location.pathname}kvm`
  ));
  connectTimer = setTimeout(() => {
    if (socket === current) recover("AMT did not finish connecting.");
  }, 20000);
  Q("disconnect").disabled = false;
  current.binaryType = "arraybuffer";
  current.onopen = () => {
    if (socket === current) Q("reconnect").disabled = false;
  };
  let failure,
    retryable = true;
  current.onmessage = event => {
    if (socket !== current) return;
    if (typeof event.data === "string") {
      const reply = JSON.parse(event.data);
      failure = reply.error;
      retryable = reply.retryable === true;
      if (!retryable) {
        disconnect();
        status(failure);
        return;
      }
      release();
      connected = false;
      status(failure);
    } else {
      try {
        renderer.ProcessBinaryData(event.data);
      } catch {
        failure = "Cannot decode AMT video. Choose Reconnect to retry.";
        disconnect();
        status(failure);
      }
    }
  };
  current.onclose = async event => {
    if (socket !== current) return;
    if (event.code === 4001) {
      disconnect();
      status(
        failure ??
          "Session released by another local console. Run just open console again to reconnect."
      );
      return;
    }
    if (event.code === 4002) {
      disconnect();
      status(
        failure ??
          "Console paused for a power action. Choose Reconnect after the host returns."
      );
      return;
    }
    if (event.code === 1001) {
      disconnect();
      status(
        "Local console stopped. Run just open console again and open the new launch URL."
      );
      return;
    }
    release();
    connected = false;
    clearTimeout(connectTimer);
    clearTimeout(retryTimer);
    status("Checking the local relay…");
    try {
      const response = await fetch(`${location.pathname}session`, {
        signal: AbortSignal.timeout(3000),
      });
      if (socket !== current) return;
      if (!response.ok) {
        disconnect();
        status(
          "Session expired. Open the launch URL printed by just open console."
        );
        return;
      }
    } catch {
      if (socket !== current) return;
      disconnect();
      status(
        "Local relay disconnected. Run just open console again and open the new launch URL."
      );
      return;
    }
    if (retryable) recover(failure ?? "AMT disconnected.");
    else {
      disconnect();
      status(failure);
    }
  };
  current.onerror = () => {
    failure ??=
      "Cannot connect to the local relay. Run just open console again, then reconnect.";
  };
}

screen.addEventListener("focus", () => {
  Q("focus").textContent = "Keyboard captured";
});
screen.addEventListener("blur", release);
screen.addEventListener("keydown", event => {
  if (event.code === "Escape" && event.shiftKey) {
    event.preventDefault();
    release();
    Q("reconnect").focus();
    return;
  }
  if (!connected || event.isComposing) return;
  event.preventDefault();
  event.stopPropagation();
  keys.down(event);
});
screen.addEventListener("keyup", event => {
  event.preventDefault();
  keys.up(event);
});
screen.addEventListener("contextmenu", event => event.preventDefault());
screen.addEventListener("pointerdown", event => {
  if (!connected) return;
  screen.focus();
  screen.setPointerCapture(event.pointerId);
  event.preventDefault();
  sendPointer(
    pointer(event, screen.getBoundingClientRect(), screen.width, screen.height)
  );
});
screen.addEventListener("pointermove", event => {
  if (!connected) return;
  const value = pointer(
    event,
    screen.getBoundingClientRect(),
    screen.width,
    screen.height
  );
  cancelAnimationFrame(motion);
  motion = requestAnimationFrame(() => sendPointer(value));
});
screen.addEventListener("pointerup", event => {
  cancelAnimationFrame(motion);
  sendPointer(
    pointer(event, screen.getBoundingClientRect(), screen.width, screen.height)
  );
});
screen.addEventListener("pointercancel", release);
screen.addEventListener("lostpointercapture", releasePointer);
screen.addEventListener(
  "wheel",
  event => {
    if (!connected || !event.deltaY) return;
    event.preventDefault();
    cancelAnimationFrame(motion);
    const value = pointer(
      event,
      screen.getBoundingClientRect(),
      screen.width,
      screen.height
    );
    sendPointer({
      ...value,
      buttons: value.buttons | (event.deltaY < 0 ? 8 : 16),
    });
    sendPointer(value);
  },
  { passive: false }
);
window.addEventListener("blur", release);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) release();
});
window.addEventListener("pagehide", () => disconnect());

const chords = {
  backspace: [0xff08],
  ctrlc: [0xffe3, 99],
  escape: [0xff1b],
  tab: [0xff09],
  f2: [0xffbf],
  f11: [0xffc8],
  cad: [0xffe3, 0xffe9, 0xffff],
};
keyButtons.forEach(button =>
  button.addEventListener("click", () => {
    keys.chord(chords[button.dataset.key]);
    screen.focus();
  })
);
Q("disconnect").onclick = () => disconnect();
Q("reconnect").onclick = start;
Q("fullscreen").onclick = async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
    screen.focus();
  } catch {
    Q("focus").textContent = "Full screen is unavailable in this browser";
  }
};
try {
  const response = await fetch(`${location.pathname}session`);
  if (!response.ok) throw new Error();
  const session = await response.json();
  Q("host").textContent = session.host;
  await start();
} catch {
  status("Session expired. Open the launch URL printed by just open console.");
}
