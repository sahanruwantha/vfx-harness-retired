# BUILD PLAN — The Barrel Roll (Silk Road world inversion)

**Overview:** One continuous 48f/24fps oner — a low aerial forward-dive over a green
emissive-particle energy tower that whip-rolls past vertical into a near-black seam at
the midpoint, then *recovers to upright* as the world is reborn CYAN: a solid windowed
"Silk Road 2.0" skyscraper on a cool blue-grey night. Motion blur ON throughout.

> **Reference-vs-brief corrections (images win — HARD):**
> 1. Reborn world is **CYAN on cool blue-grey night**, *not* purple (M3/M4). Brief palette "purple" is wrong.
> 2. Camera **lands UPRIGHT** (M4 tower is base-down, sign-up), *not* inverted. It is a recovering whip-roll, not a 360.
> 3. **Two tower objects** whose visibility ranges cross in the blackout: a green particle energy tower and a solid 2.0 skyscraper — same silhouette, one model.
> 4. City lights are **warm amber** in every phase.

---

## PALETTE (hex sampled from refs)

| swatch | hex | source |
|---|---|---|
| Silk Road green — tower core (hot) | `#B6FFB6` | M1 tower center |
| Silk Road green — emissive body | `#33DD44` | M1 tower particles |
| Green world tint / nebula | `#0A2018` | M1 sky/haze |
| Blackout near-black | `#020304` | M2 |
| Warm amber city lights | `#FFB25A` | M1 / M4 sprawl |
| Motion-blur streak (warm white) | `#E8D8B0` | M2 streaks |
| Reborn cyan — "Silk Road 2.0" sign | `#35E4D6` | M3 / M4 sign |
| Cool blue-grey night sky | `#0E1622` | M4 |
| Cool blue haze | `#182636` | M3 / M4 |
| Solid tower body (blue-grey) | `#1A2430` | M4 building |
| Ground (dark) | `#05090C` | all |

---

## WORLD (background / tint / haze per phase)

| phase | frames | bg color | tint strength | haze |
|---|---|---|---|---|
| 1 — Green marketplace | 1–16 | `#0A2018` green nebula | full (1.0) | thin green vol, density ~0.03 |
| Blackout ramp-down | 16–22 | → `#020304` | 1.0 → ~0.02 | density → ~0.005 |
| Blackout hold | 22–26 | `#020304` near-black | ~0.02 | ~0.005 |
| Reborn ramp-up | 26–32 | `#020304` → `#0E1622` | ~0.02 → full | → cool blue `#182636`, density ~0.035 |
| 3 — Silk Road 2.0 | 32–48 | `#0E1622` cool blue-grey | full (1.0) | cool blue vol, density ~0.035 |

Near-black background at all times except the tint; the only real light is tower emission + city lights.

---

## OBJECTS

Tower placed at origin, base at z=0, top ~z=100 (Z up, ground on XY).

| object | position | color / emission | visible frames | notes |
|---|---|---|---|---|
| **Ground plane** | z=0, wide XY | body `#05090C`, non-emissive, takes world tint | 1–48 | dark plane the towers stand on |
| **City-light field** (low blocks) | XY sprawl below tower, z 0–6 | window emission `#FFB25A`, str 3.5 | 1–48 | warm amber BOTH worlds; smears to streaks under motion blur; dims str 3.5→0.3 (f22–26) then back to 3.5 by f32 |
| **Green energy tower** (particles) | (0,0,0)→z100, tower silhouette | particles `#33DD44`, core `#B6FFB6`, emission str 11 | 1–28 | emitter uses the sr2_tower silhouette (see Assets); str 11 (f1–12) → 0 by f24; render OFF after f28 |
| **Solid 2.0 skyscraper** | (0,0,0)→z100 | body `#1A2430`; windows `#FFE6B0` str 2.5; **sign** `#35E4D6` str 8 | 20–48 | hidden before f20; sign emission 0 until f26 → full (8) by f32; windows on from f28 |

**Swap (ranges cross in the black):** green tower fades to 0 by f24 and renders off at f28; solid tower fades in from f20 with sign lighting from f26. Overlap f20–28 sits inside the darkest blackout, so neither is legible during the trade.

---

## CAMERA

Forward dive along +Y (~22u travel), gentle descent (aerial → lower), aim at tower with
slight downward pitch. **Roll = single recovering whip:** upright → past vertical at the
black seam → recover to upright. Net rotation 0° (lands upright, NOT a 360). Motion blur ON.

