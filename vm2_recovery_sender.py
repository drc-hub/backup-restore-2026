#!/usr/bin/env python3
"""VM2 recovery sender – chain-aware restore.

VM1 requests recovery with:
  --version <chain-v>          which chain to restore from (e.g. chain-v1)
  --chain   <N>                how many cycles to restore (most-recent N in that chain)
  --source  local|cloud|immutable  where to read from
  --send-to-vm1                rsync results back to VM1

Contract
--------
Cycles inside a chain are ordered lexicographically by cycle_id (YYYYMMDD_HHMMSS).
"--chain 3" returns the first 3 cycles in that chain (oldest-first), which is the
correct incremental replay order.

Restore sources
---------------
  local      – read encrypted cycles from local encrypted store
  cloud      – download encrypted artifacts from the general GCS bucket via rclone
  immutable  – download encrypted artifacts from the immutable GCS bucket via rclone

Output
------
  Cycles are staged to: <outgoing_root>/<chain-v>/<cycle_id>/...
  Then optionally rsync-pushed to VM1.

"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional

# Allow `from lib...` when run as a script.
_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from lib.decrypt_aesgcm_b64 import decrypt_aes256gcm_to_file  # noqa: E402
from lib.telemetry import WorkflowTelemetry, write_telemetry  # noqa: E402
from lib.fsutil import ensure_dir_copy_atomic, ensure_dirs  # noqa: E402


DEFAULT_ENCRYPTED_ROOT = "/home/recovery/local-backup/encrypted"
DEFAULT_OUTGOING_ROOT = "/home/recovery/local-backup/outgoing/primary"
DEFAULT_VM1_DEST_ROOT = "/home/primary/data/backup-incoming"

ENV_RCLONE_GENERAL_REMOTE = "VM2_RCLONE_GENERAL_REMOTE"
ENV_RCLONE_IMMUTABLE_REMOTE = "VM2_RCLONE_IMMUTABLE_REMOTE"


def _log(msg: str) -> None:
    ts = _dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"[{ts}] {msg}", flush=True)


def _decode_key_from_env() -> bytes:
    key_b64 = os.environ.get("VM2_AES_KEY_B64")
    if not key_b64:
        raise ValueError("VM2_AES_KEY_B64 is required for encrypted-source recovery")
    try:
        key = base64.b64decode(key_b64, validate=True)
    except binascii.Error as exc:
        raise ValueError("VM2_AES_KEY_B64 must be valid Base64") from exc
    if len(key) != 32:
        raise ValueError("VM2_AES_KEY_B64 must decode to exactly 32 bytes")
    return key


def _verify_cycle_ready(cycle_dir: Path) -> None:
    required = [cycle_dir / "manifest.json", cycle_dir / "checksums.sha256"]
    for p in required:
        if not p.is_file():
            raise RuntimeError(f"cycle missing required file: {p}")

    proc = subprocess.run(
        ["sha256sum", "-c", "checksums.sha256"],
        cwd=str(cycle_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"checksums failed for {cycle_dir.name}: {proc.stdout}")


def _list_encrypted_cycles(encrypted_root: Path, chain_v: str) -> list[str]:
    """Return sorted cycle_ids available from encrypted/<chain-v>/*.meta.json."""
    chain_dir = encrypted_root / chain_v
    if not chain_dir.is_dir():
        return []
    ids: list[str] = []
    for meta in chain_dir.glob("*.tar.aes256gcm.meta.json"):
        try:
            obj = json.loads(meta.read_text(encoding="utf-8"))
            cid = obj.get("cycle_id")
            if isinstance(cid, str) and cid:
                ids.append(cid)
        except Exception:
            continue
    return sorted(ids)


def _require_rclone() -> None:
    if shutil.which("rclone") is None:
        raise RuntimeError("rclone not found; required for cloud restore sources")


def _rclone_join(remote_base: str, leaf: str) -> str:
    return remote_base.rstrip("/") + "/" + leaf


def _rclone_copyto(remote_file: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["rclone", "copyto", remote_file, str(local_path)]
    proc = subprocess.run(cmd, cwd="/", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone copyto failed: {proc.stdout}")


def _rclone_list_meta(remote_base: str, chain_v: str) -> list[str]:
    """List *.tar.aes256gcm.meta.json files in remote_base/<chain-v>/ and parse cycle_ids."""
    _require_rclone()
    remote_chain = _rclone_join(remote_base, chain_v)
    cmd = ["rclone", "lsjson", remote_chain, "--include", "*.tar.aes256gcm.meta.json"]
    proc = subprocess.run(cmd, cwd="/", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone lsjson failed: {proc.stdout}")
    try:
        entries = json.loads(proc.stdout)
    except Exception:
        return []

    ids: list[str] = []
    for entry in entries:
        name = entry.get("Name", "")
        if name.endswith(".tar.aes256gcm.meta.json"):
            # Download and parse to get cycle_id.
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
                tf_path = Path(tf.name)
            try:
                remote_meta = _rclone_join(remote_chain, name)
                _rclone_copyto(remote_meta, tf_path)
                obj = json.loads(tf_path.read_text(encoding="utf-8"))
                cid = obj.get("cycle_id")
                if isinstance(cid, str) and cid:
                    ids.append(cid)
            except Exception:
                continue
            finally:
                tf_path.unlink(missing_ok=True)
    return sorted(ids)


def _rsync_send_dir(src_dir: Path, dest_root: str, chain_v: str, cycle_id: str) -> None:
    """rsync src_dir → dest_root/<chain-v>/<cycle_id>/"""
    dest = dest_root.rstrip("/") + "/" + chain_v + "/" + cycle_id + "/"
    cmd = ["rsync", "-a", str(src_dir) + "/"]
    if ":" in dest_root:
        remote_path = dest.split(":", 1)[1]
        cmd.extend(["--rsync-path", f"mkdir -p {remote_path} && rsync"])
    else:
        Path(dest).mkdir(parents=True, exist_ok=True)
    cmd.append(dest)
    proc = subprocess.run(cmd, cwd="/", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rsync failed for {chain_v}/{cycle_id}: {proc.stdout}")


def _extract_tar(tar_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, mode="r") as tf:
        members = tf.getmembers()
        for m in members:
            name = m.name
            if name.startswith("/") or name.startswith("\\"):
                raise RuntimeError(f"unsafe tar member (absolute path): {name}")
            parts = [p for p in name.split("/") if p and p != "."]
            if any(p == ".." for p in parts):
                raise RuntimeError(f"unsafe tar member (path traversal): {name}")
        tf.extractall(path=out_dir, members=members)


def _restore_from_cloud(
    *,
    key: bytes,
    rclone_remote: str,
    chain_v: str,
    cycle_id: str,
    outgoing_root: Path,
) -> tuple[Path, float, float, int, int]:
    """Download + decrypt encrypted artifact from cloud → outgoing/<chain-v>/<cycle_id>/"""
    _require_rclone()
    chain_outgoing = outgoing_root / chain_v
    chain_outgoing.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"vm2-cloud-{cycle_id}-") as td:
        td_path = Path(td)
        enc_leaf = f"{cycle_id}.tar.aes256gcm"
        remote_enc = _rclone_join(_rclone_join(rclone_remote, chain_v), enc_leaf)
        local_enc = td_path / enc_leaf
        cloud_start = time.time()
        _rclone_copyto(remote_enc, local_enc)
        cloud_time = round(time.time() - cloud_start, 4)

        tar_path = td_path / f"{cycle_id}.tar"
        dec_start = time.time()
        decrypt_aes256gcm_to_file(key=key, in_path=local_enc, out_path=tar_path)
        dec_time = round(time.time() - dec_start, 4)
        enc_size = local_enc.stat().st_size if local_enc.exists() else 0
        raw_size = tar_path.stat().st_size if tar_path.exists() else 0
        _extract_tar(tar_path, outgoing_root)

    restored = chain_outgoing / cycle_id
    if not restored.is_dir():
        raise RuntimeError(f"expected restored cycle dir not found: {restored}")
    _verify_cycle_ready(restored)
    return restored, dec_time, cloud_time, enc_size, raw_size


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="VM2 recovery sender (chain-aware)")
    p.add_argument(
        "--version",
        required=True,
        dest="chain_v",
        metavar="CHAIN_V",
        help="chain version to restore from (e.g. chain-v1)",
    )
    p.add_argument(
        "--chain",
        required=True,
        type=int,
        dest="num_cycles",
        metavar="N",
        help="number of cycles to restore (first N in lexicographic order, i.e. oldest N)",
    )
    p.add_argument(
        "--source",
        choices=["local", "general", "immutable"],
        default="local",
        help="where to read cycles from: local (encrypted store), general (general bucket), immutable (immutable bucket). Default: local",
    )
    p.add_argument("--encrypted-root", default=os.environ.get("VM2_ENCRYPTED_ROOT", DEFAULT_ENCRYPTED_ROOT))
    p.add_argument("--outgoing-root", default=os.environ.get("VM2_OUTGOING_ROOT", DEFAULT_OUTGOING_ROOT))
    p.add_argument(
        "--rclone-remote",
        default=None,
        help="override rclone remote:path base (otherwise uses env VM2_RCLONE_GENERAL_REMOTE / VM2_RCLONE_IMMUTABLE_REMOTE)",
    )
    p.add_argument(
        "--send-to-vm1",
        action="store_true",
        help="rsync-push cycles to VM1 destination root after staging",
    )
    p.add_argument(
        "--vm1-dest-root",
        default=os.environ.get("VM1_RESTORE_DEST_ROOT", DEFAULT_VM1_DEST_ROOT),
        help=f"VM1 receive root (default: env VM1_RESTORE_DEST_ROOT or {DEFAULT_VM1_DEST_ROOT})",
    )
    p.add_argument("--dry-run", action="store_true", help="print selected cycles and exit without restoring")

    args = p.parse_args(argv)

    chain_v: str = args.chain_v
    num_cycles: int = args.num_cycles
    source: str = args.source

    if num_cycles < 1:
        _log("--chain must be >= 1")
        return 2

    encrypted_root = Path(args.encrypted_root)
    outgoing_root = Path(args.outgoing_root)
    ensure_dirs(outgoing_root / chain_v)

    # ---------- resolve available cycle_ids ----------
    if source == "local":
        all_ids = _list_encrypted_cycles(encrypted_root, chain_v)
        if not all_ids:
            _log(f"no cycles found under encrypted root {encrypted_root / chain_v}")
            return 4
    elif source in ("general", "immutable"):
        env_name = ENV_RCLONE_GENERAL_REMOTE if source == "general" else ENV_RCLONE_IMMUTABLE_REMOTE
        remote = args.rclone_remote or os.environ.get(env_name)
        if not remote:
            _log(f"missing required env var {env_name} (rclone remote:path)")
            return 2
        _log(f"listing cycles from {source} bucket ({remote}/{chain_v}) ...")
        try:
            all_ids = _rclone_list_meta(remote, chain_v)
        except RuntimeError as exc:
            _log(f"failed to list cloud cycles: {exc}")
            return 4
        if not all_ids:
            _log(f"no cycle meta found in {source} bucket under {chain_v}/")
            return 4
    else:
        _log(f"unknown source: {source}")
        return 2

    # Select first N cycles (oldest-first = correct incremental replay order).
    selected_ids = all_ids[:num_cycles]

    if args.dry_run:
        _log(f"dry-run: chain={chain_v}, source={source}, available={len(all_ids)}, selected={len(selected_ids)}")
        for cid in selected_ids:
            print(cid)
        return 0

    # ---------- resolve decryption key if needed ----------
    if source in ("general", "immutable"):
        try:
            key = _decode_key_from_env()
        except ValueError as exc:
            _log(str(exc))
            return 2
        rclone_remote = args.rclone_remote or os.environ.get(
            ENV_RCLONE_GENERAL_REMOTE if source == "general" else ENV_RCLONE_IMMUTABLE_REMOTE
        )
    elif source == "local":
        try:
            key = _decode_key_from_env()
        except ValueError as exc:
            _log(str(exc))
            return 2
        rclone_remote = None
    else:
        key = b""
        rclone_remote = None

    _log(f"chain={chain_v}  source={source}  selected={len(selected_ids)} cycles")

    # ---------- restore each cycle ----------
    for cid in selected_ids:
        _log(f"  restoring {chain_v}/{cid} ...")
        loop_start = time.time()

        if source == "local":
            _log("  ↳ source: local encrypted storage")
            chain_outgoing = outgoing_root / chain_v
            chain_outgoing.mkdir(parents=True, exist_ok=True)
            local_enc = encrypted_root / chain_v / f"{cid}.tar.aes256gcm"
            with tempfile.TemporaryDirectory(prefix=f"vm2-local-{cid}-") as td:
                tar_path = Path(td) / f"{cid}.tar"
                _log("  ↳ decrypting AES-256-GCM artifact...")
                dec_start = time.time()
                decrypt_aes256gcm_to_file(key=key, in_path=local_enc, out_path=tar_path)
                dec_time = round(time.time() - dec_start, 4)
                enc_size = local_enc.stat().st_size if local_enc.exists() else 0
                raw_size = tar_path.stat().st_size if tar_path.exists() else 0
                cloud_time = 0.0
                _log("  ↳ extracting raw tar archive...")
                _extract_tar(tar_path, outgoing_root)
            
            restored = chain_outgoing / cid
            if not restored.is_dir():
                raise RuntimeError(f"expected restored cycle dir not found: {restored}")
            _verify_cycle_ready(restored)

        elif source in ("general", "immutable"):
            _log(f"  ↳ source: general/immutable bucket ({source})")
            _log("  ↳ downloading & decrypting AES-256-GCM + Base64 artifact...")
            _, dec_time, cloud_time, enc_size, raw_size = _restore_from_cloud(
                key=key,
                rclone_remote=rclone_remote,
                chain_v=chain_v,
                cycle_id=cid,
                outgoing_root=outgoing_root,
            )

        vm1_time = 0.0
        if args.send_to_vm1:
            _log(f"  ↳ sending decrypted cycle back to VM1 ({args.vm1_dest_root})...")
            staged = outgoing_root / chain_v / cid
            vm1_start = time.time()
            _rsync_send_dir(staged, args.vm1_dest_root, chain_v, cid)
            vm1_time = round(time.time() - vm1_start, 4)

        telemetry = WorkflowTelemetry(
            timestamp=_dt.datetime.now().astimezone().isoformat(),
            workflow_type="restore",
            chain_v=chain_v,
            cycle_id=cid,
            raw_size_bytes=raw_size,
            encrypted_size_bytes=enc_size,
            duration_aes256_sec=dec_time,
            duration_cloud_transfer_sec=cloud_time,
            duration_vm1_transfer_sec=vm1_time,
            total_workflow_sec=round(time.time() - loop_start, 4)
        )
        try:
            write_telemetry(telemetry)
        except Exception as exc:
            _log(f"warning: failed to write telemetry: {exc}")
            
        _log(f"━━ done restoring {chain_v}/{cid} ━━")

    _log(f"recovery export complete: {len(selected_ids)} cycle(s) from {chain_v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
