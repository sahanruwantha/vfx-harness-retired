"""Drive the authenticated `higgsfield` CLI for generate-construction plates.

The CLI carries its own auth (`higgsfield auth login`), so no API keys are handled
here. Cursor MCP at https://mcp.higgsfield.ai/mcp is a coding-agent connector and
is not the `vfx run` adapter. Image→3D remains Meshy (`meshy.py`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import httpx

from vfx_harness.infrastructure.config import Settings
from vfx_harness.observability.log import log

_NVM = Path.home() / ".nvm/versions/node/v22.14.0/bin/higgsfield"
_URL_RE = re.compile(r"https?://[^\s\"'\\]+")


def cli_bin() -> str:
    configured = Settings.from_environment().higgsfield_bin
    if configured:
        return configured
    found = shutil.which("higgsfield") or shutil.which("hf")
    if found:
        return found
    if _NVM.is_file():
        return str(_NVM)
    raise RuntimeError("higgsfield CLI not found (set HIGGSFIELD_BIN or add it to PATH)")


def ensure_fresh() -> None:
    """No-op — the higgsfield CLI manages its own session. Present so the image
    backends in `images.py` share one interface."""


def run_cli(*args: str, timeout: float = 3600.0) -> list | dict:
    """Run `higgsfield <args> --json`, streaming its progress (stderr) live to the
    log while capturing stdout (the JSON result). Returns parsed JSON."""
    log(f"↳ higgsfield {' '.join(args[:3])} … (streaming)", 2)
    proc = subprocess.Popen([cli_bin(), *args, "--json"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    tail: list[str] = []

    def _pump():
        for line in proc.stderr:  # CLI prints job progress/polling here
            line = line.rstrip()
            if line:
                tail.append(line)
                del tail[:-40]
                log(line, 3)

    th = threading.Thread(target=_pump, daemon=True)
    th.start()
    out = proc.stdout.read()
    proc.wait(timeout=timeout)
    th.join(timeout=2)
    if proc.returncode != 0:
        raise RuntimeError(f"higgsfield {' '.join(args)} failed (rc={proc.returncode}):\n"
                           + "\n".join(tail[-8:]))
    return json.loads(out)


def _first_job(out: list | dict) -> dict:
    jobs = out if isinstance(out, list) else [out]
    if not jobs:
        raise RuntimeError("higgsfield returned no job")
    return jobs[0]


def _pick_url(job: dict, suffixes: tuple[str, ...]) -> str:
    """Prefer result_url; otherwise scan the job blob for a URL with a wanted suffix."""
    ordered: list[str] = []
    if job.get("result_url"):
        ordered.append(job["result_url"])
    ordered += _URL_RE.findall(json.dumps(job))
    for u in ordered:
        if u.split("?")[0].lower().endswith(suffixes):
            return u
    if job.get("result_url"):
        return job["result_url"]  # trust it even if the suffix is opaque
    raise RuntimeError(f"no {suffixes} URL in job {job.get('id')}")


def _download(url: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=180.0) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return out


def edit_image(
    references: list[str | Path],
    prompt: str,
    out: str | Path,
    *,
    model: str = "nano_banana_pro",
    aspect_ratio: str = "1:1",
    resolution: str = "2k",
    wait_timeout: str = "8m",
) -> Path:
    """Image→image edit. Construction plates must pass a parent crop; text-only is illegal."""
    refs = [Path(path) for path in references]
    if not refs:
        raise ValueError(
            "Higgsfield image-edit needs at least one parent plate. "
            "Text-only generate is not a legal construction-route input."
        )
    missing = [str(path) for path in refs if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"parent plate(s) not found: {', '.join(missing)}")
    args = ["generate", "create", model]
    for path in refs:
        args.extend(["--image", str(path)])
    args.extend(
        [
            "--prompt",
            prompt,
            "--aspect-ratio",
            aspect_ratio,
            "--resolution",
            resolution,
            "--wait",
            "--wait-timeout",
            wait_timeout,
            "--wait-interval",
            "5s",
        ]
    )
    job = _first_job(run_cli(*args))
    return _download(_pick_url(job, (".png", ".webp", ".jpg", ".jpeg")), Path(out))


def generate_image(prompt: str, out: str | Path, *, model: str = "nano_banana_pro",
                   aspect_ratio: str = "1:1", resolution: str = "1k",
                   wait_timeout: str = "8m") -> Path:
    job = _first_job(run_cli(
        "generate", "create", model, "--prompt", prompt,
        "--aspect-ratio", aspect_ratio, "--resolution", resolution,
        "--wait", "--wait-timeout", wait_timeout, "--wait-interval", "5s"))
    return _download(_pick_url(job, (".png", ".webp", ".jpg", ".jpeg")), Path(out))


def isolate_regen(reference: str | Path, out: str | Path, *, subject: str = "the building",
                  prompt: str | None = None, model: str = "nano_banana_pro",
                  aspect_ratio: str = "3:4", resolution: str = "2k",
                  wait_timeout: str = "8m") -> Path:
    """Clean isolation via image→image: regenerate the reference subject as a sharp,
    well-lit, WHITE-BACKGROUND product shot suitable for image→3D. Far better than
    alpha-cutting a dark low-res crop — it recovers detail image→3D can actually use.
    """
    p = prompt or (
        f"{subject}. Reproduce THIS EXACT structure faithfully — same design, "
        f"proportions, and any sign text — but as a clean 3D-reference product shot: "
        f"the entire object centered and fully in frame, upright, on a plain solid "
        f"WHITE background, even soft studio lighting, crisp architectural detail, "
        f"no other buildings, no sky, no ground, no city lights, no motion blur")
    log(f"isolate(regen): {model} clean white-bg from {Path(reference).name}", 2)
    return edit_image(
        [reference],
        p,
        out,
        model=model,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        wait_timeout=wait_timeout,
    )


def remove_background(image: str | Path, out: str | Path, *, subject: str | None = None,
                      wait_timeout: str = "5m") -> Path:
    """Isolate the subject: reference scene image → clean cutout PNG (alpha).

    `subject` is accepted for parity with the `codex` backend (whose cutout redraws
    the subject first) and ignored here — the bg-remover model takes no prompt.
    """
    log(f"isolate: background-remove {Path(image).name}", 2)
    job = _first_job(run_cli(
        "generate", "create", "image_background_remover", "--image", str(image),
        "--wait", "--wait-timeout", wait_timeout, "--wait-interval", "5s"))
    return _download(_pick_url(job, (".png", ".webp")), Path(out))
