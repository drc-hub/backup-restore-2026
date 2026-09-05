import hashlib
import os
import shutil
import zlib


def copy_file_stream(src: str, dst: str, bufsize: int = 1024 * 1024) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as fr, open(dst, "wb") as fw:
        while True:
            chunk = fr.read(bufsize)
            if not chunk:
                break
            fw.write(chunk)
    shutil.copystat(src, dst)


def compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def zstd_compress_file(
    src: str,
    dst: str,
    *,
    level: int = 5,
    bufsize: int = 1024 * 1024,
    compute_raw_sha256: bool = False,
):
    import zstandard as zstd
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    cctx = zstd.ZstdCompressor(level=level)
    raw_hasher = hashlib.sha256() if compute_raw_sha256 else None

    bytes_in = 0
    tmp = dst + ".tmp"

    with open(src, "rb") as fr, open(tmp, "wb") as fw:
        with cctx.stream_writer(fw) as compressor:
            while True:
                chunk = fr.read(bufsize)
                if not chunk:
                    break
                bytes_in += len(chunk)
                if raw_hasher is not None:
                    raw_hasher.update(chunk)
                compressor.write(chunk)

    os.replace(tmp, dst)
    bytes_out = os.path.getsize(dst)
    return {
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "raw_sha256": raw_hasher.hexdigest() if raw_hasher is not None else None,
    }


__all__ = ["copy_file_stream", "compute_sha256", "zstd_compress_file"]
