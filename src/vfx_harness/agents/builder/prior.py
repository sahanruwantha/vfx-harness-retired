"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.agents.builder.builder_options import _builder_options as _builder_options
from vfx_harness.agents.builder.models import (
    UnpassedPrior,
)
from vfx_harness.blender.session import BlenderError, BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.infrastructure.trusted_files import (
    TrustedFileBinding,
    TrustedFileError,
    read_trusted_file,
    require_trusted_file_unchanged,
)
from vfx_harness.observability.log import (
    log,
)
from vfx_harness.orchestration import authority_selection, layer_publication
from vfx_harness.orchestration import generate_construction as generate_construction
from vfx_harness.orchestration.layer_plans import read_layer_plan, read_work_unit_plan
from vfx_harness.orchestration.selected_layer_chain import selected_layer_chain
from vfx_harness.orchestration.unit_evaluation_receipts import (
    ExecutedReplayDependency,
    ExecutedReplayInput,
)

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
    from vfx_harness.orchestration.ledger import Layer


class _ReceiptBackedPriorPaths(list[Path]):
    """List-compatible replay prefix retaining its terminal publication identities."""

    def __init__(
        self,
        paths: list[Path],
        *,
        shot: Shot,
        selected_authority: ResolvedSelectedAuthority,
        publications: tuple[tuple[Layer, str, str, str], ...],
    ) -> None:
        super().__init__(paths)
        self.shot = shot
        self.selected_authority = selected_authority
        self.publications = publications

    def __add__(self, other):
        return _ReceiptBackedPriorPaths(
            [*self, *other],
            shot=self.shot,
            selected_authority=self.selected_authority,
            publications=self.publications,
        )

    def require_current(self, where: str) -> None:
        for (
            layer,
            expected_receipt_digest,
            expected_script_path,
            expected_script_sha256,
        ) in self.publications:
            publication = layer_publication.require_current_layer_publication(
                self.shot.folder,
                layer,
                self.selected_authority,
            )
            if publication.receipt.receipt_digest != expected_receipt_digest:
                raise layer_publication.LayerPublicationConflict(
                    f"{where}: layer {layer.id} terminal finalization receipt changed"
                )
            if (
                publication.receipt.layer_script_path != expected_script_path
                or publication.receipt.layer_script_sha256 != expected_script_sha256
            ):
                raise layer_publication.LayerPublicationConflict(
                    f"{where}: layer {layer.id} terminal replay source changed"
                )

    def prepare_inputs(self) -> tuple[PreparedArtifactReplayInput, ...]:
        root = self.shot.folder.expanduser().absolute()
        entries: list[tuple[str, Path]] = []
        for index, path in enumerate(self):
            absolute = path.expanduser().absolute()
            try:
                locator = absolute.relative_to(root).as_posix()
            except ValueError as exc:
                raise BlenderError(
                    f"receipt-backed prior replay input {index} escapes the shot root"
                ) from exc
            entries.append((locator, absolute))
        prepared = _prepare_artifact_replay_inputs(root, entries)
        by_locator = {item.executed.script_path: item for item in prepared}
        for (
            layer,
            _receipt_digest,
            script_path,
            script_sha256,
        ) in self.publications:
            item = by_locator.get(script_path)
            if item is None or item.executed.script_sha256 != script_sha256:
                raise BlenderError(
                    f"layer {layer.id} prepared replay bytes do not match its terminal "
                    "finalization receipt"
                )
        return prepared


