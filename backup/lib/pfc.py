import hashlib
import os
import time
import zlib

from .state import get_file_state, set_file_state


def stage_pfc_deltas(
    *,
    conn_state,
    unstructured_dir: str,
    out_dir: str,
    chunk_size: int,
    compress: bool = True,
    compress_level: int = 5,
):
    started = time.perf_counter()
    print("pfc        <info>   Starting PFC chunk scan")
    print(f"pfc        <info>   Source dir: {unstructured_dir}")
    print(f"pfc        <info>   Chunk size: {chunk_size} bytes")
    if compress:
        print(f"pfc        <info>   Delta compression: zstd level={compress_level}")
    else:
        print("pfc        <info>   Delta compression: disabled")

    staged_chunks = 0
    staged_bytes_raw = 0
    staged_bytes_compressed = 0
    total_chunks = 0
    total_bytes_read = 0

    files_total = 0
    files_skipped_unchanged = 0
    files_scanned = 0

    first_staged = []
    staged_entries = []
    scanned_sizes = {}
    current_files = set()
    
    cur = conn_state.cursor()
    cur.execute("SELECT file_path FROM file_state")
    db_files = set(row[0] for row in cur.fetchall())

    for root, _dirs, files in os.walk(unstructured_dir):
        for fname in files:
            files_total += 1
            fpath = os.path.join(root, fname)
            relpath = os.path.relpath(fpath, unstructured_dir)
            current_files.add(relpath)
            try:
                mtime = int(os.path.getmtime(fpath))
                size = int(os.path.getsize(fpath))
            except Exception:
                continue

            prev = get_file_state(conn_state, relpath)
            if prev and int(prev[0]) == mtime and int(prev[1]) == size:
                files_skipped_unchanged += 1
                continue

            files_scanned += 1
            total_bytes_read += size
            scanned_sizes[relpath] = size

            chunk_index = 0
            try:
                with open(fpath, "rb") as fr:
                    while True:
                        chunk = fr.read(chunk_size)
                        if not chunk:
                            break
                        total_chunks += 1
                        sha = hashlib.sha256(chunk).hexdigest()
                        cur.execute(
                            "SELECT sha256 FROM chunk_hashes WHERE file_path=? AND chunk_index=?",
                            (relpath, chunk_index),
                        )
                        row = cur.fetchone()
                        if not row or row[0] != sha:
                            base_name = f"{relpath.replace(os.sep,'_')}_chunk{chunk_index}.bin"
                            if compress:
                                dst_name = base_name + ".zst"
                            else:
                                dst_name = base_name
                            dst_path = os.path.join(out_dir, dst_name)
                            os.makedirs(os.path.dirname(dst_path), exist_ok=True)

                            if compress:
                                import zstandard as zstd
                                cctx = zstd.ZstdCompressor(level=compress_level)
                                payload = cctx.compress(chunk)
                            else:
                                payload = chunk

                            tmp_path = dst_path + ".tmp"
                            with open(tmp_path, "wb") as fw:
                                fw.write(payload)
                            os.replace(tmp_path, dst_path)

                            staged_chunks += 1
                            staged_bytes_raw += len(chunk)
                            staged_bytes_compressed += len(payload)
                            if len(first_staged) < 5:
                                first_staged.append(dst_name)

                            staged_entries.append(
                                {
                                    "artifact": f"unstructured/pfc/{dst_name}",
                                    "source_file": relpath,
                                    "chunk_index": int(chunk_index),
                                    "raw_bytes": int(len(chunk)),
                                    "stored_bytes": int(len(payload)),
                                    "raw_sha256": sha,
                                    "compression": "zstd" if compress else "none",
                                    "compression_level": int(compress_level) if compress else None,
                                }
                            )
                            cur.execute(
                                "REPLACE INTO chunk_hashes(file_path,chunk_index,sha256) VALUES(?,?,?)",
                                (relpath, chunk_index, sha),
                            )
                        chunk_index += 1
                conn_state.commit()
                set_file_state(conn_state, relpath, mtime, size)
            except Exception as e:
                print(f"pfc        <error>  Error scanning {fpath}: {e}")

    deleted_files = list(db_files - current_files)
    for df in deleted_files:
        cur.execute("DELETE FROM file_state WHERE file_path=?", (df,))
        cur.execute("DELETE FROM chunk_hashes WHERE file_path=?", (df,))
    conn_state.commit()

    elapsed = time.perf_counter() - started

    ratio = None
    if staged_bytes_raw > 0:
        ratio = staged_bytes_compressed / staged_bytes_raw

    print(
        f"pfc        <info>   PFC summary: files_total={files_total}, scanned={files_scanned}, "
        f"deleted={len(deleted_files)}, skipped_unchanged={files_skipped_unchanged}, chunks_total={total_chunks}, "
        f"chunks_staged={staged_chunks} (raw={staged_bytes_raw} bytes, stored={staged_bytes_compressed} bytes), "
        f"bytes_scanned={total_bytes_read}, "
        f"elapsed={elapsed:.2f}s"
    )
    if ratio is not None:
        print(f"pfc        <info>   Compression ratio (stored/raw): {ratio:.3f}")
    if first_staged:
        print(f"pfc        <info>   Example staged chunks: {', '.join(first_staged)}")
        if staged_chunks > len(first_staged):
            print(f"pfc        <info>   (and {staged_chunks - len(first_staged)} more chunks)")

    return {
        "files_total": files_total,
        "files_scanned": files_scanned,
        "files_skipped_unchanged": files_skipped_unchanged,
        "files_deleted": len(deleted_files),
        "chunks_total": total_chunks,
        "chunks_staged": staged_chunks,
        "bytes_scanned": total_bytes_read,
        "bytes_staged_raw": staged_bytes_raw,
        "bytes_staged": staged_bytes_compressed,
        "compression": "zlib" if compress else "none",
        "compression_level": int(compress_level) if compress else None,
        "staged_entries": staged_entries,
        "scanned_sizes": scanned_sizes,
        "deleted_files": deleted_files,
        "elapsed_s": elapsed,
    }


__all__ = ["stage_pfc_deltas"]
