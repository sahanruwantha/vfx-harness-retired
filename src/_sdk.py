"""Shared Claude Agent SDK configuration for every bambi agent.

The SDK reads the CLI subprocess's stdout as newline-framed JSON and rejects any single
message larger than ``ClaudeAgentOptions.max_buffer_size`` (default 1 MB). That default is too
small for this pipeline: the sighted footage critics and the footage sourcer pass base64
keyframes, and the script/spine/treatment agents carry the full research dossier — either can be
echoed back on stdout as a single message well over 1 MB. Sizing the channel here, once, for the
payloads the pipeline legitimately sends is the fix; shrinking the payloads would only defer it.
"""

from __future__ import annotations

# 64 MB — a ceiling, not an allocation: only what is actually sent is buffered. Comfortably
# holds a full set of keyframes and a large dossier with room to spare, so a legitimate message
# never trips the limit again.
MAX_BUFFER_SIZE = 64 * 1024 * 1024

# Model tiers. Every stage currently runs on the reasoning tier (Opus, max effort) by request —
# the visual tier is aliased to it below, so the visual/mechanical stages (visual grammar, mode
# assignment, storyboard, footage sourcing + its sighted critic) run on Opus too. To re-enable the
# cheaper, faster Sonnet tier for those stages, set VISUAL_MODEL/VISUAL_EFFORT back to
# "claude-sonnet-5" / "high" — nothing else changes, the visual agents import these two names.
REASONING_MODEL = "claude-opus-5"
REASONING_EFFORT = "max"
VISUAL_MODEL = REASONING_MODEL
VISUAL_EFFORT = REASONING_EFFORT
