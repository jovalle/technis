import importlib.util
import json
import os
import pty
import fcntl
import termios
import struct
import time
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

PATH = Path(__file__).with_name('compose_progress.py')


def load():
    spec = importlib.util.spec_from_file_location('compose_progress', PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ProgressTest(unittest.TestCase):
    def test_states_and_errors_are_retained(self):
        p = load().Progress()
        for event in [
            {'id': 'Container web-1', 'text': 'Creating', 'status': 'Working'},
            {'id': 'Container web-1', 'text': 'Created', 'status': 'Done'},
            {'id': 'Container web-1', 'text': 'Starting', 'status': 'Working'},
            {'id': 'Container web-1', 'text': 'Started', 'status': 'Done'},
            {'id': 'Container db-1', 'text': 'Waiting', 'status': 'Working'},
            {'id': 'step', 'parent_id': 'Container db-1', 'text': 'unhealthy', 'status': 'Error'},
            {'id': 'Container db-1', 'text': 'Exited', 'status': 'Done'},
        ]:
            p.accept(json.dumps(event))
        self.assertEqual(list(p.resources), ['Container web-1', 'Container db-1'])
        self.assertEqual(p.resources['Container web-1'].text, 'Started')
        self.assertTrue(p.resources['Container db-1'].failed)
        self.assertIn('unhealthy', p.final())
        self.assertIn('Exited', p.final())

    def test_layout_fits_and_keeps_every_name_across_pages(self):
        m = load()
        entries = [f'◌ service-{i} Creating' for i in range(17)]
        pages = m.layout(entries, 55, 6)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 4 for page in pages))
        self.assertTrue(all(m.width(line) < 55 for page in pages for line in page))
        self.assertEqual(sorted(' '.join(sum(pages, [])).count(f'service-{i} ') for i in range(17)), [1] * 17)

    def test_long_unicode_entries_wrap_without_losing_content(self):
        m = load()
        name = '◌ Container 音楽非常に長い名前サービス  Starting'
        pages = m.layout([name], 12, 5)
        self.assertGreater(len(pages), 1)
        self.assertEqual(''.join(sum(pages, [])), name)
        self.assertTrue(all(m.width(row) < 12 for page in pages for row in page))

    def test_unknown_and_global_errors_remain_in_final_record(self):
        p = load().Progress()
        for event in [
            {'id':'Network shared', 'text':'Creating', 'status':'Working'},
            {'id':'Volume shared', 'text':'Created', 'status':'Done'},
            {'error':True, 'message':'configuration failed'},
            {'id':'Container orphan', 'text':'mysterious', 'status':'FutureStatus'},
        ]:
            p.accept(json.dumps(event))
        self.assertEqual(len(p.resources), 3)
        self.assertIn('configuration failed', p.final())
        self.assertIn('mysterious', p.final())
        self.assertIn('Network shared', p.final())
        self.assertIn('Volume shared', p.final())

    def test_entries_and_errors_sort_alphabetically_by_name(self):
        p = load().Progress()
        for ident in ('Container Zebra', 'Network apple', 'Container Beta'):
            p.accept(json.dumps({'id': ident, 'text': 'Error', 'status': 'Error'}))
        entries = p.entries()
        self.assertEqual([e.split('  ')[0] for e in entries],
                         ['✗ Network apple', '✗ Container Beta', '✗ Container Zebra'])
        final = p.final()
        self.assertLess(final.index('Network apple:'), final.index('Container Beta:'))
        self.assertLess(final.index('Container Beta:'), final.index('Container Zebra:'))

    def test_stack_prefix_is_removed_for_display_but_not_identity(self):
        p = load().Progress(stack='beacon')
        for name in ('Container beacon-nut-exporter-1-1', 'Container alloy',
                     'Container beacon-nut-exporter-2-1', 'Network beacon_default'):
            p.accept(json.dumps({'id': name, 'text': 'Running', 'status': 'Done'}))
        self.assertIn('Container beacon-nut-exporter-1-1', p.resources)
        self.assertEqual(p.entries(), ['✓ Container alloy  Running', '✓ Network beacon_default  Running',
                                     '✓ Container nut-exporter-1-1  Running', '✓ Container nut-exporter-2-1  Running'])

    def test_similar_display_names_do_not_hide_distinct_resources(self):
        p = load().Progress(stack='beacon')
        for name in ('Container beacon-app-1', 'Container app-1'):
            p.accept(json.dumps({'id': name, 'text': 'Error', 'status': 'Error'}))
        self.assertIn('Container beacon-app-1', p.final())
        self.assertIn('Container app-1', p.final())
        p2 = load().Progress(stack='beacon')
        p2.accept(json.dumps({'id':'Container beacon-failed-1', 'text':'Error', 'status':'Error'}))
        self.assertIn('  Container failed-1:', p2.final())
        self.assertNotIn('Container beacon-failed-1', p2.final())

    def test_color_does_not_change_layout_width_or_wrapping(self):
        m = load()
        p = m.Progress()
        p.accept(json.dumps({'id': 'Container web', 'text':'Healthy', 'status':'Done'}))
        plain = p.entries()[0]
        styled = p.entries(color=True)[0]
        self.assertIn('\x1b[32m✓', styled)
        self.assertIn('\x1b[38;5;118mHealthy', styled)
        self.assertEqual(m.width(styled), m.width(plain))
        pages = m.layout([styled], 10, 5)
        self.assertEqual(''.join(m.ESCAPE.sub('', row) for row in sum(pages, [])), plain)
        self.assertTrue(all(m.width(row) < 10 for page in pages for row in page))

    def test_lifecycle_states_each_have_distinct_colors(self):
        m = load()
        states = ['Creating', 'Created', 'Recreating', 'Recreated', 'Starting', 'Started',
                  'Running', 'Healthy', 'Waiting', 'Unhealthy', 'Exited', 'Error', 'Failed',
                  'Stopping', 'Stopped', 'Removing', 'Removed', 'Pulling', 'Pulled',
                  'Building', 'Built', 'Warning', 'Skipped', 'Unknown', 'Pending',
                  'Restarting', 'Paused', 'Dead', 'Downloading', 'Extracting',
                  'Done', 'Up to date', 'Pull complete', 'Already exists']
        colors = [m.state_color(state, '') for state in states]
        self.assertEqual(len(set(colors)), len(states))
        self.assertEqual(m.state_color('Running', ''), '38;5;34')
        self.assertEqual(m.state_color('Healthy', ''), '38;5;118')
        self.assertEqual(m.state_color('running', ''), m.state_color('Running', ''))

    def test_color_preferences_are_respected(self):
        m = load()
        with mock.patch.object(m.sys.stderr, 'isatty', return_value=True):
            with mock.patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
                self.assertTrue(m.colors_enabled())
                for variable, value in [('NO_COLOR', ''), ('JSH_COLOR', 'never'), ('JSH_PLAIN_OUTPUT', '1')]:
                    with mock.patch.dict(os.environ, {variable: value}):
                        self.assertFalse(m.colors_enabled())
            with mock.patch.dict(os.environ, {'TERM':'dumb', 'JSH_COLOR':'always'}, clear=True):
                self.assertFalse(m.colors_enabled())

    def test_terminal_final_output_has_status_colors(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 10, 80, 0, 0))
        env = {k: v for k, v in os.environ.items() if k not in ('NO_COLOR', 'JSH_COLOR', 'JSH_PLAIN_OUTPUT')}
        env['TERM'] = 'xterm'
        proc = subprocess.Popen([sys.executable, str(PATH)], stdin=subprocess.PIPE, stderr=slave,
                                stdout=subprocess.DEVNULL, env=env)
        os.close(slave)
        events = [{'id':'Container web', 'text':'Healthy', 'status':'Done'},
                  {'id':'Container db', 'text':'Error', 'status':'Error'}]
        proc.communicate(('\n'.join(map(json.dumps, events)) + '\n').encode(), timeout=5)
        data = b''
        while True:
            try: data += os.read(master, 4096)
            except OSError: break
        os.close(master)
        self.assertIn(b'\x1b[32m', data)
        self.assertIn(b'\x1b[38;5;118mHealthy', data)
        self.assertIn(b'\x1b[31m', data)
        final_list = data.split(b'Compose resources:')[-1].split(b'Errors:')[0]
        self.assertLess(final_list.index(b'Container db'), final_list.index(b'Container web'))

    def test_live_pty_repaints_and_finalizes(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 7, 52, 0, 0))
        proc = subprocess.Popen([sys.executable, str(PATH)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=slave, env={**os.environ, 'TERM': 'xterm'})
        os.close(slave)
        proc.stdin.write((json.dumps({'id':'Container web', 'text':'Creating', 'status':'Working'}) + '\n').encode())
        proc.stdin.flush()
        time.sleep(.35)
        initial = os.read(master, 4096).decode()
        self.assertIn('Creating', initial)
        proc.stdin.write((json.dumps({'id':'Container web', 'text':'Started', 'status':'Done'}) + '\n').encode())
        proc.stdin.close()
        proc.wait(timeout=5)
        output = b''
        while True:
            try:
                part = os.read(master, 4096)
                if not part: break
                output += part
            except OSError:
                break
        os.close(master)
        proc.stdout.close()
        self.assertEqual(proc.returncode, 0)
        self.assertIn('Started', output.decode())
        self.assertIn('Compose resources:', output.decode())

    def test_terminal_resize_redraws_without_new_event(self):
        import select
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 8, 60, 0, 0))
        proc = subprocess.Popen([sys.executable, str(PATH)], stdin=subprocess.PIPE, stderr=slave,
                                stdout=subprocess.DEVNULL, env={**os.environ, 'TERM':'xterm'})
        proc.stdin.write((json.dumps({'id':'Container web', 'text':'Creating', 'status':'Working'}) + '\n').encode())
        proc.stdin.flush()
        self.assertTrue(select.select([master], [], [], 2)[0])
        time.sleep(.25)
        os.read(master, 4096)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 6, 35, 0, 0))
        self.assertTrue(select.select([master], [], [], 2)[0])
        time.sleep(.3)
        redraw = os.read(master, 4096).decode()
        proc.stdin.close()
        proc.wait(timeout=5)
        os.close(master)
        os.close(slave)
        self.assertIn('Container web', redraw)

    def test_plain_stream_reports_changes_before_eof(self):
        proc = subprocess.Popen([sys.executable, str(PATH)], stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, env={**os.environ, 'TERM':'dumb'})
        proc.stdin.write((json.dumps({'id':'Container web', 'text':'Creating', 'status':'Working'}) + '\n').encode())
        proc.stdin.flush()
        import select
        self.assertTrue(select.select([proc.stderr], [], [], 2)[0])
        self.assertIn(b'Creating', os.read(proc.stderr.fileno(), 4096))
        proc.stdin.close()
        proc.wait(timeout=5)
        proc.stderr.close()

    def test_compose_up_preserves_stdout_and_failure_status(self):
        script = PATH.with_name('stacks')
        command = r"""source <(sed '$d' "$1")
root=$2
compose() { printf 'compose stdout\n'; printf '%s\n' '{"id":"Container web","text":"Creating","status":"Working"}' >&2; return 7; }
compose_up example -d
"""
        proc = subprocess.run(['bash', '-c', command, 'test', str(script), str(PATH.parents[2])],
                              capture_output=True, text=True, env={**os.environ, 'TERM':'dumb'})
        self.assertEqual(proc.returncode, 7, proc.stderr)
        self.assertEqual(proc.stdout, 'compose stdout\n')
        self.assertIn('Container web', proc.stderr)

    def test_renderer_failure_does_not_interrupt_compose(self):
        script = PATH.with_name('stacks')
        command = r"""source <(sed '$d' "$1")
root=$2
python3() { return 23; }
compose() { for ((i=0; i<2000; i++)); do printf '{"id":"Container s%s","text":"Started","status":"Done"}\n' "$i" >&2; done; return 7; }
compose_up example -d
"""
        proc = subprocess.run(['bash', '-c', command, 'test', str(script), str(PATH.parents[2])],
                              capture_output=True, text=True, timeout=10, env={**os.environ, 'TERM':'dumb'})
        self.assertEqual(proc.returncode, 7, proc.stderr[-1000:])
        self.assertIn('Container s1999', proc.stderr)
        self.assertIn('progress display failed', proc.stderr)

    def test_plain_output_retains_unknown_and_sanitizes_control(self):
        inp = '\n'.join([json.dumps({'id': 'Network app', 'text': 'Created', 'status': 'Done'}),
                         json.dumps({'level':'error', 'msg':'bad\x1b[31m thing'}), 'unexpected text']) + '\n'
        proc = subprocess.run([sys.executable, str(PATH)], input=inp, text=True, capture_output=True, env={**os.environ, 'TERM':'dumb'})
        self.assertEqual(proc.returncode, 0)
        self.assertIn('Network app', proc.stderr)
        self.assertIn('unexpected text', proc.stderr)
        self.assertIn('bad', proc.stderr)
        self.assertNotIn('\x1b', proc.stderr)
        self.assertEqual(proc.stdout, '')

if __name__ == '__main__':
    unittest.main()