| frame | position (x,y,z) | aim target | roll° (CCW) | lens |
|---|---|---|---|---|
| 1  | (0, -70, 52) | (0,0,60) | 0   | 30mm |
| 8  | (0, -64, 49) | (0,0,58) | 30  | 30mm |
| 16 | (0, -59, 46) | (0,0,56) | 100 | 30mm |
| 20 | (0, -57, 44) | (0,0,54) | 165 | 31mm |
| 24 | (0, -56, 43) | (0,0,53) | **200** | 31mm |
| 28 | (0, -54, 41) | (0,0,52) | 110 | 31mm |
| 32 | (0, -53, 40) | (0,0,52) | 35  | 32mm |
| 40 | (0, -50, 39) | (0,0,52) | 10  | 32mm |
| 48 | (0, -48, 38) | (0,0,52) | 0   | 32mm |

- Roll crosses vertical (90°) ~f16 going up and ~f30 coming down; peak **200°** (past inverted) at f24 sits in full black.
- Recovery f24→f32 is fast (165° in 8f) — the "recovering whip" — mostly hidden as the world re-lights.
- Ease-in/out at f1 and f48; roll rate peaks at the blackout (drives the motion-blur streaks in M2).

---

## EVENTS

| event | frames | driver |
|---|---|---|
| **Blackout seam** | 16–32 (near-black hold 22–26) | keyed world tint strength + all tower/city emission → ~0, then back up. No sim. |
| **World swap** | 20–28 (peak-hidden 22–26) | green tower emission→0 & render-off (f28) crossed with solid tower fade-in (f20) + sign light-up (f26→32) |
| **Whip peak** | ~24 | camera roll reaches 200° (past vertical) at the darkest frame |
| **Motion-blur streaks** | 12–30 | high roll rate + city-light emission smears (as M2) |

---

## BEATS (from brief)

| beat | frames | action |
|---|---|---|
| 1 — Upright: green marketplace | 1–16 | green energy tower full emission, green world; dive begins, roll 0°→100° |
| 2 — The inversion blackout | 16–32 | roll whips past vertical to 200° and recovers; world + emission ramp to near-black midpoint; swap hidden |
| 3 — Reborn: Silk Road 2.0 (cyan) | 32–48 | emerge upright, cyan-signed solid tower re-lit on cool blue-grey night; dive settles |

## MILESTONES

| id | frame | ref | state that must read |
|---|---|---|---|
| M1 | 1  | refs/M1_green.jpg | upright (roll 0°); full green world; green tower full emission; low aerial dive begins |
| M2 | 24 | refs/M2_blackout.jpg | roll past vertical (200°); world + emission near-black; only warm streaks — hides the swap |
| M3 | 32 | refs/M3_purple.jpg | emerging, roll recovered to ~35°; solid **cyan** "Silk Road 2.0" sign re-lit; cool blue-grey world revealed |
| M4 | 48 | refs/M4_end.jpg | settled **UPRIGHT** (roll 0°); Silk Road 2.0 cyan world; dive finished |

---

## ASSETS — 3D models to build (build these FIRST)

| # | asset | type | detail | notes / use across shot |
|---|---|---|---|---|
| 1 | **sr2_tower** (`assets/sr2_tower/model.glb`) | hero | high | ONE model, TWO roles: **particle/volume emitter** for the green energy tower (phase 1, f1–28) AND the **solid windowed building** with the cyan "Silk Road 2.0" sign (phase 3, f20–48). Tripo asset, 100u tall, base z=0. Do not duplicate as a second model. |
| 2 | **city_field** (low block sprawl) | set-dressing | low | dense low-poly rooftops below the tower that carry the warm amber window lights; rooftops read as silhouettes in M1/M4. Reused both worlds (lights recolor-free — amber throughout). |
| 3 | **ground_plane** | environment | low | dark plane the towers stand on; takes the world tint. |

### Not modelled (FX / shading / lighting)
- Green energy tower = **particle system** emitted from the sr2_tower silhouette (not a separate mesh).
- World / sky tint + green & cyan nebula; volumetric **haze/fog**.
- City **light points** / emissive glow; **cyan sign** emission shader; window emission.
- **Blackout** = keyed world-tint + emission values (no sim).
- **Motion blur**, **camera**, and all lighting.
