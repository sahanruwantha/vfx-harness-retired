# A real scatter mesh: 200 boxes as consecutive 8-vertex runs in ONE mesh, which is the
# exact precondition the recipe's reshape(-1, 8, 3) depends on.
import bpy, random

random.seed(3)
verts, faces = [], []
for i in range(200):
    x, y = random.uniform(-400, 400), random.uniform(-400, 400)
    w, h = random.uniform(4, 14), random.uniform(8, 60)
    b = len(verts)
    verts += [(x - w, y - w, 0.0), (x + w, y - w, 0.0), (x + w, y + w, 0.0), (x - w, y + w, 0.0),
              (x - w, y - w, h), (x + w, y - w, h), (x + w, y + w, h), (x - w, y + w, h)]
    faces += [[b, b + 1, b + 2, b + 3], [b + 4, b + 5, b + 6, b + 7],
              [b, b + 1, b + 5, b + 4], [b + 2, b + 3, b + 7, b + 6]]
me = bpy.data.meshes.new("city_scatter")
me.from_pydata(verts, [], faces)
me.update()
_sc_obj = bpy.data.objects.new("city_scatter", me)
bpy.context.scene.collection.objects.link(_sc_obj)

scatter_name = "city_scatter"
HERO_R, NEAR_Y, BEHIND_Y = 120.0, -60.0, -260.0
