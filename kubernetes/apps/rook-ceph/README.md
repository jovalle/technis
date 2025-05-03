# rook-ceph

Deploys rook-ceph via operator and cluster helm charts.

Before deploying anything in here, and because we are leveraging an external Ceph cluster, we must gather information. We can use the scripts in the upstream `rook` repo to generate and even deploy the necessary configmaps, secrets, etc.

## Prerequisites

Probably best to run the following from a linux host so that the necessary auth and config files are readily available. I just SSH'ed onto one of the proxmox nodes and ran the following:

```bash
git clone https://github.com/rook/rook.git
cd rook/deploy/examples
python3 create-external-cluster-resources.py --ceph-conf /etc/pve/ceph.conf --keyring /etc/pve/priv/ceph.client.admin.keyring --rbd-data-pool-name core --cephfs-filesystem-name cephfs --rgw-endpoint 192.168.31.10:7480 --namespace rook-ceph --format bash > cluster-info.sh
```

Copy the output and update your env accordingly.

## Deployment

The following will deploy with local kubeconfig.

```bash
chmod +x ../rook/deploy/examples/import-external-cluster.sh
../rook/deploy/examples/import-external-cluster.sh
```

The necessary resources should now be deployed, proceed with deploying `rook-ceph-cluster`

## Saving Resources

To store and reuse these resources, I created this script to consolidate them. Import the generated manifest (e.g. `app/secrets.yaml`)

```bash
#!/usr/bin/env bash
set -euo pipefail

# output file (overwritten each run)
OUTFILE="combined-manifests.yaml"
: > "$OUTFILE"

# namespace for namespaced resources
NAMESPACE="rook-ceph"

# list of resources to save
resources=(
  "configmap rook-ceph-mon-endpoints"
  "configmap external-cluster-user-command"
  "secret rook-ceph-mon"
  "secret rook-csi-rbd-node"
  "secret rook-csi-rbd-provisioner"
  "secret rgw-admin-ops-user"
  "secret rook-csi-cephfs-node"
  "secret rook-csi-cephfs-provisioner"
)

# iterate through save targets
for r in "${resources[@]}"; do
  kind=${r%% *}
  name=${r#* }
  echo "⮕ Dumping $kind/$name ..." >&2

  # cluster-scoped vs namespaced
  if [[ "$kind" == "namespace" || "$kind" == "storageclass" ]]; then
    ns_flag=""
  else
    ns_flag="-n $NAMESPACE"
  fi

  # fetch, clean, append
  kubectl get "$kind" "$name" $ns_flag -o yaml \
    | yq eval \
        'del(
           .metadata.resourceVersion,
           .metadata.uid,
           .metadata.selfLink,
           .metadata.creationTimestamp,
           .metadata.managedFields,
           .metadata.annotations,
           .status
         )' - \
    >> "$OUTFILE"

  # YAML document separator
  echo "---" >> "$OUTFILE"
done

echo "✅ All manifests appended to $OUTFILE"
```
