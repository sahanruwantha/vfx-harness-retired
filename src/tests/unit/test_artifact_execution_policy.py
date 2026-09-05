from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from vfx_harness.agents.builder.prior import _ARTIFACT_EVALUATION_BARRIER
from vfx_harness.blender.artifact_execution import (
    ArtifactExecutionPolicyError,
    artifact_builtins,
    validate_artifact_source,
)
from vfx_harness.blender.filesystem_confinement import (
    HARNESS_RUNTIME_ROOT,
    confined_worker_argv,
    open_real_directory,
    prepared_worker_command,
)
from vfx_harness.blender.session import BlenderSession
from vfx_harness.orchestration.generate_construction import (
    ensure_construction_read_namespace,
)


@contextlib.contextmanager
def _shot_confined_command(
    command: list[str],
    *,
    shot: Path,
    writable: Path,
    readable: tuple[Path, ...] = (),
) -> Iterator[tuple[list[str], tuple[int, ...]]]:
    descriptors = [
        open_real_directory(writable, "test writable root"),
        *(
            open_real_directory(path, "test readable root")
            for path in readable
        ),
        open_real_directory(HARNESS_RUNTIME_ROOT, "test harness runtime"),
    ]
    try:
        argv = confined_worker_argv(
            command,
            writable_roots=(writable,),
            writable_root_descriptors=(descriptors[0],),
            readable_roots=readable,
            readable_root_descriptors=tuple(descriptors[1:-1]),
            runtime_root_descriptor=descriptors[-1],
            authority_root=shot,
            current_run_root=shot / "runs" / "r1",
        )
        yield argv, tuple(descriptors)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


@pytest.mark.parametrize(
    "source, capability",
    [
        ("open('build/units/1/u.py', 'w').write('owned')", "open|write"),
        ("import pathlib\npathlib.Path('build/x.py').write_text('owned')", "pathlib"),
        ("import os\nos.replace('candidate.py', 'build/x.py')", "os"),
        ("import subprocess\nsubprocess.run(['touch', 'build/x.py'])", "subprocess"),
        (
            "import bpy\nbpy.ops.wm.save_as_mainfile(filepath='build/x.blend')",
            "closed replay vocabulary",
        ),
        ("import bpy\nbpy.data.libraries.write('build/x.blend', set())", "write"),
        (
            "import bpy\nbpy.context.scene.render.filepath='build/x.png'\n"
            "bpy.ops.render.render(write_still=True)",
            "external-output|rendering",
        ),
        (
            "import bpy\nnode=bpy.context.scene.node_tree.nodes.new('CompositorNodeOutputFile')",
            "external-output node",
        ),
        (
            "import bpy\nbpy.data.images.load('/usr/share/pixmaps/input.png')",
            "external data load",
        ),
        (
            "import bpy\nwith bpy.data.libraries.load('/usr/share/input.blend') as data:\n"
            "    pass",
            "external data load",
        ),
        (
            "import bpy as b\nb.data.images.load('/usr/share/input.png')",
            "external data load",
        ),
        (
            "from bpy import data\ndata.images.load('/usr/share/input.png')",
            "from-bpy imports",
        ),
        (
            "from bpy import ops\nops.wm.append(filepath='/usr/share/input.blend')",
            "from-bpy imports",
        ),
        (
            "import bpy\nload = bpy.data.images.load\nload('/usr/share/input.png')",
            "external data load",
        ),
        (
            "import bpy\nwm = bpy.ops.wm\nwm.append(filepath='/usr/share/input.blend')",
            "closed replay vocabulary",
        ),
        (
            "import bpy\nbox = [bpy.ops.wm]\n"
            "box[0].append(filepath='/usr/share/input.blend')",
            "capability cannot escape",
        ),
        (
            "import bpy\nbpy.ops.wm.append(filepath='/usr/share/input.blend')",
            "closed replay vocabulary",
        ),
        (
            "import bpy\nbpy.ops.import_scene.fbx(filepath='/usr/share/input.fbx')",
            "closed replay vocabulary",
        ),
        (
            "import bpy\nbpy.utils.execfile('/usr/share/input.py')",
            "execfile|utility capability",
        ),
        (
            "import bpy\nbpy.ops.clip.open(directory='/usr/share', "
            "files=[{'name': 'input.mov'}])",
            "closed replay vocabulary",
        ),
        (
            "import bpy\nbpy.ops.text.open(filepath='/usr/share/input.py')",
            "closed replay vocabulary",
        ),
        (
            "import bpy\nbpy.ops.sequencer.movie_strip_add(filepath='/usr/share/input.mov')",
            "closed replay vocabulary",
        ),
        (
            "import bpy\nbpy.ops.sequencer.sound_strip_add(filepath='/usr/share/input.wav')",
            "closed replay vocabulary",
        ),
        (
            "import bpy\nbpy.ops.preferences.addon_install(filepath='/usr/share/input.py')",
            "closed replay vocabulary",
        ),
        (
            "import bpy\nt=bpy.data.texts.new('payload')\n"
            "t.from_string(\"import bpy\\nbpy.data.images.load('/usr/share/input.png')\")\n"
            "t.as_module()",
            "as_module",
        ),
        (
            "import bpy\ndef f(m):\n"
            "    m.ops.preferences.addon_install(filepath='/usr/share/x.py')\n"
            "f(bpy)",
            "cannot escape",
        ),
        (
            "import bpy\nmods=[bpy]\n"
            "mods[0].ops.preferences.addon_install(filepath='/usr/share/x.py')",
            "cannot escape",
        ),
        (
            "import bpy\ndef f(m):\n"
            "    m.data.texts.load('/etc/hosts')\n"
            "f(bpy)",
            "cannot escape",
        ),
        (
            "import bpy\nmods=[bpy]\nmods[0].data.texts.load('/etc/hosts')",
            "cannot escape",
        ),
        (
            "import bpy\nbpy.context.blend_data.texts.load('/etc/hosts')",
            "external data load",
        ),
        (
            "import bpy\nobj=bpy.data.objects.get('hero')\n"
            "d=obj.driver_add('hide_viewport').driver\n"
            "d.expression=\"open('/etc/hosts').read() and 0\"",
            "deferred dynamic-code target",
        ),
        ("().__class__.__mro__", "__class__|__mro__"),
        ("exec('RESULT = 1')", "exec"),
    ],
)
def test_artifact_policy_refuses_filesystem_and_dynamic_code(
    source: str,
    capability: str,
) -> None:
    with pytest.raises(ArtifactExecutionPolicyError, match=capability):
        validate_artifact_source(source)


