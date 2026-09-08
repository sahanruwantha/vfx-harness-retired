"""Read-only measurement of labeled native critic trials; never qualification publication."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path

import flynn_agents_sdk as flynn
from jsonschema import Draft202012Validator
from PIL import Image

from vfx_harness.domain.critic_prompt import CriticPrompt, protocol_schema_digest
from vfx_harness.domain.critic_verdict import critic_verdict_schema
from vfx_harness.orchestration.plan_bundle_integrity import read_real_file

RATES = ("false_pass_rate", "false_failure_rate", "repeatability_failure_rate",
         "scope_leakage_rate", "irrelevant_change_sensitivity_rate")
CONTROLS = frozenset({"known_good", "identical", "defective", "near_threshold", "irrelevant", "unjudgeable"})


def fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise ValueError(f"critic calibration {detail}; repair the labeled suite or rerun its trials")


@dataclass(frozen=True)
class Trial:
    run_root: str
    report: str
    report_sha256: str
    journal_records_sha256: str


@dataclass(frozen=True)
class Case:
    id: str
    control: str
    expected: str
    trials: tuple[Trial, ...]
    baseline: str | None = None


def _read_trial(root: Path, trial: Trial, invocation: str, claim_id: str) -> dict:
    run_root = root / trial.run_root
    report_path = root / trial.report
    _require(report_path.is_relative_to(run_root), "report is outside its declared run")
    payload = read_real_file(root, report_path, "critic calibration report")
    _require(hashlib.sha256(payload).hexdigest() == trial.report_sha256, "report digest differs")
    report = json.loads(payload)
    _require(report.get("schema") == "vfx-harness.critic-observation/v2", "report schema is unsupported")
    _require(report.get("qualification_verified") is False and report.get("acceptance_authorized") is False,
             "trial asserts qualification or acceptance")
    _require(report.get("inputs_validated_after_inference") is True, "trial did not complete input validation")
    check = report.get("calibration_check", {})
    _require(check.get("status") == "matched_dispatched_configuration", "trial has no verified configuration")
    _require(report["inputs"].get("schema") == "vfx-harness.critic-inputs/v2",
             "input schema is unsupported")
    profile = check["profile"]
    _require(profile.get("schema") == "vfx-harness.critic-invocation/v2" and
             profile.get("context_schema_sha256") == protocol_schema_digest(),
             "rubric/observation protocol is unsupported")
    _require(check.get("native_invocation_sha256") == invocation == fingerprint(profile), "invocation differs")
    claims = [claim for claim in profile["scope"]["claims"] if claim["id"] == claim_id]
    _require(len(claims) == 1, "target claim is missing or ambiguous")
    claim = claims[0]
    journal_path = run_root / report["journal"]
    _require(journal_path.is_relative_to(run_root), "journal is outside its declared run")
    before = read_real_file(root, journal_path, "critic calibration journal")
    records = flynn.SQLiteRun.inspect(journal_path)
    _require(read_real_file(root, journal_path, "critic calibration journal") == before, "journal changed during read")
    _require(fingerprint(records) == trial.journal_records_sha256, "journal snapshot digest differs")
    _require(len(records["run"]) == len(records["operations"]) == len(records["inference_usage"]) == 1,
             "trial must contain exactly one model operation")
    _require(not records["commits"], "trial committed state")
    operation = records["operations"][0]
    _require(operation["stage"] == "completed", "model operation is incomplete")
    _require(records["run"][0]["outcome"] == report["termination"] and
             flynn.SessionTermination.from_json(report["termination"]).kind == "stopped",
             "trial is not durably terminal")
    usage_row = records["inference_usage"][0]
    usage = json.loads(usage_row["payload"])
    configuration = flynn.InferenceConfiguration(**profile["configuration"])
    _require(usage_row["operation_id"] == operation["id"] and usage["kind"] == "model",
             "usage is not owned by this model operation")
    _require((usage["provider"], usage["model"], usage.get("response_model")) ==
             (configuration.provider, configuration.model, configuration.model), "recorded model identity differs")
    _require(usage.get("configuration_sha256") == check["configuration_sha256"] == configuration.sha256,
             "dispatched configuration differs")
    verdict = json.loads(flynn.ToolResult.from_json(operation["output"]).data_json)
    call = json.loads(operation["call"])
    _require(call["name"] == "submit_verdict" and json.loads(call["arguments"]) == verdict,
             "recorded verdict differs from submitted tool call")
    scope = profile["scope"]
    schema = critic_verdict_schema(scope["axes"], allow_na=scope["allow_na"], focus_frames=scope["frames"])
    Draft202012Validator(schema).validate(verdict)
    sources = report["inputs"]["sources"]
    request = json.loads(operation["request"])
    suffix = ("\n\n[critic-scope]\n" + json.dumps(scope, sort_keys=True) +
              "\n\n[image-order]\n" + json.dumps(sources, sort_keys=True))
    objective = request["objective"]
    _require(objective.startswith("[critic-rubric]\n") and objective.endswith(suffix),
             "recorded context differs from scope or image manifest")
    content = objective[len("[critic-rubric]\n"):-len(suffix)]
    rubric, separator, observations = content.rpartition("\n\n[critic-observation]\n")
    _require(bool(separator), "recorded observation section is missing")
    prompt = CriticPrompt(rubric, observations, json.dumps(scope["authority"], sort_keys=True))
    prompt.validate_images(tuple((source["role"], source["path"]) for source in sources))
    _require(prompt.observation_json == observations and
             hashlib.sha256(rubric.encode()).hexdigest() == profile["prompt_sha256"] and
             hashlib.sha256(observations.encode()).hexdigest() == report["inputs"]["observation_sha256"] and
             report["inputs"]["context_schema_sha256"] == protocol_schema_digest() and
             hashlib.sha256(objective.encode()).hexdigest() == report["inputs"]["context_sha256"],
             "recorded rubric or observations differ")
    _require(request["base"] == {"revision": 0, "value": profile["accepted_state"]} and
             request["observation"] is None, "trial has unexpected prior state or observation")
    _require(request["allowed_tools"] == ["submit_verdict"] and len(request["tools"]) == 1,
             "trial has unexpected tool permissions")
    tool = request["tools"][0]
    schema_digest = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
    _require(tool["name"] == profile["tool"]["name"] and tool["description"] == profile["tool"]["description"] and
             json.loads(tool["parameters_json"]) == schema and
             schema_digest == profile["response_schema_sha256"],
             "recorded response tool differs")
    _require(len(sources) == len(request["images"]) == len(profile["images"]), "recorded image count differs")
    identities = []
    for source, attached, shape in zip(sources, request["images"], profile["images"], strict=True):
        image = read_real_file(root, root / source["path"], "critic calibration image")
        _require(hashlib.sha256(image).hexdigest() == source["sha256"], "image bytes differ")
        url = attached["url"]
        _require(hashlib.sha256(url.encode()).hexdigest() == source["input_sha256"] and
                 base64.b64decode(url.split(",", 1)[1], validate=True) == image,
                 "recorded image payload differs")
        with Image.open(io.BytesIO(image)) as decoded:
            actual_shape = {"role": source["role"], "label": source["label"], "detail": attached["detail"],
                            "mime": url.split(";", 1)[0], "size": list(decoded.size), "mode": decoded.mode,
                            "frame_count": getattr(decoded, "n_frames", 1)}
        _require(actual_shape == shape, "recorded image representation differs")
        identities.append((source["role"], source["sha256"]))
    score = verdict["scores"][claim["axis"]]
    decision = ("unjudgeable" if not verdict["reference_usable"] or verdict["focus_requests"] or score == "n/a"
                else "pass" if score >= 3 else "fail")
    allowed = {item["id"]: item for item in scope["claims"]}
    leaks, cited_target = False, False
    for observation in verdict["observations"]:
        owner = allowed.get(observation["claim_id"])
        if (owner is None or observation["axis"] != owner["axis"] or
                observation["property"] != owner["property"] or
                observation["moment"] not in owner["moments"] or
                not set(observation["roles"]).issubset(owner["subject_roles"]) or
                not set(observation["check_ids"]).issubset(row["id"] for row in owner["evidence"])):
            leaks = True
        elif observation["claim_id"] == claim_id:
            cited_target = True
    return {"decision": decision, "score": score, "reference_usable": verdict["reference_usable"],
            "citation_error": decision == "fail" and not cited_target,
            "scope_leak": leaks, "images": identities,
            "run_id": records["run"][0]["id"], "source": asdict(trial)}


def _judgment(row: dict) -> tuple:
    """Ignore free-text paraphrases, but retain score changes within the same pass/fail band."""
    return row["decision"], row["score"], row["reference_usable"], row["scope_leak"]


def evaluate(root: Path, *, suite: str, claim_id: str, invocation: str,
             cases: tuple[Case, ...], budgets: dict[str, float]) -> dict:
    """Labels/budgets are authored inputs. Reopen trials and derive counts, never accept a supplied rate."""
    _require(bool(suite.strip()) and bool(claim_id.strip()), "suite and claim must be named")
    _require(set(budgets) == set(RATES), "budget keys must name all five rates exactly")
    _require(all(type(value) in (int, float) and 0 <= value <= 1 for value in budgets.values()),
             "budgets must be finite rates in [0,1]")
    _require(bool(cases) and len({case.id for case in cases}) == len(cases), "case ids must be distinct")
    _require({case.control for case in cases} == CONTROLS, "suite must cover all six control types")
    trials, run_ids = {}, set()
    for case in cases:
        _require(bool(case.id.strip()) and case.expected in {"pass", "fail", "unjudgeable"}, "case label is invalid")
        required = {"known_good": "pass", "identical": "pass", "defective": "fail", "unjudgeable": "unjudgeable"}
        _require(case.control not in required or case.expected == required[case.control],
                 "control label contradicts its type")
        _require(case.control not in {"near_threshold", "irrelevant"} or case.expected in {"pass", "fail"},
                 "judgeable control needs a pass/fail label")
        _require(len(case.trials) >= 2, "every case requires repeated trials")
        rows = [_read_trial(root, trial, invocation, claim_id) for trial in case.trials]
        for row in rows:
            _require(row["run_id"] not in run_ids, "one model operation was counted more than once")
            run_ids.add(row["run_id"])
            _require(row["images"] == rows[0]["images"], "repeat trials use different images")
        if case.control == "identical":
            images = dict(rows[0]["images"])
            _require(images.get("reference") == images.get("candidate"), "identical control images differ")
        _require(case.control == "irrelevant" or case.baseline is None,
                 "only irrelevant controls may select a baseline")
        trials[case.id] = rows
    counts = {rate: [0, 0] for rate in RATES}

    def count(rate: str, failed: bool) -> None:
        counts[rate][0] += int(failed)
        counts[rate][1] += 1

    by_id = {case.id: case for case in cases}
    for case in cases:
        rows = trials[case.id]
        for row in rows:
            if case.expected == "pass":
                count("false_failure_rate", row["decision"] != "pass")
            else:
                count("false_pass_rate", row["decision"] == "pass")
            count("scope_leakage_rate", row["scope_leak"])
        for left, right in combinations(rows, 2):
            count("repeatability_failure_rate", _judgment(left) != _judgment(right))
        if case.control == "irrelevant":
            baseline = by_id.get(case.baseline)
            _require(baseline is not None and baseline.control not in {"irrelevant", "unjudgeable"} and
                     baseline.expected == case.expected, "irrelevant control needs a matching labeled baseline")
            base_rows = trials[baseline.id]
            _require(rows[0]["images"] != base_rows[0]["images"], "irrelevant control must change image evidence")
            _require([item for item in rows[0]["images"] if item[0] != "candidate"] ==
                     [item for item in base_rows[0]["images"] if item[0] != "candidate"],
                     "irrelevant control changed evidence outside the candidate")
            for left in rows:
                for right in base_rows:
                    count("irrelevant_change_sensitivity_rate", _judgment(left) != _judgment(right))
    _require(all(total for _errors, total in counts.values()), "a required metric has no observations")
    metrics = {rate: errors / total for rate, (errors, total) in counts.items()}
    reference_errors = sum(row["reference_usable"] is not False for case in cases if case.control == "unjudgeable"
                           for row in trials[case.id])
    citation_errors = sum(row["citation_error"] for rows in trials.values() for row in rows)
    return {"schema": "vfx-harness.critic-calibration-evaluation/v1", "suite": suite, "claim_id": claim_id,
            "native_invocation_sha256": invocation, "labels_sha256": fingerprint([asdict(case) for case in cases]),
            "budgets": dict(budgets), "metrics": metrics,
            "counts": {rate: {"errors": errors, "total": total} for rate, (errors, total) in counts.items()},
            "unjudgeable_reference_errors": reference_errors,
            "citation_errors": citation_errors,
            "within_budgets": not reference_errors and not citation_errors and
            all(metrics[rate] <= budgets[rate] for rate in RATES),
            "trials": trials, "qualification_verified": False, "acceptance_authorized": False}
