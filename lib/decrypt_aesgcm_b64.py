from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


NONCE_LEN = 12
TAG_LEN = 16


@dataclass(frozen=True)
class DecryptResult:
    nonce: bytes
    tag: bytes
    sha256_plaintext_hex: str


def decrypt_aes256gcm_to_file(*, key: bytes, in_path: Path, out_path: Path) -> DecryptResult:
    """Decrypt raw binary AES-256-GCM file (nonce || ciphertext || tag) into out_path.

    Uses streaming reads and streaming GCM decryption.
    Writes via a temp file and atomically renames.
    """
    if len(key) != 32:
        raise ValueError("key must be 32 bytes for AES-256")

    sha256 = hashlib.sha256()
    tmp = out_path.with_name(out_path.name + ".tmp")

    nonce = b""
    decryptor = None
    tail = b""  # holds last TAG_LEN bytes (the GCM tag)

    with open(in_path, "rb") as in_f, open(tmp, "wb") as out_f:
        while True:
            chunk = in_f.read(1024 * 1024)
            if not chunk:
                break

            # Pull nonce from the first NONCE_LEN bytes.
            if len(nonce) < NONCE_LEN:
                need = NONCE_LEN - len(nonce)
                nonce += chunk[:need]
                chunk = chunk[need:]
                if len(nonce) == NONCE_LEN:
                    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).decryptor()

            if not chunk:
                continue

            # Keep last TAG_LEN bytes buffered so we can pass the tag at finalize.
            tail += chunk
            if len(tail) > TAG_LEN:
                emit = tail[:-TAG_LEN]
                tail = tail[-TAG_LEN:]
                if decryptor is None:
                    raise RuntimeError("missing nonce; invalid input")
                pt = decryptor.update(emit)
                if pt:
                    sha256.update(pt)
                    out_f.write(pt)

        if decryptor is None or len(nonce) != NONCE_LEN:
            raise RuntimeError("invalid input: nonce not present")
        if len(tail) != TAG_LEN:
            raise RuntimeError(f"invalid input: expected {TAG_LEN}-byte tag, got {len(tail)}")

        tag = tail
        final_pt = decryptor.finalize_with_tag(tag)
        if final_pt:
            sha256.update(final_pt)
            out_f.write(final_pt)

        out_f.flush()
        os.fsync(out_f.fileno())

    os.replace(tmp, out_path)
    return DecryptResult(nonce=nonce, tag=tag, sha256_plaintext_hex=sha256.hexdigest())
