"""Does the durable transcript capture a REAL agent round trip?

Every other check of `vfx_harness/observability/transcript.py` builds SDK objects by hand. That proves the
serialiser, not the wiring: whether `log_message` is actually reached on a live stream,
whether an image tool result arrives in the shape the scrubber expects, and whether the
auth in `.env` works at all are separate questions this answers in one ~$0.02 call.

    .venv/bin/python docs/research/probes/spike_transcript_live.py

Deliberately cheap and deliberately image-bearing: the tool returns a real PNG as base64,
because a placeholder that works on a synthetic blob and not on a live one would be a
silent hole in exactly the direction that matters (renders are the bulk of the bytes).
"""

from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import anyio
from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query, tool
from PIL import Image

from vfx_harness.observability import transcript
from vfx_harness.observability.log import log, log_message, reset_tool_use, tool_use_summary


@tool("fake_render", "Render the scene and return the frame as an image.",
      {"frame": int})
async def fake_render(args):
    """Stand-in for render_frame: returns a real PNG so the base64 path is exercised."""
    im = Image.new("RGB", (320, 180), (30, 40, 90))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"content": [
        {"type": "text", "text": f"rendered frame {args['frame']}: 320x180, mean 41.2"},
        {"type": "image", "data": b64, "mimeType": "image/png"},
    ]}


async def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    reset_tool_use()
    tpath = transcript.bind(tmp, "probe", label="live", run_id="LIVE")
    assert tpath is not None, "transcript disabled — unset VFXH_NO_TRANSCRIPT"

    server = create_sdk_mcp_server(name="probe", version="0.1.0", tools=[fake_render])
    opts = ClaudeAgentOptions(
        mcp_servers={"probe": server},
        allowed_tools=["mcp__probe__fake_render"],
        system_prompt="You are terse. Call the tool exactly once, then say DONE.",
        permission_mode="bypassPermissions",
        setting_sources=[],
        max_turns=4,
    )
    prompt = "Call fake_render for frame 7, then reply with just DONE."
    transcript.prompt(prompt, role="kickoff", probe=True)

    log("live probe: one real SDK call with one real image-bearing tool result")
    async for m in query(prompt=prompt, options=opts):
        log_message(m)          # the production path: console + transcript
    transcript.unbind()

    evs = transcript.read(tpath)
    kinds = [e["kind"] for e in evs]
    raw = tpath.read_text(encoding="utf-8")
    size = tpath.stat().st_size

    print("\n── what the transcript captured ──")
    for e in evs:
        print(" ", json.dumps({k: v for k, v in e.items() if k != "dt"})[:170])

    fails = []
    if "prompt" not in kinds:
        fails.append("the INPUT was not recorded")
    if "tool_use" not in kinds:
        fails.append("no tool_use recorded — the model may not have called the tool")
    if "tool_result" not in kinds:
        fails.append("no tool_result recorded: log_message is not seeing UserMessages")
    if "result" not in kinds:
        fails.append("no result recorded (cost/turns lost)")

    imgs = [c for e in evs if isinstance(e.get("content"), list)
            for c in e["content"] if isinstance(c, dict) and c.get("type") == "image"]
    if not imgs:
        fails.append("the image tool result did not arrive as an image block — the "
                     "scrubber never saw it, so a real render might leak base64")
    elif not all(c.get("omitted") and c.get("bytes", 0) > 100 for c in imgs):
        fails.append(f"image block not scrubbed: {imgs}")
    if "iVBORw0KGgo" in raw:
        fails.append("RAW BASE64 IS IN THE TRANSCRIPT")

    tu = tool_use_summary()
    if tu.get("total", 0) < 1:
        fails.append(f"tool telemetry did not count the call: {tu}")

    res = next((e for e in evs if e["kind"] == "result"), {})
    print(f"\n  transcript {size} bytes · {len(evs)} events · {len(imgs)} image(s) scrubbed")
    print(f"  cost ${res.get('cost_usd') or 0:.4f} · {res.get('turns')} turns · "
          f"subtype {res.get('subtype')}")
    print(f"  tool telemetry: {json.dumps(tu)}")
    print(f"  file: {tpath}")

    if fails:
        print("\n✗ LIVE PROBE FAILED")
        for f in fails:
            print(f"   · {f}")
        return 1
    print("\n✓ live round trip captured: input, tool call, image-bearing result "
          "(base64 stripped), cost and turns")
    return 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(main))
