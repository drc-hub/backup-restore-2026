import os
import shutil
import subprocess
import time
from datetime import datetime

from .config import BackupConfig, jakarta_tz
from .state import open_state_db, set_metadata, get_metadata
from .pg_wal import harvest_wals
from .pg_basebackup import maybe_take_pg_basebackup
from .mongo_oplog import (
    build_default_mongo_uri,
    normalize_mongo_uri,
    connect_mongo,
    extract_oplog_delta,
)
from .mongo_basebackup import maybe_take_mongo_basebackup
from .pfc import stage_pfc_deltas
from .manifest import build_manifest, write_checksums_file
from .retention import enforce_retention, dir_size_bytes
from .fixtures import seed_fixtures
from .verify import verify_cycle
from .transfer import rsync_to_all_targets
from .telemetry import BackupTelemetry

try:
    import psycopg2
except Exception:
    psycopg2 = None


# ---------------------------------------------------------------------------
# Feature 4: chain version helpers
# ---------------------------------------------------------------------------

def _resolve_chain_version(conn_state, cfg: BackupConfig) -> str:
    """Return the active chain version string (e.g. 'chain-v1').

    Priority:
    1. Value stored in state DB under key 'chain_version'
    2. cfg.chain_version_init  (from env CHAIN_VERSION_INIT, default 'chain-v1')
    """
    stored = (get_metadata(conn_state, "chain_version") or "").strip()
    if stored:
        return stored
    # First run — persist the initial version.
    set_metadata(conn_state, "chain_version", cfg.chain_version_init)
    return cfg.chain_version_init


def resolve_versioned_cycles_root(conn_state, cfg: BackupConfig) -> str:
    """Return cycles_root/chain_version — the directory where this chain's cycles live."""
    chain_ver = _resolve_chain_version(conn_state, cfg)
    return os.path.join(cfg.cycles_root, chain_ver)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _isatty() -> bool:
    try:
        return bool(getattr(__import__("sys").stdout, "isatty")())
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


def _tag_good() -> str:
    return _c("<good>", "32")


def _tag_bad() -> str:
    return _c("<error>", "31")


def _tag_warn() -> str:
    return _c("<warn>", "33")


def _tag_info() -> str:
    return _c("<info>", "36")


def _log(scope: str, tag: str, msg: str) -> None:
    # Strip ANSI color codes for file logging
    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean_tag = ansi_escape.sub('', tag)
    
    # ensure tidy formatting
    # pad clean_tag to 8 chars then replace in tag to keep color
    colored_tag = tag.replace(clean_tag, f"{clean_tag:<8}") if clean_tag in tag else tag
    term_line = f"{scope:<10} {colored_tag} {msg}"
    print(term_line)
    
    try:
        # Jakarta tz
        import datetime
        tz = datetime.timezone(datetime.timedelta(hours=7))
        ts = datetime.datetime.now(tz).replace(microsecond=0).isoformat()
        file_line = f"[{ts}] {scope:<10} {clean_tag:<8} {msg}\n"
        LOG_FILE = "/home/primary/utilities/backup/backup.log"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(file_line)
    except Exception:
        pass


def _clean_dir(path: str) -> None:
    if not os.path.isdir(path):
        return
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            try:
                os.remove(full)
            except Exception:
                pass


