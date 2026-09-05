import os
import shutil
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


def take_pg_basebackup_into_cycle(
    *,
    conn_state,
    cycle_id: str,
    cycle_tmp: str,
    pg_docker_container: str,
    pg_user: str,
    pg_password: str,
    pg_port: int,
    pg_wal_archive_host_path: str,
    pg_wal_archive_container_path: str,
    checkpoint: str,
    max_rate: str,
    chain_version: str,
    compress_level: int = 5,
) -> dict | None:
    """Create a pg_basebackup (tar+gzip) and place base.tar.gz + pg_wal.tar.gz into cycle_tmp/pg/basebackup/."""

    started = time.perf_counter()

    tmp_name = f".basebackup_tmp_{cycle_id}"
    host_tmp_dir = os.path.join(pg_wal_archive_host_path, tmp_name)
    container_tmp_dir = os.path.join(pg_wal_archive_container_path, tmp_name)

    # Ensure the tmp directory is writable by the container's postgres user.
    # If the orchestrator runs under sudo and creates this on the host, it becomes root-owned
    # and pg_basebackup will fail with permission errors.
    try:
        shutil.rmtree(host_tmp_dir, ignore_errors=True)
    except Exception:
        pass
    try:
        subprocess.run(
            ["docker", "exec", "-u", "postgres", pg_docker_container, "mkdir", "-p", container_tmp_dir],
            check=True,
        )
    except Exception as e:
        print(f"pg         <error>  [BASE] FAIL: could not prepare tmp dir {container_tmp_dir}: {e}")
        return None

    cmd = [
        "docker",
        "exec",
        "-u",
        "postgres",
        "-e",
        f"PGPASSWORD={pg_password}",
        pg_docker_container,
        "pg_basebackup",
        "-h",
        "localhost",
        "-p",
        str(pg_port),
        "-U",
        str(pg_user),
        "-D",
        container_tmp_dir,
        "-F",
        "t",
        "-X",
        "fetch",
        "--checkpoint",
        str(checkpoint),
        "--max-rate",
        str(max_rate),
        "-P",
    ]

    print(f"pg         <info>   [BASE] Starting pg_basebackup -> {container_tmp_dir} (max_rate={max_rate}, checkpoint={checkpoint})")
    try:
        subprocess.run(cmd, check=True)
    except Exception as e:
        # Best-effort cleanup so WAL harvest doesn't choke on a leftover directory.
        try:
            subprocess.run(
                ["docker", "exec", "-u", "postgres", pg_docker_container, "rm", "-rf", container_tmp_dir],
                check=False,
            )
        except Exception:
            pass
        try:
            shutil.rmtree(host_tmp_dir, ignore_errors=True)
        except Exception:
            pass
        print(f"pg         <error>  [BASE] FAIL: pg_basebackup failed: {e}")
        return None

    out_dir = os.path.join(cycle_tmp, "pg", "basebackup")
    os.makedirs(out_dir, exist_ok=True)

    artifacts = []
    copied_bytes = 0
    from .io_utils import zstd_compress_file
    for name in ("base.tar", "pg_wal.tar"):
        src = os.path.join(host_tmp_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(out_dir, name + ".zst")
        print(f"pg         <info>   Compressing {name} -> {dst} (zstd level={compress_level})")
        meta = zstd_compress_file(src, dst, level=compress_level)
        artifacts.append(os.path.relpath(dst, cycle_tmp))
        copied_bytes += meta.get("bytes_out", 0)

    # Cleanup temporary basebackup directory
    try:
        shutil.rmtree(host_tmp_dir, ignore_errors=True)
    except Exception:
        pass

    elapsed = time.perf_counter() - started
    print(f"pg         <good>   [BASE] Done: artifacts={len(artifacts)} bytes={copied_bytes} elapsed={_fmt_elapsed(elapsed)}")

    set_metadata(conn_state, "pg_last_basebackup_cycle", cycle_id)
    try:
        set_metadata(conn_state, "pg_last_basebackup_cycle:n", str(int(get_metadata(conn_state, "cycle_num") or "0")))
        set_metadata(conn_state, "pg_last_basebackup_cycle:chain", chain_version)
    except Exception:
        pass

    return {
        "artifacts": artifacts,
        "format": "tar+gzip",
        "xlog_method": "fetch",
        "checkpoint": str(checkpoint),
        "max_rate": str(max_rate),
        "stored_bytes": copied_bytes,
        "raw_bytes": copied_bytes,
    }


def maybe_take_pg_basebackup(*, cfg, conn_state, cycle_id: str, cycle_tmp: str, chain_version: str) -> dict | None:
    if not getattr(cfg, "pg_basebackup_enable", False):
        print("pg         <info>   [BASE] Disabled (PG_BASEBACKUP_ENABLE=0)")
        return None

    if getattr(cfg, "pg_basebackup_force", False):
        print("pg         <info>   [BASE] Forced (PG_BASEBACKUP_FORCE=1)")
        return take_pg_basebackup_into_cycle(
            conn_state=conn_state,
            cycle_id=cycle_id,
            cycle_tmp=cycle_tmp,
            pg_docker_container=cfg.pg_docker_container,
            pg_user=cfg.pg_user,
            pg_password=cfg.pg_password,
            pg_port=5432,
            pg_wal_archive_host_path=cfg.pg_wal_archive,
            pg_wal_archive_container_path=cfg.pg_wal_archive_container_path,
            checkpoint=cfg.pg_basebackup_checkpoint,
            max_rate=cfg.pg_basebackup_max_rate,
            chain_version=chain_version,
            compress_level=cfg.pg_compress_level,
        )

    if not _should_take_basebackup(
        conn_state=conn_state,
        key_cycle="pg_last_basebackup_cycle",
        every_n=int(getattr(cfg, "pg_basebackup_every_n_cycles", 0) or 0),
        current_chain=chain_version,
    ):
        try:
            cur_n = int(get_metadata(conn_state, "cycle_num") or "0")
            last_n = int(get_metadata(conn_state, "pg_last_basebackup_cycle:n") or "0")
            last_chain = get_metadata(conn_state, "pg_last_basebackup_cycle:chain")
            every_n = int(getattr(cfg, "pg_basebackup_every_n_cycles", 0) or 0)
            print(f"pg         <info>   [BASE] Skipped (not due yet): cycle_num={cur_n} last_base_n={last_n} last_chain={last_chain} cur_chain={chain_version} every_n={every_n}")
        except Exception:
            print("pg         <info>   [BASE] Skipped (not due yet)")
        return None

    return take_pg_basebackup_into_cycle(
        conn_state=conn_state,
        cycle_id=cycle_id,
        cycle_tmp=cycle_tmp,
        pg_docker_container=cfg.pg_docker_container,
        pg_user=cfg.pg_user,
        pg_password=cfg.pg_password,
        pg_port=5432,
        pg_wal_archive_host_path=cfg.pg_wal_archive,
        pg_wal_archive_container_path=cfg.pg_wal_archive_container_path,
        checkpoint=cfg.pg_basebackup_checkpoint,
        max_rate=cfg.pg_basebackup_max_rate,
        chain_version=chain_version,
        compress_level=cfg.pg_compress_level,
    )


__all__ = ["maybe_take_pg_basebackup"]
