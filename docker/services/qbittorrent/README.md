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

qBittorrent already waits for a healthy Gluetun service. Nexus Gatus probes the
published host port at `192.168.1.3:10095`, which also detects a stale shared
network namespace that can leave a local container healthcheck green while the
service is unreachable from the host.

## Incident checks

Run these read-only checks from the repository root before making a runtime
change:

```sh
docker compose \
  --env-file docker/.env \
  --env-file docker/stacks/nexus/.env \
  --file docker/stacks/nexus/compose.yaml \
  ps gluetun qbittorrent qbittorrent-exporter

docker compose \
  --env-file docker/.env \
  --env-file docker/stacks/nexus/.env \
  --file docker/stacks/nexus/compose.yaml \
  logs --since=30m gluetun qbittorrent
```

Interpret the results as follows:

- Gluetun is unhealthy or lacks `tun0`: investigate the VPN connection first.
- qBittorrent logs the invalid-interface message: it started before the tunnel
  was ready; recreate the dependent services only after Gluetun is healthy.
- qBittorrent is healthy but the Nexus probe fails: recreate the shared
  Gluetun/qBittorrent namespace rather than trusting the local healthcheck.

After configuration validation and operator approval, recreate the shared
services together:

```sh
docker compose \
  --env-file docker/.env \
  --env-file docker/stacks/nexus/.env \
  --file docker/stacks/nexus/compose.yaml \
  up -d --force-recreate gluetun qbittorrent qbittorrent-exporter
```
