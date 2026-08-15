# `g` is the compositor Glare node the "by hand" half of the recipe pokes. Building it via
# the helper is deliberate: it proves the socket names in the recipe's table are the ones
# the helper's node actually has.
g = bvfx_glare_bloom(threshold=0.6, size=0.5, strength=0.4)
