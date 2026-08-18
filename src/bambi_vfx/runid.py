"""Identity for one pipeline invocation.

Nothing stamped which RUN produced a given artifact, so two runs of the same shot wrote
into the same files with no way to tell their outputs apart afterwards — the second run
simply overwrote the first, and the comparison between them was gone. That makes paired
evaluation impossible: you cannot A/B a change if both arms are indistinguishable on disk.

The id is inherited through the environment so a driver and the layer subprocesses it
spawns all agree they are the same run, while a fresh invocation gets a fresh id.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

_ENV = "BVFX_RUN_ID"


def _mint() -> str:
    return (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            + "-" + uuid.uuid4().hex[:6])


RUN_ID: str = os.environ.get(_ENV) or _mint()
os.environ[_ENV] = RUN_ID          # children inherit it


def attempt_key(layer_id: str) -> str:
    """Stable id for one attempt at one layer within this run."""
    return f"{RUN_ID}/{layer_id}"
