import os
import subprocess
import time

from .state import get_metadata, set_metadata


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    mins = int(seconds // 60)
    secs = seconds - mins * 60
    return f"{mins}m{secs:.1f}s"


def _should_take_basebackup(*, conn_state, key_cycle: str, every_n: int, current_chain: str) -> bool:
    last = get_metadata(conn_state, key_cycle)
    if not last:
        return True
    last_chain = get_metadata(conn_state, key_cycle + ":chain")
    if last_chain != current_chain:
        return True
    if every_n and every_n > 0:
        try:
            last_n = int(get_metadata(conn_state, key_cycle + ":n") or "0")
            cur_n = int(get_metadata(conn_state, "cycle_num") or "0")
            return (cur_n - last_n) >= int(every_n)
        except Exception:
            return False
    return False


def take_mongo_basebackup_into_cycle(
    *,
    conn_state,
    cycle_id: str,
    cycle_tmp: str,
    mongo_docker_container: str,
    mongo_uri: str,
    db_name: str,
    include_oplog: bool,
    chain_version: str,
) -> dict | None:
    """Create a mongodump archive (gzip) and place it into cycle_tmp/mongo/basebackup/mongodump.archive.gz."""

    started = time.perf_counter()

    out_dir = os.path.join(cycle_tmp, "mongo", "basebackup")
    os.makedirs(out_dir, exist_ok=True)

    out_path = os.path.join(out_dir, "mongodump.archive.gz")
    rel_artifact = os.path.relpath(out_path, cycle_tmp)

    # NOTE: mongodump --oplog is only supported for full dumps (no --db / no --collection).
    # If oplog is enabled, ignore db_name and perform a full dump.
    effective_db = (db_name or "").strip()
    if include_oplog and effective_db:
        print(f"mongo      <info>   [BASE] Note: ignoring db={effective_db} because --oplog requires full dump")
        effective_db = ""

    dump_uri = mongo_uri.replace("&directConnection=true", "").replace("directConnection=true", "")
    cmd = [
        "docker",
        "exec",
        "-i",
        mongo_docker_container,
        "mongodump",
        f"--uri={dump_uri}",
        "--archive",
        "--gzip",
    ]
    if effective_db:
        cmd.append(f"--db={effective_db}")
    if include_oplog:
        cmd.append("--oplog")

    db_label = effective_db if effective_db else "<all>"
    print(f"mongo      <info>   [BASE] Starting mongodump (db={db_label}, oplog={bool(include_oplog)}) -> {rel_artifact}")

    tmp_path = out_path + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            subprocess.run(cmd, stdout=f, check=True)
        os.replace(tmp_path, out_path)
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        print(f"mongo      <error>  [BASE] FAIL: mongodump failed: {e}")
        return None

    try:
        stored_bytes = int(os.path.getsize(out_path))
    except Exception:
        stored_bytes = 0

    elapsed = time.perf_counter() - started
    print(f"mongo      <good>   [BASE] Done: stored_bytes={stored_bytes} elapsed={_fmt_elapsed(elapsed)}")

    set_metadata(conn_state, "mongo_last_basebackup_cycle", cycle_id)
    try:
        set_metadata(conn_state, "mongo_last_basebackup_cycle:n", str(int(get_metadata(conn_state, "cycle_num") or "0")))
        set_metadata(conn_state, "mongo_last_basebackup_cycle:chain", chain_version)
    except Exception:
        pass

    return {
        "artifact": rel_artifact,
        "format": "mongodump-archive+gzip",
        "db": db_name,
        "oplog": bool(include_oplog),
        "stored_bytes": stored_bytes,
        "raw_bytes": stored_bytes,
    }


def maybe_take_mongo_basebackup(*, cfg, conn_state, cycle_id: str, cycle_tmp: str, mongo_uri: str, chain_version: str) -> dict | None:
    if not getattr(cfg, "mongo_basebackup_enable", False):
        print("mongo      <info>   [BASE] Disabled (MONGO_BASEBACKUP_ENABLE=0)")
        return None

    if getattr(cfg, "mongo_basebackup_force", False):
        print("mongo      <info>   [BASE] Forced (MONGO_BASEBACKUP_FORCE=1)")
        return take_mongo_basebackup_into_cycle(
            conn_state=conn_state,
            cycle_id=cycle_id,
            cycle_tmp=cycle_tmp,
            mongo_docker_container=cfg.mongo_docker_container,
            mongo_uri=mongo_uri,
            db_name=cfg.mongo_basebackup_db,
            include_oplog=bool(cfg.mongo_basebackup_oplog),
            chain_version=chain_version,
        )

    if not _should_take_basebackup(
        conn_state=conn_state,
        key_cycle="mongo_last_basebackup_cycle",
        every_n=int(getattr(cfg, "mongo_basebackup_every_n_cycles", 0) or 0),
        current_chain=chain_version,
    ):
        try:
            cur_n = int(get_metadata(conn_state, "cycle_num") or "0")
            last_n = int(get_metadata(conn_state, "mongo_last_basebackup_cycle:n") or "0")
            last_chain = get_metadata(conn_state, "mongo_last_basebackup_cycle:chain")
            every_n = int(getattr(cfg, "mongo_basebackup_every_n_cycles", 0) or 0)
            print(f"mongo      <info>   [BASE] Skipped (not due yet): cycle_num={cur_n} last_base_n={last_n} last_chain={last_chain} cur_chain={chain_version} every_n={every_n}")
        except Exception:
            print("mongo      <info>   [BASE] Skipped (not due yet)")
        return None

    return take_mongo_basebackup_into_cycle(
        conn_state=conn_state,
        cycle_id=cycle_id,
        cycle_tmp=cycle_tmp,
        mongo_docker_container=cfg.mongo_docker_container,
        mongo_uri=mongo_uri,
        db_name=cfg.mongo_basebackup_db,
        include_oplog=bool(cfg.mongo_basebackup_oplog),
        chain_version=chain_version,
    )


__all__ = ["maybe_take_mongo_basebackup"]
