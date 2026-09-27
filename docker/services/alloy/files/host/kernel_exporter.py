"""Supplement node/process exporters with Linux PSI, cgroups, IPC and ownership.

No listening socket. Writes an atomic Prometheus textfile for node exporter.
Reads only kernel files and the existing restricted Docker API. No command lines,
PIDs, container IDs or environment variables are exported as labels.
"""
import collections
import json
import math
import os
from pathlib import Path
import re
import time
import threading
import urllib.request

PROC = Path(os.getenv("HOST_PROC", "/host/proc"))
SYS = Path(os.getenv("HOST_SYS", "/host/sys"))
OUT = Path(os.getenv("TEXTFILE_DIR", "/textfile"))
HZ = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def read(path):
    return path.read_text().strip()


def pairs(text):
    return {parts[0]: parts[1] if len(parts) == 2 else ""
            for line in text.splitlines() if (parts := line.split(None, 1))}


def psi(text):
    return {line.split()[0]: dict(item.split("=") for item in line.split()[1:])
            for line in text.splitlines() if line.strip()}


def process_stat(text):
    # comm can contain spaces and parentheses; fields after its final ')' are fixed.
    left, right = text.index("("), text.rindex(")")
    fields = text[right + 2:].split()
    return text[left + 1:right], fields


class Metrics:
    def __init__(self):
        self.lines = []
        self.types = set()

    def add(self, name, value, metric_type="gauge", **labels):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite metric " + name)
        name = "technis_" + name
        if name not in self.types:
            self.lines.extend([f"# HELP {name} Linux supplemental collector {name}.",
                               f"# TYPE {name} {metric_type}"])
            self.types.add(name)
        suffix = "{" + ",".join(f"{k}={json.dumps(str(v), ensure_ascii=False)}"
                                for k, v in sorted(labels.items())) + "}" if labels else ""
        self.lines.append(f"{name}{suffix} {number:.12g}")


