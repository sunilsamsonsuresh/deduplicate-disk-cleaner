"""Small helpers that many files use: the log, progress bars, file sizes and parallel work."""

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
QUIET = False  # the tests set this to True to stop the progress output


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
    """A simple progress display. It prints one line at 25%, 50%, 75% and 100%.
    The tool uses it when tqdm is not installed or when there is no terminal."""

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
    """Run fn(item) for each item. Use `workers` threads at the same time. Show a progress bar.

    This gives (item, result, error) for each item, in the SAME ORDER as `items`.
    Thus the results do not change with the timing of the threads.
    If fn fails, result is None and error is the exception.
    Threads help here, because file reads and image decodes release the Python lock.
    Only the caller (one thread) writes to the database.
    """
    items = list(items)
    if not items:
        return

    def call(item):
        try:
            return fn(item), None
        except Exception as exc:  # for example a bad disk sector or a damaged image
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
            # If you push Ctrl+C, cancel the work that has not started yet.
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
