# The hero tower this recipe shades, plus the retune constants. `obj` (the prelude cube)
# stands in for the imported glb: the recipe's second block reaches into
# obj.data.materials[0], so the same object must be the one block 1 shades.
import bpy

import_result_name = obj.name

# retune-by-walking constants
LIT, DARK = 3.0, 0.008
COLS, ROWS = 2.0, 30.0

# additive transparent shell variant
tower_center = (0.0, 0.0, 0.0)
shell_dims = (2.2, 2.2, 2.2)
cell_pitch = (0.79, 0.79, 1.25)
density_x, density_y, density_z = 8.0, 8.0, 14.0

# vertical band silhouette variant
CORE_SEAM, FACE_EDGE = 0.12, 0.48
