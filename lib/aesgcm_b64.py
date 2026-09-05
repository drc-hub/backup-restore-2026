from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


NONCE_LEN = 12
TAG_LEN = 16


@dataclass(frozen=True)
class EncryptResult:
    nonce: bytes
    tag: bytes
    sha256_plaintext_hex: str


def encrypt_file_to_aes256gcm(
    *,
    key: bytes,
    plaintext_path: Path,
    out_path: Path,
    nonce: bytes | None = None,
) -> EncryptResult:
    """Encrypt a file with AES-256-GCM and write raw binary: nonce || ciphertext || tag.

    - nonce : 12 bytes (random if not provided)
    - tag   : 16 bytes (GCM authentication tag, appended after ciphertext)

    Output is written via a temp file and atomically renamed.
    """
    if len(key) != 32:
        raise ValueError("key must be 32 bytes for AES-256")

    if nonce is None:
        nonce = os.urandom(NONCE_LEN)
    if len(nonce) != NONCE_LEN:
        raise ValueError("nonce must be 12 bytes")

    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    sha256 = hashlib.sha256()

    tmp_path = out_path.with_name(out_path.name + ".tmp")
    with open(tmp_path, "wb") as out_f:
        out_f.write(nonce)

        with open(plaintext_path, "rb") as in_f:
            while True:
                chunk = in_f.read(1024 * 1024)
                if not chunk:
                    break
                sha256.update(chunk)
                ct = encryptor.update(chunk)
                if ct:
                    out_f.write(ct)

        final_ct = encryptor.finalize()
        if final_ct:
            out_f.write(final_ct)

        tag = encryptor.tag
        out_f.write(tag)

        out_f.flush()
        os.fsync(out_f.fileno())

    os.replace(tmp_path, out_path)
    return EncryptResult(nonce=nonce, tag=tag, sha256_plaintext_hex=sha256.hexdigest())
