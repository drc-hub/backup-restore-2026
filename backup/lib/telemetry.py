"""telemetry.py — Unified monitoring & telemetry for the backup pipeline.

Three independent CSV streams, each with an idempotent header:

  backup_telemetry.csv   — one row per completed backup cycle
  pg_events.csv          — one row per polling interval (PostgreSQL row counts)
  mongo_events.csv       — one row per polling interval (MongoDB doc counts)
  unstructured_events.csv — one row per polling interval (file / byte counts)

The backup telemetry is driven in-process by BackupTelemetry (called from
lib/runner.py). The DB / unstructured streams are driven externally by
tools/resource_telemetry.py running as an always-on daemon.

All timestamps are ISO-8601 in Asia/Jakarta (UTC+7).
"""

from __future__ import annotations

import csv
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Timezone helper
# ---------------------------------------------------------------------------

JAKARTA_TZ = timezone(timedelta(hours=7))


def _now_iso() -> str:
    return datetime.now(JAKARTA_TZ).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# CSV helpers (Docker-reset-safe: always re-check header on open)
# ---------------------------------------------------------------------------

def _ensure_header(path: str, header: list[str]) -> None:
    """Write header if file does not exist or is empty."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(header)


def _append_row(path: str, header: list[str], row: list) -> None:
    """Append one row — re-checking header in case the file was wiped."""
    _ensure_header(path, header)
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


# ---------------------------------------------------------------------------
# CSV schema definitions
# ---------------------------------------------------------------------------

BACKUP_TELEMETRY_HEADER = [
    # identity
    "timestamp",
    "cycle_id",
    "chain_version",
    # timing — phase durations in seconds
    "pg_basebackup_s",
    "mongo_basebackup_s",
    "pg_wal_extract_s",
    "pg_wal_compress_s",
    "mongo_oplog_extract_s",
    "mongo_oplog_compress_s",
    "unstructured_chunk_s",
    "unstructured_delta_compress_s",
    "verify_s",
    "retention_s",
    "transfer_s",
    "total_cycle_s",
    # size — raw (pre-compression) bytes
    "pg_wal_raw_bytes",
    "pg_basebackup_raw_bytes",
    "mongo_oplog_raw_bytes",
    "mongo_basebackup_raw_bytes",
    "unstructured_raw_bytes",
    # size — stored (post-compression) bytes
    "pg_wal_stored_bytes",
    "mongo_oplog_stored_bytes",
    "unstructured_stored_bytes",
    "cycle_total_stored_bytes",
    # counts
    "wal_file_count",
    "pfc_chunk_count",
    # additional metrics
    "pg_raw_size",
    "pg_compressed_size",
    "mongo_raw_size",
    "mongo_compressed_size",
    "unstr_raw_size",
    "unstr_compressed_size",
    "cycle_raw_total_size",
    "cycle_final_transfered_size",
    "unstr_base_s",
    "unstr_base_size",
]

PG_EVENTS_HEADER = [
    "timestamp",
    "status",          # ok | down | error
    "customers_total",
    "products_total",
    "orders_total",
    "customers_delta",
    "products_delta",
    "orders_delta",
    "query_latency_ms",
]

MONGO_EVENTS_HEADER = [
    "timestamp",
    "status",          # ok | down | error
    "products_total",
    "users_total",
    "orders_total",
    "products_delta",
    "users_delta",
    "orders_delta",
    "query_latency_ms",
]

UNSTRUCTURED_EVENTS_HEADER = [
    "timestamp",
    "status",          # ok | error
    "files_total",
    "bytes_total",
    "files_delta",
    "bytes_delta",
    "scan_latency_ms",
]


# ---------------------------------------------------------------------------
# BackupTelemetry — in-process cycle performance tracker
# ---------------------------------------------------------------------------

class BackupTelemetry:
    """Capture granular per-phase timings and size metrics for one backup cycle.

    Usage (inside runner.py):
        tel = BackupTelemetry()
        tel.start_cycle(cycle_id, chain_version)

        t0 = time.perf_counter()
        ...do pg wal harvest...
        tel.record_timing("pg_wal_extract_s", time.perf_counter() - t0)
        tel.record_size("pg_wal_raw_bytes", pg_stats["copied_bytes"])

        total = time.perf_counter() - cycle_started
        tel.finalize(total)   # → appends one row to backup_telemetry.csv
    """

    OUT_CSV = "/home/primary/data/monitor/backup_telemetry.csv"

    def __init__(self, out_csv: str = OUT_CSV):
        self.out_csv = out_csv
        self._timings: dict[str, float] = {}
        self._sizes: dict[str, int] = {}
        self.cycle_id: Optional[str] = None
        self.chain_version: Optional[str] = None

    # ------------------------------------------------------------------ #

    def start_cycle(self, cycle_id: str, chain_version: str) -> None:
        self.cycle_id = cycle_id
        self.chain_version = chain_version
        self._timings = {k: 0.0 for k in BACKUP_TELEMETRY_HEADER if k.endswith("_s") and k != "timestamp"}
        self._sizes = {k: 0 for k in BACKUP_TELEMETRY_HEADER if k.endswith("_bytes") or k.endswith("_count") or k.endswith("_size")}

    def record_timing(self, key: str, elapsed_s: float) -> None:
        """Record a phase duration. key must be one of the *_s columns."""
        if key in self._timings:
            self._timings[key] = round(elapsed_s, 3)

    # legacy alias used in runner.py
    def record(self, key: str, elapsed_s: float) -> None:
        self.record_timing(key, elapsed_s)

    def record_size(self, key: str, value: int) -> None:
        """Record a byte or count metric. key must be one of the *_bytes, *_count, or *_size columns."""
        if key in self._sizes:
            self._sizes[key] = int(value)

    # legacy alias used in runner.py
    def record_metric(self, key: str, value: int) -> None:
        self.record_size(key, value)

    def finalize(self, total_s: float) -> None:
        """Write one completed row to backup_telemetry.csv."""
        self._timings["total_cycle_s"] = round(total_s, 3)
        ts = _now_iso()
        row = [
            ts,
            self.cycle_id,
            self.chain_version,
            # timings
            self._timings.get("pg_basebackup_s", 0.0),
            self._timings.get("mongo_basebackup_s", 0.0),
            self._timings.get("pg_wal_extract_s", 0.0),
            self._timings.get("pg_wal_compress_s", 0.0),
            self._timings.get("mongo_oplog_extract_s", 0.0),
            self._timings.get("mongo_oplog_compress_s", 0.0),
            self._timings.get("unstructured_chunk_s", 0.0),
            self._timings.get("unstructured_delta_compress_s", 0.0),
            self._timings.get("verify_s", 0.0),
            self._timings.get("retention_s", 0.0),
            self._timings.get("transfer_s", 0.0),
            self._timings.get("total_cycle_s", 0.0),
            # raw sizes
            self._sizes.get("pg_wal_raw_bytes", 0),
            self._sizes.get("pg_basebackup_raw_bytes", 0),
            self._sizes.get("mongo_oplog_raw_bytes", 0),
            self._sizes.get("mongo_basebackup_raw_bytes", 0),
            self._sizes.get("unstructured_raw_bytes", 0),
            # stored sizes
            self._sizes.get("pg_wal_stored_bytes", 0),
            self._sizes.get("mongo_oplog_stored_bytes", 0),
            self._sizes.get("unstructured_stored_bytes", 0),
            self._sizes.get("cycle_total_stored_bytes", 0),
            # counts
            self._sizes.get("wal_file_count", 0),
            self._sizes.get("pfc_chunk_count", 0),
            # additional metrics
            self._sizes.get("pg_raw_size", 0),
            self._sizes.get("pg_compressed_size", 0),
            self._sizes.get("mongo_raw_size", 0),
            self._sizes.get("mongo_compressed_size", 0),
            self._sizes.get("unstr_raw_size", 0),
            self._sizes.get("unstr_compressed_size", 0),
            self._sizes.get("cycle_raw_total_size", 0),
            self._sizes.get("cycle_final_transfered_size", 0),
            self._timings.get("unstr_base_s", 0.0),
            self._sizes.get("unstr_base_size", 0),
        ]
        _append_row(self.out_csv, BACKUP_TELEMETRY_HEADER, row)


# ---------------------------------------------------------------------------
# DBTelemetry — external polling daemon writer
# ---------------------------------------------------------------------------

class DBTelemetry:
    """Write one row per polling tick to pg_events.csv / mongo_events.csv /
    unstructured_events.csv.

    Designed to be instantiated once at daemon startup and called in a loop.
    The Docker-reset-safe _append_row / _ensure_header pair guarantees the
    CSV files are re-created after any data wipe without restarting the daemon.
    """

    def __init__(self, out_dir: str = "/home/primary/data/monitor"):
        self.out_dir = out_dir
        self.pg_csv = os.path.join(out_dir, "pg_events.csv")
        self.mongo_csv = os.path.join(out_dir, "mongo_events.csv")
        self.unstructured_csv = os.path.join(out_dir, "unstructured_events.csv")
        # Keep previous totals to compute deltas
        self._prev_pg: Optional[tuple[int, int, int]] = None
        self._prev_mongo: Optional[tuple[int, int, int]] = None
        self._prev_unstr: Optional[tuple[int, int]] = None

    # ------------------------------------------------------------------ #
    # PostgreSQL
    # ------------------------------------------------------------------ #

    def poll_pg(self, *, container: str, db: str = "transactiondb", user: str = "postgresql") -> None:
        cmd = [
            "docker", "exec", "-i", container,
            "psql", "-U", user, "-d", db,
            "-t", "-A", "-F", ",", "-c",
            (
                "SELECT "
                "(SELECT count(*) FROM customers),"
                "(SELECT count(*) FROM products),"
                "(SELECT count(*) FROM orders);"
            ),
        ]
        ts = _now_iso()
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
            lat_ms = round((time.perf_counter() - t0) * 1000, 1)
            rc = proc.returncode
            out = proc.stdout or ""
        except Exception as e:
            lat_ms = round((time.perf_counter() - t0) * 1000, 1)
            _append_row(self.pg_csv, PG_EVENTS_HEADER,
                        [ts, "error", "", "", "", "", "", "", lat_ms])
            return

        if rc != 0:
            _append_row(self.pg_csv, PG_EVENTS_HEADER,
                        [ts, "down", "", "", "", "", "", "", lat_ms])
            self._prev_pg = None
            return

        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if not lines:
            _append_row(self.pg_csv, PG_EVENTS_HEADER,
                        [ts, "error", "", "", "", "", "", "", lat_ms])
            return

        try:
            parts = lines[-1].split(",")
            cust, prod, orders = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            _append_row(self.pg_csv, PG_EVENTS_HEADER,
                        [ts, "error", "", "", "", "", "", "", lat_ms])
            return

        prev = self._prev_pg or (cust, prod, orders)
        d_cust, d_prod, d_ord = cust - prev[0], prod - prev[1], orders - prev[2]
        self._prev_pg = (cust, prod, orders)

        _append_row(self.pg_csv, PG_EVENTS_HEADER,
                    [ts, "ok", cust, prod, orders, d_cust, d_prod, d_ord, lat_ms])

    # ------------------------------------------------------------------ #
    # MongoDB
    # ------------------------------------------------------------------ #

    def poll_mongo(self, *, container: str, uri: str) -> None:
        eval_js = (
            "print("
            "db.products.countDocuments()+',' +"
            "db.users.countDocuments()+',' +"
            "db.orders.countDocuments()"
            ");"
        )
        cmd = ["docker", "exec", "-i", container, "mongosh", uri, "--quiet", "--eval", eval_js]
        ts = _now_iso()
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
            lat_ms = round((time.perf_counter() - t0) * 1000, 1)
            rc = proc.returncode
            out = proc.stdout or ""
        except Exception:
            lat_ms = round((time.perf_counter() - t0) * 1000, 1)
            _append_row(self.mongo_csv, MONGO_EVENTS_HEADER,
                        [ts, "error", "", "", "", "", "", "", lat_ms])
            return

        if rc != 0:
            _append_row(self.mongo_csv, MONGO_EVENTS_HEADER,
                        [ts, "down", "", "", "", "", "", "", lat_ms])
            self._prev_mongo = None
            return

        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if not lines:
            _append_row(self.mongo_csv, MONGO_EVENTS_HEADER,
                        [ts, "error", "", "", "", "", "", "", lat_ms])
            return

        try:
            parts = lines[-1].split(",")
            prod, users, orders = int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            _append_row(self.mongo_csv, MONGO_EVENTS_HEADER,
                        [ts, "error", "", "", "", "", "", "", lat_ms])
            return

        prev = self._prev_mongo or (prod, users, orders)
        d_prod, d_users, d_ord = prod - prev[0], users - prev[1], orders - prev[2]
        self._prev_mongo = (prod, users, orders)

        _append_row(self.mongo_csv, MONGO_EVENTS_HEADER,
                    [ts, "ok", prod, users, orders, d_prod, d_users, d_ord, lat_ms])

    # ------------------------------------------------------------------ #
    # Unstructured files
    # ------------------------------------------------------------------ #

    def poll_unstructured(self, *, dir_path: str) -> None:
        ts = _now_iso()
        t0 = time.perf_counter()
        file_count = 0
        total_bytes = 0
        status = "ok"
        try:
            for root, _dirs, files in os.walk(dir_path):
                file_count += len(files)
                for fn in files:
                    try:
                        total_bytes += os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        pass
        except OSError:
            status = "error"
        lat_ms = round((time.perf_counter() - t0) * 1000, 1)

        prev = self._prev_unstr or (file_count, total_bytes)
        d_files = file_count - prev[0]
        d_bytes = total_bytes - prev[1]
        self._prev_unstr = (file_count, total_bytes)

        _append_row(self.unstructured_csv, UNSTRUCTURED_EVENTS_HEADER,
                    [ts, status, file_count, total_bytes, d_files, d_bytes, lat_ms])


__all__ = [
    "BackupTelemetry",
    "DBTelemetry",
    "BACKUP_TELEMETRY_HEADER",
    "PG_EVENTS_HEADER",
    "MONGO_EVENTS_HEADER",
    "UNSTRUCTURED_EVENTS_HEADER",
]
