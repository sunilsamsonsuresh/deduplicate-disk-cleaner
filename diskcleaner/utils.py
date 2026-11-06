"""Small shared helpers: logging, progress bars, sizes, and running work in parallel."""

from __future__ import annotations

import datetime as dt
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from tqdm import tqdm as _tqdm
except ImportError:  # pragma: no cover
    _tqdm = None

log = logging.getLogger("diskcleaner")
QUIET = False  # tests switch progress output off


def setup_logging(state_dir: Path | None, verbose: bool = False) -> Path | None:
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(console)
    if state_dir is None:
        return None
    (state_dir / "logs").mkdir(parents=True, exist_ok=True)
    logfile = state_dir / "logs" / f"diskcleaner_{dt.datetime.now():%Y%m%d}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    return logfile


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------
class _Plain:
    """Progress without tqdm (or without a terminal): a line at 25/50/75/100%."""

    def __init__(self, iterable=None, total=None, desc="", quiet=False):
        self.iterable, self.total, self.desc, self.quiet = iterable, total, desc, quiet
        self.n = 0

    def __iter__(self):
        for item in self.iterable:
            yield item
            self.update(1)

    def update(self, k=1):
        before = self.n * 4 // self.total if self.total else 0
        self.n += k
        if not self.quiet and self.total and self.n * 4 // self.total != before:
            print(f"  {self.desc}: {self.n * 100 // self.total}% ({self.n}/{self.total})", file=sys.stderr)

    def close(self):
        pass


def progress(iterable=None, total=None, desc="", unit="file"):
    if not QUIET and _tqdm is not None and sys.stderr.isatty():
        return _tqdm(iterable, total=total, desc=desc, unit=unit, dynamic_ncols=True, leave=False)
    return _Plain(iterable, total=total, desc=desc, quiet=QUIET)


def parallel_map(fn, items, workers: int = 1, desc: str = ""):
    """Run fn(item) for every item on `workers` threads, with a progress bar.

    Yields (item, result, error) in the SAME ORDER as `items`, so results never
    depend on timing. `error` is the exception if fn failed (result is then None).
    Reading files and decoding images releases Python's lock, so threads really
    run side by side. Only the caller (one thread) touches the database.
    """
    items = list(items)
    if not items:
        return

    def call(item):
        try:
            return fn(item), None
        except Exception as exc:  # unreadable sector, truncated image, ...
            return None, exc

    bar = progress(total=len(items), desc=desc)
    pool = None
    try:
        if workers <= 1:
            results = map(call, items)
        else:
            pool = ThreadPoolExecutor(max_workers=workers)
            results = pool.map(call, items)
        for item, (value, error) in zip(items, results):
            bar.update(1)
            yield item, value, error
    finally:
        bar.close()
        if pool is not None:
            # On Ctrl+C: drop the queued work instead of waiting for all of it.
            pool.shutdown(wait=True, cancel_futures=True)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")
