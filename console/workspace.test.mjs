import { test } from 'node:test';
import assert from 'node:assert/strict';

test('host tabs keep all viewers mounted and support keyboard navigation', async () => {
  let focused;
  class Element extends EventTarget {
    children = [];
    attributes = {};
    contentWindow = new EventTarget();
    append(child) { this.children.push(child); }
    setAttribute(name, value) { this.attributes[name] = value; }
    focus() { focused = this; }
  }
  const elements = new Map(['hosts', 'consoles', 'workspace-notice'].map(id => [id, new Element()]));
  Object.assign(globalThis, {
    document: { getElementById: id => elements.get(id), createElement: () => new Element() },
    fetch: async () => ({ ok: true, json: async () => ({ hosts: ['ms-01', 'ms-02', 'ms-03'] }) }),
  });
  await import('./workspace.mjs');
  const tabs = elements.get('hosts').children, panels = elements.get('consoles').children;
  const frames = panels.map(panel => panel.children[0]);
  assert.deepEqual(frames.map(frame => frame.src), ['/console/ms-01/', '/console/ms-02/', '/console/ms-03/']);
  assert.deepEqual(panels.map(panel => panel.hidden), [false, true, true]);
  assert.equal(elements.get('workspace-notice').hidden, true);
  let resized = 0;
  frames[2].contentWindow.addEventListener('resize', () => resized++);
  tabs[2].dispatchEvent(new Event('click'));
  assert.equal(resized, 1, 'selecting a hidden viewer refits its canvas without reconnecting');
  assert.deepEqual(tabs.map(tab => tab.attributes['aria-selected']), ['false', 'false', 'true']);
  assert.deepEqual(tabs.map(tab => tab.tabIndex), [-1, -1, 0]);
  assert.deepEqual(panels.map(panel => panel.hidden), [true, true, false]);
  for (const [from, key, to] of [[2, 'ArrowRight', 0], [0, 'ArrowLeft', 2], [2, 'Home', 0], [0, 'End', 2]]) {
    const event = Object.assign(new Event('keydown', { cancelable: true }), { key });
    tabs[from].dispatchEvent(event);
    assert.equal(event.defaultPrevented, true);
    assert.equal(focused, tabs[to]);
    assert.equal(panels[to].hidden, false);
  }
  assert.deepEqual(panels.map(panel => panel.children[0]), frames, 'switching tabs never reloads the consoles');
});
