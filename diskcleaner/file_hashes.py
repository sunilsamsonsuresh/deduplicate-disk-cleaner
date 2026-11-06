"""File hashes. A hash is a short code that the tool makes from the file content.
Two files with the same hash have the same content. This tool uses BLAKE2b (fast and strong)."""

import hashlib
from pathlib import Path

CHUNK = 1024 * 1024          # read 1 MiB at a time
PARTIAL_BYTES = 64 * 1024    # bytes from the start and from the end, for the quick check


def partial_hash(path: Path, size: int | None = None) -> str:
    """Make a quick hash from the size, the first 64 KiB and the last 64 KiB.
    This is fast. Use it to find files that can be equal, before the full hash."""
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
    """Make a hash from all of the file content."""
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
