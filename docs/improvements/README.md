# Harness Improvement Records

HIRs capture observed failure, root cause, general mechanism, rejected patches, validation,
and remaining limitations. Core behavior changes should link an HIR.

Copy [`HIR-template.md`](HIR-template.md) for a new record. Follow the
[`improvement lifecycle`](../operations/improvement-lifecycle.md) from observation through
release and revalidation.

Runtime evidence follows bound contracts' declared frames, not the activation layer's judge
list. Geometry-protected extra-frame rows remain required in live read-back, empty-scene
canonical replay, and deterministic revalidation without becoming new judge moments
(HIR-0143).