class Collector:
    def __init__(self):
        self.previous = {}
        self.counters = collections.defaultdict(float)
        self.docker_names = {}
        self.docker_networks = {}
        self.jitter_lock = threading.Lock()
        self.jitter = []

    def sample_jitter(self):
        while True:
            start = time.monotonic()
            time.sleep(0.02)
            delay = max(0, time.monotonic() - start - 0.02)
            with self.jitter_lock:
                # Keep a bounded diagnostic window if filesystem collection stalls.
                self.jitter.append(delay)
                if len(self.jitter) > 1000:
                    self.jitter.pop(0)

    def scheduler(self, m):
        with self.jitter_lock:
            values, self.jitter = self.jitter, []
        if values:
            for stat, value in {"max": max(values), "min": min(values), "mean": sum(values)/len(values)}.items():
                m.add("scheduler_wakeup_delay_seconds", value, statistic=stat)
            m.add("scheduler_wakeup_samples", len(values))

    def pressure(self, m):
        for resource in ("cpu", "memory", "io"):
            for scope, values in psi(read(PROC / "pressure" / resource)).items():
                for window in ("10", "60", "300"):
                    m.add("pressure_average_ratio", float(values["avg" + window]) / 100,
                          resource=resource, scope=scope, window=window)
                m.add("pressure_stall_seconds_total", float(values["total"]) / 1e6,
                      "counter", resource=resource, scope=scope)

    def ipc(self, m):
        for kind, fields in {"sem": ["nsems"], "shm": ["size", "rss", "swap"],
                             "msg": ["cbytes", "qnum"]}.items():
            lines = read(PROC / "sysvipc" / kind).splitlines()
            rows = [dict(zip(lines[0].split(), line.split())) for line in lines[1:]]
            m.add("ipc_objects", len(rows), kind=kind)
            for field in fields:
                m.add("ipc_" + field, sum(int(row.get(field, 0)) for row in rows), kind=kind)

    def hardware(self, m):
        for path in SYS.glob("devices/system/cpu/cpu[0-9]*/cpuidle/state*"):
            cpu, state = path.parts[-3], read(path / "name")
            m.add("cpu_idle_seconds_total", int(read(path / "time")) / 1e6,
                  "counter", cpu=cpu, state=state)
            m.add("cpu_idle_transitions_total", read(path / "usage"), "counter", cpu=cpu, state=state)
        for path in SYS.glob("bus/pci/devices/*/aer_*"):
            text = read(path)
            values = {"total": text} if text.isdigit() else pairs(text)
            for key, value in values.items():
                m.add("pci_aer_events_total", value, "counter", device=path.parent.name,
                      severity=path.name, event=key)
        for path in SYS.glob("kernel/debug/extfrag/extfrag_index"):
            for line in read(path).splitlines():
                match = re.match(r"Node\s+(\d+),\s+zone\s+(\S+)\s+(.+)", line)
                if match:
                    for order, value in enumerate(match[3].split()):
                        m.add("memory_fragmentation_index", value,
                              node=match[1], zone=match[2], order=order)

    def docker(self, m):
        api = os.getenv("DOCKER_API", "http://docker-socket-proxy:2375")
        with urllib.request.urlopen(api + "/containers/json?all=1", timeout=2) as r:
            containers = json.load(r)
        self.docker_names = {c["Id"]: c["Names"][0].lstrip("/") for c in containers if c.get("Names")}
        self.docker_networks = {c["Id"]: c.get("HostConfig", {}).get("NetworkMode", "") for c in containers}
        for c in containers:
            name = self.docker_names.get(c["Id"], "unnamed")
            m.add("docker_container_info", 1, container=name, image=c["Image"], state=c["State"])
            m.add("docker_container_running", int(c["State"] == "running"), container=name)
            status = c.get("Status", "")
            for health in ("healthy", "unhealthy", "starting"):
                m.add("docker_container_health", int("(" + health + ")" in status or
                      "(health: " + health + ")" in status), container=name, health=health)
        m.add("docker_containers", len(containers))

    def cgroups(self, m):
        root = SYS / "fs/cgroup"
        paths = list(root.rglob("cpu.stat"))
        # ponytail: cap discovery at 4096 cgroups; partition collection if a host exceeds it.
        m.add("cgroup_discovery_truncated", int(len(paths) > 4096))
        for stat in paths[:4096]:
            path = stat.parent
            relative = str(path.relative_to(root))
            if re.search(r"/(session-\d+\.scope|user@\d+\.service)/", "/" + relative + "/"):
                continue
            match = re.search(r"(?:docker-|/)([a-f0-9]{64})(?:\.scope)?$", relative)
            if match:
                name = self.docker_names.get(match[1])
                if not name:
                    continue  # Discovery failure is exposed separately; no permanent ID labels.
                label = "docker/" + name
            else:
                label = relative
            labels = {"cgroup": label, "kind": "container" if match else "service"}
            try:
                cpu = pairs(read(stat))
                if match and not self.docker_networks.get(match[1], "").startswith(("host", "container:")):
                    procs = read(path / "cgroup.procs").splitlines()
                    if procs:
                        # /proc/PID/net is readable without relaxing AppArmor.
                        # Host/shared network namespaces are excluded to avoid false attribution.
                        net = read(PROC / procs[0] / "net/dev")
                        fields = ["bytes", "packets", "errors", "drops", "fifo", "frame", "compressed", "multicast"]
                        for line in net.splitlines()[2:]:
                            device, values = line.split(":", 1)
                            values = values.split()
                            if device.strip() == "lo":
                                continue
                            for direction, offset in (("receive", 0), ("transmit", 8)):
                                for index, field in enumerate(fields[:5]):
                                    m.add("cgroup_network_" + field + "_total", values[offset+index], "counter",
                                          device=device.strip(), direction=direction, **labels)
                for key in ("usage_usec", "user_usec", "system_usec", "throttled_usec"):
                    if key in cpu:
                        m.add("cgroup_cpu_seconds_total", int(cpu[key]) / 1e6,
                              "counter", mode=key.removesuffix("_usec"), **labels)
                for key in ("nr_periods", "nr_throttled"):
                    if key in cpu:
                        m.add("cgroup_cpu_events_total", cpu[key], "counter", event=key, **labels)
                for resource in ("cpu", "memory", "io"):
                    pressure = path / (resource + ".pressure")
                    if pressure.exists():
                        for scope, values in psi(read(pressure)).items():
                            for window in ("10", "60", "300"):
                                m.add("cgroup_pressure_average_ratio", float(values["avg" + window]) / 100,
                                      resource=resource, scope=scope, window=window, **labels)
                            m.add("cgroup_pressure_stall_seconds_total", float(values["total"]) / 1e6,
                                  "counter", resource=resource, scope=scope, **labels)
                for filename in ("memory.current", "memory.max", "memory.swap.current", "memory.swap.max", "pids.current", "pids.max"):
                    if (path / filename).exists():
                        value = read(path / filename)
                        if value != "max":
                            m.add("cgroup_" + filename.replace(".", "_"), value, **labels)
                if (path / "memory.stat").exists():
                    for key, value in pairs(read(path / "memory.stat")).items():
                        counter = key.startswith(("pg", "workingset", "thp"))
                        m.add("cgroup_memory_stat" + ("_total" if counter else "_bytes"),
                              value, "counter" if counter else "gauge", field=key, **labels)
                if (path / "memory.events").exists():
                    for event, value in pairs(read(path / "memory.events")).items():
                        m.add("cgroup_memory_events_total", value, "counter", event=event, **labels)
                if (path / "io.stat").exists():
                    for line in read(path / "io.stat").splitlines():
                        parts = line.split()
                        for item in parts[1:]:
                            key, value = item.split("=")
                            m.add("cgroup_io_total", value, "counter", device=parts[0], operation=key, **labels)
            except FileNotFoundError:
                continue  # The cgroup exited while we read its snapshot.

    def owners(self, m):
        gauges = collections.defaultdict(float)
        current = {}
        errors = 0
        now_boot = float(read(PROC / "uptime").split()[0])
        for path in PROC.glob("[0-9]*"):
            try:
                comm, fields = process_stat(read(path / "stat"))
                if int(fields[6]) & 0x00200000:
                    continue  # PF_KTHREAD has no userspace descriptors or ownership workload.
                status = pairs(read(path / "status"))
                uid, gid = status["Uid:"].split()[1], status["Gid:"].split()[1]
                identity = (path.name, fields[19])
                counters = {"cpu_user_seconds": int(fields[11]) / HZ,
                            "cpu_system_seconds": int(fields[12]) / HZ,
                            "minor_faults": int(fields[7]), "major_faults": int(fields[9]),
                            "voluntary_context_switches": int(status.get("voluntary_ctxt_switches:", 0)),
                            "involuntary_context_switches": int(status.get("nonvoluntary_ctxt_switches:", 0))}
                try:
                    io = pairs(read(path / "io"))
                    counters.update({"read_bytes": int(io["read_bytes:"]), "write_bytes": int(io["write_bytes:"]),
                                     "logical_read_bytes": int(io["rchar:"]), "logical_write_bytes": int(io["wchar:"])})
                except PermissionError:
                    errors += 1
                try:
                    fds = sum(1 for _ in (path / "fd").iterdir())
                except PermissionError:
                    errors += 1
                    fds = None
                previous = self.previous.get(identity, {})
                current[identity] = previous | counters
                values = {"processes": 1, "threads": int(fields[17]),
                          "memory_bytes": int(fields[21]) * PAGE, "virtual_bytes": int(fields[20]),
                          "swap_bytes": int(status.get("VmSwap:", "0").split()[0]) * 1024,
                          "private_bytes": int(status.get("RssAnon:", "0").split()[0]) * 1024}
                if fds is not None:
                    values["fds"] = fds
                for scope, owner in (("user", uid), ("group", gid)):
                    for field, value in values.items():
                        gauges[(scope, owner, field)] += value
                    gauges[(scope, owner, "oldest_age_seconds")] = max(
                        gauges[(scope, owner, "oldest_age_seconds")], now_boot - int(fields[19]) / HZ)
                    for field, value in counters.items():
                        self.counters[(scope, owner, field)] += max(0, value - previous.get(field, 0))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                errors += 1
        self.previous = current
        for (scope, owner, field), value in gauges.items():
            m.add("owner_" + field, value, scope=scope, owner=owner)
        for (scope, owner, field), value in self.counters.items():
            m.add("owner_" + field + "_total", value, "counter", scope=scope, owner=owner)
        m.add("owner_read_errors", errors)

    def collect(self):
        metrics = Metrics()
        for name in ("docker", "pressure", "ipc", "hardware", "cgroups", "owners", "scheduler"):
            start = time.monotonic()
            try:
                getattr(self, name)(metrics)
                success = 1
            except (OSError, ValueError, KeyError, IndexError) as exc:
                print(f"collector {name}: {type(exc).__name__}: {exc}", flush=True)
                success = 0
            metrics.add("collector_success", success, collector=name)
            metrics.add("collector_duration_seconds", time.monotonic() - start, collector=name)
        metrics.add("collector_timestamp_seconds", time.time())
        return "\n".join(metrics.lines) + "\n"


def main():
    collector = Collector()
    threading.Thread(target=collector.sample_jitter, daemon=True).start()
    interval = float(os.getenv("EXPORT_INTERVAL", "5"))
    if interval < 1:
        raise ValueError("EXPORT_INTERVAL must be at least one second")
    OUT.mkdir(parents=True, exist_ok=True)
    while True:
        start = time.monotonic()
        text = collector.collect()
        temporary = OUT / "kernel.prom.tmp"
        temporary.write_text(text)
        temporary.replace(OUT / "kernel.prom")
        time.sleep(max(0.1, interval - (time.monotonic() - start)))


if __name__ == "__main__":
    main()
