#!/usr/bin/env python3
"""restore_cycle.py

Restores backup cycle artifacts into an output directory and optionally
launches isolated DB containers for PITR validation.

Key features implemented:
  Feature 2 – Single-Pass WAL Staging: WAL segments from all cycles are merged
    into one directory *before* PostgreSQL is started, so the engine sees a
    continuous stream and never forks its Timeline ID between cycles.
  Feature 4 – Chain Version Bump: after a successful restore the chain version
    stored in the state DB is incremented (chain-v1 → chain-v2 …) so new
    post-restore backup cycles are written to a fresh directory, leaving the
    old poisoned path to expire naturally.
"""

import argparse
import json
import os
import subprocess
import shutil
import sys
import time
import zlib
from dataclasses import dataclass
import re


import sys
import datetime

LOG_FILE = "/home/primary/utilities/backup/backup.log"

def _isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False

def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    return _isatty() and os.environ.get("LOG_COLOR", "1") != "0"

def _c(s: str, code: str) -> str:
    if not _color_enabled():
        return s
    return f"\x1b[{code}m{s}\x1b[0m"

def good_tag() -> str: return _c("<good>", "32")
def bad_tag() -> str: return _c("<error>", "31")
def info_tag() -> str: return _c("<info>", "36")
def warn_tag() -> str: return _c("<warn>", "33")

def _tag_good() -> str: return good_tag()
def _tag_bad() -> str: return bad_tag()
def _tag_info() -> str: return info_tag()

def _log_base(scope: str, tag: str, msg: str) -> None:
    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean_tag = ansi_escape.sub('', tag)
    
    if clean_tag not in ["<good>", "<error>", "<info>", "<warn>"]:
        tag = info_tag()
        clean_tag = "<info>"
        
    term_line = f"{scope:<10} {tag:<8} {msg}"
    print(term_line)
    
    try:
        import datetime
        tz = datetime.timezone(datetime.timedelta(hours=7))
        ts = datetime.datetime.now(tz).replace(microsecond=0).isoformat()
        file_line = f"[{ts}] {scope:<10} {clean_tag:<8} {msg}\n"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(file_line)
    except Exception:
        pass

def _log(prefix: str, msg: str, tag: str = "<info>") -> None:
    if "<good>" in tag: tag = good_tag()
    elif "<bad>" in tag or "<error>" in tag: tag = bad_tag()
    elif "<warn>" in tag: tag = warn_tag()
    else: tag = info_tag()
    _log_base(prefix, tag, msg)
TOOLS_DIR = os.path.abspath(os.path.dirname(__file__))
BACKUP_ROOT = os.path.abspath(os.path.join(TOOLS_DIR, ".."))
if BACKUP_ROOT not in sys.path:
    sys.path.insert(0, BACKUP_ROOT)

from lib.verify import verify_cycle  # noqa: E402
from lib.config import BackupConfig, RESTORE_INCOMING_DIR  # noqa: E402


def _list_cycle_ids(cycles_root: str) -> list[str]:
    if not os.path.isdir(cycles_root):
        raise FileNotFoundError(f"cycles_root is not a directory: {cycles_root}")
    cycle_ids: list[str] = []
    for name in os.listdir(cycles_root):
        p = os.path.join(cycles_root, name)
        if os.path.isdir(p):
            cycle_ids.append(name)
    cycle_ids.sort()
    return cycle_ids


def _latest_cycle_dir(cycles_root: str) -> str:
    cycle_ids = _list_cycle_ids(cycles_root)
    if not cycle_ids:
        raise FileNotFoundError(f"No cycles found under: {cycles_root}")
    return os.path.abspath(os.path.join(cycles_root, cycle_ids[-1]))


def _resolve_cycle_dir(arg: str, *, cycles_root: str) -> str:
    # 1) Accept a direct path.
    direct = os.path.abspath(arg)
    if os.path.isdir(direct):
        return direct

    # 2) Convenience keyword.
    if arg == "latest":
        return _latest_cycle_dir(cycles_root)

    # 3) Treat as cycle id under cycles_root.
    cand = os.path.join(cycles_root, arg)
    if os.path.isdir(cand):
        return os.path.abspath(cand)

    return direct


def _select_last_good_cycles(*, cycles_root: str, count: int, verify_raw: bool) -> list[str]:
    if count <= 0:
        raise ValueError("count must be >= 1")

    cycle_ids = _list_cycle_ids(cycles_root)
    if not cycle_ids:
        raise FileNotFoundError(f"No cycles found under: {cycles_root}")

    selected: list[str] = []
    skipped = 0
    for cycle_id in reversed(cycle_ids):
        cycle_dir = os.path.abspath(os.path.join(cycles_root, cycle_id))
        ok, errors = verify_cycle(cycle_dir, verify_raw=verify_raw)
        if ok:
            selected.append(cycle_dir)
            if len(selected) >= count:
                break
        else:
            skipped += 1
            msg = errors[0] if errors else "verification failed"
            _log("[SELECT]", f"Skipping corrupted cycle: {cycle_id} ({msg})")

    if len(selected) < count:
        raise RuntimeError(
            f"Only found {len(selected)} verified cycles (needed {count}); skipped={skipped}; cycles_root={cycles_root}"
        )

    # We iterated newest->oldest; restore expects oldest->newest.
    return list(reversed(selected))


def _select_first_n_cycles(*, cycles_root: str, count: int) -> list[str]:
    """Take the first *count* cycles in chronological order (oldest → newest).

    This is the complement of --last (which takes the NEWEST N).  Use this
    when you know cycle 4 is poisoned and want cycles 1-3 explicitly:

        --version chain-v1 --chain 3
    """
    if count <= 0:
        raise ValueError("count must be >= 1")

    cycle_ids = _list_cycle_ids(cycles_root)  # already sorted oldest→newest
    if not cycle_ids:
        raise FileNotFoundError(f"No cycles found under: {cycles_root}")

    selected = cycle_ids[:count]
    _log(
        "[SELECT]",
        f"--chain {count}: selecting first {len(selected)} of {len(cycle_ids)} cycles "
        f"(oldest→newest): {selected}",
    )
    if len(selected) < count:
        raise RuntimeError(
            f"Requested --chain {count} but only {len(selected)} cycle(s) found under {cycles_root}"
        )
    return [os.path.join(cycles_root, cid) for cid in selected]


