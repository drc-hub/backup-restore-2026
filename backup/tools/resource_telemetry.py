#!/usr/bin/env python3
"""resource_telemetry.py — Always-on DB & unstructured event daemon.

Polls PostgreSQL, MongoDB, and the unstructured directory on a fixed interval
and writes rows to the unified telemetry CSVs managed by lib/telemetry.py:

    pg_events.csv
    mongo_events.csv
    unstructured_events.csv

Resource-usage metrics (CPU, RAM, disk I/O) are delegated to Netdata.

Usage:
    python3 -u utilities/backup/tools/resource_telemetry.py
    python3 -u utilities/backup/tools/resource_telemetry.py --once
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

# Resolve lib/ relative to this file so it can be run from any cwd.
TOOLS_DIR = os.path.abspath(os.path.dirname(__file__))
BACKUP_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
if BACKUP_ROOT not in sys.path:
    sys.path.insert(0, BACKUP_ROOT)

from lib.telemetry import DBTelemetry  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

JAKARTA_TZ = timezone(timedelta(hours=7))
LOG_FILE = "/home/primary/utilities/backup/backup.log"


def _now_iso() -> str:
    return datetime.now(JAKARTA_TZ).replace(microsecond=0).isoformat()


def _isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    return _isatty() and os.environ.get("LOG_COLOR", "1") != "0"


def _c(s: str, code: str) -> str:
    if not _color_enabled():
        return s
    return f"\x1b[{code}m{s}\x1b[0m"


def good_tag() -> str: return _c("<good>", "32")
def bad_tag() -> str:  return _c("<error>", "31")
def info_tag() -> str: return _c("<info>", "36")
def warn_tag() -> str: return _c("<warn>", "33")


_ANSI = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def _log(scope: str, tag: str, msg: str) -> None:
    clean_tag = _ANSI.sub("", tag)
    print(f"{scope:<10} {tag:<8} {msg}")
    try:
        ts = _now_iso()
        line = f"[{ts}] {scope:<10} {clean_tag:<8} {msg}\n"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Always-on DB & unstructured event telemetry daemon. "
            "Writes pg_events.csv, mongo_events.csv, unstructured_events.csv."
        )
    )
    ap.add_argument("--interval-sec",      type=int,  default=10,
                    help="Polling interval in seconds (default: 10)")
    ap.add_argument("--out-dir",           default="/home/primary/data/monitor",
                    help="Output directory for CSV files")
    ap.add_argument("--pg-container",      default="postgres_live")
    ap.add_argument("--pg-db",             default="transactiondb")
    ap.add_argument("--pg-user",           default="postgresql")
    ap.add_argument("--mongo-container",   default="mongodb_live")
    ap.add_argument("--mongo-uri",
                    default="mongodb://mongodb:password@127.0.0.1:27017/test?authSource=admin")
    ap.add_argument("--unstructured-dir",  default="/home/primary/data/unstructured")
    ap.add_argument("--once",              action="store_true",
                    help="Run exactly one poll cycle then exit (for testing)")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out_dir)
    tel = DBTelemetry(out_dir=out_dir)

    _log("telemetry", info_tag(),
         f"out_dir={out_dir}  interval={args.interval_sec}s  "
         f"pg={args.pg_container}  mongo={args.mongo_container}")

    while True:
        # -- PostgreSQL --------------------------------------------------
        try:
            tel.poll_pg(
                container=args.pg_container,
                db=args.pg_db,
                user=args.pg_user,
            )
            _log("pg", good_tag(), "polled → pg_events.csv")
        except Exception as exc:
            _log("pg", bad_tag(), f"poll error: {exc}")

        # -- MongoDB -----------------------------------------------------
        try:
            tel.poll_mongo(
                container=args.mongo_container,
                uri=args.mongo_uri,
            )
            _log("mongo", good_tag(), "polled → mongo_events.csv")
        except Exception as exc:
            _log("mongo", bad_tag(), f"poll error: {exc}")

        # -- Unstructured ------------------------------------------------
        try:
            tel.poll_unstructured(dir_path=args.unstructured_dir)
            _log("unstr", good_tag(), "polled → unstructured_events.csv")
        except Exception as exc:
            _log("unstr", bad_tag(), f"poll error: {exc}")

        if args.once:
            return 0

        time.sleep(max(1, args.interval_sec))


if __name__ == "__main__":
    raise SystemExit(main())
