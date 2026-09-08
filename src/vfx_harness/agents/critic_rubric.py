"""Calibrated critic instructions, independent of changing candidate observations."""

from __future__ import annotations

import json

from vfx_harness.domain.critic_prompt import CriticPrompt
from vfx_harness.orchestration.ledger import Milestone


def critic_prompt(
    shot,
    m: Milestone,
    candidate_rel: str,
    axes: list[tuple[str, str]],
    motion_rel: str | None = None,
    motion_frames: list[int] | None = None,
    scope: str | None = None,
    evidence: list[dict] | None = None,
    claims: list[dict] | None = None,
    review_mode: str = "observer",
    focus_panels: list[dict] | None = None,
    focus_frames: list[int] | None = None,
    render_medium: str | None = None,
    prior_present: bool = False,
    prior_mean: float | None = None,
) -> CriticPrompt:
    axes = "\n".join(f"  - {k}: {desc}" for k, desc in axes)
    # The images are ATTACHED to this request, not fetched. The critic used to be an agent
    # that had to call Read to see them, and that indirection caused the same bug three
    # separate times: the path sandbox stonewalled its reads, relative paths resolved to
    # the repo root, and the guard meant to catch a blind verdict inspected the REQUEST
    # instead of the RESULT so it never fired. An attached frame cannot go unread.
    motion = ""
    if motion_rel:
        motion = (
            "\nThe image labelled MOTION STRIP shows the declared motion_frames of the shot side "
            "by side. Judge any MOTION/finish axis (motion blur, weighty continuous "
            "movement, the roll/dive progressing) from THAT strip, not from the single "
            "still (a still at one frame cannot show motion). Judge every other axis from "
            "the candidate.\n"
        )
    scope_block = ""
    if scope:
        scope_block = (
            "\n⚠ THIS IS A PARTIAL BUILD STAGE, NOT THE FINISHED SHOT. Its scope:\n"
            "See the selected authority target.scope.\n"
            "The axis list below has already been filtered to exactly what this layer "
            "owns. Score EVERY supplied axis with a number; there is no n/a decision in "
            "this stage. `observations` must contain only visible defects on a supplied axis "
            "that scored below 3, and only fixes inside this stage's scope — never 'add "
            "the thing a later stage adds'.\n"
        )
    evidence_block = ""
    if evidence:
        evidence_block = (
            "\nVERIFIED EVIDENCE ON THE EXACT CANDIDATE (machine-evaluated; values and "
            "PASS/FAIL are facts): see observation data evidence rows.\n"
            "If you raise a measurable issue, begin it with `[check:<FAILED_ID>]`. "
            "A measurable issue without a failed id, or one contradicting a PASS above, "
            "will be removed before it can trigger repair. A scene-contract PASS proves "
            "the named state exists, not that it reads well: report any remaining visibility "
            "problem as a qualitative observation without denying the measured fact.\n"
        )
    claims_block = ""
    if claims:
        claims_block = (
            "\nACTIVE CLAIM MANIFEST FOR THIS EXACT MOMENT: see selected authority claims.\n"
            "\nFor a planned defect, set claim_id to exactly one id above and cite only "
            "that claim's evidence ids. If a visible property is absent from this manifest, "
            "set claim_id to null: that is a coverage finding for the planner, not permission "
            "for the builder to mutate the scene. Never borrow another property's passing or "
            "failing check.\n"
        )
    focus_block = ""
    if focus_panels:
        focus_block = (
            "\nSUPPLIED FOCUS PANELS (supplemental; the full frame still controls "
            "composition/context):\n"
            "See observation data focus_panels.\n"
            "Each panel contains aligned CANDIDATE | REFERENCE and a 50/50 wipe. "
            "Its crop uses normalized TOP-LEFT coordinates in source_frame; res_pct "
            "records optical resolution.\n"
            "\nThese already answer the close-inspection request. Return an empty "
            "focus_requests list and cite any panel used in observations[].panel_ids.\n"
        )
    medium_block = ""
    if str(render_medium or "").lower() == "solid":
        medium_block = (
            "\n⚠ THIS PLATE IS WORKBENCH SOLID, NOT A BEAUTY RENDER. Materials, shaders, "
            "emission, textures and world lighting are SUPPRESSED by the renderer. It shows "
            "form only: silhouette, massing, proportion, placement, occlusion, and geometric "
            "detail that exists as actual mesh.\n"
            "Judge ONLY those. Colour, tone, brightness, reflectivity, glow, surface finish, "
            "material pattern and window/panel detail carried by a shader are NOT ABSENT FROM "
            "THE BUILD — they are absent from this rendering, by design. Their absence is not "
            "a defect and must not be scored down or raised as an observation. A flat grey "
            "surface here is what a correctly shaded surface looks like in this medium.\n"
            "If an axis can only be judged from appearance, it cannot be judged on this plate: "
            "score it from form where that is meaningful, and otherwise say so in "
            "reference_note rather than inventing an appearance verdict. The REFERENCE is a "
            "finished image; do not fault the candidate for the difference the medium itself "
            "creates.\n"
        )
    review_block = ""
    if review_mode == "evidence_audit":
        review_block = (
            "\nSECOND-OPINION ROLE: evidence auditor. Start from the executable evidence, "
            "then independently inspect only the qualitative residuals. The first judge "
            "was borderline; do not repeat a numeric estimate the evidence already answers.\n"
        )
    elif review_mode == "tie_breaker":
        review_block = (
            "\nTIE-BREAK ROLE: conservative adjudicator. Separate machine-verifiable facts "
            "from photographic judgment. Fail only for a visible qualitative defect or a "
            "cited failed check, not because another judge may have failed it.\n"
        )
    elif review_mode == "focus_review":
        review_block = (
            "\nFOCUS-REVIEW ROLE. The full reference and candidate remain the decision "
            "context; the additional aligned panels only resolve small-feature legibility. "
            "Each panel contains candidate/reference detail views at the exact same crop. "
            "Do not request another crop. If a blocking visual issue relies on a focus "
            "panel, cite its id in that observation's panel_ids.\n"
        )
    rubric = (
        "Judge the target state and exact moment declared in selected authority target.\n"
        "The image labelled REFERENCE is the target.\n"
        f"The image labelled CANDIDATE is the render to score.{motion}"
        f"{medium_block}{scope_block}{claims_block}{evidence_block}{focus_block}{review_block}"
        f"\nScore the candidate against the reference on these axes:\n"
        f"{axes}\n\n"
        f"Score each axis 0–5"
        f"{' (or n/a only when no layer scope is supplied)' if not scope else ''}. "
        f"For a score below 3, put one typed item in `observations`: visible evidence, "
        f"one atomic property, one bounded correction, the exact moment and semantic roles. "
        f"Use `focus_requests` only when a feature material to an axis scoring at or below "
        f"3 is too small to resolve in the full images: at most two [x0,y0,x1,y1] regions "
        f"in normalized TOP-LEFT coordinates. Focusable source frames with matching "
        "references are declared in observation data focus_frames. Name source_frame and whether region "
        f"uses candidate_frame coordinates or global motion_strip coordinates. A motion-strip "
        f"region must stay inside one panel. Never request a crop for a measurable fact "
        f"already settled by evidence, and return an empty list whenever focus panels are "
        f"already supplied. "
        f"If all scores are at least 3, return an empty `observations` list. "
        f"Return the JSON scorecard."
    )

    if prior_present:
        rubric += (
            "\nPREVIOUS ATTEMPT is context only. Do NOT score this image. Use it to say whether "
            "the candidate improved or regressed, and record as a typed observation anything "
            "the previous attempt got right that the candidate has lost. Its prior_mean is "
            "observation data, not evidence of current acceptance."
        )
    evidence_fields = ("id", "metric", "value", "target", "pass", "authoritative", "origin", "source", "objects")
    claim_fields = ("id", "axis", "property", "roles", "controls", "authority", "evidence_ids", "proposition")
    panel_fields = ("id", "axis", "crop", "source_frame", "reference", "views", "reason", "res_pct", "image_rel")
    return CriticPrompt(rubric, json.dumps({
        "reference": m.ref, "candidate": candidate_rel,
        "evidence": [{key: row[key] for key in evidence_fields if key in row} for row in (evidence or [])],
        "focus_panels": [{key: row[key] for key in panel_fields if key in row} for row in (focus_panels or [])],
        "motion_frames": motion_frames or [], "focus_frames": focus_frames or [m.frame], "prior_mean": prior_mean,
    }, sort_keys=True), json.dumps({
        "target": {"stage": str(m.id), "shot": shot.id, "frame": m.frame, "reads": m.reads, "scope": scope},
        "claims": [{key: row[key] for key in claim_fields if key in row} for row in (claims or [])],
    }, sort_keys=True))
