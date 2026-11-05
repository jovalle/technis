#!/usr/bin/env python3
"""Render Docker Compose's JSON progress stream on stderr."""
import json
import os
import re
import select
import sys
import time
import unicodedata
from dataclasses import dataclass, field

ESCAPE = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|.)')
LIMIT = 4096


def clean(value):
    text = ESCAPE.sub('', str(value))
    return ''.join(' ' if unicodedata.category(ch).startswith('C') else ch for ch in text).strip()


def width(text):
    text = ESCAPE.sub('', text)
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


def wrap(text, available):
    result, line, active = [], '', ''
    tokens = re.split(r'(\x1b\[[0-9;]*m)', text)
    for token in tokens:
        if token.startswith('\x1b['):
            line += token
            active = '' if token == '\x1b[0m' else token
            continue
        for char in token:
            if width(line) and width(line + char) > available:
                result.append(line + ('\x1b[0m' if active else ''))
                line = active
            line += char
    if width(line):
        result.append(line + ('\x1b[0m' if active else ''))
    return result or ['']


def paint(text, code, enabled):
    return f'\x1b[{code}m{text}\x1b[0m' if enabled else text


def resource_key(ident):
    name = re.sub(r'^(?:Container|Network|Volume|Image) ', '', ident, flags=re.IGNORECASE)
    return (name.casefold(), ident.casefold(), ident)


# ANSI 256-color palette: each known Compose lifecycle state has its own hue.
STATE_COLORS = {
    'creating': 45, 'created': 39, 'recreating': 81, 'recreated': 75,
    'starting': 214, 'started': 40, 'running': 34, 'healthy': 118,
    'waiting': 220, 'unhealthy': 196, 'exited': 166, 'error': 160,
    'failed': 197, 'stopping': 208, 'stopped': 173, 'removing': 135,
    'removed': 141, 'pulling': 33, 'pulled': 69, 'building': 99,
    'built': 105, 'warning': 178, 'skipped': 244, 'unknown': 250,
    'pending': 222, 'restarting': 215, 'paused': 180, 'dead': 124,
    'downloading': 32, 'extracting': 37, 'done': 114, 'up to date': 72,
    'pull complete': 79, 'already exists': 109,
}


def state_color(state, status):
    name = state.casefold().strip()
    fallback = status.casefold().strip()
    return f"38;5;{STATE_COLORS.get(name, STATE_COLORS.get(fallback, STATE_COLORS['unknown']))}"


def colors_enabled():
    return (sys.stderr.isatty() and os.environ.get('TERM') not in ('', 'dumb')
            and os.environ.get('JSH_PLAIN_OUTPUT') != '1'
            and os.environ.get('JSH_COLOR') != 'never' and 'NO_COLOR' not in os.environ)


