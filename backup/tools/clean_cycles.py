#!/usr/bin/env python3

import argparse
import os
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class Targets:
    cycles_root: str
    staging_root: str
    state_db: str


def _safe_rmtree(path: str) -> int:
    if not os.path.exists(path):
        return 0
    # Count files roughly (best-effort) for reporting.
    files = 0
    for _root, _dirs, fnames in os.walk(path):
        files += len(fnames)
    shutil.rmtree(path)
    return files


def _safe_remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Delete backup cycle outputs and optional incremental state.")
    ap.add_argument("--cycles-root", default="/home/primary/data/backup-cycles")
    ap.add_argument("--staging-root", default="/home/primary/data/backup-staging")
    ap.add_argument("--state-db", default="/home/primary/data/backup-metadata/pfc_index.db")
    ap.add_argument(
        "--reset-state",
        action="store_true",
        help="Also remove the SQLite state DB (and -wal/-shm) so next run becomes a fresh baseline.",
    )
    ap.add_argument("--yes", action="store_true", help="Actually perform deletion.")
    args = ap.parse_args()

    t = Targets(cycles_root=args.cycles_root, staging_root=args.staging_root, state_db=args.state_db)

    if not args.yes:
        print("Refusing to delete without --yes")
        print(f"Would delete: {t.cycles_root}")
        print(f"Would delete: {t.staging_root}")
        if args.reset_state:
            print(f"Would delete: {t.state_db} (+ -wal/-shm)")
        return 2

    deleted = {}
    deleted["cycles_files"] = _safe_rmtree(t.cycles_root)
    deleted["staging_files"] = _safe_rmtree(t.staging_root)

    if args.reset_state:
        deleted["state_db"] = _safe_remove(t.state_db)
        deleted["state_db_wal"] = _safe_remove(t.state_db + "-wal")
        deleted["state_db_shm"] = _safe_remove(t.state_db + "-shm")

    print("Cleanup complete:")
    print(f"- cycles_root: {t.cycles_root} (files deleted ~{deleted['cycles_files']})")
    print(f"- staging_root: {t.staging_root} (files deleted ~{deleted['staging_files']})")
    if args.reset_state:
        print(
            f"- state_db removed: {deleted['state_db']} (wal={deleted['state_db_wal']}, shm={deleted['state_db_shm']})"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
