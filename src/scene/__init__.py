"""The 3D reconstruction agent's Blender bridge.

Phase 0: a persistent, headless Blender the agent perceives through two channels —
``get_scene_graph`` (symbolic) and ``render_view`` (visual) — driven by :class:`BlenderBridge`.
See ``docs/3d-agent-architecture.md``.

Only the client side is imported here; :mod:`scene.server` requires ``bpy`` and is executed by
Blender, never imported by the project venv.
"""

from __future__ import annotations

from scene.animate import AnimationResult, build_animation, make_animation_realizer
from scene.assets import Asset, AssetLibrary, acquire_asset
from scene.animation import barrel_roll_code, sample_indices
from scene.bridge import BlenderBridge, BridgeError
from scene.anim_departments import (
    MOTION_STAGES,
    AnimPipelineResult,
    AnimStage,
    AnimStageResult,
    build_animation_pipeline,
)
from scene.compositor import bloom_compositor_code, comp_graph_code
from scene.critic import load_reference_images, make_scene_critic
from scene.departments import (
    DEFAULT_STAGES,
    DEFAULT_STAGES_WITH_FX,
    FX_STAGE,
    PipelineResult,
    Stage,
    StageResult,
    build_shot_pipeline,
    with_fx,
)
from scene.harness import Iteration, ShotResult, build_shot
from scene.hybrid import hybrid_stage_code, make_hybrid_realizer, plate_backdrop_code
from scene.higgsfield import HiggsfieldError, generate_image
from scene.panel import ATMOSPHERE_PRESETS, PanelResult, run_atmosphere_panel, signature_distance
from scene.plate import make_plate_generator, plate_prompt
from scene.realizer import (
    SceneBuildError,
    SceneSpec,
    asset_prompt,
    build_graybox,
    default_graybox,
    make_reconstruction_builder,
    make_scene_builder,
    scene_spec_from_beat,
)
from scene.shot_realizer import default_brief, make_pipeline_realizer, stages_for
from scene.supervisor import make_supervised_realizer
from scene.tripo import TripoError, image_to_glb

__all__ = [
    "BlenderBridge",
    "BridgeError",
    "ATMOSPHERE_PRESETS",
    "AnimationResult",
    "HiggsfieldError",
    "Iteration",
    "PanelResult",
    "SceneBuildError",
    "SceneSpec",
    "ShotResult",
    "Stage",
    "StageResult",
    "PipelineResult",
    "DEFAULT_STAGES",
    "DEFAULT_STAGES_WITH_FX",
    "FX_STAGE",
    "with_fx",
    "AnimStage",
    "AnimStageResult",
    "AnimPipelineResult",
    "MOTION_STAGES",
    "TripoError",
    "Asset",
    "AssetLibrary",
    "acquire_asset",
    "asset_prompt",
    "barrel_roll_code",
    "bloom_compositor_code",
    "comp_graph_code",
    "build_animation",
    "build_graybox",
    "build_shot",
    "build_shot_pipeline",
    "default_graybox",
    "generate_image",
    "hybrid_stage_code",
    "image_to_glb",
    "load_reference_images",
    "plate_backdrop_code",
    "make_animation_realizer",
    "make_hybrid_realizer",
    "make_plate_generator",
    "make_pipeline_realizer",
    "make_reconstruction_builder",
    "make_scene_builder",
    "make_scene_critic",
    "make_supervised_realizer",
    "default_brief",
    "stages_for",
    "plate_prompt",
    "run_atmosphere_panel",
    "sample_indices",
    "scene_spec_from_beat",
    "signature_distance",
]
