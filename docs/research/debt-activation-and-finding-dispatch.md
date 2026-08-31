# Debt activation closure and finding dispatch

Status: provisional design proposal. This note is not binding rule text. It leaves
research only through the exit records in "Leaving research".

## Summary

Several accepted HIRs already say the same sentence: **evidence must not be billed at a
boundary whose replay prefix cannot produce it.** Scene projection waits for a camera
(HIR-0085). Image-contract debt waits for optical signal (HIR-0110) and a rendered
carrier (HIR-0160). Deferred subject `bbox_*` waits for a compiled `activates_at`
(HIR-0129, HIR-0130, HIR-0134, HIR-0158); partial producers receive only diagnostic
forecasts before that boundary (HIR-0151).

Before HIR-0163, composed judgment of `approved_start` / `planner_start` image
propositions was the one debt kind with no activation rule. A camera-only layer could
therefore pass every executable unit, then fail composed replay on empty plates. The
operational recovery people reached for was global replan. That was the wrong owner.

This note proposes four ordered seams, not a new pipeline philosophy:

1. Compile a versioned HIR-0137 judgment-debt definition plus a later exact activation
   binding, so a camera-only layer can accept its executable artifact while the still-unpaid
   judgment remains durable and inactive.
2. Promote due-time and payer-closure to a compiled property of every debt kind, and
   reject a DAG that cannot legally pay a published debt.
3. Track a two-layer still as an end-to-end eval fixture so the architecture always
   has a sealed small-scale path.
4. Publish a closed stop envelope, then later add a dispatch driver that consumes it into
   receipt-producing audited transactions and never authors or skips authority.

A supervisor that "does the least thing to remove the blocker" is rejected.

## Implementation status — 2026-08-31

HIR-0163 has promoted the first bounded mechanism from this proposal: strict
requirements/JIT schemas, immutable typed debt definitions, relevant DAG-compiled
activation, exact payer unit digests, independent durable lifecycle state, one-debt
composition scheduling, no-signal preservation, ownership rejection, and final-acceptance
refusal. HIR-0158 deferred subject activation now shares the same provider-prefix compiler.
The deterministic public-orchestration ratchet also now covers camera pass with pending
debt, refusal of a selected-but-unreplayed payer, exact form payment after cumulative
replay, restart idempotence, acceptance closure, and unchanged global bundle authority.

The next bounded mechanism is also implemented. Canonical replay issues an ordered,
checkpoint-concordant receipt; a carrier-aware Blender probe seals evaluated camera,
subject, render, world, compositor, light, view-layer, and color state at the declared
frame; and the harness compiles a strict pre-render observation request across current
authority, replay, references, promoted assets, comparison, and judge configuration. The
render returns a second hash-pinned capture receipt. A no-signal attempt is append-only
state keyed by that request: unchanged direct build/restart re-proves replay but performs
no render or critic call, while a changed typed environment permits one new attempt.

The bounded stop-publication mechanism is now implemented as well. Every run-scoped
unaccepted boundary must publish and read back one immutable
`vfx-harness.stop-envelope/v1`, selected by digest from terminal status. The schema
separates a stable cause fingerprint from exact attempt evidence and carries one typed
transaction target, closed preconditions, evidence references, dispatch mode, typed
progress postcondition, and receipt schema. Strict preflight publishes
`vfx-harness.environment-result/v2` with its exact embedded probe specification; the whole-run
driver allocates its run before that
probe. Owning classifiers currently cover rejected global-plan structure, JIT
materialization structure, builder/composition hypothesis falsification, strict
preflight, and failed full-acceptance moments. Any other untyped terminal boundary fails closed
as `harness_defect`; its exit code or prose is not reinterpreted as retry or replan.
Full acceptance also publishes a content-addressed outcome for the exact selected
bundle, JIT view, accepted build chain, moments, and evidence. Final rendering revalidates
that passing outcome before Blender starts; forced and partial output are previews rather
than deliverables. Forced acceptance itself is now a run-scoped preview and cannot write
an outcome, resolution, supersession, repair route, or shot-ledger state. Acceptance and
rendering share the selected global DAG order and evaluated-frame replay barrier; JIT-added
acceptance moments come from the same fully validated content-addressed consumer view.
No-signal results preserve their deterministic decision source and make no critic call,
and final-render entry rechecks both current judgment debt and acceptance-due authority.

The downstream materialization boundary now treats a dependency's small evidence projection
as insufficient by itself. The JIT publisher, paid planner kickoff, and materialization-stop
classifier all call one current-eligibility boundary. A passed dependency must retain a
producer-valid sealed outcome whose global-DAG replay prefix, current input manifest,
script/reference inputs, canonical observation kind, and (for raster evidence) actual render
receipt and bytes remain eligible. Executable-only outcomes carry typed authoritative evidence
and no invented raster fields. Outcome locators and planning scratch paths derive from arbitrary
typed layer identities rather than decimal filename conventions. These checks close stale
dependency authorization; they are not recovery dispatch.

HIR-0167 closes the selected-authority identity seam needed before amendment execution can be
designed safely. Planning, JIT materialization, and builder falsification now bind one semantic
`vfx-harness.selected-authority-state/v2` assertion instead of producer-private bundle/view
digests. The assertion distinguishes true absence from a verified selected bundle and effective
bundle/JIT consumer view, includes the publishable gate outcome and exact semantic artifact
manifests, and excludes run ids, paths, timestamps, and pointer locators. Resolution fully
verifies plan-bundle membership and bytes plus any present JIT pointer/view, then rereads both
pointers before returning. A valid stale JIT generation is verified and inert; malformed,
unreadable, inconsistent, or symlinked selection is never treated as absence or silently ignored.
Selected resolution also rehashes the complete authored input tree and verifies the exact
planning-time prefix of both append-only decision ledgers. Later decision suffixes remain legal,
while same-size authored edits, consumed-prefix replacement or truncation, missing input
structure, symlinks, special files, and unreadable bytes fail closed under strict workspace and
provenance v2 schemas.

The amendment proposal and postcondition are now strict v2 contracts over that shared base,
exact findings and owner, the structural plan-gate schema/policy/scope, and the required successor
source (`bundle` globally, `jit` for a layer). The target no longer accepts a caller-supplied
hard-constraint Boolean. A hard-constraint falsification instead publishes a typed human question,
while corrupt global selection routes only to engineering as a harness defect. This is stronger
classification and precondition authority, not an amendment transaction: no public amendment
adapter, successor commit, receipt, evaluator, or dispatch permission has been added.

This does **not** complete the program. Deterministic equivalence/successor handling for
already-satisfied discharges, explicit same-bundle lineage/retirement, failure reasons
beyond no-signal, cross-layer fault routing, the fresh real-model two-layer CLI eval, and
several owning stop classifiers remain open. There is no dispatch controller or controller
journal. HIR-0166 implements the durable `prepared -> running -> terminal` receipt protocol and
an explicitly invoked, idempotency-key-consuming `recover_environment` adapter; it is the only
receipt-capable transaction and does not authorize automatic dispatch. Safe checkpointed
session resume is likewise unimplemented;
a legacy checkpoint-and-journal row does not establish the full authority, unit, session,
phase, and write-ahead-log identity required by that transition. The motion chamber is
not the next validation target; the remaining heterogeneous fixtures and real-model
two-layer seal remain the required bridge.

## Observed stop

Run `20260831T060854Z-9274e5` on a motion chamber fixture:

- Sparse DAG already split camera provide from later form namespaces (HIR-0128).
- Unit `aisle_target_control` passed empty-scene executable replay.
- Unit `camera_rig_framing` passed empty-scene executable replay at every judge frame.
- Composed `01_camera.py` then scheduled independent reference judgment for eight
  layer-owned `approved_start` image-domain requirement ids (HIR-0137).
