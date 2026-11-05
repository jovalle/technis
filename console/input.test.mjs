import { test } from 'node:test';
import assert from 'node:assert/strict';
import { keyboard, keysym, pointer } from './input.mjs';

test('Backspace, control shortcuts, shifted punctuation and function keys map to RFB keysyms', () => {
  for (const [code, key, expected] of [
    ['Backspace', 'Backspace', 0xff08], ['Delete', 'Delete', 0xffff],
    ['ControlLeft', 'Control', 0xffe3], ['KeyC', 'c', 99], ['KeyC', 'C', 67],
    ['Minus', '-', 45], ['Minus', '_', 95], ['Digit1', '!', 33],
    ['Tab', 'Tab', 0xff09], ['Enter', 'Enter', 0xff0d], ['Escape', 'Escape', 0xff1b],
    ['ArrowLeft', 'ArrowLeft', 0xff51], ['F12', 'F12', 0xffc9],
    ['NumpadEnter', 'Enter', 0xff0d], ['MetaLeft', 'Meta', 0xffeb],
  ]) assert.equal(keysym({ code, key }), expected, code);
  assert.equal(keysym({ code: 'Unidentified', key: 'Process' }), null);
});

test('key-up uses the original symbol even when Shift is released first; blur releases all held keys', () => {
  const sent = [], keys = keyboard((...args) => sent.push(args));
  keys.down({ code: 'ShiftLeft', key: 'Shift' });
  keys.down({ code: 'KeyA', key: 'A' });
  keys.up({ code: 'ShiftLeft', key: 'Shift' });
  keys.up({ code: 'KeyA', key: 'a' });
  keys.down({ code: 'ControlLeft', key: 'Control' });
  keys.down({ code: 'KeyC', key: 'c' });
  keys.release(); keys.release();
  assert.deepEqual(sent, [[0xffe1, 1], [65, 1], [0xffe1, 0], [65, 0], [0xffe3, 1], [99, 1], [99, 0], [0xffe3, 0]]);
});

test('toolbar Ctrl-C sends both press and release and repeat preserves matching key-up', () => {
  const sent = [], keys = keyboard((...args) => sent.push(args));
  keys.down({ code: 'Backspace', key: 'Backspace' });
  keys.down({ code: 'Backspace', key: 'Backspace', repeat: true });
  keys.chord([0xffe3, 99]);
  assert.deepEqual(sent, [[0xff08, 1], [0xff08, 1], [0xff08, 0], [0xffe3, 1], [99, 1], [99, 0], [0xffe3, 0]]);
});

test('scaled pointer coordinates clamp at the edges and map right/middle buttons correctly', () => {
  const rect = { left: 10, top: 20, width: 100, height: 50 };
  assert.deepEqual(pointer({ clientX: 60, clientY: 45, buttons: 2 }, rect, 1920, 1080), { x: 960, y: 540, buttons: 4 });
  assert.deepEqual(pointer({ clientX: -10, clientY: 99, buttons: 4 }, rect, 1920, 1080), { x: 0, y: 1079, buttons: 2 });
});