def _fmt_bytes(num: int) -> str:
    try:
        n = float(num)
    except Exception:
        return f"{num} B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    unit = 0
    while n >= 1024.0 and unit < len(units) - 1:
        n /= 1024.0
        unit += 1
    if unit == 0:
        return f"{int(n)} {units[unit]}"
    return f"{n:.2f} {units[unit]}"


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    mins = int(seconds // 60)
    secs = seconds - mins * 60
    return f"{mins}m{secs:.1f}s"


def run_cycle(cfg: BackupConfig) -> str:
    cycle_started = time.perf_counter()
    tz = jakarta_tz()
    now = datetime.now(tz)
    cycle_id = now.strftime("%Y%m%d_%H%M%S")

    _log("main", _tag_info(), f"starting cycle={cycle_id} at={now.isoformat()}")
    _log("main", _tag_info(), f"output_root={cfg.cycles_root}")
    _log("main", _tag_info(), f"staging_root={cfg.staging_root}")
    _log("main", _tag_info(), f"state_db={cfg.state_db}")
    _log(
        "main",
        _tag_info(),
        f"retention max_cycles={cfg.retention_max_cycles} max_bytes={_fmt_bytes(cfg.retention_max_bytes)} cycle_cap={_fmt_bytes(cfg.cycle_max_bytes)}",
    )

    os.makedirs(os.path.dirname(cfg.state_db), exist_ok=True)
    os.makedirs(cfg.staging_root, exist_ok=True)
    os.makedirs(cfg.cycles_root, exist_ok=True)

    conn_state = open_state_db(cfg.state_db)

    # Automatically bump chain version every 7 cycles
    try:
        chain_cycle_count = int(get_metadata(conn_state, "chain_cycle_count") or "0")
    except Exception:
        chain_cycle_count = 0
        
    if chain_cycle_count >= 7:
        stored_chain = get_metadata(conn_state, "chain_version") or cfg.chain_version_init
        if stored_chain.startswith("chain-v"):
            try:
                ver = int(stored_chain[7:])
                new_chain = f"chain-v{ver + 1}"
            except Exception:
                new_chain = "chain-v2"
        else:
            new_chain = stored_chain + "-new"
        set_metadata(conn_state, "chain_version", new_chain)
        chain_cycle_count = 0

    chain_cycle_count += 1
    set_metadata(conn_state, "chain_cycle_count", str(chain_cycle_count))

    # -----------------------------------------------------------------------
    # Feature 4: resolve / persist chain version, build versioned cycles_root
    # -----------------------------------------------------------------------
    chain_version = _resolve_chain_version(conn_state, cfg)
    versioned_cycles_root = os.path.join(cfg.cycles_root, chain_version)
    os.makedirs(versioned_cycles_root, exist_ok=True)
    _log("main", _tag_info(), f"chain_version={chain_version} versioned_cycles_root={versioned_cycles_root}")

    cycle_tmp = os.path.join(cfg.staging_root, f"cycle_{cycle_id}.tmp")
    cycle_out = os.path.join(versioned_cycles_root, cycle_id)

    telemetry = BackupTelemetry()
    telemetry.start_cycle(cycle_id, chain_version)

    if os.path.exists(cycle_tmp):
        shutil.rmtree(cycle_tmp, ignore_errors=True)

    os.makedirs(os.path.join(cycle_tmp, "pg"), exist_ok=True)
    os.makedirs(os.path.join(cycle_tmp, "mongo"), exist_ok=True)
    os.makedirs(os.path.join(cycle_tmp, "unstructured", "pfc"), exist_ok=True)

    # Monotonic cycle counter (used for basebackup scheduling)
    try:
        cycle_num = int(get_metadata(conn_state, "cycle_num") or "0") + 1
    except Exception:
        cycle_num = 1
    set_metadata(conn_state, "cycle_num", str(cycle_num))

    last_cycle_ts = get_metadata(conn_state, "last_cycle_ts")
    try:
        last_cycle_ts_int = int(last_cycle_ts) if last_cycle_ts else 0
    except Exception:
        last_cycle_ts_int = 0
    if last_cycle_ts_int:
        try:
            last_cycle_iso = datetime.fromtimestamp(last_cycle_ts_int, tz).isoformat()
        except Exception:
            last_cycle_iso = None
        if last_cycle_iso:
            _log("main", _tag_info(), f"last_cycle_ts={last_cycle_ts_int} ({last_cycle_iso})")
        else:
            _log("main", _tag_info(), f"last_cycle_ts={last_cycle_ts_int}")
    else:
        _log("main", _tag_info(), "last_cycle_ts=<none>")

    pg_conn = None
    if cfg.pg_enable_lsn and psycopg2 is not None:
        try:
            pg_conn = psycopg2.connect(
                dbname=cfg.pg_db,
                user=cfg.pg_user,
                password=cfg.pg_password,
                host=cfg.pg_host,
            )
            pg_conn.autocommit = True
        except Exception as e:
            _log("pg", _tag_bad(), f"connect failed (LSN disabled): {e}")
            pg_conn = None

    mongo_client = None
    mongo_uri = None
    mongo_ts_str = None
    mongo_delta_artifact = None
    mongo_delta_meta = None
    try:
        raw_uri = cfg.mongo_uri
        if raw_uri:
            mongo_uri = normalize_mongo_uri(raw_uri)
        else:
            mongo_uri = build_default_mongo_uri(
                host=cfg.mongo_host,
                port=cfg.mongo_port,
                user=cfg.mongo_user,
                password=cfg.mongo_password,
                auth_source=cfg.mongo_auth_source,
            )

        mongo_client = connect_mongo(
            mongo_uri=mongo_uri,
            server_selection_timeout_ms=cfg.mongo_server_selection_timeout_ms,
            connect_timeout_ms=cfg.mongo_connect_timeout_ms,
        )
    except Exception as e:
        _log("mongo", _tag_bad(), f"connect failed: {e}")
        mongo_client = None

    try:
        # 0) Optional base backups (for PITR)
        t0 = time.perf_counter()
        pg_base_meta = maybe_take_pg_basebackup(cfg=cfg, conn_state=conn_state, cycle_id=cycle_id, cycle_tmp=cycle_tmp, chain_version=chain_version)
        telemetry.record_timing("pg_basebackup_s", time.perf_counter() - t0)
        if pg_base_meta:
            telemetry.record_size("pg_basebackup_raw_bytes", pg_base_meta.get("raw_bytes", 0))

        mongo_base_meta = None
        if mongo_uri:
            t0 = time.perf_counter()
            mongo_base_meta = maybe_take_mongo_basebackup(
                cfg=cfg,
                conn_state=conn_state,
                cycle_id=cycle_id,
                cycle_tmp=cycle_tmp,
                mongo_uri=mongo_uri,
                chain_version=chain_version,
            )
            telemetry.record_timing("mongo_basebackup_s", time.perf_counter() - t0)
            if mongo_base_meta:
                telemetry.record_size("mongo_basebackup_raw_bytes", mongo_base_meta.get("raw_bytes", 0))

        unstructured_base_meta = None
        unstructured_last_chain = get_metadata(conn_state, "unstructured_last_basebackup_cycle:chain")
        if unstructured_last_chain != chain_version:
            _log("pfc", _tag_info(), f"chain changed (was {unstructured_last_chain}, now {chain_version}), taking unstructured basebackup")
            t0 = time.perf_counter()
            base_out = os.path.join(cycle_tmp, "unstructured", "basebackup")
            os.makedirs(base_out, exist_ok=True)
            tar_path = os.path.join(base_out, "base.tar.gz")
            try:
                res = subprocess.run(
                    ["tar", "-czf", tar_path, "-C", cfg.unstructured_dir, "."],
                    check=False,
                )
                if res.returncode not in (0, 1):
                    raise RuntimeError(f"tar returned non-zero exit status {res.returncode}")
                b_size = os.path.getsize(tar_path)
                unstructured_base_meta = {
                    "artifact": os.path.relpath(tar_path, cycle_tmp),
                    "format": "tar+gzip",
                    "stored_bytes": b_size,
                }
                set_metadata(conn_state, "unstructured_last_basebackup_cycle:chain", chain_version)
                telemetry.record_size("unstructured_basebackup_stored_bytes", b_size)
                telemetry.record_size("unstr_base_size", b_size)
                elapsed = time.perf_counter() - t0
                telemetry.record_timing("unstr_base_s", elapsed)
                _log("pfc", _tag_good(), f"basebackup done bytes={_fmt_bytes(b_size)} elapsed={_fmt_elapsed(elapsed)}")
            except Exception as e:
                _log("pfc", _tag_bad(), f"basebackup failed: {e}")

        # 1) Stage deltas
        t0 = time.perf_counter()
        pg_stats = harvest_wals(
            conn_state=conn_state,
            pg_wal_archive=cfg.pg_wal_archive,
            out_dir=os.path.join(cycle_tmp, "pg"),
            tzinfo=tz,
            compress_level=cfg.pg_compress_level,
        )
        telemetry.record_timing("pg_wal_extract_s", time.perf_counter() - t0)
        telemetry.record_size("pg_wal_raw_bytes", pg_stats.get("raw_bytes", 0))
        telemetry.record_size("pg_wal_stored_bytes", pg_stats.get("stored_bytes", 0))
        telemetry.record_size("wal_file_count", pg_stats.get("copied_files", 0))
        if pg_stats.get("copied_files", 0) == 0:
            _log("pg", _tag_info(), "no new WAL files")

        t0 = time.perf_counter()
        oplog_path = extract_oplog_delta(
            conn_state=conn_state,
            mongo_client=mongo_client,
            out_path=os.path.join(cycle_tmp, "mongo", "oplog_delta.json"),
        )
        telemetry.record_timing("mongo_oplog_extract_s", time.perf_counter() - t0)
        if oplog_path:
            # capture last stored ts string for manifest
            mongo_ts_str = get_metadata(conn_state, "mongo_last_ts")

            if cfg.mongo_compress:
                comp_path = oplog_path + ".zst"
                comp_started = time.perf_counter()
                from .io_utils import zstd_compress_file
                meta = zstd_compress_file(
                    oplog_path,
                    comp_path,
                    level=cfg.mongo_compress_level,
                    compute_raw_sha256=True,
                )
                try:
                    os.remove(oplog_path)
                except Exception:
                    pass

                ratio = None
                if meta.get("bytes_in", 0) > 0:
                    ratio = float(meta.get("bytes_out", 0)) / float(meta.get("bytes_in", 1))
                elapsed = time.perf_counter() - comp_started
                telemetry.record_timing("mongo_oplog_compress_s", elapsed)
                telemetry.record_size("mongo_oplog_raw_bytes", meta.get("bytes_in", 0))
                telemetry.record_size("mongo_oplog_stored_bytes", meta.get("bytes_out", 0))
                if ratio is not None:
                    _log(
                        "mongo",
                        _tag_good(),
                        f"oplog compressed raw={_fmt_bytes(meta.get('bytes_in', 0))} stored={_fmt_bytes(meta.get('bytes_out', 0))} ratio={ratio:.3f} elapsed={_fmt_elapsed(elapsed)}",
                    )
                else:
                    _log(
                        "mongo",
                        _tag_good(),
                        f"oplog compressed stored={_fmt_bytes(meta.get('bytes_out', 0))} elapsed={_fmt_elapsed(elapsed)}",
                    )

                mongo_delta_artifact = os.path.relpath(comp_path, cycle_tmp)
                mongo_delta_meta = {
                    "artifact": mongo_delta_artifact,
                    "compression": "zstd",
                    "compression_level": int(cfg.mongo_compress_level),
                    "raw_sha256": meta.get("raw_sha256"),
                    "raw_bytes": int(meta.get("bytes_in", 0)),
                    "stored_bytes": int(meta.get("bytes_out", 0)),
                }
            else:
                mongo_delta_artifact = os.path.relpath(oplog_path, cycle_tmp)
                mongo_delta_meta = {
                    "artifact": mongo_delta_artifact,
                    "compression": "none",
                }

        fixtures_meta = None
        if cfg.fixtures_enable:
            fx = seed_fixtures(
                fixtures_dir=cfg.fixtures_dir,
                unstructured_dir=cfg.unstructured_dir,
                target_subdir=cfg.fixtures_target_subdir,
            )
            fixtures_meta = {
                "enabled": bool(fx.get("enabled")),
                "target_subdir": cfg.fixtures_target_subdir,
                "seed": fx,
            }
            if fx.get("enabled"):
                _log(
                    "fixtures",
                    _tag_good(),
                    f"seeded target={fx.get('target_root')} copied={fx.get('files_copied')} skipped={fx.get('files_skipped')} bytes={fx.get('bytes_copied')} elapsed={_fmt_elapsed(fx.get('elapsed_s', 0.0))}",
                )
            else:
                _log("fixtures", _tag_info(), f"skipped reason={fx.get('reason')}")

        t0 = time.perf_counter()
        pfc_stats = stage_pfc_deltas(
            conn_state=conn_state,
            unstructured_dir=cfg.unstructured_dir,
            out_dir=os.path.join(cycle_tmp, "unstructured", "pfc"),
            chunk_size=cfg.chunk_size,
            compress=cfg.pfc_compress,
            compress_level=cfg.pfc_compress_level,
        )
        telemetry.record_timing("unstructured_chunk_s", time.perf_counter() - t0)
        telemetry.record_timing("unstructured_delta_compress_s", pfc_stats.get("compress_elapsed_s", 0.0))
        telemetry.record_size("unstructured_raw_bytes", pfc_stats.get("bytes_staged_raw", 0))
        telemetry.record_size("unstructured_stored_bytes", pfc_stats.get("bytes_staged", 0))
        telemetry.record_size("pfc_chunk_count", pfc_stats.get("chunks_staged", 0))
        if pfc_stats.get("chunks_staged", 0) == 0:
            _log("pfc", _tag_info(), "no chunk deltas")

        # 2) Safety cap (avoid cycle blowing disk)
        cap_started = time.perf_counter()
        size_now = dir_size_bytes(cycle_tmp)
        _log("main", _tag_info(), f"staging_size={_fmt_bytes(size_now)} checked_in={_fmt_elapsed(time.perf_counter() - cap_started)}")
        if size_now > cfg.cycle_max_bytes:
            raise RuntimeError(f"Cycle size {size_now} exceeds CYCLE_MAX_BYTES={cfg.cycle_max_bytes}")

        # 3) Checksums + manifest
        checksums_path = write_checksums_file(root_dir=cycle_tmp)
        extras = {
            "pfc": {
                "compression": pfc_stats.get("compression"),
                "compression_level": pfc_stats.get("compression_level"),
                "deltas": pfc_stats.get("staged_entries", []),
                "scanned_sizes": pfc_stats.get("scanned_sizes", {}),
                "deleted_files": pfc_stats.get("deleted_files", []),
            },
            "chain_version": chain_version,
        }
        if unstructured_base_meta:
            extras["unstructured_basebackup"] = unstructured_base_meta
        if pg_base_meta is not None:
            extras["pg_basebackup"] = pg_base_meta
        if mongo_base_meta is not None:
            extras["mongo_basebackup"] = mongo_base_meta
        if fixtures_meta is not None:
            extras["fixtures"] = fixtures_meta
        if mongo_delta_meta is not None:
            extras["mongo"] = {
                "last_ts": mongo_ts_str,
                "delta": mongo_delta_meta,
            }

        _ = build_manifest(
            root_dir=cycle_tmp,
            timestamp=now,
            pg_last_lsn=get_metadata(conn_state, "pg_last_lsn"),
            mongo_last_ts=mongo_ts_str,
            extras=extras,
        )
        if checksums_path:
            _log("main", _tag_good(), f"wrote checksums={checksums_path}")

        # 4) Finalize (atomic-ish)
        finalize_started = time.perf_counter()
        if os.path.exists(cycle_out):
            raise RuntimeError(f"Cycle output already exists: {cycle_out}")
        os.makedirs(versioned_cycles_root, exist_ok=True)
        os.rename(cycle_tmp, cycle_out)
        _log("main", _tag_good(), f"finalized dir elapsed={_fmt_elapsed(time.perf_counter() - finalize_started)}")

        # 4b) Primary-side verification (before state update / retention / transfer)
        if cfg.transfer_verify:
            verify_started = time.perf_counter()
            ok, errors = verify_cycle(cycle_out, verify_raw=cfg.transfer_verify_raw)
            if not ok:
                for err in errors[:20]:
                    _log("verify", _tag_bad(), err)
                if len(errors) > 20:
                    _log("verify", _tag_bad(), f"(and {len(errors) - 20} more)")
                raise RuntimeError("Primary-side cycle verification failed")
            elapsed_verify = time.perf_counter() - verify_started
            telemetry.record_timing("verify_s", elapsed_verify)
            _log("verify", _tag_good(), f"ok elapsed={_fmt_elapsed(elapsed_verify)}")

        # 5) Update state only after finalize
        set_metadata(conn_state, "last_cycle_ts", int(time.time()))
        try:
            newest_seg = pg_stats.get("newest_segment_copied")
            if newest_seg:
                set_metadata(conn_state, "pg_last_wal_fname", str(newest_seg))
                _log("pg", _tag_info(), f"state pg_last_wal_fname={newest_seg}")
        except Exception:
            pass

        # 6) Retention — apply to the versioned chain dir only.
        retention_started = time.perf_counter()
        enforce_retention(
            cycles_root=versioned_cycles_root,
            max_cycles=cfg.retention_max_cycles,
            max_bytes=cfg.retention_max_bytes,
        )
        elapsed_retention = time.perf_counter() - retention_started
        telemetry.record_timing("retention_s", elapsed_retention)
        _log("main", _tag_info(), f"retention elapsed={_fmt_elapsed(elapsed_retention)}")

        # Calculate final local dir size before transfer potentially deletes it
        try:
            out_size = dir_size_bytes(cycle_out)
        except Exception:
            out_size = 0

        # 7) Feature 3: Transfer finalized cycle to ALL configured destinations.
        #    Feature 4: dest_name preserves chain version so the backup VM's
        #    incoming/ directory mirrors the same chain-vN/<cycle_id> layout:
        #      incoming/chain-v1/20260601_170654/
        #    This keeps old poisoned chains isolated on the remote side too.
        if cfg.transfer_enable:
            transfer_started = time.perf_counter()
            targets = list(cfg.recovery_rsync_targets)
            if targets:
                # dest_name = "chain-v1/20260601_170654" → written under incoming/chain-v1/cycle_id/
                chain_dest_name = f"{chain_version}/{cycle_id}"
                _log("transfer", _tag_info(),
                     f"pushing to {len(targets)} destination(s) as {chain_dest_name!r}: {targets}")
                try:
                    dest_dirs = rsync_to_all_targets(
                        src_dir=cycle_out,
                        targets=targets,
                        dest_name=chain_dest_name,
                        ssh_port=cfg.recovery_ssh_port,
                        ssh_key=cfg.recovery_ssh_key,
                    )
                    elapsed_transfer = time.perf_counter() - transfer_started
                    telemetry.record_timing("transfer_s", elapsed_transfer)
                    _log(
                        "transfer",
                        _tag_good(),
                        f"all {len(dest_dirs)} destination(s) OK elapsed={_fmt_elapsed(elapsed_transfer)}",
                    )
                    _log("main", _tag_info(), f"Deleting local cycle since it was transferred: {cycle_out}")
                    shutil.rmtree(cycle_out, ignore_errors=True)
                except Exception as e:
                    raise RuntimeError(f"Transfer failed: {e}")
            else:
                _log("transfer", _tag_bad(), "TRANSFER_ENABLE=1 but no targets configured (RECOVERY_RSYNC_TARGETS is empty)")

        # Summary — finalize telemetry

        pg_raw = pg_stats.get('copied_bytes', 0) + (pg_base_meta.get('raw_bytes', 0) if pg_base_meta else 0)
        pg_comp = pg_stats.get('copied_bytes', 0) + (pg_base_meta.get('stored_bytes', 0) if pg_base_meta else 0)

        mongo_delta_raw = mongo_delta_meta.get("raw_bytes", 0) if mongo_delta_meta else 0
        if not mongo_delta_raw and mongo_delta_meta and mongo_delta_meta.get("compression") == "none":
            try:
                mongo_delta_raw = os.path.getsize(os.path.join(cycle_tmp, mongo_delta_meta["artifact"]))
            except Exception:
                mongo_delta_raw = 0
        mongo_delta_comp = mongo_delta_meta.get("stored_bytes", mongo_delta_raw) if mongo_delta_meta else 0

        mongo_raw = mongo_delta_raw + (mongo_base_meta.get('raw_bytes', 0) if mongo_base_meta else 0)
        mongo_comp = mongo_delta_comp + (mongo_base_meta.get('stored_bytes', 0) if mongo_base_meta else 0)

        unstr_base_s_bytes = unstructured_base_meta.get("stored_bytes", 0) if unstructured_base_meta else 0
        unstr_raw = pfc_stats.get("bytes_staged_raw", 0) + unstr_base_s_bytes
        unstr_comp = pfc_stats.get("bytes_staged", 0) + unstr_base_s_bytes

        telemetry.record_size("pg_raw_size", pg_raw)
        telemetry.record_size("pg_compressed_size", pg_comp)
        telemetry.record_size("mongo_raw_size", mongo_raw)
        telemetry.record_size("mongo_compressed_size", mongo_comp)
        telemetry.record_size("unstr_raw_size", unstr_raw)
        telemetry.record_size("unstr_compressed_size", unstr_comp)
        
        telemetry.record_size("cycle_raw_total_size", pg_raw + mongo_raw + unstr_raw)
        telemetry.record_size("cycle_final_transfered_size", out_size)
        telemetry.record_size("cycle_total_stored_bytes", out_size)

        _log(
            "main", _tag_info(),
            f"cycle_summary: wal_files={pg_stats.get('copied_files')} "
            f"({_fmt_bytes(pg_stats.get('copied_bytes', 0))}), "
            f"pfc_chunks={pfc_stats.get('chunks_staged')} "
            f"(stored={_fmt_bytes(pfc_stats.get('bytes_staged', 0))}, "
            f"raw={_fmt_bytes(pfc_stats.get('bytes_staged_raw', 0))}), "
            f"cycle_stored={_fmt_bytes(out_size)} chain={chain_version}",
        )

        total_elapsed = time.perf_counter() - cycle_started
        telemetry.finalize(total_elapsed)
        _log("main", _tag_good(), f"completed cycle={cycle_id} chain={chain_version} elapsed={_fmt_elapsed(total_elapsed)}")
        return cycle_out

    finally:
        try:
            if pg_conn:
                pg_conn.close()
        except Exception:
            pass
        try:
            conn_state.close()
        except Exception:
            pass
        try:
            if mongo_client:
                mongo_client.close()
        except Exception:
            pass
        # If finalize failed, clean temp
        if os.path.exists(cycle_tmp):
            shutil.rmtree(cycle_tmp, ignore_errors=True)


__all__ = ["run_cycle", "resolve_versioned_cycles_root"]
