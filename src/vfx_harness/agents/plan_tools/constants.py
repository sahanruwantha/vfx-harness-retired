"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab."""

from __future__ import annotations

SERVER_NAME = "plan"
_MAX_TILES = 25  # 5 columns × up to 5 rows per contact sheet
_MAX_FRAMES = 4  # full-detail frames per extract_frames call
_SPIKE_TIMEOUT = 180  # s, hard cap for one headless blender run
_JPEG_Q = 85
_SPIKE_CONTRACT_MARKER = "@@VFX_PLAN_CONTRACT@@"


_CALIBRATION_CLOSED = (
    "CALIBRATION CLOSED: this session already spent its initial batch and one repair "
    "batch. Do not keep tuning image checks — they are optional evidence. Keep the "
    "candidates that passed, DROP the unresolved ones, and write the DAG, ownership "
    "register, and Layer 1 unit now. Anything an image check could not calibrate belongs "
    "to an executable scene contract or the producing unit's build-time falsification."
)
