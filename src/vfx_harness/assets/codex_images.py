"""Image generation via Codex's `gpt-image-2` route (ISOLATION + concept art).

Drop-in replacement for `higgsfield.py`: same three entry points
(`generate_image`, `isolate_regen`, `remove_background`), same "no API keys here"
property — it borrows the Codex CLI's own ChatGPT credentials from
`$CODEX_HOME/auth.json` (default `~/.codex`), exactly as the CLI's built-in image
tool does. Log in once with `codex login` and this works.

Wire format (confirmed against a live capture of Codex TUI 0.147.0):

    POST https://chatgpt.com/backend-api/codex/images/{generations,edits}
    authorization: Bearer <access_token>   chatgpt-account-id: <account_id>
    originator: codex_cli_rs               x-codex-image-turn-id: <uuid>
    {"prompt": ..., "model": "gpt-image-2", "size", "quality", "background"}
    → {"data": [{"b64_json": <png>}], "size": "1254x1254", ...}

Edits take `images: [{"image_url": "data:image/png;base64,..."}]`, max 5.

Three behaviours of this route worth knowing (all measured, not assumed):

  * `size`/`quality`/`background` are **hints**. The backend answers with its own
    resolution (~1.2-1.4 MP) and `quality: "low"` no matter what you ask for. The
    size hint still steers the ASPECT, so it is worth sending.
  * `background: "transparent"` is **not honoured** — you get an opaque RGB PNG.
    Worse, asking for transparency in the *prompt* makes the model paint a fake
    grey checkerboard into the pixels. Never do it; `remove_background` keys the
    white background off locally instead.
  * Tokens refresh themselves. `codex_auth` mirrors the CLI's lazy refresh (5-minute
    expiry window, reactive retry on 401) and **persists the rotation**, so you log
    in once with `codex login` and the pipeline keeps working from then on.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image

from vfx_harness.observability.log import log

from . import codex_auth
from .codex_auth import CodexAuthError

# Hardcoded on purpose: an env-overridable base URL is a lever that redirects a live
# bearer token to an arbitrary host (the `CODEX_REFRESH_TOKEN_URL_OVERRIDE` class of
# hole). Nothing in a VFX pipeline needs to repoint this.
BASE_URL = "https://chatgpt.com/backend-api/codex"
IMAGE_MODEL = "gpt-image-2"
MAX_EDIT_IMAGES = 5  # matches the CLI tool's own cap
_CLIENT_VERSION = "0.147.0"
_TIMEOUT = 300.0
_RETRIES = 2
_BACKOFF = 5.0  # seconds, multiplied by the attempt number

# `size` is an aspect hint (see module docstring) — the backend picks the real pixels.
_ASPECT_SIZES = {
    "1:1": "1024x1024",
    "3:4": "1024x1536",
    "4:3": "1536x1024",
    "2:3": "1024x1536",
    "3:2": "1536x1024",
    "9:16": "1024x1536",
    "16:9": "1536x1024",
}


class _Permanent(RuntimeError):
    """A failure retrying won't fix (bad request, refusal, malformed response)."""


def _headers(*, force_refresh: bool = False) -> dict[str, str]:
    access, account = codex_auth.auth(force_refresh=force_refresh)
    h = {
        "version": _CLIENT_VERSION,
        "authorization": f"Bearer {access}",
        "content-type": "application/json",
        "accept": "*/*",
        "originator": "codex_cli_rs",
        "user-agent": f"codex_cli_rs/{_CLIENT_VERSION} (Linux; x86_64)",
        "x-codex-image-turn-id": str(uuid.uuid4()),
    }
    if account:
        h["chatgpt-account-id"] = account
    return h