def _select_newest_contiguous_verified_chain(*, cycles_root: str, verify_raw: bool) -> list[str]:
    """Pick the newest contiguous chain of VERIFIED cycles.

    - Finds the newest verified cycle (starting from newest).
    - Then walks backwards including prior cycles as long as they verify.
    - Stops at the first corrupted/missing cycle to avoid breaking incremental dependencies.

    Returns cycle directories in chronological order (oldest -> newest).
    """

    cycle_ids = _list_cycle_ids(cycles_root)
    if not cycle_ids:
        raise FileNotFoundError(f"No cycles found under: {cycles_root}")

    newest_ok_idx: int | None = None
    for i in range(len(cycle_ids) - 1, -1, -1):
        cycle_id = cycle_ids[i]
        cycle_dir = os.path.abspath(os.path.join(cycles_root, cycle_id))
        ok, errors = verify_cycle(cycle_dir, verify_raw=verify_raw)
        if ok:
            newest_ok_idx = i
            break
        msg = errors[0] if errors else "verification failed"
        _log("[SELECT]", f"Ignoring corrupted newest cycle: {cycle_id} ({msg})")

    if newest_ok_idx is None:
        raise RuntimeError(f"No verified cycles found under: {cycles_root}")

    selected: list[str] = []
    for i in range(newest_ok_idx, -1, -1):
        cycle_id = cycle_ids[i]
        cycle_dir = os.path.abspath(os.path.join(cycles_root, cycle_id))
        ok, errors = verify_cycle(cycle_dir, verify_raw=verify_raw)
        if not ok:
            msg = errors[0] if errors else "verification failed"
            _log("[SELECT]", f"Stopping chain at corrupted cycle: {cycle_id} ({msg})")
            break
        selected.append(cycle_dir)

    return list(reversed(selected))


def _read_manifest(cycle_dir: str) -> dict:
    path = os.path.join(cycle_dir, "manifest.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _ensure_empty_dir(path: str) -> None:
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


def _copy_tree(src: str, dst: str) -> tuple[int, int]:
    files = 0
    bytes_total = 0
    os.makedirs(dst, exist_ok=True)
    for walk_root, dirs, filenames in os.walk(src):
        rel_root = os.path.relpath(walk_root, src)
        for d in dirs:
            os.makedirs(os.path.join(dst, rel_root, d), exist_ok=True)
        for name in filenames:
            sp = os.path.join(walk_root, name)
            dp = os.path.join(dst, rel_root, name)
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            shutil.copy2(sp, dp)
            files += 1
            try:
                bytes_total += int(os.path.getsize(sp))
            except Exception:
                pass
    return files, bytes_total


def _decompress_zlib_file(src: str, dst: str) -> int:
    with open(src, "rb") as f:
        payload = f.read()
    raw = zlib.decompress(payload)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(raw)
    return len(raw)

def _decompress_zstd_file(src: str, dst: str) -> int:
    import zstandard as zstd
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    dctx = zstd.ZstdDecompressor()
    with open(src, "rb") as fr, open(dst, "wb") as fw:
        dctx.copy_stream(fr, fw)
    return os.path.getsize(dst)


def _apply_chunk(*, out_unstructured_dir: str, rel_file: str, chunk_index: int, chunk_size: int, chunk_bytes: bytes) -> None:
    dst_path = os.path.join(out_unstructured_dir, rel_file)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)

    offset = int(chunk_index) * int(chunk_size)

    mode = "r+b" if os.path.exists(dst_path) else "w+b"
    with open(dst_path, mode) as f:
        f.seek(0, os.SEEK_END)
        cur_size = f.tell()
        if cur_size < offset:
            f.write(b"\x00" * (offset - cur_size))
        f.seek(offset)
        f.write(chunk_bytes)


@dataclass
class RestoreStats:
    out_root: str
    out_unstructured: str
    out_pg: str
    out_mongo: str
    applied_chunks: int
    verified_ok: int
    verified_fail: int
    wal_files_copied: int
    oplog_files_written: int
    pg_basebackup_files_copied: int
    mongo_basebackup_files_copied: int
    pg_latest_base_tar_gz: str | None
    mongo_latest_archive_gz: str | None
    merged_wal_dir: str | None = None


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    _log("[CMD]", " ".join(cmd))
    return subprocess.run(cmd, check=check)


def _docker_rm(container: str) -> None:
    try:
        _run(["docker", "rm", "-f", container], check=False)
    except Exception:
        pass


def _build_merged_wal_dir(cycle_dirs: list[str], out_pg: str) -> str:
    """Feature 2 – Single-Pass WAL Staging.

    Collect every WAL segment and .backup history file from all cycles into
    a single flat directory (``out_pg/merged_wal/``) *before* PostgreSQL is
    started.  Files from later cycles overwrite same-named files from earlier
    ones so the newest version always wins (WAL segments are immutable by
    name, but .backup files may be refreshed).

    Returns the path to the merged directory.
    """
    merged = os.path.join(out_pg, "merged_wal")
    os.makedirs(merged, exist_ok=True)

    total = 0
    for cycle_dir in cycle_dirs:
        src_pg = os.path.join(cycle_dir, "pg")
        if not os.path.isdir(src_pg):
            continue
        for name in sorted(os.listdir(src_pg)):
            sp = os.path.join(src_pg, name)
            if not os.path.isfile(sp):
                # skip basebackup/ subdir etc.
                continue
            
            if name.endswith(".zst"):
                out_name = name[:-4]
                dp = os.path.join(merged, out_name)
                import zstandard as zstd
                dctx = zstd.ZstdDecompressor()
                with open(sp, "rb") as fr, open(dp, "wb") as fw:
                    dctx.copy_stream(fr, fw)
            else:
                dp = os.path.join(merged, name)
                shutil.copy2(sp, dp)
            total += 1

    _log(
        "[PG][STAGE]",
        f"Merged WAL from {len(cycle_dirs)} cycle(s) into single stream: "
        f"{total} files -> {merged}",
    )
    return merged


