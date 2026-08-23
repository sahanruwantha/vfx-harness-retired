# Check that ownership is executable; stop teaching the global planner the shot

**Status:** research + prototype (2026-08-23). Prototype checker lives at
`src/vfx_harness/evaluation/ownership_feasibility.py` with heterogeneous fixtures in
`tests/unit/test_ownership_feasibility.py`. It is NOT wired into `plan_gate`, publication,
or any schema. Promotion to an ADR requires the generality evidence below.

## Why

The first published sparse bundle (`a5692e9f…`, run `20260823T110844Z-6281c8`) passed the
structural gate and still contained two defects that no deterministic rule could see:

1. Whole-shot conditions (camera clearance, lighting arc, replay fidelity, multi-frame
   silhouette readability) were parked on the final layer, which is judged at one frame
   and can neither observe the named moments nor repair the implicated controls.
2. A downstream layer must mutate state produced by an upstream layer (architecture
   fractured and reassembled), which exclusive role reservations cannot represent; the
   materialization gate would have rejected the mutation as a namespace escape.

Both were discoverable from the brief, DAG, judge frames, and namespace declarations
alone. The post-publication review that found them also opened all ten reference images —
and its visual conclusions were the weak ones (scale speculation that belongs to Layer-1
runtime falsification; a blade-count "ambiguity" that is already explicit brief
authority). The lesson cuts both ways: authority relationships are checkable globally;
visual analysis is not, and inviting it encourages false escalation.

The fix direction is therefore: make the harness verify that every ownership assignment
is *executable* — not make the global planner smarter about any particular shot. No
`fracture`, `iris`, `gantry`, or frame numbers in core code.

## 1. Shrink what the model authors

Run 6 spent ~24.5 min and $5.74 authoring duplicated authority across nine files plus
prose, and the verifier rewrote a 147-line `global.md`. Mechanical content belongs to the
harness:

- clause IDs, citations, and exact brief text extraction;
- empty acceptance/check/scene-contract documents;
- `global.md` rendered from the accepted machine records;
- schema boilerplate and content hashes.

The model returns only the compact mapping: layer charters and dependencies; requirement
ownership mappings; durable decisions and blockers; critic routing axes; declared
cross-layer interfaces. The verifier audits that mapping — it does not rewrite a 10 KB
plan — and stays inside the 6-turn default ceiling. The `--max-turns 24` override stops
being part of the working invocation.

## 2. Split the three meanings of "owner"

`owner_layer` currently conflates producer, verifier, and repair route. Direction:

```json
{"producer": "6", "verify_at": {"kind": "acceptance"}, "repair_routes": ["2", "6"]}
```

- Camera clearance: produced collectively, verified cumulatively, repaired by the camera
  owner or the layer that introduced the offending geometry.
- Lighting arc: produced by several layers, verified at acceptance, repaired by the
  failing state's owner.
- Replay fidelity: harness-verified, routed to the first script whose replay diverges —
  not "owned" by the last layer.

## 3. Typed cross-layer handoffs

Some systems are created by one layer and intentionally mutated later. Exclusive
reservations cannot say that. Optional interface declaration:

```json
{"id": "architecture.mutable", "producer": "2", "consumers": ["6", "7"],
 "mode": "ordered_mutation_handoff"}
```

The gate verifies only generic relationships: producer precedes every consumer; consumers
depend on the producer; mutation follows the declared order; identity survival is
registered as a build-time obligation; undeclared overlap remains forbidden.

## 4. Bounded ownership-feasibility gate

Before publication, deterministically ask:

1. Can the assigned verification boundary observe every explicitly declared moment?
2. Can at least one repair route mutate the implicated role or control?
3. Does downstream mutation of upstream state have a declared handoff?
4. Does every handoff follow the DAG?

Explicit moments become lightweight registration metadata — frames a clause names, not
thresholds or evidence design. A requirement naming frames 132/150/204/240 cannot be
assigned to a layer judged only at 240. The prototype demonstrates that these four checks
reject both published-bundle defects without opening a single reference image or naming
any technique.

## 5. Visual analysis stays JIT

Global planning does not inspect references. Each JIT unit inspects only its due
references after upstream state exists. Scale and framing questions resolve by runtime
falsification at the owning unit, not by global review.

## 6. Generality before core contracts

Cross-layer handoff is currently evidenced by one shot, so this stays a research note and
prototype until the mechanism passes heterogeneous fixtures:

- architecture created early and animated later;
- a character/prop created by one unit and manipulated by another;
- a static product shot requiring no handoff at all;
- a multi-frame acceptance rule outside any single build layer;
- a single-frame local requirement that remains layer-owned.

Acceptance: the mechanism reasons only over roles, dependencies, moments, and boundaries —
a source-scan test enforces that no shot vocabulary appears in the checker.

## Target flow

```
brief clause extraction
→ sparse ownership/DAG proposal
→ ownership-feasibility gate
→ narrow omission verifier
→ publish
→ materialize root unit
→ first authoritative scene evidence
```

## Open questions before an ADR

- Adapter: current bundles carry `owner_layer` and no moments metadata; migration needs
  the requirements schema to grow `producer`/`verify_at`/`repair_routes`/`moments` and an
  interfaces document, with fail-closed reads of old bundles.
- Authoring diet mechanics: which extraction steps are deterministic enough to own
  (clause spans exist in the gate today), and what the model-facing "compact mapping"
  schema looks like as a single tool-returned object rather than nine files.
- Harness-verified requirements (replay fidelity) need a divergence-routing rule: first
  script whose replay diverges receives the fault, which is a build/accept mechanism, not
  a planning one.
- Whether `verify_at: acceptance` requirements also need a declared measurement owner so
  acceptance does not become a silent dumping ground (the L8 parking failure in new
  clothes).
