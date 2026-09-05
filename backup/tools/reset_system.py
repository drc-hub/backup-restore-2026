#!/usr/bin/env python3

import argparse
import os
import shutil
import subprocess
import sys
import pwd


POSTGRES_UID = 70
POSTGRES_GID = 70

DEFAULT_PATHS = {
    "pg_data": "/home/primary/data/transactional/postgres/data",
    "pg_wal_archive": "/home/primary/data/transactional/postgres/wal_archive",
    "mongo_data": "/home/primary/data/transactional/mongo/data",
    "unstructured": "/home/primary/data/unstructured",
    "cycles_root": "/home/primary/data/backup-cycles",
    "staging_root": "/home/primary/data/backup-staging",
    "metadata_root": "/home/primary/data/backup-metadata",
    "state_db": "/home/primary/data/backup-metadata/pfc_index.db",
    "monitor": "/home/primary/data/monitor",
    # VM2 (Recovery VM) directories must NOT be referenced here.
    # This script runs on the Primary VM.
    # If you want VM2 to send restored cycles back to VM1, VM1 can receive them here.
    "backup_incoming": "/home/primary/data/backup-incoming",
}

DEFAULT_CONTAINERS = [
    "postgres_live",
    "postgres_restore",
    "postgres-data-generator-1",
    "mongodb_live",
    "mongodb_restore",
    "mongodb-data-generator-mongo-1",
    "unstructured_generator",
]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _rm_rf(path: str) -> None:
    if not os.path.exists(path):
        return
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
        return
    shutil.rmtree(path)


def _ensure_dir(path: str, *, mode: int | None = None, chown: tuple[int, int] | None = None) -> None:
    os.makedirs(path, exist_ok=True)
    if mode is not None:
        os.chmod(path, mode)
    if chown is not None:
        os.chown(path, chown[0], chown[1])


