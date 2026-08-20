"""Client for the warm Blender worker — launch once, call many times."""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path

SENT = "@@BVFX@@"
_WORKER = Path(__file__).with_name("worker.py")


class BlenderError(RuntimeError):
    pass


@lru_cache(maxsize=8)
def resolve_blender(requested: str) -> str:
    """Resolve and smoke-test the real Blender binary before starting a worker.

    Desktop launchers (notably Snap shims) can exist and still be unusable in the current
    process context.  Selection is based on a successful ``--version`` execution, not on
    a filename existing, and diagnostics from every rejected candidate are preserved.
    """
    candidates = []
    for value in (
        requested,
        shutil.which(requested),
        os.environ.get("BLENDER_BIN"),
        "/snap/blender/current/blender",
        "/usr/bin/blender",
    ):
        if value and value not in candidates:
            candidates.append(str(value))
    failures = []
    for candidate in candidates:
        path = shutil.which(candidate) or candidate
        if not Path(path).is_file():
            failures.append(f"{candidate}: not found")
            continue
        try:
            probe = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        if probe.returncode == 0 and "Blender" in (probe.stdout + probe.stderr):
            # Do not resolve symlinks: multi-call launchers such as /snap/bin/blender
            # select the application from argv[0]; resolving it to /usr/bin/snap breaks it.
            return str(path)
        reason = (probe.stderr or probe.stdout or f"exit {probe.returncode}").strip()
        failures.append(f"{path}: {reason[-240:]}")
    raise BlenderError("no runnable Blender binary; " + "; ".join(failures))


