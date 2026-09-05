# Changelog

All notable changes to VFX Harness are recorded here. Detailed causal reasoning and validation
live in the linked Harness Improvement Records.

## Unreleased

### Added

- Deferred-subject rows now tell each producer who shares their projected union and how
  much room is left on the irreversible side: the unit card lists the union's producers in
  dependency order and the ones still pending, and the forecast read-back states the
  measured slack before a partial producer freezes (HIR-0197). Room layer 2 froze its
  facade at 0.349 of a 0.35 height ceiling and the last producer could only grow the union.

- A builder finding whose fault owners are sealed in one earlier layer now amends that
  owner's layer view instead of the stopped layer's, and `vfx run` dispatches it: the
  rematerialization is invoked with every open finding naming the owner as evidence, the
  kickoff renders those findings, and the stopped layer's units are preserved or superseded
  by the authority-state transaction (HIR-0191). Owners spanning layers, an owner outside
  the run's `--from` range, and hard constraints still stop the run naming what to include.

- `vfx run` is now a receipt-backed controller (ADR-0010, HIR-0186): a builder stop that
  names a same-layer `publish_validated_amendment` is dispatched as that layer's
  rematerialization, proven through an immutable commit and independent evaluation,
  recorded as a controller ledger row under the run, and the run continues from the
  replaced layer. Identity convergence and dispatch, per-layer, and USD caps bound the
  loop; `--single-pass` keeps the single-pass form.
- `vfx run <shot>` on a shot with no selected plan authority drafts, verifies, gates, and
  repairs the global plan first, under the same run ID, so one command takes a new shot from
  brief and references to the production chain (HIR-0186).

### Fixed

- A recorded vocabulary gap is now read where the tool writes it
  ([HIR-0218](docs/improvements/HIR-0218-a-recorded-gap-is-read-where-it-is-written.md)).
  `escalate_vocabulary_gap` writes under `<shot>/state/plan-escalations/`; the
  materialization validator read the same relative path under the selected **plan bundle**,
  which is content-addressed and has no `state/` directory. So the read returned nothing in
  every shot and every run, and HIR-0202's rule — that a recorded gap makes a decision legal
  on a structural-only requirement — had never once executed. A materializer escalated the
  same requirement three times, submitted the prescribed decision twice, and was refused
  each time by the rule that had asked for the escalation. The path is now one shared
  function, and materialization validation takes `shot_folder` as a required argument
  distinct from the plan-bundle root. The defect survived its own tests because every
  fixture passed a single directory as both roots; the regression test keeps them apart and
  asserts the staging outcome changes, since a test of the reader alone passes either way.

- A refusal no longer prescribes an action the session has already taken
  ([HIR-0222](docs/improvements/HIR-0222-a-refusal-does-not-prescribe-an-action-already-taken.md)).
  A layer-2 materialization spent 17 refusals alternating between "this requirement has a
  recorded gap, so contracts cannot close it — bind a decision" and "a decision cannot pay
  structural domains — call escalate_vocabulary_gap first", with the gap already recorded.
  Both were true; neither named the resolution, which is to remove the contract bindings the
  gap asserts cannot measure the statement. Where gaps are recorded, the refusal now names
  the bindings blocking the decision, the removal, and that escalating again changes nothing.

- The layer order derived from the DAG now keeps authored position among independent
  layers ([HIR-0221](docs/improvements/HIR-0221-authored-order-survives-when-it-is-already-topological.md)).
  A build died on "selected authority capsules do not preserve the stable topological layer
  order" where the authored order was itself topologically valid — layers 3 and 4 were
  independent, and the sorter simply chose the other valid order. Newly unlocked layers were
  appended to the ready queue with only the new batch sorted, so a layer unlocked early sat
  ahead of a lower-authored layer unlocked later. The queue is now ordered by authored
  position throughout, which is what the function already documented. Stability stays a
  tie-break: an authored order that violates an edge is still reordered and a cyclic DAG is
  still refused.

- Each metric now declares the values it can produce, and a threshold outside them is
  refused at authoring
  ([HIR-0219](docs/improvements/HIR-0219-a-metric-declares-the-values-it-can-produce.md)).
  `transform_return_delta` is a vector length, a quaternion angle, or a maximum of
  absolute steps — non-negative in every branch — and two wholly negative bands over it
  cleared authoring, materialization and the plan gate. A builder proved them impossible
  with six probes and abstained; the controller dispatched a rematerialization that
  authored satisfiable replacements. The loop worked, and cost $3.50 to learn what
  authoring can now refuse for nothing. Range-aware validation already existed, written
  by hand four times for four kinds, so a fifth kind inherited none of it; the interval
  now sits in the canonical kind registry beside the domain and camera declarations. The
  check decides disjointness only and runs last, so the four existing vacuity refusals
  keep their exact wording, and metrics that are genuinely signed declare no range rather
  than receiving a non-negative default.

- One work-unit record now carries one role notation
  ([HIR-0217](docs/improvements/HIR-0217-one-record-carries-one-role-notation.md)).
  HIR-0150 replaced absolute `mutates.roles` with a relative `role_namespace` plus
  `role_members` and left `control_roles` — the one other field whose values must be drawn
  from that list — reading the old absolute shape, with no description of its own. Thirteen
  refusals across seven materializations on all three shots wrote `$self` or a bare member
  into `control_roles`, the notation the schema taught three lines above, and were told only
  which token was wrong. `control_roles` now takes relative members compiled from the same
  namespace by the same rule; a control mapped on a unit with no `role_members` is refused
  naming the write family it cannot derive; and every role-value refusal names the accepted
  set. `dresses` stays absolute by design, since it names another layer's roles.
  The same dialect now reaches `patch_materialization`, which compiles a patched `mutates`
  through the staging tool's own function, and `MutationScope.parse` refuses
  `role_namespace`/`role_members` instead of discarding them. Silently discarding them is
  how a published unit came to mutate nothing at all: a patch in the staging dialect landed
  with its roles dropped, leaving a bare control with no derivable write family and no legal
  `run_bpy`.

- A gate reports every violation it found, and a kickoff renders the evidence it cites
  ([HIR-0216](docs/improvements/HIR-0216-a-gate-reports-every-violation-and-the-kickoff-renders-what-it-cites.md)).
  The artifact policy walked a candidate once, found every violation, and reported one — three
  write-then-probe cycles on adjacent lines of one file, on the most expensive phase in a shot. The
  journal that finalizers compose from is written in the dialect that policy rejects and its header
  never said so. And a controller-dispatched rematerialization cited its blocking findings as a
  file path the materialization workspace correctly refuses, so the session was told it must answer
  findings it could not open. Violations are now collected in source order, journal entries carry a
  policy note on every line that will be refused, and plan-gate findings are compiled into the
  kickoff with their unit names and diagnostics.

- A per-run cap counts its run, and a unit that can mutate nothing is refused before publication
  ([HIR-0215](docs/improvements/HIR-0215-a-cap-counts-its-run-and-a-unit-that-cannot-mutate-is-refused.md)).
  `run_max_replans_per_layer` was enforced against every dispatch a shot had ever made, while the
  two caps beside it were run-scoped, so two live shots reached a permanent ceiling on the number of
  distinct defects a layer could ever have fixed; the durable anti-repeat property is the
  cause-fingerprint guard, which is untouched. Separately, a unit with no mutation roles and a bare
  control derived no write-cluster, passed staging, validation and the terminal gate, and could then
  execute no `run_bpy` at all — the predicate that teaches this needs a namespace to reject and that
  unit had none. The builder's refusal now also names `cannot_express_in_scope`, the only action a
  builder session can take when its unit cannot mutate.

- A falsified unit is reported as a typed stop, and an unclassified boundary's cause has an
  identity
  ([HIR-0214](docs/improvements/HIR-0214-a-falsified-unit-is-a-stop-and-a-cause-has-an-identity.md)).
  The driver selected a `hypothesis_falsified` unit and the claim rejected it with a traceback,
  from a site holding the unit, its state, the legal set, and a finding already in durable state;
  the run then read as a deadlock because nothing named `vfx plan --rematerialize` as the
  transaction. Separately, every unclassified boundary in every shot shared one cause fingerprint
  and one finding id — five different causes across two shots, including an operator's own
  SIGTERM — so the controller's already-dispatched guard would suppress the second real defect in
  a shot as a repeat of the first.

- A sealed projection and its re-derivation select evidence the same way
  ([HIR-0213](docs/improvements/HIR-0213-a-projection-and-its-re-derivation-select-alike.md)).
  HIR-0210 converted the producers of the sealed evidence list to keep every typed measurement
  and missed the outcome projection that re-derives it, so the two sets differed by exactly the
  builder-paid image rows and the projection refused a record that was correct. Every module that
  produces or re-derives sealed evidence is now named and asserted to use the shared predicate.

- A deferred forecast row on the unit card states the condition it turns on
  ([HIR-0212](docs/improvements/HIR-0212-a-forecast-row-states-its-condition-not-a-verdict.md)).
  The card asserted `diagnostic_only: True` at kickoff, where the rule makes that conditional on
  a live measurement, three lines below the sharing data added to warn about the same bound. A
  builder quoted the card, reasoned correctly from it, and the evaluator failed the unit on those
  rows a minute later. The card now carries the irreversible bound and the pending producers and
  derives no status.

