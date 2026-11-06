import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

spec = importlib.util.spec_from_file_location('pilot', Path(__file__).with_name('pilot_update.py'))
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)
suffix = uuid.uuid4().hex[:12]
project = 'technispilottest' + suffix
container = project + '-service'
good = project + ':good'
bad = project + ':bad'
built = []
tried_deploy = False


def command(*args, input_text=None):
    result = subprocess.run(['docker', '--context', 'nexus', *args], input=input_text,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, 'DOCKER_BUILDKIT': '0'})
    if result.returncode:
        raise RuntimeError(f'{args[:2]} failed: {result.stderr[-1000:]}')
    return result.stdout.strip()


original_run = pilot.run


def isolated_run(args, **kwargs):
    args = list(args)
    if 'compose' in args:
        args[args.index('--project-name') + 1] = project
        args[args.index('--wait-timeout') + 1] = '15'
    if 'inspect' in args and args[-1] == 'flaresolverr':
        args[-1] = container
    return original_run(args, **kwargs)


try:
    for tag, marker in [(good, 'good'), (bad, 'bad')]:
        command('build', '-t', tag, '-', input_text=f'FROM busybox:latest\nRUN touch /pilot_{marker}\n')
        built.append(tag)
    good_id = command('image', 'inspect', good, '--format', '{{.Id}}')
    bad_id = command('image', 'inspect', bad, '--format', '{{.Id}}')
    model = {'services': {'flaresolverr': {
        'image': good_id, 'container_name': container, 'network_mode': 'none',
        'command': ['sh', '-c', 'sleep 300'],
        'healthcheck': {'test': ['CMD-SHELL', 'test -f /pilot_good'],
                        'interval': '1s', 'timeout': '1s', 'retries': 2},
    }}}
    pilot.run = isolated_run
    sleep = time.sleep
    pilot.time.sleep = lambda seconds: sleep(0.1)
    tried_deploy = True
    pilot.deploy(good_id, good_id, model)
    print('PASS: disposable container deployed and observed healthy', flush=True)
    try:
        pilot.apply_images(bad_id, good_id,
                           lambda image: pilot.deploy(image, image, model))
    except pilot.Recovered:
        pass
    else:
        raise RuntimeError('unhealthy candidate did not trigger rollback')
    if pilot.current() != good_id:
        raise RuntimeError('rollback did not restore the previous image')
    print('PASS: unhealthy candidate rolled back to the healthy previous image', flush=True)
finally:
    failed = []
    if tried_deploy and subprocess.run(['docker', '--context', 'nexus', 'rm', '-f', container], capture_output=True).returncode:
        failed.append(container)
    if built and subprocess.run(['docker', '--context', 'nexus', 'image', 'rm', *built], capture_output=True).returncode:
        failed.extend(built)
    if failed:
        raise RuntimeError('could not verify cleanup of: ' + ', '.join(failed))