- Workbench-solid plates had no relevant rendered carrier/subject and insufficient pixel
  signal. HIR-0032 correctly refused the critic.
- The layer recorded `contract_gap`, published finding `hf-07bb5cb3c464b371aaf9`, and
  exited 9: units passed, layer verdict did not.

The builder did not fail a lock. Composed replay billed appearance of subjects whose first
legal activation boundary would be a later relevant geometry layer; HIR-0137 debt cannot
currently express that activation.

An earlier generation of the same fixture combined camera provide with form
`reserved_roles` on one sparse layer. Materialization then staged a shading-only unit
that could not create meshes. That stop required global republication. This stop does
not. Treating both as "replan the shot" is the operational defect.

## Diagnosis

This is not a new hole. It is the sixth member of a family already on disk:

| Record | Debt kind | Prefix that can pay |
| --- | --- | --- |
| HIR-0085 | camera-projecting scene contracts | camera provider in the dependency closure |
| HIR-0110 | image-contract debt | `light` / `shading` / `volume` / `compositor` family |
| HIR-0160 | image-contract debt | `mesh` / `volume` / `compositor` carrier in the replay prefix |
| HIR-0158 | deferred subject `bbox_*` | compiled `earliest_geometry_layer` as `activates_at` |
| HIR-0129, HIR-0130 | future-active scene rows | activation layer pays; owner layer binds through `composition_context` |
| HIR-0134 | deferred composition | dependency-complete payer closure, not an earlier partial producer |
| HIR-0151 | deferred composition | diagnostic forecast before the dependency-complete payer |
| **this stop** | HIR-0137 composed image judgment | **missing: same carrier activation as HIR-0160** |

HIR-0032's no-signal refusal is not the defect. Before it existed, a camera-only
composed critic scored passing marks on pitch-black plates. That gate is what made
the false pass unrepresentable. Loosening it, or letting an untyped supervisor skip
it, restores 4.0-on-black.

The discovery process is serial, runtime, and full-price. Each hole is found on the
current production fixture after a plan/materialize/build spend, then recovered by
rewriting that fixture's mapping. Mechanisms in core are scene-independent; **which
hole is found next is not**. That is overfitting of the investigation path, not of
the predicates.

## What "fix what's wrong" means here

The owner is harness code. After slice 1, Layer 1 of the exposing run should accept its
executable artifact from existing unit checkpoints while its judgment debt remains open:
do not replan, do not re-pay the unit sessions, and re-run only composed replay against
debts that are actually due. Artifact acceptance is not requirement satisfaction.

Global republication stays reserved for wrong selected global authority — a bad DAG,
requirement owner, evidence-domain assignment, durable constraint, or reserved interface.
A camera-providing layer that reserved form namespaces is one example; HIR-0128's
reserved-roles gate is the lock for that class.

## Design invariants

The proposal is accepted only if the implementation preserves all of these distinctions:

- **Decision ownership is not activation.** The semantic owner keeps the proposition;
  a later replay prefix may merely make it observable.
- **Activation is not fault ownership.** A provider that makes pixels possible is not
  automatically the thing that must be repaired when judgment fails.
- **Artifact state is not debt state.** A deterministic layer artifact may be `passed`
  while a current-authority-generation judgment debt is `pending_not_due`.
- **A carrier must be relevant.** Global presence of any mesh, volume, or compositor is
  insufficient. The payer closure must cover the debt's typed subject selectors or
  interface and every required observation capability.
- **Prerequisites compose with AND.** Camera, carrier, optical signal, observation medium,
  temporal state, and judgment qualification/independence are separate requirements where
  applicable;
  one generic `payer` flag cannot replace them.
- **Absence never pays debt.** No-signal, missing subject, missing frame, missing provider,
  or missing qualified verdict cannot become a pass.
- **Selected authority remains singular.** A debt definition derives from one selected
  bundle and owner-view generation; a later activation binding names its exact payer-view
  generation. State rows from another authority generation or definition digest are inert.
- **Acceptance closes the ledger.** Final acceptance requires every current required debt,
  regardless of lifecycle, to be `satisfied` by current evidence or retired by an authorized
  typed transaction. Earlier layer/window expiry gates are not trusted as the only safeguard.

Two examples pin the ownership rule:

| Proposition | Semantic owner | Earliest possible observation prefix |
| --- | --- | --- |
| Camera framing of a hall that a later form layer creates | Camera/framing owner | Sealed camera plus the relevant form carrier |
| The hall has the required architectural appearance | Form/look owner | The relevant form carrier, plus the declared observation medium |

Activation may defer the first row. It must not be used to make the camera layer own the
second row; that is a plan-ownership defect.

## Slice 1 — Compile and activate HIR-0137 judgment debt

**Owning record after promotion:** a new HIR extending HIR-0137 and HIR-0160. If the
selected requirements or consumer-view schema changes, the HIR must carry that strict
migration; it cannot land as an optional-field compatibility path.

### Versioned debt authority

A requirement deferred to a layer and selected as `approved_start` or `planner_start` with
an unpaid qualitative domain compiles one definition per independently payable proposition.
Current-generation falsification lineage may keep that definition active after a mechanical
contract rebind (HIR-0142).

One immutable record cannot contain both an owner-time proposition and the identities of
future payer units that do not exist yet. Doing so would make the record self-referential or
silently mutable. The compiler therefore emits two linked immutable records.

`vfx-harness.judgment-debt-definition/v1` defines one independently payable proposition in
the selected owner materialization generation. The harness derives its stable `debt_id` from
the canonical tuple:

```text
requirement id
  x evidence-domain binding
  x semantic claim kind/property
  x owned axis
  x subject interface
  x canonical judge-moment set
```

The compiler sorts selectors, moments, and reference identities before hashing. The planner
model neither chooses the id nor partitions one statement into convenient ad hoc debts. A
partition must follow the closed atomic-proposition vocabulary; an unrepresentable statement
fails materialization for reviewed authority repair.

The definition contains:

- stable debt id, requirement id, exact statement, and decision strength;
- selected global bundle digest, owner materialization parent/input digest, owner generation
  id, and definition digest;
- semantic decision owner, owned axis, and closed semantic claim kind/property;
- exact subject-role selectors and/or a typed subject interface;
- owner judge moments and immutable reference identities;
- required carrier families and any camera, optical-signal, temporal, or subject-completeness
  prerequisites;
- a closed `observation_medium`, such as `workbench_solid` or `eevee`, independent of who
  may decide the result;
- a closed `judgment_authority`, such as an executable contract, qualified independent
  critic, or reviewed human boundary, plus qualification, independence, disagreement, and
  escalation policy;
- the DAG-derived activation-layer/provider promise, without nonexistent future unit ids;
- typed `fault_owner`: either an exact existing owner reference or
  `derive_from_observation` with closed allowed axes/interfaces and mandatory abstention on
  ambiguity; a later falsification event pins the resolved repair owner and must not invent a
  future producer at definition time; and
- lifecycle (`layer`, `window`, or `persistent`) and any typed valid-through boundary.

The definition digest covers the canonical record with its digest field omitted. A builder
or producer cannot qualify as the independent judge of its own qualitative result merely
because it emitted the candidate.

Owner and payer generation ids are transaction-derived from the selected global bundle,
layer id, parent selected-generation digest, staged input digests, and revision. They contain
no timestamp, random value, model-authored token, or resulting container digest.

`vfx-harness.judgment-debt-activation/v1` is created only when the promised activation layer
materializes. It links to the exact definition id and digest and pins:

- the payer materialization parent/input digest and generation id;
- exact dependency-complete payer unit ids and unit digests;
- the interfaces, subject-role overlap proof, carrier/write families, camera dependency, and
  observation medium that make payment legal;
- the expected cumulative replay-prefix identity derivation; and
- its own activation-binding digest and optional predecessor binding/definition reference.

