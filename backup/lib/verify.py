import hashlib
import json
import os
import zlib


def _sha256_file(path: str, *, bufsize: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _parse_checksums_sha256(path: str) -> dict:
    out = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            sha = parts[0]
            rel = parts[1].strip()
            if rel.startswith(" "):
                rel = rel.strip()
            out[rel] = sha
    return out


def _zlib_decompress_sha256(path: str, *, bufsize: int = 1024 * 1024) -> tuple[str, int]:
    h = hashlib.sha256()
    raw_bytes = 0
    decomp = zlib.decompressobj()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(bufsize)
            if not chunk:
                break
            out = decomp.decompress(chunk)
            if out:
                h.update(out)
                raw_bytes += len(out)
        tail = decomp.flush()
        if tail:
            h.update(tail)
            raw_bytes += len(tail)
    return h.hexdigest(), raw_bytes


def verify_cycle(cycle_dir: str, *, verify_raw: bool) -> tuple[bool, list[str]]:
    errors: list[str] = []

    manifest_path = os.path.join(cycle_dir, "manifest.json")
    checksums_path = os.path.join(cycle_dir, "checksums.sha256")

    if not os.path.isfile(manifest_path):
        errors.append(f"Missing manifest.json: {manifest_path}")
        return False, errors
    if not os.path.isfile(checksums_path):
        errors.append(f"Missing checksums.sha256: {checksums_path}")
        return False, errors

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    files_map = manifest.get("files") or {}
    if not isinstance(files_map, dict) or not files_map:
        errors.append("manifest.json missing/invalid 'files' map")
        return False, errors

    for rel, expected_sha in sorted(files_map.items()):
        path = os.path.join(cycle_dir, rel)
        if not os.path.isfile(path):
            errors.append(f"Missing file listed in manifest: {rel}")
            continue
        actual_sha = _sha256_file(path)
        if actual_sha != expected_sha:
            errors.append(f"SHA mismatch (manifest): {rel} expected={expected_sha} actual={actual_sha}")

    sums = _parse_checksums_sha256(checksums_path)
    for rel, expected_sha in sorted(sums.items()):
        path = os.path.join(cycle_dir, rel)
        if not os.path.isfile(path):
            errors.append(f"Missing file listed in checksums.sha256: {rel}")
            continue
        actual_sha = _sha256_file(path)
        if actual_sha != expected_sha:
            errors.append(f"SHA mismatch (checksums.sha256): {rel} expected={expected_sha} actual={actual_sha}")

    if verify_raw:
        mongo = manifest.get("mongo") or {}
        delta = (mongo.get("delta") or {}) if isinstance(mongo, dict) else {}
        if isinstance(delta, dict) and delta.get("compression") == "zlib":
            rel = delta.get("artifact")
            raw_sha = delta.get("raw_sha256")
            raw_bytes_expected = delta.get("raw_bytes")
            if rel and raw_sha and raw_bytes_expected is not None:
                path = os.path.join(cycle_dir, rel)
                if os.path.isfile(path):
                    actual_raw_sha, actual_raw_bytes = _zlib_decompress_sha256(path)
                    if actual_raw_sha != raw_sha or int(actual_raw_bytes) != int(raw_bytes_expected):
                        errors.append(
                            f"Mongo raw verify failed: {rel} sha_ok={actual_raw_sha == raw_sha} "
                            f"bytes_ok={int(actual_raw_bytes) == int(raw_bytes_expected)}"
                        )
                else:
                    errors.append(f"Missing mongo artifact for raw verify: {rel}")

        pfc = manifest.get("pfc") or {}
        deltas = pfc.get("deltas") if isinstance(pfc, dict) else None
        if isinstance(deltas, list):
            for entry in deltas:
                if not isinstance(entry, dict):
                    continue
                if entry.get("compression") != "zlib":
                    continue
                rel = entry.get("artifact")
                raw_sha = entry.get("raw_sha256")
                raw_bytes_expected = entry.get("raw_bytes")
                if not rel or not raw_sha or raw_bytes_expected is None:
                    continue
                path = os.path.join(cycle_dir, rel)
                if not os.path.isfile(path):
                    errors.append(f"Missing PFC artifact for raw verify: {rel}")
                    continue
                actual_raw_sha, actual_raw_bytes = _zlib_decompress_sha256(path)
                if actual_raw_sha != raw_sha or int(actual_raw_bytes) != int(raw_bytes_expected):
                    errors.append(
                        f"PFC raw verify failed: {rel} sha_ok={actual_raw_sha == raw_sha} "
                        f"bytes_ok={int(actual_raw_bytes) == int(raw_bytes_expected)}"
                    )

    return (len(errors) == 0), errors


__all__ = ["verify_cycle"]
