#!/usr/bin/env python3
"""Restore nexus' ASUS KCS interface and tty1 on the ASPEED framebuffer."""
import fcntl
from pathlib import Path
import struct
import subprocess
import time


def hardware(sysfs=Path('/sys')):
    if (sysfs / 'class/dmi/id/board_name').read_text().strip() != 'Pro WS W680-ACE IPMI':
        raise ValueError('This fix is restricted to the ASUS Pro WS W680-ACE IPMI')
    devices = [p for p in (sysfs / 'bus/pci/devices').iterdir()
               if (p / 'vendor').read_text().strip() == '0x1a03'
               and (p / 'device').read_text().strip() == '0x2402'
               and (p / 'class').read_text().strip() == '0x0c0701']
    frames = [p for p in (sysfs / 'class/graphics').glob('fb[0-9]*')
              if (p / 'name').read_text().strip() == 'astdrmfb']
    if not devices or not frames:
        raise LookupError('Waiting for the ASUS KCS device and ASPEED framebuffer')
    if len(devices) != 1 or len(frames) != 1:
        raise ValueError('Ambiguous ASUS KCS device or ASPEED framebuffer')
    start, end, flags = [int(v, 16) for v in (devices[0] / 'resource').read_text().splitlines()[1].split()]
    # shortcut: ASMB10 1.1.40 places KCS at BAR1 + 0xe88; revalidate after BMC firmware changes.
    if not start or end - start < 0xe8c or not flags & 0x200:
        raise ValueError('ASPEED BAR1 does not contain the expected memory registers')
    return start + 0xe88, int(frames[0].name[2:])


def restore(address, framebuffer):
    run = lambda args: subprocess.run(args, check=True, capture_output=True, text=True, timeout=15)
    if not Path('/dev/ipmi0').exists():
        run(['/usr/sbin/modprobe', 'ipmi_devintf'])
        run(['/usr/sbin/modprobe', 'ipmi_si'])
        if not Path('/dev/ipmi0').exists():
            Path('/sys/module/ipmi_si/parameters/hotmod').write_text(f'add,kcs,mem,{address:#x},rsp=4,rsi=1\n')
    run(['/usr/bin/ipmitool', 'mc', 'info'])
    with open(f'/dev/fb{framebuffer}', 'rb', buffering=0) as device:
        mapping = bytearray(struct.pack('II', 1, 0))
        fcntl.ioctl(device, 0x460f, mapping, True)  # FBIOGET_CON2FBMAP
        previous = struct.unpack('II', mapping)[1]
        if previous != framebuffer:
            fcntl.ioctl(device, 0x4610, struct.pack('II', 1, framebuffer))  # FBIOPUT_CON2FBMAP
        fcntl.ioctl(device, 0x460f, mapping, True)
        if struct.unpack('II', mapping)[1] != framebuffer:
            raise RuntimeError('tty1 did not move to the ASPEED framebuffer')
        fcntl.ioctl(device, 0x4611, 0)  # FBIOBLANK / FB_BLANK_UNBLANK
    print(f'IPMI query passed; KCS at {address:#x}; tty1 on ASPEED fb{framebuffer} (was fb{previous})')


def main():
    deadline = time.monotonic() + 30
    while True:
        try:
            address, framebuffer = hardware()
            break
        except (FileNotFoundError, LookupError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    restore(address, framebuffer)


if __name__ == '__main__':
    main()
