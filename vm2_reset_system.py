#!/usr/bin/env python3

import argparse
import os
import pwd
import shutil
import stat
from pathlib import Path


DEFAULT_INCOMING_DIR = "/home/recovery/local-backup/incoming"
DEFAULT_RESTORE_REQUEST_DIR = "/home/recovery/local-backup/restore-requests"
DEFAULT_OUTGOING_ROOT = "/home/recovery/local-backup/outgoing/primary"
DEFAULT_STATE_DIR = "/home/recovery/local-backup/state"

DEFAULT_ENCRYPTED_ROOT = "/home/recovery/local-backup/encrypted"


def _rm_rf(path: Path) -> None:
    # lstat so we don't follow symlinks
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    mode = st.st_mode
    if stat.S_ISDIR(mode):
        shutil.rmtree(path)
    else:
        # handles regular files, symlinks, FIFOs, sockets, devices, etc.
        path.unlink(missing_ok=True)


def _rm_contents(dir_path: Path) -> None:
    if not dir_path.exists():
        return
    if not dir_path.is_dir():
        _rm_rf(dir_path)
        return
    for child in dir_path.iterdir():
        _rm_rf(child)


def _ensure_dir(path: Path, *, mode: int | None = None, chown: tuple[int, int] | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if mode is not None:
        os.chmod(path, mode)
    if chown is not None:
        try:
            os.chown(path, chown[0], chown[1])
        except PermissionError:
            # Best-effort: some mounts disallow chown.
            pass


def _owner_ids() -> tuple[int, int]:
    owner_user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "recovery"
    try:
        pw = pwd.getpwnam(owner_user)
        return int(pw.pw_uid), int(pw.pw_gid)
    except Exception:
        return 0, 0


def _needs_root(paths: list[Path]) -> bool:
    # Require root only for paths outside the recovery user's home.
    return any(
        str(p).startswith("/local-backup") and not str(p).startswith("/home/recovery")
        for p in paths
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reset VM2 local-backup state to an initial empty state (DESTRUCTIVE, local only)."
    )
    ap.add_argument("--yes", action="store_true", help="Actually perform deletion.")

    ap.add_argument("--incoming-dir", default=DEFAULT_INCOMING_DIR)
    ap.add_argument("--restore-request-dir", default=DEFAULT_RESTORE_REQUEST_DIR)
    ap.add_argument("--outgoing-root", default=DEFAULT_OUTGOING_ROOT)
    ap.add_argument("--state-dir", default=DEFAULT_STATE_DIR)

    ap.add_argument("--encrypted-root", default=DEFAULT_ENCRYPTED_ROOT)
    ap.add_argument(
        "--work-dir",
        default=None,
        help="Optional staging/work dir; default: <encrypted-root>/../staging",
    )

    ap.add_argument(
        "--keep-incoming",
        action="store_true",
        help="Do not wipe incoming/ (default: wipe).",
    )
    ap.add_argument(
        "--keep-requests",
        action="store_true",
        help="Do not wipe restore-requests/ (default: wipe).",
    )
    ap.add_argument(
        "--keep-outgoing",
        action="store_true",
        help="Do not wipe outgoing/primary (default: wipe).",
    )

    args = ap.parse_args()

    incoming_dir = Path(args.incoming_dir)
    restore_request_dir = Path(args.restore_request_dir)
    outgoing_root = Path(args.outgoing_root)
    state_dir = Path(args.state_dir)

    encrypted_root = Path(args.encrypted_root)
    work_dir = Path(args.work_dir) if args.work_dir else encrypted_root.parent / "staging"

    targets = [
        incoming_dir,
        restore_request_dir,
        outgoing_root,
        state_dir,
        encrypted_root,
        work_dir,
    ]

    if not args.yes:
        print("Refusing to reset without --yes")
        print("This will DELETE VM2 local state (cloud is NOT touched):")
        for t in targets:
            print(f"  - {t}")
        print("Recommended: stop vm2_service.sh first.")
        print("Run (may require sudo if /local-backup is root-owned):")
        print("  sudo python3 utilities/vm2_reset_system.py --yes")
        return 2

    if _needs_root(targets) and os.geteuid() != 0:
        print("[FAIL] This reset targets /local-backup; run as root.")
        print("Run:")
        print("  sudo python3 utilities/vm2_reset_system.py --yes")
        return 2

    owner_uid, owner_gid = _owner_ids()

    if not args.keep_incoming:
        _rm_contents(incoming_dir)
    if not args.keep_requests:
        _rm_contents(restore_request_dir)
    if not args.keep_outgoing:
        _rm_contents(outgoing_root)

    _rm_contents(state_dir)
    _rm_contents(encrypted_root)
    _rm_contents(work_dir)

    # Recreate required top-level dirs.
    for d in [
        incoming_dir,
        restore_request_dir,
        outgoing_root,
        state_dir,
        encrypted_root,
        work_dir,
    ]:
        _ensure_dir(d, mode=0o775, chown=(owner_uid, owner_gid))

    print("[OK] VM2 reset complete (local only).")
    print("- incoming cleared" if not args.keep_incoming else "- incoming kept")
    print("- restore-requests cleared" if not args.keep_requests else "- restore-requests kept")
    print("- outgoing cleared" if not args.keep_outgoing else "- outgoing kept")
    print(f"- encrypted cleared: {encrypted_root}")
    print(f"- work/staging cleared: {work_dir}")
    print(f"- state cleared: {state_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