def _remove_state_db(state_db: str) -> None:
    for suffix in ["", "-wal", "-shm"]:
        p = state_db + suffix
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reset the whole system to a clean, newly-created state (DESTRUCTIVE)."
    )
    ap.add_argument("--yes", action="store_true", help="Actually perform deletion.")
    ap.add_argument(
        "--stop-containers",
        action="store_true",
        default=True,
        help="Stop/remove known containers (default: on).",
    )
    ap.add_argument(
        "--containers",
        nargs="*",
        default=DEFAULT_CONTAINERS,
        help="Container names to stop/remove.",
    )
    ap.add_argument(
        "--keep-monitor",
        action="store_true",
        help="Do not delete /home/primary/data/monitor files.",
    )
    ap.add_argument(
        "--prepare-incoming",
        action="store_true",
        default=False,
        help="Ensure a local incoming directory exists (optional; used to receive restored cycles back onto VM1).",
    )
    ap.add_argument(
        "--recovery-incoming",
        default=DEFAULT_PATHS["backup_incoming"],
        help="Local incoming directory path on VM1 (used for restored cycles).",
    )

    args = ap.parse_args()

    if not args.yes:
        print("Refusing to reset without --yes")
        print("This will DELETE Postgres/Mongo/unstructured data and all backup artifacts.")
        print("Run as root:")
        print("  sudo python3 utilities/backup/tools/reset_system.py --yes")
        return 2

    if os.geteuid() != 0:
        print("<error> This reset requires root (bind-mount dirs are root/uid-owned by Docker).")
        print("Run:")
        print("  sudo python3 utilities/backup/tools/reset_system.py --yes")
        return 2

    owner_user = os.environ.get("SUDO_USER") or "primary"
    try:
        pw = pwd.getpwnam(owner_user)
        owner_uid = int(pw.pw_uid)
        owner_gid = int(pw.pw_gid)
    except Exception:
        owner_uid = 0
        owner_gid = 0

    # 1) Stop/remove containers to release file locks
    if args.stop_containers:
        docker = shutil.which("docker")
        if docker:
            for name in args.containers:
                _run([docker, "rm", "-f", name])
        else:
            print("<warn> docker not found; skipping container stop")

    # 2) Delete DB and data directories
    _rm_rf(DEFAULT_PATHS["pg_data"])
    _rm_rf(DEFAULT_PATHS["pg_wal_archive"])
    _rm_rf(DEFAULT_PATHS["mongo_data"])

    # Unstructured: wipe contents but keep directory
    if os.path.isdir(DEFAULT_PATHS["unstructured"]):
        for entry in os.listdir(DEFAULT_PATHS["unstructured"]):
            _rm_rf(os.path.join(DEFAULT_PATHS["unstructured"], entry))
    else:
        _ensure_dir(DEFAULT_PATHS["unstructured"], mode=0o775)

    # Backup artifacts/state
    _rm_rf(DEFAULT_PATHS["cycles_root"])
    _rm_rf(DEFAULT_PATHS["staging_root"])
    _rm_rf(DEFAULT_PATHS["metadata_root"])

    # Backup incoming (restored cycles received from VM2): wipe contents, keep dir.
    if os.path.isdir(DEFAULT_PATHS["backup_incoming"]):
        for entry in os.listdir(DEFAULT_PATHS["backup_incoming"]):
            _rm_rf(os.path.join(DEFAULT_PATHS["backup_incoming"], entry))
    else:
        _ensure_dir(DEFAULT_PATHS["backup_incoming"], mode=0o775)

    # Monitoring logs
    if not args.keep_monitor:
        if os.path.isdir(DEFAULT_PATHS["monitor"]):
            for entry in os.listdir(DEFAULT_PATHS["monitor"]):
                _rm_rf(os.path.join(DEFAULT_PATHS["monitor"], entry))

    # 3) Recreate required directory structure with safe perms
    # Postgres: must be owned by uid/gid 70:70 in postgres:15-alpine
    _ensure_dir(DEFAULT_PATHS["pg_data"], mode=0o700, chown=(POSTGRES_UID, POSTGRES_GID))
    _ensure_dir(DEFAULT_PATHS["pg_wal_archive"], mode=0o755, chown=(POSTGRES_UID, POSTGRES_GID))

    # Mongo: keep permissive so container UID mismatch doesn't block init
    _ensure_dir(DEFAULT_PATHS["mongo_data"], mode=0o777)

    _ensure_dir(DEFAULT_PATHS["unstructured"], mode=0o775, chown=(owner_uid, owner_gid))
    _ensure_dir(DEFAULT_PATHS["metadata_root"], mode=0o775, chown=(owner_uid, owner_gid))

    # Backup incoming: recreate empty, ready to receive cycles from VM2.
    _ensure_dir(DEFAULT_PATHS["backup_incoming"], mode=0o775, chown=(owner_uid, owner_gid))

    # Ensure the backup process can write logs/metrics if used.
    _ensure_dir(DEFAULT_PATHS["monitor"], mode=0o775, chown=(owner_uid, owner_gid))

    if args.prepare_incoming:
        _ensure_dir(args.recovery_incoming, mode=0o775, chown=(owner_uid, owner_gid))

    print("<good> Reset complete")
    print(f"- Postgres: wiped + recreated {DEFAULT_PATHS['pg_data']} and {DEFAULT_PATHS['pg_wal_archive']}")
    print(f"- Mongo: wiped + recreated {DEFAULT_PATHS['mongo_data']}")
    print(f"- Unstructured: wiped contents under {DEFAULT_PATHS['unstructured']}")
    print(f"- Backup artifacts: wiped {DEFAULT_PATHS['cycles_root']} {DEFAULT_PATHS['staging_root']} {DEFAULT_PATHS['metadata_root']}")
    print(f"- Backup incoming: wiped contents, dir ready at {DEFAULT_PATHS['backup_incoming']}")
    if args.prepare_incoming:
        print(f"- Backup incoming ready (restore receive): {args.recovery_incoming}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