def test_artifact_policy_accepts_scene_delta_vocabulary() -> None:
    validate_artifact_source(
        """
import bpy, bmesh, json, math
from mathutils import Matrix, Vector

def place(name, location):
    obj = bpy.data.objects.get(name)
    if obj is not None:
        obj.location = Vector(location)
    return obj

RESULT = json.dumps({"angle": round(math.pi, 4), "placed": bool(place("hero", (1, 2, 3)))})
"""
    )


def test_artifact_policy_keeps_in_memory_collection_link_and_append_legal() -> None:
    validate_artifact_source(
        """
import bpy
mesh = bpy.data.meshes.new("hero")
obj = bpy.data.objects.new("hero", mesh)
bpy.context.scene.collection.objects.link(obj)
obj.data.materials.append(bpy.data.materials.new("hero-material"))
"""
    )


def test_artifact_policy_tracks_safe_bpy_and_json_aliases() -> None:
    validate_artifact_source(
        """
import bpy as b
import json as j
meshes = b.data.meshes
mesh = meshes.new("hero")
obj = b.data.objects.new("hero", mesh)
b.context.scene.collection.objects.link(obj)
mesh.materials.append(b.data.materials.new("hero-material"))
operators = b.ops.mesh
operators.primitive_cube_add()
RESULT = j.dumps({"ok": True})
"""
    )


def test_artifact_policy_accepts_exact_current_frame_reevaluation_barrier() -> None:
    validate_artifact_source(_ARTIFACT_EVALUATION_BARRIER)
    validate_artifact_source(
        "import bpy\n"
        "bpy.context.scene.frame_set(int(bpy.context.scene.frame_current))\n"
    )


