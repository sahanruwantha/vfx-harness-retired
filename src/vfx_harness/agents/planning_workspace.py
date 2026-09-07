"""One exact VFX planning workspace shared by native role capabilities."""

from __future__ import annotations

from collections.abc import Callable

from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration import authority_selection, plan_authoring, plan_inputs
from vfx_harness.orchestration.authority_selection_transaction import (
    AUTHORITY_SELECTION_LOCK,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.plan_bundle_integrity import (
    digest,
    read_real_file,
    regular_files_under,
    require_real_directory,
)


class PlanningWorkspace:
    def __init__(self, layout: RunLayout, check_current: Callable[[], None]):
        self.layout = layout
        self.root = layout.scratch / "plan-workspace"
        self.marker = self.root / ".plan-workspace.json"
        self.marker_bytes = read_real_file(layout.shot, self.marker, "native planning workspace marker")
        self.record = plan_inputs.read_workspace_marker(layout.shot, self.marker)
        if self.record["run_id"] != layout.run_id or self.record["shot"] != str(layout.shot):
            raise ValueError("native planning workspace must belong to the exact current shot and run")
        self.base = plan_inputs.workspace_base_selection(self.record)
        self._owner_check = check_current
        self.check_inputs()
        self.owned = self.outputs()

    def outputs(self):
        identities = {}
        for name in ("ownership_mapping.json", *plan_authoring.MAPPING_ARTIFACTS):
            path = self.root / name
            if path.parent.exists() or path.parent.is_symlink():
                require_real_directory(self.layout.shot, path.parent, "native mapping output parent")
            if path.exists() and path.stat().st_nlink != 1:
                raise ValueError("native mapping output must not share a hard-linked inode")
            identities[name] = (
                digest(read_real_file(self.layout.shot, path, "native mapping output"))
                if path.exists() or path.is_symlink() else None
            )
        return identities

    def check_inputs(self):
        self._owner_check()
        if read_real_file(self.layout.shot, self.marker, "native planning workspace marker") != self.marker_bytes:
            raise ValueError("native planning workspace marker changed; start a new bound attempt")
        current = authority_selection.resolve_selected_authority(self.layout.shot)
        require_matching_authority_selection_token(self.base, current.selection_token)
        for root in (self.layout.shot, self.root):
            plan_inputs.require_exact_planning_input_identity(
                root, expected_authored_inputs=self.record["authored_inputs"],
                expected_decision_inputs=self.record["decision_inputs"], where="native planning workspace",
            )

    def check(self):
        self.check_inputs()
        if self.outputs() != self.owned:
            raise ValueError("native planning output changed outside this attempt; preserve the newer draft")

    def identity(self):
        return {
            "workspace": str(self.root.relative_to(self.layout.root)),
            "base_selection": self.base.to_dict(), "workspace_marker_sha256": digest(self.marker_bytes),
            "artifacts": dict(self.owned),
        }

    def require_closed_tree(self):
        # The evaluator may acquire its normal selection fence. That permanent
        # lock is synchronization metadata, not another plan input or pointer.
        allowed = {".plan-workspace.json", AUTHORITY_SELECTION_LOCK.as_posix(), *self.record["authored_inputs"],
                   *self.record["decision_inputs"], *self.owned}
        actual = {
            str(path.relative_to(self.root))
            for path in regular_files_under(self.layout.shot, self.root, "native gate workspace")
        }
        if extra := actual - allowed:
            raise ValueError("native gate workspace has unbound inputs: " + ", ".join(sorted(extra)))
