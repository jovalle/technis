# Source: https://raw.githubusercontent.com/prometheus-operator/kube-prometheus/main/example.jsonnet

local ingress(metadata, domain, service) = {
  apiVersion: 'networking.k8s.io/v1',
  kind: 'Ingress',
  metadata: metadata,
  spec: {
    rules: [{
      host: domain,
      http: {
        paths: [{
          path: '/',
          pathType: 'Prefix',
          backend: {
            service: service,
          },
        }],
      },
    }],
  },
};

local kp =
  (import 'kube-prometheus/main.libsonnet') +
  // TODO: add loki, pihole-exporter, rpi-exporter, snmp-exporter, uptime-kuma
  // Uncomment the following imports to enable its patches
  // (import 'kube-prometheus/addons/anti-affinity.libsonnet') +
  // (import 'kube-prometheus/addons/managed-cluster.libsonnet') +
  // (import 'kube-prometheus/addons/node-ports.libsonnet') +
  // (import 'kube-prometheus/addons/static-etcd.libsonnet') +
  // (import 'kube-prometheus/addons/custom-metrics.libsonnet') +
  // (import 'kube-prometheus/addons/external-metrics.libsonnet') +
  {
    values+:: {
      common+: {
        namespace: 'monitoring',
        baseDomain: 'k8s.techn.is',
      },
      other: {},
    },
    alertmanager+: {
      alertmanager+: {
        spec+: {
          externalUrl: 'https://alertmanager.' + $.values.common.baseDomain,
        },
      },
      ingress: ingress(
        { name: 'grafana' },
        'grafana.' + $.values.common.baseDomain,
        {
          name: $.grafana.service.metadata.name,
          port: {
            name: $.grafana.service.spec.ports[0].name,
          },
        },
      ),
    },
    grafana+: {
      ingress: ingress(
        {
          name: 'grafana',
          namespace: $.values.common.namespace,
        },
        'grafana.' + $.values.common.baseDomain,
        {
          name: $.grafana.service.metadata.name,
          port: {
            name: $.grafana.service.spec.ports[0].name,
          },
        },
      ),
    },
    prometheus+: {
      prometheus+: {
        spec+: {
          externalUrl: 'https://prometheus.' + $.values.common.baseDomain,
          replicas: 1,
          retention: '7d',
          retentionSize: '40GB',
          // nodeSelector+: {
          //   'kubernetes.io/hostname': 'nexus',
          // },
          storage: {
            volumeClaimTemplate: {
              metadata: {
                name: 'promdata',
              },
              spec: {
                // storageClassName: 'local-path',
                storageClassName: 'longhorn',
                accessModes: ['ReadWriteOnce'],
                resources: {
                  requests: { storage: '40Gi' },
                },
              },
            },
          },
          // additionalScrapeConfigs: {
          //   scrape_configs: [{
          //     job_name: 'pihole',
          //     static_configs: [{
          //       targets: ['192.168.1.2:9617'],
          //     }],
          //   }],
          // },
        },
      },
      ingress: ingress(
        {
          name: 'prometheus',
          namespace: $.values.common.namespace,
        },
        'prometheus.' + $.values.common.baseDomain,
        {
          name: $.prometheus.service.metadata.name,
          port: {
            name: $.prometheus.service.spec.ports[0].name,
          },
        },
      ),
    },
  };

{ 'setup/0namespace-namespace': kp.kubePrometheus.namespace } +
{
  ['setup/prometheus-operator-' + name]: kp.prometheusOperator[name]
  for name in std.filter((function(name) name != 'serviceMonitor' && name != 'prometheusRule'), std.objectFields(kp.prometheusOperator))
} +
// serviceMonitor and prometheusRule are separated so that they can be created after the CRDs are ready
{ 'prometheus-operator-serviceMonitor': kp.prometheusOperator.serviceMonitor } +
{ 'prometheus-operator-prometheusRule': kp.prometheusOperator.prometheusRule } +
{ 'kube-prometheus-prometheusRule': kp.kubePrometheus.prometheusRule } +
{ ['alertmanager-' + name]: kp.alertmanager[name] for name in std.objectFields(kp.alertmanager) } +
{ ['blackbox-exporter-' + name]: kp.blackboxExporter[name] for name in std.objectFields(kp.blackboxExporter) } +
{ ['grafana-' + name]: kp.grafana[name] for name in std.objectFields(kp.grafana) } +
{ ['kube-state-metrics-' + name]: kp.kubeStateMetrics[name] for name in std.objectFields(kp.kubeStateMetrics) } +
{ ['kubernetes-' + name]: kp.kubernetesControlPlane[name] for name in std.objectFields(kp.kubernetesControlPlane) }
{ ['node-exporter-' + name]: kp.nodeExporter[name] for name in std.objectFields(kp.nodeExporter) } +
{ ['prometheus-' + name]: kp.prometheus[name] for name in std.objectFields(kp.prometheus) } +
{ ['prometheus-adapter-' + name]: kp.prometheusAdapter[name] for name in std.objectFields(kp.prometheusAdapter) }