def layout(entries, columns, lines):
    """Return pages of rows, reserving two rows for heading and footer."""
    usable = max(1, columns - 1)  # avoid terminal autowrap at the last column
    height = max(1, lines - 2)
    max_width = max((width(entry) for entry in entries), default=1)
    count = max(1, (usable + 2) // (max_width + 2))
    if count == 1:
        rows = [row for entry in entries for row in wrap(entry, usable)]
    else:
        rows = []
        for start in range(0, len(entries), count):
            cells = entries[start:start + count]
            rows.append('  '.join(cell + ' ' * (max_width - width(cell)) for cell in cells).rstrip())
    return [rows[i:i + height] for i in range(0, len(rows), height)] or [[]]


@dataclass
class Resource:
    text: str = 'Unknown'
    status: str = ''
    failed: bool = False
    errors: list = field(default_factory=list)


class Progress:
    def __init__(self, stack=""):
        self.stack = stack
        self.resources = {}
        self.messages = []
        self.dropped = 0

    def message(self, value):
        value = clean(value)
        if not value:
            return
        if len(value) > LIMIT:
            value = value[:LIMIT] + ' [message truncated]'
        if len(self.messages) < 1000:
            self.messages.append(value)
        else:
            self.dropped += 1

    def accept(self, line):
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            self.message(line)
            return
        if not isinstance(event, dict):
            self.message(line)
            return
        if event.get('level') is not None:
            self.message(f"{event.get('level')}: {event.get('msg', '')}")
            return
        if event.get('error'):
            self.message(event.get('message') if event['error'] is True else event['error'])
            return
        ident = event.get('parent_id') or event.get('id')
        if not isinstance(ident, str) or not isinstance(event.get('text'), str):
            self.message(line)
            return
        ident = clean(ident)
        if not ident:
            self.message(line)
            return
        resource = self.resources.setdefault(ident, Resource())
        state = clean(event['text']) or 'Unknown'
        status = clean(event.get('status') or '')
        detail = clean(event.get('details') or '')
        if not event.get('parent_id'):
            resource.text, resource.status = state, status
        if status.lower() in ('error', 'fatal'):
            resource.failed = True
            message = (state + (' ' + detail if detail else ''))[:LIMIT]
            if message not in resource.errors and len(resource.errors) < 100:
                resource.errors.append(message)
            elif message not in resource.errors:
                self.dropped += 1
        elif status.lower() == 'warning':
            self.message(f'{ident}: {state}' + (f' {detail}' if detail else ''))

    def display_names(self):
        names = {}
        prefix = f'Container {self.stack}-'
        for ident in self.resources:
            names[ident] = ('Container ' + ident[len(prefix):]
                            if self.stack and ident.startswith(prefix) else ident)
        counts = {}
        for name in names.values():
            counts[name] = counts.get(name, 0) + 1
        return {ident: ident if counts[name] > 1 else name for ident, name in names.items()}

    def entries(self, color=False):
        entries = []
        names = self.display_names()
        for ident in sorted(self.resources, key=lambda ident: resource_key(names[ident])):
            r = self.resources[ident]
            mark = '✗' if r.failed else '!' if r.status.lower() == 'warning' else '◌' if r.status.lower() == 'working' else '✓' if r.status.lower() in ('done', 'completed') else '–'
            shade = '31' if r.failed else '33' if r.status.lower() == 'warning' else '32' if mark == '✓' else '36' if mark == '◌' else '2;37'
            state_shade = state_color(r.text, r.status)
            entries.append(f'{paint(mark, shade, color)} {paint(names[ident], "1", color)}  {paint(r.text, state_shade, color)}')
        return entries

    def final(self, color=False):
        failed = sum(r.failed for r in self.resources.values())
        done = sum(not r.failed and r.status.lower() in ('done', 'completed') for r in self.resources.values())
        pending = len(self.resources) - failed - done
        out = [f'Compose resources: {done} reported done · {failed} failed · {pending} in progress or unknown']
        out.extend('  ' + entry for entry in self.entries(color))
        if not self.resources:
            out.append('  No resource progress reported.')
        names = self.display_names()
        failures = [(names[name], self.resources[name]) for name in sorted(self.resources, key=lambda ident: resource_key(names[ident])) if self.resources[name].failed]
        if failures:
            out.append('\nErrors:')
            for name, resource in failures:
                out.append(f'  {name}:')
                out.extend('    Compose: ' + error for error in resource.errors)
                out.append('    Container logs: not collected')
        if self.messages:
            out.append('\nCompose messages:')
            out.extend('  ' + message for message in self.messages)
        if self.dropped:
            out.append(f'  [{self.dropped} additional messages omitted]')
        return '\n'.join(out)


def main():
    progress = Progress(stack=sys.argv[1] if len(sys.argv) > 1 else "")
    interactive = sys.stderr.isatty() and os.environ.get('TERM') not in ('', 'dumb')
    color = colors_enabled()
    drawn = 0
    last_draw = 0
    page = 0
    last_page = time.monotonic()
    pending = b''
    dirty = True
    dimensions = None

    def clear():
        nonlocal drawn
        if drawn:
            sys.stderr.write(f'\x1b[{drawn}A' + ''.join('\r\x1b[2K\x1b[1B' for _ in range(drawn)) + f'\x1b[{drawn}A\r')
            drawn = 0

    def render(now):
        nonlocal drawn, page, last_page, last_draw, dirty, dimensions
        try:
            size = os.get_terminal_size(sys.stderr.fileno())
        except OSError:
            size = os.terminal_size((80, 24))
        if size.lines < 4 or size.columns < 8:
            clear()
            dimensions = (size.columns, size.lines)
            return
        if dimensions != (size.columns, size.lines):
            clear()
            dimensions = (size.columns, size.lines)
            page = 0
            dirty = True
        pages = layout(progress.entries(color), size.columns, size.lines)
        if now - last_page >= 3 and len(pages) > 1:
            page = (page + 1) % len(pages)
            last_page = now
            dirty = True
        page %= len(pages)
        if not dirty or now - last_draw < .1:
            return
        clear()
        header = f'Compose progress: {len(progress.resources)} resources'
        footer = f'Page {page + 1}/{len(pages)} · more resources on other pages' if len(pages) > 1 else 'Waiting for Compose…'
        lines = [wrap(header, size.columns - 1)[0], *pages[page], wrap(footer, size.columns - 1)[0]]
        sys.stderr.write(''.join('\r\x1b[2K' + line + '\n' for line in lines))
        sys.stderr.flush()
        drawn = len(lines)
        last_draw = now
        dirty = False

    try:
        while True:
            ready, _, _ = select.select([sys.stdin.fileno()], [], [], .15)
            if ready:
                chunk = os.read(sys.stdin.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                while b'\n' in pending:
                    raw, pending = pending.split(b'\n', 1)
                    line = raw.decode('utf-8', 'replace')
                    before = progress.entries() if not interactive else None
                    old_messages = len(progress.messages)
                    progress.accept(line)
                    if not interactive:
                        after = progress.entries()
                        for entry in after:
                            if entry not in before:
                                print(entry, file=sys.stderr, flush=True)
                        for message in progress.messages[old_messages:]:
                            print(message, file=sys.stderr, flush=True)
                    dirty = True
            if interactive:
                render(time.monotonic())
        if pending:
            progress.accept(pending.decode('utf-8', 'replace'))
    finally:
        if interactive:
            clear()
        sys.stderr.write(progress.final(color) + '\n')
        sys.stderr.flush()


if __name__ == '__main__':
    main()