An immutable binding never names a future successor. A successor names its predecessor, or an
append-only revision-checked lineage event links the two after both exist.

A payer-view change creates a new binding; it never edits the definition. A definition change
creates a new definition generation and an explicit lineage transition. Neither record embeds
the digest of the container that includes it. The selected pointer/manifest pins the resulting
owner and payer consumer-view digests externally, and lifecycle events pin those selected
digests when they act. This avoids a container-hash cycle and prevents a future unit digest from
being smuggled into parent authority after its digest was selected.

Definitions and activation bindings belong to selected plan authority. Before a binding
exists, `pending_not_due` is definition-generation state keyed by the exact selected owner
view with `binding: null`. Selecting the first activation binding creates the first bound
payment generation. Bound lifecycle and payment-attempt events then key the exact definition,
binding, and payment generation. All cross-run events live under shot-root `state/`; none is
reconstructed from transcripts, old findings, filenames, or prose.

### Typed global ownership seed

Sparse publication must represent enough semantics to reject wrong ownership and an
unreachable payer before JIT spend, without pulling concrete evidence design back into global
planning. The ownership-only requirement register and reserved-interface authority therefore
need closed fields for semantic claim kind/property, owner axis, subject-interface ids, and
provider-capability promises. The harness derives required carrier capabilities from that
vocabulary and the declared evidence domains.

The global gate consumes a canonical registry from semantic claim kind/property to required
owner capability/interface and allowed fault-routing policy. It verifies that the owner layer
declares that capability and that the selected DAG contains a reachable matching provider
promise. For example, a hall-appearance property requires form/look ownership; a camera-only
framing capability cannot own it merely by declaring a broad `reference_match` axis.

The planner model authors the closed semantic classification, and an independent plan
verifier audits statement-to-classification consistency or abstains. Its typed attestation
digest is pinned by plan-gate publication; absence, disagreement, or abstention blocks
selection. The deterministic gate enforces the declared types and registry mapping; it does
not pretend to understand prose.
Neither boundary infers ownership or subject relevance from layer titles, role-name keywords,
or department names. Judge thresholds, exact frames, comparisons, and payer unit ids remain
JIT authority. Adding these fields requires a strict selected-authority schema bump rather
than an optional "unknown means any provider" path.

### Activation compilation

The harness, never the planner model, derives `activates_at`. "A mesh exists" is not the
predicate. The first legal activation boundary is the earliest stable topological replay
prefix that satisfies every debt prerequisite:

1. it is the earliest owner-or-successor composition after the definition exists; its
   cumulative prefix may already contain an accepted ancestor carrier or may gain the carrier
   from dependency-complete same-layer fan-in;
2. its dependency closure contains the required camera or other upstream interfaces;
3. it derives an allowed carrier family that can appear in the selected observation medium;
4. the carrier's reserved roles, published interface, or exact materialized roles match
   the debt's subject selectors;
5. it is dependency-complete for an aggregate subject rather than merely the first partial
   mesh producer; and
6. its judge operation can replay the owner's declared frames and references.

Sparse global publication searches the owner's existing dependency closure first, then the
owner layer, then reachable successors. It can prove only a **provider promise**, because
later unit ids do not exist yet. JIT materialization must pin the exact dependency-complete
payer units and digests. Runtime then verifies that those exact artifacts are present in the
cumulative replay prefix. A mismatch at any stage fails closed at that stage.

If sparse publication cannot find a relevant provider promise in the prior/current closure or
any reachable successor, it fails immediately before builder or critic spend. If the
proposition is assigned to the wrong semantic capability, the ownership gate fails first;
activation is never a mechanism for concealing misownership.

`mesh`, `volume`, and `compositor` are not interchangeable witnesses. Workbench solid can
make a mesh observable but may not expose a volume or compositor result; those debts must
activate only at a render mode that can display their carrier. HIR-0110 optical signal and
HIR-0160 rendered carrier remain separate predicates.

### Independent artifact and debt state

Executable artifact status and judgment-debt status are separate state machines:

```text
artifact: pending -> building -> passed | failed | superseded

debt payment generation: pending_not_due -> due -> satisfied
                                              +-> falsified
```

`satisfied` and `falsified` are terminal for one payment generation, not permission to
rewrite history. An authorized repair, relevant replay-prefix change, or authority change
creates a successor generation linked to the prior event. It never mutates the old verdict.
`retired` and `superseded` are typed authority-lineage outcomes, not judgment results; neither
can be emitted by a payment attempt.

`pending_not_due` means:

- the debt stays bound to its semantic owner; decision authority does not move;
- it does not enter the current composed critic's required set;
- it produces no no-signal `contract_gap` at this boundary;
- the owner artifact may pass and downstream work may advance only because the selected
  DAG already proves a reachable typed provider promise; and
- it remains visible in run summaries and acceptance accounting.

At the compiled payer boundary the debt becomes `due`. Payment is scheduled at most once for
the tuple `(definition digest, activation-binding digest, payment generation,
observation-scope digest)`. `vfx-harness.observation-scope/v1` is a canonical provenance
contract, not the phrase "everything relevant." Its medium-specific registry pins:

- selected global/owner/payer authority generations;
- the canonical parent-chain digest plus every accepted script/artifact/checkpoint and unit
  digest in the evaluated prefix;
- evaluated camera and matching subject/interface closure at every declared frame;
- external asset content hashes and construction/import provenance;
- Blender build, render-engine version, stochastic seeds, and scene/render/view-layer/world/
  compositor/color-management settings required by the medium; and
- candidate, immutable reference, comparison configuration, and judge qualification/config
  hashes.

The implemented boundary splits this concept into a pre-render
`vfx-harness.judgment-observation-request/v1` and a post-render
`vfx-harness.canonical-render-capture/v1`. This is necessary rather than cosmetic: the
unchanged-attempt lookup must be computable before raster spend, while candidate bytes and
actual worker render settings can exist only afterward. Their digests are joined in the
canonical verdict/payment evidence; the request digest is the suppression key.

The compiler excludes a contributor only through a registered deterministic influence rule.
An unknown contributor or unsupported version fails closed and forces re-judgment; it cannot
be silently omitted from the digest.

A successful qualified judgment writes an append-only `satisfied` event containing those
identities, judge identity/configuration, qualification proof, verdict, and any dissent. A
qualified negative judgment writes `falsified` plus a typed finding with the same evidence.
The candidate builder cannot self-certify a qualitative debt.

Missing evidence, no relevant subject pixels, no optical signal, an unavailable frame, or an
infrastructure failure creates a typed payment-attempt failure and leaves the debt `due`; it
does **not** falsify the qualitative proposition and does not call a critic. A separately bound
deterministic contract may falsify its own availability hypothesis, but that result cannot be
relabelled as the qualitative verdict.

The scheduler persists the failed attempt's exact tuple before returning. Every entry point,
including a direct `vfx build` restart, resurfaces the same typed stop without another render or
critic call until a relevant authority, observation-scope, or environment/session digest
changes. Unchanged-attempt suppression is a debt-ledger rule, not a feature available only to
the later dispatch controller.

After a `falsified` event, only a legal repair or authority transaction that changes the
declared repair target or relevant replay identity may create the next payment generation. A
retry against the same observation-scope digest is refused. Likewise, a `satisfied` event is
carried forward only while a deterministic impact check proves that the current camera,
matching subject closure, observation mode/settings, moments, references, and other
medium-visible contributors are unchanged. A relevant change makes a successor generation
`due`; an unknown impact fails closed and requires re-judgment. Final acceptance verifies that
the discharge covers the final canonical observation scope, or a provably equivalent scope,
rather than accepting an attractive render from an obsolete prefix.

