# EXPOSURE_OFFSET is the one number the recipe leaves to the shot (the AgX->Filmic stop
# pull it documents as -0.6..-1.0). The glare + beat make dip_glare_size() invocable.
EXPOSURE_OFFSET = -0.85

SPIKE_ARGS["glare"] = bvfx_glare_bloom(threshold=0.6, size=0.25, strength=0.7)
SPIKE_ARGS["beat"] = (18, 20, 24, 26)
