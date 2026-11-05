const get = id => document.getElementById(id);
if (window.self !== window.top) document.body.classList.add("embedded");
const screen = get("screen"),
  viewport = get("viewport"),
  context = screen.getContext("2d");
const buttons = [...document.querySelectorAll("[data-key]")],
  held = new Set(),
  mouse = new Set();
let socket,
  decoder,
  codec,
  wanted = false,
  generation = 0,
  retry,
  heartbeat,
  snapshots,
  snapshotTimer,
  frameAbort;
let host,
  live = false,
  keyframe = true,
  fingerbotBusy = false,
  duration,
  strength;
let sourceWidth = screen.width,
  sourceHeight = screen.height;
function fit() {
  if (viewport.clientWidth <= 16 || viewport.clientHeight <= 16) return;
  const scale = Math.min(
    (viewport.clientWidth - 16) / sourceWidth,
    (viewport.clientHeight - 16) / sourceHeight
  );
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(sourceWidth * scale * ratio));
  const height = Math.max(1, Math.round(sourceHeight * scale * ratio));
  if (screen.width !== width || screen.height !== height) {
    screen.width = width;
    screen.height = height;
  }
  screen.style.width = `${width / ratio}px`;
  screen.style.height = `${height / ratio}px`;
  // Resample once at device-pixel resolution; preserve hard edges when enlarging.
  context.imageSmoothingEnabled = width < sourceWidth || height < sourceHeight;
  context.imageSmoothingQuality = "high";
}
function status(message, connected = false) {
  live = connected;
  get("status").textContent = connected ? "Connected" : message;
  get("notice").textContent = message;
  get("notice").hidden = connected;
  buttons.forEach(button => {
    button.disabled = !connected;
  });
  get("disconnect").disabled = !wanted;
}
function send(event_type, event) {
  if (socket?.readyState === WebSocket.OPEN)
    socket.send(JSON.stringify({ event_type, event }));
}
function release() {
  for (const key of [...held].reverse()) send("key", { key, state: false });
  for (const button of mouse) send("mouse_button", { button, state: false });
  held.clear();
  mouse.clear();
  get("focus").textContent = "Click to control";
}
function draw(frame, attempt) {
  try {
    if (attempt !== generation || !wanted) return;
    sourceWidth = frame.displayWidth ?? frame.width;
    sourceHeight = frame.displayHeight ?? frame.height;
    fit();
    context.drawImage(frame, 0, 0, screen.width, screen.height);
    get("resolution").textContent = `${sourceWidth} × ${sourceHeight}`;
    status("", true);
  } finally {
    frame.close();
  }
}
function stop() {
  wanted = false;
  generation++;
  release();
  clearTimeout(retry);
  clearTimeout(snapshotTimer);
  clearInterval(heartbeat);
  frameAbort?.abort();
  snapshots = false;
  socket?.close();
  socket = null;
  if (decoder && decoder.state !== "closed") decoder.close();
  decoder = null;
  codec = "";
  keyframe = true;
  status("Disconnected");
}
function fallback(attempt) {
  if (snapshots || attempt !== generation) return;
  snapshots = true;
  async function next() {
    if (attempt !== generation || !wanted) return;
    try {
      frameAbort = new AbortController();
      const response = await fetch(`${location.pathname}snapshot`, {
        signal: frameAbort.signal,
      });
      if (!response.ok) throw new Error();
      draw(await createImageBitmap(await response.blob()), attempt);
    } catch {
      if (attempt === generation)
        status(
          "GLKVM video unavailable. Check HDMI; choose Reconnect to retry."
        );
    }
    if (attempt === generation && wanted) snapshotTimer = setTimeout(next, 250);
  }
  next();
}
async function media(video, attempt) {
  if (attempt !== generation || !wanted) return;
  const format = video?.h264?.profile_level_id;
  codec = format ? `avc1.${format}` : "";
  try {
    if (
      !codec ||
      typeof VideoDecoder === "undefined" ||
      !(await VideoDecoder.isConfigSupported({ codec })).supported
    ) {
      fallback(attempt);
      return;
    }
    if (attempt !== generation || !wanted) return;
    decoder = new VideoDecoder({
      output: frame => draw(frame, attempt),
      error: () => fallback(attempt),
    });
    decoder.configure({ codec, optimizeForLatency: true });
    send("start", { type: "video", format: "h264" });
  } catch {
    fallback(attempt);
  }
}
function start() {
  stop();
  wanted = true;
  const attempt = generation;
  status("Connecting to GLKVM…");
  const current = (socket = new WebSocket(
    `${location.origin.replace("http", "ws")}${location.pathname}kvm`
  ));
  current.binaryType = "arraybuffer";
  current.onopen = () => {
    heartbeat = setInterval(() => {
      if (current.readyState === WebSocket.OPEN)
        current.send(new Uint8Array([0]));
    }, 1000);
  };
  current.onmessage = event => {
    if (attempt !== generation) return;
    if (typeof event.data === "string") {
      const value = JSON.parse(event.data);
      if (value.event_type === "media") media(value.event.video, attempt);
      if (value.event_type === "fingerbot") refreshFingerbot();
      if (
        value.event_type === "streamer" &&
        value.event.streamer?.source?.online === false
      )
        status("No HDMI signal. Check Stargate and its HDMI cable.");
    } else {
      const bytes = new Uint8Array(event.data);
      if (bytes[0] !== 1 || snapshots || decoder?.state !== "configured")
        return;
      if (decoder.decodeQueueSize > 6) keyframe = true;
      if (keyframe && !bytes[1]) return;
      keyframe = false;
      try {
        decoder.decode(
          new EncodedVideoChunk({
            timestamp: Math.round(performance.now() * 1000),
            type: bytes[1] ? "key" : "delta",
            data: bytes.subarray(2),
          })
        );
      } catch {
        fallback(attempt);
      }
    }
  };
  current.onerror = () => current.close();
  current.onclose = event => {
    if (attempt !== generation) return;
    stop();
    if ([4001, 1001].includes(event.code)) {
      status("Session released. Run just open console again.");
      return;
    }
    wanted = true;
    status("GLKVM disconnected. Reconnecting…");
    retry = setTimeout(start, 3000);
  };
}
screen.addEventListener("keydown", event => {
  if (event.code === "Escape" && event.shiftKey) {
    event.preventDefault();
    release();
    get("reconnect").focus();
    return;
  }
  if (!live) return;
  event.preventDefault();
  if (!held.has(event.code)) {
    held.add(event.code);
    send("key", { key: event.code, state: true });
  }
});
screen.addEventListener("keyup", event => {
  if (!held.delete(event.code)) return;
  event.preventDefault();
  send("key", { key: event.code, state: false });
});
const chords = {
  backspace: ["Backspace"],
  ctrlc: ["ControlLeft", "KeyC"],
  escape: ["Escape"],
  tab: ["Tab"],
  f2: ["F2"],
  f11: ["F11"],
  cad: ["ControlLeft", "AltLeft", "Delete"],
};
buttons.forEach(button =>
  button.addEventListener("click", () => {
    release();
    for (const state of [true, false])
      for (const key of state
        ? chords[button.dataset.key]
        : [...chords[button.dataset.key]].reverse())
        send("key", { key, state });
    screen.focus();
  })
);
function move(event) {
  const rect = screen.getBoundingClientRect();
  const axis = (value, offset, size) =>
    Math.round(
      Math.max(0, Math.min(1, (value - offset) / size)) * 65535 - 32768
    );
  send("mouse_move", {
    to: {
      x: axis(event.clientX, rect.left, rect.width),
      y: axis(event.clientY, rect.top, rect.height),
    },
  });
}
screen.addEventListener("pointermove", event => {
  if (live) move(event);
});
screen.addEventListener("pointerdown", event => {
  if (!live) return;
  event.preventDefault();
  screen.focus();
  screen.setPointerCapture(event.pointerId);
  move(event);
  const button = ["left", "middle", "right"][event.button];
  if (button) {
    mouse.add(button);
    send("mouse_button", { button, state: true });
  }
});
screen.addEventListener("pointerup", event => {
  const button = ["left", "middle", "right"][event.button];
  if (mouse.delete(button)) send("mouse_button", { button, state: false });
});
screen.addEventListener("pointercancel", release);
screen.addEventListener("lostpointercapture", release);
screen.addEventListener("contextmenu", event => event.preventDefault());
screen.addEventListener(
  "wheel",
  event => {
    if (!live) return;
    event.preventDefault();
    send("mouse_wheel", {
      delta: { x: Math.sign(event.deltaX), y: -Math.sign(event.deltaY) },
    });
  },
  { passive: false }
);
screen.addEventListener("focus", () => {
  get("focus").textContent = "Keyboard captured";
});
screen.addEventListener("blur", release);
window.addEventListener("blur", release);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) release();
  else refreshFingerbot();
});
new ResizeObserver(fit).observe(viewport);
window.addEventListener("resize", fit);
get("disconnect").onclick = stop;
get("reconnect").onclick = start;
get("fullscreen").onclick = async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch {
    get("focus").textContent = "Full screen is unavailable in this browser";
  }
};
async function refreshFingerbot() {
  if (fingerbotBusy) return;
  const button = get("fingerbot");
  try {
    const response = await fetch(`${location.pathname}fingerbot`);
    if (!response.ok) throw new Error();
    const value = await response.json();
    duration = value.duration;
    strength = value.strength;
    button.hidden = value.enabled !== true;
    button.disabled = false;
    button.title = `Press physical power button for ${duration / 1000} s`;
  } catch {
    button.hidden = true;
    button.disabled = true;
  }
}
get("fingerbot").onclick = async () => {
  if (fingerbotBusy || get("fingerbot").disabled || get("fingerbot").hidden)
    return;
  release();
  if (
    !window.confirm(
      `Press ${host}'s physical power button for ${duration / 1000} s?\n\nThis can start or shut down the host, depending on its power state.`
    )
  )
    return;
  fingerbotBusy = true;
  get("fingerbot").disabled = true;
  const feedback = get("fingerbot-feedback");
  feedback.textContent = "Pressing Fingerbot…";
  try {
    const response = await fetch(
      `${location.pathname}fingerbot/press?duration=${duration}&strength=${strength}`,
      { method: "POST" }
    );
    if (!response.ok) throw new Error();
    feedback.textContent = "Fingerbot press accepted.";
  } catch {
    feedback.textContent =
      "Fingerbot request failed; it may have pressed. Check the host before trying again.";
  } finally {
    setTimeout(() => {
      fingerbotBusy = false;
      refreshFingerbot();
    }, 10000);
  }
};
const poll = setInterval(() => {
  if (!document.hidden) refreshFingerbot();
}, 15000);
window.addEventListener("pagehide", () => {
  stop();
  clearInterval(poll);
});
try {
  const response = await fetch(`${location.pathname}session`);
  if (!response.ok) throw new Error();
  ({ host } = await response.json());
  get("host").textContent = host;
  await refreshFingerbot();
  start();
} catch {
  status("Session expired. Open the launch URL printed by just open console.");
}
