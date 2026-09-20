"""Add bounded statistical recording rules and missing-telemetry alerts."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / "docker/services/vmalert/files/rules/metrics.yaml"


def groups():
    signals = {
        "cpu_busy_ratio": '1 - avg by (host) (rate(node_cpu_seconds_total{job="node",mode="idle"}[1m]))',
        "memory_used_ratio": 'max by (host) (1 - node_memory_MemAvailable_bytes{job="node"} / node_memory_MemTotal_bytes{job="node"})',
        "cpu_pressure_ratio": 'max by (host) (technis_pressure_average_ratio{job="node",resource="cpu",scope="some",window="10"})',
        "memory_pressure_ratio": 'max by (host) (technis_pressure_average_ratio{job="node",resource="memory",scope="some",window="10"})',
        "io_pressure_ratio": 'max by (host) (technis_pressure_average_ratio{job="node",resource="io",scope="some",window="10"})',
        "disk_read_mib_s": 'sum by (host) (rate(node_disk_read_bytes_total{job="node",device!~"loop.*|dm-.*"}[1m])) / 1024^2',
        "disk_write_mib_s": 'sum by (host) (rate(node_disk_written_bytes_total{job="node",device!~"loop.*|dm-.*"}[1m])) / 1024^2',
        "network_receive_mib_s": 'sum by (host) (rate(node_network_receive_bytes_total{job="node",device!~"lo|veth.*|br-.*|docker.*|tailscale.*"}[1m])) / 1024^2',
        "tcp_retransmits_s": 'sum by (host) (rate(node_netstat_Tcp_RetransSegs{job="node"}[1m]))',
        "context_switches_s": 'sum by (host) (rate(node_context_switches_total{job="node"}[1m]))',
    }
    rows = [{"record": "technis:signal", "expr": expr, "labels": {"signal": name}}
            for name, expr in signals.items()]
    baseline = [
        {"record": "technis:baseline_mean", "expr": "avg_over_time(technis:signal[24h] offset 5m)"},
        {"record": "technis:baseline_stddev", "expr": "stddev_over_time(technis:signal[24h] offset 5m)"},
        {"record": "technis:baseline_samples", "expr": "count_over_time(technis:signal[24h] offset 5m)"},
    ]
    scored = [
        {"record": "technis:anomaly_score", "expr": "(abs(technis:signal - technis:baseline_mean) / clamp_min(technis:baseline_stddev, 0.02)) and (technis:baseline_samples >= 120)"},
        {"record": "technis:upper_band", "expr": "(technis:baseline_mean + 3 * clamp_min(technis:baseline_stddev, 0.02)) and (technis:baseline_samples >= 120)"},
        {"record": "technis:lower_band", "expr": "clamp_min(technis:baseline_mean - 3 * clamp_min(technis:baseline_stddev, 0.02), 0) and (technis:baseline_samples >= 120)"},
    ]
    alerts = []
    for host in ("stargate", "nexus", "mothership"):
        alerts.append({"alert": "HostDiagnosticTelemetryMissing", "expr": f'absent(technis_collector_timestamp_seconds{{host="{host}"}}) or (time() - technis_collector_timestamp_seconds{{host="{host}"}} > 60)',
                       "for": "5m", "labels": {"host": host, "priority": "P2", "service": "observability"},
                       "annotations": {"summary": "Host diagnostic telemetry is missing", "description": "Check host-observer and kernel-exporter before trusting empty panels."}})
    alerts += [
        {"alert": "HardwareExporterMissing", "expr": 'technis_expected_exporter unless on(host,exporter) label_replace(up{job=~"smartctl|drm|nvidia"} == 1,"exporter","$1","job","(.*)")', "for": "5m",
         "labels": {"priority": "P2", "service": "observability"}, "annotations": {"summary": "An expected hardware exporter is unavailable"}},
        {"alert": "DiskSMARTFailed", "expr": "smartctl_device_smart_status == 0 or smartctl_device_critical_warning > 0", "for": "5m",
         "labels": {"priority": "P2", "service": "storage"}, "annotations": {"summary": "Disk reports failed SMART health or an NVMe critical warning"}},
        {"alert": "NVIDIATelemetryFailed", "expr": 'nvidia_smi_last_collect_success == 0', "for": "5m",
         "labels": {"priority": "P2", "service": "observability"}, "annotations": {"summary": "NVIDIA exporter cannot collect GPU telemetry"}},
        {"alert": "SupplementalCollectorFailed", "expr": "technis_collector_success == 0 or node_textfile_scrape_error{job=\"node\"} == 1", "for": "5m",
         "labels": {"priority": "P2", "service": "observability"}, "annotations": {"summary": "Diagnostic collection is incomplete"}},
        {"alert": "CgroupDiscoveryTruncated", "expr": "technis_cgroup_discovery_truncated > 0", "for": "5m",
         "labels": {"priority": "P2", "service": "observability"}, "annotations": {"summary": "Host exceeds the diagnostic cgroup discovery limit"}},
    ]
    return [{"name": "technis-diagnostic-signals", "interval": "30s", "rules": rows},
            {"name": "technis-diagnostic-baselines", "interval": "30s", "rules": baseline},
            {"name": "technis-diagnostic-scores", "interval": "30s", "rules": scored},
            {"name": "technis-diagnostic-coverage", "interval": "30s", "rules": alerts}]


if __name__ == "__main__":
    document = yaml.safe_load(DEST.read_text())
    document["groups"] = [g for g in document["groups"] if not g["name"].startswith("technis-diagnostic-")] + groups()
    DEST.write_text(yaml.safe_dump(document, sort_keys=False, width=140))
    print("Generated 10 diagnostic signals, guarded baseline scores, and hardware/coverage alerts")
