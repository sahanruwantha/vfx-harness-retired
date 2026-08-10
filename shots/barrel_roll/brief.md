---
id: barrel_roll
title: The Barrel Roll — Silk Road world inversion
type: motion            # still | motion
frames: 48
fps: 24
engine: BLENDER_EEVEE_NEXT
palette: [silk-road green, blackout, silk-road-2.0 purple]
---

# The Barrel Roll — Silk Road world inversion

## Logline
One continuous camera oner: the green Silk Road marketplace-world rolls upside down through a
blackout at the point of inversion and is reborn, inverted, as the purple "Silk Road 2.0" world.

## Description
A single unbroken shot. The camera flies forward over a field of emissive towers — the Silk Road
marketplace rendered as a city — while barrel-rolling counter-clockwise. As the world passes through
90° toward full inversion, the world colour and the towers' emission drop to near-black: a blackout
at the exact moment of inversion. The camera emerges upside down into a world rebuilt in purple —
"Silk Road 2.0" — and finishes the roll and the forward dive settled, inverted, over the new world.
The blackout at the midpoint is deliberate: it hides the swap of the green world for the purple one,
so the two marketplaces read as one continuous place turned inside out.

## Visual grammar
- Colour is the signal. GREEN = the original Silk Road; PURPLE = Silk Road 2.0; the BLACKOUT at
  inversion is the seam between them and must fully hide the swap.
- Towers are emissive verticals on a dark ground — a circuit-board city, not lit architecture.
- Near-black background throughout; the only real light is the towers' own emission and the world tint.
- Motion feel: smooth, weighty, continuous — one move, no cuts. Motion blur on.

## Elements to build
- world/ground: a dark plane the towers stand on, plus a world colour that tints the whole frame.
- towers: a field of tall emissive towers (the marketplace-as-city), in a green and a purple colourway.
- camera: the single rolling, diving oner (all the motion lives here).
- (no separate FX pass — the blackout is driven by keyed world/emission values, not a sim.)

## Camera
One continuous move over `frames` 1–48:
- forward DIVE across the whole shot (travels ~22 units toward the towers).
- one full COUNTER-CLOCKWISE barrel roll (0° → 360°), so the horizon passes through vertical and the
  frame ends inverted.
- slight downward pitch so the tower field reads from a low, aerial angle.

## Milestones
The key state-change frames — the non-negotiable targets. The motion critic checks each of these
against its reference; everything between them is the desk's to interpolate smoothly. Times are in the
reference's ~4.5s @10fps roll; map by STRUCTURE (not absolute time) to our 48-frame @24fps build.

| id | reference | our frame | state that MUST read |
|---|---|---|---|
| M1 | refs/M1_green.jpg | 1 | upright; full green world; tower at full emission; low aerial dive begins |
| M2 | refs/M2_blackout.jpg | ~24 | horizon past vertical; world + emission ramped to near-black — hides the swap |
| M3 | refs/M3_purple.jpg | ~32 | emerging inverted; Silk Road 2.0 tower re-lit; the new world revealed |
| M4 | refs/M4_end.jpg | 48 | settled, fully inverted; Silk Road 2.0 world; dive finished |

## Reference board
The actual image files the desks build to and the critics judge against — the master roll plus the
four isolated milestone crops above.

| role | file | shows |
|---|---|---|
| master-roll | refs/roll_10fps_contactsheet.jpg | the whole roll at 10fps (green → blackout → reveal → end), 45 frames |
| M1-green | refs/M1_green.jpg | upright, full green world, tower emissive (00:00.0) |
| M2-blackout | refs/M2_blackout.jpg | near-black inversion seam (~00:01.8) |
| M3-reveal | refs/M3_purple.jpg | emerging inverted, Silk Road 2.0 tower re-lit (~00:02.7) |
| M4-end | refs/M4_end.jpg | settled, inverted, Silk Road 2.0 world (~00:04.4) |

## Beats
| beat | frames | visual action | starts at | ends at | camera / motion |
|---|---|---|---|---|---|
| 1 — Upright: the green marketplace | 1–16 | The green Silk Road world: a field of emissive green towers on a dark ground, green world tint at full strength. | M1 | — | dive begins; roll 0°→~120°; low aerial angle |
| 2 — The inversion blackout | 16–32 | The roll passes through 90° toward inversion; world colour and tower emission ramp to near-black across the midpoint — the blackout that hides the world-swap. | M2 | — | dive continues; roll ~120°→~240°; horizon rotates past vertical |
| 3 — Inverted: Silk Road 2.0 revealed | 32–48 | Emerging from the blackout now upside down, the world is reborn as Silk Road 2.0 and settles. | M3 | M4 | dive finishes; roll ~240°→360°; ends fully inverted |
