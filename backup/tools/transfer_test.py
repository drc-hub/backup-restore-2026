#!/usr/bin/env python3
"""transfer_test.py — Test the rsync transfer pipeline to the recovery VM.

Sends a real cycle directory (or a synthetic probe) to every configured
destination and verifies it arrives correctly.

Usage:
    # Source env first, then run:
    source utilities/backup/backup.env
    python3 utilities/backup/tools/transfer_test.py

    # Or inline:
    python3 utilities/backup/tools/transfer_test.py \
        --target recovery@192.168.10.129:/home/recovery/local-backup/incoming \
        --key ~/.ssh/recovery_rsync_ed25519

    # Test with a real existing cycle:
    python3 utilities/backup/tools/transfer_test.py \
        --cycle /home/primary/data/backup-cycles/chain-v1/20260605_195813 \
        --chain-version chain-v1
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time

TOOLS_DIR = os.path.dirname(__file__)
BACKUP_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
if BACKUP_ROOT not in sys.path:
    sys.path.insert(0, BACKUP_ROOT)

from lib.config import BackupConfig, RESTORE_INCOMING_DIR  # noqa: E402
from lib.transfer import rsync_to_target  # noqa: E402


def _c(s: str, code: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\x1b[{code}m{s}\x1b[0m"


def _good(s: str) -> str:
    return _c(f"<good> {s}", "32")


def _bad(s: str) -> str:
    return _c(f"<bad>  {s}", "31")


def _info(s: str) -> str:
    return _c(f"<info> {s}", "36")


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, timeout=timeout)
    return r.returncode, r.stdout.strip()


def test_ssh(user_host: str, port: int, key: str | None) -> bool:
    print(_info(f"SSH connectivity  →  {user_host}"))
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout=5",
           "-p", str(port)]
    if key:
        cmd += ["-i", key]
    cmd += [user_host, "echo SSH_OK && whoami"]
    rc, out = _run(cmd)
    if rc == 0 and "SSH_OK" in out:
        print(_good(f"SSH OK: {out.replace('SSH_OK', '').strip()}"))
        return True
    print(_bad(f"SSH FAILED (code={rc}): {out[-200:]}"))
    return False


def test_rsync(src_dir: str, target: str, dest_name: str,
               port: int, key: str | None) -> bool:
    print(_info(f"rsync  {src_dir!r}  →  {target}/{dest_name}"))
    try:
        dest = rsync_to_target(
            src_dir=src_dir,
            target=target,
            dest_name=dest_name,
            ssh_port=port,
            ssh_key=key,
        )
        print(_good(f"rsync OK → {dest}"))
        return True
    except Exception as e:
        print(_bad(f"rsync FAILED: {e}"))
        return False


def verify_remote(user_host: str, remote_path: str, port: int, key: str | None) -> bool:
    """Check the probe file landed on the remote side."""
    probe_file = os.path.join(remote_path, "transfer_probe.txt")
    cmd = ["ssh", "-o", "BatchMode=yes", "-p", str(port)]
    if key:
        cmd += ["-i", key]
    cmd += [user_host, f"cat {probe_file}"]
    rc, out = _run(cmd)
    if rc == 0 and "transfer_probe" in out:
        print(_good(f"Remote verify OK: {probe_file}"))
        return True
    print(_bad(f"Remote verify FAILED: file not found or empty at {probe_file}"))
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Test the backup transfer pipeline.")
    ap.add_argument(
        "--target",
        default=None,
        help=(
            "rsync target (e.g. recovery@192.168.10.129:/home/recovery/local-backup/incoming). "
            "Defaults to first entry in RECOVERY_RSYNC_TARGETS env var."
        ),
    )
    ap.add_argument("--key", default=None,
                    help="SSH key path. Defaults to RECOVERY_SSH_KEY env var.")
    ap.add_argument("--port", type=int, default=None,
                    help="SSH port. Defaults to RECOVERY_SSH_PORT env var (22).")
    ap.add_argument(
        "--cycle",
        default=None,
        help="Path to a real cycle dir to transfer. If omitted a synthetic probe is used.",
    )
    ap.add_argument(
        "--chain-version",
        default="chain-v1",
        help="Chain version prefix for the dest_name (default: chain-v1).",
    )
    args = ap.parse_args()

    cfg = BackupConfig()

    # Resolve target
    target = args.target
    if not target:
        targets = list(cfg.recovery_rsync_targets)
        if not targets:
            print(_bad("No target configured. Set RECOVERY_RSYNC_TARGETS or pass --target."))
            return 2
        target = targets[0]
        print(_info(f"Using target from config: {target}"))

    key = args.key or cfg.recovery_ssh_key or None
    port = args.port or cfg.recovery_ssh_port or 22

    # Parse user@host from target
    user_host = None
    remote_root = None
    if ":" in target and not target.startswith("/"):
        user_host, remote_root = target.split(":", 1)

    print()
    print("=" * 60)
    print("Transfer pipeline test")
    print(f"  target : {target}")
    print(f"  key    : {key}")
    print(f"  port   : {port}")
    print("=" * 60)
    print()

    passed = 0
    failed = 0

    # 1) SSH check (remote only)
    if user_host:
        ok = test_ssh(user_host, port, key)
        passed += ok
        failed += not ok
        if not ok:
            print(_bad("Aborting: SSH failed, rsync will also fail."))
            return 1
    print()

    # 2) Build test source dir
    if args.cycle and os.path.isdir(args.cycle):
        src_dir = args.cycle
        cycle_id = os.path.basename(src_dir.rstrip("/"))
        dest_name = f"{args.chain_version}/{cycle_id}"
        print(_info(f"Using real cycle: {src_dir}"))
    else:
        # Synthetic probe: a tiny temp dir with a marker file.
        probe_id = f"transfer_probe_{int(time.time())}"
        tmp = tempfile.mkdtemp(prefix="bk_transfer_test_")
        probe_path = os.path.join(tmp, "transfer_probe.txt")
        with open(probe_path, "w") as f:
            f.write(f"transfer_probe ts={int(time.time())} target={target}\n")
        src_dir = tmp
        dest_name = f"{args.chain_version}/{probe_id}"
        print(_info(f"Using synthetic probe: {src_dir}  dest_name={dest_name}"))

    print()

    # 3) rsync
    ok = test_rsync(src_dir, target, dest_name, port, key)
    passed += ok
    failed += not ok

    # 4) Remote verify (only for synthetic probe, remote targets)
    if user_host and remote_root and ok and not args.cycle:
        remote_probe = f"{remote_root.rstrip('/')}/{dest_name}"
        print()
        ok2 = verify_remote(user_host, remote_probe, port, key)
        passed += ok2
        failed += not ok2

    print()
    print("=" * 60)
    if failed == 0:
        print(_good(f"All {passed} check(s) passed."))
        print()
        print(_info("Transfer is working. Cycles will land at:"))
        if remote_root:
            print(f"  {target}/{args.chain_version}/<cycle_id>/")
        else:
            print(f"  {target}/{args.chain_version}/<cycle_id>/")
    else:
        print(_bad(f"{failed} check(s) failed, {passed} passed."))
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