- A passing critic verdict records how it was decided
  ([HIR-0211](docs/improvements/HIR-0211-a-passing-critic-verdict-says-how-it-decided.md)). Every
  early-exit branch labelled itself and the ordinary passing critic verdict labelled nothing; one
  consumer defaulted it while the layer evaluation receipt required it, so the first composed
  critic row that ever passed reached mint with an empty field.

- The fourth consumer of the autonomy flag is converted, and the rest are inventoried
  ([HIR-0210](docs/improvements/HIR-0210-the-fourth-consumer-and-the-inventory-that-finds-the-fifth.md)).
  `LayerReplayPointObservation.mint` still demanded autonomy of a bound id and refused every
  builder-paid image row, killing a layer immediately after the unit HIR-0205 unblocked had
  published cleanly; the same function also excluded failing image rows from its failure set, so a
  failed image contract could read as passed. HIR-0205's audit had truncated its search at sixty
  lines and treated the visible subset as complete. The record-of-record path now decides through
  the shared predicates only, and an architecture test pins the twenty-two remaining raw reads.

- Sessions are told the exact deferred MCP tool names instead of guessing them
  ([HIR-0209](docs/improvements/HIR-0209-a-session-is-told-its-exact-tool-names.md)). The
  qualified `mcp__<server>__<tool>` names are built by the harness and passed as
  `allowed_tools` in the same construction, and were never stated, so sessions queried the bare
  form, received "No matching deferred tools found", and re-queried with the prefix — two calls
  on every session type, not just builders. The one options constructor now states them.

- A sealed record now re-serializes to the exact bytes it was written in
  ([HIR-0208](docs/improvements/HIR-0208-an-additive-field-is-written-only-when-it-differs.md)).
  HIR-0207 made two additive fields optional on read, which fixed parsing and not the digest:
  `observation_digest` is computed over the record's own serialization, and emitting the new keys
  unconditionally meant every receipt sealed before them parsed and then failed verification.
  Additive fields are now written only when they differ from the default a reader derives, so a
  record that predates one is byte-identical across the change.

- A judgment debt now owes an observation only at the judge points it owns
  ([HIR-0206](docs/improvements/HIR-0206-a-debt-owes-an-observation-where-it-owns-the-point.md)).
  The payment compiler declines to produce an observation where the debt is not due, as HIR-0163
  requires, while the evaluation receipt demanded one at every qualitative row from the
  layer-level `debt_id`. A layer whose debt owned one of three judge frames died with
  "judgment_observation has unsupported shape", discarding the critic's only qualitative reading
  into a defect audit. The group plan now carries the debt's own points from one source of truth,
  a stray observation outside them is refused, and the ledger round records the frame it judged
  and whether it carried an observation.

- A widened durable record still reads the generation sealed before it
  ([HIR-0207](docs/improvements/HIR-0207-a-widened-record-still-reads-the-generation-before-it.md)).
  HIR-0204 added a required key to the layer replay claim, orphaning every receipt sealed before
  it: two shots refused to resume at the gate with two passed layers and eight sealed units
  between them. Additive record fields are now required on write and optional on read, with the
  historical default derived where absence has exactly one meaning, and pinned by tests that parse
  the previous key set.

- A required image contract can now be paid, so a look-owning unit can publish an outcome
  ([HIR-0205](docs/improvements/HIR-0205-a-bound-requirement-does-not-ask-for-autonomy.md)).
  One boolean answered two questions: `authoritative` says a row may veto with nobody having
  bound it, which a builder-authored image check deliberately may not, and three consumers
  read it to ask whether a bound required id had been produced and passed. Since only a builder
  payment can discharge a materialization-minted `image_contract` id, no unit with a required
  image claim could seal in any shot. hansa's `hero_facade` produced three passing image rows
  and was told the evaluator had produced none. Bound satisfaction and unbound veto are now two
  named predicates in one leaf module, shared by the unit-outcome receipt, critic
  reconciliation, and the sealed revalidation record; the refusal separates produced-but-failing
  from never-produced. Unit and layer capsules are unchanged, so sealed units stay resumable.

- Layer replay now demands each evidence row at the frame its contract declares
  ([HIR-0204](docs/improvements/HIR-0204-evidence-is-due-at-the-frame-its-contract-declares.md)).
  A claim's judge list was treated as its bindings' schedule, so eight frame-pinned contracts
  were each demanded at the two frames where they cannot exist; the evaluation reported
  `missing` evidence with nothing failed and no layer could finalize. Replay claim requirements
  now carry per-id declared frames read from the selected scene contracts, and an unframed row
  keeps the every-judged-frame fallback.

- The vocabulary-gap escalation path now terminates in a pass instead of padding
  ([HIR-0202](docs/improvements/HIR-0202-a-recorded-vocabulary-gap-closes-a-requirement.md)).
  A materializer that proved no registry metric can express a requirement was told to close it
  with a decision, which the domain validator then refused for structural domains, so two
  requirements on two layers were closed by contracts that cannot bear on them. A recorded gap
  now lets the decision pay any declared domain, a gap plus contract bindings is refused, and
  the false "every declared domain already has contract evidence" finding — emitted for
  bindings with no contract ids at all — states the true reason.

- A motion edit on a host that also carries an optics schedule is expressible again
  ([HIR-0203](docs/improvements/HIR-0203-interpolation-scopes-to-the-curves-a-unit-meant.md)).
  `bvfx_interp` walked the whole host closure with no way to restrict it, so re-interpolating
  a camera's rotation necessarily re-interpolated its passing protected lens schedule and the
  guard refused a contract-motivated edit that had no other legal form; the run then stalled
  and failed. Interpolation now takes `data_paths`/`exclude_paths`, and the guard names those
  forms instead of volumetric density advice aimed at a unit with no lights.

- A refused stage call now reports every unit-local gate at once and says that nothing was
  staged ([HIR-0201](docs/improvements/HIR-0201-pre-write-staging-gates-report-together.md)).
  Each gate returned on its first finding, so one camera unit took nine stage calls and eight
  refusals to place, while the collectable validator in the same session returned seven
  findings in one call; three of those turns were spent probing whether a refused stage had
  left anything behind.

- Every model stream now carries the event-idle deadline, and a run says how much of it is
  spent ([HIR-0200](docs/improvements/HIR-0200-every-model-stream-has-a-visible-deadline.md)).
  Only the builder's drain loop enforced the deadline, so a hung planner or materialization
  session would have waited forever, and three sessions watching three shots each invented a
  liveness heuristic from file mtimes, two of which gave wrong answers. Streams now iterate one
  helper that fails closed with the typed cause, and each phase writes a heartbeat carrying its
  deadline, last event and event count.

- A model session's turn budget is now measured by the harness that sets it
  ([HIR-0199](docs/improvements/HIR-0199-the-harness-counts-the-turns-it-budgets.md)). The
  budget was handed to the SDK as `max_turns` and reported at completion as the provider's
  `num_turns`, a different counter that read 14 against a cap of 12 while terminating
  successfully, so no operator could tell whether a budget bound anything. Completion now
  reports observed turns over the declared budget, prints the provider counter separately,
  and every SDK options construction goes through one constructor that declares the budget.

- The global verify pass gets the budget its rule states
  ([HIR-0198](docs/improvements/HIR-0198-the-verify-budget-has-one-ceiling.md)). The draft
  pass's `max_turns` was clamping the per-layer verify budget, so a six-layer shot ran verify
  on 12 turns instead of 18 and the scaling was inert for every shot with four or more
  layers. The budget now has one ceiling and its log line states its derivation. A
  missing-credential rejection also names the dotenv file the process resolved, instead of
  sending an operator to edit variables that were already correct in another checkout.

- `bbox_*` and `visible_fraction` measure the rendered subject
  ([HIR-0196](docs/improvements/HIR-0196-rendered-subject-metrics-respect-render-visibility.md)).
  Both counted objects hidden from render, so a builder that hid the four objects it had created
  read the identical `bbox_height` back and concluded its geometry was not responsible — the
  instrument could not answer the ablation. A subject hidden from render could also satisfy a
  required `visible_fraction`, which the per-role AND rule exists to prevent.

- A sibling `object_count` now counts as evidence of descendant population
  ([HIR-0195](docs/improvements/HIR-0195-a-sibling-count-is-evidence-of-population.md)). The
  cross-row contradiction check skipped every sibling whose kind was `object_count` — the rows
  that most directly state how many hosts a namespace must hold — and counted each descendant
  namespace as one host, so `eq 1` over `exterior.facade` survived beside `min 12` over
  `exterior.facade.window`. A builder spent a session measuring the contradiction and abstained,
  blocking three more units.

- The confined Blender worker renders on the host GPU again
  ([HIR-0194](docs/improvements/HIR-0194-confined-worker-renders-on-the-host-gpu.md)). The
  sandbox's minimal `/dev` hid every GPU device node, so Blender fell back to llvmpipe
  software OpenGL for every render (a 1080p volumetric EEVEE frame took 21 s instead of
  0.9 s). The confinement now dev-binds the present GPU nodes, the worker ping reports the
  GPU platform, and strict preflight fails closed when the host has a GPU but the worker
  reports software rendering.