def _prior_layer_paths(
    shot: Shot,
    layer,
    *,
    force: bool = False,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[Path]:
    """Return the exact accepted selected-DAG prefix before this layer.

    Raises UnpassedPrior unless every one of them is recorded 'passed'."""

    selected_authority = selected_authority or authority_selection.resolve_selected_authority(
        shot.folder
    )

    chain = selected_layer_chain(
        shot,
        selected_authority=selected_authority,
    )
    matches = [index for index, candidate in enumerate(chain) if candidate.id == layer.id]
    if len(matches) != 1 or chain[matches[0]] != layer:
        raise ValueError(
            f"layer {getattr(layer, 'id', None)!r} is not the exact selected-DAG layer"
        )
    prior_layers = chain[: matches[0]]
    # A replay prefix without one complete public finalization is not a degraded mode.
    # Accepted paths come from the exact terminal receipt, never from directory presence.
    keep: list[Path] = []
    publications: list[tuple[Layer, str, str, str]] = []
    unpassed: list[str] = []
    for g in prior_layers:
        fallback = shot.folder / g.script
        try:
            publication = layer_publication.require_current_layer_publication(
                shot.folder,
                g,
                selected_authority,
            )
        except layer_publication.LayerPublicationConflict as exc:
            unpassed.append(f"layer {g.id} publication is invalid: {exc}")
            if force and fallback.is_file():
                keep.append(fallback)
            continue
        keep.append(shot.folder / publication.receipt.layer_script_path)
        publications.append(
            (
                g,
                publication.receipt.receipt_digest,
                publication.receipt.layer_script_path,
                publication.receipt.layer_script_sha256,
            )
        )
    # FAIL CLOSED. This used to warn and chain anyway, so a layer could be built on top of
    # a predecessor whose content was never accepted — every judgement above it then rests
    # on unreviewed geometry. The protection previously lived in the shell script that
    # drove a full run, which meant running a single layer by hand silently bypassed it.
    if unpassed and not force:
        raise UnpassedPrior(
            f"refusing to build layer {layer.id} on unaccepted work: "
            + "; ".join(unpassed)
            + ". Re-run those layers, or pass --force to chain anyway (debugging only)."
        )
    if unpassed:
        log(f"! --force: chaining {len(unpassed)} unaccepted prior(s) — " + "; ".join(unpassed))
    return _ReceiptBackedPriorPaths(
        keep,
        shot=shot,
        selected_authority=selected_authority,
        publications=tuple(publications),
    )


class ChainBroken(RuntimeError):
    """A prior layer script no longer composes — the chain must be repaired first."""


_ARTIFACT_EVALUATION_BARRIER = (
    "import bpy\n"
    "_vfx_scene=bpy.context.scene\n"
    "_vfx_scene.frame_set(int(_vfx_scene.frame_current))\n"
    "bpy.context.view_layer.update()\n"
)


@dataclass(frozen=True, slots=True)
class PreparedArtifactReplayInput:
    """Exact descriptor-read source bytes plus their canonical replay locator."""

    executed: ExecutedReplayInput
    source_path: Path
    payload: bytes
    construction: generate_construction.PreparedConstructionReplayInput | None = None
    construction_prepared: bool = False


def require_prepared_artifact_replay_input_unchanged(
    prepared: PreparedArtifactReplayInput,
) -> None:
    """Retain script plus optional pointer/GLB identity through publication."""

    try:
        require_trusted_file_unchanged(
            prepared.executed.source_binding,
            "canonical artifact replay input",
        )
        if prepared.construction is not None:
            generate_construction.require_prepared_construction_replay_current(
                prepared.construction
            )
    except (TrustedFileError, generate_construction.GenerateConstructionError) as exc:
        raise BlenderError(str(exc)) from exc


def _prepare_artifact_replay_inputs(
    shot_root: str | Path,
    entries: list[tuple[str, Path]],
) -> tuple[PreparedArtifactReplayInput, ...]:
    """Read the complete ordered replay prefix before any byte reaches Blender."""

    root = Path(shot_root).expanduser().absolute()
    prepared: list[PreparedArtifactReplayInput] = []
    for index, (locator, source_path) in enumerate(entries):
        try:
            snapshot = read_trusted_file(
                root,
                source_path,
                f"canonical artifact replay input {index}",
                require_nonempty=True,
            )
        except TrustedFileError as exc:
            raise BlenderError(str(exc)) from exc
        try:
            # Scratch candidate bytes execute under their canonical script identity.
            # Construction belongs to that identity, not to the temporary source path.
            construction = generate_construction.prepare_construction_replay_input(
                root,
                root / locator,
            )
        except generate_construction.GenerateConstructionError as exc:
            raise BlenderError(str(exc)) from exc
        dependencies = (
            ()
            if construction is None
            else tuple(
                ExecutedReplayDependency(
                    kind=dependency.kind,
                    path=dependency.path,
                    sha256=dependency.sha256,
                    source_binding=dependency.binding,
                )
                for dependency in construction.dependencies
            )
        )
        prepared.append(
            PreparedArtifactReplayInput(
                executed=ExecutedReplayInput(
                    script_path=locator,
                    script_sha256=snapshot.sha256,
                    source_binding=snapshot.binding,
                    dependencies=dependencies,
                ),
                source_path=snapshot.binding.path,
                payload=snapshot.payload,
                construction=construction,
                construction_prepared=True,
            )
        )
    for item in prepared:
        require_prepared_artifact_replay_input_unchanged(item)
    return tuple(prepared)


def _run_artifact_script(
    session: BlenderSession,
    path: Path,
    prepared_input: PreparedArtifactReplayInput | None = None,
    *,
    journal: bool = True,
) -> dict:
    """Replay one artifact and publish its evaluated state to the next consumer.

    A successful Python execution is not yet a Blender dependency-graph boundary.
    Successors may legally consume producer world transforms immediately, so every
    artifact replay ends with an unjournalled current-frame evaluation (HIR-0117).
    """
    source: str
    source_binding: TrustedFileBinding | None = None
    if prepared_input is None:
        source = path.read_text(encoding="utf-8")
    else:
        expected = Path(path).expanduser().absolute()
        if prepared_input.source_path != expected:
            raise BlenderError(
                "prepared artifact replay input belongs to another source path: "
                f"expected {expected}, found {prepared_input.source_path}"
            )
        source_binding = prepared_input.executed.source_binding
        try:
            require_trusted_file_unchanged(
                source_binding,
                "canonical artifact replay input",
            )
        except TrustedFileError as exc:
            raise BlenderError(str(exc)) from exc
        try:
            source = prepared_input.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BlenderError(f"artifact replay script is not UTF-8: {path}") from exc
    if prepared_input is not None and prepared_input.construction_prepared:
        try:
            generate_construction.pin_prepared_construction_replay(
                session,
                prepared_input.construction,
            )
        except generate_construction.GenerateConstructionError as exc:
            raise BlenderError(str(exc)) from exc
    else:
        generate_construction.pin_for_script(session, path)
    result = session.run(
        source,
        journal=journal,
        execution_policy="artifact",
    )
    session.run(_ARTIFACT_EVALUATION_BARRIER, journal=False)
    if source_binding is not None:
        require_prepared_artifact_replay_input_unchanged(prepared_input)
    return result


def _run_prior_paths(
    session: BlenderSession,
    paths: list[Path],
    prepared_inputs: tuple[PreparedArtifactReplayInput, ...] | None = None,
) -> list[str]:
    """Replay the accepted chain. A failure here is NOT this layer's fault: layer scripts
    reference each other's objects by name (30_purple.py does D.objects['tower_dot']
    from 20_green.py), so re-running an early layer can invalidate every later one and
    the break only surfaces now. Say so plainly instead of leaking a raw bpy KeyError."""
    if prepared_inputs is not None and len(prepared_inputs) != len(paths):
        raise ChainBroken(
            "canonical prior replay input count does not match the selected prefix"
        )
    receipt_backed = paths if isinstance(paths, _ReceiptBackedPriorPaths) else None
    if receipt_backed is not None:
        try:
            receipt_backed.require_current("before prior replay")
            if prepared_inputs is None:
                prepared_inputs = receipt_backed.prepare_inputs()
        except (BlenderError, layer_publication.LayerPublicationConflict) as exc:
            raise ChainBroken(
                f"prior layer publication changed before replay: {exc}"
            ) from exc
    names = []
    for index, p in enumerate(paths):
        log(f"running prior layer script {p.name}")
        try:
            _run_artifact_script(
                session,
                p,
                None if prepared_inputs is None else prepared_inputs[index],
            )
        except BlenderError as e:
            first = str(e).strip().splitlines()[0]
            raise ChainBroken(
                f"{p.name} no longer composes onto the scene built by the layers before "
                f"it: {first}\n  The chain is broken, not this layer. Re-run {p.name}'s "
                f"layer (or restore the prior script it was authored against) before "
                f"building further."
            ) from e
        if receipt_backed is not None:
            try:
                receipt_backed.require_current(f"after replaying {p.name}")
            except layer_publication.LayerPublicationConflict as exc:
                raise ChainBroken(
                    f"prior layer publication changed during replay: {exc}"
                ) from exc
        names.append(p.name)
    return names


def _plan_layer_excerpt(
    shot: Shot,
    layer,
    unit=None,
    *,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> str:
    """The layer's just-in-time execution plan; giant-plan fallback is forbidden."""

    if unit is not None:
        return read_work_unit_plan(
            shot.folder,
            layer,
            unit,
            selected_authority=selected_authority,
        )
    return read_layer_plan(
        shot.folder,
        layer,
        selected_authority=selected_authority,
    )


def _preamble(shot: Shot) -> str:
    """Deterministic scene setup the build script may assume is already applied."""
    return (
        "import bpy\n"
        "sc = bpy.context.scene\n"
        # resolve the EEVEE engine name for this build (5.2 = BLENDER_EEVEE, not …_NEXT)
        "_av = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}\n"
        "sc.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _av else 'BLENDER_EEVEE'\n"
        f"sc.render.fps = {shot.fps}\n"
        f"sc.render.resolution_x = {shot.resolution[0]}\n"
        f"sc.render.resolution_y = {shot.resolution[1]}\n"
        "sc.render.resolution_percentage = 100\n"
        "sc.frame_start = 1\n"
        f"sc.frame_end = {shot.frames}\n"
        "sc.render.use_motion_blur = True\n"
    )
