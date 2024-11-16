# Technis

![Technis](docs/src/assets/technis.webp)

## 🌅 Overview

Test bed for all things virtualization, automation, and Kubernetes.

## 💿 Virtualization

### [ESXi/vSphere](docs/src/assets/friendship.png)

### Current Environments

| Cluster        | Technologies                    | Description                                                                        |
| -------------- | ------------------------------- | ---------------------------------------------------------------------------------- |
| Atlantis       | Terraform + libvirt + KVM       | Lab environment on Linux desktop                                                   |
| Gotham/Krypton | Vagrant + Virtualbox            | Lab environment on Linux/macOS laptops                                             |
| Hades          | Multi-arch baremetal            | Physical lab environment comprising of Intel/AMD mini PCs and Raspberry Pi devices |
| Technis        | Terraform + Proxmox + baremetal | Main cluster and "production" environment                                          |

## 🤖 Automation

Taskfiles will call all required, and differing, tooling (e.g. `terraform`, `ansible-playbook`, `taloctl` and `helm`) to provision and configure targets.

## ☸️ Kubernetes

### Distros

- [k3s](https://k3s.io)
- [Talos Linux](https://www.talos.dev/)

## 🤝 Kudos

- [awesome-selfhosted](https://github.com/awesome-selfhosted/awesome-selfhosted)
- [dmacvicar/terraform-provider-libvirt](https://github.com/dmacvicar/terraform-provider-libvirt)
- [gandazgul/k8s-infrastructure](https://github.com/gandazgul/k8s-infrastructure)
- [kelseyhightower/kubernetes-the-hard-way](https://github.com/kelseyhightower/kubernetes-the-hard-way)
- [kinvolk-archives/kubernetes-the-hard-way-vagrant](https://github.com/kinvolk-archives/kubernetes-the-hard-way-vagrant)
- [onedr0p/home-ops](https://github.com/onedr0p/home-ops)
- [techno-tim/launchpad](https://github.com/techno-tim/launchpad)
