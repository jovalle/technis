#!/usr/bin/env bash

set -e
set -x
set -o pipefail

die() { echo "$*" 1>&2; exit 1; }

need() { which "$1" &>/dev/null || die "need '$1' installed"; }

need "go"
need "jsonnet"
need "kubectl"

go install -a github.com/jsonnet-bundler/jsonnet-bundler/cmd/jb@latest
go install -a github.com/brancz/gojsontoyaml@latest

if [[ ! -f jsonnetfile.json ]]; then
  jb init
fi

jb install github.com/prometheus-operator/kube-prometheus/jsonnet/kube-prometheus@main
curl -LO https://raw.githubusercontent.com/prometheus-operator/kube-prometheus/main/build.sh
jb update

./build.sh main.jsonnet

kubectl create -f manifests/setup || true
kubectl apply -f manifests/