@pytest.mark.parametrize(
    "source",
    [
        (
            "import bpy\n"
            "scene = bpy.context.scene\n"
            "escaped = (scene.frame_current,)\n"
        ),
        (
            "import bpy\n"
            "scene = bpy.context.scene\n"
            "escaped = int(scene.frame_current)\n"
        ),
        (
            "import bpy\n"
            "scene = bpy.context.scene\n"
            "scene.frame_set(int(scene.frame_current).bit_length())\n"
        ),
        (
            "import bpy\n"
            "scene = bpy.context.scene\n"
            "scene.frame_set(int(scene.frame_end))\n"
        ),
        (
            "import bpy\n"
            "scene = bpy.context.scene\n"
            "print(int(scene.frame_current))\n"
        ),
    ],
)
def test_current_frame_exception_cannot_escape_exact_reevaluation_call(
    source: str,
) -> None:
    with pytest.raises(ArtifactExecutionPolicyError, match="capability cannot escape"):
        validate_artifact_source(source)


def test_rejected_artifact_cannot_overwrite_authoritative_script(tmp_path: Path) -> None:
    authoritative = tmp_path / "build" / "units" / "1" / "hero.py"
    authoritative.parent.mkdir(parents=True)
    authoritative.write_text("accepted\n", encoding="utf-8")
    source = f"open({str(authoritative)!r}, 'w').write('stale')"

    with pytest.raises(ArtifactExecutionPolicyError, match=r"open|write"):
        validate_artifact_source(source)

    assert authoritative.read_text(encoding="utf-8") == "accepted\n"


def test_artifact_worker_builtins_exclude_mutation_escape_hatches() -> None:
    allowed = artifact_builtins()
    assert "open" not in allowed
    assert "exec" not in allowed
    assert "eval" not in allowed
    with pytest.raises(TypeError):
        allowed["open"] = open  # type: ignore[index]


def test_session_forwards_explicit_artifact_policy(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_call(self, command, **kwargs):
        calls.append((command, kwargs))
        return {"ok": True}

    monkeypatch.setattr(BlenderSession, "call", fake_call)
    session = BlenderSession(artifacts_dir=tmp_path)
    session.run("import bpy", journal=False, execution_policy="artifact")

    assert calls == [
        (
            "run",
            {
                "code": "import bpy",
                "journal": False,
                "transactional": False,
                "execution_policy": "artifact",
            },
        )
    ]


def test_canonical_reset_clears_persistent_handlers_before_factory_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vfx_harness.agents.builder.models import _RESET

    events: list[str] = []

    def retained_callback(*_args) -> None:
        events.append("retained callback ran")

    retained_callback._bpy_persistent = True  # type: ignore[attr-defined]
    handlers = types.SimpleNamespace(
        frame_change_post=[retained_callback],
        load_post=[retained_callback],
        persistent=lambda callback: callback,
    )

    def read_factory_settings(*, use_empty: bool) -> None:
        assert use_empty is True
        for callback in tuple(handlers.load_post):
            callback(None)

    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace(handlers=handlers)
    bpy.ops = types.SimpleNamespace(
        wm=types.SimpleNamespace(read_factory_settings=read_factory_settings)
    )
    monkeypatch.setitem(sys.modules, "bpy", bpy)

    exec(_RESET, {})
    for callback in tuple(handlers.frame_change_post):
        callback(None)

    assert handlers.frame_change_post == []
    assert handlers.load_post == []
    assert events == []


def test_worker_os_confinement_mounts_only_run_roots_writable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scratch = tmp_path / "runs" / "r1" / "scratch"
    scratch.mkdir(parents=True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)

    with _shot_confined_command(
        ["/usr/bin/blender", "--background"],
        shot=tmp_path,
        writable=scratch,
    ) as (argv, _descriptors):
        pass

    assert argv[:2] == ["/usr/bin/bwrap", "--die-with-parent"]
    assert "--unshare-pid" in argv
    assert "--unshare-ipc" in argv
    assert "--unshare-uts" in argv
    assert ["--ro-bind", "/", "/"] not in [
        argv[index : index + 3] for index in range(len(argv) - 2)
    ]
    assert "--clearenv" in argv
    assert ["--tmpfs", str(tmp_path)] in [
        argv[index : index + 2] for index in range(len(argv) - 1)
    ]
    binds = [
        tuple(argv[index + 1 : index + 3])
        for index, value in enumerate(argv)
        if value == "--bind-fd"
    ]
    assert len(binds) == 1
    assert binds[0][1] == str(scratch)
    assert str(tmp_path / "build") not in argv


def test_worker_confinement_refuses_symlink_writable_root(tmp_path: Path) -> None:
    build = tmp_path / "build"
    build.mkdir()
    scratch = tmp_path / "runs" / "r1" / "scratch"
    scratch.mkdir(parents=True)
    escape = scratch / "blender"
    escape.symlink_to(build, target_is_directory=True)

    runtime_descriptor = open_real_directory(
        HARNESS_RUNTIME_ROOT,
        "test harness runtime",
    )
    try:
        with pytest.raises(Exception, match="symlink component"):
            confined_worker_argv(
                ["/usr/bin/blender", "--background"],
                writable_roots=(escape,),
                writable_root_descriptors=(runtime_descriptor,),
                runtime_root_descriptor=runtime_descriptor,
                authority_root=tmp_path,
                current_run_root=tmp_path / "runs" / "r1",
            )
    finally:
        os.close(runtime_descriptor)


def test_real_confined_process_cannot_overwrite_authority(tmp_path: Path) -> None:
    authority = tmp_path / "build" / "units" / "1" / "hero.py"
    authority.parent.mkdir(parents=True)
    authority.write_text("accepted\n", encoding="utf-8")
    scratch = tmp_path / "runs" / "r1" / "scratch"
    scratch.mkdir(parents=True)
    staged = scratch / "candidate.py"
    source = f"""
from pathlib import Path
authority = Path({str(authority)!r})
staged = Path({str(staged)!r})
try:
    authority.write_text('stale')
except OSError:
    pass
else:
    raise AssertionError('authority mount was writable')
staged.write_text('candidate')
print('confined')
"""
    argv = confined_worker_argv(
        [sys.executable, "-c", source],
        writable_roots=(scratch,),
    )
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "confined"
    assert authority.read_text(encoding="utf-8") == "accepted\n"
    assert staged.read_text(encoding="utf-8") == "candidate"


def test_real_confined_process_cannot_overwrite_checkpoint(tmp_path: Path) -> None:
    checkpoint = tmp_path / "runs" / "r1" / "checkpoints" / "sealed.blend"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"sealed")
    scratch = tmp_path / "runs" / "r1" / "scratch"
    scratch.mkdir(parents=True)
    source = f"""
from pathlib import Path
target = Path({str(checkpoint)!r})
try:
    target.write_bytes(b'stale')
except OSError:
    print('confined')
else:
    raise AssertionError('checkpoint mount was writable')
"""
    with _shot_confined_command(
        ["/usr/bin/python3", "-c", source],
        shot=tmp_path,
        writable=scratch,
    ) as (argv, descriptors):
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            pass_fds=descriptors,
        )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "confined"
    assert checkpoint.read_bytes() == b"sealed"


