import json
import os
import time
from datetime import datetime

from .io_utils import compute_sha256


def build_manifest(*, root_dir: str, timestamp: datetime, pg_last_lsn, mongo_last_ts, extras: dict | None = None) -> str:
    started = time.perf_counter()
    print("manifest   <info>   Building manifest")
    manifest = {
        "cycle_timestamp": timestamp.isoformat(),
        "pg_last_lsn": str(pg_last_lsn) if pg_last_lsn is not None else None,
        "mongo_last_ts": mongo_last_ts,
        "files": {},
    }

    if extras:
        # Keep extras separate from the hashed file map.
        manifest.update(extras)

    files_count = 0
    bytes_count = 0
    for walk_root, _dirs, files in os.walk(root_dir):
        for fname in files:
            path = os.path.join(walk_root, fname)
            rel = os.path.relpath(path, root_dir)
            try:
                bytes_count += int(os.path.getsize(path))
            except Exception:
                pass
            manifest["files"][rel] = compute_sha256(path)
            files_count += 1

    manifest_path = os.path.join(root_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as mf:
        json.dump(manifest, mf, indent=2)
    print(f"manifest   <info>   Wrote {manifest_path}")
    elapsed = time.perf_counter() - started
    print(f"manifest   <info>   Manifest summary: files_hashed={files_count}, bytes_hashed={bytes_count}, elapsed={elapsed:.2f}s")
    return manifest_path


def write_checksums_file(*, root_dir: str, out_name: str = "checksums.sha256") -> str:
    started = time.perf_counter()
    out_path = os.path.join(root_dir, out_name)
    entries = []
    bytes_count = 0
    for walk_root, _dirs, files in os.walk(root_dir):
        for fname in files:
            if fname == out_name:
                continue
            path = os.path.join(walk_root, fname)
            rel = os.path.relpath(path, root_dir)
            try:
                bytes_count += int(os.path.getsize(path))
            except Exception:
                pass
            entries.append((rel, compute_sha256(path)))

    entries.sort(key=lambda x: x[0])
    with open(out_path, "w", encoding="utf-8") as f:
        for rel, sha in entries:
            f.write(f"{sha}  {rel}\n")
    elapsed = time.perf_counter() - started
    print(
        f"[MANIFEST] Checksums summary: files_hashed={len(entries)}, bytes_hashed={bytes_count}, "
        f"out={out_path}, elapsed={elapsed:.2f}s"
    )
    return out_path


__all__ = ["build_manifest", "write_checksums_file"]
