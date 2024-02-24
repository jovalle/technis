# Technis

![Technis Logo](logo.svg)

Test bed for all things virtualization, automation, and Kubernetes.

## Current Environments

### Default
Baremetal amd64/arm64

### Atlantis
Terraform + libvirt + KVM

### Gotham/Krypton
Vagrant + Virtualbox

## OS

`nvidia-container-runtime` does not support Debian beyond amd64. Ubuntu Server 21.10 is the pick for now as it is supported and built for Raspberry Pis and baremetal alike.

## Kubernetes

### Principles

* All images must have multi-arch support (at least arm64 and amd64).
* Physical resource constraints aside (e.g. GPUs, memory, network bandwidth), no workload should be confined to a singular node.
* In the interest of learning and technological diversity, using multiple solutions to a problem simultaneously is ideal.

### Ingress

Using Traefik in the homelab. Gotta love the web UI for quick and easy validation. Traefik Proxy does well to allow upstream `Ingress` (`networking.k8s.io/v1`) usage without vendor specific annotations!

Running Traefik Proxy as a DaemonSet across the control plane nodes for high-availability (HA). The Traefik ingress service is of type LoadBalancer with MetalLB in the background connecting the dots. Haproxy + Keepalived would have sufficed but where's the fun in reusing that. Haproxy + Keepalived are available for manual failover scenarios should MetalLB suddenly stop functioning (again).

Gave up on the Let's Encrypt + Cloudflare DNS bundle as an automated certificate resolver due to what seems to be an unsupported TLD (.is) and rate limiting obstacles while troubleshooting.

Using cert-manager to issue once and maintain (cert-manager will auto-renew) a wildcard cert for the cluster workloads (*.techn.is). Setting TLSStore to leverage this cert by default and configured via CLI redirection of all HTTP requests to the "websecure" (HTTPS) entrypoint.