- A gate report's signature is computed once, by code its validator shares
  ([HIR-0193](docs/improvements/HIR-0193-one-gate-report-signature.md)). The signature is a
  60-character slice of each finding's text; a finding whose text had a space at index 59
  produced a signature ending in whitespace, which the published-report validator rejects, so
  a run that had correctly diagnosed itself routed to the engineering sink instead of
  dispatching its own repair. Excerpts are stripped, and the producer and reader no longer
  keep separate copies of the rule.

- A capability is originated once per layer
  ([HIR-0192](docs/improvements/HIR-0192-a-capability-is-originated-once.md)). Layer 1 of a
  shot declared two units with `provides: ["camera"]` and no edge between them; the one that
  only set the focal length replayed before the one that creates the camera, and the accepted
  chain broke mid-layer with `AttributeError: 'NoneType' object has no attribute 'data'` after
  $5.91 of build. Every declarer of a capability must now reach the unit that originates it,
  checked at materialization and the plan gate.

- No blocking gate finding is left without a dispatcher
  ([HIR-0190](docs/improvements/HIR-0190-no-finding-is-left-without-a-dispatcher.md)).
  Scoping each gate to the layer that owns its findings kept the wrong session from being
  blocked, but a finding owned by an already-passed layer, or by a layer outside the run's
  range, was then filtered out of every gate and dispatched by nobody — so `vfx run --from 2`
  built layer 2 on a camera the gate rejects. A passed layer is skipped only while its own
  authority still clears the gate, and a run refuses to start when a blocking finding is owned
  by a layer it never visits, naming the layer to include.

- A layer's materialization is no longer gated on another layer's finding
  ([HIR-0189](docs/improvements/HIR-0189-materialization-gates-scope-by-owner.md)). A
  layer-2 rematerialization staged every unit and closed every requirement binding, then
  could not finalize because layer 1's camera owed a framing row it had no scope to author;
  it retried five times before the run was stopped. Both the finalize tool and the terminal
  publication gate now decide on the findings that layer owns plus every plan-wide one.

- `lifecycle: "window"` scene contracts are expressible again
  ([HIR-0188](docs/improvements/HIR-0188-lifecycle-keys-have-one-vocabulary.md)). The
  lifecycle domain requires `valid_through` for a window row while the scene-contract row
  vocabulary accepted only `expires_at`, a name nothing validated, so no contract could
  satisfy both and a rematerialization session burned four turns oscillating between the
  two refusals. Row-key vocabularies now derive the lifecycle key names from the domain
  that validates them, and the dead key is removed.

### Changed

- A per-layer plan-gate rejection is now typed transaction authority instead of an
  unclassified boundary defect
  ([HIR-0187](docs/improvements/HIR-0187-plan-gate-findings-carry-layer-authority.md)).
  `vfx run` gates the selected authority in process before building a layer and scopes the
  verdict by ownership: another layer's finding no longer blocks this one, every blocker the
  gated layer owns becomes one `publish_validated_amendment` on that layer view which the
  controller dispatches, and a plan-wide blocker stays a reviewed global amendment. Gate
  findings about a layer now carry it in their typed `layer` field — 33 of the 34 layer-owned
  constructions previously wrote the owner only into their message, so `clean_for` could not
  read it and each was silently promoted to a plan-wide block. An architecture test keeps the
  attribution from regressing. Where an amendment must land is now decided by one
  domain helper both the validator and the stop builder call, so a stop envelope that
  its own validator would reject cannot be constructed.

- The global plan's verify pass is budgeted from the layers the draft's ownership
  mapping declares (6 plus two turns per layer, capped at 24) instead of a fixed six
  turns that exhausted before the last layers of a six-layer draft (HIR-0177).
- A strict-read planning session's path denial enumerates only its declared read
  surface (`brief.md`, `refs/` stills, exact exceptions) instead of the shot's file
  tree, so a unit planner is no longer offered superseded unit scripts
  ([HIR-0156](docs/improvements/HIR-0156-plan-workspace-path-miss-enumerates-staged-reads.md)).
- Materialization turn budgets scale with the layer's owned requirements (24 plus two
  per requirement, capped at 96), and `patch_materialization` pointers may address a
  list row by its stable id (`id=<row id>`) instead of a guessed index. The staging
  schema offers only the claim authorities an authored claim can carry, and a mutated
  role without a required claim, a cross-row contract contradiction, or a camera unit
  whose judge frames lack rendered-subject framing is refused at the stage call rather
  than at finalize
  ([HIR-0177](docs/improvements/HIR-0177-materialization-budget-and-id-addressed-patches.md)).
- `check_scene(kind='bbox_feasibility')` searches every axis-aligned proxy box under the
  sealed camera for one that satisfies a unit's bound `bbox_*` rows and, after six
  mutations that leave the same row failing, `run_bpy` is refused until it runs; an
  infeasible verdict under harness-derived bounds fails closed into the typed abstention;
  bounds derive from the camera before any host exists, framing and bbox checks return the
  union for a shared role, and stage calls list a form layer's uncovered judge frames
  ([HIR-0183](docs/improvements/HIR-0183-coupled-bbox-bands-are-decided-by-a-feasibility-instrument.md)).
- `DIGEST_SCHEMA` moves to 5 for the HIR-0181 capsule change, golden tests pin the layer
  capsule digest to the schema, and `vfx migrate-digest-schema <shot>` migrates
  prior-generation work-unit state through the authority-state transaction instead of
  failing closed in the planner
  ([HIR-0182](docs/improvements/HIR-0182-digest-generations-migrate-through-the-transaction.md)).
- A `keyframe_schedule` or `object_property` row on a light or camera data-block property is
  refused at materialization and the plan gate unless a unit in the binding unit's dependency
  closure or an earlier layer writes that carrier family
  ([HIR-0185](docs/improvements/HIR-0185-data-block-rows-need-a-carrier-producer.md)).
- A camera-providing layer that owns projected composition must author a persistent framing
  row for every later subject at each shared judge frame, and its builder sees after every mutation whether a proxy box can
  pay those rows under the current path, and `bbox_feasibility` derives bounds and seeded
  starts from the camera frustums when no subject exists yet; an infeasible single-box
  verdict is advisory everywhere (it clears the mutation streak and names the legal paths)
  because a multi-part subject can satisfy bands one box cannot
  ([HIR-0184](docs/improvements/HIR-0184-camera-layers-prove-downstream-framing-before-sealing.md)).
- A decision a layer's materialization makes on its own deferred requirement stays in that
  layer's capsule, so a later layer's publication no longer supersedes an unchanged earlier
  layer's terminal receipt, and `vfx run` builds a reopened lower layer before the newly
  materialized one instead of stopping on the builder's unaccepted-prior refusal
  ([HIR-0181](docs/improvements/HIR-0181-a-layers-decision-stays-in-its-own-capsule.md)).
- Every `stage_materialization_unit` call runs the terminal collectable validator on the
  proposed candidate and refuses the findings it introduces inside its own write, listing
  the open layer-level findings with the staged result
  ([HIR-0180](docs/improvements/HIR-0180-stage-calls-refuse-findings-inside-their-own-write.md)).
- `Ledger.begin` moves the previous attempt's terminal projection (receipt digest and
  script hashes) into history, so re-finalizing a layer that had passed before a
  rematerialization no longer crashes on the finalization claim's scope check
  ([HIR-0179](docs/improvements/HIR-0179-new-attempts-start-from-a-bare-ledger-row.md)).
- An `object_count` upper bound over a literal namespace whose dotted descendants
  sibling rows require is refused at materialization and the plan gate with the
  matcher rule and the leaf-role or raised-bound fix, and the object_count read-back
  names every matched descendant beside the authored selector
  ([HIR-0178](docs/improvements/HIR-0178-namespace-counts-cannot-exclude-required-descendants.md)).
- Absolute pixel statistics (`render_region_stat`, `control_render_response`) bound by
  a required image claim count as image debts for the signal and subject bootstrap
  gates, so a camera-only unit cannot carry them;
  `bpy.data.worlds.new` classifies as volume work in the payload classifier; the
  projection and render instruments teach the camera-provider rule when no camera exists
  ([HIR-0176](docs/improvements/HIR-0176-camera-only-units-cannot-carry-image-rows.md)).
- The materialization kickoff compiles the closed judgment-property vocabulary and the
  layer's legal choice from its sparse row (a camera-providing layer owns only
  `camera_framing`), so the materializer no longer pays a rejection turn to learn it
  ([HIR-0175](docs/improvements/HIR-0175-typed-abstention-reaches-a-typed-stop.md)).
- The derived hypothesis-falsification projection moves from `state/work-units/` to
  `state/hypothesis-falsifications/`; the strict work-unit namespace enumerator refused
  it as an unknown member, failing every rematerialization that followed a real
  falsification ([HIR-0175](docs/improvements/HIR-0175-typed-abstention-reaches-a-typed-stop.md)).
- Interruption receipts bind authored inputs: the exact `brief.md`, every admissible
  `refs/` still, and the registration and crop of every selected `refobs-*` witness,
  captured into the run-owned archive and verified with the other source families
  ([HIR-0172](docs/improvements/HIR-0172-run-interruption-is-action-free-terminal-evidence.md)).
