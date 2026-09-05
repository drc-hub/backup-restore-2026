import os
import shutil


def dir_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except Exception:
                pass
    return total


def list_cycle_dirs(cycles_root: str):
    if not os.path.isdir(cycles_root):
        return []
    dirs = []
    for name in os.listdir(cycles_root):
        full = os.path.join(cycles_root, name)
        if os.path.isdir(full):
            dirs.append(full)
    dirs.sort(key=lambda p: os.path.getmtime(p))
    return dirs


def enforce_retention(*, cycles_root: str, max_cycles: int, max_bytes: int):
    os.makedirs(cycles_root, exist_ok=True)
    dirs = list_cycle_dirs(cycles_root)

    # prune by count
    while max_cycles > 0 and len(dirs) > max_cycles:
        victim = dirs.pop(0)
        print(f"retention  <info>   Removing old cycle {victim}")
        shutil.rmtree(victim, ignore_errors=True)

    # prune by total size
    def total_size():
        return sum(dir_size_bytes(d) for d in dirs)

    while dirs and total_size() > max_bytes:
        victim = dirs.pop(0)
        print(f"retention  <info>   Removing cycle to reduce size {victim}")
        shutil.rmtree(victim, ignore_errors=True)


__all__ = ["enforce_retention", "dir_size_bytes"]
