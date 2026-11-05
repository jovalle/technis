const get = id => document.getElementById(id);
const controls = get("power-controls"),
  state = get("power-state"),
  feedback = get("power-feedback");
const buttons = [...controls.querySelectorAll("button[data-power]")];
let busy = false,
  released = false,
  host;

async function refresh() {
  if (busy || released) return;
  try {
    const response = await fetch(`${location.pathname}power`);
    if (response.status === 410 || response.status === 403) released = true;
    if (!response.ok) throw new Error();
    const result = await response.json();
    if (busy || released) return;
    state.textContent = result.state;
    for (const button of buttons) {
      button.hidden = !result.actions.includes(button.dataset.power);
      button.disabled = false;
    }
  } catch {
    state.textContent = "unknown";
    buttons.forEach(button => {
      button.disabled = true;
    });
    feedback.textContent = released
      ? "Session released or expired. Run just open console again."
      : "Power status unavailable. Check LAN/VPN, credentials and certificate pin.";
  }
}

for (const button of buttons)
  button.addEventListener("click", async () => {
    if (busy || released) return;
    const warning = ["off", "restart", "cycle"].includes(button.dataset.power)
      ? "This skips OS shutdown and can lose unsaved data."
      : button.dataset.power === "shutdown"
        ? "This requests an orderly OS shutdown."
        : "This starts the host.";
    if (!window.confirm(`${button.textContent} ${host}?\n\n${warning}`)) return;
    busy = true;
    buttons.forEach(item => {
      item.disabled = true;
    });
    get("power-refresh").disabled = true;
    feedback.textContent = `Sending ${button.textContent.toLowerCase()} to ${host}…`;
    try {
      const response = await fetch(
        `${location.pathname}power/${button.dataset.power}`,
        { method: "POST" }
      );
      if (!response.ok) throw new Error();
      feedback.textContent = `${button.textContent} accepted by ${host}. Check power state to confirm completion.`;
    } catch {
      feedback.textContent =
        "Power request failed; it may have reached the host. Refresh status before trying again.";
    } finally {
      state.textContent = "unknown";
      // AMT needs time to finish a transition; never automatically repeat a power command.
      setTimeout(async () => {
        busy = false;
        get("power-refresh").disabled = false;
        await refresh();
      }, 10000);
    }
  });
get("power-refresh").addEventListener("click", () => {
  feedback.textContent = "";
  refresh();
});
controls.addEventListener("toggle", () => {
  if (controls.open) refresh();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
const poll = setInterval(() => {
  if (!document.hidden) refresh();
}, 15000);
window.addEventListener("pagehide", () => clearInterval(poll));
try {
  const response = await fetch(`${location.pathname}session`);
  if (!response.ok) throw new Error();
  ({ host } = await response.json());
  if (typeof host !== "string" || !host) throw new Error();
  await refresh();
} catch {
  released = true;
  feedback.textContent = "Session expired. Run just open console again.";
}