def test_shot_worker_sees_only_declared_inputs_and_active_blender_scratch(
    tmp_path: Path,
) -> None:
    refs = tmp_path / "refs"
    refs.mkdir()
    declared = refs / "declared.txt"
    declared.write_text("declared", encoding="utf-8")
    build_script = tmp_path / "build" / "units" / "1" / "unit.py"
    build_script.parent.mkdir(parents=True)
    build_script.write_text("authority", encoding="utf-8")
    old_secret = tmp_path / "runs" / "old" / "scratch" / "secret.txt"
    old_secret.parent.mkdir(parents=True)
    old_secret.write_text("old", encoding="utf-8")
    active = tmp_path / "runs" / "r1" / "scratch" / "blender"
    active.mkdir(parents=True)
    sibling = active.parent / "sibling.txt"
    sibling.write_text("sibling", encoding="utf-8")
    undeclared_host_input = Path(__file__).resolve()
    output = active / "output.txt"
    source = f"""
import os
from pathlib import Path
assert Path({str(declared)!r}).read_text() == 'declared'
for hidden in (
    Path({str(build_script)!r}),
    Path({str(old_secret)!r}),
    Path({str(sibling)!r}),
    Path({str(undeclared_host_input)!r}),
):
    assert not hidden.exists(), hidden
assert 'VFXH_CONFINEMENT_CANARY' not in os.environ
Path({str(output)!r}).write_text('active')
try:
    Path({str(declared)!r}).write_text('changed')
except OSError:
    pass
else:
    raise AssertionError('declared input was writable')
print('confined')
"""
    with _shot_confined_command(
        ["/usr/bin/python3", "-c", source],
        shot=tmp_path,
        writable=active,
        readable=(refs,),
    ) as (argv, descriptors):
        environment = dict(os.environ)
        environment["VFXH_CONFINEMENT_CANARY"] = "must-not-cross"
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            pass_fds=descriptors,
            env=environment,
        )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "confined"
    assert output.read_text(encoding="utf-8") == "active"
    assert declared.read_text(encoding="utf-8") == "declared"


