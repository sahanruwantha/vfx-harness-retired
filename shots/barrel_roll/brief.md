---
id: barrel_roll
title: The Barrel Roll — Silk Road world inversion
type: motion            # still | motion
frames: 48
fps: 24
engine: BLENDER_EEVEE_NEXT
palette: [silk-road green, blackout, silk-road-2.0 violet, sign cyan]
---

# The Barrel Roll — Silk Road world inversion

One shot, 48 frames @ 24fps, 1920×1080. Deliver the full render (mp4) plus the four
approval stills. This document is the CLIENT BRIEF — it says what and why, and how the
work will be judged. The team's interpretation (frame mappings, numbers, build order,
method) lives in `plan.md` and is not this document's business.

## Intent — read this first; it settles every ambiguity

The Silk Road marketplace is seized and reborn as Silk Road 2.0. One unbroken camera
move rolls the world upside down through a blackout — the seizure — and emerges over
the same city, re-branded. The audience must feel it is ONE place turned inside out,
not a cut to a new place. The emotional beat of the shot is the reveal: the old
market's green energy settling onto the new tower and re-lighting as its name.

Consequences of the intent, stated so nobody re-derives them wrong:
- "Inverted" describes the WORLD's story-state after the flip — not the camera being
  upside down at the end. The shot ends composed and stable.
- The blackout exists to hide the world-swap. If a viewer can point at the moment the
  swap happens, the shot has failed.
- The sign re-lighting is the payoff. Everything else serves it.

## The shot

Roughly the first third: the green world at full strength — an emissive particle tower
over a glittering dark city under a turbulent green sky — while the camera dives
forward and the roll gathers. A violent middle: the roll whips past vertical, the
world dies to near-black, and the swap happens here, invisibly; the only life on
screen is a few streaking lights. The remainder: the new world resolves — the same
city under a violet sky, a solid dark tower where the energy tower stood, the green
remnant on its crown becoming the lit cyan "Silk Road 2.0" sign — and the move settles
into a calm hold before the shot ends.

## Non-negotiables — failing any of these fails review

1. The world-swap is undetectable. The seam is sacred.
2. One continuous move: no cuts, no fades, a single rotational direction throughout.
3. The "Silk Road 2.0" sign reads clearly in the end state.
4. Colour is the narrative: saturated green = the old market · near-black = the
   seizure · violet night = the reborn world · cyan = the new brand. No palette bleed
   across the seam.
5. The blackout is a beat INSIDE the move (heavy motion, streaked light), not an edit.
6. The motion fully resolves before the last frame — the shot ends settled, not
   mid-move.

## References & authority

| ref | authoritative for |
|---|---|
| `refs/source_25fps.mp4` | motion, timing, structure, choreography — THE authority |
| `refs/M1_green.jpg` | colour + composition at approval moment 1 (green hold) |
| `refs/M2_blackout.jpg` | colour + composition at approval moment 2 (the seam) |
| `refs/M3_purple.jpg` | colour + composition at approval moment 3 (the reveal) |
| `refs/M4_end.jpg` | colour + composition at approval moment 4 (settled end) |
| `refs/roll_10fps_contactsheet.jpg` | deprecated — undersampled (drops the seam and the morph); history only |

Rules: where this document's prose and a reference disagree, the reference wins.
Between references: the video wins on anything that moves; the stills win on colour
and framing at their moments.

## Acceptance

Judged on four approval moments — the stills above — plus a motion review of the whole
move against the source video (rotation direction and profile, the pass-by, the seam
depth and brevity, the settle-and-hold). Moments are defined by STATE, not by frame
number; mapping moments to frames is the plan's job:

1. **Green hold** — upright, full green world, particle tower at full strength, dive
   beginning.
2. **The seam** — near-black, a few streaked lights, nothing legible, heavy motion.
3. **The reveal** — the new tower still tilted, green energy on its crown, the sign
   emerging, the violet world at partial strength.
4. **Settled end** — stable, sign lit and legible, violet world at full strength, no
   green left anywhere.

## Constraints

- Engine: EEVEE Next on Blender 5.x (the harness owns engine setup; enum gotchas live
  in the recipes).
- No particle/physics simulations — all state changes are keyed values. (Dissolves and
  morphs must be achieved with keyable rigs.)
- The 2.0 building is the committed `sr2_tower` asset — continuity with the asset
  library, do not model a replacement.
- Duration locked: 48 frames. The look is carried by emission and atmosphere — there
  is no sun, no sky light, no lit-architecture rig.

## Anti-goals — known misreads, all previously paid for

- This is not lit architecture. It is a circuit-board world: emission on darkness.
- The end frame is not "the camera upside down." See Intent.
- The green tower is not a windowed building — it is a hologram of light with no
  visible surface.
- Do not brighten the seam "so something stays visible." Dead black with a few warm
  streaks is correct.
- The city must survive the low final framing — it reads as buildings, not as a
  carpet of dots.

## Conflicts

If references disagree beyond the authority rules, or the intent appears to conflict
with a non-negotiable, STOP and record the conflict and your proposed resolution in
`plan.md`'s resolved-decisions section — with rationale. Do not silently "correct"
this document, and do not encode the resolution anywhere it can't be seen (a prompt,
a hardcode). This brief changes only by its author's hand.
