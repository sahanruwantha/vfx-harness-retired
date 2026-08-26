---
id: HIR-0023
title: Materialization findings are pointer-addressed and returned together
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: one_error_per_rewrite_exhausted_materialization
mechanism: json_pointer_findings_and_structured_patch
adr: null
---

# Materialization findings are pointer-addressed and returned together

## Observed failure

Layer-1 rematerialization (`l1-remat3`) died `error_max_turns`, then raised
`ValueError: scene contract cam-vis-interior-f72 must be owned by layer 1`. The
write-hook returned one validator error per `Write`. The session rewrote the
whole JSON document to fix that field, received the next field, and exhausted
the turn budget. Attempt 3 (run `48a0f1`) walked the same loop: twenty-four
turns, thirteen writes, one field-precise error each time. The kickoff already
carried a generic valid-shape example — a prompt-level patch for a mechanical
defect. The owner-layer message had been improved but still had no location
and still aborted the rest of the document.

## Root cause

`validate_materialization` raised on the first collectable defect. The only
repair instrument was `Write` of the entire candidate. `Edit` was (correctly)
denied. Glob of historical bundles was a wasted deny-turn under `strict_reads`,
not a legal authority path. Classification: orchestration diagnostic gap — the
schema lived only in the validator, and the validator would not report the
document.

## Decision criteria

- Rejections teach: one write returns every collectable finding, each named by
  an RFC 6901 pointer (`/scene_contracts/2/owner_layer: …`).
- Close the loop: a mutating tool that sets one pointer has a matching
  re-validation.
- Query, don't recall: predecessor mistakes arrive through the `replacing`
  kickoff block and the selected bundle, never Glob of prior plan files.
- Fatal document errors stay fatal: unreadable JSON, wrong schema, wrong
  bundle hash, layer not an object.
- No prompt patch. The example JSON stays the document shape; repair is the
  finding list plus `patch_materialization`.

## General mechanism

- `domain/json_pointer.py` encodes, gets, and sets RFC 6901 pointers (`~0`/`~1`;
  no document-root replace).
- `validate_materialization` collects findings as `{pointer}: {message}` and
  raises them joined. `inspect_materialization` returns the list.
  `apply_materialization_patch` sets one pointer on the candidate file and
  re-inspects.
- The materialization write-hook splits that list. `patch_materialization` is
  registered only when a candidate path is set; it JSON-decodes `value`, patches,
  and returns VALIDATION PASSED or remaining findings. `Edit` stays denied.
- The materialization session denies Glob and Grep (`MATERIALIZATION_DENIED_TOOLS`).
  Authority is the selected bundle, `state/plan-resolutions.jsonl`, and
  `plans/outcomes/`.

## Rejected patch-level alternatives

- More example JSON in the kickoff: the example already existed; the session
  still walked one error per write.
- Enabling `Edit`: JSON text-edit is the wrong instrument; a pointer set is
  the closed loop.
- Loosening `max_turns` or the budget: that spends more to keep the walk.
- Glob of historical bundles: discarded authority is not a repair surface;
  `replacing` already carries why the predecessor was discarded.

## Validation

- `tests/unit/test_json_pointer.py`: encode, get, set, reject root replace,
  `~1` escaping.
- `tests/unit/test_plan_records.py`: two independent defects (`owner_layer` on
  `vis-f239` and omitted `look_capabilities`) appear in one inspect; the first
  patch leaves the second pointer; the second patch clears the document.
- `tests/unit/test_plan_session_budgets.py`: materialization denies Glob/Grep;
  `patch_materialization` is registered only with a candidate file.
- Existing `pytest.raises(..., match="...")` substring tests still match because
  messages remain suffixes after the pointer.
- `.venv/bin/ruff check src tests`: All checks passed.
- `.venv/bin/python -m pytest -q`: 313 passed.
- `.venv/bin/python -m tests.integration.test_harness`: ALL PASS (0 failed).
- `.venv/bin/vfx --help`: ok.

## Release and rollback

No schema migration. Rollback is reverting the collector, the pointer module,
and the patch tool. Existing materialization documents are unchanged.

## Remaining limitations

- A finding that requires adding a missing claim or contract is not a single
  field set; the session still `Write`s those. Pointers cover the field-precise
  walk that exhausted remat.
- `patch_materialization` cannot replace the document root.
- Production `l1-remat4` is still the acceptance gate for HIR-0019 (iris-crossing
  spine; interior `visible_fraction` at f72/f150 owned by layer 1). This HIR does
  not cheapen that rebuild against a falsified view.
