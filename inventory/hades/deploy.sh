export DOMAIN=technis.net
export K3S_OPTIONS="--flannel-backend=none --disable-kube-proxy --disable-network-policy --disable servicelb"
export K3S_SERVERS=(cerberus zagreus thanatos)
export K3S_AGENTS=(orpheus)
export K3S_VERSION="v1.27.7+k3s2"
export KUBE_API_IP=192.168.0.10
export KUBE_INGRESS_IP=192.168.0.20

# set -eo pipefail
set -e

die() { echo "$*" 1>&2; exit 1; }
need() { which "$1" &>/dev/null || die "Binary '$1' is missing but required"; }

# Checking prereqs
need "curl"
need "helm"
need "jq"
need "k3sup"
need "kubectl"

echo "Initiating k3s on ${K3S_SERVER[0]}.${DOMAIN}..."
k3sup install --cluster --host ${K3S_SERVERS[0]}.${DOMAIN} --ssh-key $HOME/.ssh/technis --local-path ~/.kube/config --context technis --merge --k3s-version "${K3S_VERSION}" --k3s-extra-args "$K3S_OPTIONS" --tls-san k8s.techn.is --print-command --print-config

for server in ${K3S_SERVERS[@]:1}; do
  echo "Joining $server to k3s cluster control plane..."
  k3sup join --host ${server}.${DOMAIN} --server-host ${K3S_SERVERS[0]}.${DOMAIN} --server --ssh-key $HOME/.ssh/technis --k3s-version "${K3S_VERSION}" --k3s-extra-args "$K3S_OPTIONS" --tls-san ${KUBE_API_IP} --print-command
done

for agent in ${K3S_AGENTS[@]}; do
  echo "Joining $agent to k3s cluster..."
  k3sup join --host ${agent}.${DOMAIN} --server-host ${K3S_SERVERS[0]}.${DOMAIN} --ssh-key $HOME/.ssh/technis --k3s-version "${K3S_VERSION}" --print-command
done

echo "Creating admin.conf via symlink for consistency..."
for host in ${K3S_SERVERS[@]}
do
  ssh ${host}.${DOMAIN} 'mkdir -p /etc/kubernetes; unset /etc/kubernetes/admin.conf; ln -s /etc/rancher/k3s/k3s.yaml /etc/kubernetes/admin.conf; rm -f ~/.kube/config; mkdir -p ~/.kube; ln -s /etc/rancher/k3s/k3s.yaml ~/.kube/config || echo'
done

scp ${K3S_SERVERS[0]}.${DOMAIN}:/etc/kubernetes/admin.conf ~/.kube/technis
sed -i -- "s/127.0.0.1/${K3S_SERVERS[0]}.${DOMAIN}/g" ~/.kube/technis
export KUBECONFIG=~/.kube/technis

kubectl cluster-info
kubectl apply -f https://kube-vip.io/manifests/rbac.yaml

echo "Deploying kube-vip as static pods for kube-api VIP..."
for host in ${K3S_SERVERS[@]}; do
  cat << EOF | ssh ${host}.${DOMAIN} 'cat - | sudo tee /var/lib/rancher/k3s/agent/pod-manifests/kube-vip.yaml'
---
apiVersion: v1
kind: Pod
metadata:
  creationTimestamp: null
  name: kube-vip
  namespace: kube-system
spec:
  containers:
  - args:
    - manager
    env:
    - name: vip_arp
      value: "true"
    - name: port
      value: "6443"
    - name: vip_interface
      value: eth0
    - name: vip_cidr
      value: "32"
    - name: cp_enable
      value: "true"
    - name: cp_namespace
      value: kube-system
    - name: vip_ddns
      value: "false"
    - name: svc_enable
      value: "false"
    - name: vip_leaderelection
      value: "true"
    - name: vip_leaseduration
      value: "5"
    - name: vip_renewdeadline
      value: "3"
    - name: vip_retryperiod
      value: "1"
    - name: vip_address
      value: ${KUBE_API_IP}
    image: ghcr.io/kube-vip/kube-vip:v0.7.0
    imagePullPolicy: Always
    name: kube-vip
    resources: {}
    securityContext:
      capabilities:
        add:
        - NET_ADMIN
        - NET_RAW
        - SYS_TIME
    volumeMounts:
    - mountPath: /etc/kubernetes/admin.conf
      name: kubeconfig
  hostAliases:
  - hostnames:
    - kubernetes
    ip: 127.0.0.1
  hostNetwork: true
  volumes:
  - hostPath:
      path: /etc/kubernetes/admin.conf
    name: kubeconfig
status: {}
EOF
done

sleep 30
ping -c 1 ${KUBE_API_IP}
echo "Checking for Cilium..."
if [[ $(helm status cilium -n kube-system -o json 2>/dev/null | jq -r '.info.status') == "deployed" ]]
then
  echo "Cilium already installed. Upgrading..."
  helm_action=upgrade
else
  echo "Deploying Cilium..."
  helm_action=install
fi

helm repo add cilium https://helm.cilium.io/
helm repo update
helm ${helm_action} cilium cilium/cilium \
  --version 1.15.1 \
  --namespace kube-system \
  --set bpf.masquerade=true \
  --set hubble.metrics.enabled="{dns,drop,tcp,flow,icmp,http}" \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --set k8sServiceHost=${KUBE_API_IP} \
  --set k8sServicePort=6443 \
  --set kubeProxyReplacement=strict \
  --set prometheus.enabled=true

sed -i -- "s/192.168.0.11/${KUBE_API_IP}/g" ~/.kube/technis
kubectl cluster-info

kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.3/config/manifests/metallb-native.yaml
sleep 60
echo "Configuring metalLB for service loadbalancing..."
cat << EOF | kubectl apply -f -
---
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: technis
  namespace: metallb-system
spec:
  addresses:
  - 192.168.0.20-192.168.0.29
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: technis
  namespace: metallb-system
EOF

sleep 60
echo "Configuring Traefik with dedicated ingress IP..."
cat << EOF | kubectl apply -f -
---
apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |-
    deployment:
      kind: DaemonSet
    ingressRoute:
      dashboard:
        enabled: true
    nodeSelector:
      node-role.kubernetes.io/control-plane: "true"
    ports:
      web:
        redirectTo:
          port: websecure
    service:
      enabled: true
      type: LoadBalancer
      spec:
        loadBalancerIP: ${KUBE_INGRESS_IP}
EOF

sleep 60
echo "Deploying test pod and loadbalancer service..."
cat << EOF | kubectl apply -f -
---
apiVersion: v1
kind: Service
metadata:
  name: test-lb
spec:
  type: LoadBalancer
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
    name: http
  selector:
    svc: test-lb
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
spec:
  selector:
    matchLabels:
      svc: test-lb
  template:
    metadata:
      labels:
        svc: test-lb
    spec:
      containers:
      - name: web
        image: nginx
        imagePullPolicy: IfNotPresent
        ports:
        - containerPort: 80
        readinessProbe:
          httpGet:
            path: /
            port: 80
EOF

curl -vv "http://$(kubectl -n default get service test-lb -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