def test_final_replay_snapshot_is_read_only_and_sibling_scratch_is_hidden(
    tmp_path: Path,
) -> None:
    replay = tmp_path / "runs" / "r1" / "scratch" / "final-render-chain-abc"
    replay.mkdir(parents=True)
    replay_script = replay / "layer.py"
    replay_script.write_text("snapshot", encoding="utf-8")
    active = tmp_path / "runs" / "r1" / "scratch" / "blender"
    active.mkdir(parents=True)
    sibling = active.parent / "candidate.py"
    sibling.write_text("candidate", encoding="utf-8")
    output = active / "render.txt"
    source = f"""
from pathlib import Path
replay = Path({str(replay_script)!r})
assert replay.read_text() == 'snapshot'
assert not Path({str(sibling)!r}).exists()
try:
    replay.write_text('changed')
except OSError:
    pass
else:
    raise AssertionError('replay snapshot was writable')
Path({str(output)!r}).write_text('rendered')
print('confined')
"""
    with _shot_confined_command(
        ["/usr/bin/python3", "-c", source],
        shot=tmp_path,
        writable=active,
        readable=(replay,),
    ) as (argv, descriptors):
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            pass_fds=descriptors,
        )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "confined"
    assert replay_script.read_text(encoding="utf-8") == "snapshot"
    assert output.read_text(encoding="utf-8") == "rendered"


def test_worker_writable_bind_is_pinned_before_path_substitution(tmp_path: Path) -> None:
    active = tmp_path / "runs" / "r1" / "scratch" / "blender"
    active.mkdir(parents=True)
    original = active.with_name("blender-original")
    output = active / "output.txt"
    source = f"""
from pathlib import Path
Path({str(output)!r}).write_text('pinned')
print('confined')
"""
    with _shot_confined_command(
        ["/usr/bin/python3", "-c", source],
        shot=tmp_path,
        writable=active,
    ) as (argv, descriptors):
        active.rename(original)
        active.mkdir()
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            pass_fds=descriptors,
        )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "confined"
    assert (original / "output.txt").read_text(encoding="utf-8") == "pinned"
    assert not output.exists()


def test_construction_namespace_creation_is_narrow_and_symlink_safe(
    tmp_path: Path,
) -> None:
    (tmp_path / "build").mkdir()
    namespace = ensure_construction_read_namespace(tmp_path)
    assert namespace == tmp_path / "build" / "construction"
    assert namespace.is_dir()
    assert ensure_construction_read_namespace(tmp_path) == namespace

    namespace.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    namespace.symlink_to(outside, target_is_directory=True)
    with pytest.raises(Exception, match="real non-symlink"):
        ensure_construction_read_namespace(tmp_path)
    assert list(outside.iterdir()) == []


def test_first_construction_promotion_is_visible_through_prelaunched_namespace(
    tmp_path: Path,
) -> None:
    (tmp_path / "build").mkdir()
    construction = ensure_construction_read_namespace(tmp_path)
    active = tmp_path / "runs" / "r1" / "scratch" / "blender"
    active.mkdir(parents=True)
    promoted = construction / "first.glb"
    ready = active / "ready"
    source = f"""
import time
from pathlib import Path
Path({str(ready)!r}).write_text('ready')
for _ in range(250):
    if Path({str(promoted)!r}).exists():
        break
    time.sleep(0.02)
assert Path({str(promoted)!r}).read_bytes() == b'glTF-first'
print('visible')
"""
    with prepared_worker_command(
        ["/usr/bin/python3", "-c", source],
        writable_roots=(active,),
        authority_root=tmp_path,
        current_run_root=tmp_path / "runs" / "r1",
    ) as command:
        process = subprocess.Popen(
            command.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=command.pass_fds,
        )
        # The confined process is already live, exactly as Blender is before a
        # generate-route unit promotes its first CAS object.
        for _ in range(250):
            if ready.is_file():
                break
            time.sleep(0.02)
        assert ready.is_file()
        promoted.write_bytes(b"glTF-first")
        stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 0, stderr
    assert stdout.strip() == "visible"


