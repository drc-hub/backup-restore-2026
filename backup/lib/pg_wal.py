import os
import time
from datetime import datetime
from .io_utils import copy_file_stream
from .state import get_metadata

# PostgreSQL WAL segment size is fixed at 16 MiB (default, compiled in).
_WAL_SEGMENT_BYTES = 16 * 1024 * 1024  # 16 MiB


def _is_wal_segment(name: str) -> bool:
    # Typical WAL segment name: 24 hex chars (timeline+log+seg)
    if not name or len(name) != 24:
        return False
    for ch in name:
        if ch not in "0123456789ABCDEF":
            return False
    return True


def _pad_wal_to_full(path: str) -> int:
    """Pad a WAL file at *path* to exactly _WAL_SEGMENT_BYTES with zero bytes.

    Returns the number of bytes added (0 if already full-sized).
    Raises ValueError if the file is larger than expected.
    """
    size = os.path.getsize(path)
    if size == _WAL_SEGMENT_BYTES:
        return 0
    if size > _WAL_SEGMENT_BYTES:
        raise ValueError(
            f"WAL segment {path} is {size} bytes (larger than expected {_WAL_SEGMENT_BYTES})"
        )
    pad = _WAL_SEGMENT_BYTES - size
    with open(path, "ab") as f:
        # Write in 1 MiB chunks to avoid a single huge allocation.
        remaining = pad
        chunk_size = 1024 * 1024
        zero_chunk = b"\x00" * chunk_size
        while remaining >= chunk_size:
            f.write(zero_chunk)
            remaining -= chunk_size
        if remaining:
            f.write(b"\x00" * remaining)
    return pad


def harvest_wals(
    *,
    conn_state,
    pg_wal_archive: str,
    out_dir: str,
    last_cycle_key: str = "last_cycle_ts",
    tzinfo=None,
    compress_level: int = 5,
):
    started = time.perf_counter()
    print("pg         <info>   Starting WAL harvest")

    last_cycle_ts = get_metadata(conn_state, last_cycle_key)
    try:
        last_cycle_ts = int(last_cycle_ts) if last_cycle_ts else 0
    except Exception:
        last_cycle_ts = 0

    last_cycle_iso = None
    if last_cycle_ts:
        try:
            if tzinfo is not None:
                last_cycle_iso = datetime.fromtimestamp(last_cycle_ts, tzinfo).isoformat()
            else:
                last_cycle_iso = datetime.fromtimestamp(last_cycle_ts).isoformat()
        except Exception:
            last_cycle_iso = None
    if last_cycle_iso:
        print(f"pg         <info>   Last cycle ts: {last_cycle_ts} ({last_cycle_iso})")
    else:
        print(f"pg         <info>   Last cycle ts: {last_cycle_ts}")
    print(f"pg         <info>   WAL archive: {pg_wal_archive}")

    last_wal_fname = (get_metadata(conn_state, "pg_last_wal_fname") or "").strip().upper()
    if last_wal_fname:
        print(f"pg         <info>   Last WAL fname: {last_wal_fname}")
    else:
        print("pg         <info>   Last WAL fname: <none>")

    copied = []
    stored_bytes = 0
    raw_bytes = 0
    skipped = 0
    padded_count = 0
    padded_bytes = 0
    newest_segment_copied = None
    if os.path.isdir(pg_wal_archive):
        for fname in sorted(os.listdir(pg_wal_archive)):
            src = os.path.join(pg_wal_archive, fname)
            if not os.path.isfile(src):
                # Basebackup temp directories (or other non-files) can appear here.
                # Never treat them as WAL artifacts.
                print(f"pg         <info>   Skipping non-file in WAL archive: {src}")
                continue
            try:
                mtime = int(os.path.getmtime(src))
            except Exception:
                continue
            upper = str(fname).upper()
            # For WAL segments, prefer name-based incremental selection.
            if _is_wal_segment(upper) and (not last_wal_fname or upper > last_wal_fname):
                dst = os.path.join(out_dir, fname + ".zst")
                try:
                    size = int(os.path.getsize(src))
                except Exception:
                    size = 0
                print(f"pg         <info>   Compressing WAL {src} -> {dst} (raw={size} bytes)")

                import zstandard as zstd
                cctx = zstd.ZstdCompressor(level=compress_level)
                
                with open(src, "rb") as fr, open(dst, "wb") as fw:
                    with cctx.stream_writer(fw) as compressor:
                        copied_raw = 0
                        while True:
                            chunk = fr.read(1024 * 1024)
                            if not chunk:
                                break
                            compressor.write(chunk)
                            copied_raw += len(chunk)

                        if size < _WAL_SEGMENT_BYTES:
                            added = _WAL_SEGMENT_BYTES - copied_raw
                            if added > 0:
                                print(
                                    f"[PG] Padded WAL {fname}: {copied_raw} -> {_WAL_SEGMENT_BYTES} bytes "
                                    f"(added {added} zero bytes to reach 16 MiB)"
                                )
                                padded_count += 1
                                padded_bytes += added
                                remaining = added
                                zero_chunk = b"\x00" * min(1024 * 1024, remaining)
                                while remaining > 0:
                                    chunk = zero_chunk[:remaining]
                                    compressor.write(chunk)
                                    remaining -= len(chunk)
                        elif size > _WAL_SEGMENT_BYTES:
                            print(
                                f"[PG] WARNING: WAL {fname} is {size} bytes (>{_WAL_SEGMENT_BYTES}); "
                                "not a standard 16 MiB segment — compressed as-is"
                            )

                copied.append(dst)
                stored_bytes += os.path.getsize(dst)
                raw_bytes += copied_raw + added if size < _WAL_SEGMENT_BYTES else size
                newest_segment_copied = upper
            elif mtime > last_cycle_ts:
                # Non-segment artifacts (e.g., .backup history) are still copied by mtime.
                dst = os.path.join(out_dir, fname + ".zst")
                try:
                    size = int(os.path.getsize(src))
                except Exception:
                    size = 0
                print(f"pg         <info>   Compressing WAL {src} -> {dst} (raw={size} bytes)")
                
                import zstandard as zstd
                cctx = zstd.ZstdCompressor(level=compress_level)
                with open(src, "rb") as fr, open(dst, "wb") as fw:
                    with cctx.stream_writer(fw) as compressor:
                        while True:
                            chunk = fr.read(1024 * 1024)
                            if not chunk:
                                break
                            compressor.write(chunk)

                copied.append(dst)
                stored_bytes += os.path.getsize(dst)
                raw_bytes += size
            else:
                skipped += 1

    elapsed = time.perf_counter() - started
    print(
        f"[PG] WAL summary: copied={len(copied)} files (raw={raw_bytes} bytes, stored={stored_bytes} bytes), "
        f"padded={padded_count} segments (+{padded_bytes} bytes), "
        f"skipped={skipped} older-or-equal, elapsed={elapsed:.2f}s"
    )

    # record that we harvested (even if 0) via a monotonic timestamp key; actual state update happens in runner
    return {
        "copied_paths": copied,
        "copied_files": len(copied),
        "raw_bytes": raw_bytes,
        "stored_bytes": stored_bytes,
        "padded_count": padded_count,
        "padded_bytes": padded_bytes,
        "skipped_files": skipped,
        "last_cycle_ts": last_cycle_ts,
        "newest_segment_copied": newest_segment_copied,
        "elapsed_s": elapsed,
    }


__all__ = ["harvest_wals"]