Authority lineage is explicit even when the global bundle digest is unchanged. A selected
owner or payer consumer-view generation change runs a revision-checked carry-forward/
supersession transaction. Open and falsified lineage survives contract rebinding as required by
HIR-0142. A satisfied event survives only when its exact evidence remains valid under the new
binding; otherwise the successor is `due`. Retry, restart, rematerialization, or a flattering
later render cannot erase debt.

Nonpersistent lifecycle is not deletion. A `layer` or `window` debt must be satisfied before
its typed valid-through boundary; reaching that boundary unpaid is an acceptance/authority
failure that names the debt and legal repair. Only an explicit selected-authority transaction
may supersede or retire it. Final acceptance independently refuses every current required
`pending_not_due`, `due`, or `falsified` debt, including a layer/window row whose expiry gate
was missed or malformed. `pending_not_due` at acceptance is reported as a missing legal
payment boundary, not silently waived.

### Cross-layer fault routing

Three identities remain independent:

- **decision owner** — owns the proposition and any decision amendment;
- **payer closure** — makes observation legally possible; and
- **fault owner** — owns the mutation needed after the actual observation.

When a later judgment fails, producer attribution traverses the exact multi-layer replay
prefix and its unit digests. A subject-form observation may implicate a geometry producer;
a framing observation may implicate the camera producer; an interaction may require a typed
coordination owner. No role match or several equally legal matches causes abstention and
enumerates the candidates. It must not fall back to every unit in the activation layer or
assume that the payer is the repair owner.

### Slice-1 proof

- Camera-owned framing debt whose subject is produced later: the camera artifact passes,
  debt remains `pending_not_due`, and no critic is called on the empty plate.
- Matching carrier already accepted upstream: the debt activates at owner composition rather
  than demanding a fictional future provider.
- Matching subject completed by same-layer fan-in: the first dependency-complete owner-layer
  prefix activates it.
- Matching form payer: the exact debt becomes due at the first dependency-complete prefix
  and is judged once using the owner's frames and references.
- Unrelated mesh before the matching form payer: the debt remains inactive.
- Partial subject producer before its dependency-complete aggregate: the debt remains
  inactive.
- Due payment with a matching carrier but no relevant subject pixels or optical signal:
  HIR-0032 refuses the critic, records a typed payment-attempt failure, and leaves the debt
  `due`; it is neither reclassified as inactive nor falsely judged.
- Repeating that direct build/restart with unchanged authority and observation scope resurfaces
  the same attempt without another render or critic spend.
- Hall-appearance debt assigned to a camera/framing owner: the plan gate rejects semantic
  misownership instead of deferring it.
- Passing judgment writes a hash-pinned discharge; rematerialization and restart preserve
  it only when the authority lineage and current observation-scope identities still match.
- A relevant camera, subject, render-setting, or payer-view change creates a successor due
  generation; an unrelated change may carry satisfaction forward only with a deterministic
  equivalence proof.
- A falsified generation cannot be paid again until a legal repair or authority transaction
  changes the relevant digest, and its lineage survives contract rebinding.
- Do not encode the exposing shot's names, frames, departments, or layer count.

### Checkpoint reuse on the exposing run

After the selected plan is explicitly migrated or republished under the debt schema, re-run
composed canonical from the already-passed unit artifacts. Preserve matching checkpoints and
unit digests. Do not `--until-clean`, do not `--discard-accepted`, and do not perform a global
semantic replan when the selected DAG and ownership are unchanged.

## Slice 2 — Compiled due-time and payer closure for every debt kind

**Owning record after promotion:** an ADR. HIR-0134 already rejects a DAG with no
dependency-complete bbox payer; that closure is not yet universal. HIR-0151 separately
supplies diagnostic forecasts to partial producers.

### Universal debt compiler

Every published debt compiles an activation point and a conjunction of legal payer
requirements:

- scene / projected rows that need a camera: HIR-0085;
- image-contract rows: HIR-0110 plus HIR-0160;
- deferred subject composition: HIR-0158 / HIR-0134, with HIR-0151 forecasts before
  authoritative payment;
- HIR-0137 image/human judgment: slice 1;
- required `visible_fraction`: existing repair-owner activation (HIR-0051,
  HIR-0132).

The compiler is a dependency-free domain mechanism shared by publication,
materialization, runtime scheduling, and acceptance. It emits one closed prerequisite
specification that the debt definition and later activation binding both consume, rather
than letting each boundary rediscover provider rules. A debt can require several witnesses
— for example camera AND matching carrier AND optical signal — and each witness names its
canonical registry-derived capability family.

Closure is refined at three boundaries:

1. **Sparse publication:** prove an acyclic reachable layer capability and subject-interface
   promise exists for every prerequisite.
2. **JIT materialization:** pin exact provider units, interfaces, roles, write families,
   dependency closure, activation layer, and unit digests.
3. **Runtime replay:** verify those exact providers are in the evaluated cumulative prefix
   before scheduling payment.

Publication and materialization fail closed when the selected DAG has no legal closure and
name the debt, every missing prerequisite, the observed providers, and the layers/units that
could cover it. A sibling outside the dependency closure is reported but does not pay.
`reason` prose, layer titles, role-name keywords, and model confidence do not satisfy the
gate.

Semantic ownership closes independently. The owner layer must own the proposition and its
axis and must declare a legal fault-routing policy. It need not be the producer ultimately
repaired after observation; that owner is derived from the typed fault and replay prefix. A
downstream observation provider cannot make an otherwise misowned requirement publishable.

### Cheap fixture sweep (no model spend)

Gate-only combinations, not production briefs:

- camera-only root that owns an image-domain requirement;
- owner whose matching carrier already exists in an accepted ancestor;
- owner-layer fan-in whose final unit completes the matching subject;
- camera-framing debt followed by an unrelated mesh and then the matching subject;
- partial subject producer followed by its dependency-complete aggregate;
- shading root with image-contract debt and no carrier;
- look-owning unit whose dependency closure has no camera;
- Workbench judgment whose only potential provider is a volume or compositor it cannot
  display;
- camera-providing layer that reserves a form namespace (HIR-0128);
- appearance claim classified to a camera-only owner capability, plus an independent verifier
  abstention/mismatch case for the statement-to-classification boundary;
- deferred bbox with no later geometry-capable layer.

These are the kill for "the next shot will break differently." They cover shape,
subject relevance, medium compatibility, and closure rather than one fixture's nouns.

### Universal-closure exit criteria

- Every required debt kind is registered with its prerequisite compiler and discharge
  boundary; an unregistered kind is a publication error.
- Every semantic claim/property maps to a closed owner capability/interface; an unregistered
  mapping or verifier abstention blocks publication rather than defaulting to the declared
  owner.
- No consumer owns a second hand-maintained provider-family table.
- The gate rejects a debt with no reachable payer before any builder or critic spend.
- Materialization rejects a promised provider that fails to pin an exact unit closure.
- Runtime never calls a payer whose pinned prefix identities are stale or incomplete.
- Acceptance can enumerate every satisfied, unpaid, falsified, and superseded debt without
  reading transcripts or historical runs.

## Slice 3 — Tracked small-scale sealed scene

**Owning record:** eval fixture plus architecture-test ratchet. Staged-pipeline
already requires a working vertical slice; there must be no period where the
architecture cannot run end to end at small scale.

A two-layer still — camera provide, then one hero mesh with procedural construction — is
a **tracked eval**, not a playground shot. Fresh run from brief through empty-scene
acceptance is the regression. The exposing motion chamber returns to what it is good at:
load-testing deferred subjects and generate routes **after** the small seal exists.

Use two levels of ratchet:

- a deterministic no-model lifecycle fixture in ordinary tests, proving authority,
  activation, state, replay, and acceptance cheaply; and
- a periodic real-model end-to-end eval recording cost, latency, turns, manual
  interventions, and accepted units, without making nondeterministic provider spend the
  only CI signal.

