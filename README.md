# Technis

Test bed for all things virtualization, automation, and Kubernetes.


## Features

* Upstream Kubernetes
  * backed by etcd
  * options for CNI/CRI
* Scalable deployment
* Support for Terraform/Vagrant
* Auto-generated certificates


## Current Environments

### Atlantis
Terraform + libvirt + KVM

### Gotham
Vagrant + Virtualbox


## Future Enhancements

### Container Runtime
* CRI-O
### Infrastructure
* EKS
* GCP
* KinD
* Multipass (+k3s)
* VMware Fusion (+custom provider)
### Misc
* CockroachDB
* Vitess DB
### Monitoring/Alerting/Tracing
* Alertmanager
* EFK
* Grafana
* Kiali
* Prometheus
* Splunk
* Thanos
### Networking/Ingress/Loadbalancing
* Ambassador
* Calico Enterprise
* Cillium
* Contour
* Haproxy (+LB)
* Kong
* MetalLB
* Traefik
### Policy
* Calico (+Enterprise)
* OPA
### Service Mesh
* Linkerd
* Istio
### Storage
* Ceph
* Local provisioner
* NFS provisioner
* StorageOS
