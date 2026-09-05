"""transfer.py — rsync helper supporting multiple destinations.

Feature 3 (Multi-Destination Transfer): every finalized cycle is pushed to
every target in the list so that the 20-minute RPO survives a total local wipe.
"""

import os
import shutil
import subprocess
from typing import List


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _is_remote(target: str) -> bool:
    """Return True if *target* looks like a remote rsync path (user@host:/path)."""
    # A colon that is NOT part of a Windows drive letter (not that we run on Windows,
    # but be explicit) indicates a remote destination.
    return ":" in target


def _known_hosts_file() -> str | None:
    """Return a usable known_hosts file.

    When running as root the default /root/.ssh/known_hosts is often empty.
    Fall back to the primary user's file so host-key verification doesn't
    fail just because we escalated via sudo.
    """
    candidates = [
        os.path.expanduser("~/.ssh/known_hosts"),         # current user (root or primary)
        "/home/primary/.ssh/known_hosts",                 # primary user fallback
    ]
    for p in candidates:
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    return None


def _ssh_base_opts(*, port: int, ssh_key: str | None) -> list[str]:
    """Build the common SSH option flags shared by mkdir and rsync."""
    known_hosts = _known_hosts_file()
    opts = [
        "-p", str(port),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        # Accept new host keys automatically (no prompt), but still reject
        # changed keys to protect against MITM.
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if known_hosts:
        opts += ["-o", f"UserKnownHostsFile={known_hosts}"]
    if ssh_key:
        opts += ["-i", ssh_key]
    return opts


def _ssh_mkdir_p(*, user_host: str, port: int, path: str, ssh_key: str | None) -> None:
    ssh_path = _which("ssh")
    if not ssh_path:
        raise RuntimeError("ssh not found on PATH")

    cmd = [ssh_path] + _ssh_base_opts(port=port, ssh_key=ssh_key)
    cmd += [user_host, f"mkdir -p {path!s}"]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ssh mkdir failed (code={proc.returncode}): {proc.stdout.strip()}")


def rsync_tree(
    *,
    src_dir: str,
    dest_root: str,
    dest_name: str | None = None,
    remote_target: str | None = None,
    ssh_port: int = 22,
    ssh_key: str | None = None,
) -> str:
    """Rsync a finalized cycle directory into dest_root/dest_name.

    Intended for copying immutable per-cycle outputs to a receiver "incoming" directory.
    """

    src_dir = os.path.abspath(src_dir)
    if not os.path.isdir(src_dir):
        raise RuntimeError(f"Not a directory: {src_dir}")

    if dest_name is None:
        dest_name = os.path.basename(src_dir.rstrip(os.sep))

    if remote_target:
        # remote_target format: user@host:/abs/path
        if ":" not in remote_target:
            raise RuntimeError("RECOVERY_RSYNC_TARGET must look like user@host:/abs/path")
        user_host, remote_root = remote_target.split(":", 1)
        remote_root = remote_root.rstrip("/")
        remote_dest = f"{remote_root}/{dest_name}"
        _ssh_mkdir_p(user_host=user_host, port=ssh_port, path=remote_dest, ssh_key=ssh_key)
        dest_dir = f"{user_host}:{remote_dest}"
    else:
        dest_root = os.path.abspath(dest_root)
        dest_dir = os.path.join(dest_root, dest_name)
        os.makedirs(dest_dir, exist_ok=True)

    rsync_path = _which("rsync")
    if not rsync_path:
        raise RuntimeError("rsync not found on PATH")

    # Trailing slashes mean: copy contents of src_dir into dest_dir.
    cmd = [rsync_path, "-a", "--human-readable", "--info=stats2,progress2"]
    if remote_target:
        ssh_cmd = f"ssh -p {ssh_port} -o BatchMode=yes -o ConnectTimeout=10"
        if ssh_key:
            ssh_cmd += f" -i {ssh_key}"
        cmd += [
            "-e",
            ssh_cmd,
        ]
    cmd += [
        src_dir.rstrip(os.sep) + os.sep,
        dest_dir.rstrip(os.sep) + os.sep,
    ]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rsync failed (code={proc.returncode}): {proc.stdout.strip()}")

    return dest_dir


def rsync_to_target(
    *,
    src_dir: str,
    target: str,
    dest_name: str | None = None,
    ssh_port: int = 22,
    ssh_key: str | None = None,
) -> str:
    """Push *src_dir* to a single *target* (local path or remote rsync string).

    This is the unified entry-point used by the multi-destination loop.
    It auto-detects whether *target* is local or remote.
    """
    src_dir = os.path.abspath(src_dir)
    if dest_name is None:
        dest_name = os.path.basename(src_dir.rstrip(os.sep))

    if _is_remote(target):
        # Remote rsync target: user@host:/abs/path
        if ":" not in target:
            raise RuntimeError(f"Remote target must look like user@host:/abs/path, got: {target!r}")
        user_host, remote_root = target.split(":", 1)
        remote_root = remote_root.rstrip("/")
        remote_dest = f"{remote_root}/{dest_name}"
        _ssh_mkdir_p(user_host=user_host, port=ssh_port, path=remote_dest, ssh_key=ssh_key)
        dest_dir = f"{user_host}:{remote_dest}"

        rsync_path = _which("rsync")
        if not rsync_path:
            raise RuntimeError("rsync not found on PATH")

        # Build the -e ssh string from the same option set used by _ssh_mkdir_p.
        ssh_opts = _ssh_base_opts(port=ssh_port, ssh_key=ssh_key)
        ssh_cmd = "ssh " + " ".join(ssh_opts)
        cmd = [
            rsync_path,
            "-a",
            "--human-readable",
            "--info=stats2,progress2",
            "-e", ssh_cmd,
            src_dir.rstrip(os.sep) + os.sep,
            dest_dir.rstrip(os.sep) + os.sep,
        ]
    else:
        # Local destination directory.
        local_dest = os.path.join(os.path.abspath(target), dest_name)
        os.makedirs(local_dest, exist_ok=True)
        dest_dir = local_dest

        rsync_path = _which("rsync")
        if not rsync_path:
            raise RuntimeError("rsync not found on PATH")

        cmd = [
            rsync_path,
            "-a",
            "--human-readable",
            "--info=stats2,progress2",
            src_dir.rstrip(os.sep) + os.sep,
            dest_dir.rstrip(os.sep) + os.sep,
        ]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"rsync to {target!r} failed (code={proc.returncode}): {proc.stdout.strip()}"
        )

    return dest_dir


def rsync_to_all_targets(
    *,
    src_dir: str,
    targets: List[str],
    dest_name: str | None = None,
    ssh_port: int = 22,
    ssh_key: str | None = None,
) -> List[str]:
    """Push *src_dir* to every target in *targets* (Feature 3).

    Returns a list of (target, dest_dir) strings for logging.
    Raises RuntimeError if ANY target fails — the caller decides whether to
    abort or continue.
    """
    results: List[str] = []
    errors: List[str] = []

    for target in targets:
        try:
            dest = rsync_to_target(
                src_dir=src_dir,
                target=target,
                dest_name=dest_name,
                ssh_port=ssh_port,
                ssh_key=ssh_key,
            )
            results.append(dest)
            print(f"transfer   <good>   OK: {target!r} -> {dest}")
        except Exception as exc:
            errors.append(f"{target!r}: {exc}")
            print(f"transfer   <error>  FAIL: {target!r}: {exc}")

    if errors:
        raise RuntimeError(
            f"Transfer failed for {len(errors)}/{len(targets)} target(s): {'; '.join(errors)}"
        )

    return results


__all__ = ["rsync_tree", "rsync_to_target", "rsync_to_all_targets"]