def test_worker_seccomp_denies_network_and_process_execution() -> None:
    source = """
import errno, os, socket, threading
from vfx_harness.blender.process_confinement import install_worker_seccomp
go = threading.Event()
result = []
def preexisting_thread():
    go.wait()
    try:
        os.execve('/bin/true', ['/bin/true'], {})
    except OSError as exc:
        result.append(exc.errno)
thread = threading.Thread(target=preexisting_thread)
thread.start()
install_worker_seccomp()
go.set()
thread.join()
assert result == [errno.EPERM], result
for operation in (
    lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
    lambda: os.execve('/bin/true', ['/bin/true'], {}),
):
    try:
        operation()
    except OSError as exc:
        assert exc.errno == errno.EPERM, exc
    else:
        raise AssertionError('confinement operation unexpectedly succeeded')
print('confined')
"""
    completed = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "confined"


def test_alias_escape_rejection_names_line_capability_and_legal_forms() -> None:
    source = (
        "import bpy\n"
        "bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))\n"
        "obj = bpy.context.active_object\n"
        'obj.name = "aim_target"\n'
        'bvfx_role(obj, "camera.aim", owner_layer="1")\n'
    )
    with pytest.raises(ArtifactExecutionPolicyError) as exc:
        validate_artifact_source(source)
    message = str(exc.value)
    assert "cannot escape a tracked attribute or simple alias" in message
    assert "line 5 uses `obj`" in message
    assert "bpy.context.active_object" in message
    assert "bpy.data.objects.new(...)" in message
    assert "bvfx_* helper return value" in message


def test_a_denied_import_is_reported_with_every_other_violation() -> None:
    """HIR-0225: a refused import must not short-circuit the collector HIR-0216 built.

    `artifact_violations` calls `_artifact_bindings` on its first line. That function
    raised on the first denied import, so one `import itertools` on journal line 123
    returned no violations at all and took the process down — strictly worse than the
    report-only-the-first behaviour HIR-0216 replaced. A layer that had already passed
    11/11 owned contracts and 7/7 bound checks was discarded at write time.
    """
    import ast

    from vfx_harness.blender.artifact_execution import artifact_violations

    source = (
        "import bpy\n"
        "import itertools\n"
        "signs = list(itertools.product([-1, 1], repeat=3))\n"
        "from os import path\n"
        "import bpy.ops\n"
    )
    violations = artifact_violations(source, ast.parse(source))
    messages = [message for _line, message in violations]

    assert "artifact import denied: itertools" in messages
    assert "artifact import denied: os" in messages
    assert any("bpy submodule" in message for message in messages)
    # Reported together, in source order, so one edit can fix all of them.
    assert [line for line, _ in violations] == sorted(line for line, _ in violations)
    assert [line for line, _ in violations] == [2, 4, 5]


def test_every_import_refusal_is_recorded_rather_than_raised() -> None:
    """All six raise sites convert; none of them may abort the walk."""
    import ast

    from vfx_harness.blender.artifact_execution import artifact_violations

    for source, expected in (
        ("import itertools\n", "artifact import denied: itertools"),
        ("import bpy.ops\n", "bpy submodule"),
        ("from . import thing\n", "relative artifact imports are denied"),
        ("from os import path\n", "artifact import denied: os"),
        ("from bpy import ops\n", "from-bpy imports are denied"),
        ("from math import *\n", "wildcard imports are denied"),
    ):
        violations = artifact_violations(source, ast.parse(source))
        assert violations, source
        assert any(expected in message for _line, message in violations), source


def test_recording_an_import_still_admits_nothing() -> None:
    """The policy is unchanged: the execution boundary refuses on any violation."""
    for source in (
        "import itertools\n",
        "import bpy.ops\n",
        "from . import thing\n",
        "from bpy import ops\n",
        "from math import *\n",
    ):
        with pytest.raises(ArtifactExecutionPolicyError):
            validate_artifact_source(source)

    validate_artifact_source("import bpy\nbpy.ops.mesh.primitive_cube_add()\n")


def test_a_denied_import_and_a_capability_violation_report_together() -> None:
    """The case that motivated converting all six rather than only the first.

    A journal carrying a refused import and a refused capability used to report the
    import by dying; the capability was never reached.
    """
    import ast

    from vfx_harness.blender.artifact_execution import artifact_violations

    source = "import itertools\nimport bpy\nexec('x = 1')\n"
    messages = [message for _line, message in artifact_violations(source, ast.parse(source))]

    assert any("import denied: itertools" in message for message in messages)
    assert len(messages) >= 2, messages
