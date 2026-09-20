# Host observability

Deployed on Stargate, Nexus and Mothership on 2026-09-18. Start with [Host overview](https://grafana.techn.is/d/technis-host). Host detail, Processes and ownership, Containers and services, Kernel and hardware, Anomalies and telemetry health, GPUs, and Disk health link from there.

This is a working expansion of the existing VictoriaMetrics/Grafana stack. **It is not yet a complete Netdata replacement. Keep Netdata until the gaps below are closed and compared against the live inventory.**

## What runs

`host-observer` uses Alloy's embedded Unix and process exporters in the host PID/network namespaces. It sends metrics to the existing private vmauth endpoint. Its management endpoint binds only to `127.0.0.1:12346`. The main Alloy container continues collecting application metrics, logs, traces and cAdvisor metrics. Its former Unix exporter was removed to avoid duplicate host series and incorrect container-network counters.

`kernel-exporter` reads Linux procfs/sysfs and the existing restricted Docker API. It writes an atomic Prometheus textfile; it has no listening endpoint. It adds PSI averages, cgroup v2 resources and network counters, UID/GID aggregates, IPC, CPU idle residency, PCIe errors and scheduler wake-up delay. Process labels exclude command lines and PIDs. Docker labels use names rather than IDs. Network counters exclude host/shared-network containers because their traffic cannot be attributed independently this way.

Host and process collection runs every **5 seconds**, existing application/cAdvisor collection every 30 seconds, and statistical rules every 30 seconds. Read-only mounts, dropped capabilities and resource limits remain enabled. The user approved AppArmor removal for the two host collectors on 2026-09-18; hardware exporters retain default confinement. The collectors have SYS_PTRACE and DAC_READ_SEARCH; see the Compose file for the complete access boundary.

## Coverage and remaining work

The captured [Netdata inventory](files/netdata-inventory.json) contains 403 contexts on Stargate, 472 on Nexus and 514 on Mothership. These include Netdata's own internal telemetry; context counts are not a parity percentage.

| Area                                            | Current coverage                                                                                               | Remaining gap                                                                                                                                                                                                                              |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| CPU, memory, disk, filesystems, network, kernel | Unix exporter plus extensive Host detail dashboard                                                             | Metric-by-metric comparison, including optional/kernel-specific counters                                                                                                                                                                   |
| Processes, users and groups                     | CPU, memory, faults, context switches, descriptors and available I/O                                           | Process-read errors now zero on all hosts after approved access changes; monitor `technis_owner_read_errors` for regressions                                                                                                               |
| Containers and systemd services                 | cgroup CPU, throttling, memory, swap, PIDs, I/O, PSI and attributable network counters; systemd state/restarts | Netdata's exact app grouping, all Docker image statistics and every context dimension                                                                                                                                                      |
| ZFS                                             | Nexus ARC metrics confirmed live through Unix exporter                                                         | Dedicated ZFS dashboard and full context comparison                                                                                                                                                                                        |
| Redis/Postgres                                  | Existing exporters confirmed up on Nexus                                                                       | Service dashboards and exact Redis context parity                                                                                                                                                                                          |
| Chrony/dnsmasq                                  | Not added in this deployment                                                                                   | Nexus has Netdata contexts for both; dedicated collection and dashboards still required                                                                                                                                                    |
| Docker engine                                   | Existing coverage not fully audited                                                                            | Compare Mothership's native engine/Prometheus contexts and scrape any missing endpoint                                                                                                                                                     |
| Hardware                                        | Available hwmon, CPU frequency, thermal, RAPL, idle and PCIe metrics                                           | SMART live for 13 disks; Intel DRM memory, frequency, power and engine utilization live on both physical hosts after explicit root-execution approval. AMD/NVIDIA selection is implemented but not live-tested; VPS lacks physical sensors |
| Intelligence                                    | Ten host signals with rolling mean/stddev, deviation scores and expected bands                                 | Netdata's automatic per-dimension ML and metric correlation are not reproduced                                                                                                                                                             |
| Resolution                                      | Five-second host/process/cgroup samples                                                                        | Netdata's one-second capture; measure ingestion and collector cost before reducing intervals                                                                                                                                               |
| History                                         | New collection starts with deployment                                                                          | Old Netdata history has not been migrated                                                                                                                                                                                                  |

The baseline uses the preceding 24 hours, excludes the last five minutes, and requires 120 samples before producing scores (roughly 65 minutes after first recording). It does not model seasonality or prove causation. No statistical anomaly notifications were enabled. Existing metric alerts and five diagnostic coverage alerts plus hardware availability, disk health and NVIDIA collection alerts are active.

Grafana Metrics Drilldown and the VictoriaLogs datasource plugin were already installed. These dashboards use native Grafana panels; no additional panel plugins were needed.

## Deploy and verify

Run from the repository root with Python 3, PyYAML and the existing SSH aliases. Deployment validates Alloy configuration and touches only the two collector containers. It preserves unrelated services and makes timestamped backups under each host's `host-observer/backups` directory.

```sh
rtk proxy python3 scripts/observability/test_kernel_exporter.py
rtk proxy python3 scripts/observability/test_hardware.py
rtk proxy python3 scripts/observability/deploy.py stargate nexus mothership --hardware --host-process-access --gpu-root
rtk proxy python3 scripts/observability/verify.py
```

The verification command checks all eight dashboard query sets against all three hosts, expected scrape targets, supplemental failures, textfile errors, owner-read errors, expected hardware exporters and vmalert rule evaluation. It saves detailed results to `/tmp/technis-observability/verification.json`. A successful query with no series is not proof of coverage: inspect empty results against the hardware and service inventory.

Dashboard generation: `rtk proxy python3 scripts/observability/dashboards.py`. Statistical rule generation: `rtk proxy python3 scripts/observability/rules.py`. These write repository files only. Live dashboard files reside at `/mnt/data/grafana/dashboards`; metric rules reside at `/mnt/data/vmalert/rules/metrics.yaml` on Nexus. Validate staged rules with vmalert's `-dryRun` before replacing them, then POST its private `/-/reload` endpoint.

Optional deployment flags are **off by default**:

- `--hardware` scans PCI drivers and SMART devices on each deployment. It selects DRM for Intel i915/xe and AMD amdgpu, NVIDIA exporter for NVIDIA PCI devices, and SMART only when devices exist. Mixed-vendor hosts can run both GPU exporters. Exporters discover their visible GPUs and use native labels; no card index or model is hardcoded.
- `--host-process-access` removes AppArmor confinement for these two collectors only.

The user approved both flags on 2026-09-18. SMART runs with SYS_RAWIO/SYS_ADMIN and read-only device mappings. DRM runs as root with only PERFMON, after explicit user approval on 2026-09-18. It retains default AppArmor confinement, no-new-privileges, a read-only filesystem/sysfs, localhost binding and resource limits; privileged mode remains disabled. The `--gpu-root` deployment flag preserves this choice. Engine metrics were verified at both exporter endpoints and in VictoriaMetrics: four engine classes on Stargate and five on Nexus.

All five collector/exporter image references use SHA-256 digests, including the Alloy image used for configuration validation. DRM's `0.3.3` release tag was resolved against the publisher registry and matched the deployed digest. The runnable hardware check rejects unpinned image references. Digest pinning prevents tag drift; it is not a claim that an image is vulnerability-free or that its signature was independently verified.

NVIDIA uses the pinned `utkuozdemir/nvidia_gpu_exporter:1.15.1` image with all detected GPUs and the `utility` driver capability. It requires the host NVIDIA driver, `nvidia-smi`, and NVIDIA Container Toolkit runtime. Deployment fails explicitly if these prerequisites are missing; it does not install a kernel driver or silently pretend collection works. No NVIDIA host exists in the current fleet. AMD uses the same DRM exporter; no AMD GPU exists here either. Intel xe engine metrics require a sufficiently recent kernel (upstream specifies 6.16+).

Rerun deployment after adding/removing hardware or changing GPU drivers. This is deployment-time discovery, not unattended driver installation or hot-plug reconciliation. Existing SMART devices are rescanned by the exporter, but new device-node access requires redeployment. All hardware endpoints bind to localhost (SMART 9633, DRM 9634, NVIDIA 9835).

## Verification recorded on 2026-09-18

- Parser/accounting checks passed, including PID reuse, cumulative counters and network namespace attribution. A deliberate counter-accounting mutation failed the check as expected, then was reverted.
- 1,283 live query checks: zero query errors; 138 empty results, including intentionally empty failure queries, baseline warm-up, absent TCP states, unavailable hardware/vendor metrics.
- Nine expected node/process/self scrape targets were up. All seven supplemental collectors succeeded on each host. No textfile errors.
- 41 metric rules evaluated without errors. Alert delivery was not tested with a synthetic notification.
- Redis/Postgres exporters were up; Nexus ZFS ARC metrics were present.
- Owner read errors: zero on Stargate, Nexus and Mothership after the approved access changes. SMART health series: 1 disk on Stargate and 12 on Nexus. Both DRM and both SMART targets are up; Intel engine utilization is ingested on both GPU hosts. AMD/NVIDIA exporter selection and missing-runtime failure checks passed locally; actual AMD/NVIDIA telemetry remains untested.
- Observer CPU over a five-minute window was approximately 0.09, 0.22 and 0.23 CPU cores respectively. This is a short deployment observation, not a capacity benchmark; it excludes the separate kernel exporter.

## Roll back

Each collector deployment saves the prior `config.alloy`, `kernel_exporter.py` and `compose.yaml` when present. Restore a chosen backup into the same paths, validate the Compose/Alloy configuration, and recreate only `host-observer` and `kernel-exporter`. Do not run `down` against the shared host project.

For a complete return to the former host collector, first restore the main Alloy file from `host-observer/backups/main-alloy-before-host-cutover.alloy`, validate/reload it, then stop the two new collectors. The old collector's network-namespace limitation returns with that rollback. Dashboard backups are under `/mnt/data/grafana/backups` on Nexus. The original live metric-rules directory was empty; disabling the restored rules removes alert coverage.

The imported Host detail dashboard retains the upstream [Apache 2.0 license](../grafana/files/dashboards/node-exporter-full.LICENSE).

GPU implementation references: [DRM exporter](https://github.com/home-operations/drm-exporter), [NVIDIA exporter](https://github.com/utkuozdemir/nvidia_gpu_exporter). Their metric sets vary by GPU, driver and kernel. Unsupported readings remain absent.
