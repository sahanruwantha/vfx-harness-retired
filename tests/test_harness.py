"""Deterministic self-test for the harness. No models, no cost.

Written because an external review pointed out the README claimed a test suite that did
not exist, and because three defects shipped today were caught only by ad-hoc checks —
including one in a fix whose own test covered the failure I imagined rather than the one
I had watched happen.

    .venv/bin/python -m tests.test_harness
"""
from __future__ import annotations

import anyio, json, shutil, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    from pipeline.brief import load_shot
    from pipeline.ledger import Ledger, Milestone, load_layers, load_axes, load_milestones, plan_strips
    from pipeline.build_agent import (_repro_tolerance, _verdict, _prior_layer_paths,
                                      _plan_layer_excerpt, _warn_unowned_axes)
    from pipeline.metrics import compare, look_vector, report
    from pipeline.sandbox import path_sandbox, _relocate
    from pipeline.guardrails import api_guardrails, web_allowlist, metrics_feedback
    from pipeline.script_map import outline, find_lines
    from pipeline.recipes import recipe_index, search_recipes, _all
    from pipeline.escalate import ask, load as lq, answer, answers_block
    from pipeline.shot_context import write_layer_context, clear_layer_context
    from pipeline.blender.tools import build_blender_tools

    shot = load_shot("shots/barrel_roll")
    layers, axes, moments = load_layers(shot), load_axes(shot), load_milestones(shot)

    print("\n[plan artifacts]")
    check("layers load", len(layers) == 8, f"{len(layers)}")
    check("ids are 1..N", [l.id for l in layers.values()] == [str(i) for i in range(1, 9)])
    check("script prefix matches id",
          all(Path(l.script).name.startswith(f"{int(l.id):02d}_") for l in layers.values()))
    check("every layer multi-frame aware", all(len(l.judges) >= 1 for l in layers.values()))
    check("every judge ref exists",
          all((shot.folder / r).is_file() for l in layers.values() for _f, r in l.judges))
    check("plan excerpt resolves for all layers",
          all(len(_plan_layer_excerpt(shot, l)) > 200 for l in layers.values()))
    check("acceptance moments load", len(moments) == 10, f"{len(moments)}")
    check("layers inherit plan strips",
          len(layers["1"].as_milestone(plan_strips(shot)).strip) > 0)
    owned = {a for l in layers.values() for a in l.owns}
    check("every axis owned", {k for k, _ in axes} == owned)
    check("no mute layer", all(l.owns for l in layers.values()))

    print("\n[verdict + tolerance]")
    check("single-axis pass needs min>=3", _verdict({"scores": {"a": 3}})["pass"])
    check("single-axis 2 fails", not _verdict({"scores": {"a": 2}})["pass"])
    check("n/a excluded from mean", _verdict({"scores": {"a": 4, "b": "n/a"}})["mean"] == 4.0)
    check("tolerance capped at 0.5", _repro_tolerance(1) == 0.5, f"{_repro_tolerance(1)}")
    check("full grade drop is NOT reproduced", 3.0 < 4.0 - _repro_tolerance(1))

    print("\n[metrics]")
    ref = str(shot.folder / "refs/f100_city.jpg")
    v = look_vector(ref)
    check("look vector has banded metrics", {"detail_bot", "points_bot", "halation_mid"} <= set(v))
    check("identical images -> no deltas", compare(v, v) == [])
    d = compare({**v, "points_bot": v["points_bot"] * 0.1}, v)
    check("large drop is blocking", any(x.blocking for x in d))
    check("zero-ref does not explode",
          "~0" in str(compare({"halation_mid": 900.0}, {"halation_mid": 0.0})[0]))

    # compare_frame used to force BOTH images to height 512 AFTER the render had already
    # been shrunk by `scale`, so the reference (always full-res) downscaled and stayed
    # sharp while a default scale=0.4 render was UPSCALED 1.33x — every detail metric read
    # soft, and two calls at different scales were not comparable to each other. Measuring
    # an image against ITSELF must therefore give the same answer at every scale.
    from PIL import Image as _I
    from pipeline.blender.tools import _compare_image as _ci
    _src = _I.open(ref).convert("RGB")
    _sigs = set()
    for _s in (0.35, 0.4, 0.6, 1.0):
        _p = Path(tempfile.mkdtemp()) / f"s{_s}.png"
        _src.resize((round(_src.width * _s), round(_src.height * _s)), _I.LANCZOS).save(_p)
        _lines = _ci(str(_p), Path(ref), "x")["content"][0]["text"].splitlines()
        # exposure + per-band structure must not depend on the scale knob
        _sigs.add(_lines[1] + " | " + _lines[2].split("· halation")[0])
    check("metrics are scale-invariant for an image vs itself", len(_sigs) == 1,
          f"{len(_sigs)} variants: {sorted(_sigs)[:2]}")
    _out = _ci(str(shot.folder / "renders/1@f100_canonical_f100.png"), Path(ref), "x")
    check("compare_frame states the SIGNED gap, not just raw values",
          "LOW" in _out["content"][0]["text"] or "HIGH" in _out["content"][0]["text"])

    print("\n[sandbox]")
    async def sb():
        chk = path_sandbox(shot.folder, cwd=shot.folder).hooks[0]
        async def dec(p):
            r = await chk({"tool_name": "Read", "tool_input": {"file_path": p}}, None, None)
            return r.get("hookSpecificOutput", {}).get("permissionDecisionReason", "ALLOW")
        return (await dec("/home/sahan/Desktop/bambi-vfx/refs/f100_city.jpg"),
                await dec("/etc/passwd"),
                await dec(str(shot.folder / "brief.md")))
    wrong, forbidden, ok = anyio.run(sb)
    check("wrong path -> redirect", wrong.startswith("WRONG PATH"))
    check("forbidden path -> hard deny", "outside this agent" in forbidden)
    check("own file allowed", ok == "ALLOW")
    check("no escape via absolute component", _relocate(Path("/etc/passwd"), [shot.folder]) is None)

    print("\n[guardrails]")
    async def gr():
        g = api_guardrails().hooks[0]; w = web_allowlist().hooks[0]
        a = await g({"tool_name": "mcp__blender__run_bpy",
                     "tool_input": {"script": "x.glare_type='B'"}}, None, None)
        b = await g({"tool_name": "mcp__blender__run_bpy",
                     "tool_input": {"script": "bvfx_glare_bloom(0.5,0.6,0.3)"}}, None, None)
        c = await w({"tool_name": "WebFetch",
                     "tool_input": {"url": "https://news.ycombinator.com"}}, None, None)
        d_ = await w({"tool_name": "WebFetch",
                      "tool_input": {"url": "https://docs.blender.org/api/"}}, None, None)
        return a, b, c, d_
    bad, good, offsite, docs = anyio.run(gr)
    check("blocks Blender-4 idiom", bad["hookSpecificOutput"]["permissionDecision"] == "deny")
    check("allows the helper", good == {})
    check("blocks off-domain web", offsite["hookSpecificOutput"]["permissionDecision"] == "deny")
    check("allows Blender docs", docs == {})

    # An error HINT teaches one SESSION; every layer is a fresh process, so bare next()
    # raised StopIteration in one run, was hinted and absorbed, then raised again in the
    # next run. A PreToolUse deny is the only form of help that crosses that boundary.
    from pipeline.guardrails import script_sanity
    _ss = script_sanity().hooks[0]

    def _blocked(code):
        r = anyio.run(_ss, {"tool_name": "mcp__blender__run_bpy",
                            "tool_input": {"script": code}}, None, None)
        return r.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    check("bare next() is blocked before it runs",
          _blocked("n = next(n for n in nt.nodes if n.type=='EMISSION')"))
    check("next(gen, None) is allowed",
          not _blocked("n = next((n for n in nt.nodes if n.type=='X'), None)"))
    check("bare next nested in another call is still caught",
          _blocked("print(len(next(g for g in gs if g)))"))
    check("a variable named next is not a call",
          not _blocked("next = 5\nprint(next)"))
    check("a syntax error never reaches Blender",
          _blocked("for i in range(3)\n    print(i)"))
    check("ordinary scripts pass untouched",
          not _blocked("import bpy\nbpy.ops.mesh.primitive_cube_add()"))

    # Every run_bpy call is a FRESH NAMESPACE, which the system prompt states and the
    # builder ignored: it defined build_window_nodes in one call and used it in the next.
    # Under-reports on purpose — a false positive blocks legitimate work, which is worse
    # than a NameError the builder would see anyway.
    check("a helper from a previous run_bpy call is blocked",
          _blocked("m = build_window_nodes(mat, 8)"))
    check("a helper defined in THIS call is fine",
          not _blocked("def f(a):\n    return a\nx = f(1)"))
    check("injected bvfx helpers are in scope",
          not _blocked("bvfx_emissive_windows(o, density=8)"))
    check("dynamic binding disables the check rather than guessing",
          not _blocked("g = globals()\nmystery_fn(1)"))
    import ast as _a2, glob as _g2
    from pipeline.guardrails import _undefined_names as _un
    _fp = [f for f in _g2.glob("shots/*/build/*.py") + _g2.glob("shots/*/logs/journals/*.py")
           if _un(_a2.parse(Path(f).read_text(encoding="utf-8")))]
    check("no real build script trips the undefined-name check", not _fp, str(_fp[:3]))

    print("\n[script map]")
    tmp = Path(tempfile.mkdtemp()) / "s.py"
    tmp.write_text("import bpy\ndef helper():\n    pass\no = bpy.data.objects.new('tower', None)\n"
                   "m = bpy.data.materials.new('sky_mat')\nx = bpy.data.objects['prior_thing']\n")
    o = outline(tmp)
    check("outline finds created names", "tower" in o and "sky_mat" in o)
    check("outline flags cross-layer refs", "prior_thing" in o)
    check("outline is far cheaper than the file", len(o) < len(tmp.read_text()) * 3)
    check("find_lines locates", "tower" in find_lines(tmp, "tower"))

    print("\n[recipes]")
    recs = _all()
    check("cookbook non-empty", len(recs) >= 20, f"{len(recs)}")
    check("index is compact", len(recipe_index()) // 4 < 1500)
    check("every recipe indexed", all(r["name"] in recipe_index() for r in recs if r["when"]))
    check("search finds by intent", "night-city-field" in
          [h["name"] for h in search_recipes("make the city look real")])

    print("\n[escalation]")
    q = Path(tempfile.mkdtemp())
    qid = ask(q, layer="PLAN", question="aspect?", assumption="2:1")
    ask(q, layer="PLAN", question="aspect?", assumption="2:1")
    check("duplicate suppressed", len(lq(q)) == 1)
    check("open question blocks", not lq(q)[0].get("answer"))
    answer(q, qid, "2:1")
    check("answer becomes law", "2:1" in answers_block(q))

    print("\n[layer context]")
    p = write_layer_context(shot, layers["6"], axes, {195: "mean 0.5"})
    body = p.read_text()
    check("names its layer", "# Layer 6" in body)
    check("lists every judge frame", all(f"f{f}" in body for f, _ in layers["6"].judges))
    check("carries fingerprints", "mean 0.5" in body)
    check("has summary instructions", "Summary instructions" in body)
    clear_layer_context(shot)
    check("clear_layer_context removes it", not (shot.folder / "CLAUDE.md").exists())

    print("\n[tools]")
    class S: pass
    _, names = build_blender_tools(S(), shot_dir=shot.folder, layer_id="1")
    short = [n.split("__")[-1] for n in names]
    check("render_frames registered", "render_frames" in short)
    check("script_map + worklist registered", {"script_map", "worklist"} <= set(short))
    check("ask_supervisor NOT in build tools", "ask_supervisor" not in short)
    import pipeline.blender.tools as T
    check("no undefined _encode", "_encode" not in Path(T.__file__).read_text())

    print("\n[observability]")
    from pipeline.runlog import bump, reset_counts, snapshot_counts, summary
    reset_counts(); bump("sandbox_denied", 2)
    check("hook counters accumulate", snapshot_counts() == {"sandbox_denied": 2})
    s_no = summary({"layer": "1", "title": "t", "status": "passed", "hooks": {}})
    check("silent hooks are flagged", "NOTHING FIRED" in s_no)
    s_nm = summary({"layer": "1", "title": "t", "status": "passed",
                    "hooks": {"sandbox_denied": 1}})
    check("missing metric feedback is flagged", "NO objective metric feedback" in s_nm)
    s_ok = summary({"layer": "1", "title": "t", "status": "passed",
                    "hooks": {"metric_feedback": 9}})
    check("healthy run is not flagged", "NO objective metric" not in s_ok)
    import ast as _ast

    def _mute(handler) -> bool:
        """Does this handler swallow the failure without anyone finding out?

        A handler is NOT mute if it logs, prints, re-raises — or RETURNS the problem to
        its caller, which several legitimately do (an is_error tool result, a "verdict
        INCONCLUSIVE" note, a list of problems). The first version of this check only
        looked for log/print/raise and so counted 53 handlers, most of which were
        reporting perfectly well through their return value. Flagging those trains
        everyone to ignore the check, which costs more than the handlers do.
        """
        src = _ast.unparse(handler)
        if any(k in src for k in ("log(", "print(", "raise", "bump(")):
            return False
        for n in _ast.walk(handler):
            # a return/append carrying an f-string or the exception name is a report
            if isinstance(n, (_ast.Return, _ast.Assign)) and _ast.unparse(n).count("e") \
                    and ("JoinedStr" in str(type(getattr(n, "value", None)))
                         or "is_error" in _ast.unparse(n)
                         or "error" in _ast.unparse(n).lower()):
                return False
        return True

    # The hook path specifically: a hook that dies quietly is how metrics_feedback
    # no-opped for an entire build phase with no trace at all.
    crit = {"guardrails.py", "recipes.py", "sandbox.py", "runlog.py", "layer_state.py"}
    quiet = [f"{f.name}:{n.lineno}"
             for f in Path("pipeline").rglob("*.py") if f.name in crit
             for n in _ast.walk(_ast.parse(f.read_text()))
             if isinstance(n, _ast.ExceptHandler) and _mute(n)]
    check("no silent handlers in hook code", not quiet, str(quiet))

    # The recipe frontmatter guard: enforced at the WRITE, not merely requested in the
    # distiller prompt. Prompt-only, a self-declared `verified: true` lands quietly and
    # fails a LATER suite run on a file nobody in that session meant to write.
    import anyio as _anyio
    from pipeline.guardrails import recipe_write_guard
    _g = recipe_write_guard().hooks[0]

    def _denied(path, body):
        r = _anyio.run(_g, {"tool_name": "Write",
                            "tool_input": {"file_path": path, "content": body}},
                       None, None)
        return r.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    _fm = "---\nname: x\nverified: %s\n---\nbody\n"
    check("recipe claiming verified:true is blocked",
          _denied("pipeline/recipes/x.md", _fm % "true"))
    check("recipe with verified:false is allowed",
          not _denied("pipeline/recipes/x.md", _fm % "false"))
    check("spike scaffolding in a recipe body is blocked",
          _denied("pipeline/recipes/x.md", (_fm % "false") + "SPIKE_ARGS['a']=1\n"))
    check("the guard does not touch non-recipe writes",
          not _denied("shots/b/build/01_layout.py", _fm % "true"))

    # And the handlers that were individually judged to lose information (#7).
    fixed = {"pipeline/blender/session.py": "artifact sweep failed",
             "pipeline/assets/_normalize_bpy.py": "normalize SKIPPED",
             "pipeline/skills.py": "skills: skipping",
             "pipeline/escalate.py": "is not valid JSON and was SKIPPED"}
    missing = [p for p, marker in fixed.items()
               if marker not in Path(p).read_text(encoding="utf-8")]
    check("degradations that lose data announce themselves", not missing, str(missing))

    # The mesh is a reconstruction OF the plate; nothing checked it still looked like it.
    # The cost of not checking was paid three layers later, as a critic demanding a
    # "stepped podium" at every frame with no way to tell whether the mesh lacked one or
    # the render was hiding it. Ratios only — the preview and the plate are framed
    # differently, so absolute widths are not comparable but base-flare/shaft is.
    print("\n[asset fidelity]")
    from pipeline.assets.normalize import compare_to_plate
    _plate = shot.folder / "assets/sr2_tower/isolated/view_0.png"
    _prev = shot.folder / "assets/sr2_tower/preview.png"
    if _plate.is_file() and _prev.is_file():
        _f = compare_to_plate(_plate, _prev)
        check("the committed asset matches its design plate",
              _f["verdict"] == "consistent", f"{_f['verdict']}: {_f['note'][:80]}")
        from PIL import Image as _I2
        _im = _I2.open(_prev).convert("L"); _w, _h = _im.size
        _cut = Path(tempfile.mkdtemp()) / "nopodium.png"
        _im.crop((0, 0, _w, int(_h * 0.66))).resize((_w, _h)).save(_cut)
        check("a mesh that lost its base massing is caught",
              compare_to_plate(_plate, _cut)["verdict"] == "mesh-lost-structure")
    else:
        check("asset fidelity fixture present", False, "plate/preview missing")

    print("\n[ledger]")
    t2 = Path(tempfile.mkdtemp()) / "br"
    shutil.copytree(shot.folder, t2, ignore=shutil.ignore_patterns("refs", "assets", "renders"))
    s2 = load_shot(t2); m1 = Milestone("1", 1, "r", "")
    a, b = Ledger(s2), Ledger(s2)
    a.mark(m1, "passed"); b.mark(Milestone("2", 2, "r", ""), "failed")
    keys = json.loads((t2 / "shot.json").read_text())["milestones"]
    check("concurrent writes do not clobber", {"1", "2"} <= set(keys))
    c = Ledger(s2); c.data["acceptance"] = {"run": 1}; c.save()
    d2 = Ledger(s2); d2.data["acceptance"] = {"run": 2}; d2.save()
    check("top-level key updates persist",
          json.loads((t2 / "shot.json").read_text())["acceptance"] == {"run": 2})

    # ---- BEGIN recipe-verification block ------------------------------------------
    # (added with the verify_recipes rewrite; self-contained, safe to move/merge)
    from pipeline.verify_recipes import (LEDGER, SPIKES, _blocks, _static, audit,
                                         current_sha, load_ledger)

    print("\n[recipe verification]")
    # The bug this whole tool exists to close: a call INSIDE a def is not evidence the def
    # ever ran. An injected 4.x API sat in an uncalled function and was reported "verified".
    defs, called, err = _static("def f(x):\n    bpy.thing()\n\ny = 1\n")
    check("a call inside a def is not a top-level call", (defs, called, err) == (["f"], [], None))
    defs, called, _ = _static("def f(x):\n    pass\n\nf(1)\n")
    check("a real top-level call is seen", (defs, called) == (["f"], ["f"]))
    check("a syntax error is reported, not swallowed", _static("def f(:\n")[2] is not None)

    body = ("prose\n```python\n  a = 1\n  b = 2\n```\n"
            "```python skip\nthis is not code at all\n```\n"
            "```python\nglare.glare_type = 'BLOOM'   # 4.x\n```\n")
    blk = _blocks(body)
    check("indented fences are dedented", blk == ["a = 1\nb = 2\n"], repr(blk))
    check("skip + 4.x contrast fences are not executed", len(blk) == 1)

    recs = _all()
    ledger = load_ledger()
    check("spike ledger exists", LEDGER.is_file(), str(LEDGER))
    rows = audit(recs, ledger)
    liars = [r["name"] for r, entitled, _w in rows if r["verified"] and not entitled]
    check("no recipe claims verification without live spike evidence", not liars, str(liars))
    lying_low = [r["name"] for r, entitled, _w in rows if entitled and not r["verified"]]
    check("no proven recipe is left flagged unverified", not lying_low, str(lying_low))
    check("every ledger entry names a real recipe",
          set(ledger) <= {r["name"] for r in recs}, str(set(ledger) - {r["name"] for r in recs}))
    stale = [n for n, e in ledger.items() if e.get("verdict") == "stale"]
    check("no recipe is stale against this Blender", not stale, str(stale))

    # editing a recipe must invalidate its evidence, or `verified:` decays into a rumour
    if recs and ledger:
        r0 = next((r for r in recs if r["verified"] and _blocks(r["body"])), None)
        if r0:
            tampered = dict(r0, body=r0["body"] + "\n```python\nx = 1\n```\n")
            check("an edited recipe loses its verified claim",
                  not audit([tampered], ledger)[0][1])
            check("code hash changes with the code", current_sha(tampered) != current_sha(r0))

    # scaffolding must not leak into what find_recipe hands the builder
    check("spike fixtures live outside the .md",
          not any(f.suffix == ".md" and f.name != "README.md" for f in SPIKES.glob("*"))
          if SPIKES.is_dir() else True)
    check("no recipe body mentions SPIKE_ARGS",
          not [r["name"] for r in recs if "SPIKE_ARGS" in r["body"]])
    # ---- END recipe-verification block --------------------------------------------

    # ---- BEGIN eval-harness block (pipeline/evals.py + pipeline/eval/) -------------
    # Self-contained, safe to move/merge. Added with A7.
    #
    # These test the INSTRUMENT, never the current state of the repo. A check that
    # asserted "artifact integrity passes for barrel_roll" would encode today's shot
    # folder into the suite: it would fail the moment someone starts a build, and it
    # would say nothing about whether the checker can see a problem. So every checker
    # here is pointed at a fixture whose answer is known by construction. What the repo
    # actually scores is a finding, and findings belong in `python -m pipeline.evals
    # check`, not in a pass/fail suite.
    from pipeline.eval import baseline as EB
    from pipeline.eval import compare as EC
    from pipeline.eval import variance as EV
    from pipeline.eval.determinism import RENDER_SCALES, Result, metric_scale_consistency
    from pipeline.eval.integrity import _problems as integrity_problems

    print("\n[evals · baseline]")
    t3 = Path(tempfile.mkdtemp()) / "br"
    shutil.copytree(shot.folder, t3, ignore=shutil.ignore_patterns(
        ".artifacts", ".snapshots", ".versions", "assets"))
    s3 = load_shot(t3)
    rec = EB.freeze(s3, label="unit", note="fixture")
    check("baseline names its shot and schema",
          rec["shot"] == "barrel_roll" and rec["schema"] == EB.SCHEMA)
    check("baseline carries per-layer telemetry",
          rec["layers"]["1"]["telemetry"]["cost_usd"] is not None)
    check("baseline carries run_id + attempt for pairing",
          "run_id" in rec["layers"]["1"] and "attempt" in rec["layers"]["1"])
    # Assert the PROPERTY (the body is stored verbatim), not a string that happens to be
    # in it. This originally checked for "import bpy" and passed only because one script
    # in the fixture contained it — build scripts do not import bpy, since the harness
    # injects it into the namespace. Moving an unrelated aborted script out of build/
    # removed the coincidence and the test failed while nothing was broken.
    check("baseline stores script bodies, not pointers",
          bool(rec["scripts"]) and all(
              s.get("text") == (t3 / name).read_text(encoding="utf-8") and s.get("text")
              for name, s in rec["scripts"].items()
              if (t3 / name).is_file()))
    check("baseline hashes every judged render",
          all(v for v in rec["renders"].values()) and len(rec["renders"]) > 0)
    # Absent acceptance must read as absent, never as zero: "0/10 passed" and "never
    # judged" support opposite conclusions and conflating them turns a partial build
    # into a quality regression.
    check("missing acceptance is 'unavailable', not 0",
          rec["final"]["present"] is False and "why" in rec["final"])
    check("baseline records code identity", rec["git"]["commit"] != "")
    (t3 / "renders").mkdir(exist_ok=True)
    (t3 / "renders" / "zzz_new.png").write_bytes(b"not really a png")
    rec2 = EB.freeze(s3, label="unit2")
    check("a new render shows up in a re-freeze",
          "renders/zzz_new.png" in rec2["renders"]
          and "renders/zzz_new.png" not in rec["renders"])

    print("\n[evals · integrity]")
    # Fixture with three planted defects, one of each class the checker claims to find.
    t4 = Path(tempfile.mkdtemp()) / "br"
    shutil.copytree(shot.folder, t4, ignore=shutil.ignore_patterns(
        ".artifacts", ".snapshots", ".versions", "assets"))
    led = json.loads((t4 / "shot.json").read_text())
    led["milestones"]["1"]["rounds"][0]["render"] = "renders/vanished.png"
    # Plant the defect explicitly rather than relying on layer 3 happening to have no
    # script. It had none this morning and has one now, so the fixture silently stopped
    # planting anything and the check passed on repo state instead of on the property.
    led["milestones"]["3"] = {"status": "passed", "rounds": [{"round": 1, "render": ""}]}
    for _s in (t4 / "build").glob("03_*.py"):
        _s.unlink()
    (t4 / "build" / "99_experiment.py").write_text("# stray\n")
    (t4 / "shot.json").write_text(json.dumps(led, indent=2))
    errs, warns, data = integrity_problems(load_shot(t4))
    check("a vanished judged render is an ERROR",
          any("vanished.png" in e for e in errs), str(errs))
    check("a 'passed' layer with no script on disk is an ERROR",
          any("layer 3" in e and "03_city.py" in e for e in errs), str(errs))
    check("an orphan build script is reported",
          "99_experiment.py" in str(warns) or "99_experiment.py" in str(errs))
    check("orphans are listed in the data, not just prose",
          "99_experiment.py" in data.get("orphan_scripts", []))
    clean_errs, _w, _d = integrity_problems(load_shot(t3))
    check("the checker does not invent errors on an untouched shot",
          not clean_errs, str(clean_errs))

    print("\n[evals · metric self-consistency]")
    # The one invariant that holds both before and after the in-flight compare_frame fix:
    # scale 1.0 is the identity, so it cannot disagree with itself. Whether the SUB-1.0
    # scales agree is the open finding this check exists to report — asserting either
    # answer here would bake today's bug (or tomorrow's fix) into the suite.
    r_ident = metric_scale_consistency([Path(ref)], scales=(1.0,))
    check("identity scale is self-consistent", r_ident.ok is True, r_ident.detail)
    check("no images -> SKIP, not PASS",
          metric_scale_consistency([Path("/nonexistent.png")]).ok is None)
    # The sweep must model the PIPELINE's geometry: a render is scale × the delivery
    # resolution. The first version of this check swept already-downscaled 960x480
    # stashed renders by a further 0.25 — a scale of 0.125 that nothing renders at — and
    # reported "11 blocking failures". Overstating a finding is the same defect as
    # missing one, so a plate that cannot serve as a full-quality control is SKIPPED.
    small = Path(tempfile.mkdtemp()) / "half.png"
    from PIL import Image as _PILImage
    _PILImage.open(ref).resize((960, 480)).save(small)
    r_small = metric_scale_consistency([small], delivery=(1920, 960))
    check("an under-resolution plate is skipped, not judged",
          r_small.ok is None and "960x480" in r_small.detail, r_small.detail[:120])
    r_mixed = metric_scale_consistency([Path(ref), small], delivery=(1920, 960))
    check("the full-res plate is still swept when a small one is dropped",
          r_mixed.ok is not None and "half.png" in str(r_mixed.data.get("skipped")))
    r_all = metric_scale_consistency([Path(ref)], scales=RENDER_SCALES,
                                     delivery=shot.resolution)
    print(f"    (informational, not a check) {Path(ref).name} across "
          f"{list(RENDER_SCALES)}: {r_all.state} — {r_all.detail.splitlines()[0][:110]}")
    check("a skipped check never reads as a pass",
          Result("x", ok=None, detail="").state == "SKIP")

    print("\n[evals · statistics]")
    # The whole point of the compare stage is refusing to call small differences results.
    check("no discordant pairs -> p=1", EC.sign_test_p(0, 0) == 1.0)
    check("one flip is not significant", EC.sign_test_p(1, 0) > 0.05)
    check("5 one-way flips still are not", EC.sign_test_p(5, 0) > 0.05)
    check("6 one-way flips are", EC.sign_test_p(6, 0) <= 0.05)
    check("the design states its own resolution",
          EC.min_discordant_for_significance() == 6,
          str(EC.min_discordant_for_significance()))
    check("the test is symmetric", EC.sign_test_p(2, 5) == EC.sign_test_p(5, 2))

    print("\n[evals · compare]")
    import copy as _copy
    ba = EB.freeze(s3, label="A")
    bb = _copy.deepcopy(ba); bb["label"] = "B"
    txt = EC.report(ba, bb)
    check("final task success leads the report",
          txt.index("PRIMARY: FINAL TASK SUCCESS")
          < txt.index("layer verdicts") < txt.index("telemetry only"))
    check("no acceptance -> the primary statistic is UNAVAILABLE", "UNAVAILABLE" in txt)
    check("secondary statistics are labelled gameable", "GAMEABLE" in txt)
    check("cost is labelled telemetry, not quality",
          "NEVER a quality argument" in txt)

    def _acc(flags):
        return {"present": True, "passed": sum(flags.values()), "total": len(flags),
                "moments": {k: {"frame": 1, "pass": v, "critic_pass": v, "mean": 3.5,
                                "decided_by": "critic", "metric_failures": [],
                                "scores": {}, "render": "", "render_sha": None}
                            for k, v in flags.items()}}
    ids = [f"M{i}" for i in range(1, 11)]
    ba["final"] = _acc({m: True for m in ids})
    one = dict.fromkeys(ids, True); one["M9"] = False
    bb["final"] = _acc(one)
    txt1 = EC.report(ba, bb)
    check("a single moment flip is reported as noise, not a delta",
          "WITHIN NOISE" in txt1 and "NOT a result" in txt1)
    big = dict.fromkeys(ids, True)
    for m in ids[:7]:
        big[m] = False
    bb["final"] = _acc(big)
    txt2 = EC.report(ba, bb)
    check("a 7-moment regression IS called a regression",
          "REGRESSED" in txt2 and "WITHIN NOISE" not in txt2)
    bb["final"] = _acc({m: True for m in ids[:5]})
    txt3 = EC.report(ba, bb)
    check("a changed acceptance suite is flagged as unpaired",
          "acceptance suites DIFFER" in txt3)
    ba["shot"], bb["shot"] = "a", "b"
    check("comparing two different shots is refused loudly",
          "DIFFERENT SHOTS" in EC.report(ba, bb))

    print("\n[evals · variance]")
    d = EV._dispersion([2.0, 4.0, 3.0, 3.0])
    check("dispersion reports the spread that flips verdicts",
          d["spread"] == 2.0 and d["median"] == 3.0)
    fake = {"at": "2026-01-01T00:00:00+00:00", "shot": "x", "render": "r", "ref": "f",
            "layer": "1", "scope": "layer", "n": 3, "critic_model": "m",
            "motion_strip": False, "axes_in_rubric": ["a"],
            "per_axis": {"a": {**EV._dispersion([2.0, 3.0, 4.0]), "n_a": 0,
                               "scope_unstable": False}},
            "mean": EV._dispersion([2.0, 3.0, 4.0]), "verdicts": [],
            "pass_count": 2, "flip_rate": 0.33, "unanimous": False,
            "thresholds": {"PASS_MEAN": 3.1, "PASS_MIN": 2, "ADJUDICATE_BAND": 0.4},
            "band_evidence": 1.0, "enough_for_band": False}
    rep = EV.report(fake)
    check("a low-N run refuses to recommend a band",
          "TOO SMALL TO RECOMMEND A BAND" in rep)
    check("the no-motion-strip proxy is declared in the output", "NO strip" in rep)
    fake["n"], fake["enough_for_band"] = 12, True
    check("a sufficient-N run quotes a band AND its caveat",
          "should be AT LEAST" in EV.report(fake)
          and "ONE render/reference pair" in EV.report(fake))
    # Drift tripwire: variance.layer_scope MIRRORS the scope block build_layer builds
    # inline. If build_agent's wording moves, the eval silently starts measuring the
    # critic under a prompt production never sends.
    ba_src = Path("pipeline/build_agent.py").read_text(encoding="utf-8")
    sc = EV.layer_scope(shot, layers["1"])
    check("layer scope mirrors build_agent's block",
          all(mark in ba_src and mark in sc
              for mark in ("THIS LAYER OWNS:", "one build stage of many")), sc[:120])
    check("layer scope names the layer's own axes",
          all(a in sc for a in layers["1"].owns))
    # ---- END eval-harness block ---------------------------------------------------

    print("\n[canonical repair · what a round achieved]")
    from pipeline.build_agent import _repair_delta

    def vs(*frames):
        """(frame, mean, pass) triples -> the verdict shape _repair_delta consumes."""
        return [((f, f"r{f}.png"), {"mean": m, "pass": p}) for f, m, p in frames]

    # THE LAYER-2 CASE, and the reason this function exists. The improving frame is NOT
    # the binding one: f440 lifts 2.0 -> 2.75 while f200 sits at 1.0. The old test summed
    # the failing frames, saw the total rise, and bought another repair round — but the
    # layer passes only when its WORST frame clears the bar, and that frame did not move.
    # (An earlier draft of this test used the worst frame as the improving one, which is
    # genuine progress and rightly returns True. The defect needs a non-binding frame.)
    pre = vs((45, 3.0, True), (100, 3.0, True), (200, 1.0, False), (440, 2.0, False))
    post = vs((45, 3.0, True), (100, 3.0, True), (200, 1.0, False), (440, 2.75, False))
    d = _repair_delta(pre, post)
    check("a rising SUM with a static worst frame is not progress",
          not d["progressed"], f"worst {d['was_worst']}->{d['now_worst']}")
    check("  ... and the sum really did rise (the old test would have passed it)",
          sum(d["now"][f] for f in (200, 440)) > sum(d["was"][f] for f in (200, 440)))

    # The worst frame moving IS progress, even when the total is unchanged.
    d = _repair_delta(
        vs((200, 1.0, False), (440, 3.0, False)),
        vs((200, 2.0, False), (440, 2.0, False)))
    check("the worst failing frame improving is progress", d["progressed"],
          f"worst {d['was_worst']}->{d['now_worst']}")

    # So is clearing a frame outright, even if the remaining worst is untouched.
    d = _repair_delta(
        vs((200, 2.0, False), (440, 2.0, False)),
        vs((200, 3.0, True), (440, 2.0, False)))
    check("one fewer failing frame is progress", d["progressed"],
          f"{d['was_failing']}->{d['now_failing']} failing")

    # Regression detection is independent of progress: a repair can lift the worst
    # failing frame AND break a passing one. The caller reverts on `broke` first.
    d = _repair_delta(
        vs((45, 4.0, True), (440, 1.0, False)),
        vs((45, 2.0, False), (440, 3.0, True)))
    check("a trade against a passing frame is reported as broken", d["broke"] == [45],
          str(d["broke"]))

    check("nothing broken when every passing frame holds",
          _repair_delta(vs((45, 4.0, True), (440, 1.0, False)),
                        vs((45, 4.0, True), (440, 2.0, False)))["broke"] == [])

    # A frame absent from the post-repair verdicts has no score to compare. Scoring its
    # absence as 0 would read as a regression and mask the real state.
    d = _repair_delta(vs((200, 2.0, False), (440, 2.0, False)), vs((200, 3.0, True)))
    check("a frame missing after repair is not scored as zero",
          d["now_worst"] == 3.0 and d["broke"] == [], f"worst {d['now_worst']}")

    print("\n[facade profile]")
    from pipeline.facade import facade_profile, compare_profiles
    from PIL import Image as _Im

    def synth(path, strips, w=200, h=600, bg=255, body=40, win=250):
        """A synthetic tower: `strips` are (x0,x1) fractions of the shaft that are lit."""
        im = _Im.new("L", (w, h), bg)
        px = im.load()
        sx0, sx1 = int(0.25 * w), int(0.75 * w)
        for y in range(int(0.05 * h), int(0.95 * h)):
            for x in range(sx0, sx1):
                f = (x - sx0) / (sx1 - sx0)
                lit = any(a <= f <= b for a, b in strips) and (y // 6) % 2 == 0
                px[x, y] = win if lit else body
        im.save(path)

    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        outer_p = Path(td) / "outer.png"
        centre_p = Path(td) / "centre.png"
        # OUTER strips against a dark core -- the plate's polarity.
        synth(outer_p, [(0.02, 0.22), (0.78, 0.98)])
        # The inverse: bright centre, dark edges. This is what layer 1 actually renders,
        # and the instrument exists to tell these two apart by number.
        synth(centre_p, [(0.40, 0.60)])
        o = facade_profile(outer_p)
        c = facade_profile(centre_p)
        check("outer-strip tower reads a high outer/core ratio",
              o["outer_core_ratio"] > 3.0, str(o["outer_core_ratio"]))
        check("centre-lit tower reads a low outer/core ratio",
              c["outer_core_ratio"] < 1.0, str(c["outer_core_ratio"]))
        check("the two polarities are far apart in profile L1",
              compare_profiles(c, o)["l1"] > 0.3, str(compare_profiles(c, o)["l1"]))
        check("a profile compared with itself is identical",
              compare_profiles(o, o)["l1"] == 0.0)
        check("shaft width excludes the background",
              abs(o["shaft_px"] - 100) <= 4, str(o["shaft_px"]))

        # Segmentation must FAIL LOUDLY rather than profile the backdrop. A hardcoded
        # 240 threshold silently classified a whole 720px turntable render as subject,
        # and every angle then returned an identical "measurement".
        flat = Path(td) / "flat.png"
        _Im.new("L", (200, 600), 211).save(flat)
        try:
            r = facade_profile(flat)
            check("a frame with no separable subject is flagged, not silently profiled",
                  bool(r.get("warning")), str(r.get("warning")))
        except ValueError:
            check("a frame with no separable subject is flagged, not silently profiled",
                  True)

        # The backdrop is segmented by its ACTUAL value, not a constant: a 211-grey
        # backdrop must still yield a ~100px shaft, which the old threshold could not do.
        grey_bg = Path(td) / "greybg.png"
        synth(grey_bg, [(0.02, 0.22), (0.78, 0.98)], bg=211)
        g = facade_profile(grey_bg)
        check("segments against a non-white backdrop (211, not 255)",
              abs(g["shaft_px"] - 100) <= 4 and not g.get("warning"),
              f"shaft {g['shaft_px']} warn {g.get('warning')}")

    print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}"
          f"  ({'0' if not FAILS else len(FAILS)} failed)")
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
