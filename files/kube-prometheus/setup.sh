#!/usr/bin/env bash

set -e
set -x
set -o pipefail

go install -a github.com/jsonnet-bundler/jsonnet-bundler/cmd/jb@latest

if [[ ! -f jsonnetfile.json ]]; then
  jb init
fi

jb install github.com/prometheus-operator/kube-prometheus/jsonnet/kube-prometheus@main
curl -LO https://raw.githubusercontent.com/prometheus-operator/kube-prometheus/main/build.sh
jb update

./build.sh main.jsonnet

kubectl create -f manifests/setup || true
kubectl apply -f manifests/