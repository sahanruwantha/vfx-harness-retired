"""Provider-independent, flushing console output."""

import time

_t0: float | None = None


def _elapsed() -> float:
    global _t0
    now = time.monotonic()
    if _t0 is None:
        _t0 = now
    return now - _t0


def log(msg: str, indent: int = 0) -> None:
    """Timestamped, immediately-flushed log line."""
    print(f"[{_elapsed():6.1f}s] {'  ' * indent}{msg}", flush=True)


