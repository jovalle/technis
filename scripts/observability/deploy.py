"""Deploy only the supplemental collectors; preserve unrelated running services.

python3 scripts/observability/deploy.py stargate [nexus mothership]
SSH uses the existing configured aliases. No credentials are copied or printed.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import time
import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docker/services/host-observer"
HOSTS = {"stargate": ("/opt/stargate", "edge", "stargate_default"),
         "nexus": ("/mnt/data", "home", "nexus_default"),
         "mothership": ("/opt/mothership", "home", "internal")}


def ssh(host, command, data=None):
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                            input=data, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode() + result.stdout.decode())
    return result.stdout.decode()


def put(host, path, data):
    ssh(host, "cat > " + shlex.quote(path), data.encode())


DISCOVER = """
import json, pathlib, shutil, subprocess
cards=[]
for path in pathlib.Path('/sys/bus/pci/devices').iterdir():
 if int((path/'class').read_text().strip(),16) >> 16 != 3: continue
 cards.append({'slot':path.name,'vendor':(path/'vendor').read_text().strip(),
               'driver':(path/'driver').resolve().name if (path/'driver').exists() else ''})
disks=[]
if shutil.which('smartctl'):
 scan=subprocess.run(['smartctl','--scan','-j'],capture_output=True,text=True,check=True)
 disks=json.loads(scan.stdout).get('devices',[])
runtimes=json.loads(subprocess.check_output(['docker','info','--format','{{json .Runtimes}}']))
print(json.dumps({'gpus':cards,'disks':disks,'dri':pathlib.Path('/dev/dri').exists(),
                  'drm_groups':sorted({p.stat().st_gid for p in pathlib.Path('/dev/dri').glob('*') if p.is_char_device()}),
                  'nvidia_runtime':'nvidia' in runtimes,'nvidia_smi':bool(shutil.which('nvidia-smi'))}))
