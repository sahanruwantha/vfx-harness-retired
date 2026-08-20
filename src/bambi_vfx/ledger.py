"""shot.json — the milestone ledger.

No git history: a deterministic `build/<milestone>.py` per milestone is the artifact,
and this file is the *state* — which milestones passed, the critic verdict that
gated them, and the script + render that produced each. Downstream stages and
resumed sessions read the ledger to know what's done.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .brief import Shot
from .runid import RUN_ID


@dataclass(frozen=True)
class Milestone:
    """One non-negotiable state-change frame the critic layers on (from brief.md)."""

    id: str
    frame: int
    ref: str  # path relative to the shot folder, e.g. "refs/M1_green.jpg"
    reads: str  # the state that MUST read at this frame
    strip: tuple[int, ...] = ()   # extra frames judged with it (motion/continuity)
    fingerprint: str = ""         # the plan's measured expectation for this moment


# Fallback critic axes. Per shot, the real axes are DERIVED from the brief + refs and
# cached at shot/critic_axes.json (see build_agent.ensure_axes) — that's what makes the
# critic generalise to any scene/style. These defaults are only used if none exist.
DEFAULT_AXES: list[tuple[str, str]] = [
    ("composition", "Framing, silhouette, camera angle and subject placement match the ref."),
    ("atmosphere", "Volumetric depth / haze / lighting mood match the ref — not a flat CG void."),
    ("subject_detail", "The hero subject reads with the ref's level of form and detail."),
    ("environment", "The surrounding environment/ground reads as in the ref."),
    ("palette", "Colours, contrast and tone match the ref."),
    ("finish", "Post/grade/bloom/motion cues match the ref's finish."),
]


def load_axes(shot: Shot) -> list[tuple[str, str]]:
    """The critic rubric for this shot: shot/critic_axes.json if present, else defaults.
    Stored as a list of {"key","desc"} objects."""
    path = shot.folder / "critic_axes.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            axes = [(a["key"], a["desc"]) for a in data if a.get("key") and a.get("desc")]
            if axes:
                return axes
        except Exception as e:
            # falling back to GENERIC axes silently means the whole shot is judged on
            # the wrong rubric and every score becomes uninterpretable
            print(f"! critic_axes.json unreadable — using generic defaults ({str(e)[:50]})",
                  flush=True)
    return DEFAULT_AXES

@dataclass(frozen=True)
class Layer:
    """One build layer from the plan (build ORDER), judged at a primary frame/ref on the
    axes it OWNS. Acceptance moments are a separate, time-ordered list (acceptance.json)
    judged once over the finished chain — a moment belongs to the cumulative chain, not
    to any single layer."""

    id: str
    script: str  # e.g. "build/20_green.py" — chained in numeric order
    title: str
    # EVERY frame this layer answers for, ((frame, ref), …) in frame order. A layer's
    # responsibility is multi-frame while its judgment used to be single-frame: SH's G50
    # declared "path at f300 AND underfoot at f368", was judged only at f300, and shipped
    # a path scoring 4 there and 2 at f368. A frame not listed here is never checked.
    judges: tuple[tuple[int, str], ...]
    reads: str
    # The rubric axes this layer is ANSWERABLE for. The critic scores only these and
    # marks the rest n/a — a layout layer cannot earn the finish grade, and judging it
    # on one only produces a floor score it can never lift (BR layer G: 2.83 twice).
    owns: tuple[str, ...] = ()

    @property
    def judge_frame(self) -> int:
        """Primary judge frame (earliest) — what the iteration loop renders."""
        return self.judges[0][0]

    @property
    def judge_ref(self) -> str:
        return self.judges[0][1]

    def as_milestone(self, strips: dict[int, tuple[int, ...]] | None = None) -> Milestone:
        """The critic loop speaks Milestone — adapt the layer's PRIMARY judge point.

        `strips` maps frame -> the plan's strip for the moment at that frame. Without it
        the motion strip falls back to frame/+6/+12, which only looks FORWARD: barrel_roll
        judged M2 at f20 (correctly near-black) while f16-f18 sat at mean ~57 with the
        world-swap in plain view, and the plan's own [12,16,18,20,22] was never used.
        """
        return Milestone(self.id, self.judge_frame, self.judge_ref, self.reads,
                         (strips or {}).get(self.judge_frame, ()))

    def milestone_at(self, frame: int, ref: str,
                     strips: dict[int, tuple[int, ...]] | None = None) -> Milestone:
        """A Milestone for one of this layer's judge points (id tagged with the frame)."""
        return Milestone(f"{self.id}@f{frame}", frame, ref, self.reads,
                         (strips or {}).get(frame, ()))


