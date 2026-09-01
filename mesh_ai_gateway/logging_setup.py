from __future__ import annotations

import logging
import threading
from collections import deque


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with self._lock:
                self.lines.append(line)
        except Exception:
            self.handleError(record)

    def recent(self, limit: int = 100) -> list[str]:
        with self._lock:
            return list(self.lines)[-max(1, limit):]


def configure_logging(level: str, retain_lines: int) -> RingBufferHandler:
    root = logging.getLogger()
    root.handlers.clear()
    numeric = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    ring = RingBufferHandler(retain_lines)
    ring.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(ring)
    return ring
