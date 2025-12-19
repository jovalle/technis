<div align="center">

<img src=".github/assets/logo.svg" alt="Technis Logo" width="400" height="400" style="margin-bottom: -100px;">

# Technis

[![Home Internet](https://img.shields.io/endpoint?url=https://status.techn.is/api/v1/endpoints/egress_cloudflare/health/badge.shields&style=for-the-badge&logo=ubiquiti&logoColor=white&label=Home%20Internet)](http://status.techn.is/endpoints/egress_cloudflare) [![Status Page](https://img.shields.io/endpoint?url=https://status.techn.is/api/v1/endpoints/external_status/health/badge.shields&style=for-the-badge&logo=statuspage&logoColor=white&label=Status%20Page)](https://status.techn.is)

![Availability](https://status.techn.is/api/v1/endpoints/external_status/uptimes/24h/badge.svg) ![Node Uptime](https://img.shields.io/endpoint?url=https://stat.techn.is/query?metric=cluster_min_uptime&style=flat&label=uptime) ![CPU Usage](https://img.shields.io/endpoint?url=https://stat.techn.is/query?metric=cluster_cpu_usage&style=flat&label=cpu) ![Memory Usage](https://img.shields.io/endpoint?url=https://stat.techn.is/query?metric=cluster_memory_usage&style=flat&label=memory) ![Docker Containers](https://img.shields.io/endpoint?url=https://stat.techn.is/query?metric=docker_containers_running&style=flat&label=containers)

</div>

## 📖 Overview

This repository serves as the **single source of truth** for my homelab infrastructure, following [best practices](#pillars-of-infrastructure) to manage everything from bare metal provisioning to application deployment.

It is a continuous work in progress.

## 🚀 Services

Technis provides a whole host of self-hosted services, for friends and family, including:

- **VPS Hosting** - Virtual private servers for development and testing
- **On-Demand Video Streaming** - Media streaming and transcoding services
- **Personal Cloud Storage** – High-performance, privacy-first file sharing and automated backups

## 💪🏼 Pillars of Infrastructure

1. Infrastructure as Code
2. GitOps
3. Security by Design
4. Observability and Monitoring
5. Backup and Recovery
6. Automation First
7. Documentation and Runbooks
8. Change Management Discipline

## 📁 Project Structure

```sh
technis/
├── .github/     # Git assets
├── .taskfiles/  # Scripts
├── ansible/     # Automated playbooks
├── archive/     # Legacy code
├── docs/        # Documentation site (submodule)
├── docker/      # Local container services
├── kubernetes/  # Distributed container services
│   ├── apps/    # Deployments
│   │   ├── kube-system     # Namespace
│   │   │   ├── traefik     # Component
│   │   │   │   ├── app/    # Helm resources
│   │   │   │   └── config/ # Additional resources
│   ├── bootstrap/ # Assemble cluster
│   │   ├── flux/  # Provision FluxCD
│   │   └── talos/ # Provision Talos cluster
├── terraform/
└── web/         # Web frontend (submodule)
```

## 🤝 Kudos

- [awesome-selfhosted](https://github.com/awesome-selfhosted/awesome-selfhosted)
- [bjw-s/helm-charts](https://github.com/bjw-s/helm-charts)
- [dmacvicar/terraform-provider-libvirt](https://github.com/dmacvicar/terraform-provider-libvirt)
- [gandazgul/k8s-infrastructure](https://github.com/gandazgul/k8s-infrastructure)
- [kelseyhightower/kubernetes-the-hard-way](https://github.com/kelseyhightower/kubernetes-the-hard-way)
- [kinvolk-archives/kubernetes-the-hard-way-vagrant](https://github.com/kinvolk-archives/kubernetes-the-hard-way-vagrant)
- [onedr0p/home-ops](https://github.com/onedr0p/home-ops)
- [techno-tim/launchpad](https://github.com/techno-tim/launchpad)