The fixture matrix is part of the mechanism, not follow-up polish:

| Fixture | Required result |
| --- | --- |
| Camera -> matching hero mesh | Camera artifact passes; debt activates on form and is paid once |
| Matching carrier already exists in an accepted ancestor | Debt activates at the owner composition; no invented future payer is required |
| Matching carrier is completed by owner-layer fan-in | Debt activates at the first dependency-complete owner-layer prefix |
| Camera -> unrelated mesh -> matching mesh | Unrelated mesh does not activate the debt |
| Camera with no reachable matching carrier | Publication fails before model/build spend |
| Partial mesh -> dependency-complete aggregate | Activation waits for the complete declared payer closure |
| Volume carrier | Activates only with an observation medium that can expose the volume |
| Compositor-only subject | Activates only with a compositor-observing medium and exact interface |
| Hall appearance assigned to camera framing | Plan gate rejects semantic misownership |
| Matching provider exists only as a sibling outside dependency closure | Publication fails and names the bad DAG edge/closure |
| Debt survives rematerialization/replan | Open/falsified lineage is mapped; satisfied lineage survives only exact evidence equivalence, otherwise it becomes due |
| Satisfied debt followed by a relevant camera/subject/settings change | A successor payment generation becomes due; old discharge remains history |
| Satisfied debt followed by engine/version or external-asset change | Observation scope changes and requires re-judgment |
| Due carrier with no relevant pixels/signal | No critic call; typed attempt failure; debt remains due |
| Direct restart after identical no-signal attempt | The same typed stop resurfaces without a render or critic call |
| Final acceptance with unresolved debt | Acceptance fails and names the debt and missing payer/discharge |
| Direct acceptance with unresolved `layer`/`window` debt | Acceptance fails even if an earlier expiry gate was skipped |
| Failing later judgment | Finding names exact decision, payer, and observed fault candidates |
| Camera-only framing fault | Fault routes to the camera owner, not automatically to the payer |
| Subject-form-only fault | Fault routes to the matching geometry/form producer |
| True camera/subject interaction | Fault routes to a declared coordination owner or abstains if none exists |
| No legal fault-owner match | Routing abstains and enumerates the observed closure; it never falls back to all units |
| Several equally legal fault owners | Routing abstains and enumerates candidates; payer identity is not the tie-breaker |

Do not encode fixture display names, coordinates, frames, layer count, or department terms
into core. Tests assert the generic invariants: an artifact can pass with legally deferred
debt, only a relevant complete payer activates it, payment is durable and exact, and final
acceptance cannot lose the debt.

## Slice 4 — Dispatch driver, not a negotiator

**Owning record after promotion:** HIR plus CLI contract. HIR-0011 already says
an orchestrator should pick the cheapest owning transaction before another paid
round. That loop is not a public driver.

### Closed stop envelope

Exit codes and `contract_gap` are too coarse to dispatch directly. Every run-scoped
unaccepted boundary publishes `vfx-harness.stop-envelope/v1` before returning. Standalone
preflight uses the environment-result exception defined below. The envelope contains:

- closed `stop_class`, stage, and whether the stop is retryable;
- run, selected bundle/view, layer, unit, unit-plan, unit-digest, candidate, checkpoint,
  and settings identities that apply;
- exact finding ids, a stable `cause_fingerprint` over the violated contract and causal
  classification, and a separate `attempt_evidence_digest` over the exact authority,
  checkpoint, candidate, readings, and evidence identities;
- current artifact/debt state and the owning boundary's classification evidence;
- exactly one typed action with a closed transaction target, derived dispatch mode,
  typed state preconditions, and content-addressed evidence references;
- one budget key, one typed postcondition that would constitute authoritative progress,
  and the required transaction-receipt schema; and
- human-readable expected/found/next-action text derived from the same fields.

The owning boundary, not the driver, declares the exact action. Retryability is derived
only from an exact-unit retry action. A generic `contract_gap`, max-turns string, exit
code, or transcript sentence is never enough to infer a replan or retry. A valid action
describes the only legal route; it is not automatically dispatchable without the receipt
protocol and controller described below.

The two digests have different jobs. `cause_fingerprint` is stable across attempts and hashes
the stop class, violated invariant/contract, semantic owner scope, and normalized causal facts;
it excludes run ids, timestamps, and candidate filenames. `attempt_evidence_digest` pins the
exact selected authority, unit/checkpoint/candidate identities, readings, and evidence used for
this classification. Evidence content hashes, not regenerable run-local filenames or
timestamps, establish identity; locators remain separate audit fields. The first hash enforces
loop budgets; the second prevents stale or substituted evidence. Neither is reconstructed from
human-readable detail.

### Stop taxonomy

| Stop class | Legal next action |
| --- | --- |
| `local_implementation_miss` | Retry the exact unit only when its envelope marks the miss retryable and budget remains |
| `authority_defect` | Execute the envelope's one named authority action; a later independently classified stop may name revision-checked replan only after amended authority exists |
| `harness_defect` | Stop paid execution and route the cause fingerprint plus exact evidence to engineering |
| `infrastructure_failure` | Recover environment/session; resume only from a legal checkpoint and journal identity |
| `human_decision_required` | Escalate the exact question; automation does not answer it |

`evidence_not_due` is deliberately **not** a stop class. It is ordinary successful progress:
the executable artifact passes, the debt ledger remains `pending_not_due`, and scheduling
advances because current selected authority proves a reachable typed provider promise. The
runtime currently records that fact through debt state and ordinary successful scheduling; the
standalone `EvidenceNotDue` classifier value is pure-domain scaffolding and is not yet emitted
in a run summary. The pure classifier detects the same `cause_fingerprint` against the same
authoritative before-state, but currently refuses to route it. Future routing requires a parsed,
verified transaction receipt plus a content-addressed `repeated-dispatch-defect/v1` record that
binds the full stable current stop and prior evaluation. No runtime producer or consumer for
those records exists yet.

Existing lower-level causes map into this taxonomy only at their owning boundary:

- a builder evidence miss may be local, authority-related, or a harness defect;
- `contract_gap` may name missing plan authority, premature evidence billing, or an
  unclassified mechanism hole;
- max-turns may permit a digest-bound resume, require rematerialization, or prove a stalled
  transaction; and
- `hypothesis_falsified` authorizes consumption only after amended selected authority exists.

`vfx units replan --falsification` moves durable state to already-published amended
authority; it does not author the amendment. A stop envelope expresses exactly one action: the
current authority-defect producers name `publish_validated_amendment`. Only after that action
has actually published and selected new authority may a later independently classified stop
name `apply_revision_checked_replan`; the two are never smuggled into one action. Invalidation
preview is a read-only precondition executed inside the apply transaction. It may be journaled
for audit, but it is not a standalone progress-producing action and consumes no dispatch
attempt. If preview requires a policy choice rather than a deterministic check, the boundary
emits `human_decision_required`.

### Dispatchable transaction protocol — implemented for environment reverification only

A public transaction is not legal for automated dispatch merely because it accepts a CLI
flag. It must implement `vfx-harness.transaction-receipt/v1`, keyed by the controller's
idempotency key, with `prepared -> running -> terminal` phases. The receipt pins the
transaction id, exact authoritative input revision, declared postcondition, model/session or
external-request ids where applicable, authoritative commit marker, spend, and terminal
result. The transaction publishes `prepared` before any model call, external side effect, or
authority mutation and records the same key in its authoritative commit.

The protocol exposes a read-only query by key. Recovery may resume `running` work only when
the transaction's ordinary checkpoint/session/authority identities still authorize resume. A
terminal receipt is reconciled with the authoritative commit and returned without executing
again. If a crash occurred during paid/external work and the result cannot be proven or legally
resumed, recovery halts with an infrastructure/harness stop; absence of a controller
`committed` row is never permission to repeat an uncertain transaction.

