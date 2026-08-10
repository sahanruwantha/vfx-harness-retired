"""Higgsfield image client — generate the object image that Tripo turns into a mesh.

The generation half of the asset pipeline: Higgsfield makes the *image* (from a prompt, optionally
conditioned on a reference image), Tripo (:mod:`scene.tripo`) turns it into a GLB.

Implemented over the **Higgsfield CLI** (``@higgsfield/cli``), not the raw HTTP API, because the
CLI is the funded/authenticated path here (the platform API key pair was out of credits) and it
already handles auth, job submission, and polling — ``generate create <model> --wait --json``
blocks until the job finishes and prints the result URL. We shell out, parse the JSON, and
download the image. The subprocess runner and the fetch are injectable so the client is testable
without the CLI or the network; the JSON→URL selection is a pure, tested helper.

The one live-verified shape: ``--json`` returns a list of job objects, each with ``status`` and
``result_url`` (full res) / ``min_result_url`` (preview). ``text2image_soul_v2`` is the default;
``image_references`` (local paths, auto-uploaded) enables reference-conditioned generation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

CLI = "higgsfield"
DEFAULT_MODEL = "text2image_soul_v2"
DEFAULT_ASPECT = "16:9"
DEFAULT_WAIT_TIMEOUT = "5m"
USER_AGENT = "theBambiProject-scene/0.1 (+https://localhost)"
_COMPLETED = {"completed", "success"}


class HiggsfieldError(RuntimeError):
    """A Higgsfield CLI generation failed, or produced no image URL."""


def available() -> bool:
    """True when the Higgsfield CLI is on PATH — callers skip image gen gracefully otherwise."""
    return shutil.which(CLI) is not None


# --- pure helpers (tested without the CLI) ------------------------------------------


def build_command(
    prompt: str,
    *,
    cli: str = CLI,
    model: str = DEFAULT_MODEL,
    aspect_ratio: str = DEFAULT_ASPECT,
    quality: str | None = None,
    image_references: Sequence[str] | None = None,
    wait_timeout: str = DEFAULT_WAIT_TIMEOUT,
    extra: Mapping[str, Any] | None = None,
) -> list[str]:
    """The ``generate create`` argv. Params are ``--name value``; underscores become dashes."""
    cmd = [cli, "generate", "create", model, "--prompt", prompt, "--aspect-ratio", aspect_ratio]
    if quality:
        cmd += ["--quality", quality]
    for ref in image_references or []:
        cmd += ["--image-references", str(ref)]
    for key, value in (extra or {}).items():
        cmd += [f"--{key.replace('_', '-')}", str(value)]
    cmd += ["--wait", "--wait-timeout", wait_timeout, "--json"]
    return cmd


def result_url_from_jobs(payload: Any) -> str | None:
    """The finished image URL from the CLI's ``--json`` output (a list of job objects)."""
    jobs = payload if isinstance(payload, list) else (payload.get("jobs") or [payload])
    for job in jobs:
        if isinstance(job, dict) and str(job.get("status", "")).lower() in _COMPLETED:
            url = job.get("result_url") or job.get("min_result_url")
            if url:
                return str(url)
    for job in jobs:  # fallback: any result URL, even if status was phrased differently
        if isinstance(job, dict) and (job.get("result_url") or job.get("min_result_url")):
            return str(job.get("result_url") or job.get("min_result_url"))
    return None


# --- steps --------------------------------------------------------------------------


def download(url: str, dest: str | Path, *, timeout: float = 120.0) -> Path:
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as fh:  # noqa: S310
        while chunk := resp.read(1 << 16):
            fh.write(chunk)
    return dest_path


def generate_image(
    prompt: str,
    dest: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    aspect_ratio: str = DEFAULT_ASPECT,
    quality: str | None = None,
    image_references: Sequence[str] | None = None,
    wait_timeout: str = DEFAULT_WAIT_TIMEOUT,
    extra: Mapping[str, Any] | None = None,
    timeout: float = 600.0,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    _fetch: Callable[..., Path] = download,
) -> Path:
    """Prompt (+ optional reference image) → generated image file, via the Higgsfield CLI.

    ``_run``/``_fetch`` are injectable for testing. Raises :class:`HiggsfieldError` on a CLI failure
    (its stderr is surfaced) or when the job produced no image URL.
    """
    cmd = build_command(
        prompt, model=model, aspect_ratio=aspect_ratio, quality=quality,
        image_references=image_references, wait_timeout=wait_timeout, extra=extra,
    )
    proc = _run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise HiggsfieldError(f"higgsfield CLI failed ({proc.returncode}): {(proc.stderr or '').strip()[-400:]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise HiggsfieldError(f"could not parse CLI JSON: {(proc.stdout or '')[:300]}") from exc
    url = result_url_from_jobs(payload)
    if not url:
        raise HiggsfieldError(f"generation returned no image URL: {payload}")
    return _fetch(url, dest)
