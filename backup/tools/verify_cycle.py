#!/usr/bin/env python3

import argparse
import os
import sys

TOOLS_DIR = os.path.dirname(__file__)
BACKUP_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
if BACKUP_ROOT not in sys.path:
    sys.path.insert(0, BACKUP_ROOT)

from lib.verify import verify_cycle
from lib.config import BackupConfig


def _latest_cycle_dir(cycles_root: str) -> str:
    if not os.path.isdir(cycles_root):
        raise FileNotFoundError(f"cycles_root is not a directory: {cycles_root}")
    cycle_ids: list[str] = []
    for name in os.listdir(cycles_root):
        p = os.path.join(cycles_root, name)
        if os.path.isdir(p):
            cycle_ids.append(name)
    cycle_ids.sort()
    if not cycle_ids:
        raise FileNotFoundError(f"No cycles found under: {cycles_root}")
    return os.path.abspath(os.path.join(cycles_root, cycle_ids[-1]))


def _resolve_cycle_dir(arg: str, *, cycles_root: str) -> str:
    # 1) Accept a direct path (absolute or relative).
    direct = os.path.abspath(arg)
    if os.path.isdir(direct):
        return direct

    # 2) Convenience keyword.
    if arg == "latest":
        return _latest_cycle_dir(cycles_root)

    # 3) Accept a cycle id (folder name) under cycles_root.
    cand = os.path.join(cycles_root, arg)
    if os.path.isdir(cand):
        return os.path.abspath(cand)

    return direct


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a backup cycle directory via manifest.json and checksums.sha256")
    ap.add_argument(
        "cycle",
        help=(
            "Cycle path, cycle id, or 'latest'. "
            "Examples: 20260526_025335 | latest | /home/primary/data/backup-cycles/20260526_025335"
        ),
    )
    ap.add_argument(
        "--cycles-root",
        default=BackupConfig().cycles_root,
        help="Root directory containing cycle folders (default from BackupConfig)",
    )
    ap.add_argument(
        "--verify-raw",
        action="store_true",
        help="Also zlib-decompress mongo/pfc artifacts and verify raw_sha256/raw_bytes from manifest extras",
    )
    args = ap.parse_args()

    try:
        cycle_dir = _resolve_cycle_dir(args.cycle, cycles_root=os.path.abspath(args.cycles_root))
    except Exception as e:
        print(f"<error> {e}")
        return 2
    if not os.path.isdir(cycle_dir):
        print(f"<error> Not a directory: {cycle_dir}")
        return 2

    ok, errors = verify_cycle(cycle_dir, verify_raw=args.verify_raw)
    if ok:
        print(f"verify     <good>   OK: {cycle_dir}")
        return 0
    for err in errors:
        print(f"verify     <error>  FAIL: {err}")
    print(f"verify     <error>  FAIL: {cycle_dir}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
