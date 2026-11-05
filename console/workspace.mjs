const hosts = document.getElementById("hosts");
const consoles = document.getElementById("consoles");
const notice = document.getElementById("workspace-notice");

try {
  const response = await fetch("/session");
  if (!response.ok) throw new Error();
  const session = await response.json();
  const tabs = [],
    panels = [];
  function select(index) {
    tabs.forEach((tab, i) => {
      tab.setAttribute("aria-selected", String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
      panels[i].hidden = i !== index;
    });
    // Hidden iframes can miss ResizeObserver updates while firmware changes modes.
    panels[index].children[0].contentWindow.dispatchEvent(new Event("resize"));
  }
  for (const [i, host] of session.hosts.entries()) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.id = `host-${i}`;
    tab.textContent = host;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", `console-${i}`);
    const panel = document.createElement("div");
    panel.id = `console-${i}`;
    panel.className = "console-panel";
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
    const frame = document.createElement("iframe");
    frame.src = `/console/${encodeURIComponent(host)}/`;
    frame.title = `${host} hardware console`;
    frame.allowFullscreen = true;
    panel.append(frame);
    tabs.push(tab);
    panels.push(panel);
    tab.addEventListener("click", () => select(i));
    tab.addEventListener("keydown", event => {
      const next = {
        ArrowRight: (i + 1) % tabs.length,
        ArrowLeft: (i + tabs.length - 1) % tabs.length,
        Home: 0,
        End: tabs.length - 1,
      }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      select(next);
      tabs[next].focus();
    });
    hosts.append(tab);
    consoles.append(panel);
  }
  select(0);
  notice.hidden = true;
} catch {
  notice.textContent =
    "Session expired. Open the launch URL printed by just open console.";
}
