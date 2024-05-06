# Network UPS Tools (NUT)

Tooling for exposing UPS device metrics. Paired with Telegraf to publish data to InfluxDB and visualized with Grafana.

## Troubleshooting

Check UPS device is connected
```bash
# lsusb
Bus 001 Device 014: ID 09ae:2012 Tripp Lite Tripp Lite UPS
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
```

Check systemd units
```bash
# systemctl status nut-server
● nut-server.service - Network UPS Tools - power devices information server
     Loaded: loaded (/lib/systemd/system/nut-server.service; enabled; vendor preset: enabled)
     Active: active (running) since Wed 2022-07-13 10:27:41 EDT; 12min ago
    Process: 2318 ExecStart=/sbin/upsd (code=exited, status=0/SUCCESS)
   Main PID: 2319 (upsd)
      Tasks: 1 (limit: 415)
        CPU: 422ms
     CGroup: /system.slice/nut-server.service
             └─2319 /lib/nut/upsd

Jul 13 10:27:41 observer upsd[2318]: fopen /run/nut/upsd.pid: No such file or directory
Jul 13 10:27:41 observer upsd[2318]: listening on 0.0.0.0 port 3493
Jul 13 10:27:41 observer upsd[2318]: listening on 0.0.0.0 port 3493
Jul 13 10:27:41 observer upsd[2318]: Connected to UPS [tripplite]: usbhid-ups-tripplite
Jul 13 10:27:41 observer upsd[2318]: Connected to UPS [tripplite]: usbhid-ups-tripplite
Jul 13 10:27:41 observer systemd[1]: Started Network UPS Tools - power devices information server.
Jul 13 10:27:41 observer upsd[2319]: Startup successful
Jul 13 10:28:16 observer upsd[2319]: Can't connect to UPS [tripplite] (usbhid-ups-tripplite): No such file or directory
Jul 13 10:31:03 observer upsd[2319]: Connected to UPS [tripplite]: usbhid-ups-tripplite
Jul 13 10:31:27 observer upsd[2319]: Connected to UPS [tripplite]: usbhid-ups-tripplite
```

Check UPS device is detected by `upsd`
```bash
# nut-scanner -U
SNMP library not found. SNMP search disabled.
Neon library not found. XML search disabled.
IPMI library not found. IPMI search disabled.
Scanning USB bus.
[nutdev1]
        driver = "usbhid-ups"
        port = "auto"
        vendorid = "09AE"
        productid = "2012"
        product = "Tripp Lite UPS"
        vendor = "Tripp Lite"
        bus = "001"
```

Run manual query using `upsc`
```bash
# upsc tripplite@localhost
Init SSL without certificate database
battery.charge: 100
battery.runtime: 1806
battery.type: PbAC
battery.voltage: 26.3
battery.voltage.nominal: 24.0
device.mfr: Tripp Lite
device.model: Tripp Lite UPS
device.type: ups
driver.name: usbhid-ups
driver.parameter.pollfreq: 30
driver.parameter.pollinterval: 1
driver.parameter.port: auto
driver.parameter.productid: 2012
driver.parameter.synchronous: no
driver.parameter.vendorid: 09AE
driver.version: 2.7.4
driver.version.data: TrippLite HID 0.82
driver.version.internal: 0.41
input.frequency: 60.0
input.voltage: 114.1
input.voltage.nominal: 120
output.frequency.nominal: 60
output.voltage: 114.0
output.voltage.nominal: 120
ups.beeper.status: disabled
ups.delay.shutdown: 20
ups.load: 19
ups.mfr: Tripp Lite
ups.model: Tripp Lite UPS
ups.power: 0.0
ups.power.nominal: 1500
ups.productid: 2012
ups.status: OL
ups.timer.reboot: 65535
ups.timer.shutdown: 65535
ups.vendorid: 09ae
ups.watchdog.status: 0
```
