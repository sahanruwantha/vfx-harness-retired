---
id: HIR-0156
title: Plan-workspace path miss enumerates staged relative reads
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: plan_workspace_path_miss_is_one_sided
mechanism: staged_relative_read_card
adr: ADR-0004
---

# Plan-workspace path miss enumerates staged relative reads

## Observed failure

The verify pass of run `20260830T141150Z-77bb73` is bounded to six turns. Its first
three Reads targeted `brief.md`, `plans/global.draft.md`, and
`ownership_mapping.json` under an unrelated absolute project prefix. The PreToolUse
denial named the isolated workspace root and said to use "the staged brief, refs, and
current candidate," but did not list the live relative files. The session recovered
only after Glob of the workspace absolute path, then relative Reads.

Kickoff already asked for `brief.md` as a relative name. The model still invented
another filesystem root because the miss did not enumerate what actually exists in
the cwd. That is half a verify budget spent rediscovering the staging boundary.

The same kickoff also claimed "there is no source video." That is true of the isolated
workspace (video tools register only `refs/*.mp4`) and false as a statement about the
shot folder. A clip outside `refs/` is not planning input; claiming ontological
absence teaches the wrong fact.

## Root cause

`_path_denial` reported the requested path and the workspace root, then discarded the
staged tree it already confined the session to. The verifier had no two-sided miss
(HIR-0018): requested versus present. Compiled kickoff named relative files in prose
but did not compile the live workspace card, so an off-workspace absolute prefix was
cheaper than Glob.

## Decision criteria

- A denied plan-workspace Read remains fail-closed; the foreign path is never opened.
- The rejection names the requested path and the staged relative files in that cwd.
- Kickoff compiles the same relative-read card so the first legal action is not a guess.
- Stills remain the staged visual target; video tools still register only for clips
  inside `refs/`. Isolation does not change.
- The mechanism must not encode a shot id, foreign project path, or fixed file count.

## General mechanism

`staged_relative_reads` lists the live files under the isolated plan workspace.
`_path_denial` includes that list and forbids prefixing another filesystem root.
Draft, verify, and repair kickoffs compile `plan_workspace_read_card` with the first
relative names for that role. `_refs_block` states the staging rule instead of claiming
that no source video exists.

## Rejected patch-level alternatives

- Raise `VFXH_PLAN_VERIFY_MAX_TURNS` so three denied Reads still leave a full audit:
  enlarging the session papers over a missing enumeration.
- Prompt-only "use relative paths" (already in `PLANNER_SYSTEM`): the live session
  ignored it until Glob accidentally taught the tree.
- Stage shot-root clips into `refs/` so the "no video" sentence becomes true: that
  would leak non-`refs/` packaging into the authored-input workspace (ADR-0004).
- Deny every absolute path including the workspace itself: relative names resolve
  through cwd, and repair snapshots remain exact absolute exceptions.

## Validation

A temporary workspace with `brief.md`, `ownership_mapping.json`,
`plans/global.draft.md`, and `refs/frame.png` denies Read of a sibling-project
`brief.md` and the reason contains every staged relative name plus "do not prefix
another filesystem root." Verify kickoff for that workspace compiles those names,
names the first reads, and does not contain "there is no source video." A three-file
cap remainder is named as `and N more`.

Verification on 2026-08-30:

- `.venv/bin/ruff check src tests` — passed.
- `.venv/bin/python -m pytest -q tests/unit/test_plan_authority.py` — `28 passed in 3.05s`.
- `.venv/bin/python -m pytest -q` — `669 passed in 46.16s`.
- `.venv/bin/vfx --help` — exit 0.

## Release and rollback

No schema migration. Rollback would restore one-sided denials and the false "no
source video" kickoff sentence.

## Remaining limitations

The card lists files; it does not attach stills or register video tools. A clip
packaged beside `brief.md` rather than under `refs/` remains invisible to global
planning by design. Turn and spend caps are unchanged.
