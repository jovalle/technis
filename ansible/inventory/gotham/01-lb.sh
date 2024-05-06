#!/bin/sh -e

# deploy metallb with BGP config
if [[ -z $(kubectl get ns metallb-system --ignore-not-found --no-headers) ]]; then
  kubectl create ns metallb-system
fi
kubectl apply -f https://raw.githubusercontent.com/google/metallb/v0.9.5/manifests/metallb.yaml
cat << EOF | kubectl apply -f -
---
apiVersion: v1
kind: ConfigMap
metadata:
  namespace: metallb-system
  name: config
data:
  config: |
    peers:
    - my-asn: 64522
      peer-asn: 64512
      peer-address: 192.168.1.1
      peer-port: 179
      router-id: 192.168.1.1
    address-pools:
    - name: aux
      protocol: bgp
      avoid-buggy-ips: true
      addresses:
      - 192.168.2.192/26
EOF

if [[ -z $(kubectl -n metallb-system get secret memberlist --no-headers --ignore-not-found) ]]; then
  kubectl -n metallb-system create secret generic memberlist --from-literal=secretkey="$(openssl rand -base64 128)"
fi

