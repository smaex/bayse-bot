"""Keep repeated per-tick log lines from piling up.

The evaluator runs for every user on every price tick, so a decision such as
"SKIP MAKER BTC — passive orders require CLOB" can be logged hundreds of times a
minute. The decision itself is unchanged; only the repeats are collapsed.

* ``RepeatLogFilter`` is attached to the stdout handler. It only touches INFO
  records that match the known noisy families (SKIP, EV, DRY RUN, PAUSED).
  Warnings and errors always pass. The first occurrence of a normalised message
  is logged, repeats inside the window are dropped, and the next emitted line
  notes how many were suppressed.
* ``Throttle`` is for error paths that repeat on a timer, such as a database
  read failing every cycle. It allows one event per key per interval.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable

_NOISY = re.compile(
    r"^(\[[^\]]*\]\s+)?(SKIP\b|EV\s|DRY RUN\b|.*\bPAUSED\b)"
)
_DIGITS = re.compile(r"\d+(\.\d+)?")


def _normalise(message: str) -> str:
    # Numbers (chat ids, prices, sizes, EV %) vary per tick; the decision does not.
    return _DIGITS.sub("#", message)


class RepeatLogFilter(logging.Filter):
    def __init__(self, window_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__()
        self.window = window_seconds
        self.clock = clock
        # key -> [window_start, suppressed_count]
        self._seen: dict[tuple[str, str], list] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.INFO:
            return True
        message = record.getMessage()
        if not _NOISY.search(message):
            return True

        key = (record.name, _normalise(message))
        now = self.clock()
        state = self._seen.get(key)
        if state is None or now - state[0] >= self.window:
            suppressed = state[1] if state else 0
            self._seen[key] = [now, 0]
            if suppressed:
                record.msg = f"{message} ({suppressed} similar in last {int(self.window)}s)"
                record.args = ()
            if len(self._seen) > 5000:
                self._prune(now)
            return True

        state[1] += 1
        return False

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._seen.items() if now - v[0] >= self.window]:
            del self._seen[key]


def attach(handler: logging.Handler, window_seconds: float = 60.0) -> None:
    handler.addFilter(RepeatLogFilter(window_seconds=window_seconds))


class Throttle:
    def __init__(self, interval_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.interval = interval_seconds
        self.clock = clock
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        last = self._last.get(key)
        if last is not None and now - last < self.interval:
            return False
        self._last[key] = now
        return True
