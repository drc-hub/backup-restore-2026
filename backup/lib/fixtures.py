import os
import shutil
import time


def _copy_atomic(src: str, dst: str, *, bufsize: int = 1024 * 1024) -> int:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    copied = 0
    with open(src, "rb") as fr, open(tmp, "wb") as fw:
        while True:
            chunk = fr.read(bufsize)
            if not chunk:
                break
            fw.write(chunk)
            copied += len(chunk)
    os.replace(tmp, dst)
    try:
        shutil.copystat(src, dst)
    except Exception:
        pass
    return copied


def seed_fixtures(
    *,
    fixtures_dir: str,
    unstructured_dir: str,
    target_subdir: str = "fixtures",
) -> dict:
    started = time.perf_counter()

    if not fixtures_dir or not os.path.isdir(fixtures_dir):
        return {
            "enabled": False,
            "reason": "fixtures_dir_missing",
            "files_copied": 0,
            "files_skipped": 0,
            "bytes_copied": 0,
            "elapsed_s": 0.0,
        }

    target_root = os.path.join(unstructured_dir, target_subdir)
    os.makedirs(target_root, exist_ok=True)

    copied = 0
    skipped = 0
    bytes_copied = 0

    for root, _dirs, files in os.walk(fixtures_dir):
        for fname in files:
            src = os.path.join(root, fname)
            rel = os.path.relpath(src, fixtures_dir)
            dst = os.path.join(target_root, rel)

            try:
                src_stat = os.stat(src)
            except Exception:
                continue

            try:
                dst_stat = os.stat(dst)
                # If size+mtime match, treat as already seeded.
                if int(dst_stat.st_size) == int(src_stat.st_size) and int(dst_stat.st_mtime) == int(src_stat.st_mtime):
                    skipped += 1
                    continue
            except FileNotFoundError:
                pass
            except Exception:
                pass

            bytes_copied += _copy_atomic(src, dst)
            copied += 1

    elapsed = time.perf_counter() - started
    return {
        "enabled": True,
        "target_root": target_root,
        "files_copied": copied,
        "files_skipped": skipped,
        "bytes_copied": bytes_copied,
        "elapsed_s": elapsed,
    }


__all__ = ["seed_fixtures"]
