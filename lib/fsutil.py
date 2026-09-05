from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def ensure_dirs(*dirs: Path) -> None:
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, obj: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def rsync_copy_dir(src_dir: Path, dest_dir: Path, *, delete_extra: bool) -> None:
    cmd = ["rsync", "-a"]
    if delete_extra:
        cmd.append("--delete")
    cmd += [str(src_dir) + "/", str(dest_dir) + "/"]
    try:
        proc = subprocess.run(
            cmd,
            cwd="/",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("rsync not found; required for reliable directory copy")
    if proc.returncode != 0:
        raise RuntimeError(f"rsync failed: {proc.stdout}")


def ensure_dir_copy_atomic(src_dir: Path, dest_dir: Path) -> None:
    if dest_dir.exists():
        if not dest_dir.is_dir():
            raise RuntimeError(f"destination exists but is not a directory: {dest_dir}")
        rsync_copy_dir(src_dir, dest_dir, delete_extra=True)
        return

    tmp_parent = dest_dir.parent
    tmp_dir = tmp_parent / (dest_dir.name + f".tmp.{os.getpid()}")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    rsync_copy_dir(src_dir, tmp_dir, delete_extra=True)
    os.replace(tmp_dir, dest_dir)


class SingleInstanceLock:
    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeError(f"another instance is running (lock: {self._lock_path})")
            raise
        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
