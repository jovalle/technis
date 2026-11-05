const special = {
  Backspace: 0xff08, Tab: 0xff09, Enter: 0xff0d, Escape: 0xff1b, Delete: 0xffff,
  Home: 0xff50, End: 0xff57, ArrowLeft: 0xff51, ArrowUp: 0xff52,
  ArrowRight: 0xff53, ArrowDown: 0xff54, PageUp: 0xff55, PageDown: 0xff56,
  Insert: 0xff63, ShiftLeft: 0xffe1, ShiftRight: 0xffe2,
  ControlLeft: 0xffe3, ControlRight: 0xffe4, AltLeft: 0xffe9, AltRight: 0xffea,
  MetaLeft: 0xffeb, MetaRight: 0xffec, CapsLock: 0xffe5,
  NumLock: 0xff7f, ScrollLock: 0xff14, PrintScreen: 0xff61, Pause: 0xff13,
};

export function keysym(event) {
  if (special[event.code]) return special[event.code];
  if (/^F([1-9]|1[0-2])$/.test(event.code)) return 0xffbd + Number(event.code.slice(1));
  if (event.code === 'NumpadEnter') return 0xff0d;
  if (event.key?.length === 1 && event.key.charCodeAt(0) <= 255) return event.key.charCodeAt(0);
  return null;
}

export function keyboard(send) {
  const held = new Map();
  return {
    down(event) {
      const symbol = held.get(event.code) ?? keysym(event);
      if (symbol === null) return;
      held.set(event.code, symbol);
      send(symbol, 1);
    },
    up(event) {
      const symbol = held.get(event.code);
      if (symbol === undefined) return;
      send(symbol, 0);
      held.delete(event.code);
    },
    release() {
      for (const symbol of [...held.values()].reverse()) send(symbol, 0);
      held.clear();
    },
    chord(symbols) {
      this.release();
      for (const symbol of symbols) send(symbol, 1);
      for (const symbol of [...symbols].reverse()) send(symbol, 0);
    },
  };
}

export function pointer(event, rect, width, height) {
  return {
    x: Math.max(0, Math.min(width - 1, Math.floor((event.clientX - rect.left) * width / rect.width))),
    y: Math.max(0, Math.min(height - 1, Math.floor((event.clientY - rect.top) * height / rect.height))),
    buttons: (event.buttons & 1) | ((event.buttons & 2) << 1) | ((event.buttons & 4) >> 1),
  };
}
