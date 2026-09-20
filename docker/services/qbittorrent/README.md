# qBittorrent operations

## Root cause

qBittorrent shares Gluetun's network namespace and its persisted configuration
binds BitTorrent traffic to `tun0`. The old Gluetun healthcheck verified its
control endpoint and DNS, but not the tunnel interface. A qBittorrent
container could therefore start while the namespace existed and before
`tun0` was ready.

The retained qBittorrent log records this failure on 2026-02-08:

```text
The configured network interface is invalid. Interface: "tun0"
```

The same startup did bind the WebUI, so this evidence proves a torrent-network
failure, not a complete WebUI outage. The repository log ends on 2026-04-08,
so it cannot prove the cause of the current August incident without live
Nexus container state and logs.

## Corrective control

The Gluetun healthcheck now requires all of the following before dependent
services start:

- the Gluetun control endpoint responds;
- `/sys/class/net/tun0` exists;
- DNS resolves through Gluetun.

qBittorrent waits for a healthy Gluetun service before starting.

## Incident checks

Run these read-only checks from the repository root before making a runtime
change:

```sh
./bin/tctl status nexus gluetun qbittorrent
./bin/tctl logs nexus gluetun
./bin/tctl logs nexus qbittorrent
```

Interpret the results as follows:

- Gluetun is unhealthy or lacks `tun0`: investigate the VPN connection first.
- qBittorrent logs the invalid-interface message: it started before the tunnel
  was ready; recreate the dependent services only after Gluetun is healthy.
- qBittorrent is healthy but unreachable: recreate the shared
  Gluetun/qBittorrent namespace.

After configuration validation and operator approval, recreate the shared
services together:

```sh
./bin/tctl deploy nexus gluetun qbittorrent
```
