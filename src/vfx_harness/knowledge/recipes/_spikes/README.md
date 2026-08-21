# spike fixtures

One file per recipe that needs scaffolding before its code can run: `<recipe-name>.py`.

`vfx_harness/knowledge/verify_recipes.py` execs the fixture in the spike scene **before** the recipe's
own ```python blocks, in the same namespace. Use it to create the named datablocks the
recipe assumes exist in a real shot (`street_mat`, `studio_cone`, a keyed `path_gold`),
to bind placeholder constants (`EXPOSURE_OFFSET`, `f_start`), and to seed `SPIKE_ARGS`
so the verifier can invoke a callable whose parameters it cannot guess:

    SPIKE_ARGS["glare"] = bvfx_glare_bloom(threshold=0.6)
    SPIKE_ARGS["spans"] = [(1, True), (10, False)]

Everything the prelude builds is already in scope: `sc`, `obj`, `mat`, `nt`, `bsdf`,
`cam`, `cam_rig`, `floor`, `holder`, and the whole `bvfx_*` helper namespace.

These live OUTSIDE the .md on purpose. `find_recipe` hands the build agent the recipe body
verbatim; test scaffolding in there would read as part of the technique and get pasted into
a build. A fixture is evidence plumbing, not craft.

A fixture may NOT paper over a defect. If a recipe calls a helper that does not exist
anywhere (`legible-3d-typography` used to call an undefined `key_strength`), fix the
recipe — defining the missing name here would verify a snippet nobody can actually run.