def load_layers(shot: Shot) -> dict[str, Layer]:
    """Per-shot build layers from shots/<id>/layers.json (written by the PLAN stage)."""
    path = shot.folder / "layers.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} missing — run the plan agent first (its layers define the build "
            f"order; milestones.json defines the acceptance moments)")
    out: dict[str, Layer] = {}
    for g in json.loads(path.read_text()):
        j = g["judge"]
        entries = [j] if isinstance(j, dict) else list(j)   # accept the old single form
        if not entries:
            raise ValueError(f"layer {g['id']}: empty judge list")
        judges = tuple(sorted(((int(e["frame"]), e["ref"]) for e in entries),
                              key=lambda fr: fr[0]))
        out[g["id"]] = Layer(g["id"], g["script"], g.get("title", g["id"]),
                            judges, g.get("reads", ""), tuple(g.get("owns", ())))
    return out


def load_milestones(shot: Shot) -> dict[str, Milestone]:
    """The acceptance suite (plan §4), in TIME order — judged ONCE over the finished
    chain by the accept stage.

    Deliberately NOT derived from layers. Build order (layer) and acceptance order (time)
    are different orderings and are allowed to disagree: server_to_hansa builds
    typography (M2 @ f184) before studio light (M1 @ f72). Attributing a whole-frame
    moment to one additive layer is what made layers get judged on work they don't own.
    """
    path = shot.folder / "acceptance.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} missing — run the plan agent (it writes layers.json for the build "
            f"order and acceptance.json for the approval moments)")
    out: dict[str, Milestone] = {}
    for m in json.loads(path.read_text()):
        out[m["id"]] = Milestone(m["id"], int(m["frame"]), m["ref"], m.get("reads", ""),
                                 tuple(m.get("strip", ())), m.get("fingerprint", ""))
    return dict(sorted(out.items(), key=lambda kv: out[kv[0]].frame))


