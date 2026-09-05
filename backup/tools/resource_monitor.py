#!/usr/bin/env python3

import argparse
import csv
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


JAKARTA_TZ = timezone(timedelta(hours=7))


def now_iso_jakarta() -> str:
    return datetime.now(JAKARTA_TZ).replace(microsecond=0).isoformat()


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.returncode, proc.stdout


_UNIT_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]+)?\s*$")


def _parse_size_to_bytes(s: str) -> int:
    s = (s or "").strip()
    if not s:
        return 0
    m = _UNIT_RE.match(s)
    if not m:
        return 0
    num = float(m.group(1))
    unit = (m.group(2) or "B").strip()

    # docker stats uses binary-ish suffixes (KiB/MiB/GiB) but may omit the 'i' (KB/MB/GB)
    unit = unit.replace("i", "")
    scale = {
        "B": 1,
        "KB": 1024,
        "MB": 1024**2,
        "GB": 1024**3,
        "TB": 1024**4,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
    }.get(unit.upper(), 1)

    return int(num * scale)


def _parse_percent(s: str) -> float:
    s = (s or "").strip().rstrip("%")
    try:
        return float(s)
    except Exception:
        return 0.0


@dataclass(frozen=True)
class HostStats:
    load1: float
    load5: float
    load15: float
    mem_total_bytes: int
    mem_avail_bytes: int
    disk_total_bytes: int
    disk_used_bytes: int
    disk_free_bytes: int


def read_host_stats(*, disk_path: str) -> HostStats:
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0

    mem_total = 0
    mem_avail = 0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) * 1024
    except Exception:
        pass

    du = shutil.disk_usage(disk_path)
    disk_total = int(du.total)
    disk_free = int(du.free)
    disk_used = int(du.used)

    return HostStats(
        load1=float(load1),
        load5=float(load5),
        load15=float(load15),
        mem_total_bytes=int(mem_total),
        mem_avail_bytes=int(mem_avail),
        disk_total_bytes=disk_total,
        disk_used_bytes=disk_used,
        disk_free_bytes=disk_free,
    )


def iter_docker_stats() -> list[dict]:
    docker = shutil.which("docker")
    if not docker:
        return []

    # name,cpu,mem_used / mem_limit,mem%,net,block,pids
    fmt = "{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}},{{.BlockIO}},{{.PIDs}}"
    rc, out = _run([docker, "stats", "--no-stream", "--format", fmt])
    if rc != 0:
        return []

    rows: list[dict] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        name, cpu, mem_usage, mem_perc, net_io, block_io, pids = parts[:7]

        mem_used = 0
        mem_limit = 0
        if "/" in mem_usage:
            left, right = [x.strip() for x in mem_usage.split("/", 1)]
            mem_used = _parse_size_to_bytes(left)
            mem_limit = _parse_size_to_bytes(right)

        rows.append(
            {
                "container": name,
                "cpu_percent": _parse_percent(cpu),
                "mem_used_bytes": mem_used,
                "mem_limit_bytes": mem_limit,
                "mem_percent": _parse_percent(mem_perc),
                "net_io": net_io,
                "block_io": block_io,
                "pids": int(pids) if str(pids).isdigit() else 0,
            }
        )

    return rows


def ensure_header(path: str) -> None:
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "timestamp",
                "scope",
                "name",
                "cpu_percent",
                "mem_used_bytes",
                "mem_limit_bytes",
                "mem_percent",
                "host_load1",
                "host_load5",
                "host_load15",
                "host_mem_avail_bytes",
                "host_mem_total_bytes",
                "host_disk_used_bytes",
                "host_disk_total_bytes",
                "host_disk_free_bytes",
                "net_io",
                "block_io",
                "pids",
            ]
        )


def append_rows(path: str, host: HostStats, docker_rows: list[dict]) -> None:
    ts = now_iso_jakarta()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if docker_rows:
            for r in docker_rows:
                w.writerow(
                    [
                        ts,
                        "docker",
                        r.get("container", ""),
                        f"{r.get('cpu_percent', 0.0):.2f}",
                        r.get("mem_used_bytes", 0),
                        r.get("mem_limit_bytes", 0),
                        f"{r.get('mem_percent', 0.0):.2f}",
                        f"{host.load1:.2f}",
                        f"{host.load5:.2f}",
                        f"{host.load15:.2f}",
                        host.mem_avail_bytes,
                        host.mem_total_bytes,
                        host.disk_used_bytes,
                        host.disk_total_bytes,
                        host.disk_free_bytes,
                        r.get("net_io", ""),
                        r.get("block_io", ""),
                        r.get("pids", 0),
                    ]
                )
        else:
            # Still emit one host-only row so the CSV has a heartbeat even if docker isn't available.
            w.writerow(
                [
                    ts,
                    "host",
                    "primary",
                    "0.00",
                    0,
                    0,
                    "0.00",
                    f"{host.load1:.2f}",
                    f"{host.load5:.2f}",
                    f"{host.load15:.2f}",
                    host.mem_avail_bytes,
                    host.mem_total_bytes,
                    host.disk_used_bytes,
                    host.disk_total_bytes,
                    host.disk_free_bytes,
                    "",
                    "",
                    0,
                ]
            )


def main() -> int:
    ap = argparse.ArgumentParser(description="Write host + docker resource usage to a CSV (Asia/Jakarta timestamps).")
    ap.add_argument("--out", default="/home/primary/data/monitor/resource_usage.csv")
    ap.add_argument("--disk-path", default="/home/primary/data")
    args = ap.parse_args()

    ensure_header(args.out)
    host = read_host_stats(disk_path=args.disk_path)
    docker_rows = iter_docker_stats()
    append_rows(args.out, host, docker_rows)

    print(f"<good> Wrote resource usage to {args.out} ({len(docker_rows)} docker rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