def _pg_restore_container(
    *,
    pgdata: str,
    base_tar_gz: str,
    merged_wal_dir: str,
    container: str,
    port: int,
    image: str,
) -> None:
    """Start an isolated Postgres restore container.

    *merged_wal_dir* must already contain WAL from **all** required cycles
    (built by _build_merged_wal_dir) so Postgres sees one continuous stream
    and never forks its Timeline ID between incremental cycles.
    """
    if not os.path.isfile(base_tar_gz):
        raise FileNotFoundError(f"Missing PG basebackup tar: {base_tar_gz}")
    if not os.path.isdir(merged_wal_dir):
        raise FileNotFoundError(f"Missing merged WAL dir: {merged_wal_dir}")

    os.makedirs(pgdata, exist_ok=True)

    if base_tar_gz.endswith(".zst"):
        uncompressed_tar = base_tar_gz[:-4]
        _decompress_zstd_file(base_tar_gz, uncompressed_tar)
        target_tar = uncompressed_tar
        tar_flags = "-xf"
    else:
        target_tar = base_tar_gz
        tar_flags = "-xzf"

    # Populate PGDATA from base.tar.gz inside a throwaway container.
    _run(
        [
            "docker",
            "run",
            "--rm",
            "-u",
            "root",
            "-v",
            f"{pgdata}:/var/lib/postgresql/data",
            "-v",
            f"{os.path.abspath(target_tar)}:/base.tar:ro",
            image,
            "sh",
            "-lc",
            "rm -rf /var/lib/postgresql/data/* "
            f"&& tar {tar_flags} /base.tar -C /var/lib/postgresql/data "
            "&& chown -R postgres:postgres /var/lib/postgresql/data "
            "&& touch /var/lib/postgresql/data/recovery.signal",
        ],
        check=True,
    )

    if target_tar != base_tar_gz and os.path.isfile(target_tar):
        try:
            os.remove(target_tar)
        except Exception:
            pass

    _docker_rm(container)

    # Start Postgres; replay WAL from the single merged directory.
    # Because all cycles' WAL is already present, Postgres will replay the
    # complete continuous stream without forking the timeline.
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-p",
            f"{int(port)}:5432",
            "-v",
            f"{pgdata}:/var/lib/postgresql/data",
            "-v",
            f"{os.path.abspath(merged_wal_dir)}:/wal:ro",
            image,
            "-c",
            "restore_command=cp /wal/%f %p",
        ],
        check=True,
    )

    _log(
        "[PG][RESTORE]",
        f"Started container={container} host_port={port} "
        f"PGDATA={pgdata} merged_WAL={merged_wal_dir}",
    )


def _mongo_restore_container(*, mongo_data: str, archive_gz: str, container: str, port: int, image: str) -> None:
    if not os.path.isfile(archive_gz):
        raise FileNotFoundError(f"Missing Mongo basebackup archive: {archive_gz}")

    os.makedirs(mongo_data, exist_ok=True)

    _docker_rm(container)
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-p",
            f"{int(port)}:27017",
            "-v",
            f"{mongo_data}:/data/db",
            image,
            "--bind_ip_all",
        ],
        check=True,
    )

    # Restore the archive into the new container.
    with open(archive_gz, "rb") as f:
        subprocess.run(
            ["docker", "exec", "-i", container, "mongorestore", "--archive", "--gzip", "--drop"],
            stdin=f,
            check=True,
        )

    _log("[MONGO][RESTORE]", f"Started container={container} host_port={port} data={mongo_data}")


