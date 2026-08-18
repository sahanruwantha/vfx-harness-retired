# This recipe actually RENDERS — that is the point of it, and a spike that skipped the
# render would prove nothing. Keep it cheap (80x45 after the probe's own 25%) and keep the
# PNGs out of the repo.
import os, tempfile, bpy

sc = bpy.context.scene
sc.render.resolution_x, sc.render.resolution_y = 320, 180
_tmp = tempfile.mkdtemp(prefix="bvfx_spike_")
os.makedirs(os.path.join(_tmp, "renders"), exist_ok=True)
os.chdir(_tmp)                       # the probe writes to cwd/renders/_m.png


def band_stats(px, w, h):
    """Stand-in for the shot's metric: mean luma per horizontal third.

    Also records what it saw as the spike's measured result — this recipe builds nothing,
    so a rendered number IS its evidence."""
    global SPIKE_NOTE
    rows = []
    for band in range(3):
        y0, y1 = band * h // 3, (band + 1) * h // 3
        vals = [px[(y * w + x) * 4] for y in range(y0, y1) for x in range(0, w, 4)]
        rows.append(round(sum(vals) / max(1, len(vals)), 5))
    SPIKE_NOTE = "measure_frame() rendered %dx%d, band means %s" % (w, h, rows)
    return {"bands": rows, "n": w * h}


# the ablation half needs a socket to switch off and measure. inputs[7] is Strength on
# 5.2's CompositorNodeGlare — the recipe's own example index, so the spike checks it.
glare = bvfx_glare_bloom(threshold=0.6, size=0.5, strength=0.4)

SPIKE_ARGS["fr"] = 1
SPIKE_NOTE = "renders real frames through measure_frame() at 320x180 x 25%"
