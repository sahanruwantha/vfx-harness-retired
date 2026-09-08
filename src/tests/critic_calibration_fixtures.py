"""Offline, real-journal calibration fixtures; these do not qualify a live model."""

import hashlib
import json
from dataclasses import asdict, replace

import flynn_agents_sdk as flynn
from PIL import Image

from tests.contract.test_flynn_critic_transport import response, verdict
from vfx_harness.evaluation import critic_calibration
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import critic_qualification_publication as publication


def measured_artifact(bound, claim, expected, monkeypatch, invoke):
    with monkeypatch.context() as context:
        context.setenv(run_artifacts.ENV, str(bound.root))
        layout = run_artifacts.create(bound.shot, "measured-qualification-fixture")
        prefix = layout.scratch.relative_to(bound.shot)
        for name in ("reference.png", "focus.png"):
            (bound.shot / prefix / name).write_bytes((bound.shot / name).read_bytes())
        cases = []
        labels = {"known_good": "pass", "identical": "pass", "defective": "fail",
                  "near_threshold": "fail", "irrelevant": "pass", "unjudgeable": "unjudgeable"}
        profile = None
        for index, (control, expected_label) in enumerate(labels.items()):
            candidate = prefix / f"{control}.png"
            if control == "identical":
                (bound.shot / candidate).write_bytes((bound.shot / prefix / "reference.png").read_bytes())
            else:
                Image.new("RGB", (4, 4), (index * 30, 0, 200)).save(bound.shot / candidate)
            rows = []
            for _ in range(2):
                value = verdict()
                value["scores"]["form"] = 4 if expected_label == "pass" else 2
                value["reference_usable"] = expected_label != "unjudgeable"
                if expected_label == "fail":
                    value["observations"] = [{
                        "id": "form-miss", "kind": "qualitative", "axis": claim.axis, "property": claim.property,
                        "observation": "Form differs", "action": "Correct form", "moment": 7,
                        "roles": list(claim.subject_roles), "claim_id": claim.id,
                        "check_ids": list(claim.binding_ids), "panel_ids": [],
                    }]
                result = invoke(layout, claim, lambda _, value=value: response(value), qualification_claims=(),
                                calibration_claims=(replace(claim, qualification=None),),
                                images=(("reference", (prefix / "reference.png").as_posix()),
                                        ("candidate", candidate.as_posix()),
                                        ("focus", (prefix / "focus.png").as_posix())))
                report = json.loads((bound.shot / result["report"]).read_text())
                profile = report["calibration_check"]["profile"]
                assert critic_calibration.fingerprint(profile) == expected["native_invocation_sha256"]
                rows.append(critic_calibration.Trial(
                    layout.root.relative_to(bound.shot).as_posix(), result["report"], result["report_sha256"],
                    critic_calibration.fingerprint(flynn.SQLiteRun.inspect(layout.root / report["journal"])),
                ))
            cases.append(critic_calibration.Case(control, control, expected_label, tuple(rows),
                                                "known_good" if control == "irrelevant" else None))
        request = {"schema": publication.SUITE_SCHEMA, "suite": expected["suite"], "claim_id": claim.id,
                   "profile": profile, "cases": [asdict(case) for case in cases], "budgets": expected["budgets"]}
        path = layout.write_report("selected-calibration-suite", request)
        selected = {"path": path.relative_to(bound.shot).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        published = publication.publish(bound.shot, request=selected, check_current=lambda: None)
        return json.loads((bound.shot / published["path"]).read_text())