def _mongo_apply_oplog_deltas(*, container: str, delta_files: list[str]) -> None:
    if not delta_files:
        _log("[MONGO][OPLOG]", "No oplog deltas to apply")
        return

    js = r"""
const fs = require('fs');
const path = '/tmp/oplog_delta.json';
const raw = fs.readFileSync(path, 'utf8');
const ops = EJSON.parse(raw);

let applied = 0;
let skipped = 0;
let failed = 0;

let skippedDupInsert = 0;
let skippedUnsupportedUpdate = 0;
let skippedCmd = 0;
let skippedNoNs = 0;
let skippedInternalNs = 0;

let failedInsert = 0;
let failedUpdate = 0;
let failedDelete = 0;

const errSamples = [];

function sampleErr(where, err) {
    if (errSamples.length >= 5) return;
    const msg = (err && (err.message || err.errmsg)) ? (err.message || err.errmsg) : String(err);
    errSamples.push(`${where}: ${msg}`);
}

function nsParts(ns) {
  const i = (ns || '').indexOf('.');
  if (i <= 0) return null;
  return { db: ns.slice(0, i), coll: ns.slice(i + 1) };
}

function isModifierUpdate(doc) {
    if (!doc || typeof doc !== 'object') return false;
    for (const k of Object.keys(doc)) {
        if (k.startsWith('$')) return true;
    }
    return false;
}

function diffToUpdate(diff, prefix = '') {
    // Convert MongoDB $v:2 oplog diff into a standard {$set,$unset} update.
    // Handles basic field updates used by our generator.
    const set = {};
    const unset = {};
    let ok = true;

    function join(p, k) {
        return p ? `${p}.${k}` : k;
    }

    function walk(d, p) {
        if (!d || typeof d !== 'object') return;

        if (d.u && typeof d.u === 'object') {
            for (const [k, v] of Object.entries(d.u)) {
                set[join(p, k)] = v;
            }
        }
        if (d.i && typeof d.i === 'object') {
            for (const [k, v] of Object.entries(d.i)) {
                set[join(p, k)] = v;
            }
        }
        if (d.d && typeof d.d === 'object') {
            for (const k of Object.keys(d.d)) {
                unset[join(p, k)] = '';
            }
        }
        // sub-diff (nested object updates)
        if (d.s && typeof d.s === 'object') {
            for (const [k, sub] of Object.entries(d.s)) {
                walk(sub, join(p, k));
            }
        }
        // array diffs exist, but are more complex; skip them for now
        if (d.a) {
            ok = false;
        }
    }

    walk(diff, prefix);
    const update = {};
    if (Object.keys(set).length) update['$set'] = set;
    if (Object.keys(unset).length) update['$unset'] = unset;

    if (!Object.keys(update).length) {
        ok = false;
    }

    return { ok, update };
}

for (const e of ops) {
  try {
    const op = e.op;
    const ns = e.ns;
        if (!op || !ns) { skipped++; skippedNoNs++; continue; }

    // Never replay internals
        if (ns.startsWith('local.') || ns.startsWith('config.') || ns.startsWith('admin.')) { skipped++; skippedInternalNs++; continue; }

    const p = nsParts(ns);
    if (!p) { skipped++; continue; }

        const targetDb = db.getSiblingDB(p.db);
        const targetColl = targetDb.getCollection(p.coll);

    if (op === 'i') {
            try {
                targetColl.insertOne(e.o);
                applied++;
            } catch (err) {
                // Duplicate key inserts are expected when basebackup already includes some docs.
                if (err && err.code === 11000) {
                    skipped++; skippedDupInsert++;
                } else {
                    failed++; failedInsert++;
                    sampleErr('insert', err);
                }
            }
    } else if (op === 'u') {
            const o = e.o || {};
            // MongoDB 7 uses $v:2 diff format in the oplog.
            if (o && typeof o === 'object' && o['$v'] === 2 && o['diff'] && typeof o['diff'] === 'object') {
                const conv = diffToUpdate(o['diff']);
                if (!conv.ok) {
                    skipped++; skippedUnsupportedUpdate++;
                } else {
                    try {
                        targetColl.updateOne(e.o2 || {}, conv.update, { upsert: false });
                        applied++;
                    } catch (err) {
                        failed++; failedUpdate++;
                        sampleErr('update(diff)', err);
                    }
                }
            } else if (isModifierUpdate(o)) {
                try {
                    targetColl.updateOne(e.o2 || {}, o, { upsert: false });
                    applied++;
                } catch (err) {
                    failed++; failedUpdate++;
                    sampleErr('update(mod)', err);
                }
            } else {
                // replacement-style update
                try {
                    targetColl.replaceOne(e.o2 || {}, o, { upsert: false });
                    applied++;
                } catch (err) {
                    failed++; failedUpdate++;
                    sampleErr('replace', err);
                }
            }
    } else if (op === 'd') {
            try {
                targetColl.deleteOne(e.o || {});
                applied++;
            } catch (err) {
                failed++; failedDelete++;
                sampleErr('delete', err);
            }
    } else {
            skipped++;
            skippedCmd++;
    }
  } catch (err) {
        failed++;
        sampleErr('top', err);
  }
}

print(
    `APPLIED=${applied} SKIPPED=${skipped} FAILED=${failed}` +
    ` | skipped_dup_insert=${skippedDupInsert}` +
    ` skipped_unsupported_update=${skippedUnsupportedUpdate}` +
    ` skipped_cmd=${skippedCmd}` +
    ` | failed_i=${failedInsert} failed_u=${failedUpdate} failed_d=${failedDelete}` +
    (errSamples.length ? ` | samples=${errSamples.join(' || ')}` : '')
);
"""

    for path in delta_files:
        _log("[MONGO][OPLOG]", f"Applying delta: {path}")
        _run(["docker", "cp", path, f"{container}:/tmp/oplog_delta.json"], check=True)

        proc = subprocess.run(
            ["docker", "exec", "-i", container, "mongosh", "mongodb://localhost:27017/admin", "--quiet", "--eval", js],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        out = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
        if proc.returncode != 0:
            raise RuntimeError(f"oplog replay failed for {os.path.basename(path)}: {out or 'mongosh error'}")
        _log("[MONGO][OPLOG]", out or "OK")


def restore_cycle(
    *,
    base_unstructured_dir: str,
    out_root: str,
    out_unstructured: str | None = None,
    cycle_dirs: list[str],
    chunk_size: int,
    verify_latest: bool,
    verify_raw: bool,
) -> RestoreStats:
    if not cycle_dirs:
        raise ValueError("No cycle dirs provided")

    cycle_dirs = [os.path.abspath(d) for d in cycle_dirs]
    latest = cycle_dirs[-1]

    _log("[RESTORE]", f"Base unstructured: {base_unstructured_dir}")
    _log("[RESTORE]", f"Output root:       {out_root}")
    _log("[RESTORE]", f"Cycles:            {len(cycle_dirs)}")

    if verify_latest:
        _log("[VERIFY]", f"Verifying latest cycle: {latest} (raw={verify_raw})")
        ok, errors = verify_cycle(latest, verify_raw=verify_raw)
        if not ok:
            for err in errors[:20]:
                _log("[VERIFY]", f"FAIL: {err}")
            raise RuntimeError(f"Cycle verification failed: {latest}")
        _log("[VERIFY]", "OK")

    out_root = os.path.abspath(out_root)
    out_unstructured = os.path.abspath(out_unstructured) if out_unstructured else os.path.join(out_root, "unstructured")
    out_pg = os.path.join(out_root, "pg")
    out_mongo = os.path.join(out_root, "mongo")

    # Safely clear only the artifact output directories. Do not wipe out_root entirely.
    for d in [out_unstructured, out_pg, out_mongo]:
        if d == base_unstructured_dir:
            continue
        _ensure_empty_dir(d)

    # 1) Base copy for unstructured
    if base_unstructured_dir != out_unstructured:
        _log("[PFC]", f"Copying base unstructured -> {out_unstructured}")
        started = time.perf_counter()
        base_files, base_bytes = _copy_tree(base_unstructured_dir, out_unstructured)
        _log("[PFC]", f"Base copy: files={base_files} bytes={base_bytes} elapsed={time.perf_counter() - started:.2f}s")
    else:
        _log("[PFC]", f"Base unstructured and out_unstructured are identical ({out_unstructured}). Applying deltas in-place.")

    # 2) Apply PFC deltas across cycle chain
    applied = 0
    verified_ok = 0
    verified_fail = 0

    for cycle_dir in cycle_dirs:
        manifest_path = os.path.join(cycle_dir, "manifest.json")
        _log("[RESTORE]", f"Reading manifest: {manifest_path}")
        manifest = _read_manifest(cycle_dir)

        pfc = manifest.get("pfc") or {}
        unstr_base = manifest.get("unstructured_basebackup")
        if unstr_base:
            rel_art = unstr_base.get("artifact")
            if rel_art:
                art_path = os.path.join(cycle_dir, rel_art)
                if os.path.isfile(art_path):
                    _log("[PFC]", f"Extracting unstructured basebackup -> {out_unstructured}")
                    _run(["tar", "-xzf", art_path, "-C", out_unstructured], check=True)
                else:
                    _log("[PFC]", f"Warning: unstructured basebackup artifact missing: {art_path}")
        deltas = pfc.get("deltas") if isinstance(pfc, dict) else None
        if not isinstance(deltas, list):
            deltas = []

        _log("[PFC]", f"Applying PFC deltas from {cycle_dir}: count={len(deltas)}")
        for entry in deltas:
            if not isinstance(entry, dict):
                continue

            rel_art = entry.get("artifact")
            rel_file = entry.get("source_file")
            chunk_index = entry.get("chunk_index")
            compression = entry.get("compression")
            expected_sha = entry.get("raw_sha256")

            if not rel_art or not rel_file or chunk_index is None:
                continue

            art_path = os.path.join(cycle_dir, rel_art)
            if not os.path.isfile(art_path):
                raise FileNotFoundError(f"Missing PFC artifact: {art_path}")

            if compression == "zlib":
                with open(art_path, "rb") as f:
                    import zlib
                    chunk_bytes = zlib.decompress(f.read())
            elif compression == "zstd":
                with open(art_path, "rb") as f:
                    import zstandard as zstd
                    dctx = zstd.ZstdDecompressor()
                    chunk_bytes = dctx.decompress(f.read())
            else:
                with open(art_path, "rb") as f:
                    chunk_bytes = f.read()

            # integrity check of the chunk payload
            if expected_sha:
                import hashlib

                actual_sha = hashlib.sha256(chunk_bytes).hexdigest()
                if actual_sha == expected_sha:
                    verified_ok += 1
                else:
                    verified_fail += 1
                    _log(
                        "[PFC]",
                        f"HASH MISMATCH file={rel_file} chunk={chunk_index} expected={expected_sha} actual={actual_sha}",
                    )

            _apply_chunk(
                out_unstructured_dir=out_unstructured,
                rel_file=rel_file,
                chunk_index=int(chunk_index),
                chunk_size=int(chunk_size),
                chunk_bytes=chunk_bytes,
            )
            applied += 1

        scanned_sizes = pfc.get("scanned_sizes", {})
        if scanned_sizes:
            for rel_file, final_size in scanned_sizes.items():
                dst_path = os.path.join(out_unstructured, rel_file)
                if not os.path.exists(dst_path):
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    with open(dst_path, "wb"):
                        pass
                with open(dst_path, "r+b") as f:
                    f.truncate(final_size)
                    
        deleted_files = pfc.get("deleted_files", [])
        if deleted_files:
            for rel_file in deleted_files:
                dst_path = os.path.join(out_unstructured, rel_file)
                if os.path.exists(dst_path):
                    try:
                        os.remove(dst_path)
                    except Exception as e:
                        _log("[PFC]", f"Warning: Failed to remove deleted file {rel_file}: {e}")

    # 3) Feature 2 – Single-Pass WAL Staging.
    # Merge ALL cycles' WAL into one directory so the restore container gets a
    # continuous stream from the start.  This prevents Postgres from forking
    # its Timeline ID after replaying the first cycle's WAL.
    os.makedirs(out_pg, exist_ok=True)
    merged_wal_dir = _build_merged_wal_dir(cycle_dirs, out_pg)
    wal_files_copied = sum(
        1 for f in os.listdir(merged_wal_dir)
        if os.path.isfile(os.path.join(merged_wal_dir, f))
    )
    _log("[PG]", f"Single-pass WAL staging: files={wal_files_copied} merged_dir={merged_wal_dir}")

    # 3b) Copy PG basebackup artifacts (if present) so the restore output is complete.
    pg_basebackup_files_copied = 0
    pg_latest_base_tar_gz = None
    for cycle_dir in cycle_dirs:
        src_base = os.path.join(cycle_dir, "pg", "basebackup")
        if not os.path.isdir(src_base):
            continue
        cycle_id = os.path.basename(os.path.abspath(cycle_dir))
        dst_base = os.path.join(out_pg, "basebackup", cycle_id)
        files_copied, _bytes_copied = _copy_tree(src_base, dst_base)
        pg_basebackup_files_copied += files_copied
        cand_gz = os.path.join(dst_base, "base.tar.gz")
        cand_zst = os.path.join(dst_base, "base.tar.zst")
        if os.path.isfile(cand_zst):
            pg_latest_base_tar_gz = cand_zst
        elif os.path.isfile(cand_gz):
            pg_latest_base_tar_gz = cand_gz
    if pg_basebackup_files_copied:
        _log("[PG]", f"Restored basebackup artifacts: files_copied={pg_basebackup_files_copied} -> {out_pg}/basebackup")

    # 4) Restore/decompress Mongo oplog deltas for inspection (artifact restore)
    oplog_files_written = 0
    os.makedirs(out_mongo, exist_ok=True)
    for cycle_dir in cycle_dirs:
        m = _read_manifest(cycle_dir)
        mongo = m.get("mongo") or {}
        delta = mongo.get("delta") if isinstance(mongo, dict) else None
        if not isinstance(delta, dict):
            continue
        artifact = delta.get("artifact")
        compression = delta.get("compression")
        if not artifact:
            continue

        src = os.path.join(cycle_dir, artifact)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"Missing mongo artifact: {src}")

        cycle_id = os.path.basename(os.path.abspath(cycle_dir))
        if compression == "zlib":
            dst = os.path.join(out_mongo, f"oplog_delta.{cycle_id}.json")
            raw_bytes = _decompress_zlib_file(src, dst)
            _log("[MONGO]", f"Decompressed oplog delta: {src} -> {dst} bytes={raw_bytes}")
        elif compression == "zstd":
            dst = os.path.join(out_mongo, f"oplog_delta.{cycle_id}.json")
            raw_bytes = _decompress_zstd_file(src, dst)
            _log("[MONGO]", f"Decompressed oplog delta (zstd): {src} -> {dst} bytes={raw_bytes}")
        else:
            dst = os.path.join(out_mongo, os.path.basename(src))
            shutil.copy2(src, dst)
            _log("[MONGO]", f"Copied oplog delta: {src} -> {dst}")
        oplog_files_written += 1

    # 4b) Copy Mongo basebackup artifacts (if present) so the restore output is complete.
    mongo_basebackup_files_copied = 0
    mongo_latest_archive_gz = None
    for cycle_dir in cycle_dirs:
        src_base = os.path.join(cycle_dir, "mongo", "basebackup")
        if not os.path.isdir(src_base):
            continue
        cycle_id = os.path.basename(os.path.abspath(cycle_dir))
        dst_base = os.path.join(out_mongo, "basebackup", cycle_id)
        files_copied, _bytes_copied = _copy_tree(src_base, dst_base)
        mongo_basebackup_files_copied += files_copied
        cand = os.path.join(dst_base, "mongodump.archive.gz")
        if os.path.isfile(cand):
            mongo_latest_archive_gz = cand
    if mongo_basebackup_files_copied:
        _log(
            "[MONGO]",
            f"Restored basebackup artifacts: files_copied={mongo_basebackup_files_copied} -> {out_mongo}/basebackup",
        )

    _log(
        "[RESTORE]",
        f"Done: out_root={out_root} pfc_chunks_applied={applied} "
        f"verified_ok={verified_ok} verified_fail={verified_fail} "
        f"wal_merged_dir={merged_wal_dir}",
    )

    if verified_fail:
        raise RuntimeError("PFC restore failed: chunk hash mismatches")

    return RestoreStats(
        out_root=out_root,
        out_unstructured=out_unstructured,
        out_pg=out_pg,
        out_mongo=out_mongo,
        applied_chunks=applied,
        verified_ok=verified_ok,
        verified_fail=verified_fail,
        wal_files_copied=wal_files_copied,
        oplog_files_written=oplog_files_written,
        pg_basebackup_files_copied=pg_basebackup_files_copied,
        mongo_basebackup_files_copied=mongo_basebackup_files_copied,
        pg_latest_base_tar_gz=pg_latest_base_tar_gz,
        mongo_latest_archive_gz=mongo_latest_archive_gz,
        merged_wal_dir=merged_wal_dir,
    )


