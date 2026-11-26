import os
from pathlib import Path
import subprocess
import unittest


SCRIPT = Path(__file__).with_name('stacks')


def run_script(body):
    return subprocess.run(
        ['bash', '-c', 'source <(sed \'/^# Bash parses the whole group/,$d\' "$1")\n' + body,
         'test', str(SCRIPT)],
        capture_output=True, text=True, env={**os.environ, 'TERM': 'dumb'},
    )


class StacksTest(unittest.TestCase):
    def test_status_displays_stopped_containers(self):
        result = run_script(r'''
targets=(nexus)
compose() {
  [[ " $* " == *" --all "* ]] || return 1
  printf '%s\n' '{"Service":"immich","State":"exited","Health":"","Status":"Exited (143)","Image":"immich"}'
}
ps_table
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('immich', result.stdout)
        self.assertIn('Exited (143)', result.stdout)
        self.assertIn('not healthy', result.stderr)

    def test_deploy_and_update_wait_and_propagate_failure(self):
        for verb in ('deploy', 'upgrade'):
            with self.subTest(verb=verb):
                result = run_script(r'''
stacks() { echo nexus; }
has() { return 0; }
preflight() { return 0; }
ensure_ctx() { return 0; }
guard_releases() { :; }
endpoint() { :; }
compose() {
  printf '%s\n' "$*"
  if [[ " $* " == *" up "* ]]; then
    [[ " $* " == *" --wait "* ]] || return 7
    return 9
  fi
}
main "$1" nexus
''' .replace('main "$1" nexus', f'main {verb} nexus'))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('--wait', result.stdout)
                self.assertIn('exit 9', result.stderr)


if __name__ == '__main__':
    unittest.main()