"""


def hardware_services(inventory):
    """Select independently: a mixed Intel/AMD/NVIDIA host can run both exporters."""
    templates = yaml.safe_load((SOURCE / "hardware.yaml").read_text())["services"]
    selected = {}
    if inventory['disks']:
        service = templates['smartctl-exporter']
        service['devices'] = [d['name']+':'+d['name']+':r' for d in inventory['disks']]
        selected['smartctl-exporter'] = service
    if inventory['dri'] and any(g['driver'] in ('i915','xe','amdgpu') for g in inventory['gpus']):
        selected['drm-exporter'] = templates['drm-exporter']
        selected['drm-exporter']['group_add'] = [str(g) for g in inventory.get('drm_groups',[])]
    if any(g['vendor'] == '0x10de' for g in inventory['gpus']):
        if not inventory['nvidia_runtime'] or not inventory['nvidia_smi']:
            raise RuntimeError('NVIDIA GPU detected but NVIDIA driver/nvidia-smi or Container Toolkit runtime is missing')
        selected['nvidia-gpu-exporter'] = templates['nvidia-gpu-exporter']
    return selected


def hardware_scrapes(services):
    text = ''
    for service, job, port, interval in [('smartctl-exporter','smartctl',9633,'60s'),
                                        ('drm-exporter','drm',9634,'5s'),
                                        ('nvidia-gpu-exporter','nvidia',9835,'5s')]:
        if service not in services:
            continue
        text += ('\nprometheus.scrape "'+job+'" {\n'
                 '  targets = [{"__address__" = "127.0.0.1:'+str(port)+'", "instance" = sys.env("OBSERVABILITY_HOST")}]\n'
                 '  job_name = "'+job+'"\n'
                 '  forward_to = [prometheus.remote_write.central.receiver]\n'
                 '  scrape_interval = "'+interval+'"\n'
                 '  scrape_timeout = "'+('30s' if job=='smartctl' else '4s')+'"\n}\n')
    return text


def deploy(host, hardware_enabled=False, host_process_access=False, gpu_root=False):
    base, site, network = HOSTS[host]
    directory = base + "/host-observer"
    hardware = {}
    if hardware_enabled:
        inventory = json.loads(ssh(host, 'python3 -', DISCOVER.encode()))
        hardware = hardware_services(inventory)
        if gpu_root and 'drm-exporter' in hardware:
            hardware['drm-exporter']['user'] = 'root'
        print(host+': detected '+json.dumps(inventory['gpus'])+'; exporters '+', '.join(hardware), flush=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = directory + "/backups/" + stamp
    ssh(host, f"set -e; test $(hostname) = {host}; mkdir -p {directory}/textfile {directory}/data {backup}; "
        f"for name in config.alloy kernel_exporter.py compose.yaml; do "
        f"if test -f {directory}/$name; then cp -p {directory}/$name {backup}/; fi; done")
    if hardware_enabled:
        put(host, directory + '/hardware-inventory.json', json.dumps(inventory,indent=2)+'\n')
        jobs={'smartctl-exporter':'smartctl','drm-exporter':'drm','nvidia-gpu-exporter':'nvidia'}
        put(host, directory+'/textfile/hardware.prom', ''.join(
            'technis_expected_exporter{exporter="'+jobs[name]+'"} 1\n' for name in hardware))
    for name in ("config.alloy", "kernel_exporter.py"):
        text = (SOURCE / "files" / name).read_text()
        if name == "config.alloy":
            text += hardware_scrapes(hardware)
        put(host, directory + "/" + name + ".next", text)
    print(ssh(host, f"docker run --rm -v {directory}/config.alloy.next:/config.alloy:ro " +
                   shlex.quote(yaml.safe_load((SOURCE / "compose.yaml").read_text())["services"]["host-observer"]["image"]) + " validate /config.alloy"), end="", flush=True)
    combined = yaml.safe_load((SOURCE / "compose.yaml").read_text())
    combined['services'].update(hardware)
    compose = yaml.safe_dump(combined,sort_keys=False)
    services = list(combined['services'])
    if host_process_access:
        # Explicit opt-in: removes only these collectors' AppArmor confinement.
        combined = yaml.safe_load(compose)
        for service in ("host-observer", "kernel-exporter"):
            combined["services"][service]["security_opt"].append("apparmor:unconfined")
        compose = yaml.safe_dump(combined, sort_keys=False)
    compose += f"\nnetworks:\n  default:\n    external: true\n    name: {network}\n"
    put(host, directory + "/compose.yaml", compose)
    prefix = f"STACK_DATA_ROOT={base} OBSERVABILITY_HOST={host} OBSERVABILITY_SITE={site} "
    command = prefix + f"docker compose -p {host} -f {directory}/compose.yaml"
    ssh(host, command + " config --quiet")
    # Copy into existing inodes so running bind mounts see new content.
    ssh(host, f"cat {directory}/config.alloy.next > {directory}/config.alloy; "
        f"cat {directory}/kernel_exporter.py.next > {directory}/kernel_exporter.py")
    print(ssh(host, command + " up -d --no-deps " + " ".join(services)), end="", flush=True)
    print(ssh(host, "docker restart host-observer kernel-exporter"), end="", flush=True)
    probe = "import time,urllib.request\nfor attempt in range(30):\n try:\n  urllib.request.urlopen('http://127.0.0.1:12346/-/ready',timeout=2);break\n except Exception:\n  if attempt==29: raise\n  time.sleep(1)"
    ssh(host, 'python3 -', probe.encode())
    print(f"{host}: deployed and ready; backup {backup}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="+", choices=HOSTS)
    parser.add_argument("--hardware", action="store_true", help="Install SMART/GPU exporters with device capabilities; requires explicit authorization")
    parser.add_argument("--host-process-access", action="store_true", help="Disable AppArmor confinement for host-process reads; requires explicit authorization")
    parser.add_argument("--gpu-root", action="store_true", help="Run DRM exporter as root for effective PERFMON; requires explicit authorization")
    args = parser.parse_args()
    for host in args.hosts:
        deploy(host, args.hardware, args.host_process_access, args.gpu_root)
