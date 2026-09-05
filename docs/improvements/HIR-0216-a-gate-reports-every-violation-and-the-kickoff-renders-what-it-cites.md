---
id: HIR-0216
title: A gate reports every violation it found, and a kickoff renders the evidence it cites
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: findings_already_in_hand_were_delivered_serially_or_by_a_path_the_reader_cannot_open
mechanism: the_artifact_walk_collects_every_violation_the_journal_names_them_in_place_and_plan_gate_evidence_is_compiled_into_the_kickoff
adr: null
---

# A gate reports every violation it found, and a kickoff renders the evidence it cites

## Observed failure

Three surfaces, two shots, one shape: information the harness already held, delivered in
a form the receiving session could not act on in one pass.

**The artifact policy reported one violation at a time.** caesar's dais unit, three probe
cycles on one candidate:

```
probe 1   line 72 uses `bpy.data.objects`
probe 2   line 24 uses `bpy.data.objects`
probe 3   line 25 uses `bpy.data.objects`
```

Lines 24 and 25 are adjacent. After fixing 24 the builder paid another
write-then-probe round trip to be told about 25, which was already there and already
illegal when line 72 was reported. The unit closed at $3.22 and 95 turns, on the phase
that is $30.63 of that shot's $46.11.

**The journal is written in the dialect the policy rejects.** Every journal file across
two shots contains `obj = bpy.data.objects[name]` and similar — legal live, invalid in an
artifact. Its header teaches exactly one transformation:

```
# Superseded tweaks and probes included: PRUNE, do not paste.
```

Nothing about the policy. On caesar the rejected line numbers matched the journal's own
content exactly, so a finalizer following the header precisely produces a source the
policy refuses.

**A dispatched rematerialization cites evidence by a path its workspace forbids.** hansa
run `20260905T035847Z-dc8f69`:

```
EVIDENCE THIS REPLACEMENT MUST ANSWER. Each record below is an executable finding ...
 - evidence runs/20260905T035847Z-dc8f69/reports/plan-stop-evidence.json
```

"Each record below" is one filename. Reading it is refused, correctly — fresh planning may
read only its staged relative files, which are `brief.md` and ten reference stills. The
kickoff contained the finding count and two opaque fingerprints; it did not contain the
unit names or what was wrong with them.

## Root cause

Each is the same defect at a different boundary: the harness holds the answer and hands
over a pointer to it, or the first item of a list it has already finished computing.

`validate_artifact_source` is a single `for node in ast.walk(tree)` that raises on the
first match. One traversal finds every violation and discards all but one. They cannot be
ordered by candidate state the way two staging gates can — line 25 is not created by
fixing line 24 — so nothing about the serial delivery is load-bearing.

The journal writer joins accepted `run_bpy` bodies verbatim. The harness owns both that
writer and the validator, so it knows at write time which lines will be refused, and says
nothing.

`replacement_evidence_block` renders hypothesis falsifications compactly, which is
HIR-0191 working. A plan-gate stop is a different record, so `load_hypothesis_falsification`
raises and the code falls to its last resort, `f"- evidence {locator}"`. The workspace
confinement then refuses that locator. Two mechanisms, each correct alone, composing into
a context gap — and the reporting session's framing is the right one: nobody is wrong
locally.

## Decision criteria

- A gate that has already found every violation reports every violation.
- Source order, not traversal order: a list reading 72, 24, 25 is worse than three ordered
  refusals.
- Collecting never admits anything — any violation still refuses the artifact.
- A single violation keeps its exact existing message; only a list gains a header.
- The harness annotates what it knows will be refused; it does not rewrite model-authored
  code, because a transformation that changes meaning is worse than a rejection.
- Evidence a kickoff cites is compiled into it, matching how that kickoff already compiles
  the layer row, owned requirements and upstream outcomes. Staging the file instead would
  trade a context gap for a wider read surface.

## General mechanism

1. `artifact_violations(source, tree)` returns every violation as `(line, message)`,
   sorted by line and deduplicated. `validate_artifact_source` collects and raises once,
   listing all of them with the count and "fix all of them in one edit"; a single
   violation raises with its exact prior message.
2. `blender/journal_policy.annotate_journal` runs that validator over each journal entry
   after the worker writes the file, inserts a `# POLICY line N:` note above every entry
   that will be refused, and prepends a header stating the rule. Entries that parse clean
   are untouched and a clean journal is left byte-identical. The worker cannot do this
   itself — it is a standalone script inside Blender with no harness imports — so the
   parent does it at the point it captures the journal.
3. `_render_gate_findings` renders a plan-gate stop's blocking findings into the
   replacement evidence block: check, layer, unit and the full diagnostic text, bounded at
   twelve findings with per-finding truncation. Anything still unrecognised is named by
   path, so nothing cited is hidden.

## Rejected patch-level alternatives

- Rewriting journal entries into replay-legal forms: the harness transforming
  model-authored code, with a semantic-change risk worse than the rejection it avoids.
- Applying the artifact policy at live `run_bpy` call time: the split is probe versus
  mutation, not live versus artifact, and probe payloads are pruned before composition, so
  it would narrow live exploration for no benefit.
- Staging the evidence file into the materialization workspace: the refusal's own text
  contemplates it, but it widens the read surface the confinement exists to hold.

## Validation

- `src/tests/unit/test_journal_states_the_artifact_policy.py`: every violation is found in
  one walk and returned in source order; the refusal lists all of them and says so; a
  single violation keeps its exact message; the three legal forms are still accepted; the
  journal annotates only the entries that will be refused, preserves their content
  verbatim, leaves a clean journal byte-identical, and skips an entry that does not parse.
- `src/tests/unit/test_rematerialization_evidence.py`: a plan-gate stop renders its
  blocking findings with the unit name and diagnostic text, advisory rows are excluded,
  and an unrecognised evidence file is still named by path.
- Verified against the live artifact rather than a fixture: hansa's actual
  `plan-stop-evidence.json` renders both unit names, both selector sets, and the rule
  sentence its materializer was missing.

## Release and rollback

The collected refusal changes the text of a multi-violation rejection and leaves the
single-violation message unchanged. The journal gains annotations and a header. The
kickoff gains rendered findings, bounded. No schema, authority or evidence change.

## Remaining limitations

The journal annotation names lines in the entry that will be refused; it does not verify
that the artifact the finalizer eventually composes is clean, which remains the validator's
job at write time. And the underlying reason the journal contains the rejected dialect is
untouched: live calls legitimately use constructs an artifact may not, so the finalizer
still has to rewrite them — it is now told which, rather than discovering them one probe
at a time.
