# jsonnet

Customized kube-prometheus stack inspired by [@paulfantom](https://github.com/paulfantom)'s [ankhmorpork](https://github.com/thaum-xyz/ankhmorpork) repo

## Setup

```
go install github.com/brancz/gojsontoyaml@latest
go install github.com/google/go-jsonnet/cmd/jsonnet@latest
go install -a github.com/jsonnet-bundler/jsonnet-bundler/cmd/jb@latest
jb install github.com/prometheus-operator/kube-prometheus/jsonnet/kube-prometheus@main
jb update
```

## Generating new manifests

Run the `build.sh` to create the Kubernetes manifests. Then deploy those in `manifests/setup` before deploying those in the base `manifests` directory.

```
./build.sh
kubectl create -f manifests/setup
kubectl create -f manifests
```
