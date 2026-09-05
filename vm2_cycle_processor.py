#!/usr/bin/env python3
"""VM2 cycle processor – chain-aware.

Directory layout expected from VM1:

    incoming/<chain-v>/<cycle_id>/
        manifest.json
        checksums.sha256
        <data files>

A cycle is processed only when:
  - manifest.json and checksums.sha256 are present
  - sha256sum -c checksums.sha256 succeeds inside that cycle directory

For each chain-v / cycle_id pair:
  - Encrypted artifact at:     <encrypted_root>/<chain-v>/<cycle_id>.tar.aes256gcm
  - Metadata at:               <encrypted_root>/<chain-v>/<cycle_id>.tar.aes256gcm.meta.json
  - Idempotency marker:        <encrypted_root>/<chain-v>/<cycle_id>.vm2_done
After successful processing, the incoming cycle directory is deleted (default) to avoid
disk bloat. Set VM2_DELETE_INCOMING_ON_SUCCESS=0 to disable.

Required environment
  VM2_AES_KEY_B64  – Base64 of 32 raw bytes (256-bit key). Never logged.

Cloud upload (optional / placeholder)
  VM2_UPLOAD_GENERAL_CMD    – shell command template; {src} and {cycle_id} are substituted.
  VM2_UPLOAD_IMMUTABLE_CMD  – shell command template (same placeholders).
  VM2_ENABLE_IMMUTABLE_UPLOAD – set to "1" to enable immutable uploads (default: "0").

Decrypt verification (Python snippet):
    import base64, os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key  = base64.b64decode(os.environ['VM2_AES_KEY_B64'])
    blob = open('CYCLE.tar.aes256gcm','rb').read()
    nonce, rest = blob[:12], blob[12:]
    ciphertext, tag = rest[:-16], rest[-16:]
    pt = AESGCM(key).decrypt(nonce, ciphertext + tag, None)
    open('cycle.tar','wb').write(pt)
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as _dt
import hashlib
import json
import os
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Allow `from lib...` when run as a script.
_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from lib.aesgcm_b64 import EncryptResult, encrypt_file_to_aes256gcm  # noqa: E402
from lib.fsutil import SingleInstanceLock, atomic_write_json, ensure_dir_copy_atomic, ensure_dirs  # noqa: E402
from lib.telemetry import WorkflowTelemetry, write_telemetry  # noqa: E402


NONCE_LEN = 12
TAG_LEN = 16
DEFAULT_INCOMING = "/home/recovery/local-backup/incoming"
DEFAULT_ENCRYPTED_ROOT = "/home/recovery/local-backup/encrypted"
DEFAULT_POLL_SECONDS = 15


@dataclass(frozen=True)
class Outputs:
    done_marker: Path
    encrypted: Path
    encrypted_meta: Path
    uploaded_general_marker: Path
    uploaded_immutable_marker: Path


def _iso_now() -> str:
    return _dt.datetime.now().astimezone().isoformat()


def _log(msg: str) -> None:
    ts = _dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"[{ts}] {msg}", flush=True)


def _decode_key_from_env() -> bytes:
    key_b64 = os.environ.get("VM2_AES_KEY_B64")
    if not key_b64:
        raise ValueError("VM2_AES_KEY_B64 is required")
    try:
        key = base64.b64decode(key_b64, validate=True)
    except binascii.Error as exc:
        raise ValueError("VM2_AES_KEY_B64 must be valid Base64") from exc
    if len(key) != 32:
        raise ValueError("VM2_AES_KEY_B64 must decode to exactly 32 bytes")
    return key


def _list_chain_dirs(incoming_root: Path) -> list[Path]:
    """Return sorted chain-v subdirectories under incoming_root."""
    if not incoming_root.exists():
        return []
    if not incoming_root.is_dir():
        raise NotADirectoryError(str(incoming_root))
    return [p for p in sorted(incoming_root.iterdir()) if p.is_dir() and not p.name.startswith(".")]


def _list_cycle_dirs(chain_dir: Path) -> list[Path]:
    """Return sorted cycle directories under a chain directory."""
    if not chain_dir.exists():
        return []
    return [p for p in sorted(chain_dir.iterdir()) if p.is_dir() and not p.name.startswith(".")]


def _verify_cycle_ready(cycle_dir: Path) -> bool:
    required = [cycle_dir / "manifest.json", cycle_dir / "checksums.sha256"]
    for p in required:
        if not p.is_file():
            return False

    try:
        proc = subprocess.run(
            ["sha256sum", "-c", "checksums.sha256"],
            cwd=str(cycle_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("sha256sum not found; required for checksums verification")

    if proc.returncode != 0:
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        tail = " | ".join(lines[-5:])
        _log(f"cycle {cycle_dir.name} not ready (checksums failed): {tail}")
        return False

    return True


def _compute_outputs(
    encrypted_root: Path,
    chain_v: str,
    cycle_id: str,
) -> Outputs:
    enc_chain_dir = encrypted_root / chain_v
    return Outputs(
        done_marker=enc_chain_dir / f"{cycle_id}.vm2_done",
        encrypted=enc_chain_dir / f"{cycle_id}.tar.aes256gcm",
        encrypted_meta=enc_chain_dir / f"{cycle_id}.tar.aes256gcm.meta.json",
        uploaded_general_marker=enc_chain_dir / f"{cycle_id}.vm2_uploaded_general",
        uploaded_immutable_marker=enc_chain_dir / f"{cycle_id}.vm2_uploaded_immutable",
    )


def _run_upload_cmd(template: str, *, src: Path, cycle_id: str, chain_v: str) -> None:
    """Render and execute an upload command template.

    Supported placeholders: {src}, {cycle_id}, {chain_v}
    """
    rendered = template.format(src=str(src), cycle_id=cycle_id, chain_v=chain_v)
    argv = shlex.split(rendered)
    if not argv:
        raise RuntimeError("upload command template rendered to empty command")
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout.strip() or f"upload failed with code {proc.returncode}")


def _maybe_upload(
    *,
    cmd_template: Optional[str],
    marker_path: Path,
    cycle_id: str,
    chain_v: str,
    files: list[Path],
    label: str,
) -> float:
    if not cmd_template:
        return 0.0
    if marker_path.exists():
        return 0.0

    upload_start = time.time()
    for f in files:
        _run_upload_cmd(cmd_template, src=f, cycle_id=cycle_id, chain_v=chain_v)

    atomic_write_json(
        marker_path,
        {"cycle_id": cycle_id, "chain_v": chain_v, "uploaded_at": _iso_now(), "target": label},
    )
    _log(f"upload [{label}] succeeded for {chain_v}/{cycle_id}")

    return round(time.time() - upload_start, 4)


def _verify_checksums_in_dir(dir_path: Path) -> None:
    proc = subprocess.run(
        ["sha256sum", "-c", "checksums.sha256"],
        cwd=str(dir_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"checksums verification failed in {dir_path}: {proc.stdout}")


def _create_tar_from_dir(src_dir: Path, arcname: str, tar_path: Path) -> None:
    """Create a tar with top-level entry named arcname (e.g. chain-v1/cycle_id)."""
    with tarfile.open(tar_path, mode="w") as tf:
        tf.add(src_dir, arcname=arcname)


def _write_meta_atomic(
    *,
    meta_path: Path,
    cycle_id: str,
    chain_v: str,
    enc: EncryptResult,
    cycle_timestamp: Optional[str],
    manifest_summary: Optional[dict] = None,
) -> None:
    meta = {
        "alg": "AES-256-GCM",
        "chain_v": chain_v,
        "cycle_id": cycle_id,
        "created_at": _iso_now(),
        "nonce_hex": enc.nonce.hex(),
        "tag_hex": enc.tag.hex(),
        "plaintext": "tar",
        "sha256_plaintext_tar": enc.sha256_plaintext_hex,
    }
    if isinstance(cycle_timestamp, str) and cycle_timestamp:
        meta["cycle_timestamp"] = cycle_timestamp
    if manifest_summary:
        meta["manifest_summary"] = manifest_summary
    tmp = meta_path.with_name(meta_path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, meta_path)


def _write_done_marker(
    done_path: Path,
    cycle_id: str,
    chain_v: str,
    cycle_timestamp: Optional[str],
    manifest_summary: Optional[dict] = None,
) -> None:
    obj = {"cycle_id": cycle_id, "chain_v": chain_v, "done_at": _iso_now()}
    if isinstance(cycle_timestamp, str) and cycle_timestamp:
        obj["cycle_timestamp"] = cycle_timestamp
    if manifest_summary:
        obj["manifest_summary"] = manifest_summary
    atomic_write_json(done_path, obj)


def _safe_delete_incoming_cycle(chain_dir: Path, cycle_dir: Path) -> None:
    """Delete a cycle dir that is a direct child of chain_dir (never follow symlinks)."""
    if cycle_dir.is_symlink():
        raise RuntimeError(f"refusing to delete symlink cycle dir: {cycle_dir}")
    chain_resolved = chain_dir.resolve()
    cycle_resolved = cycle_dir.resolve()
    if cycle_resolved.parent != chain_resolved:
        raise RuntimeError(f"refusing to delete non-child path: {cycle_dir}")
    shutil.rmtree(cycle_dir)


def process_one_cycle(
    *,
    key: bytes,
    incoming_chain_dir: Path,
    incoming_cycle: Path,
    chain_v: str,
    encrypted_root: Path,
    work_dir: Path,
    delete_incoming_on_success: bool,
    upload_general_cmd: Optional[str],
    upload_immutable_cmd: Optional[str],
) -> bool:
    cycle_id = incoming_cycle.name
    outputs = _compute_outputs(encrypted_root, chain_v, cycle_id)

    # Ensure encrypted chain dir exists.
    ensure_dirs(outputs.encrypted.parent)

    if outputs.done_marker.exists():
        # Already processed: retry any pending uploads.
        if outputs.encrypted.is_file() and outputs.encrypted_meta.is_file():
            try:
                _maybe_upload(
                    cmd_template=upload_general_cmd,
                    marker_path=outputs.uploaded_general_marker,
                    cycle_id=cycle_id,
                    chain_v=chain_v,
                    files=[outputs.encrypted, outputs.encrypted_meta],
                    label="general",
                )
                _maybe_upload(
                    cmd_template=upload_immutable_cmd,
                    marker_path=outputs.uploaded_immutable_marker,
                    cycle_id=cycle_id,
                    chain_v=chain_v,
                    files=[outputs.encrypted, outputs.encrypted_meta],
                    label="immutable",
                )
            except Exception as exc:
                _log(f"warning: upload failed for already-processed cycle {chain_v}/{cycle_id}: {exc}")

        if delete_incoming_on_success and incoming_cycle.exists():
            if _verify_cycle_ready(incoming_cycle):
                if outputs.encrypted.is_file() and outputs.encrypted_meta.is_file():
                    try:
                        _safe_delete_incoming_cycle(incoming_chain_dir, incoming_cycle)
                        _log(f"cleaned incoming cycle {chain_v}/{cycle_id}")
                    except Exception as exc:
                        _log(f"warning: failed to delete incoming cycle {chain_v}/{cycle_id}: {exc}")
        return False

    if not _verify_cycle_ready(incoming_cycle):
        return False

    _log(f"━━ processing cycle {chain_v}/{cycle_id} ━━")

    _log("  ↳ verifying checksums...")
    _verify_checksums_in_dir(incoming_cycle)

    cycle_timestamp: Optional[str] = None
    manifest_summary: Optional[dict] = None
    try:
        with open(incoming_cycle / "manifest.json", "r", encoding="utf-8") as f:
            mf = json.load(f)
        cycle_timestamp = mf.get("cycle_timestamp")
        # Build a compact summary of the new multi-component manifest format.
        summary: dict = {}
        if "chain_version" in mf:
            summary["chain_version"] = mf["chain_version"]
        if "pg_last_lsn" in mf:
            summary["pg_last_lsn"] = mf["pg_last_lsn"]
        if "mongo_last_ts" in mf:
            summary["mongo_last_ts"] = mf["mongo_last_ts"]
        # Record which top-level components are present.
        components = []
        for comp in ("pg_basebackup", "mongo_basebackup", "unstructured_basebackup", "mongo", "pfc", "files", "fixtures"):
            if comp in mf:
                components.append(comp)
        if components:
            summary["components"] = components
        # Total raw bytes from unstructured scanned_sizes if present.
        try:
            scanned = mf.get("pfc", {}).get("scanned_sizes") or mf.get("files", {}).get("scanned_sizes", {})
            if scanned:
                summary["unstructured_raw_bytes"] = sum(scanned.values())
        except Exception:
            pass
        if summary:
            manifest_summary = summary
    except Exception:
        cycle_timestamp = None
        manifest_summary = None

    # 2) Create tar from incoming copy directly.
    tar_tmp: Optional[Path] = None
    ensure_dirs(work_dir / chain_v)
    if outputs.encrypted.is_file() and outputs.encrypted_meta.is_file():
        # Reuse existing encrypted outputs (prior partial attempt).
        pass
    else:
        tar_tmp = work_dir / chain_v / f"{cycle_id}.tar.tmp"
        if tar_tmp.exists():
            tar_tmp.unlink()
        # Tar top-level path is chain-v/cycle_id so extraction preserves structure.
        _log("  ↳ creating tar archive...")
        _create_tar_from_dir(
            incoming_cycle,
            arcname=f"{chain_v}/{cycle_id}",
            tar_path=tar_tmp,
        )

        workflow_start = time.time()
        # 3) Encrypt tar → base64 artifact.
        _log("  ↳ encrypting with AES-256-GCM...")
        enc_start = time.time()
        enc = encrypt_file_to_aes256gcm(key=key, plaintext_path=tar_tmp, out_path=outputs.encrypted)
        enc_duration = round(time.time() - enc_start, 4)
        raw_size = tar_tmp.stat().st_size if tar_tmp.exists() else 0
        enc_size = outputs.encrypted.stat().st_size if outputs.encrypted.exists() else 0
        
        _write_meta_atomic(
            meta_path=outputs.encrypted_meta,
            cycle_id=cycle_id,
            chain_v=chain_v,
            enc=enc,
            cycle_timestamp=cycle_timestamp,
            manifest_summary=manifest_summary,
        )

    # 4) Optional cloud uploads (two targets).
    _log("  ↳ syncing artifacts to cloud...")
    up_time1 = _maybe_upload(
        cmd_template=upload_general_cmd,
        marker_path=outputs.uploaded_general_marker,
        cycle_id=cycle_id,
        chain_v=chain_v,
        files=[outputs.encrypted, outputs.encrypted_meta],
        label="general",
    )
    up_time2 = _maybe_upload(
        cmd_template=upload_immutable_cmd,
        marker_path=outputs.uploaded_immutable_marker,
        cycle_id=cycle_id,
        chain_v=chain_v,
        files=[outputs.encrypted, outputs.encrypted_meta],
        label="immutable",
    )

    # 5) Write done marker last (idempotency).
    _log("  ↳ finalizing idempotency markers & cleaning up...")
    _write_done_marker(outputs.done_marker, cycle_id, chain_v, cycle_timestamp, manifest_summary)

    if tar_tmp is not None:
        try:
            tar_tmp.unlink()
        except FileNotFoundError:
            pass

    # 6) Delete incoming cycle to avoid disk bloat.
    if delete_incoming_on_success:
        try:
            _safe_delete_incoming_cycle(incoming_chain_dir, incoming_cycle)
        except Exception as exc:
            _log(f"warning: failed to delete incoming cycle {chain_v}/{cycle_id}: {exc}")

    total_duration = round(time.time() - workflow_start, 4) if 'workflow_start' in locals() else 0.0
    if 'enc_duration' in locals():
        telemetry = WorkflowTelemetry(
            timestamp=_dt.datetime.now().astimezone().isoformat(),
            workflow_type="backup",
            chain_v=chain_v,
            cycle_id=cycle_id,
            raw_size_bytes=raw_size,
            encrypted_size_bytes=enc_size,
            duration_aes256_sec=enc_duration,
            duration_cloud_transfer_sec=round((up_time1 or 0) + (up_time2 or 0), 4),
            total_workflow_sec=total_duration
        )
        try:
            write_telemetry(telemetry)
        except Exception as exc:
            _log(f"warning: failed to write telemetry: {exc}")

    _log(f"━━ done cycle {chain_v}/{cycle_id} ━━")
    return True


def run_once(
    *,
    key: bytes,
    incoming_root: Path,
    encrypted_root: Path,
    work_dir: Path,
    delete_incoming_on_success: bool,
    upload_general_cmd: Optional[str],
    upload_immutable_cmd: Optional[str],
    chain_filter: Optional[str] = None,
) -> int:
    """Scan all chain-v directories under incoming_root and process ready cycles."""
    chain_dirs = _list_chain_dirs(incoming_root)
    for chain_dir in chain_dirs:
        chain_v = chain_dir.name
        if chain_filter and chain_v != chain_filter:
            continue
        cycle_dirs = _list_cycle_dirs(chain_dir)
        for cycle_dir in cycle_dirs:
            try:
                process_one_cycle(
                    key=key,
                    incoming_chain_dir=chain_dir,
                    incoming_cycle=cycle_dir,
                    chain_v=chain_v,
                    encrypted_root=encrypted_root,
                    work_dir=work_dir,
                    delete_incoming_on_success=delete_incoming_on_success,
                    upload_general_cmd=upload_general_cmd,
                    upload_immutable_cmd=upload_immutable_cmd,
                )
            except Exception as exc:
                _log(f"error processing {chain_v}/{cycle_dir.name}: {exc}")
    return 0


def _env_flag(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VM2 incoming cycle processor (chain-aware)")
    parser.add_argument("--once", action="store_true", help="process ready cycles once and exit")
    parser.add_argument(
        "--incoming-dir",
        default=os.environ.get("VM2_INCOMING_DIR", DEFAULT_INCOMING),
        help=f"incoming root (default: env VM2_INCOMING_DIR or {DEFAULT_INCOMING})",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.environ.get("VM2_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))),
        help=f"poll interval seconds (default: env VM2_POLL_SECONDS or {DEFAULT_POLL_SECONDS})",
    )
    parser.add_argument(
        "--encrypted-root",
        default=os.environ.get("VM2_ENCRYPTED_ROOT", DEFAULT_ENCRYPTED_ROOT),
        help=f"encrypted output root (default: {DEFAULT_ENCRYPTED_ROOT})",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="working dir for temp tar files (default: <encrypted-root>/../staging)",
    )
    parser.add_argument(
        "--lock-file",
        default=None,
        help="optional lock file path to prevent multiple instances",
    )
    parser.add_argument(
        "--chain",
        default=None,
        dest="chain_filter",
        help="only process a specific chain-v (e.g. chain-v1); default: all chains",
    )
    parser.add_argument(
        "--delete-incoming-on-success",
        action="store_true",
        default=None,
        help="delete incoming cycle dirs after successful processing (defaults to env VM2_DELETE_INCOMING_ON_SUCCESS)",
    )
    parser.add_argument(
        "--keep-incoming",
        action="store_true",
        help="do not delete incoming cycles (overrides --delete-incoming-on-success)",
    )

    args = parser.parse_args(argv)

    try:
        key = _decode_key_from_env()
    except ValueError as exc:
        _log(str(exc))
        return 2

    incoming_root = Path(args.incoming_dir)
    encrypted_root = Path(args.encrypted_root)

    delete_incoming_on_success = _env_flag("VM2_DELETE_INCOMING_ON_SUCCESS", True)
    if args.delete_incoming_on_success is True:
        delete_incoming_on_success = True
    if args.keep_incoming:
        delete_incoming_on_success = False

    try:
        ensure_dirs(encrypted_root)
    except PermissionError as exc:
        _log(f"cannot create output directories (need write access): {exc}")
        return 3

    general_enabled = _env_flag("VM2_ENABLE_GENERAL_UPLOAD", True)
    upload_general_cmd = os.environ.get("VM2_UPLOAD_GENERAL_CMD") if general_enabled else None
    immutable_enabled = _env_flag("VM2_ENABLE_IMMUTABLE_UPLOAD", False)
    upload_immutable_cmd = os.environ.get("VM2_UPLOAD_IMMUTABLE_CMD") if immutable_enabled else None

    if args.work_dir:
        work_dir = Path(args.work_dir)
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            _log(f"cannot create work dir: {exc}")
            return 3
    else:
        work_dir = encrypted_root.parent / "staging"
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            work_dir = Path(tempfile.gettempdir())

    lock_cm = SingleInstanceLock(Path(args.lock_file)) if args.lock_file else None

    def _run() -> int:
        return run_once(
            key=key,
            incoming_root=incoming_root,
            encrypted_root=encrypted_root,
            work_dir=work_dir,
            delete_incoming_on_success=delete_incoming_on_success,
            upload_general_cmd=upload_general_cmd,
            upload_immutable_cmd=upload_immutable_cmd,
            chain_filter=args.chain_filter,
        )

    try:
        if lock_cm:
            with lock_cm:
                if args.once:
                    return _run()
                while True:
                    _run()
                    time.sleep(max(1, int(args.poll_seconds)))
        else:
            if args.once:
                return _run()
            while True:
                _run()
                time.sleep(max(1, int(args.poll_seconds)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