HIR-0166 implements the generic immutable key-addressable receipt chain plus the explicit
`vfx recover-environment` adapter. The adapter records `prepared` before its read-only probe,
keeps a still-broken environment `running`, commits only a fully passing exact probe, reconciles
a unique direct receipt orphan before any new observation, reconciles a commit-before-terminal
crash, and is evaluated by a separate byte-verifying boundary. Semantically identical source runs
converge on one key even when their run-local evidence locators differ. It does not edit the
environment or run automatically. The other six actions remain non-dispatchable.
In particular, the existing builder resume record does not seal the selected bundle/view, exact
unit plan and candidate, model session and phase, and durable write-ahead-log identity needed by
`resume_checkpointed_session`.

### Deterministic controller loop — not implemented

`vfx run --until-accepted` (name TBD) performs only this state machine:

1. Read the selected run's structured stop envelope and its cited authoritative records.
2. Verify every identity still matches current selected authority and durable state.
3. Verify the stable cause fingerprint and exact `attempt_evidence_digest`. Refuse stale evidence,
   or the same cause against the same authoritative before-state after an attempt failed to
   make its declared postcondition true.
4. Validate the envelope's one typed action, dispatch mode, target, and current
   preconditions. The controller never chooses among alternatives or invents a transaction.
5. Derive a stable idempotency key from `(receipt schema, declared transaction id, exact
   authoritative before revision, attempt_evidence_digest)`. Human-readable detail, timestamps,
   filenames, and other regenerable envelope fields are canonical exclusions. Atomically append
   a controller `prepared` journal record **before** invoking the transaction.
6. Invoke the existing public transaction with that key. The transaction must consume the key
   idempotently and remain revision-checked; model judgment, if needed, stays inside that
   bounded transaction and its tool policy.
7. Re-read authority and state. Progress requires the declared domain digest/state
   postcondition, not a changed log message, a journal row, or another paid attempt. Append a
   `committed` or `failed` journal record with before/after identities, spend,
   `cause_fingerprint`, and `attempt_evidence_digest`.
8. On recovery, resolve any controller `prepared` record through the transaction's durable
   key-addressable receipt and authoritative commit before considering another invocation.
   Resume only when its typed receipt authorizes resume; halt on an unknowable middle. Then
   continue from the newly selected envelope or halt.

The command requires monotone limits for transaction count, model cost, elapsed time, and
per-cause attempts. It also provides a read-only `--explain`/dry-run view showing the
classification, legal transaction, invalidation scope, budget, and expected postcondition.
Every selected action must declare and then produce a change in authoritative domain state or
an authoritative digest. A new log line, journal row, attempt counter, run id, or model response
does not count as progress. Environment recovery counts only when a typed environment/session
state changes and the original precondition is re-verified.

The controller may never:

- use `--force` or skip a gate;
- widen roles, controls, construction route, or script authority;
- rewrite plans or requirement rows informally;
- answer a human decision;
- use `--discard-accepted` or approve a hard-constraint change;
- consume stale identities or a finding against unchanged authority; or
- repeatedly consume an unchanged cause against an unchanged authoritative state.

This is the legitimate 80% of "an orchestrator on top." The other 20% — delete the
eight rows, broaden mutation, skip no-signal, or patch until green — is an untyped repair
owner and is forbidden.

### Dispatch proof

- Every stop class has a fixture proving its one legal transition and at least one refused
  illegal transition.
- Crash before controller prepare performs no mutation; crash between controller prepare and
  transaction prepare safely enters the same key; crash during a model/external call resumes by
  receipt or halts; crash after authority mutation but before controller commit reconciles the
  authoritative key and never executes twice.
- Regenerating only human-readable envelope detail yields the same idempotency key; changing
  exact evidence without changing authority is detected rather than treated as progress.
- A receipt with an unknowable paid/external outcome halts and never retries speculatively.
- Read-only invalidation preview cannot satisfy a progress postcondition or consume a
  standalone dispatch attempt.
- A stale bundle, unit digest, checkpoint, or finding is refused before mutation.
- An unchanged cause and authoritative before-state halt without a second paid round, while
  exact attempt-evidence digests still prevent stale evidence from being substituted.
- A transaction that returns success without its authoritative postcondition is a
  `harness_defect`, not progress.
- The driver cannot reach `--force`, `--discard-accepted`, hard-constraint approval, or an
  arbitrary command through any envelope payload.

### Preflight and envelope publication boundary

`vfx run` must allocate its structured invocation/run layout before strict preflight so an
environment stop has a legal run-local envelope destination. Standalone `vfx preflight
--strict` may have no shot or run; it emits a separate typed
`vfx-harness.environment-result/v2` to stdout and, when explicitly requested, an output path.
It does not create or advance shot authority merely to obtain an envelope.

If a run boundary cannot atomically publish or read back its envelope, the driver halts paid
execution with a minimal terminal `process_error` naming that observability failure. It must
not guess a transition from a partial envelope, and the fallback status itself is not treated
as authoritative recovery input.

## Schema, authority, and migration

This design changes selected authority and durable state. Strict migration applies:

- Version the debt definition, activation binding, debt-state/payment-attempt event, stop
  envelope, standalone environment result, transaction receipt, and dispatch journal.
- If provisional domain bindings gain subject, medium, or activation fields, bump the
  requirements/consumer-view schema rather than treating missing fields as "any carrier".
- Bump every digest schema whose identity payload changes. A new field must not silently
  make old and new unit/debt identities appear comparable.
- Historical immutable bundles and runs remain audit evidence. They are not edited and do
  not become current authority by proximity.
- An explicit migration/republish transaction creates a new selected generation. It may
  preserve accepted unit checkpoints only when current-schema unit digests and dependency
  identities match exactly.
- The migration publishes a total lineage map from every current open, falsified, or satisfied
  provisional requirement/finding to one successor definition or a typed retirement
  transaction. Retirement pins the source definition/generation, closed retirement kind,
  selected successor requirement or global decision that authorizes retirement, target
  authority revision, and proof that no surviving requirement, binding, or consumer still
  references the debt. Free-form review prose is audit context, not retirement authority. A
  row cannot disappear because its resolution kind changed.
- `pending_not_due`, `due`, and `falsified` lineage carries forward. A `satisfied` event carries
  forward only when its evidence and current observation-scope equivalence validate under the
  new schema; otherwise the successor becomes `due`. Migration cannot launder falsification or
  turn missing evidence into satisfaction.
- A same-bundle consumer-view change still creates a new authority generation and executes the
  same revision-checked lineage transaction (HIR-0142); bundle equality is not proof that JIT
  debt identity stayed fixed.
- When subject relevance, observation medium, or payer closure cannot be derived without a
  model guess, migration fails closed and requires an explicit reviewed republication.
- Old debt-state or dispatch rows keyed to another bundle/schema remain inert. There is no
  silent compatibility window unless an ADR defines an expiry and deterministic mapping.
- The schema, compiler, migration transaction, lifecycle reader, and acceptance refusal select
  atomically. Intermediate implementation commits may exist, but production cannot select a
  generation that writes the new authority while an old reader can ignore it.

The likely authority split is:

- immutable debt definitions in the selected plan consumer view;
- immutable activation bindings in the selected payer consumer view, linked to definition
  digests;
- append-only debt lifecycle/discharge events under shot-root `state/`;
- durable key-addressable transaction receipts and controller journal under the owning
  shot/invocation state boundary;
- run-local renders, comparisons, stop envelopes, and dispatch evidence in the active
  structured run; and
- accepted scripts/checkpoints in the existing deterministic build chain.

No prior run is a write destination, and no run artifact is promoted into plan/state
authority without the typed transaction above.