# ---------------------------------------------------------------------------
# Feature 4: chain version bump
# ---------------------------------------------------------------------------

def _bump_chain_version(state_db: str) -> str:
    """Increment the chain version in the state DB and return the new value.

    chain-v1 -> chain-v2, chain-v2 -> chain-v3, etc.
    """
    import sqlite3
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.commit()
        cur = conn.execute("SELECT value FROM metadata WHERE key='chain_version'")
        row = cur.fetchone()
        old_ver = (row[0] or "").strip() if row else ""
        new_n = 2
        if old_ver.startswith("chain-v"):
            try:
                new_n = int(old_ver[len("chain-v"):]) + 1
            except ValueError:
                pass
        new_ver = f"chain-v{new_n}"
        conn.execute(
            "REPLACE INTO metadata(key,value) VALUES(?,?)",
            ("chain_version", new_ver),
        )
        conn.commit()
    finally:
        conn.close()
    _log(
        "[CHAIN]",
        f"Chain version bumped: {old_ver or '<none>'} -> {new_ver} "
        f"(next backup cycle writes to {new_ver}/)",
    )
    return new_ver


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Restore cycle artifacts (PFC/unstructured + PG WAL files + Mongo oplog deltas) into an output directory. "
            "Note: PG/Mongo restore here is artifact-level (copy/decompress) and integrity verification; full DB PITR requires base backups."
        )
    )
    ap.add_argument("--base-unstructured", default="/home/primary/data/unstructured", help="Base unstructured dir to copy")
    ap.add_argument(
        "--out-root",
        default=RESTORE_INCOMING_DIR,
        help=(
            "Output root dir for restored artifacts "
            f"(default: {RESTORE_INCOMING_DIR}, override via RESTORE_INCOMING_DIR env var). "
            "Matches reset_system.py backup_incoming path."
        ),
    )
    ap.add_argument("--chunk-size", type=int, default=1024 * 1024, help="PFC chunk size (default 1048576)")
    ap.add_argument("--no-verify", action="store_true", help="Skip verify of newest cycle")
    ap.add_argument("--verify-raw", action="store_true", help="Also raw-verify PFC/Mongo artifacts")

    ap.add_argument(
        "--version",
        default=None,
        metavar="CHAIN_VER",
        help=(
            "Chain version to restore from, e.g. 'chain-v1'. "
            "Resolves to --cycles-root/CHAIN_VER/.  "
            "Overrides the auto-detect logic. Example: --version chain-v1"
        ),
    )
    ap.add_argument(
        "--cycles-root",
        default=BackupConfig().cycles_root,
        help="Root directory containing chain-vN sub-dirs or cycle folders (default from BackupConfig)",
    )
    ap.add_argument(
        "--chain",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Restore the first N cycles in chronological order (oldest→newest). "
            "Use this to deliberately exclude a poisoned tail — e.g. if cycle 4 is bad: "
            "--version chain-v1 --chain 3"
        ),
    )
    ap.add_argument(
        "--last",
        type=int,
        default=0,
        help=(
            "Restore the newest N cycles under --cycles-root (applied in order). "
            "Example: --last 2"
        ),
    )
    ap.add_argument(
        "--last-good",
        type=int,
        default=0,
        help=(
            "Restore the newest N VERIFIED cycles under --cycles-root (applied in order), "
            "skipping corrupted cycles. Uses --verify-raw mode if provided. Example: --last-good 2"
        ),
    )
    ap.add_argument(
        "--good-chain",
        action="store_true",
        help=(
            "Restore from the newest contiguous VERIFIED cycle chain under --cycles-root. "
            "If the newest cycle is corrupted, it is ignored; selection stops at the first corrupted cycle when walking backwards. "
            "Preserves incremental dependencies (recommended when you suspect corruption)."
        ),
    )

    # Optional: start isolated DB restore containers (does not touch live DBs)
    ap.add_argument("--pg-restore", action="store_true", help="Start a Postgres restore container from latest basebackup")
    ap.add_argument("--pg-container", default="postgres_live", help="Postgres restore container name")
    ap.add_argument("--pg-port", type=int, default=5432, help="Host port for Postgres restore container")
    ap.add_argument("--pg-image", default="postgres:15-alpine", help="Docker image for Postgres restore")

    ap.add_argument("--mongo-restore", action="store_true", help="Start a Mongo restore container from latest basebackup")
    ap.add_argument("--mongo-container", default="mongodb_live", help="Mongo restore container name")
    ap.add_argument("--mongo-port", type=int, default=27017, help="Host port for Mongo restore container")
    ap.add_argument("--mongo-image", default="mongo:7.0", help="Docker image for Mongo restore")

    ap.add_argument(
        "--promote",
        action="store_true",
        help=(
            "Start restored DB containers as the LIVE stack (postgres_live:5432, mongodb_live:27017) "
            "instead of the isolated _restore aliases on alternate ports. "
            "Use when the primary stack is down and you want the monitor/app to pick up "
            "the restored data immediately without any rename step."
        ),
    )

    # Feature 4: chain version bump
    ap.add_argument(
        "--bump-chain-version",
        action="store_true",
        help=(
            "After a successful restore, bump the chain version in --state-db "
            "(chain-v1 -> chain-v2 etc.) so the next backup cycle is isolated."
        ),
    )
    ap.add_argument(
        "--state-db",
        default=BackupConfig().state_db,
        help="Path to the backup state SQLite DB (used for --bump-chain-version).",
    )

    ap.add_argument(
        "cycles",
        nargs="*",
        help=(
            "One or more cycle directories or cycle IDs (apply in order). "
            "You can also use 'latest'. Alternatively, pass --last N."
        ),
    )
    args = ap.parse_args()

    # -----------------------------------------------------------------------
    # Resolve cycles_root:
    #  1. --version chain-v1   → explicit chain sub-dir under --cycles-root
    #  2. auto-detect          → descend into newest chain-vN if no cycles at root
    #  3. direct               → use --cycles-root as-is
    # -----------------------------------------------------------------------
    cycles_root_raw = os.path.abspath(args.cycles_root)

    if args.version:
        # Explicit chain version: cycles_root/chain-v1/
        cycles_root = os.path.join(cycles_root_raw, args.version)
        if not os.path.isdir(cycles_root):
            ap.error(f"--version {args.version!r}: directory not found: {cycles_root}")
        _log("[SELECT]", f"Using explicit chain version: {cycles_root}")
    elif os.path.isdir(cycles_root_raw):
        # Auto-detect: if root has chain-vN dirs but no YYYYMMDD_HHMMSS dirs, descend.
        direct_children = [
            n for n in os.listdir(cycles_root_raw)
            if os.path.isdir(os.path.join(cycles_root_raw, n))
        ]
        has_cycles = any(len(n) == 15 and n[8:9] == "_" for n in direct_children)
        chain_dirs = sorted(
            n for n in direct_children
            if n.startswith("chain-v") and not has_cycles
        )
        if chain_dirs and not has_cycles:
            cycles_root = os.path.join(cycles_root_raw, chain_dirs[-1])
            _log("[SELECT]", f"Auto-detected chain dir: {cycles_root}")
        else:
            cycles_root = cycles_root_raw
    else:
        cycles_root = cycles_root_raw

    selector_count = (
        int(bool(args.chain))
        + int(bool(args.last))
        + int(bool(args.last_good))
        + int(bool(args.good_chain))
    )
    if selector_count > 1:
        ap.error("Use only one of: --chain N, --last N, --last-good N, --good-chain")
    if selector_count and args.cycles:
        ap.error("Use either a selector (--chain/--last/--last-good/--good-chain) or explicit cycles, not both")
    if not selector_count and not args.cycles:
        ap.error("Provide at least one cycle (path/id/latest) or use a selector (--chain/--last/--last-good/--good-chain)")

    try:
        if args.chain:
            if int(args.chain) <= 0:
                ap.error("--chain must be >= 1")
            cycle_dirs = _select_first_n_cycles(
                cycles_root=cycles_root,
                count=int(args.chain),
            )
        elif args.good_chain:
            cycle_dirs = _select_newest_contiguous_verified_chain(
                cycles_root=cycles_root,
                verify_raw=bool(args.verify_raw),
            )
        elif args.last_good:
            if int(args.last_good) <= 0:
                ap.error("--last-good must be >= 1")
            cycle_dirs = _select_last_good_cycles(
                cycles_root=cycles_root,
                count=int(args.last_good),
                verify_raw=bool(args.verify_raw),
            )
        elif args.last:
            if int(args.last) <= 0:
                ap.error("--last must be >= 1")
            cycle_ids = _list_cycle_ids(cycles_root)
            if not cycle_ids:
                ap.error(f"No cycles found under: {cycles_root}")
            selected = cycle_ids[-int(args.last):]
            cycle_dirs = [os.path.join(cycles_root, cid) for cid in selected]
        else:
            cycle_dirs = [_resolve_cycle_dir(c, cycles_root=cycles_root) for c in args.cycles]
    except SystemExit:
        raise
    except Exception as e:
        ap.error(str(e))

    try:
        stats = restore_cycle(
            base_unstructured_dir=args.base_unstructured,
            out_root=args.out_root,
            out_unstructured="/home/primary/data/unstructured" if args.promote else None,
            cycle_dirs=cycle_dirs,
            chunk_size=args.chunk_size,
            verify_latest=(not args.no_verify),
            verify_raw=bool(args.verify_raw),
        )

        if args.pg_restore:
            if not stats.pg_latest_base_tar_gz:
                raise RuntimeError("No PG basebackup found in provided cycles")

            # --promote: boot directly as the live stack so the monitor sees it.
            pg_container = "postgres_live" if args.promote else args.pg_container
            pg_port = 5432 if args.promote else int(args.pg_port)
            if args.promote:
                _log("[PROMOTE]", f"Starting as live stack: container={pg_container} port={pg_port}")

            # Feature 2: pass the pre-merged WAL dir so PG sees a single stream.
            merged_wal = stats.merged_wal_dir or stats.out_pg
            pgdata_dir = "/home/primary/data/transactional/postgres/data" if args.promote else os.path.join(stats.out_root, "pgdata")
            if args.promote:
                _ensure_empty_dir(pgdata_dir)

            _pg_restore_container(
                pgdata=pgdata_dir,
                base_tar_gz=stats.pg_latest_base_tar_gz,
                merged_wal_dir=merged_wal,
                container=pg_container,
                port=pg_port,
                image=args.pg_image,
            )

        if args.mongo_restore:
            if not stats.mongo_latest_archive_gz:
                raise RuntimeError("No Mongo basebackup found in provided cycles")

            # --promote: boot directly as the live stack.
            mongo_container = "mongodb_live" if args.promote else args.mongo_container
            mongo_port = 27017 if args.promote else int(args.mongo_port)
            if args.promote:
                _log("[PROMOTE]", f"Starting as live stack: container={mongo_container} port={mongo_port}")

            mongo_data_dir = "/home/primary/data/transactional/mongo/data" if args.promote else os.path.join(stats.out_root, "mongo_data")
            if args.promote:
                _ensure_empty_dir(mongo_data_dir)

            _mongo_restore_container(
                mongo_data=mongo_data_dir,
                archive_gz=stats.mongo_latest_archive_gz,
                container=mongo_container,
                port=mongo_port,
                image=args.mongo_image,
            )

            # If cycle chain included oplog deltas, replay them into the restored container.
            # restore_cycle() already decompressed deltas into out_mongo as oplog_delta.<cycle_id>.json
            delta_files: list[str] = []
            try:
                for name in os.listdir(stats.out_mongo):
                    m = re.match(r"^oplog_delta\.(\d{8}_\d{6})\.json$", name)
                    if m:
                        delta_files.append(os.path.join(stats.out_mongo, name))
            except Exception:
                delta_files = []
            delta_files.sort()
            if delta_files:
                # Replay oplog into the chosen container
                _mongo_apply_oplog_deltas(container=mongo_container, delta_files=delta_files)

        # Feature 4: bump chain version in state DB after a successful restore.
        # The next backup cycle will write to chain-v(N+1)/, leaving the old
        # poisoned chain to expire via retention / cloud 7-day TTL.
        if getattr(args, "bump_chain_version", False):
            _bump_chain_version(args.state_db)

        if args.promote:
            _log("[PROMOTE]", "Waiting for databases to finish recovery before starting LIVE stack...")
            if args.pg_restore:
                _log("[PROMOTE]", "Waiting for Postgres WAL replay to complete...")
                while True:
                    try:
                        res = subprocess.run(
                            ["docker", "exec", "postgres_live", "psql", "-U", "postgresql", "-d", "postgres", "-tAc", "SELECT pg_is_in_recovery()"],
                            capture_output=True, text=True
                        )
                        if res.stdout.strip() == "f":
                            break
                    except Exception:
                        pass
                    time.sleep(2)
                _log("[PROMOTE]", "Postgres recovery complete.")
            
            _log("[PROMOTE]", "Stopping temporary restore containers...")
            if args.pg_restore:
                _docker_rm("postgres_live")
            if args.mongo_restore:
                _docker_rm("mongodb_live")
            
            _log("[PROMOTE]", "Bringing up full LIVE stack via docker-compose (including generators)...")
            compose_cmds = [
                ["docker", "compose", "-f", "/home/primary/utilities/postgres/postgresql-manifest.yml", "up", "-d", "--force-recreate"],
                ["docker", "compose", "-f", "/home/primary/utilities/mongodb/mongodb-manifest.yml", "up", "-d", "--force-recreate"],
                ["docker", "compose", "-f", "/home/primary/utilities/unstructured/docker-compose.unstructured.yml", "up", "-d", "--force-recreate"],
            ]
            for cmd in compose_cmds:
                _run(cmd, check=True)
            _log("[PROMOTE]", "LIVE stack successfully promoted and fully online!")

    except Exception as e:
        _log("[RESTORE]", f"FAIL: {e}")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
