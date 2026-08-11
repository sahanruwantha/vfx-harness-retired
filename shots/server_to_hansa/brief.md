---
id: server_to_hansa
title: The Handover — a staging server becomes Hansa
type: motion            # still | motion
frames: 480
fps: 24
engine: BLENDER_EEVEE_NEXT
palette: [studio grey, led signal colours, data-path gold, hansa blue-white, hansa amber, violet night]
---

# The Handover — a staging server becomes Hansa

One shot, 480 frames @ 24fps, 1920×1080. Deliver the full render (mp4) plus the five
approval stills. This document is the CLIENT BRIEF — what, why, and how it will be
judged. The team's interpretation (frame mappings, numbers, build order, method)
belongs in `plan.md`, not here.

## Intent — read this first; it settles every ambiguity

Law enforcement did not shut the marketplace down — they became it. The shot must
leave the audience understanding one thing: the city-scale HANSA marketplace is now
running on the humble grey box they were introduced to a minute earlier. The device
is scale contrast: one ordinary rack on a bare floor versus a skyscraper wearing the
brand in amber. The connecting light-path is the proof — one unbroken line from the
box to the tower, which the camera itself then travels.

Consequences of the intent, stated so nobody re-derives them wrong:
- The rack must stay mundane and believable. Its ordinariness IS the story — a real
  machine you could rack in any data center, not a sci-fi prop.
- The quiet beat where the rack falls dark and plain, then re-lights, is THE handover.
  It must read, and it must not be dramatized. The market blinked; it came back; it
  came back on their machine.
- The path is literal evidence, not decoration: it starts at the rack's base, crosses
  the floor, and ends at the tower. The travel section follows that same line.
- The HANSA reveal must feel monumental precisely because the box felt small.

## The shot

**The box — roughly the first two-fifths.** Out of darkness a single server rack
materializes on a bare studio floor and grows to full presence. It is labeled plainly
in clean white type: "Staging Server." The camera goes intimate — a macro pass along
its blinking panels and yellow patch cables — then pulls back while its contents are
typed beside it: Code. Configs. Backups. The machine is established: real, ordinary,
loaded.

**The connection — to roughly three-quarters.** A warm gold data-path draws itself
out of the rack's base and zig-zags across the black floor toward a blue-lit
high-rise that fades up in the far darkness. The camera circles the rack as it sits
under an interrogating cone of light. For a moment the rack falls dark and plain —
then its lights return. The camera commits toward the path with a whip.

**The arrival — the remainder.** A ground-level race along the glowing path through
a corridor of dark buildings, the blue tower growing ahead; arrival at its base in a
spray of light; a climb up the ribbed blue-white facade; and the reveal — the amber
HANSA sign blazing at the crown against a violet night. The camera settles and holds.

## Non-negotiables — failing any of these fails review

1. The scale contrast lands: intimate ordinary box ↔ monumental branded tower.
2. The typography reads at delivery resolution: the "Staging Server" label, the typed
   inventory ("Code · Configs · Backups"), and the HANSA sign.
3. The blink reads: dark, plain machine → re-lit — BEFORE the journey commits.
4. One unbroken path connects box to tower, and the travel follows that path — the
   audience must never suspect the destination is a different place than the line
   led to.
5. The HANSA crown is amber-gold and legible; the shot ends settled, not mid-move.
6. The piece reads as one flowing camera move; transitions are hidden in speed and
   blur, never a visible cut.
7. Brand colours hold: tower blue-white, sign amber, sky violet. No bleed.

## References & authority

| ref | authoritative for |
|---|---|
| `refs/source_25fps.mp4` | motion, timing, structure, choreography — THE authority |
| `refs/M1_staging_server.jpg` | colour + composition at approval moment 1 (the box, labeled) |
| `refs/M2_inventory.jpg` | colour + composition at approval moment 2 (the typed inventory) |
| `refs/M3_connection.jpg` | colour + composition at approval moment 3 (box, path, waiting tower) |
| `refs/M4_datarun.jpg` | colour + composition at approval moment 4 (the race along the path) |
| `refs/M5_hansa_reveal.jpg` | colour + composition at approval moment 5 (settled HANSA end state) |
| `../barrel_roll/refs/M4_end.jpg` | secondary — series continuity for the violet-night sky family |

Rules: where this document's prose and a reference disagree, the reference wins.
Between references: the video wins on anything that moves; the stills win on colour
and framing at their moments.

## Acceptance

Judged on five approval moments — the stills above — plus a motion review of the
whole piece against the source (the materialize, the orbit feel, the blink, the whip
transitions, the race speed, the climb, the settle). Moments are defined by STATE,
not frame number; mapping moments to frames is the plan's job:

1. **The box, labeled** — rack alone under the overhead light, LEDs alive,
   "Staging Server" floating clean above it.
2. **The inventory** — rack with the fully typed list beside it: Code, Configs,
   Backups.
3. **The connection** — rack foreground under its cone of light, the gold path
   zig-zagging away across the floor, the blue tower waiting small in the dark.
4. **The run** — on the path at street level: path blazing underfoot, dark walls
   streaking past, the tower ahead.
5. **The reveal** — settled wide: amber HANSA crown, blue-white shaft, violet sky,
   mist at the base, city lights on the horizon.

## Constraints

- Engine: EEVEE Next on Blender 5.x (the harness owns engine setup; enum gotchas live
  in the recipes).
- No particle/physics simulations — all state changes are keyed values on keyable rigs.
- Duration locked: 480 frames. This is a structure-preserving restage of the 43.6s
  source at the pipeline's established compression (same ratio as barrel_roll's
  48/113).
- The Hansa brand look is fixed: blue-white tower, amber crown sign, violet night —
  shared sky family with the silk-road-2.0 world for series continuity.
- The look is emission-on-darkness plus a single overhead studio cone for the rack —
  no sun, no sky light, no lit-architecture rig.

## Anti-goals — known misreads

- The rack is not a sci-fi hero prop. Honest materials, small real LEDs, restraint.
- The text elements are clean white type, not holograms or glitch gimmicks.
- The path is a calm drawn line on the floor — not a laser show, not a particle
  stream. Speed supplies the drama later, in the run.
- The run corridor stays near-black with sparse lit windows: a corridor of dark, not
  a lit metropolis.
- The blink is quiet. No sparks, no flicker drama — the lights simply die and return.
- The reveal is a hold, not a flourish. Do not orbit the sign.

## Conflicts

If references disagree beyond the authority rules, or the intent appears to conflict
with a non-negotiable, STOP and record the conflict and your proposed resolution in
`plan.md`'s resolved-decisions section — with rationale. Do not silently "correct"
this document, and do not encode the resolution anywhere it can't be seen (a prompt,
a hardcode). This brief changes only by its author's hand.
