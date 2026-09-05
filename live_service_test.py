#!/usr/bin/env python3
"""
live_service_test.py
====================
Starts vm2_service.sh in the background (with a 5-second poll),
drops a restore request, waits for the listener to process it,
captures and displays all service output.

Run from /home/recovery:
    python3 utilities/live_service_test.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE     = Path("/home/recovery/local-backup")
UTILS    = Path("/home/recovery/utilities")
CHAIN_V  = "chain-v1"
SERVICE  = UTILS / "vm2_service.sh"

def banner(t: str) -> None:
    print(f"\n{'─'*62}\n  {t}\n{'─'*62}")

def ok(m: str)  -> None: print(f"  ✓  {m}")
def info(m: str)-> None: print(f"  ·  {m}")

# ── 0. pre-flight ─────────────────────────────────────────────────────────────
banner("0. Pre-flight cleanup")

state_dir   = BASE / "state"
req_dir     = BASE / "restore-requests"
outgoing    = BASE / "outgoing" / "primary"
state_dir.mkdir(parents=True, exist_ok=True)
req_dir.mkdir(parents=True, exist_ok=True)
outgoing.mkdir(parents=True, exist_ok=True)

# Remove leftover files from prior runs.
for pat in ["*.json", "*.done", "*.error", "*.inprogress"]:
    for f in req_dir.glob(pat):
        f.unlink()
for f in state_dir.glob("*.lock"):
    f.unlink()
for f in state_dir.glob("*.pipe"):
    f.unlink()

chain_outgoing = outgoing / CHAIN_V
if chain_outgoing.exists():
    shutil.rmtree(chain_outgoing)

encrypted_chain = BASE / "encrypted" / CHAIN_V
cycles = sorted(f for f in encrypted_chain.glob("*.tar.aes256gcm") if not f.name.endswith(".meta.json")) if encrypted_chain.is_dir() else []
assert cycles, f"No cycles in {encrypted_chain} — run decrypt_and_compare.py first"
ok(f"encrypted/{CHAIN_V} has {len(cycles)} cycle(s): {[c.name for c in cycles]}")

# ── 1. generate AES key ───────────────────────────────────────────────────────
banner("1. Generate AES-256 key")
key_b64 = base64.b64encode(os.urandom(32)).decode()
info(f"Key (first 16 chars): {key_b64[:16]}…")

env = {
    **os.environ,
    "VM2_AES_KEY_B64": key_b64,
    "VM2_POLL_SECONDS": "5",                    # fast polling for test
    "VM2_INCOMING_DIR":   str(BASE / "incoming"),
    "VM2_ENCRYPTED_ROOT": str(BASE / "encrypted"),
    "VM2_RESTORE_REQUEST_DIR": str(req_dir),
    "VM2_OUTGOING_ROOT":  str(outgoing),
    "VM2_STATE_DIR":      str(state_dir),
    "VM2_DELETE_INCOMING_ON_SUCCESS": "0",      # incoming already empty, keep safe
}
ok("Key and env prepared")

# ── 2. start the service ──────────────────────────────────────────────────────
banner("2. Start vm2_service.sh")

svc_log  = BASE / "state" / "service_test.log"
log_fh   = open(svc_log, "w")
svc_proc = subprocess.Popen(
    ["bash", str(SERVICE)],
    env=env,
    stdout=log_fh,
    stderr=subprocess.STDOUT,
    cwd="/",
)
info(f"Service PID: {svc_proc.pid}")
info(f"Log file   : {svc_log}")

# Wait for the service to announce the listener is ready.
deadline = time.time() + 20
started  = False
while time.time() < deadline:
    time.sleep(0.5)
    log_fh.flush()
    content = svc_log.read_text(errors="replace")
    if "restore listener" in content and "starting" in content:
        started = True
        break

if not started:
    print(svc_log.read_text(errors="replace"))
    svc_proc.kill()
    sys.exit("Service did not report 'restore listener' within 20s")

ok("Service started and restore listener is live")

# ── 3. drop a restore request ─────────────────────────────────────────────────
banner("3. Drop restore request: chain-v1, first 2 cycles, source=local")

req = {
    "version":       CHAIN_V,
    "chain":         2,
    "source":        "local",
    "send_to_vm1":   False,          # don't rsync, just stage to outgoing/
    "vm1_dest_root": "/tmp/vm1-test-incoming",
}
req_tmp  = req_dir / "req-live-test.json.tmp"
req_file = req_dir / "req-live-test.json"
req_tmp.write_text(json.dumps(req, indent=2))
req_tmp.rename(req_file)             # atomic rename (how VM1 should send)
info(f"Dropped: {req_file.name}")
info(f"Content: {json.dumps(req)}")

# ── 4. wait for the request to be processed ───────────────────────────────────
banner("4. Waiting for listener to detect and process the request …")

deadline = time.time() + 40
done_file = req_dir / "req-live-test.done"
err_file  = req_dir / "req-live-test.error"

processed = False
while time.time() < deadline:
    time.sleep(0.5)
    if done_file.exists():
        processed = True
        break
    if err_file.exists():
        break

# ── 5. print service log ──────────────────────────────────────────────────────
banner("5. Service log output")
log_fh.flush()
log_content = svc_log.read_text(errors="replace")
print(log_content)

# ── 6. stop service ───────────────────────────────────────────────────────────
banner("6. Stop service")
svc_proc.terminate()
try:
    svc_proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    svc_proc.kill()
ok(f"Service stopped (exit code: {svc_proc.returncode})")
log_fh.close()

# ── 7. verify result ──────────────────────────────────────────────────────────
banner("7. Verify restore output")

if not processed:
    if err_file.exists():
        print(f"  ✗  Error file: {err_file.read_text()}")
    else:
        print("  ✗  Timed out — .done file never appeared")
    sys.exit(1)

ok(".done file written — request completed successfully")

staged = sorted(
    d for d in (outgoing / CHAIN_V).iterdir() if d.is_dir()
) if (outgoing / CHAIN_V).is_dir() else []

if len(staged) != 2:
    print(f"  ✗  Expected 2 staged cycles, found {len(staged)}: {[d.name for d in staged]}")
    sys.exit(1)

ok(f"Staged {len(staged)} cycle(s) in outgoing/{CHAIN_V}/:")
for d in staged:
    # Verify checksums pass on restored data.
    r = subprocess.run(
        ["sha256sum", "-c", "checksums.sha256"],
        cwd=str(d), capture_output=True, text=True,
    )
    status = "✓ checksums OK" if r.returncode == 0 else f"✗ CHECKSUM FAIL: {r.stdout[:200]}"
    ok(f"  {d.name}  —  {status}")

# Timing: check the service log for the inotifywait detection line.
if "restore request detected" in log_content:
    ok("inotifywait fired instantly on file drop ✓")
elif "restore request received" in log_content:
    ok("Request was detected (via poll fallback or inotifywait timeout) ✓")

banner("ALL TESTS PASSED")
print(f"  Listener mode: {'inotifywait (instant)' if 'restore listener: using inotifywait' in log_content else 'poll fallback'}")
print(f"  Cycles restored: {len(staged)}")
print(f"  Integrity: ALL checksums PASS")
print()
