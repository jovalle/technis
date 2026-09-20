"""Run directly: verify exporter selection for GPU-free and mixed-vendor hosts."""
from deploy import hardware_services, hardware_scrapes

def inventory(*gpus):
    return dict(gpus=[dict(vendor=v,driver=d) for v,d in gpus], disks=[],dri=True,nvidia_runtime=True,nvidia_smi=True)

assert hardware_services(inventory()) == {}
assert hardware_services(inventory(('0x1a03','ast'))) == {}
for driver in ('i915','xe','amdgpu'):
    assert set(hardware_services(inventory(('0x8086',driver)))) == {'drm-exporter'}
mixed=inventory(('0x8086','i915'),('0x1002','amdgpu'),('0x10de','nvidia'))
mixed['disks']=[{'name':'/dev/nvme0'}]
services=hardware_services(mixed)
assert set(services)=={'smartctl-exporter','drm-exporter','nvidia-gpu-exporter'}
assert services['smartctl-exporter']['devices']==['/dev/nvme0:/dev/nvme0:r']
assert '9835' in hardware_scrapes(services)
assert 'scrape_timeout = "4s"' in hardware_scrapes(services)
assert '9634' not in hardware_scrapes({'nvidia-gpu-exporter':{}})
mixed['nvidia_runtime']=False
try:
    hardware_services(mixed)
except RuntimeError as e:
    assert 'NVIDIA' in str(e)
else:
    raise AssertionError('Missing NVIDIA runtime must fail before deployment')
print('PASS: GPU-free, Intel, AMD, NVIDIA/mixed selection, device mappings, scrape selection, missing runtime')

# Runtime and configuration validation must use immutable publisher images.
import re, yaml
from deploy import SOURCE
for manifest in ("compose.yaml", "hardware.yaml"):
    for service in yaml.safe_load((SOURCE / manifest).read_text())["services"].values():
        assert re.search(r"@sha256:[0-9a-f]{64}$", service["image"]), service["image"]
print("PASS: every collector/exporter image is pinned by SHA-256")