## Observability and product progress

Target observability after the unimplemented dispatch milestone should expose, without
transcript inspection:

- artifact counts by state and exact accepted unit digests;
- debt counts and ids by `pending_not_due`, `due`, `satisfied`, `falsified`, `retired`, and
  `superseded` lineage outcome;
- each debt's definition generation, activation-binding generation, decision owner, compiled
  activation boundary, payer closure, observation scope, and discharge/finding/payment-failure
  locator;
- stop-envelope class, cause fingerprint, attempt-evidence digest, and its single declared
  transaction;
- dispatch attempts, before/after authority identities, postcondition, cost, turns, and
  elapsed time, including transaction-receipt and controller prepared/committed/failed
  status; and
- repeated causes against unchanged authority and manual interventions.

Track these ratchets across the deterministic fixture, real-model still, and production
load fixture:

- accepted work units per model dollar and per wall-clock hour;
- manual interventions per accepted layer and per accepted shot;
- paid attempts per stable cause and exact attempt-evidence identity;
- critic calls on ineligible/no-signal prefixes (target: zero);
- debts with no reachable payer (target: rejected before model spend);
- time from a classified stop to its owning legal transaction; and
- number of preserved checkpoints across amendment/rematerialization.

These metrics do not lower acceptance. They reveal whether the control system is gaining
liveness or merely producing more precise stops.

## Implementation plan

Each milestone is a separately reviewable mechanism with a failing fixture first. Do not
start the dispatcher while activation or stop classification still relies on string/exit-code
inference.

### Milestone 0 — Pin reproduction and authority

**Work**

- Record the exposing run's shot id, manifest, selected bundle/view hashes, layer report,
  unit-state records, debt inputs, render/settings hashes, and finding locator.
- Add one minimal deterministic reproduction that fails under the current unconditional
  HIR-0137 composition schedule.
- Classify the stop explicitly as evidence scheduling plus recovery dispatch, not a builder
  quality failure.

**Exit**

- The fixture fails for the same mechanism without production-shot names or frames.
- The production evidence is readable in canonical run-artifact order and does not depend
  on terminal prose.

### Milestone 1 — Judgment-debt domain and lifecycle

**Work**

- Add dependency-free typed definition, activation-binding, prerequisite,
  observation-medium, judgment-authority, lifecycle/payment-event, and digest contracts
  under `domain`.
- Derive atomic proposition ids from the canonical requirement/domain/claim/axis/subject/
  moment tuple; add strict parsers, canonical serialization, digest fixtures, and rejection
  of model-authored or ambiguous ids.
- Add the append-only authority-generation lifecycle ledger, qualified discharge evidence,
  payment-attempt failures, successor generations, and nonpersistent expiry refusal.
- Represent pre-activation state with `binding: null`; selecting an immutable binding starts
  the first payment generation, and successor records point only backward.
- Add explicit same-bundle view lineage plus selected-generation carry-forward/
  supersession and typed retirement transactions.
- Add acceptance enumeration/refusal for every unresolved current required debt and
  validation of satisfaction against the final observation scope.

**Exit**

- The state machine rejects illegal transitions, stale definition/binding identities,
  duplicate payment, self-certification, evidence-free satisfaction, and retry of an
  unchanged falsified generation.
- Open/falsified debt survives contract rebinding; satisfied debt survives only a proven
  equivalent observation scope.
- Acceptance fails closed on `pending_not_due`, `due`, `falsified`, or expired unresolved
  debt.

### Milestone 2 — Relevant activation and payer closure

**Work**

- Extend global ownership/interface authority with closed semantic claim/property, owner
  axis, subject-interface, and provider-promise fields; make declared owner-capability
  mismatch mechanically rejectable.
- Add the canonical claim/property-to-owner-capability registry and an independent
  statement-to-classification verification/abstention boundary.
- Compile sparse provider promises and the legal `activates_at` layer from the selected DAG.
- Bind debt subject selectors/interfaces, owner axes/frames, observation medium, and
  judgment authority during JIT materialization through a closed schema.
- Publish the immutable activation binding with exact dependency-complete payer unit digests
  and reject unrelated/partial providers.
- Verify runtime cumulative-prefix identities before moving debt to `due`.
- Generalize the compiler registry to existing camera, signal, carrier, deferred bbox, and
  visibility debts without duplicating family tables.

**Exit**

- Every fixture in Slice 2 passes, including unrelated carrier and mode-incompatible
  provider failures, plus an accepted ancestor carrier and owner-layer fan-in payer.
- A DAG with no legal payer is rejected before builder or critic spend.
- A matching provider cannot rescue an incorrectly owned proposition, and a valid owner is
  not forced to become the eventual repair producer.

### Milestone 3 — Composed scheduling, payment, and fault routing

**Implementation note (2026-08-31):** lifecycle scheduling, exact replay receipts,
pre-render observation identity, carrier-aware environment capture, canonical render
capture, no-signal failure persistence, unchanged direct-restart suppression, and
successful-verdict provenance are landed. Discharge equivalence/successor generations,
the remaining typed attempt-failure classes, and cross-layer fault routing are still open.

**Work**

- Replace owner-layer-wide provisional-decision loading with current-prefix debt loading by
  lifecycle and exact activation identity.
- Schedule no raster or critic work for `pending_not_due` debt.
- At `due`, replay through HIR-0117's fresh evaluated-state barrier, explicitly evaluate every
  selected frame as required by HIR-0116, and render the declared medium at the owner
  frames/references.
- Compile the closed observation-scope provenance record, including artifact chain, assets,
  engine/version, render state, frames/references, and stochastic inputs; unknown influence
  fails closed.
- Call only the independently qualified judgment authority. Write one exact satisfaction or
  falsification event when judgment is eligible; on missing subject/signal/evidence, write a
  typed payment-attempt failure and leave the debt `due` without invoking the critic.
- Revalidate a discharge after relevant prefix changes and at final canonical acceptance;
  create a successor payment generation when deterministic equivalence cannot be proven.
- Persist and suppress an unchanged failed payment tuple across every public entry point,
  including direct build/restart without the controller.
- Build the cross-layer producer index from replayed unit digests and route observed faults
  to exact owners; abstain on ambiguity.
- Publish layer/run summaries that distinguish artifact pass from open judgment debt.

**Exit**

- The key acceptance test passes: Layer 1's executable checkpoint is accepted, its visual
  debt remains pending, Layer 2 activates and judges it exactly once, and no global replan
  occurs unless selected global authority is actually wrong or otherwise changes.
- A new run reuses the exposing run's accepted unit artifacts after explicit schema migration;
  this is checkpoint reuse, not automatic model-session resume.
- No empty/no-signal plate reaches a critic, and no old satisfaction pays a changed final
  observation scope.
- Camera-only, geometry-only, coordination, zero-match, and multi-match fault fixtures prove
  that payer identity never substitutes for fault ownership.

### Milestone 4 — Small sealed-scene ratchet

**Work**

- Land the deterministic two-layer lifecycle fixture in the ordinary architecture/
  integration suite.
- Add the real-model two-layer still eval and publish cost/latency baselines.
- Run the heterogeneous activation matrix and injected failures.

**Exit**

- A fresh small shot reaches empty-scene acceptance through the public CLI.
- The ratchet remains green before returning to the motion-chamber load fixture.

### Milestone 5 — Stop envelope and classification

**Implementation note (2026-08-31):** the strict envelope/action/evidence/state/
postcondition schemas, immutable run publication and read-back, run-before-preflight
boundary, standalone environment result, generic fail-closed fallback, inspect surface,
and the owning classifiers named in the implementation-status section are landed.
`evidence_not_due` remains successful continuation. Classification is intentionally
incomplete: no exact local-implementation retry or checkpointed-session-resume producer
has earned those actions, and other legacy builder/session/replay stops fall back to
`harness_defect` rather than being guessed from their strings or exit codes.
HIR-0167 additionally gives all current amendment-producing boundaries one verified semantic
selected-authority assertion and strict amendment-v2 target/postcondition. Corrupt selection
cannot become initial-plan amendment authority, and hard-constraint findings route to a typed
human question. These are closed proposal and classification contracts; they do not make
amendment or human-decision actions dispatchable.

