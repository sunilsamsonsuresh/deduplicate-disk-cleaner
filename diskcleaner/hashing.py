"""Content hashing (BLAKE2b - fast and cryptographically strong)."""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 1024 * 1024          # 1 MiB reads
PARTIAL_BYTES = 64 * 1024    # bytes from start and end for the quick pre-check


def partial_hash(path: Path, size: int | None = None) -> str:
    """Hash of size + first 64 KiB + last 64 KiB. Cheap filter before a full hash."""
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as fh:
        if size is None:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(0)
        h.update(str(size).encode())
        h.update(fh.read(PARTIAL_BYTES))
        if size > PARTIAL_BYTES * 2:
            fh.seek(-PARTIAL_BYTES, 2)
            h.update(fh.read(PARTIAL_BYTES))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()
