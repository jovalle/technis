"""Generate Technis Grafana drilldowns with native panels and explicit units."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docker/services/grafana/files/dashboards"
DS = {"type": "prometheus", "uid": "victoriametrics"}
HOST = 'host=~"$host"'
NODE = HOST + ',job="node"'
PAGES = {"host": "Host overview", "host-full": "Host detail", "processes": "Processes and ownership",
         "cgroups": "Containers and services", "kernel": "Kernel and hardware",
         "intelligence": "Anomalies and telemetry health", "gpu": "GPUs", "disks": "Disk health"}


def variable(name, query, default=".*", all_values=True):
    return {"name": name, "label": name.capitalize(), "type": "query", "datasource": DS,
            "query": query, "refresh": 1, "sort": 1, "multi": all_values, "includeAll": all_values,
            "allValue": ".*", "current": {"text": "All" if all_values else default,
                                          "value": "$__all" if all_values else default}}


def dashboard(key, description, variables=()):
    return {"id": None, "uid": "technis-" + key, "title": PAGES[key], "schemaVersion": 39,
            "version": 1, "editable": False, "tags": ["technis", "observability"],
            "description": description, "timezone": "browser", "refresh": "5s",
            "time": {"from": "now-1h", "to": "now"}, "panels": [],
            "templating": {"list": [variable("host", 'label_values(node_uname_info{job="node"}, host)',
                                                 "stargate", False), *variables]},
            "links": [{"title": title, "type": "link", "url": "/d/technis-" + uid,
                       "includeVars": True, "keepTime": True} for uid, title in PAGES.items() if uid != key]}


def panel(d, title, expr, unit="short", legend="", kind="timeseries", description=""):
    panels = d["panels"]
    index = len(panels)
    expressions = expr if isinstance(expr, list) else [expr]
    p = {"id": index + 1, "title": title, "type": kind, "datasource": DS,
         "description": description, "gridPos": {"x": index % 2 * 12, "y": index // 2 * 8, "w": 12, "h": 8},
         "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": legend[i] if isinstance(legend,list) else legend, "datasource": DS,
                      "instant": kind == "table", "range": kind != "table",
                      "format": "table" if kind == "table" else "time_series"} for i, e in enumerate(expressions)],
         "fieldConfig": {"defaults": {"unit": unit, "noValue": "No samples", "color": {"mode": "palette-classic"},
                                      "custom": {"lineWidth": 1, "fillOpacity": 8, "spanNulls": False}}, "overrides": []},
         "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
                     "tooltip": {"mode": "multi", "sort": "desc"}}}
    if kind == "table":
        p["options"] = {"showHeader": True, "sortBy": [{"displayName": "Value", "desc": True}]}
        hidden = {k: True for k in ("Time", "__name__", "environment", "instance", "site", "stack", "host")}
        if not any(e.startswith("up{") for e in expressions):
            hidden["job"] = True
        p["transformations"] = [{"id": "organize", "options": {"excludeByName": hidden,
            "indexByName": {}, "renameByName": {"container": "Container", "state": "State", "health": "Health", "name": "Name", "image": "Image", "job": "Job"}}}]
    panels.append(p)
    return p


def metric(name, labels=NODE):
    return name + "{" + labels + "}"


def rate(name, labels=NODE):
    return "rate(" + metric(name, labels) + "[$__rate_interval])"


def save(d):
    (OUT / (d["uid"].removeprefix("technis-") + ".json")).write_text(json.dumps(d, indent=2) + "\n")


def generate():
    d = dashboard("host", "Start here, then follow the detail links. Host metrics use the host network namespace. Missing samples are not zero.")
    panel(d, "CPU by mode", 'sum by (mode) (' + rate("node_cpu_seconds_total",NODE+',mode!="idle"') + ') / count(' + metric('node_cpu_seconds_total', NODE+',mode="idle"') + ')', "percentunit", "{{mode}}")
    panel(d, "Available and total memory", [metric("node_memory_MemAvailable_bytes"), metric("node_memory_MemTotal_bytes")], "bytes", ["Available", "Total"])
    panel(d, "CPU / memory / I/O pressure, exact 10-second averages", metric("technis_pressure_average_ratio", NODE+',window="10"'), "percentunit", "{{resource}} {{scope}}")
    panel(d, "Host network throughput", [rate("node_network_receive_bytes_total", NODE+',device!~"lo|veth.*|br-.*|docker.*"'),rate("node_network_transmit_bytes_total", NODE+',device!~"lo|veth.*|br-.*|docker.*"')], "Bps", ["{{device}} receive", "{{device}} transmit"])
    panel(d, "Disk throughput", [rate("node_disk_read_bytes_total"),rate("node_disk_written_bytes_total")], "Bps", ["{{device}} read", "{{device}} write"])
    panel(d, "Filesystem utilization", '1 - '+metric("node_filesystem_avail_bytes")+' / '+metric("node_filesystem_size_bytes"), "percentunit", "{{mountpoint}}")
    panel(d, "Largest application CPU consumers", 'topk(10, sum by (groupname) ('+rate("namedprocess_namegroup_cpu_seconds_total",HOST)+'))', "cores", "{{groupname}}")
    panel(d, "Running and stopped containers", metric("technis_docker_container_running"), legend="{{container}}", kind="table")
    panel(d, "Collector freshness", 'time() - '+metric("technis_collector_timestamp_seconds"), "s", "{{host}}")
    panel(d, "Scrape availability", metric("up",HOST), legend="{{job}}", kind="table")
    save(d)

    d = dashboard("processes", "Application groups use executable name and effective user. Owner views use numeric UID/GID. No PID or command-line labels are retained.", [variable("group", 'label_values(namedprocess_namegroup_num_procs{'+HOST+'}, groupname)')])
    labels = HOST + ',groupname=~"$group"'
    for title, name, unit, counter in [
        ("CPU", "cpu_seconds_total", "cores", True), ("Resident / virtual / proportional memory", "memory_bytes", "bytes", False),
        ("Processes", "num_procs", "short", False), ("Threads", "num_threads", "short", False),
        ("Disk reads", "read_bytes_total", "Bps", True), ("Disk writes", "write_bytes_total", "Bps", True),
        ("Open descriptors", "open_filedesc", "short", False), ("Descriptor limit utilization", "worst_fd_ratio", "percentunit", False),
        ("Major page faults", "major_page_faults_total", "ops", True), ("Minor page faults", "minor_page_faults_total", "ops", True),
        ("Context switches", "context_switches_total", "ops", True), ("Process states", "states", "short", False)]:
        panel(d, title, (rate if counter else metric)("namedprocess_namegroup_"+name, labels), unit, "{{groupname}} {{mode}} {{memtype}} {{state}} {{ctxswitchtype}}")
    panel(d,"Oldest process age",'time() - '+metric("namedprocess_namegroup_oldest_start_time_seconds",labels),"s","{{groupname}}")
    panel(d,"Process read errors",rate("namedprocess_scrape_procread_errors",HOST),"ops",description="An increase means some process data is unavailable; inspect permissions and process exits.")
    for scope in ["user", "group"]:
        lab=NODE+',scope="'+scope+'"'
        for title, name, unit, counter in [("CPU user", "cpu_user_seconds_total", "cores", True),("CPU system", "cpu_system_seconds_total", "cores", True),
            ("Memory", "memory_bytes", "bytes", False),("Private memory", "private_bytes", "bytes", False),("Swap", "swap_bytes", "bytes", False),
            ("Disk reads", "read_bytes_total", "Bps", True),("Disk writes", "write_bytes_total", "Bps", True),
            ("Logical reads", "logical_read_bytes_total", "Bps", True),("Logical writes", "logical_write_bytes_total", "Bps", True),
            ("Processes", "processes", "short", False),("Threads", "threads", "short", False),("Descriptors", "fds", "short", False)]:
            panel(d, scope.capitalize()+" · "+title,(rate if counter else metric)("technis_owner_"+name,lab),unit,"{{owner}}")
    save(d)

    d = dashboard("cgroups", "Docker containers and systemd cgroups. Parent cgroups include descendants; do not sum parents and children. Pressure values are the kernel's averages.", [variable("cgroup",'label_values(technis_cgroup_cpu_seconds_total{'+HOST+'}, cgroup)')])
    lab=NODE+',cgroup=~"$cgroup"'
    panel(d,"CPU usage",rate("technis_cgroup_cpu_seconds_total",lab+',mode="usage"'),"cores","{{cgroup}}")
    panel(d,"CPU throttling",rate("technis_cgroup_cpu_seconds_total",lab+',mode="throttled"'),"s","{{cgroup}}")
    panel(d,"Memory",metric("technis_cgroup_memory_current",lab),"bytes","{{cgroup}}")
    panel(d,"Memory limit",metric("technis_cgroup_memory_max",lab),"bytes","{{cgroup}}",description="Unlimited cgroups have no finite limit sample.")
    for resource in ["cpu","memory","io"]:
        panel(d,resource.upper()+" pressure, 10 seconds",metric("technis_cgroup_pressure_average_ratio",lab+',window="10",resource="'+resource+'"'),"percentunit","{{cgroup}} {{scope}}")
    panel(d,"Swap usage",metric("technis_cgroup_memory_swap_current",lab),"bytes","{{cgroup}}")
    panel(d,"Processes",metric("technis_cgroup_pids_current",lab),"short","{{cgroup}}")
    panel(d,"Memory events including OOM",rate("technis_cgroup_memory_events_total",lab),"ops","{{cgroup}} {{event}}")
    panel(d,"I/O bandwidth",rate("technis_cgroup_io_total",lab+',operation=~"[rw]bytes"'),"Bps","{{cgroup}} {{device}} {{operation}}")
    panel(d,"I/O operations",rate("technis_cgroup_io_total",lab+',operation=~"[rw]ios"'),"iops","{{cgroup}} {{device}} {{operation}}")
    panel(d,"Memory composition",metric("technis_cgroup_memory_stat_bytes",lab+',field=~"anon|file|kernel|slab|file_dirty|file_writeback"'),"bytes","{{cgroup}} {{field}}")
    panel(d,"Paging and reclaim",rate("technis_cgroup_memory_stat_total",lab),"ops","{{cgroup}} {{field}}")
    panel(d,"Docker state",metric("technis_docker_container_info"),kind="table")
    panel(d,"Docker health",metric("technis_docker_container_health")+" == 1",kind="table")
    panel(d,"Systemd unit state",metric("node_systemd_unit_state")+" == 1",kind="table")
    panel(d,"Systemd restarts",rate("node_systemd_service_restart_total"),"ops","{{name}}")
    panel(d,"Container network receive",rate("technis_cgroup_network_bytes_total",lab+',direction="receive"'),"Bps","{{cgroup}} {{device}}",description="Host and shared network namespaces are excluded to prevent attributing shared traffic to individual containers.")
    panel(d,"Container network transmit",rate("technis_cgroup_network_bytes_total",lab+',direction="transmit"'),"Bps","{{cgroup}} {{device}}")
    save(d)

    d=dashboard("kernel","Exact Linux pressure averages, IPC, CPU idle residency, hardware and collector visibility. Unsupported hardware is shown as no samples.")
    for resource in ["cpu","memory","io"]:
        for scope in ["some","full"]:
            panel(d,resource.upper()+" "+scope+" pressure",metric("technis_pressure_average_ratio",NODE+',resource="'+resource+'",scope="'+scope+'"'),"percentunit","{{window}} seconds")
    panel(d,"IPC objects",metric("technis_ipc_objects"),"short","{{kind}}")
    panel(d,"Semaphores",metric("technis_ipc_nsems"))
    panel(d,"Shared memory",metric("technis_ipc_size"),"bytes")
    panel(d,"IPC message queue bytes",metric("technis_ipc_cbytes"),"bytes")
    panel(d,"CPU idle residency",rate("technis_cpu_idle_seconds_total"),"percentunit","{{cpu}} {{state}}")
    panel(d,"CPU idle transitions",rate("technis_cpu_idle_transitions_total"),"ops","{{cpu}} {{state}}")
    panel(d,"Scheduler wake-up delay",metric("technis_scheduler_wakeup_delay_seconds"),"s","{{statistic}}",description="Excess delay after a 20 ms userspace sleep, summarized over each collection interval. This is scheduling jitter, not CPU idle residency.")
    panel(d,"Scheduler timing samples",metric("technis_scheduler_wakeup_samples"))
    panel(d,"PCIe AER errors",rate("technis_pci_aer_events_total"),"ops","{{device}} {{severity}} {{event}}")
    panel(d,"Memory fragmentation",metric("technis_memory_fragmentation_index"),"short","{{node}} {{zone}} order {{order}}")
    panel(d,"Temperature",metric("node_hwmon_temp_celsius"),"celsius","{{chip}} {{sensor}}")
    panel(d,"Package power",rate("node_rapl_package_joules_total"),"watt","{{index}}")
    panel(d,"CPU frequency",metric("node_cpu_scaling_frequency_hertz"),"hertz","{{cpu}}")
    panel(d,"Core thermal throttles",rate("node_cpu_core_throttles_total"),"ops","{{core}}")
    panel(d,"Hardware sensors",metric("node_hwmon_sensor_label"),kind="table")
    panel(d,"Login sessions",metric("node_logind_sessions"),"short","{{type}} {{class}} {{remote}}")
    panel(d,"Supplemental collector status",metric("technis_collector_success"),kind="table")
    panel(d,"Owner collection permission errors",metric("technis_owner_read_errors"),description="Zero is expected for userspace processes. Nonzero means ownership I/O or descriptor detail is incomplete.")
    save(d)

    d=dashboard("gpu", "Intel/AMD DRM and NVIDIA telemetry selected from the installed hardware. Missing hardware or unsupported counters show no samples. Intel engine utilization requires effective PERFMON capability.")
    panel(d,"Intel / AMD inventory",metric("drm_info",HOST),kind="table")
    panel(d,"NVIDIA inventory",metric("nvidia_smi_gpu_info",HOST),kind="table")
    for title, drm, nv, unit in [
        ("Engine utilization", "drm_engine_utilization_ratio", "nvidia_smi_utilization_gpu_ratio", "percentunit"),
        ("Memory used", "drm_memory_used_bytes", "nvidia_smi_memory_used_bytes", "bytes"),
        ("Memory capacity", "drm_memory_total_bytes", "nvidia_smi_memory_total_bytes", "bytes"),
        ("Power", "drm_power_watts", "nvidia_smi_power_draw_watts", "watt"),
        ("Temperature", "drm_temperature_celsius", "nvidia_smi_temperature_gpu", "celsius")]:
        panel(d,title,[metric(drm,HOST),metric(nv,HOST)],unit,["{{device}} {{engine}} {{pool}} {{domain}} {{sensor}}", "NVIDIA {{uuid}}"])
    panel(d,"Intel / AMD clocks",metric("drm_frequency_hertz",HOST),"hertz","{{device}} {{domain}} {{kind}}")
    panel(d,"NVIDIA graphics clocks",metric("nvidia_smi_clocks_current_graphics_clock_hz",HOST),"hertz","{{uuid}}")
    panel(d,"Intel / AMD fans",metric("drm_fan_speed_rpm",HOST),"rpm","{{device}} {{fan}}")
    panel(d,"NVIDIA fan duty",metric("nvidia_smi_fan_speed_ratio",HOST),"percentunit","{{uuid}}")
    panel(d,"GPU exporter availability",metric("up",HOST+',job=~"drm|nvidia"'),kind="table")
    save(d)

    d=dashboard("disks", "SMART disk health. Device support varies; missing attributes are not zero. SMART collection runs every 60 seconds.")
    lab=HOST+',job="smartctl"'
    panel(d,"Disk inventory",metric("smartctl_device",lab),kind="table")
    panel(d,"SMART health (1 = passed)",metric("smartctl_device_smart_status",lab),kind="table")
    for title, name, unit in [
        ("Disk temperature", "temperature", "celsius"),
        ("Power-on time", "power_on_seconds", "s"),
        ("Power cycles", "power_cycle_count", "short"),
        ("NVMe lifetime used", "percentage_used", "percent"),
        ("NVMe spare capacity", "available_spare", "percent"),
        ("NVMe critical warning", "critical_warning", "short"),
        ("NVMe media errors", "media_errors", "short"),
        ("Error log entries", "num_err_log_entries", "short")]:
        panel(d,title,metric("smartctl_device_"+name,lab),unit,"{{device}} {{temperature_type}}")
    panel(d,"SMART raw attributes",metric("smartctl_device_attribute",lab+',attribute_value_type="raw"'),kind="table")
    panel(d,"SMART command exit bitmask",metric("smartctl_device_smartctl_exit_status",lab),kind="table",description="smartctl uses a bitmask; nonzero can indicate disk health warnings as well as command errors.")
    save(d)

    d=dashboard("intelligence","Statistical deviations, not causal inference or Netdata ML. Baselines exclude the latest five minutes, warm up for one hour, and become more representative after 24 hours. No anomaly pages are sent.", [variable("signal",'label_values(technis:signal{'+HOST+'}, signal)', 'cpu_busy_ratio', False)])
    lab=HOST+',signal=~"$signal"'
    panel(d,"Largest deviations now",'topk(20, '+metric("technis:anomaly_score",HOST)+')',kind="table",description="Absolute standard deviations from the previous baseline. Rank alone does not establish a fault or cause.")
    panel(d,"Largest deviations in selected interval",'topk(20, max_over_time('+metric("technis:anomaly_score",HOST)+'[$__range]))',kind="table")
    panel(d,"Observed and expected range",[metric("technis:signal",lab),metric("technis:upper_band",lab),metric("technis:lower_band",lab)],legend=["Observed", "Upper expected", "Lower expected"])
    panel(d,"Baseline sample count",metric("technis:baseline_samples",lab),legend="{{signal}}",description="At least 120 prior 30-second samples are required before scores appear.")
    panel(d,"Missing scrape targets",metric("up",HOST)+" == 0",kind="table",description="No rows means no currently reported failing targets. Host telemetry absence has a separate alert.")
    panel(d,"Supplemental collection status",metric("technis_collector_success"),kind="table")
    panel(d,"Data age",'time() - '+metric("technis_collector_timestamp_seconds"),"s")
    panel(d,"Scrape duration",metric("scrape_duration_seconds",HOST),"s","{{job}}")
    panel(d,"Samples per scrape",metric("scrape_samples_scraped",HOST),"short","{{job}}")
    panel(d,"Remote-write lag",'time() - '+metric("prometheus_remote_storage_queue_highest_sent_timestamp_seconds",HOST),"s","{{job}}")
    panel(d,"Dropped samples",rate("prometheus_remote_storage_samples_dropped_total",HOST),"ops","{{job}}")
    panel(d,"Supplemental collector duration",metric("technis_collector_duration_seconds"),"s","{{collector}}")
    panel(d,"Cgroup discovery truncated",metric("technis_cgroup_discovery_truncated"),description="Must stay zero for complete discovery.")
    panel(d,"Textfile parse errors",metric("node_textfile_scrape_error"),description="Must stay zero. Errors can remove all supplemental measurements from a scrape.")
    save(d)


if __name__ == "__main__":
    generate()
    print("Generated", len(PAGES)-1, "Technis dashboards")
