"""The Blender agent tools, exercised against a fake bridge — no SDK query, no real Blender.

Each ``@tool`` is an ``SdkMcpTool`` with an async ``.handler``; we call handlers directly and assert
on the content blocks they return. The fake bridge records the bpy it was asked to run and returns
canned payloads shaped like the real ``run_python``/``render``/``image_stats``/``get_scene_graph``.
"""

from __future__ import annotations

import asyncio
import tempfile

from agents.blender_tools import build_blender_tools
from scene.bridge import BridgeError


class FakeBridge:
    def __init__(self, *, luma: float = 0.4, run_ok: bool = True, raise_on_run: bool = False) -> None:
        self.luma = luma
        self.run_ok = run_ok
        self.raise_on_run = raise_on_run
        self.render_dir = tempfile.mkdtemp(prefix="fakebridge-")  # tools mkdir the agent subdir under here
        self.code_seen: list[str] = []
        self.rendered: list[dict] = []
        self.introspect_result: dict | None = None

    def run_python(self, code: str) -> dict:
        self.code_seen.append(code)
        if self.raise_on_run:
            raise BridgeError("socket closed")
        if "result = {" in code and "eval(" in code and self.introspect_result is not None:
            # introspection probe — hand back the structured result the server expects
            return {"ok": True, "stdout": "", "result": self.introspect_result}
        if not self.run_ok:
            return {"ok": False, "stdout": "boom-out", "error": "NameError: name 'foo' is not defined"}
        return {"ok": True, "stdout": "hello", "result": {"built": True}}

    def render(self, **params) -> dict:
        self.rendered.append(params)
        return {"path": params["path"], "bytes": 123, "image_b64": "SU1HMDk="}

    def image_stats(self, **_params) -> dict:
        return {"luma_mean": self.luma, "luma_std": 0.15, "mean_rgb": [0.4, 0.4, 0.4], "hist8": [0.125] * 8}

    def get_scene_graph(self) -> dict:
        return {
            "scene": {"active_camera": "Camera", "engine": "BLENDER_EEVEE"},
            "objects": [{"type": "MESH"}, {"type": "LIGHT", "light": {"energy": 5000}}, {"type": "CAMERA"}],
        }


def _tools(bridge):
    """The four tools by name."""
    return {t.name: t for t in build_blender_tools(bridge, render_subdir="test")}


def _text_of(result: dict) -> str:
    return "\n".join(b["text"] for b in result["content"] if b["type"] == "text")


def test_run_bpy_returns_stdout_and_result() -> None:
    bridge = FakeBridge()
    tools = _tools(bridge)
    out = asyncio.run(tools["run_bpy"].handler({"code": "x = 1"}))
    assert not out.get("is_error")
    body = _text_of(out)
    assert "hello" in body and "built" in body
    assert bridge.code_seen == ["x = 1"]


def test_run_bpy_surfaces_the_traceback_as_error() -> None:
    bridge = FakeBridge(run_ok=False)
    tools = _tools(bridge)
    out = asyncio.run(tools["run_bpy"].handler({"code": "boom"}))
    assert out.get("is_error") is True
    assert "NameError" in _text_of(out)


def test_run_bpy_requires_code() -> None:
    out = asyncio.run(_tools(FakeBridge())["run_bpy"].handler({"code": "   "}))
    assert out.get("is_error") is True


def test_run_bpy_wraps_bridge_error() -> None:
    out = asyncio.run(_tools(FakeBridge(raise_on_run=True))["run_bpy"].handler({"code": "x=1"}))
    assert out.get("is_error") is True
    assert "bridge error" in _text_of(out)


def test_introspect_reports_members_when_the_expr_resolves() -> None:
    bridge = FakeBridge()
    bridge.introspect_result = {
        "ok": True, "type": "Scene", "repr": "bpy.data.scenes['Scene']",
        "members": ["eevee", "camera", "frame_set"], "member_count": 3, "doc": "A scene.",
    }
    tools = _tools(bridge)
    out = asyncio.run(tools["introspect"].handler({"expr": "bpy.context.scene"}))
    assert not out.get("is_error")
    body = _text_of(out)
    assert "Scene" in body and "eevee" in body and "frame_set" in body
    # the probe code must actually evaluate the expr the agent asked about
    assert "bpy.context.scene" in bridge.code_seen[-1]


def test_introspect_reports_a_missing_api_as_the_answer_not_an_error() -> None:
    bridge = FakeBridge()
    bridge.introspect_result = {"ok": False, "error": "AttributeError: 'Scene' object has no attribute 'node_tree'"}
    tools = _tools(bridge)
    out = asyncio.run(tools["introspect"].handler({"expr": "bpy.context.scene.node_tree"}))
    assert not out.get("is_error")  # a missing attribute is data, not a tool failure
    assert "does NOT resolve" in _text_of(out) and "node_tree" in _text_of(out)


