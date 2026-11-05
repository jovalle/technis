const get = id => document.getElementById(id);
// shortcut: AMI viewer globals match ASMB10 firmware 1.1.40; verify the adapter after firmware upgrades.
const viewer = get("viewer"),
  buttons = [...document.querySelectorAll("[data-key]")];
if (window.self !== window.top) document.body.classList.add("embedded");
let wanted = false,
  timer,
  retry,
  connectTimer,
  generation = 0;
const held = new Map(),
  mouseButtons = new Set();
let mouse = { x: 0, y: 0 };
function status(text, live = false) {
  get("status").textContent = live ? "Connected" : text;
  get("notice").textContent = text;
  get("notice").hidden = live;
  buttons.forEach(button => {
    button.disabled = !live;
  });
  get("disconnect").disabled = !wanted;
}
function release() {
  const input = viewer.contentWindow?._video?.textbox;
  if (input)
    for (const [keyCode, location] of held)
      input.onkeyup?.(
        new KeyboardEvent("keyup", { keyCode, which: keyCode, location })
      );
  held.clear();
  const canvas = viewer.contentWindow?._video?.cursor_canvas;
  if (canvas)
    for (const which of mouseButtons)
      canvas.onmouseup?.({
        which,
        offsetX: mouse.x,
        offsetY: mouse.y,
        layerX: mouse.x,
        layerY: mouse.y,
        stopPropagation() {},
        preventDefault() {},
      });
  mouseButtons.clear();
  get("focus").textContent = "Click to control";
}
function stop() {
  wanted = false;
  generation++;
  clearInterval(timer);
  clearTimeout(retry);
  clearTimeout(connectTimer);
  release();
  viewer.hidden = true;
  viewer.src = "about:blank";
  get("take-control").hidden = true;
  status("Disconnected");
}
let host;
async function start() {
  stop();
  wanted = true;
  const attempt = generation;
  status("Connecting to ASMB10…");
  const path = `/asmb/${encodeURIComponent(host)}/viewer.html?generation=${attempt}`;
  try {
    const response = await fetch(path);
    if (attempt !== generation) return;
    if (!response.ok) {
      stop();
      status(
        response.status === 410
          ? "Session released. Run just open console again."
          : "Cannot reach ASMB10. Check LAN/VPN, certificate pin, and just vault ipmi/nexus/password."
      );
      return;
    }
  } catch {
    stop();
    status("Local relay disconnected. Run just open console again.");
    return;
  }
  viewer.hidden = false;
  viewer.src = path;
  connectTimer = setTimeout(() => {
    if (attempt === generation) {
      stop();
      status(
        "ASMB10 viewer did not finish connecting. Choose Reconnect to retry."
      );
    }
  }, 20000);
  timer = setInterval(() => {
    if (attempt !== generation) return;
    const video = viewer.contentWindow?._video;
    if (video?.running && video.sock?.validated) {
      clearTimeout(connectTimer);
      viewer.hidden = false;
      if (viewer.clientWidth > 16 && viewer.clientHeight > 16) {
        const scale = Math.min(
          (viewer.clientWidth - 16) / video.canvas.width,
          (viewer.clientHeight - 16) / video.canvas.height
        );
        video.videoScale = scale;
        for (const canvas of [video.canvas, video.cursor_canvas]) {
          canvas.style.width = `${canvas.width * scale}px`;
          canvas.style.height = `${canvas.height * scale}px`;
        }
        const scroll = viewer.contentDocument.getElementById("scroll_div");
        scroll.style.setProperty(
          "--console-width",
          `${video.canvas.width * scale}px`
        );
        scroll.style.setProperty(
          "--console-height",
          `${video.canvas.height * scale}px`
        );
        scroll.scrollLeft = scroll.scrollTop = 0;
      }
      status("", true);
      const master = viewer.contentWindow.master === true;
      get("status").textContent = master ? "Connected" : "View only";
      buttons.forEach(button => {
        button.disabled = !master;
      });
      get("take-control").hidden = master;
      get("resolution").textContent =
        `${video.canvas.width} × ${video.canvas.height}`;
    }
  }, 500);
}
window.addEventListener("message", async event => {
  if (
    event.origin !== location.origin ||
    event.source !== viewer.contentWindow ||
    !wanted
  )
    return;
  if (event.data?.generation !== String(generation)) return;
  if (event.data?.type === "asmb-error") {
    stop();
    status(event.data.error);
    return;
  }
  if (event.data?.type !== "asmb-close") return;
  if ([4001, 1001].includes(event.data.code)) {
    stop();
    status(
      event.data.code === 4001
        ? "Session released by another local console. Run just open console again to reconnect."
        : "Local console stopped. Run just open console again."
    );
    return;
  }
  status("Reconnecting…");
  retry = setTimeout(start, 3000);
});
const chords = {
  backspace: [8],
  ctrlc: [17, 67],
  escape: [27],
  tab: [9],
  f2: [113],
  f11: [122],
  cad: [17, 18, 46],
};
buttons.forEach(button =>
  button.addEventListener("click", () => {
    const input = viewer.contentWindow?._video?.textbox;
    if (!input) return;
    for (const type of ["keydown", "keyup"])
      for (const keyCode of type === "keyup"
        ? [...chords[button.dataset.key]].reverse()
        : chords[button.dataset.key])
        input[`on${type}`]?.(
          new KeyboardEvent(type, {
            keyCode,
            which: keyCode,
            location: [16, 17, 18].includes(keyCode) ? 1 : 0,
            bubbles: true,
            cancelable: true,
          })
        );
    input.focus();
  })
);
viewer.addEventListener("load", () => {
  if (!wanted) return;
  const doc = viewer.contentDocument;
  doc.addEventListener(
    "keydown",
    event => {
      if (event.code === "Escape" && event.shiftKey) {
        event.preventDefault();
        event.stopImmediatePropagation();
        release();
        get("reconnect").focus();
      } else held.set(event.keyCode, event.location ?? 0);
    },
    true
  );
  doc.addEventListener("keyup", event => held.delete(event.keyCode), true);
  doc.addEventListener("focusin", () => {
    get("focus").textContent = "Keyboard captured";
  });
  doc.addEventListener(
    "pointerdown",
    event => {
      if (event.target === viewer.contentWindow?._video?.cursor_canvas)
        event.target.setPointerCapture?.(event.pointerId);
    },
    true
  );
  doc.addEventListener(
    "mousedown",
    event => {
      if (event.target === viewer.contentWindow?._video?.cursor_canvas)
        mouseButtons.add(event.which);
    },
    true
  );
  doc.addEventListener(
    "mousemove",
    event => {
      if (event.target === viewer.contentWindow?._video?.cursor_canvas)
        mouse = { x: event.offsetX, y: event.offsetY };
    },
    true
  );
  doc.addEventListener(
    "mouseup",
    event => mouseButtons.delete(event.which),
    true
  );
  viewer.contentWindow.addEventListener("blur", release);
});
window.addEventListener("blur", release);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) release();
});
window.addEventListener("pagehide", stop);
get("disconnect").onclick = stop;
get("reconnect").onclick = start;
get("take-control").onclick = () =>
  viewer.contentDocument.getElementById("request_full")?.click();
get("fullscreen").onclick = async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch {
    get("focus").textContent = "Full screen is unavailable in this browser";
  }
};
try {
  const response = await fetch(`${location.pathname}session`);
  if (!response.ok) throw new Error();
  ({ host } = await response.json());
  get("host").textContent = host;
  await start();
} catch {
  status("Session expired. Open the launch URL printed by just open console.");
}