def plan_strips(shot: Shot) -> dict[int, tuple[int, ...]]:
    """frame -> the plan's strip for the acceptance moment at that frame (if any)."""
    try:
        return {m.frame: m.strip for m in load_milestones(shot).values() if m.strip}
    except Exception as e:
        # the default strip only looks FORWARD, which is precisely how barrel_roll
        # judged f20 and never saw the broken f16-f18 beside it
        print(f"! plan strips unavailable — motion strips fall back to the default "
              f"({str(e)[:60]})", flush=True)
        return {}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def _locked(path: Path):
    """Exclusive lock on a sidecar, so two processes cannot interleave a read-merge-write."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(path: Path, text: str) -> None:
    """Publish by rename: a reader sees the old file or the new one, never a half file."""
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Ledger:
    """Read/modify/write `shot.json` for one shot."""

    def __init__(self, shot: Shot):
        self.shot = shot
        self.path = shot.folder / "shot.json"
        if self.path.is_file():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"shot": shot.id, "milestones": {}}
        self._touched: set[str] = set()  # layers THIS instance owns (see save)
        # snapshot of what we loaded, so save() can tell "I changed this" from
        # "I never looked at it" for non-milestone top-level keys (e.g. acceptance)
        self._loaded = json.loads(json.dumps(self.data))

    # -- accessors -----------------------------------------------------------
    def _slot(self, m: Milestone) -> dict:
        self._touched.add(m.id)
        return self.data.setdefault("milestones", {}).setdefault(m.id, {})

    def status(self, m: Milestone) -> str:
        return self._slot(m).get("status", "pending")

    # -- mutations -----------------------------------------------------------
    def begin(self, m: Milestone) -> None:
        slot = self._slot(m)
        # Which run and which attempt produced this. Without it, re-running a layer
        # overwrites the previous verdict leaving no trace that an earlier attempt
        # existed, let alone what it scored — so "did the change help?" is unanswerable.
        # The script name comes from the PLAN, not from the layer id. Deriving it as
        # build/<id>.py recorded "build/1.py" for a layer whose script is
        # build/01_layout.py and has never existed under any other name. Nothing broke,
        # because chaining reads layers.json — which is exactly why it went unnoticed:
        # the ledger's own field was misinformation that no code depended on, so it would
        # only ever mislead a human reading the record.
        script = slot.get("script")
        if not script:
            try:
                script = load_layers(self.shot)[m.id].script
            except (KeyError, FileNotFoundError, json.JSONDecodeError):
                script = f"build/{m.id.lower()}.py"   # milestone with no plan layer
        previous_attempt = int(slot.get("attempt", 0))
        if previous_attempt:
            slot.setdefault("history", []).append({
                "run_id": slot.get("run_id"),
                "attempt": previous_attempt,
                "status": slot.get("status"),
                "started": slot.get("started"),
                "updated": slot.get("updated"),
                "rounds": list(slot.get("rounds") or []),
                "best": slot.get("best"),
                "script_sha": slot.get("script_sha"),
            })
        # ``rounds`` is current-attempt state.  Historical rounds have their own durable
        # records above; retaining them here made attempt 5's report look like seven new
        # rounds and contaminated convergence analysis with unrelated runs.
        slot.update(frame=m.frame, ref=m.ref, status="in_progress",
                    script=script, rounds=[], reviews=[], ablation={}, resume=None,
                    run_id=RUN_ID, attempt=previous_attempt + 1,
                    started=_now())
        self.save()

    def record_round(self, m: Milestone, *, kind: str, index: int,
                     render: str, verdict: dict) -> None:
        """Append one critic round (kind='iter' during the loop, 'canonical' for the
        deterministic re-run of the build script)."""
        slot = self._slot(m)
        slot.setdefault("rounds", []).append({
            "round": index,
            "kind": kind,
            "render": render,
            "scores": verdict.get("scores", {}),
            "mean": verdict.get("mean"),
            "pass": verdict.get("pass", False),
            "round_s": verdict.get("round_s"),  # wall-time telemetry (for eval)
            "issues": verdict.get("issues", []),
            "contradicted_issues": verdict.get("contradicted_issues", []),
            "judge_conflict": bool(verdict.get("judge_conflict")),
            "evidence": verdict.get("evidence", []),
            "focus_requested": verdict.get("focus_requested", []),
            "focus_panels": verdict.get("focus_panels", []),
            "focus_error": verdict.get("focus_error"),
            "reproduction": verdict.get("reproduction"),
            "decided_by": verdict.get("decided_by", "critic"),
            # what the adjudication panel saw, when one was convened — a barely-passed
            # verdict must be distinguishable from a solid one after the fact
            "panel": verdict.get("panel"),
            "run_id": RUN_ID,
            "attempt": slot.get("attempt"),
            "at": _now(),
        })
        self.save()

    def script_digest(self, m: Milestone) -> str | None:
        """Hash of this layer's build script as it stands on disk right now."""
        try:
            rel = self._slot(m).get("script") or load_layers(self.shot)[m.id].script
        except Exception:
            return None
        p = self.shot.folder / rel
        if not p.is_file():
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]

    def stale(self, m: Milestone) -> str | None:
        """Why this layer's recorded verdict no longer describes its script, if so.

        A verdict is about a SCRIPT, not about a layer id. Edit the script afterwards and
        the ledger still reads `passed` while the thing that passed no longer exists —
        and every layer chained on top builds against renders of code that is gone.

        This happened for real: 01_layout.py was changed to stop overwriting the asset's
        baked facade, and nothing noticed that layer 1's `passed` and its canonical
        renders now described the previous script. provenance.py already does exactly
        this for PLAN artifacts against brief.md; build scripts had no equivalent, which
        is the same gap one level down.
        """
        slot = self._slot(m)
        if slot.get("status") != "passed":
            return None
        was, now = slot.get("script_sha"), self.script_digest(m)
        if was and now and was != now:
            return (f"layer {m.id} is recorded as passed against script_sha {was}, but "
                    f"{slot.get('script')} now hashes to {now}. That verdict and the "
                    f"canonical renders under renders/{m.id}@* describe the OLD script — "
                    f"re-verify before chaining onto it.")
        return None          # no recorded digest = predates this check; nothing to compare

    def mark(self, m: Milestone, status: str, best: dict | None = None) -> None:
        slot = self._slot(m)
        slot["status"] = status
        slot["updated"] = _now()
        # Bind the verdict to the exact script it was reached on, so a later edit becomes
        # detectable instead of silently inheriting the pass.
        d = self.script_digest(m)
        if d:
            slot["script_sha"] = d
        if best is not None:
            slot["best"] = {"round": best.get("round"), "mean": best.get("mean"),
                            "render": best.get("render")}
        self.save()

    def set_resume(self, m: Milestone, *, session_id: str | None, blend: str,
                   journal_index: int, round: int) -> None:
        """Record where a crashed/truncated layer can pick up: the SDK session to resume
        AND the scene checkpoint to restore. Layer G's re-run cost ~$14 and 40 minutes
        rebuilding work it had already done, because neither was ever written down."""
        self._slot(m)["resume"] = {"session_id": session_id, "blend": blend,
                                   "journal_index": journal_index, "round": round,
                                   "at": _now()}
        self.save()

    def get_resume(self, m: Milestone) -> dict | None:
        r = self._slot(m).get("resume")
        return r if r and Path(r.get("blend", "")).is_file() else None

    def snapshot_scripts(self, m: Milestone, tag: str = "pass") -> str | None:
        """Copy build/*.py aside whenever a layer lands.

        Not git ceremony — just enough history to undo. Layer scripts reference each
        other's objects by NAME, so re-running an early layer can silently invalidate
        every later one: rebuilding 20_green.py broke 30_purple.py's `tower_dot` lookup,
        and the chain survived only because backups had been taken BY HAND, twice.
        """
        src = self.shot.folder / "build"
        if not src.is_dir():
            return None
        stamp = _now().replace(":", "").replace("-", "")[:15]
        dst = self.shot.folder / ".versions" / f"{m.id}_{tag}_{stamp}"
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for p in sorted(src.glob("[0-9]*.py")):
            (dst / p.name).write_bytes(p.read_bytes())
            n += 1
        self._slot(m).setdefault("versions", []).append(
            {"tag": tag, "path": str(dst.relative_to(self.shot.folder)),
             "scripts": n, "at": _now()})
        self.save()
        return str(dst)

    def record_ablation(self, m: Milestone, abl: dict) -> None:
        """Did this layer's script move anything at its own judge frame?"""
        self._slot(m)["ablation"] = {**abl, "at": _now()}
        self.save()

    def record_review(self, m: Milestone, round: int, out: dict) -> None:
        """Keep approach reviews: a REPLACE verdict is a negative result worth carrying —
        it says a whole technique could not reach the reference here."""
        self._slot(m).setdefault("reviews", []).append(
            {"round": round, "replace": out.get("replace", False),
             "text": out.get("text", "")[:1200], "at": _now()})
        self.save()

    def save(self) -> None:
        """Write back only the layers this instance touched.

        A shot's layers run as separate processes (and can overlap with an out-of-band
        edit), each holding a snapshot taken at construction. Rewriting the whole
        snapshot would silently revert everyone else's work, so re-read and splice.

        The read-merge-write is done under an exclusive file lock and published with an
        atomic rename. The merge alone only protects concurrent writers WITHIN a process:
        two processes could still interleave between the re-read and the write, and a
        crash mid-write could leave a truncated shot.json that later stages parse as a
        shot with no recorded layers.
        """
        with _locked(self.path):
            on_disk = {}
            if self.path.is_file():
                try:
                    on_disk = json.loads(self.path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    # treating a corrupt ledger as empty would let this write clobber
                    # every layer recorded so far
                    print(f"! shot.json unreadable on merge, NOT clobbering ({e})",
                          flush=True)
                    raise
            # Disk wins for top-level keys we never modified; ours wins where we did.
            # (Plain `{**self.data, **on_disk}` let disk clobber our own new keys, so a
            # second acceptance run silently kept the first run's block.)
            merged = dict(on_disk)
            for k, v in self.data.items():
                if k == "milestones":
                    continue
                if k not in on_disk or v != self._loaded.get(k):
                    merged[k] = v
            slots = dict(on_disk.get("milestones", {}))
            for gid in self._touched:
                slots[gid] = self.data.get("milestones", {}).get(gid, {})
            merged["milestones"] = slots
            merged.setdefault("runs", [])
            if RUN_ID not in merged["runs"]:
                merged["runs"] = (merged["runs"] + [RUN_ID])[-20:]
            _atomic_write(self.path, json.dumps(merged, indent=2) + "\n")