class BlenderSession:
    """A long-lived headless Blender process holding one scene in memory.

    Snap-Blender note: artifacts must live under $HOME (snap confinement can't see
    /tmp), so the default artifacts dir is created next to the repo, not in /tmp.
    """

    def __init__(
        self,
        blender: str = "blender",
        artifacts_dir: str | Path | None = None,
        blend_file: str | Path | None = None,
        boot_timeout: float = 60.0,
        assets_dir: str | Path | None = None,
        cwd: str | Path | None = None,
    ):
        self.blender = blender
        self.blend_file = str(blend_file) if blend_file else None
        self.boot_timeout = boot_timeout
        self.assets_dir = str(assets_dir) if assets_dir else None
        # the worker must run FROM the shot folder: agents are configured with
        # cwd=shot.folder, so relative paths inside run_bpy ("refs/…", "build/…") have
        # to resolve the same way — otherwise the builder reads the repo root and
        # concludes its own references are missing.
        self.cwd = str(cwd) if cwd else None
        # Renders are throwaway; SNAPSHOTS are not — resume needs to find them again.
        # These used to be mkdtemp(dir=$HOME) per session and were never cleaned: 107
        # dirs and 1.8 GB accumulated in one day, and the .blend checkpoints inside them
        # were unreachable, which is why a failed layer could only be rebuilt from zero.
        self._ephemeral = artifacts_dir is None and cwd is None
        if artifacts_dir is None:
            base = Path(cwd) if cwd else Path.home()
            artifacts_dir = (
                Path(base) / ".artifacts" if cwd else Path(tempfile.mkdtemp(prefix=".bvfx-render-", dir=Path.home()))
            )
        self.artifacts = Path(artifacts_dir)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.snapshots = (Path(cwd) / ".snapshots") if cwd else self.artifacts
        self.snapshots.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen | None = None
        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._ids = itertools.count(1)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> BlenderSession:
        self.blender = resolve_blender(self.blender)
        argv = [self.blender, "--background", "--factory-startup"]
        if self.blend_file:
            argv.append(self.blend_file)
        argv += ["--python", str(_WORKER), "--", "--artifacts", str(self.artifacts)]
        if self.assets_dir:
            argv += ["--assets", self.assets_dir]
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.cwd,
        )
        self._stdout_queue = queue.Queue()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            msg = self._read(timeout=max(0.0, deadline - time.monotonic()))
            if msg is None:
                break
            if msg.get("event") == "ready":
                return self
        stderr = self._stop_and_stderr()
        raise BlenderError("Blender worker did not become ready" + (f": {stderr}" if stderr else ""))

    def _stop_and_stderr(self) -> str:
        if not self.proc:
            return ""
        if self.proc.poll() is None:
            self.proc.kill()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=5)
        if not self.proc.stderr:
            return ""
        with contextlib.suppress(Exception):
            return self.proc.stderr.read()[-1200:].strip()
        return ""

    def _sweep(self, keep: int = 40) -> None:
        """Renders pile up fast; keep the newest and bin the rest. Snapshots are kept —
        they are the resume checkpoints."""
        try:
            pngs = sorted(self.artifacts.glob("*.png"), key=lambda p: p.stat().st_mtime)
            for p in pngs[:-keep]:
                p.unlink(missing_ok=True)
        except OSError as e:
            # Housekeeping, so never fatal — but a sweep that keeps failing means renders
            # accumulate unbounded, and this pipeline has already leaked 1.8 GB into $HOME
            # once by nobody noticing exactly this.
            print(f"! artifact sweep failed ({e}) — renders may accumulate in {self.artifacts}", flush=True)

    def close(self) -> None:
        self._sweep()
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
    def _pump_stdout(self) -> None:
        """Drain stdout continuously so boot timeouts work with buffered text streams."""
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._stdout_queue.put(line)
        self._stdout_queue.put(None)

    def _write(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _read(self, timeout: float | None = None) -> dict | None:
        assert self.proc and self.proc.stdout
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                line = self._stdout_queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            line = line.rstrip("\n")
            if line.startswith(SENT) and line.endswith(SENT) and len(line) > 2 * len(SENT):
                return json.loads(line[len(SENT) : -len(SENT)])

    def call(self, cmd: str, **args) -> dict:
        if not self.proc or self.proc.poll() is not None:
            raise BlenderError("Blender worker is not running")
        rid = next(self._ids)
        self._write({"id": rid, "cmd": cmd, "args": args})
        while True:
            msg = self._read()
            if msg is None:
                stderr = self._stop_and_stderr()
                raise BlenderError(f"worker died during {cmd!r}" + (f": {stderr}" if stderr else ""))
            if msg.get("id") != rid:
                continue  # skip stray events
            if not msg.get("ok"):
                raise BlenderError(msg.get("error", "unknown") + "\n" + msg.get("trace", ""))
            return msg.get("result", {})

    # -- typed convenience wrappers -----------------------------------------
    def ping(self) -> dict:
        return self.call("ping")

    def run(self, code: str, *, journal: bool = True) -> dict:
        """Execute Blender Python; read-only probes can opt out of the replay journal."""
        return self.call("run", code=code, journal=journal)

    def inspect(self, section: str = "all") -> str:
        return self.call("inspect", section=section)["text"]

    def keyframes(self, obj: str) -> str:
        return self.call("keyframes", object=obj)["text"]

    def render(self, frame: int, mode: str = "eevee", scale: float = 0.5, **kw) -> str:
        return self.call("render", frame=frame, mode=mode, scale=scale, **kw)["image_path"]

    def render_full(self, frame: int, mode: str = "eevee", scale: float = 0.5, **kw) -> dict:
        """`render` returns only the path; the Phase-2 modes also return a caption and
        the settings they used, and a caption that never reaches the reader is the
        measured failure mode of every diagnostic render mode."""
        return self.call("render", frame=frame, mode=mode, scale=scale, **kw)

    def check(self, kind: str, **kw) -> dict:
        """Phase 1 — a judgment-free scene check. `kind='self_test'` runs the fixtures."""
        return self.call("check", kind=kind, **kw)

    def diff(self, a: str, b: str, dest: str | None = None) -> dict:
        """|A − B| for two PNGs already rendered.

        Client-side on purpose: Blender's bundled Python has no Pillow, and subtracting
        two files that are already on disk never needed a scene."""
        from .tools import subtract_png

        return subtract_png(a, b, dest or str(self.artifacts / "diff.png"))

    def snapshot(self, tag: str) -> dict:
        """Checkpoint the scene. Returns {blend, journal_index} — the journal index is
        the write-ahead position, so replaying entries after it reconstructs any work
        done between this checkpoint and a crash."""
        return self.call("snapshot", tag=tag, dir=str(self.snapshots))

    def journal(self, path: str | None = None, clear: bool = False) -> dict:
        """Dump/clear the accepted-run_bpy transcript (see worker.h_journal)."""
        return self.call("journal", path=path, clear=clear)

    def replay(self, start: int = 0) -> dict:
        """Re-exec journalled run_bpy calls from `start` (use after restore)."""
        return self.call("replay", start=start)

    def restore(self, blend: str) -> dict:
        """Restore a .blend checkpoint (exact scene state at snapshot time)."""
        return self.call("restore", blend=blend)