def _post(path: str, payload: dict, out: Path) -> Path:
    """POST an images request, retry transient failures, write the PNG to `out`."""
    last: Exception | None = None
    reauthed = force_next = False  # one forced refresh per request (UnauthorizedRecovery)
    for attempt in range(1, _RETRIES + 2):
        try:
            if attempt > 1:
                log(f"retry {attempt}/{_RETRIES + 1} in {_BACKOFF * (attempt - 1)}s "
                    f"({str(last)[:80]})", 3)
                time.sleep(_BACKOFF * (attempt - 1))
            headers = _headers(force_refresh=force_next)
            force_next = False  # refresh once per 401, not on every later attempt
            r = httpx.post(f"{BASE_URL}/{path}", headers=headers, json=payload,
                           timeout=_TIMEOUT)
            if r.status_code in (401, 403) and not reauthed:
                # Revoked or rotated server-side before `exp` — force a refresh and retry.
                log(f"{r.status_code} from images API — forcing a token refresh", 3)
                reauthed = force_next = True
                last = RuntimeError(f"{r.status_code} (re-authenticating)")
                continue
            if r.status_code in (401, 403):
                raise CodexAuthError(
                    f"Codex rejected the token ({r.status_code}) even after refreshing — "
                    "run `codex login` to sign in again.")
            if r.status_code >= 500 or r.status_code == 429:
                raise RuntimeError(f"{r.status_code} {r.text[:200]}")  # transient → retry
            if r.status_code != 200:
                raise _Permanent(f"{path} failed ({r.status_code}): {r.text[:400]}")

            body = r.json()
            data = (body.get("data") or [])
            if not data or not data[0].get("b64_json"):
                raise _Permanent(f"{path} returned no image: {json.dumps(body)[:300]}")
            png = base64.b64decode(data[0]["b64_json"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(png)
            usage = body.get("usage") or {}
            log(f"↳ gpt-image-2 {path.split('/')[-1]} → {out.name} "
                f"({body.get('size')}, {len(png) // 1024} KB, "
                f"{usage.get('total_tokens', '?')} tok)", 3)
            return out
        except (_Permanent, CodexAuthError):
            raise  # bad request, refusal, or a dead login — retrying cannot help
        except Exception as e:
            last = e
    raise RuntimeError(f"{path} failed after {_RETRIES + 1} tries: {last}")


def ensure_fresh() -> None:
    """Refresh the token up-front so a long stage doesn't stall on it mid-run.

    Optional — every call refreshes lazily anyway. This just moves the (rare) network
    round-trip to stage start, where a failure is easier to read.
    """
    codex_auth.ensure_fresh()


def _data_url(image: str | Path) -> str:
    p = Path(image)
    if not p.is_file():
        raise FileNotFoundError(f"reference image not found: {p}")
    mime = {".png": "image/png", ".webp": "image/webp"}.get(p.suffix.lower(), "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def _size(aspect_ratio: str) -> str:
    return _ASPECT_SIZES.get(aspect_ratio, "auto")


def generate_image(prompt: str, out: str | Path, *, aspect_ratio: str = "1:1",
                   quality: str = "high") -> Path:
    """Text→image. `quality` is passed through but the route currently answers low."""
    log(f"generate: gpt-image-2 {aspect_ratio} → {Path(out).name}", 2)
    return _post("images/generations",
                 {"prompt": prompt, "model": IMAGE_MODEL, "size": _size(aspect_ratio),
                  "quality": quality, "background": "auto"},
                 Path(out))


def edit_image(references: list[str | Path], prompt: str, out: str | Path, *,
               aspect_ratio: str = "1:1", quality: str = "high") -> Path:
    """Image→image edit against up to MAX_EDIT_IMAGES reference views."""
    refs = list(references)
    if not refs:
        raise ValueError("edit_image needs at least one reference image")
    if len(refs) > MAX_EDIT_IMAGES:
        log(f"note: {len(refs)} references given, using the first {MAX_EDIT_IMAGES}", 3)
        refs = refs[:MAX_EDIT_IMAGES]
    return _post("images/edits",
                 {"prompt": prompt, "model": IMAGE_MODEL, "size": _size(aspect_ratio),
                  "quality": quality, "background": "auto",
                  "images": [{"image_url": _data_url(r)} for r in refs]},
                 Path(out))


def isolate_regen(reference: str | Path, out: str | Path, *, subject: str = "the building",
                  prompt: str | None = None, aspect_ratio: str = "3:4") -> Path:
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
    log(f"isolate(regen): gpt-image-2 clean white-bg from {Path(reference).name}", 2)
    return edit_image([reference], p, out, aspect_ratio=aspect_ratio)


def remove_background(image: str | Path, out: str | Path, *, tolerance: int = 28,
                      regen: bool = True, subject: str = "the subject") -> Path:
    """Isolate the subject → cutout PNG with real alpha.

    The image route cannot emit transparency (see module docstring), so the alpha is
    keyed locally: flood-fill the connected background in from the border and knock it
    out. By default the source is first redrawn on a clean white backdrop (`regen`),
    which is what makes a border flood-fill reliable; pass `regen=False` to key an
    image that is already on a flat background.
    """
    src = Path(image)
    if regen:
        staged = Path(out).with_name(Path(out).stem + "_white.png")
        src = edit_image([image], (
            f"{subject}. Reproduce THIS EXACT subject faithfully — same design, "
            f"proportions and markings — centered, fully in frame, on a plain solid "
            f"WHITE background with even soft studio lighting. No shadow on the "
            f"backdrop, no other objects, no ground, no scenery."), staged)
    log(f"isolate(cutout): keying background off {src.name}", 2)
    return _key_background(src, Path(out), tolerance=tolerance)


def _key_background(src: Path, out: Path, *, tolerance: int = 28) -> Path:
    """Flood-fill the flat backdrop from the image border and write RGBA."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    px = im.load()
    # Seed from the border corners; the backdrop is whatever they agree on.
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    bg = tuple(sum(c[i] for c in corners) // len(corners) for i in range(3))
    tol_sq = tolerance * tolerance * 3

    def matches(p) -> bool:
        return sum((p[i] - bg[i]) ** 2 for i in range(3)) <= tol_sq

    alpha = [255] * (w * h)
    stack = [(x, y) for x in range(w) for y in (0, h - 1)]
    stack += [(x, y) for y in range(h) for x in (0, w - 1)]
    seen = bytearray(w * h)
    while stack:  # 4-connected flood fill inward from the frame edge
        x, y = stack.pop()
        i = y * w + x
        if seen[i]:
            continue
        seen[i] = 1
        if not matches(px[x, y]):
            continue
        alpha[i] = 0
        if x > 0:
            stack.append((x - 1, y))
        if x < w - 1:
            stack.append((x + 1, y))
        if y > 0:
            stack.append((x, y - 1))
        if y < h - 1:
            stack.append((x, y + 1))

    cut = im.convert("RGBA")
    a = Image.new("L", (w, h))
    a.putdata(alpha)
    cut.putalpha(a)
    out.parent.mkdir(parents=True, exist_ok=True)
    cut.save(out)
    kept = sum(1 for v in alpha if v) * 100 // (w * h)
    log(f"↳ cutout → {out.name} ({kept}% subject coverage)", 3)
    return out