- Two `curve_derivative_max` rows on the same roles and property that contradict each
  other (a floor inside a lower cap's window) are refused at staging, materialization,
  and the plan gate; the falsification stop compiler compares the finding's layer
  capsule digest, not a whole-file hash, so `cannot_express_in_scope` publishes its typed
  plan defect; the planner kickoff and judge-frame rejection state the 1-based frame
  convention ([HIR-0175](docs/improvements/HIR-0175-typed-abstention-reaches-a-typed-stop.md)).
- `publish_unit_plan` stamps the bundle-pinned integrity sidecar with the bytes it
  publishes, so a unit-planning session's own `gate_preview` evaluates the draft
  instead of reporting it absent; the stale-claim remedy names `vfx units retry`
  and `vfx build --layer`, not the retired `--unit` flag
  ([HIR-0174](docs/improvements/HIR-0174-sessions-receive-their-own-artifacts.md)).
- `keyframe_schedule` and `object_property` rows on `data.*` paths judge every
  selected host that owns a data-block; a host with none (a camera rig's Empty pivot)
  is typed out and named, and a selection with no carrier fails closed
  ([HIR-0174](docs/improvements/HIR-0174-sessions-receive-their-own-artifacts.md)).
- The Blender session mints the one `checkpoints/journals/` destination for unit
  journals; the finalizer takes it from the session and a refused or failed journal
  capture fails the finalize closed instead of degrading the finalizer prompt
  ([HIR-0174](docs/improvements/HIR-0174-sessions-receive-their-own-artifacts.md)).
- Artifact execution-policy rejections name the line, expression, capability chain,
  and legal replay forms; finalize/repair script sessions journal their kickoff and
  continuation prompts in the build transcript
  ([HIR-0174](docs/improvements/HIR-0174-sessions-receive-their-own-artifacts.md)).
- The first `compare_frame` scale is derived from the shot's frame height so the
  default comparison reaches the measurement height instead of measuring an upscaled
  plate; `render_pass` counts as applicable only for units with typed look feedback
  ([HIR-0174](docs/improvements/HIR-0174-sessions-receive-their-own-artifacts.md)).
- The unit scope card and kickoff name the camera-owned deferred subject rows the
  dependency-complete geometry producer pays or protects as required evidence; only a
  partial producer's rows remain diagnostic forecasts
  ([HIR-0174](docs/improvements/HIR-0174-sessions-receive-their-own-artifacts.md)).
- `BlenderSession.restore` re-stages a parent-published checkpoint into the confined
  worker's scratch and verifies the bytes before the worker opens it; the first
  best-round restore under confinement had died with ENOENT
  ([HIR-0174](docs/improvements/HIR-0174-sessions-receive-their-own-artifacts.md)).
- `human_required` claim authority and `human_decision` evidence are retired from
  work-unit claims with a teaching rejection; the human domain is judgment debt on the
  owning requirement, so an executable-only unit no longer owes raster rounds for a
  claim nothing can pay
  ([HIR-0174](docs/improvements/HIR-0174-sessions-receive-their-own-artifacts.md)).
- The `vfx run` driver terminalizes an exception no stage classified as `failed`
  exactly once instead of leaving the run `running` under a dead owner
  ([HIR-0172](docs/improvements/HIR-0172-run-interruption-is-action-free-terminal-evidence.md)).
- The root owner's signal handler mints a typed `RecordedSignalIntent`; the
  terminalizer selects an owned interruption only from that record and accepts no
  kind string, and an architecture test pins the handler as its sole issuer
  ([HIR-0172](docs/improvements/HIR-0172-run-interruption-is-action-free-terminal-evidence.md)).
- A camera-providing sparse layer may reserve only its camera grant. Extra form
  selectors fail at ownership mapping and the plan gate, so global publication
  cannot combine camera provide with later subject-form namespaces
  ([HIR-0128](docs/improvements/HIR-0128-camera-capability-does-not-authorize-subject-form.md)).
- Internal layout: oversized modules are subpackages under the existing layer
  folders (`domain/work_units`, `agents/builder`, `blender/tools`, and so on).
  Public imports, CLI entry points, and `python -m` commands are unchanged.
- A denied plan-workspace Read enumerates the staged relative files and forbids
  prefixing another filesystem root; draft/verify/repair kickoffs compile that card
  instead of claiming there is no source video
  ([HIR-0156](docs/improvements/HIR-0156-plan-workspace-path-miss-enumerates-staged-reads.md)).
- A camera-providing unit that binds a camera-property row and same-host motion
  evidence publishes one `camera` write-cluster; the optics row no longer leaves
  temporal kinds as a sibling `control_host/keyframe` cluster
  ([HIR-0157](docs/improvements/HIR-0157-camera-optics-do-not-split-motion.md)).
- Camera-layer deferred `bbox_*` `activates_at` is compiled from the selected DAG's
  earliest geometry successor; `ask_supervisor` is not the occupancy instrument, and
  unanswered-question stops name `vfx escalate`
  ([HIR-0158](docs/improvements/HIR-0158-deferred-bbox-activation-is-dag-authority.md)).
- Materialization validation refuses required scene-contract role selectors the
  binding unit does not mutate or dress, so a mutation-empty observer cannot look
  locally clean and then die on terminal-gate `role-selector-closure`
  ([HIR-0159](docs/improvements/HIR-0159-materialization-refuses-out-of-scope-role-selectors.md)).
- Image-contract debt cannot publish until a mesh, volume, or compositor carrier
  is in the unit's replay prefix; shading and lights are optical signal, not a
  subject
  ([HIR-0160](docs/improvements/HIR-0160-image-debt-requires-rendered-carrier.md)).
- Same-layer `dresses` of a sibling mutation role fail at staging; this layer's
  `dressable` grant is for later layers only, and the rejection no longer tells
  the materializer to list those selectors on the owning layer's row
  ([HIR-0161](docs/improvements/HIR-0161-same-layer-dressing-is-not-owner-granted.md)).
- A work unit may declare a closed construction route (`procedural` default,
  `generate`, `retrieve`, `simplify`). `generate`/`retrieve` require a mesh write
  family; `generate` requires `refobs-*` witnesses and cannot bind a required
  `object_count` whose minimum exceeds 1. The Meshy adapter posts every supplied
  view (1–4) to `/multi-image-to-3d` and cannot drop extras onto a single
  `image_url`
  ([HIR-0162](docs/improvements/HIR-0162-construction-route-replaces-ungoverned-asset-import.md),
  [ADR-0009](docs/decisions/ADR-0009-construction-route-authority.md)).
  Generate-construction plates (`isolate_relight`, `orbit_view`, `isolate_cutout`)
  call Higgsfield with a parent crop; text-only generate is refused, and an all-white
  derived plate fails identity before Meshy. Materialization mints `refobs-*` crops
  into shot-root `state/refobs/` and refuses unregistered generate witnesses. On
  `vfx run`, a generate unit is prepared before the builder session: identity-gated
  plates → Meshy multi-image → hash-verified promotion to
  `build/construction/<sha256>.glb`. Generate units import with
  `bvfx_import_construction()`; `vfx asset` and generate-unit `import_asset` fail
  closed.
- Provisional image judgment is now typed debt with exact requirement, owner,
  fault unit, subject, moment, carrier, medium, lifecycle, and digest authority.
  The harness derives activation from the selected DAG's first relevant carrier,
  pins exact payer unit digests, and moves debt to `due` only after cumulative
  empty-scene replay issues a checkpoint- and artifact-verified payer receipt. It
  keeps executable checkpoints independent from `pending_not_due` debt and records
  durable `due → satisfied|falsified` transitions. A deterministic two-layer
  orchestration ratchet proves the camera artifact can pass, matching form pays once,
  a camera-only prefix is refused, restart does not repay, and acceptance cannot lose
  open debt. Unrelated carriers and misowned appearance fail closed, no-signal plates
  do not call a critic, and final acceptance refuses unresolved debt. Due observations
  now carry a sealed current-authority/replay/reference/environment/config request plus
  the actual render-settings/PNG capture receipt. No-signal attempts persist under the
  exact request digest: unchanged direct restart revalidates replay but performs no
  raster or critic call, while a changed typed environment permits one new attempt.
  Downstream JIT publication, planner kickoff, and materialization-stop classification
  also require the dependency's producer-valid current sealed outcome: global-DAG replay
  prefix, script/reference inputs, explicit raster-or-executable observation kind, and
  render receipt/bytes where applicable. Semantic layer ids use traversal-safe outcome
  and planning-scratch locators rather than decimal filename conventions
  ([HIR-0163](docs/improvements/HIR-0163-provisional-judgment-debt-activates-on-relevant-carriers.md)).
- Unaccepted stage boundaries now publish one immutable, digest-selected
  `vfx-harness.stop-envelope/v1` whose closed class, exact authority/evidence identity,
  and single typed transaction replace exit codes and prose as machine dispatch
  authority. `EvidenceNotDue` is successful continuation. Transaction targets derive
  their exact state assertions, dispatch mode, receipt schema, and matching domain
  postcondition; receipt-bound evaluation and idempotency contracts make unchanged
  repeated causes detectable without claiming progress. The common run boundary reads
  the envelope back and `status.json` selects it by digest. This release provides
  classification and transaction scaffolding only: there is no recovery controller,
  transaction-receipt producer/consumer, commit reconciliation, automatic dispatch,
  safe resume producer, or proven local-implementation retry producer
  ([HIR-0164](docs/improvements/HIR-0164-closed-stop-envelopes-precede-recovery-dispatch.md)).
- The construction read-namespace guard creates the shot's `build/` root durably on a freshly
  started shot instead of crashing the first builder stage, and still refuses a symlinked or
  non-directory `build` (found by the first real run on a clean shot).
- `vfx reconcile <shot> --run-id <run>` proves owner loss for one exact run by acquiring its
  recorded fence and publishes an action-free `owner_lost` interruption, completes a prepared
  receipt left by an owner that died before status selection, and leaves a live owner
  untouched. Strict preflight now proves the kernel-owned plan-consumer directory primitive
  (`plan_consumer_directory`, probe revision 5), and its confinement smoke root is a valid v2 shot
  id ([HIR-0172](docs/improvements/HIR-0172-run-interruption-is-action-free-terminal-evidence.md)).
- Every public run is now the `vfx-harness.run/v2` generation and the v1 writers are gone: the
  manifest carries a typed dispatch discriminant, the root owner acquires its claim and fence
  before publishing a `vfx-harness.run-status/v2` running status, SIGINT and SIGTERM record
  intent and terminalize an `interrupted` run with exit 130 or 143, passed runs select their
  summary and failed runs their typed stop envelope by digest, and terminal diagnostics such
  as `terminal_cause` move from `status.json` to `reports/summary.json`. Stages inside a driver
  publish only their typed stop, which the driver consumes before selecting the terminal status.
  Prior-generation runs fail closed in every reader
  ([HIR-0172](docs/improvements/HIR-0172-run-interruption-is-action-free-terminal-evidence.md)).
- Interrupted runs now commit exactly once through the terminalizer: the root owner, holding
  its live fence, captures the authority observation, publishes the receipt, lets the
  independent evaluator reopen the archive, and then publishes the evaluation, an interruption
  summary with zero legal transactions, the inventory, the v2 `interrupted` status selecting
  receipt and evaluation by digest, and the latest projection. An unsatisfied evaluation leaves
  the run running with interruption authority unavailable, and the authoritative reader
  re-evaluates the archive before returning a status
  ([HIR-0172](docs/improvements/HIR-0172-run-interruption-is-action-free-terminal-evidence.md)).
- Interruption evidence now has its run-owned immutable archive and independent evaluator in
  bounded form: a fence-held capturer classifies every closed authority source through one
  domain function, copies exact bytes into `archive/interruption/objects/<sha256>` under the
  target run, and publishes a closed archive manifest the receipt binds; the evaluator derives
  `satisfied | failed` by reopening only the run's records and archive with a closed issue
  vocabulary, so a later valid authority change cannot falsify a committed interruption.
  Terminal `interrupted` publication remains refused until the terminalizer lands
  ([HIR-0172](docs/improvements/HIR-0172-run-interruption-is-action-free-terminal-evidence.md)).
- `shot.json` now carries a strict `vfx-harness.shot-ledger/v2` accepted-build index under
  `accepted_build`, derived only by the shot-ledger derivation writer from the selected DAG's
  stable order, passed terminal receipts, sealed outcomes, composed script bytes, and the
  coordinator head, with the acceptance-chain digest shared with acceptance and acceptance
  bound through durable per-moment evidence records. Layer finalization and acceptance derive
  it inside their ledger publications, a crash-resume reconcile reproduces it byte for byte
  because chain rows bind every durable terminal receipt rather than the ledger slot being
  rewritten, plan and JIT republication republish it inside the authority-state transaction
  once the successor head is current, `vfx recover-authority-state` republishes a member left
  stale by a death in that window and reports `accepted_build_projection` in its
  `vfx-harness.authority-state-recovery-result/v2` record, the transport refuses any other
  change to the member, and readers re-derive it instead of trusting the stored value
  ([HIR-0172](docs/improvements/HIR-0172-run-interruption-is-action-free-terminal-evidence.md)).
- Retired by decision three tests that could not pass on a clean checkout: the print-based
  `tests.integration.test_harness` script and the hierarchy-gate copy test depended on the
  untracked local shots `barrel_roll` and `beacon_wake`, and the single-authority test
  forbade the tracked `.cursor/rules/` mirrors that AGENTS.md declares. Behavioural coverage
  lives in the discoverable pytest suites, and AGENTS.md remains the sole rule authority with
  its Cursor mirrors kept in the same change.
- Blender is now selected by a `--version` probe executed inside the mandatory worker
  confinement, so a launcher that only works on the host (a snap shim that needs snapd) is
  rejected at resolution and strict preflight with the sandbox's own diagnostic and the
  `BLENDER_BIN` next action, instead of timing out at worker boot; the packaged real binary
  is selected automatically and the preflight probe specification advances to revision 4
  ([HIR-0173](docs/improvements/HIR-0173-blender-resolution-proves-the-launcher-inside-confinement.md)).
- Canonical `shot.json` commits now use one opaque prepared transaction under the shared
  shot-authority writer fence and ordered real ledger lock, with exact CAS/readback and
  interruption- and fork-safe descriptor ownership. Generic file writers cannot target either
  `shot.json` or its `shot.json.lock`, and plan-consumer/candidate copies require an exact
  physically isolated view whose directories are created through a kernel-proven fanotify
  target-FID and `openat2` primitive (Linux 5.17+ on a local filesystem; unsupported hosts fail
  closed). The fork-visible descriptor registry now retains unreadable slots instead of assuming
  them closed, neutralizes an unreadable adoption, allocates the owner claim inside the armed
  acquisition, and reports typed retained state on lease release. This is a legacy transport
  boundary only; strict `shot-ledger/v2` accepted-member derivation and
  interruption publication remain disabled
  ([HIR-0172](docs/improvements/HIR-0172-run-interruption-is-action-free-terminal-evidence.md)).
- A normal full render now requires a complete passing
  `vfx-harness.acceptance-outcome/v1` for the exact current bundle, materialized view,
  accepted layer/unit script chain, selected moments, and unchanged render/reference
  evidence. Missing, failed, partial, or stale outcomes refuse deliverable publication.
  Forced and `--upto` renders remain previews and default to the current run's scratch
  tree rather than the deliverables directory
  ([HIR-0165](docs/improvements/HIR-0165-deliverables-require-current-acceptance.md)).
- Durable layer memory now lives under shot-root `state/`; reading it cannot create an
  orphan direct run or displace the latest production-run pointer
  ([HIR-0155](docs/improvements/HIR-0155-layer-memory-does-not-create-runs.md)).
- Rematerialization writes a reverted overlay as the design base and selects only
  when the replacement publishes: a crash, truncation, or broken pipe leaves the
  previously selected view. Materialization sessions also deny Task/Agent
  ([HIR-0026](docs/improvements/HIR-0026-remat-revert-must-not-select-a-hole.md)).
  An exhausted session does not publish because a candidate file exists
  ([HIR-0027](docs/improvements/HIR-0027-max-turns-must-not-publish.md)).
  Structured `values.contract` adoption is last-write-wins for the selected bundle:
  a prior generation's row is inert, a later superseded or falsified row retires
  the id, and materialization copies the compiled binding set
  ([HIR-0028](docs/improvements/HIR-0028-structured-decisions-bind-the-selected-bundle.md)).
  Extra-frame scene contracts bind through `composition_context.contract_ids`; the
  kickoff compiles the layer judge list and named sealed outcomes, and judge-set
  rejections name that binding
  ([HIR-0029](docs/improvements/HIR-0029-extra-frame-contracts-bind-through-ids.md)).
  Published `keyframe_schedule` samples that already exceed a same-role
  `curve_derivative_max.hi` are refused at authoring, materialization, and the
  plan gate
  ([HIR-0030](docs/improvements/HIR-0030-schedule-smoothness-linear-floor.md)).
  Repair records `cannot_express_in_scope` and skips the remaining budget instead
  of burning the next attempt
  ([HIR-0031](docs/improvements/HIR-0031-cannot-express-stops-repair-budget.md)).
  A declaring unit's empty `look_capabilities` is executable-only, and a
  no-signal plate is not a look score
  ([HIR-0032](docs/improvements/HIR-0032-no-look-on-no-signal-plates.md)).
  `find_recipe` abstains when the hit requires mutation roles the unit does not
  own
  ([HIR-0033](docs/improvements/HIR-0033-find-recipe-abstains-off-scope.md)).
  `run_bpy` errors that reinvent `path_clearance_min` name the bound instrument
  ([HIR-0034](docs/improvements/HIR-0034-run-bpy-names-bound-instruments.md)).
  A `curve_derivative_max` miss names the argmax adjacent-frame pair
  ([HIR-0035](docs/improvements/HIR-0035-curve-derivative-names-argmax.md)).
  Look-less live `render_frame` / `verify_change` default to Workbench solid
  ([HIR-0036](docs/improvements/HIR-0036-lookless-preview-defaults-to-workbench.md)).
  Look-less `compare_frame` now uses the same typed Workbench-solid default for live
  form/layout reference diagnostics, while EEVEE beauty debt remains signal-gated and
  the rejection names the executable-only alternative
  ([HIR-0131](docs/improvements/HIR-0131-lookless-reference-comparison-is-workbench.md)).
  Integer `SystemExit` `status.json` detail is the meaning, not the digit
  ([HIR-0037](docs/improvements/HIR-0037-status-detail-names-the-stop.md)).
  Materialization sessions bind a durable transcript
  ([HIR-0038](docs/improvements/HIR-0038-materialization-binds-transcript.md)).
  Composed canonical of a look-less layer fans in unit executable claims
  instead of a critic look vote; `vfx build` exits 9 when units passed but
  the composed verdict did not
  ([HIR-0039](docs/improvements/HIR-0039-lookless-composition-is-not-a-critic.md)).
  Executable-only units now evaluate live and canonical scene/interface evidence
  without first rasterizing; functional image metrics still derive raster need from
  the canonical registry
  ([HIR-0114](docs/improvements/HIR-0114-executable-only-evidence-precedes-raster.md)).
  Candidate finalization and repair probes now expose only the active unit's exact
  frame-bound evidence plus typed visibility protections, so sibling failures cannot
  authorize an out-of-scope script edit
  ([HIR-0115](docs/improvements/HIR-0115-candidate-readback-is-unit-scoped.md)).
  Scene inspection now refreshes Blender's current frame and dependency graph on every
  call and reads evaluated object, camera, and light hosts, so omitting `frame=` cannot
  return a stale world transform
  ([HIR-0116](docs/improvements/HIR-0116-scene-inspection-is-fresh-by-default.md)).
  Artifact replay now publishes evaluated current-frame state between producers and
  consumers across live chaining, candidate probes, revalidation, warm starts,
  canonical checks, ablation, and composed unit fan-in
  ([HIR-0117](docs/improvements/HIR-0117-artifact-replay-publishes-evaluated-state.md)).
  Automatic scene-contract read-back no longer freezes an executable-only builder on an
  intermediate structural pass; terminal handoff is that candidate's freeze boundary,
  while image-bound units retain the immutable compare-before-more-mutation lock
  ([HIR-0118](docs/improvements/HIR-0118-automatic-readback-is-not-candidate-freeze.md)).
  Accepted unit priors and composed layer artifacts now derive one stable topological
  order from `depends_on`, so retries cannot replay a consumer before its producer merely
  because `stages[]` stores them in another order
  ([HIR-0119](docs/improvements/HIR-0119-replay-order-is-derived-from-the-unit-dag.md)).
  A deferred_owner requirement declares evidence domains from the same closed
  vocabulary as layer `evidence_domains` and `claim.asserts`; the owner layer
  must cover every declared domain, and a rejection names covering layers
  ([HIR-0124](docs/improvements/HIR-0124-deferred-owner-domains-must-cover-owner-layer.md)).
  Each work unit owns exactly one identity-derived replay file
  (`build/units/<layer>/<unit-id>.py`); composed layer paths and `#fragment`
  notation fail closed at staging and `load_layers`
  ([HIR-0126](docs/improvements/HIR-0126-unit-replay-is-an-identity-derived-file.md)).
  Subject composition is `bbox_*` of a rendered subject, not `projected_origin` of
  a camera-only host; vacuous origin/bbox bands fail closed; camera-owned bbox
  due at a later geometry layer stays inactive on the camera unit; geometry
  freeze-protects matching deferred rows; and `cannot_express` may name an
  earlier-layer camera without treating it as a same-layer affected seed
  ([HIR-0127](docs/improvements/HIR-0127-subject-composition-is-due-when-geometry-exists.md)).
  Sparse camera authority now compiles a camera-only unit capability vocabulary;
  staging and finalization refuse proxy `geometry` before it can replace deferred
  rendered-subject bbox evidence
  ([HIR-0128](docs/improvements/HIR-0128-camera-capability-does-not-authorize-subject-form.md)).
  Future-active scene contracts are context bindings, not evidence that can seal their
  authoring unit; staging, finalization, and the terminal gate now agree on that due
  boundary
  ([HIR-0129](docs/improvements/HIR-0129-deferred-contracts-are-context-not-unit-evidence.md)).
  Deferred contracts now retain their owner layer's judge-frame authority while later
  activation layers evaluate those moments as extra-frame evidence
  ([HIR-0130](docs/improvements/HIR-0130-deferred-contracts-keep-owner-frame-authority.md)).
  Required visibility now activates at its typed repair-owner unit: downstream geometry
  must depend on and protect it, while earlier units no longer owe surfaces that do not
  exist yet; ambiguous rows retain conservative layer-wide protection
  ([HIR-0132](docs/improvements/HIR-0132-visibility-activates-at-its-unit-owner.md)).
  Plain JIT layer materialization now reconciles prior-generation durable unit state
  through the same digest-backed `apply_replan` transaction as rematerialization, including
  deterministic recovery when publication completed before the state move
  ([HIR-0133](docs/improvements/HIR-0133-direct-materialization-reconciles-durable-state.md)).
  Camera-owned deferred subject bbox no longer fails layer-start replay before geometry
  exists; the dependency-complete overlapping geometry unit pays it, and publication
  rejects a DAG with no such payer
  ([HIR-0134](docs/improvements/HIR-0134-deferred-subject-bbox-starts-at-complete-geometry.md)).
  A sibling rematerialization that only changes the combined `layers.json`
  hash adopts that identity and preserves the unchanged layer's unit
  statuses; it is not a DAG replan
  ([HIR-0040](docs/improvements/HIR-0040-sibling-view-hash-is-not-a-dag-change.md)).
  A shared `bvfx_role` on several hosts names `object=` as the next action
  for a single-subject check, and `list_keyframes` enumerates every host
  ([HIR-0041](docs/improvements/HIR-0041-shared-role-is-not-an-inexact-selector.md)).
  `probe_candidate` on a look-owning unit returns a draft beauty plate;
  Workbench solid is not the critic's domain
  ([HIR-0042](docs/improvements/HIR-0042-look-probe-is-not-workbench-solid.md)).
  Canonical repair binds `cannot_express_in_scope` on the candidate server
  ([HIR-0043](docs/improvements/HIR-0043-repair-must-bind-cannot-express.md)).
  A look-owning unit with no image bindings does not lock `run_bpy` on a
  0/0 critic handoff
  ([HIR-0044](docs/improvements/HIR-0044-look-without-image-contracts-is-not-a-handoff.md)).
  A unit judge frame with no required claim is a contract_gap, not a critic look
  vote
  ([HIR-0045](docs/improvements/HIR-0045-uncovered-judge-frame-is-not-a-critic.md)).
  A look-owning unit cannot seal 5.0 on scene counts; every judge frame needs a
  required image-domain claim
  ([HIR-0046](docs/improvements/HIR-0046-look-ownership-is-not-a-scene-seal.md)).
  Claim-closure counts those bound `image_contract` ids as producers while
  `checks.json` is still empty
  ([HIR-0047](docs/improvements/HIR-0047-image-contract-debt-is-not-missing.md)).
  Owed look `image_contract` ids compile to a payment card; `propose_checks`
  must match id, frame, property kind, and axis; candidate freeze refuses
  while any remain unpaid without a typed `unpaid_image_debt` abstention;
  the `render_region_stat` family is the `METRICS` `region_*` prefix, and
  falsification-id readers strip `check:`
  ([HIR-0048](docs/improvements/HIR-0048-unpaid-image-debt-is-not-a-selector-miss.md)).
  A JIT-layer `--falsification` compares `plan_hash` to the materialized view
  / durable state, not the sparse bundle file, and reopens the finding's
  unit plus affected closure without empty-base-replanning passed siblings
  ([HIR-0049](docs/improvements/HIR-0049-jit-falsification-identity-is-the-view-hash.md)).
  A `keyframe_schedule` sample path matches object `P`, object `data.P`, and
  the data-block `P` fcurve; a path miss names both sides and fails closed
  instead of reading as INAPPLICABLE
  ([HIR-0050](docs/improvements/HIR-0050-keyframe-schedule-path-miss-is-not-inapplicable.md)).
  A required `visible_fraction` claim is repaired by a camera unit or the
  mutator of those roles; multi-role vis is AND across named roles; geometry
  units freeze-protect active-layer vis including sibling rows
  ([HIR-0051](docs/improvements/HIR-0051-occlusion-needs-a-ray-changing-owner.md)).
  Rematerialization of a layer that already has accepted units is
  `apply_replan`: matching digests stay; `--discard-accepted` is not the
  door on remat
  ([HIR-0052](docs/improvements/HIR-0052-remat-with-accepted-units-is-apply-replan.md)).
  A work unit must publish one derived write-cluster; authored family fields cannot
  satisfy the gate. Consumed interfaces are read-only and cannot hide mixed clusters;
  unresolved write families fail closed
  ([HIR-0083](docs/improvements/HIR-0083-work-unit-atomicity-is-a-derived-publication-predicate.md)).
  Successor cards carry typed publish interfaces whose values are roles, controls,
  or sealed contract ids. Authored `publishes`/`consumes` participate in producer
  identity; readiness requires a declared interface id/kind match against a
  digest-matched producer
  ([HIR-0084](docs/improvements/HIR-0084-successor-interfaces-are-typed-references-bound-to-producer-digests.md)).
- Camera-dependent evidence now requires a typed camera provider in the unit/layer
  dependency closure; global camera capability is bound to reserved role selectors,
  and materialized views retain that sparse global authority
  ([HIR-0085](docs/improvements/HIR-0085-camera-dependent-evidence-needs-an-available-camera.md),
  [HIR-0086](docs/improvements/HIR-0086-global-camera-capability-must-be-role-bound.md),
  [HIR-0087](docs/improvements/HIR-0087-materialized-views-must-retain-global-capability-authority.md)).
- Materialization repair accepts atomic pointer batches, scheduling refreshes durable
  state before each ready-set decision, unit-plan publication writes to one fixed sink,
  and layer materialization stages one bounded unit at a time with atomicity due before
  the staged bytes change
  ([HIR-0088](docs/improvements/HIR-0088-materialization-repairs-need-atomic-batches.md),
  [HIR-0089](docs/improvements/HIR-0089-ready-set-must-read-current-durable-state.md),
  [HIR-0091](docs/improvements/HIR-0091-unit-plan-output-is-a-fixed-sink.md),
  [HIR-0092](docs/improvements/HIR-0092-materialization-stages-one-bounded-unit-at-a-time.md),
  [HIR-0093](docs/improvements/HIR-0093-atomicity-is-due-at-unit-staging.md)).
- Control hosts use camera-owned point projection rather than invented proxy mesh.
  Object transforms remain control-state writes rather than inferred animation family,
  and same-layer projection observes a digest-bound consumed interface without granting
  mutation authority
  ([HIR-0090](docs/improvements/HIR-0090-control-points-need-point-projection-not-proxy-mesh.md),
  [HIR-0094](docs/improvements/HIR-0094-point-projection-is-camera-owned-alignment.md),
  [HIR-0095](docs/improvements/HIR-0095-state-observation-does-not-invent-write-families.md),
  [HIR-0096](docs/improvements/HIR-0096-point-projection-observes-a-consumed-interface.md)).
- The staging tool exposes the closed WorkUnit schema, authored `family` padding is
  rejected before generation or staging, camera availability comes only from typed
  capability authority, and projection consumption must match the exact interface that
  exports the selector
  ([HIR-0097](docs/improvements/HIR-0097-unit-ticket-schema-is-an-agent-instrument.md),
  [HIR-0098](docs/improvements/HIR-0098-camera-availability-is-only-a-typed-capability.md),
  [HIR-0099](docs/improvements/HIR-0099-consumption-matches-the-exporting-interface.md)).
- Materialization stage and patch writes share one locked, revision-checked transaction.
  Patch cannot insert or replace stage rows, and unit-affecting field repairs pass the
  local staging gates before candidate bytes change
  ([HIR-0100](docs/improvements/HIR-0100-materialization-candidate-writes-are-serialized-transactions.md)).
- Rematerialization after global republication derives its unpublished design base
  from the newly selected bundle when the live JIT view belongs to the prior
  generation; the superseded view remains selected only until the validated
  replacement publishes
  ([HIR-0101](docs/improvements/HIR-0101-remat-rebases-on-selected-global-generation.md)).
- Rematerialization moves accepted predecessor state through a digest-bound replan
  even when global republication has made the old JIT view inert: matching
  checkpoints stay, changed/new closure reopens, and removed accepted units are
  superseded by the amendment rather than misclassified as orphans
  ([HIR-0102](docs/improvements/HIR-0102-remat-replan-base-is-durable-unit-state.md)).
- Unresolved derived write families now return a registry-backed witness card naming
  the unresolved mutated roles, legal contract kinds and mutation selector fields;
  read-only comparison selectors no longer have to be rediscovered by staging retries
  ([HIR-0103](docs/improvements/HIR-0103-unresolved-write-family-names-legal-witnesses.md)).
- Unpublished materialization scratch now has a typed, revision-checked unit-retirement
  operation. It refuses surviving dependency or consume edges and prunes only contracts
  and requirement bindings no surviving staged unit uses, without reopening whole-stage
  patch authority
  ([HIR-0104](docs/improvements/HIR-0104-materialization-decomposition-has-typed-unstage.md)).
- Invalid JSON-pointer list locations now report the live list length, valid range,
  indexed stable row ids, and the exact append action. Negative indices are rejected
  instead of mutating from the end
  ([HIR-0105](docs/improvements/HIR-0105-json-pointer-rejection-enumerates-live-list.md)).
- Mutual geometry/visibility producer gaps now compile into one strongly connected
  cycle finding with the exact units, contracts, roles, and directed edges. Internal
  edge findings are suppressed while unrelated acyclic HIR-0057 gaps remain intact
  ([HIR-0106](docs/improvements/HIR-0106-mutual-geometry-vis-is-one-cycle-finding.md)).
- Durable builder worklists are now bound to the exact layer, unit id, and unit digest.
  Same-generation retries retain unresolved work, while superseded layer-only and sibling
  lists are inert; worklist JSON is identity-checked and published atomically
  ([HIR-0107](docs/improvements/HIR-0107-builder-worklist-is-unit-digest-bound.md)).
- Successful materialization finalization now writes a bundle- and candidate-revision
  attestation. A model session exhausting on that exact final tool call may publish the
  attested revision; generic candidate existence, patch validation, and every other
  max-turn path remain failed transactions
  ([HIR-0108](docs/improvements/HIR-0108-finalization-attests-the-last-model-turn.md)).
- The active work unit is compiled into one scope card — mutation surface, bound
  contracts, claims, judge frames, and `run_bpy` helper signatures — shared by
  kickoff, `CLAUDE.md`, and the `unit_scope` tool
  ([HIR-0025](docs/improvements/HIR-0025-unit-scope-was-a-translation-job.md)).
- An empty `path_clearance_min` obstacle selection is not a passing clearance: the 1e9
  sentinel never PASSes, and authoring refuses sentinel-scale or zero-floor bounds
  ([HIR-0024](docs/improvements/HIR-0024-empty-path-clearance-is-not-a-pass.md)).
- An audited `vfx units retry` of a unit whose executable rows already pass may mutate
  until the first in-session verdict: the convergence guard no longer treats a failed
  qualitative claim as sealed work
  ([HIR-0021](docs/improvements/HIR-0021-a-reopened-unit-may-do-the-work-its-retry-prescribes.md)).
- Materialization findings are pointer-addressed and returned together: the write-hook and
  `patch_materialization` operate on RFC 6901 locations in the candidate file, not a
  one-error-per-rewrite walk and not Glob of prior bundles
  ([HIR-0023](docs/improvements/HIR-0023-materialization-findings-are-pointer-addressed.md)).
- Made a semantic role one dotted token: `_bvfx_role` rejects commas and other
  non-token characters (CSV is not membership), contract selectors refuse the same
  punctuation at authoring, and `inspect_scene` / `check_scene` / `list_keyframes`
  address `role=` through the one matcher, naming present names and roles on a miss
  ([HIR-0022](docs/improvements/HIR-0022-a-comma-joined-role-was-stored-as-one-token.md),
  [HIR-0018](docs/improvements/HIR-0018-selector-diagnostics-say-both-sides.md)).
- Added `visible_fraction`, occlusion-true visibility evidence (camera-ray fraction of a
  subject's on-screen surface samples), and made every judge frame require it at
  materialization: a whole lookdev layer had been judged at frames where every subject sat
  behind a solid blockout disc, invisible to projection-only bbox rows
  ([HIR-0019](docs/improvements/HIR-0019-judge-frames-must-prove-visibility.md)).
- Made every selector and socket miss report both sides: node/control misses enumerate the
  semantic tags actually present in the searched graphs, socket misses enumerate the node's
  real sockets plus the literal-`'Value'` resolution rule, measured zeros carry the same
  enumeration as notes through evidence, probes, and verdicts. `probe_control` now renders
  the one canonical control resolver instead of a private near-copy, selectors match control
  and role tags either-of (a control tag no longer shadows a node's role), and an auto-socket
  response row sharing its selector with a socket-pinned sibling is refused at authoring and
  advisory at the gate
  ([HIR-0018](docs/improvements/HIR-0018-selector-diagnostics-say-both-sides.md)).

- Taught the plan gate the verified materialization lifecycle, added three evidence kinds
  (motion smoothness, persistent path clearance, parallax profile) with fail-closed vacuity
  linting, and gave planning/repair sessions decision-grade instruments — `evidence_vocabulary`,
  `gate_preview`, `probe_candidate` (with the `rig_contract` check), typed vocabulary-gap
  escalation, reproduction-carrying failures, and repair-session recipes. One hermetic fixture
  now drives a full authority generation end-to-end in the suite
  ([HIR-0017](docs/improvements/HIR-0017-lifecycle-aware-authority-and-agent-instruments.md)).
- Made `vfx units replan` able to express generation supersession under unit-first authority:
  when both bundles carry empty layer DAGs, the old identity is durable state's own recorded
  plan hash, state units absent from the new generation are superseded with audit ("orphaned"
  in the replan record), and retiring an accepted orphan requires `--discard-accepted` or a
  typed falsification record.
- Taught bundle resolution the `plans/ownership_mapping.json` member the publisher already seals,
  and made publication refuse any member resolution cannot read — the first mapping-carrying
  bundle published as clean and then failed closed for every consumer (the
  [HIR-0016](docs/improvements/HIR-0016-gate-attested-unit-plan-publication.md) writer/reader
  class at the bundle boundary).
- Made JIT unit-plan publication a two-phase transaction: the deterministic gate runs inside
  generation, a clean result earns a gate attestation in the authority sidecar (schema v2), a
  dirty result rolls the shot back, and every build-time consumer refuses unattested plans
  ([HIR-0016](docs/improvements/HIR-0016-gate-attested-unit-plan-publication.md)).
- Made every scene-contract probe row measure its declared frame with its own depsgraph, and
  replaced raw vertex projection with one frustum-clipped, fail-closed implementation shared by
  authoritative `bbox_*` evidence and the advisory framing checks
  ([HIR-0015](docs/improvements/HIR-0015-declared-frame-and-frustum-truth.md)).
- Made repository-root `.env` loading resolve the checkout root instead of `src/`.
- Made `vfx plan --until-clean` exit 3 and publish a failed run when blocking findings remain.
- Made the plan gate report every unknown work-unit dependency in one repair brief instead of
  revealing one invalid layer per paid repair round.
- Persisted final plan-gate authority in `reports/plan_gate.json` and terminal run metadata.
- Added warm-session validation for planner machine artifacts and a bounded read-only repair gate.
- Unified reference fingerprints under the typed `vfx-harness.look-vector/v1` metric registry.
- Added temporal scene contracts, rendered frame-delta evidence, and temporal claim coverage.
- Added projected-composition and mutation/fault-ownership coverage warnings.
- Added run-owned content-addressed plan bundles, atomic `plans/current.json` publication, and
  run-isolated repair snapshots as the first ADR-0004 migration slice.
- Declared global planner role capabilities so draft, verify, and repair can all patch artifacts
  and call the bounded deterministic gate while their context is warm.
- Moved global-plan authoring into an authored-input-only workspace owned by each run, preventing
  prior shot-root plans, contracts, questions, and run files from leaking into a fresh pass.
- Isolated generated output under `runs/<run-id>/` with stable log, report, evidence,
  checkpoint, scratch, and deliverable categories.
- Added manifest, status, summary, artifact-index, and latest-run metadata for machine readers.
- Added explicit run selection and run listing to `vfx inspect` while retaining legacy readers.
- Added model-free, gate-checked promotion of retained clean plan candidates into fresh
  run-owned immutable bundles.
- Added `vfx units replan` for fail-closed migration of durable work-unit state between an
  explicitly named old bundle and current selected plan authority.
- Added audited failed-unit retry transitions and made direct build runs fail when any requested
  work unit remains unaccepted.
- Redefined plan cleanliness as structural authority, added explicit decision strengths and typed
  `hypothesis_falsified` work-unit outcomes, and bound transactional replanning to those immutable
  executable findings while preserving hard-constraint approval.
- Routed terminal unit failures whose failing bound contracts are a decision's declared
  falsification path into the same typed `hypothesis_falsified` outcome, so an unreachable
  approved or planner start stops as replanning evidence instead of a generic unit failure.
- Scoped global-plan recipe selection, spikes, and numeric check calibration to Layer 1 and
  cross-layer DAG facts; later-layer execution detail now waits for its JIT pass and upstream
  checkpoints instead of being simulated before the first build.
- Replaced deferred-layer contract promises with ownership-only requirement registration.
  Global publication now rejects later-layer evidence design and fingerprints; JIT
  materialization closes each owned requirement with required producing evidence or a typed
  decision and extends the cumulative acceptance view.
- Bounded global-plan session economics: spikes now carry a session ceiling and one failed
  retry per hypothesis (with a contract-kind reference on invalid rows), reference
  fingerprints are computed once per run and reused across draft and verify, and
  repeated gate signatures stop the in-session edit loop, `VFXH_PLAN_MAX_TURNS` defaults to 24,
  and verification is separately bounded to 12 turns.
- Capped image-check calibration at an initial batch plus one repair batch per plan session;
  when the ceiling closes, both calibration tools refuse with instructions to drop unresolved
  optional image checks and proceed on executable scene contracts and build-time falsification.
- Made reference ingestion follow execution scope: global kickoff no longer embeds every future
  approval image, `measure_ref` refuses references outside ready units, and JIT materialization
  receives only its layer's judge references.
- Enforced spike eligibility at the tool boundary instead of prompt prose: adopted decision
  values, decision falsification paths, self-fulfilling existence/count/rendered-response
  contracts, and proxy lighting/visibility reads are refused deterministically, and budget
  identity keys the semantic hypothesis so renaming a contract cannot buy another attempt.
- Made the subscription token (`CLAUDE_CODE_OAUTH_TOKEN`) the default Claude credential when
  both are configured, withholding `ANTHROPIC_API_KEY` from the SDK unless
  `VFXH_CREDENTIAL=api_key` selects it; preflight reports the applied selection.
- Limited plan spikes to optional citation-integrity evidence: claimed spikes now freeze exact
  script, output, Blender identity, and contract rows, while unspiked composition work may proceed
  to its producing runtime unit.
- Moved the first layer across the JIT boundary: schema-5 global plans publish only the layer DAG,
  ownership, durable constraints, and blockers; dependency roots materialize without fictional
  upstream outcomes, and image-check calibration is unavailable until a real candidate exists.
- Added typed terminal causes for operator interruption, model-turn exhaustion, plan-gate stalls,
  plan-budget exhaustion, usage limits, and process errors in run status and summaries.
- Made build-time readings unable to overstate themselves: contract rows reject keys the
  harness ignores and must declare the frame they read; `onset_order` rejects selectors
  that compare a set against itself; every metric declares the evidence domain it can
  certify and every required claim declares the domain it asserts, so a count cannot
  close a timing claim; units declare look capabilities instead of having them guessed
  from axis names, and declared appearance ownership requires candidate-bound image
  evidence; required evidence that was never produced blocks sealing instead of passing
  by absence; finalization is bounded to the selected checkpoint's journal prefix; and
  mutation scope is checked against the active unit on every path, reported live on the
  call that violates it.
- Put global planning on an authoring diet: the model writes one compact ownership/DAG
  mapping and the harness mechanically generates clause ids, exact citations, the
  requirements register, all-deferred schema-5 layers with derived `owned_requirements`,
  routing axes, empty evidence documents, and a rendered `plans/global.md` on every
  mapping write, with enumerated validation errors fed back warm; writes outside the
  mapping are denied, the verifier audits the mapping inside the default 6-turn ceiling,
  and expansion from a valid mapping passes the deterministic gate by construction on
  heterogeneous fixture families.
- Folded the SDK session-result subtype into the collected failure signal and widened the
  classifier to the SDK's raised "maximum number of turns" phrasing, so a real max-turns
  termination is labeled `max_turns_exhausted` immediately instead of burning retry sessions
  and reporting `session_stalled`; aligned global repair/verify kickoffs with the sparse
  contract by removing instructions to re-prove calibration and spike evidence those roles can
  no longer produce.

See [HIR-0002](docs/improvements/HIR-0002-structured-run-output.md),
[HIR-0003](docs/improvements/HIR-0003-truthful-until-clean-planning.md),
[HIR-0004](docs/improvements/HIR-0004-checkout-root-environment-loading.md),
[HIR-0005](docs/improvements/HIR-0005-plan-gate-authority-and-warm-repair.md),
[HIR-0006](docs/improvements/HIR-0006-canonical-reference-fingerprints.md),
[HIR-0007](docs/improvements/HIR-0007-temporal-and-ownership-evidence-coverage.md),
[HIR-0008](docs/improvements/HIR-0008-transactional-plan-publication-foundation.md),
[HIR-0009](docs/improvements/HIR-0009-run-scoped-plan-authoring.md),
[HIR-0010](docs/improvements/HIR-0010-executable-plan-authority-and-due-gates.md),
[HIR-0011](docs/improvements/HIR-0011-build-time-plan-falsification.md),
[HIR-0012](docs/improvements/HIR-0012-sparse-global-publication-contract.md),
[HIR-0013](docs/improvements/HIR-0013-unit-first-evidence-materialization.md),
[HIR-0014](docs/improvements/HIR-0014-instruments-that-cannot-lie.md),
[ADR-0002](docs/decisions/ADR-0002-run-scoped-artifact-authority.md),
[ADR-0003](docs/decisions/ADR-0003-explicit-metric-and-temporal-evidence-identity.md),
[ADR-0004](docs/decisions/ADR-0004-transactional-plan-authority.md),
[ADR-0005](docs/decisions/ADR-0005-sparse-global-publication-contract.md),
and [ADR-0006](docs/decisions/ADR-0006-unit-first-evidence-materialization.md).

## 0.3.0 — 2026-08-21

### Changed

- Renamed the project, package, commands, and environment prefix from the former project name to
  VFX Harness, `vfx-harness`, `vfx_harness`, `vfx`, and `VFXH_*`.
- Renamed the GitHub repository to `sahanruwantha/vfx-harness`.
- Reorganized runtime modules by decision responsibility.
- Separated architecture, decisions, improvements, operations, and research documentation.
- Separated tracked evaluation definitions from generated evaluation evidence.
- Categorized tests as unit, contract, architecture, and integration guarantees.

See [HIR-0001](docs/improvements/HIR-0001-project-identity-and-repository-structure.md) and
[ADR-0001](docs/decisions/ADR-0001-repository-authority-boundaries.md).
