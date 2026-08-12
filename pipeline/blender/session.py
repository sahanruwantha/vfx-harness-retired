"""Client for the warm Blender worker — launch once, call many times."""

from __future__ import annotations

import itertools
import json
import subprocess
import tempfile
import time
from pathlib import Path

SENT = "@@BVFX@@"
_WORKER = Path(__file__).with_name("worker.py")


class BlenderError(RuntimeError):
    pass


class BlenderSession:
    """A long-lived headless Blender process holding one scene in memory.

    Snap-Blender note: artifacts must live under $HOME (snap confinement can't see
    /tmp), so the default artifacts dir is created next to the repo, not in /tmp.
    """

    def __init__(self, blender: str = "blender", artifacts_dir: str | Path | None = None,
                 blend_file: str | Path | None = None, boot_timeout: float = 60.0,
                 assets_dir: str | Path | None = None, cwd: str | Path | None = None):
        self.blender = blender
        self.blend_file = str(blend_file) if blend_file else None
        self.boot_timeout = boot_timeout
        self.assets_dir = str(assets_dir) if assets_dir else None
        # the worker must run FROM the shot folder: agents are configured with
        # cwd=shot.folder, so relative paths inside run_bpy ("refs/…", "build/…") have
        # to resolve the same way — otherwise the builder reads the repo root and
        # concludes its own references are missing.
        self.cwd = str(cwd) if cwd else None
        if artifacts_dir is None:
            artifacts_dir = Path(tempfile.mkdtemp(prefix=".bvfx-render-", dir=Path.home()))
        self.artifacts = Path(artifacts_dir)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "BlenderSession":
        argv = [self.blender, "--background", "--factory-startup"]
        if self.blend_file:
            argv.append(self.blend_file)
        argv += ["--python", str(_WORKER), "--", "--artifacts", str(self.artifacts)]
        if self.assets_dir:
            argv += ["--assets", self.assets_dir]
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=self.cwd,
        )
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            msg = self._read()
            if msg is None:
                break
            if msg.get("event") == "ready":
                return self
        raise BlenderError("Blender worker did not become ready")

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self._write({"id": next(self._ids), "cmd": "shutdown"})
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # -- io ------------------------------------------------------------------
    def _write(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _read(self) -> dict | None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line.startswith(SENT) and line.endswith(SENT) and len(line) > 2 * len(SENT):
                return json.loads(line[len(SENT):-len(SENT)])
        return None  # EOF

    def call(self, cmd: str, **args) -> dict:
        if not self.proc or self.proc.poll() is not None:
            raise BlenderError("Blender worker is not running")
        rid = next(self._ids)
        self._write({"id": rid, "cmd": cmd, "args": args})
        while True:
            msg = self._read()
            if msg is None:
                raise BlenderError(f"worker died during {cmd!r}")
            if msg.get("id") != rid:
                continue  # skip stray events
            if not msg.get("ok"):
                raise BlenderError(msg.get("error", "unknown") + "\n" + msg.get("trace", ""))
            return msg.get("result", {})

    # -- typed convenience wrappers -----------------------------------------
    def ping(self) -> dict:
        return self.call("ping")

    def run(self, code: str) -> dict:
        return self.call("run", code=code)

    def inspect(self, section: str = "all") -> str:
        return self.call("inspect", section=section)["text"]

    def keyframes(self, obj: str) -> str:
        return self.call("keyframes", object=obj)["text"]

    def render(self, frame: int, mode: str = "eevee", scale: float = 0.5, **kw) -> str:
        return self.call("render", frame=frame, mode=mode, scale=scale, **kw)["image_path"]

    def snapshot(self, tag: str) -> str:
        """Checkpoint the whole scene to a .blend; returns its path."""
        return self.call("snapshot", tag=tag)["blend"]

    def restore(self, blend: str) -> dict:
        """Restore a .blend checkpoint (exact scene state at snapshot time)."""
        return self.call("restore", blend=blend)