def test_render_returns_the_image_and_its_stats() -> None:
    bridge = FakeBridge(luma=0.4)
    tools = _tools(bridge)
    out = asyncio.run(tools["render"].handler({"label": "lit"}))
    kinds = [b["type"] for b in out["content"]]
    assert "image" in kinds and "text" in kinds
    assert "luma_mean=0.4" in _text_of(out)
    img = next(b for b in out["content"] if b["type"] == "image")
    assert img["data"] == "SU1HMDk=" and img["mimeType"] == "image/jpeg"


def test_render_warns_on_a_black_frame() -> None:
    out = asyncio.run(_tools(FakeBridge(luma=0.0))["render"].handler({}))
    assert "near-black" in _text_of(out)


def test_render_sets_the_frame_first_when_asked() -> None:
    bridge = FakeBridge()
    tools = _tools(bridge)
    asyncio.run(tools["render"].handler({"frame": 12}))
    assert any("frame_set(int(12))" in c for c in bridge.code_seen)


def test_scene_graph_reports_the_digest() -> None:
    out = asyncio.run(_tools(FakeBridge())["scene_graph"].handler({}))
    body = _text_of(out)
    assert "1 mesh" in body and "1 light" in body and "active_camera=Camera" in body
    assert "5000" in body  # total light energy surfaced


# --- asset tools (gated on an asset library) ------------------------------------------

import types  # noqa: E402


def _import_glb_bridge():
    b = FakeBridge()
    b.imported = []
    b.import_glb = lambda **p: (b.imported.append(p["path"]) or {"objects": ["Asset_0"]})
    return b


def test_asset_tools_absent_without_a_library():
    names = {t.name for t in build_blender_tools(FakeBridge())}
    assert names == {"run_bpy", "introspect", "render", "viewport_snapshot", "scene_graph"}  # no asset tools


def test_acquire_asset_tool_generates_then_imports():
    bridge = _import_glb_bridge()
    calls = []

    async def fake_acquire(*, description, library):
        calls.append(description)
        return types.SimpleNamespace(path="/tmp/tower.glb", description=description, kind="mesh")

    lib = types.SimpleNamespace(list=lambda: [])
    tools = {t.name: t for t in build_blender_tools(bridge, asset_library=lib, acquire=fake_acquire)}
    assert "acquire_asset" in tools and "list_assets" in tools  # tools appear with a library

    out = asyncio.run(tools["acquire_asset"].handler({"description": "a green data tower"}))
    body = _text_of(out)
    assert calls == ["a green data tower"]  # generation was requested
    assert bridge.imported == ["/tmp/tower.glb"]  # and the GLB was imported into the scene
    assert "imported" in body


def test_acquire_asset_degrades_when_generation_fails():
    async def boom(*, description, library):
        raise RuntimeError("higgsfield out of credits")

    tools = {t.name: t for t in build_blender_tools(FakeBridge(), asset_library=types.SimpleNamespace(list=list), acquire=boom)}
    out = asyncio.run(tools["acquire_asset"].handler({"description": "x"}))
    assert out.get("is_error") is True and "Build it from geometry instead" in _text_of(out)


def test_list_assets_tool_reports_the_library():
    lib = types.SimpleNamespace(list=lambda: [types.SimpleNamespace(description="a chair", kind="mesh")])
    tools = {t.name: t for t in build_blender_tools(FakeBridge(), asset_library=lib, acquire=None)}
    out = asyncio.run(tools["list_assets"].handler({}))
    assert "a chair" in _text_of(out)


# --- viewport_snapshot + the visual-look budget ---------------------------------------


def test_viewport_snapshot_uses_workbench_and_returns_an_image():
    bridge = FakeBridge()
    tools = {t.name: t for t in build_blender_tools(bridge)}
    out = asyncio.run(tools["viewport_snapshot"].handler({"label": "blocking"}))
    kinds = [b["type"] for b in out["content"]]
    assert "image" in kinds and "solid-shaded" in _text_of(out)
    assert bridge.rendered[-1]["engine"] == "BLENDER_WORKBENCH" and bridge.rendered[-1]["samples"] == 1


def test_look_budget_caps_render_and_snapshot_combined():
    bridge = FakeBridge()
    tools = {t.name: t for t in build_blender_tools(bridge, look_budget=2)}
    # two visual looks allowed (a snapshot then a render), the third is denied with guidance
    assert not asyncio.run(tools["viewport_snapshot"].handler({})).get("is_error")
    assert not asyncio.run(tools["render"].handler({})).get("is_error")
    denied = asyncio.run(tools["render"].handler({}))
    assert denied.get("is_error") is True and "budget spent" in _text_of(denied)
    assert "scene_graph" in _text_of(denied)  # points the desk at the free channel
    # and a further snapshot is denied too (shared budget)
    assert asyncio.run(tools["viewport_snapshot"].handler({})).get("is_error") is True


def test_scene_graph_is_free_even_past_the_look_budget():
    bridge = FakeBridge()
    tools = {t.name: t for t in build_blender_tools(bridge, look_budget=1)}
    asyncio.run(tools["render"].handler({}))  # spend the budget
    assert asyncio.run(tools["render"].handler({})).get("is_error") is True  # visual look denied
    out = asyncio.run(tools["scene_graph"].handler({}))  # symbolic look still free
    assert not out.get("is_error") and "active_camera=Camera" in _text_of(out)