**Work**

- Add the closed stop schema and pure classifier inputs/outputs.
- Make each run-scoped planning/gate, materialization, builder, composition, acceptance, and
  infrastructure boundary publish an envelope before returning unaccepted.
- Allocate `vfx run` layout before strict preflight; give standalone preflight its separate
  typed environment-result contract and pin envelope-publication failure behavior.
- Remove recovery meaning from generic exit codes and free-form detail; retain both only as
  user-facing summaries.
- Pin every stop class, legal transaction, stable cause fingerprint, exact
  `attempt_evidence_digest`, and refusal in tests. Prove `evidence_not_due` is
  continuation, not a stop.

**Exit**

- Every run-scoped unaccepted boundary publishes and reads back a valid envelope; standalone
  preflight emits a valid environment result. Envelope publication failure halts without
  dispatching from partial state.
- `contract_gap`, max-turns, and builder failure cannot dispatch without the more specific
  typed classification.

### Milestone 6 — Bounded dispatch driver

**Implementation note (2026-08-31):** controller implementation has not started. HIR-0166
lands the generic durable receipt protocol and one explicit key-consuming
`recover_environment` transaction with independent evaluation. HIR-0167 lands the shared
semantic before-state and strict amendment-v2 proposal, but not amendment execution. Plan/JIT
pointers still lack a shared monotone revision, lock, and compare-and-swap commit; global planning
does not yet bind the complete input/candidate gate attestation needed to name one exact successor;
and there is no amendment adapter, commit record, receipt reconciliation, or independent evaluator.
There is no controller command or persistent controller journal, and no other action is
dispatchable.

**Work**

- Add the public controller with explain/dry-run mode, monotone budgets, persistent journal,
  prepared/committed/failed records, canonical idempotency keys, identity checks, and
  authoritative-progress postconditions. Read-only preview is an apply-transaction
  precondition, never progress.
- Invoke only existing audited public transactions; add a new transaction only through its
  own HIR and tests. A transaction is not dispatchable until it consumes the controller key
  idempotently and publishes a durable queryable transaction receipt.
- Prove crash before controller prepare, after controller prepare, during a model/external
  call, after authority mutation but before controller commit, and after terminal commit, plus
  stale identity, unchanged cause/authority, wording-only envelope regeneration, changed exact
  evidence, budget exhaustion, and no-op success handling.

**Exit**

- The controller advances every classified fixture to acceptance or one correct terminal
  escalation without repeating a cause against unchanged authoritative state.
- No controller path can force, skip, broaden, discard accepted work, approve hard changes,
  or execute arbitrary commands.

### Milestone 7 — Production validation and promotion

**Work**

- Run the small sealed-scene ratchet first, then the exposing motion chamber from preserved
  checkpoints, then at least one heterogeneous held-out shot.
- Compare accepted units, cost, time, manual interventions, repeated findings, and preserved
  checkpoints against the recorded baseline.
- Promote the mechanisms through the HIR/ADR/operations records listed below only after the
  evidence is complete.

**Exit**

- No empty-prefix judgment, unnecessary global replan, or repeated unchanged finding occurs.
- A fresh simple scene seals end to end and the complex fixture advances beyond the original
  stop without weakening any gate.

## Rejected alternatives

- **Prompt the materializer not to bind image-domain provisional rows on a
  camera layer.** Mechanical due-time belongs in compilation. Prompt wording
  will recur on the next fixture.
- **Move those requirements to the form layer by global replan.** Correct only
  when sparse ownership is actually wrong. A camera owner may bind camera-framing debt
  whose subject appears later; it may not own hall-form or hall-look appearance merely
  because the camera observes it.
- **Activate on the first mesh/volume/compositor anywhere in the prefix.** An unrelated
  or incomplete carrier would recreate premature judgment one layer later. Activation
  requires relevant, dependency-complete, medium-compatible payer closure.
- **Put future payer digests inside one owner-time mutable debt card.** Those units do not
  exist when the owner definition is selected; later mutation would break digest authority.
  Definition and activation binding are separate linked records.
- **Treat observation medium and judge authority as one field.** Workbench/EEVEE describes
  what can be seen; executable, qualified independent, or reviewed-human authority describes
  who may decide it. Mixing them permits unqualified self-certification.
- **Treat layer `passed` as payment of every owned requirement.** Artifact acceptance and
  debt satisfaction are independent; final acceptance reads the debt ledger.
- **Waive HIR-0032 when the plate is empty.** Restores false pass on black.
- **Call no-signal a qualitative falsification.** It would turn an inability to observe into a
  statement about appearance. The payment attempt fails and debt stays due.
- **Make `evidence_not_due` an unaccepted stop.** The proven executable artifact should
  advance; debt state, not recovery dispatch, carries the future obligation.
- **Supervisor / coding-agent loop that patches until green.** Wrong owner,
  no finding, hole waits for the next shot.
- **Larger builder sessions or extra mutation so the camera unit can invent a
  proxy mesh.** HIR-0128 exists so that cannot happen.
- **`--force` past composed `contract_gap`.** Debugging experiment, never a
  deliverable.
- **Dispatch directly from exit code, max-turns, or `contract_gap`.** Those values do not
  prove retryability, ownership, or a legal authority transaction.
- **Write the dispatch journal after mutation.** A crash in between can execute the same
  transaction twice. Prepared intent and an idempotency key must exist first.

## Order and dependencies

1. Milestones 0–3 pin the failure, establish debt authority/lifecycle, compile relevant
   payer closure, and correct composed scheduling. This is the root-cause fix.
2. Milestone 4 makes a sealed small scene a permanent ratchet before more production load
   testing.
3. Milestone 5 replaces overloaded failures with a closed stop envelope. Classification
   must be complete before automation.
4. Milestone 6 removes the human tax on classified stops. A dispatcher over an incomplete
   due-time or stop table would only route people back to replan faster.
5. Milestone 7 validates the mechanisms on the exposing fixture and held-out shots before
   promotion.

Milestones 1–3 may be reviewed as separate changes, but their new schema, migration,
compiler, runtime reader, and acceptance refusal are one production selection boundary. No
intermediate deployment may write debt that the active runtime can ignore.

No new philosophy. The last stretch is finishing two seams the existing
enforcement already implies: **when** a debt is due, and **who** consumes a
finding.

## Leaving research

| Outcome | Record |
| --- | --- |
| Judgment-debt definition plus activation binding, lineage, lifecycle, scheduling, independent discharge, and cross-layer fault map | new HIR extending HIR-0137 / HIR-0160; strict schema migration; AGENTS.md in the same change |
| Universal compiled due-time and relevant payer closure | ADR (successor concern to ADR-0005 / ADR-0006); HIR for publication/JIT/runtime gates |
| Two-layer still as ratchet | deterministic architecture/integration fixture plus tracked real-model eval; not core vocabulary |
| Closed stop envelope, standalone environment result, shared selected-authority identity, and owning-boundary classification | HIR-0164, HIR-0166, and HIR-0167 plus versioned schemas and run-summary contract |
| `--until-accepted` bounded dispatch | HIR plus public CLI and operations procedure; transaction table, prepared journal/idempotency contract, and safety refusals pinned by tests |
| Production promotion | recorded small-scene, exposing-fixture, and held-out evidence with cost/liveness comparison |

Until those records exist, do not treat this file as permission to skip a gate,
replan a selected bundle, or add a supervisor.
