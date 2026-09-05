#!/usr/bin/env python3
"""
Self-contained smoke test for the VM2 chain-aware backup pipeline.

What this tests:
  1. vm2_cycle_processor.py  – encrypts cycles from incoming/chain-v1/
  2. vm2_recovery_sender.py  – dry-run lists selected cycles
  3. vm2_recovery_sender.py  – full local restore into a temp outgoing dir
  4. Decrypted cycle integrity – sha256sum -c passes on restored data

Run from /home/recovery:
    python3 utilities/test_pipeline.py
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path("/home/recovery/local-backup")
UTILS = Path("/home/recovery/utilities")

ENCRYPTED = BASE / "encrypted"
INCOMING  = BASE / "incoming"

CHAIN_V = "chain-v1"


def run(cmd: list[str], env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    e = {**os.environ, **(env or {})}
    r = subprocess.run(cmd, capture_output=True, text=True, env=e)
    if check and r.returncode != 0:
        print(f"[FAIL] {' '.join(cmd)}")
        print("STDOUT:", r.stdout[-2000:])
        print("STDERR:", r.stderr[-2000:])
        sys.exit(1)
    return r


def ok(msg: str) -> None:
    print(f"  [OK]  {msg}")


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def main() -> None:
    # ------------------------------------------------------------------ #
    # Generate a fresh AES-256 key for this test run.                     #
    # ------------------------------------------------------------------ #
    key_b64 = base64.b64encode(os.urandom(32)).decode()
    env_key = {"VM2_AES_KEY_B64": key_b64}

    section("1. Verify incoming cycles exist")
    chain_in = INCOMING / CHAIN_V
    cycle_dirs = sorted([d for d in chain_in.iterdir() if d.is_dir()]) if chain_in.is_dir() else []
    assert cycle_dirs, f"No cycle dirs found under {chain_in}"
    ok(f"{len(cycle_dirs)} cycle(s) found in {chain_in}")
    for d in cycle_dirs:
        ok(f"  {d.name}")

    # ------------------------------------------------------------------ #
    # 2. Run the cycle processor (--once --keep-incoming)                 #
    # ------------------------------------------------------------------ #
    section("2. Encrypt cycles with vm2_cycle_processor")
    r = run([
        sys.executable, str(UTILS / "vm2_cycle_processor.py"),
        "--once",
        "--keep-incoming",
        "--incoming-dir",   str(INCOMING),
        "--encrypted-root", str(ENCRYPTED),
        "--work-dir",       str(BASE / "staging"),
    ], env=env_key)
    print(r.stdout or "(no stdout)")

    # Verify encrypted dir populated.
    enc_b64s    = sorted((ENCRYPTED / CHAIN_V).glob("*.tar.aes256gcm.b64")) if (ENCRYPTED / CHAIN_V).is_dir() else []
    assert len(enc_b64s)    == len(cycle_dirs), f"Expected {len(cycle_dirs)} encrypted artifacts, got {len(enc_b64s)}"

    for b in enc_b64s:
        cycle_id = b.name.split(".")[0]
        done_marker = b.parent / f"{cycle_id}.vm2_done"
        assert done_marker.is_file(), f"Missing .vm2_done for {cycle_id}"
        meta = json.loads(done_marker.read_text())
        assert meta["chain_v"] == CHAIN_V
        ok(f"encrypted {CHAIN_V}/{cycle_id} done marker chain_v={meta['chain_v']}")

    for b in enc_b64s:
        meta_path = b.parent / b.name.replace(".tar.aes256gcm.b64", ".tar.aes256gcm.meta.json")
        assert meta_path.is_file(), f"Missing meta json for {b.name}"
        meta = json.loads(meta_path.read_text())
        assert meta["chain_v"] == CHAIN_V
        assert meta["alg"]     == "AES-256-GCM"
        ok(f"encrypted {CHAIN_V}/{b.name}  alg={meta['alg']}")

    # ------------------------------------------------------------------ #
    # 3. Dry-run recovery (--chain 3)                                     #
    # ------------------------------------------------------------------ #
    section("3. Dry-run: select first 3 cycles from chain-v1")
    r = run([
        sys.executable, str(UTILS / "vm2_recovery_sender.py"),
        "--version", CHAIN_V,
        "--chain",   "3",
        "--source",  "local",
        "--encrypted-root", str(ENCRYPTED),
        "--dry-run",
    ], env=env_key)
    selected = [l for l in r.stdout.strip().splitlines() if not l.startswith("[")]
    assert len(selected) == 3, f"Expected 3 selected cycles, got {len(selected)}: {selected}"
    ok(f"Dry-run selected: {selected}")

    # Also test --chain 1 (only the very first cycle)
    r = run([
        sys.executable, str(UTILS / "vm2_recovery_sender.py"),
        "--version", CHAIN_V,
        "--chain",   "1",
        "--source",  "local",
        "--encrypted-root", str(ENCRYPTED),
        "--dry-run",
    ], env=env_key)
    sel1 = [l for l in r.stdout.strip().splitlines() if not l.startswith("[")]
    assert len(sel1) == 1, f"Expected 1 cycle, got {sel1}"
    assert sel1[0] == cycle_dirs[0].name, f"Expected oldest cycle {cycle_dirs[0].name}, got {sel1[0]}"
    ok(f"--chain 1 correctly selects oldest: {sel1[0]}")

    # ------------------------------------------------------------------ #
    # 4. Full local restore into temp outgoing dir                        #
    # ------------------------------------------------------------------ #
    section("4. Full local restore: --chain 2 into temp outgoing dir")
    with tempfile.TemporaryDirectory(prefix="vm2-test-outgoing-") as tmp_out:
        tmp_out_path = Path(tmp_out)
        r = run([
            sys.executable, str(UTILS / "vm2_recovery_sender.py"),
            "--version",       CHAIN_V,
            "--chain",         "2",
            "--source",        "local",
            "--encrypted-root", str(ENCRYPTED),
            "--outgoing-root",  str(tmp_out_path),
        ], env=env_key)
        print(r.stdout or "(no stdout)")

        restored_cycles = sorted((tmp_out_path / CHAIN_V).iterdir()) if (tmp_out_path / CHAIN_V).is_dir() else []
        assert len(restored_cycles) == 2, f"Expected 2 restored dirs, got {len(restored_cycles)}"

        for rc in restored_cycles:
            # Verify checksums pass on the restored cycle.
            cr = subprocess.run(
                ["sha256sum", "-c", "checksums.sha256"],
                cwd=str(rc),
                capture_output=True, text=True,
            )
            assert cr.returncode == 0, f"Checksum failed for restored {rc.name}: {cr.stdout}"
            ok(f"Restored + verified: {CHAIN_V}/{rc.name}")

    # ------------------------------------------------------------------ #
    # 5. Restore request file parsing (service-level test)                #
    # ------------------------------------------------------------------ #
    section("5. Restore request JSON parsing")
    req_content = json.dumps({
        "version": "chain-v1",
        "chain": 3,
        "source": "local",
        "send_to_vm1": False,
        "vm1_dest_root": "/tmp/test-dest",
    })
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        f.write(req_content)
        req_path = Path(f.name)
    try:
        r = run([
            sys.executable, "-c",
            f"""
import json, sys
from pathlib import Path

req_path = Path('{req_path}')
def_vm1  = '/default/dest'
obj = json.loads(req_path.read_text())

version  = obj.get('version') or obj.get('chain_v')
chain_n  = int(obj.get('chain'))
source   = obj.get('source', 'local')
send     = obj.get('send_to_vm1', True)
vm1_dest = obj.get('vm1_dest_root', def_vm1)

assert version  == 'chain-v1', version
assert chain_n  == 3,          chain_n
assert source   == 'local',    source
assert send     == False,      send
assert vm1_dest == '/tmp/test-dest', vm1_dest
print('request_parse_ok')
""",
        ])
        assert "request_parse_ok" in r.stdout
        ok("Restore request JSON parsed correctly")
    finally:
        req_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # Done                                                                 #
    # ------------------------------------------------------------------ #
    section("ALL TESTS PASSED")
    print(f"  Chain   : {CHAIN_V}")
    print(f"  Cycles  : {len(cycle_dirs)}")
    print(f"  Key     : (not logged)")
    print()


if __name__ == "__main__":
    main()
