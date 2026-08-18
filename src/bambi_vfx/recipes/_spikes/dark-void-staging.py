# The recipe widens an existing key spot; `floor` is already in the prelude.
import bpy

_spot = bpy.data.objects.new("studio_cone", bpy.data.lights.new("studio_cone", "SPOT"))
bpy.context.scene.collection.objects.link(_spot)
_spot.location = (0.0, -6.0, 9.0)
