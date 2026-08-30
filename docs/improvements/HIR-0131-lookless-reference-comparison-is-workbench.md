---
id: HIR-0131
title: Look-less reference comparison is a Workbench instrument
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: geometry_form_reference_forced_beauty_debt
mechanism: typed_lookless_comparison_default_and_teaching_signal_rejection
adr: null
---

# Look-less reference comparison is a Workbench instrument

## Observed failure

Room 1046 Layer 2 materialization run `20260830T050926Z-ab4e40` correctly split
building mass, roof, ground island, and streetlight geometry into independent derived
write clusters. Every unit nevertheless declared `look_capabilities: ["detail"]` and
owed a required `frame_detail` image contract at frame 39. Finalization refused all
four units with `image-signal-bootstrap`: the selected cumulative view contains a sealed
camera but no earlier or same-layer light, shading, volume, or compositor provider.

That layer owns form and silhouette. Its reserved roles and capability vocabulary do not
authorize lighting or shading, and those responsibilities belong to later layers. The
planner therefore had no legal way to preserve reference-guided form iteration: keeping
the image claims demanded an out-of-scope optical provider, while dropping them left
`compare_frame` defaulting to an unlit EEVEE plate unless the builder remembered a mode.

## Root cause

HIR-0036 compiled typed look authority into `render_frame` and `verify_change`, but
explicitly left `compare_frame` unchanged. Its first round default was hard-coded EEVEE;
only a later crop inherited a locked mode. HIR-0110 correctly rejects Workbench as payment
for EEVEE beauty debt, but its teaching rule named only signal-producer ordering and did
not distinguish a genuine look claim from executable form/layout guided by a diagnostic.

Classification: missing instrument default plus incomplete rejection guidance. The
builder was forced to recall `mode="solid"`, and the materializer was pushed toward
authority it did not own.

## Decision criteria

- The first `compare_frame` call derives its omitted mode from typed look authority:
  Workbench `solid` for look-less units, EEVEE for look-owning units.
- An explicit mode remains authoritative, and crop comparisons inherit the round lock.
- Workbench comparison is live diagnostic context only. It does not create an immutable
  EEVEE payment handle, satisfy an `image_contract`, or weaken HIR-0110.
- `image-signal-bootstrap` names both legal responses: bind a registry-derived optical
  signal provider for genuine beauty, or remove inapplicable look ownership/debt and bind
  executable scene/projected-composition claims for form/layout. Missing authority
  escalates.
- The mechanism contains no shot, frame, role, layer-count, or display-name inference.

## General mechanism

`_comparison_mode_scale` now accepts the compiled `look_actions` bit already used by the
Blender tool server and calls the shared `preview_render_mode` resolver for the first
comparison. A round lock still takes precedence for later frames/crops, and an explicit
request still wins. The tool card and compact ownership instruction report the derived
behavior rather than asking the model to remember it.

`IMAGE_SIGNAL_DEPENDENCY_RULE` keeps the closed signal-family gate intact and adds the
geometry-only alternative. The materialization and independent plan gates consume the
same rule, so both rejection surfaces teach the same legal next actions.

## Rejected alternatives

- Count Workbench pixels as beauty payment: this changes evidence domain and would undo
  HIR-0046 through HIR-0048 and HIR-0110.
- Add a temporary/default light: it is undeclared mutation absent from empty-scene replay.
- Tell the planner or builder to remember `mode="solid"`: a recurring guess is a missing
  tool default, and prompt-only discipline cannot enforce the evidence boundary.
- Infer form layers from names such as `massing`, `building`, or `silhouette`: current
  fixtures are not core authority; the existing typed `look_actions` bit is sufficient.

## Validation

Focused tests prove look-less, look-owning, explicit-mode, and crop-lock behavior, and
prove both materialization and plan-gate signal findings name the executable-only path
without changing the provider predicate. The existing HIR-0110 fixtures continue to
prove that mesh, camera, controls, role labels, and look labels cannot pay beauty debt.

Validation on 2026-08-30: the focused look-capability, atomicity, and plan-record suites
passed 164 tests in 6.13 seconds; the full repository suite passed 625 tests in 43.14
seconds. `.venv/bin/ruff check src tests` and `.venv/bin/vfx --help` passed.

Production validation is the next bounded Layer 2 rematerialization: a geometry-only
candidate must be able to publish without optical mutation, and its builder must receive
a Workbench reference comparison while deferred projected-composition rows remain due.

## Release and rollback

No persisted schema or authority migration. Rollback restores EEVEE as the omitted
look-less comparison mode and removes the legal form/layout route from signal feedback,
again forcing the model to recall an instrument setting or invent look ownership.

## Remaining limitations

Workbench comparison guides construction but is not a qualified autonomous visual judge.
Layer sealing still requires executable claims and any declared qualitative/human evidence.
If form resemblance needs autonomous blocking beyond available scene/projected metrics, it
requires a separately qualified evidence contract rather than relabeling solid as beauty.
