"""Terminal progress bar (standard library only).

On a terminal it draws one self-updating line at the bottom:

    Classify  [████████░░░░░░░░░░░░]  1,204/5,591  22%  2:41 elapsed  ~9:30 left  Overlord, Vol. 3

Log lines printed while a bar is active appear above it. When output is not a
terminal (cron, a pipe, a log file) no bar is drawn; a plain progress line is
printed about every 10% instead.
"""
from __future__ import annotations

import shutil
import sys
import threading
import time


def _fmt_secs(s: float) -> str:
    s = int(max(0, s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


class ProgressBar:
    _active = None  # the bar currently drawn, so log lines can clear and redraw it
    _lock = threading.Lock()

    def __init__(self, label: str, total: int, stream=None, plain_log=None, enabled: bool | None = None):
        self.label = label
        self.total = max(0, int(total))
        self.stream = stream or sys.stderr
        self.tty = self.stream.isatty() if enabled is None else enabled
        self.plain_log = plain_log
        self.n = 0
        self.item = ""
        self.start = time.monotonic()
        self._last_draw = 0.0
        self._last_plain_pct = -1
        self._ticker = None
        self._stop = threading.Event()
        self._previous = None

    # ----------------------------------------------------------- lifecycle --
    def __enter__(self):
        if self.tty:
            self._previous = ProgressBar._active  # e.g. a write batch inside the fetch pass
            ProgressBar._active = self
            self._draw(force=True)
            # Redraw once a second so the elapsed time moves even during a long lookup.
            self._ticker = threading.Thread(target=self._tick, daemon=True)
            self._ticker.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self.tty:
            with ProgressBar._lock:
                self._clear()
                ProgressBar._active = self._previous
            if self._previous is not None:
                self._previous._draw(force=True)
        return False

    def _tick(self):
        while not self._stop.wait(1.0):
            self._draw(force=True)

    # --------------------------------------------------------------- update --
    def update(self, n: int | None = None, item: str | None = None, advance: int = 0):
        if n is not None:
            self.n = n
        self.n += advance
        if item is not None:
            self.item = item
        if self.tty:
            self._draw()
        elif self.plain_log and self.total:
            pct = int(self.n * 100 / self.total)
            if pct // 10 > self._last_plain_pct // 10 or self.n == self.total:
                self._last_plain_pct = pct
                self.plain_log(f"  {self.label}: {self.n}/{self.total} ({pct}%), "
                               f"{_fmt_secs(time.monotonic() - self.start)} elapsed{self._eta_text()}")

    def advance(self, item: str | None = None):
        self.update(item=item, advance=1)

    # -------------------------------------------------------------- drawing --
    def _eta_text(self) -> str:
        if not self.total or self.n <= 0 or self.n >= self.total:
            return ""
        rate = (time.monotonic() - self.start) / self.n
        return f", ~{_fmt_secs(rate * (self.total - self.n))} left"

    def _line(self) -> str:
        width = shutil.get_terminal_size((100, 20)).columns
        elapsed = time.monotonic() - self.start
        if self.total:
            frac = min(1.0, self.n / self.total)
            bar_w = 20
            filled = int(frac * bar_w)
            bar = "█" * filled + "░" * (bar_w - filled)
            eta = ""
            if 0 < self.n < self.total:
                eta = f"  ~{_fmt_secs(elapsed / self.n * (self.total - self.n))} left"
            core = f"{self.label}  [{bar}]  {self.n:,}/{self.total:,}  {int(frac * 100)}%  {_fmt_secs(elapsed)} elapsed{eta}"
        else:
            core = f"{self.label}  {self.n:,} done  {_fmt_secs(elapsed)} elapsed"
        if self.item:
            core += "  " + self.item
        if len(core) > width - 1:
            core = core[: max(0, width - 2)] + "…"
        return core

    def _draw(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_draw < 0.1:
            return
        with ProgressBar._lock:
            if ProgressBar._active is not self:
                return
            self._last_draw = now
            self.stream.write("\r\x1b[2K" + self._line())
            self.stream.flush()

    def _clear(self):
        self.stream.write("\r\x1b[2K")
        self.stream.flush()

    @classmethod
    def stop_all(cls):
        """Remove any bars left on screen (after Ctrl+C or an error)."""
        with cls._lock:
            bar = cls._active
            while bar is not None:
                bar._stop.set()
                bar = bar._previous
            if cls._active is not None:
                cls._active._clear()
            cls._active = None

    # ------------------------------------------------- log line integration --
    @classmethod
    def print_above(cls, text: str, stream):
        """Print a log line without breaking an active bar."""
        bar = cls._active
        if bar is None:
            print(text, file=stream, flush=True)
            return
        with cls._lock:
            bar.stream.write("\r\x1b[2K")
            bar.stream.flush()
            print(text, file=stream, flush=True)
            bar.stream.write(bar._line())
            bar.stream.flush()


class NullBar:
    """Stand-in used when nothing should be shown."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, *a, **k):
        pass

    def advance(self, *a, **k):
        pass
