#!/usr/bin/env python3
"""
decrypt_and_compare.py
======================
Full pipeline test:

  1. Reset encrypted/ (keeps incoming/ as the reference)
  2. Re-encrypt all cycles with a freshly-generated key
     (incoming is DELETED after each cycle — the production default)
  3. Decrypt every encrypted artifact back to a temp directory
  4. Extract the tar and compare every file's SHA-256 against the
     original checksums.sha256 that came in with the cycle

Since step 2 deletes incoming, the comparison in step 4 is purely against
the checksums.sha256 that was stored (and verified) inside the cycle itself.

Run from /home/recovery:
    python3 utilities/decrypt_and_compare.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BASE      = Path("/home/recovery/local-backup")
UTILS     = Path("/home/recovery/utilities")
CHAIN_V   = "chain-v1"

INCOMING  = BASE / "incoming"
ENCRYPTED = BASE / "encrypted"
STAGING   = BASE / "staging"

# ── helpers ────────────────────────────────────────────────────────────────

def banner(title: str) -> None:
    print(f"\n{'─'*62}")
    print(f"  {title}")
    print('─'*62)

def ok(msg: str)  -> None: print(f"  ✓  {msg}")
def err(msg: str) -> None: print(f"  ✗  {msg}"); sys.exit(1)

def run(cmd: list[str], env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    e = {**os.environ, **(env or {})}
    r = subprocess.run(cmd, capture_output=True, text=True, env=e, cwd=str(cwd) if cwd else None)
    if r.returncode != 0:
        print(f"  CMD: {' '.join(cmd)}")
        print(f"  STDOUT: {r.stdout[-3000:]}")
        print(f"  STDERR: {r.stderr[-3000:]}")
        err(f"Command failed (rc={r.returncode})")
    return r

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

# ── step 0: pre-flight ─────────────────────────────────────────────────────

banner("0. Pre-flight checks")

incoming_chain = INCOMING / CHAIN_V
cycle_dirs = sorted(d for d in incoming_chain.iterdir() if d.is_dir()) if incoming_chain.is_dir() else []
if not cycle_dirs:
    err(f"No cycles found under {incoming_chain} — nothing to process")

# Snapshot checksums from incoming BEFORE we delete it.
# Map: cycle_id → { relative_path → expected_sha256 }
incoming_snapshots: dict[str, dict[str, str]] = {}
for cd in cycle_dirs:
    checksum_file = cd / "checksums.sha256"
    if not checksum_file.is_file():
        err(f"Missing checksums.sha256 in {cd}")
    lines = checksum_file.read_text(encoding="utf-8").splitlines()
    incoming_snapshots[cd.name] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            digest, rel = parts
            # sha256sum uses "  filename" or " *filename"
            rel = rel.lstrip("* ")
            incoming_snapshots[cd.name][rel] = digest

ok(f"Found {len(cycle_dirs)} cycle(s) in {incoming_chain}:")
for cd in cycle_dirs:
    n = len(incoming_snapshots[cd.name])
    ok(f"  {cd.name}  ({n} checksummed files)")

# ── step 1: reset encrypted/ ───────────────────────────────────────────────

banner("1. Reset encrypted/ (keeping incoming/ as reference)")

def wipe_and_recreate(d: Path) -> None:
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)

wipe_and_recreate(ENCRYPTED)
wipe_and_recreate(STAGING)
ok("Cleared encrypted/, staging/")

# ── step 2: generate key and encrypt ──────────────────────────────────────

banner("2. Encrypt all cycles (incoming WILL be deleted after each)")

key_bytes = os.urandom(32)
key_b64   = base64.b64encode(key_bytes).decode()
env_key   = {"VM2_AES_KEY_B64": key_b64, "VM2_DELETE_INCOMING_ON_SUCCESS": "1"}

print(f"  Key (b64): {key_b64[:12]}...  [truncated for display]")

r = run([
    sys.executable, str(UTILS / "vm2_cycle_processor.py"),
    "--once",
    "--incoming-dir",   str(INCOMING),
    "--encrypted-root", str(ENCRYPTED),
    "--work-dir",       str(STAGING),
    # NO --keep-incoming → incoming dirs will be deleted after success
], env=env_key)

if r.stdout.strip():
    print(r.stdout.strip())

# Confirm incoming is now empty.
remaining_in = [d for d in incoming_chain.iterdir() if d.is_dir()] if incoming_chain.is_dir() else []
if remaining_in:
    err(f"Incoming not cleaned! Still present: {[d.name for d in remaining_in]}")
ok("All incoming cycle directories deleted after encryption ✓")

# Confirm encrypted artifacts exist.
enc_chain = ENCRYPTED / CHAIN_V
enc_files = sorted(enc_chain.glob("*.tar.aes256gcm"))
enc_files = [f for f in enc_files if not f.name.endswith(".meta.json")]
enc_metas = sorted(enc_chain.glob("*.tar.aes256gcm.meta.json"))
if len(enc_files) != len(cycle_dirs):
    err(f"Expected {len(cycle_dirs)} encrypted artifacts, found {len(enc_files)}")
ok(f"Encrypted artifacts: {len(enc_files)} .tar.aes256gcm + {len(enc_metas)} .meta.json")

# Confirm encrypted markers.
for cd in cycle_dirs:
    marker = enc_chain / f"{cd.name}.vm2_done"
    if not marker.is_file():
        err(f"Missing .vm2_done in encrypted/{CHAIN_V}/{cd.name}.vm2_done")
ok(f"All .vm2_done markers present in encrypted/{CHAIN_V}/")

# ── step 3: decrypt each artifact and compare ─────────────────────────────

banner("3. Decrypt each artifact and compare file hashes")

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # type: ignore

NONCE_LEN = 12
TAG_LEN   = 16

def decrypt_aes256gcm(key: bytes, enc_path: Path, out_path: Path) -> None:
    """Streaming AES-256-GCM decrypt (raw binary: nonce || ciphertext || tag) → out_path."""
    raw        = enc_path.read_bytes()
    nonce      = raw[:NONCE_LEN]
    ciphertext = raw[NONCE_LEN:-TAG_LEN]
    tag        = raw[-TAG_LEN:]

    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(plaintext)

total_files_checked = 0
total_cycles_ok     = 0

with tempfile.TemporaryDirectory(prefix="vm2-decrypt-test-") as tmp:
    tmp_path = Path(tmp)

    for enc_path in enc_files:
        cycle_id = enc_path.name.removesuffix(".tar.aes256gcm")

        meta_path = enc_chain / f"{cycle_id}.tar.aes256gcm.meta.json"
        meta      = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["chain_v"]  == CHAIN_V,      f"chain_v mismatch in meta: {meta}"
        assert meta["alg"]      == "AES-256-GCM", f"alg mismatch in meta: {meta}"
        assert meta["cycle_id"] == cycle_id,      f"cycle_id mismatch in meta: {meta}"

        print(f"\n  ── {CHAIN_V}/{cycle_id}")
        print(f"     enc size : {enc_path.stat().st_size:,} bytes")
        print(f"     nonce    : {meta['nonce_hex']}")
        print(f"     tag      : {meta['tag_hex']}")

        # 3a. Decrypt → tar
        tar_path = tmp_path / f"{cycle_id}.tar"
        decrypt_aes256gcm(key_bytes, enc_path, tar_path)
        ok(f"Decrypted → {tar_path.name}  ({tar_path.stat().st_size:,} bytes)")

        # 3b. Verify sha256 of plaintext tar matches metadata
        actual_sha256 = sha256_file(tar_path)
        expected_sha256 = meta["sha256_plaintext_tar"]
        if actual_sha256 != expected_sha256:
            err(f"TAR sha256 MISMATCH!\n  expected: {expected_sha256}\n  got:      {actual_sha256}")
        ok(f"Plaintext tar sha256 matches meta  ✓")

        # 3c. Extract tar
        extract_dir = tmp_path / f"extracted_{cycle_id}"
        extract_dir.mkdir()
        with tarfile.open(tar_path, "r") as tf:
            # Safety check for path traversal.
            for m in tf.getmembers():
                parts = [p for p in m.name.replace("\\", "/").split("/") if p and p != "."]
                if any(p == ".." for p in parts) or m.name.startswith("/"):
                    err(f"Unsafe tar member: {m.name}")
            tf.extractall(path=extract_dir)
        ok(f"Extracted tar → {extract_dir.name}/")

        # 3d. The tar top-level is chain-v/cycle_id/
        cycle_root = extract_dir / CHAIN_V / cycle_id
        if not cycle_root.is_dir():
            err(f"Expected extracted dir not found: {cycle_root}")

        # 3e. Compare every file hash against the original checksums snapshot.
        snapshot = incoming_snapshots.get(cycle_id, {})
        if not snapshot:
            print(f"  ⚠  No incoming snapshot for {cycle_id} — skipping hash comparison")
            continue

        files_ok    = 0
        files_fail  = 0
        files_extra = 0

        for rel_path, expected_hex in snapshot.items():
            extracted_file = cycle_root / rel_path
            if not extracted_file.is_file():
                # Some checksums entries include the checksums.sha256 itself; skip.
                if rel_path == "checksums.sha256":
                    continue
                print(f"     ⚠  File in snapshot but not extracted: {rel_path}")
                files_fail += 1
                continue

            actual_hex = sha256_file(extracted_file)
            if actual_hex != expected_hex:
                print(f"     ✗  HASH MISMATCH: {rel_path}")
                print(f"        expected: {expected_hex}")
                print(f"        got:      {actual_hex}")
                files_fail += 1
            else:
                files_ok += 1

        # Also check checksums.sha256 itself (the file inside the extracted cycle).
        cs_file = cycle_root / "checksums.sha256"
        if cs_file.is_file():
            snap_hex = snapshot.get("checksums.sha256")
            if snap_hex:
                actual = sha256_file(cs_file)
                if actual == snap_hex:
                    files_ok += 1
                else:
                    files_fail += 1

        total_files_checked += files_ok + files_fail

        if files_fail > 0:
            err(f"{cycle_id}: {files_fail} file(s) FAILED hash check")

        ok(f"All {files_ok} files match original checksums  ✓")

        # 3f. Also run sha256sum -c inside the extracted cycle for belt-and-suspenders.
        cs_result = subprocess.run(
            ["sha256sum", "-c", "checksums.sha256"],
            cwd=str(cycle_root),
            capture_output=True, text=True,
        )
        if cs_result.returncode != 0:
            err(f"sha256sum -c FAILED for {cycle_id}:\n{cs_result.stdout}")
        ok(f"sha256sum -c passed inside extracted cycle  ✓")

        total_cycles_ok += 1

# ── summary ────────────────────────────────────────────────────────────────

banner("RESULT")
print(f"  Cycles processed  : {len(cycle_dirs)}")
print(f"  Cycles verified   : {total_cycles_ok}")
print(f"  Files hash-checked: {total_files_checked}")
print(f"  Incoming cleaned  : YES (deleted after encryption)")
print(f"  Decryption        : AES-256-GCM (raw binary)")
print(f"  Result            : ALL PASS ✓")
print()
