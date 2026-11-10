<div align="center">

<img src="assets/impossible-t.svg" alt="Technis Logo" width="400" height="400">

# Technis

[![Home Internet](https://img.shields.io/endpoint?url=https://status.techn.is/api/v1/endpoints/_connectivity/health/badge.shields&style=for-the-badge&logo=ubiquiti&logoColor=white&label=Home%20Internet&cacheSeconds=60)](https://status.techn.is/endpoints/_connectivity) [![Status Page](https://img.shields.io/endpoint?url=https://status.techn.is/api/v1/endpoints/_status/health/badge.shields&style=for-the-badge&logo=statuspage&logoColor=white&label=Status%20Page&cacheSeconds=60)](https://status.techn.is)

![Availability](https://status.techn.is/api/v1/endpoints/_status/uptimes/24h/badge.svg) ![Node Uptime](https://img.shields.io/endpoint?url=https://stat.techn.is/query?metric=cluster_min_uptime&style=flat&label=uptime) ![CPU Usage](https://img.shields.io/endpoint?url=https://stat.techn.is/query?metric=cluster_cpu_usage&style=flat&label=cpu) ![Memory Usage](https://img.shields.io/endpoint?url=https://stat.techn.is/query?metric=cluster_memory_usage&style=flat&label=memory) ![Docker Containers](https://img.shields.io/endpoint?url=https%3A%2F%2Fstat.techn.is%2Fquery%3Fmetric%3Ddocker_containers_running%26v%3D2&style=flat&label=containers&cacheSeconds=300)

</div>

## Secret Scanning

Install the root commit checks with `pre-commit install`. Gitleaks scans staged changes;
if it reports an exposed credential, remove it from the commit and rotate it.

The Secrets CI check scans incoming Git history, including merged commits, using the
Gitleaks version pinned in `.pre-commit-config.yaml`. Local hooks can be bypassed;
configure GitHub push protection and require the CI check on the default branch.
Renovate proposes dependency updates and automerges passing non-major updates.
