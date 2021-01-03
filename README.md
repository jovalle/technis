# Technis

Test bed for all things virtualization, automation, and Kubernetes.

## Current Environments

### Atlantis
Terraform + libvirt + KVM

### Gotham
Vagrant + Virtualbox

### Hades
Baremetal, hybrid (arm64/amd64)

## Kubernetes

### Strategy

* Avoid Helm. Reverse engineer a chart (e.g. cert-manager) if necessary.

### Ingress

Using Traefik in the homelab. Gotta love the web UI for quick and easy validation. Traefik Proxy does well to allow upstream Ingress (networking.k8s.io/v1) usage without vendor specific annotations!

Running Traefik Proxy as a DaemonSet across the control plane nodes for HA. Ingress service is of type LoadBalancer with MetalLB in the background connecting the dots. Haproxy + Keepalived would have sufficed but where's the fun in reusing that.

Gave up on the Let's Encrypt + Cloudflare DNS bundle as an automated certificate resolver due to what seems to be an unsupported TLD (.is) and rate limiting obstacles while troubleshooting.

Using cert-manager to issue once and maintain (cert-manager will auto-renew) a wildcard cert for the cluster workloads (*.k8s.techn.is). Set TLSStore to leverage this cert by default. CLI-configured to redirect all HTTP requests to the "websecure" (HTTPS) entrypoint.