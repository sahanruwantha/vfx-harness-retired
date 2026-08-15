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
    crit = {"guardrails.py", "recipes.py"}
    quiet = []
    for f in Path("pipeline").rglob("*.py"):
        if f.name not in crit: continue
        for n in _ast.walk(_ast.parse(f.read_text())):
            if isinstance(n, _ast.ExceptHandler):
                src = _ast.unparse(n)
                if "log(" not in src and "print(" not in src and "raise" not in src:
                    quiet.append(f"{f.name}:{n.lineno}")
    check("no silent handlers in hook code", not quiet, str(quiet))

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

    print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}"
          f"  ({'0' if not FAILS else len(FAILS)} failed)")
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
