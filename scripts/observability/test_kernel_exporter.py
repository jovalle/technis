"""Run with python3 scripts/observability/test_kernel_exporter.py."""
import importlib.util
from pathlib import Path
import tempfile

source = Path(__file__).resolve().parents[2] / "docker/services/host-observer/files/kernel_exporter.py"
spec = importlib.util.spec_from_file_location("kernel_exporter", source)
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    k.PROC = root / "proc"
    k.SYS = root / "sys"
    (k.PROC / "pressure").mkdir(parents=True)
    for resource in ("cpu", "memory", "io"):
        (k.PROC / "pressure" / resource).write_text(
            "some avg10=12.50 avg60=2.00 avg300=0.25 total=1250000\n"
            "full avg10=1.25 avg60=0.20 avg300=0.00 total=250000\n")
    m = k.Metrics()
    collector = k.Collector()
    collector.pressure(m)
    assert 'technis_pressure_average_ratio{resource="cpu",scope="some",window="10"} 0.125' in m.lines
    assert 'technis_pressure_stall_seconds_total{resource="io",scope="full"} 0.25' in m.lines

    path = k.PROC / "42"
    path.mkdir()
    (path / "fd").mkdir()
    (path / "status").write_text("Uid:\t1000 1000 1000 1000\nGid:\t100 100 100 100\nGroups:\nVmSwap:\t4 kB\n")
    (path / "io").write_text("read_bytes: 20\nwrite_bytes: 30\nrchar: 40\nwchar: 50\n")
    (k.PROC / "uptime").write_text("1000 0\n")
    # Linux stat fields 3..24: state, parent/session flags, faults, CPU, threads,
    # start ticks, virtual size and resident pages. comm deliberately contains ')'.
    fields = ["S", "1", "1", "1", "0", "0", "0", "7", "0", "2", "0",
              "100", "50", "0", "0", "20", "0", "2", "0", "500", "4096", "3", "0"]
    (path / "stat").write_text("42 (test ) worker) " + " ".join(fields))
    assert k.process_stat((path / "stat").read_text())[0] == "test ) worker"
    m = k.Metrics()
    collector.owners(m)
    assert 'technis_owner_swap_bytes{owner="1000",scope="user"} 4096' in m.lines
    assert collector.counters[("group", "100", "read_bytes")] == 20
    # Reading an unchanged process twice must not double count lifetime counters.
    collector.owners(k.Metrics())
    assert collector.counters[("group", "100", "read_bytes")] == 20
    (path / "io").write_text("read_bytes: 27\nwrite_bytes: 30\nrchar: 40\nwchar: 50\n")
    collector.owners(k.Metrics())
    assert collector.counters[("group", "100", "read_bytes")] == 27
    # PID reuse with a different start time starts a new counter contribution.
    fields[19] = "900"
    (path / "stat").write_text("42 (replacement) " + " ".join(fields))
    (path / "io").write_text("read_bytes: 3\nwrite_bytes: 0\nrchar: 0\nwchar: 0\n")
    collector.owners(k.Metrics())
    assert collector.counters[("group", "100", "read_bytes")] == 30

    container_id = "a" * 64
    cg = k.SYS / "fs/cgroup" / ("docker-" + container_id + ".scope")
    cg.mkdir(parents=True)
    (cg / "cpu.stat").write_text("usage_usec 2500000\n")
    (cg / "cgroup.procs").write_text("42\n")
    (path / "net").mkdir()
    (path / "net/dev").write_text("header\nheader\n eth0: 1024 2 0 0 0 0 0 0 2048 4 0 0 0 0 0 0\n")
    collector.docker_names[container_id] = "web"
    collector.docker_networks[container_id] = "bridge"
    m = k.Metrics()
    collector.cgroups(m)
    assert 'technis_cgroup_network_bytes_total{cgroup="docker/web",device="eth0",direction="receive",kind="container"} 1024' in m.lines
    assert all(container_id not in line for line in m.lines)
    collector.docker_networks[container_id] = "host"
    m = k.Metrics()
    collector.cgroups(m)
    assert not any(line.startswith('technis_cgroup_network_') for line in m.lines)

    m = k.Metrics()
    m.add("example", 2, kind='service"line\n')
    assert m.lines[-1] == 'technis_example{kind="service\\"line\\n"} 2'
print("PASS: PSI units, process parsing, ownership, cumulative accounting, PID reuse, network namespace attribution, label escaping")
