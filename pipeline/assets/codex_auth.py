"""Codex credential handling — mirrors the CLI's `AuthManager` so we never re-login.

The Codex CLI refreshes lazily: whenever something asks for auth it does a cheap
local check (`should_refresh_proactively`) and only hits the network when the access
token is nearly expired. We do the same against the same endpoint, so the pipeline
picks up where the CLI left off and either side can refresh for the other.

    proactive: access-token `exp` within 5 min      → refresh
               (or `last_refresh` older than 8 days if `exp` is unreadable)
    reactive:  a 401 from the API                   → force a refresh, retry once
    POST https://auth.openai.com/oauth/token
         {client_id, grant_type: "refresh_token", refresh_token}

**Rotation is the whole reason this module exists.** The refresh token rotates on
every use and the backend detects reuse — so a refresh whose result we failed to
write back would invalidate the login the next time either client refreshed. Every
refresh here therefore persists the rotated tokens before returning, and does it
more carefully than the CLI does (which truncates `auth.json` in place):

  * write a temp file in the same directory at mode 0600, then `os.replace` —
    atomic, so a concurrent reader sees the whole old file or the whole new one,
    never a half-written or zero-length one;
  * re-assert 0600 on the destination rather than trusting an inherited mode;
  * preserve every field we don't own (`auth_mode`, `agent_identity`, …).

Cross-process safety follows the CLI's approach: take a lock, then **reload and
compare** before refreshing — if another process rotated the token while we waited,
we adopt its result instead of spending our (now stale) refresh token. Note the lock
is ours alone; the Rust CLI uses its own, so a simultaneous refresh by both is still
possible in principle. The reload-and-compare is what actually makes that benign in
the common case, and a lost race surfaces as a clear "run codex login" error rather
than silent corruption.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from ..log import log

REFRESH_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # the CLI's own OAuth client
REFRESH_WINDOW = timedelta(minutes=5)  # CHATGPT_ACCESS_TOKEN_REFRESH_WINDOW_MINUTES
STALE_AFTER = timedelta(days=8)  # TOKEN_REFRESH_INTERVAL, when `exp` is unreadable
_TIMEOUT = 60.0
_RETRIES = 2
_BACKOFF = 3.0

# Backend codes that mean the refresh chain is dead — retrying cannot help.
_PERMANENT_CODES = ("refresh_token_expired", "refresh_token_reused",
                    "refresh_token_invalidated", "invalid_grant")


class CodexAuthError(RuntimeError):
    """No usable Codex login. Actionable, and never worth retrying."""


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def auth_path() -> Path:
    return codex_home() / "auth.json"


def _load() -> dict:
    p = auth_path()
    if not p.is_file():
        raise CodexAuthError(f"no Codex credentials at {p} — run `codex login` first")
    try:
        return json.loads(p.read_text()) or {}
    except json.JSONDecodeError as e:
        raise CodexAuthError(f"{p} is not valid JSON ({e}) — run `codex login`") from None


def _claims(jwt: str) -> dict:
    """JWT payload, base64-decoded but NOT signature-verified — same as the CLI does.

    Only ever used for local scheduling (`exp`) and display (`account_id`, plan). Never
    gate anything security-relevant on these: they are unverified attacker-shaped data
    if the file is tampered with, and the server re-validates the token regardless.
    """
    try:
        p = jwt.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:  # noqa: BLE001 — an opaque token just means "can't tell"
        return {}


def expires_at(access_token: str) -> datetime | None:
    exp = _claims(access_token).get("exp")
    return datetime.fromtimestamp(exp, timezone.utc) if exp else None


def _account_id(data: dict) -> str:
    tokens = data.get("tokens") or {}
    claimed = (_claims(tokens.get("id_token") or "")
               .get("https://api.openai.com/auth", {}) or {}).get("chatgpt_account_id")
    return claimed or tokens.get("account_id") or ""


def should_refresh(data: dict) -> bool:
    """The CLI's `should_refresh_proactively`, minus the modes we don't support."""
    tokens = data.get("tokens") or {}
    if not tokens.get("refresh_token"):
        return False  # nothing to refresh with
    exp = expires_at(tokens.get("access_token") or "")
    if exp:
        return datetime.now(timezone.utc) + REFRESH_WINDOW >= exp
    last = data.get("last_refresh")
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - when >= STALE_AFTER


@contextlib.contextmanager
def _lock():
    """Serialize refreshes between our own processes (the CLI holds its own lock)."""
    path = codex_home() / ".bambi-refresh.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh, fcntl.LOCK_UN)
        os.close(fh)


def _save(data: dict) -> None:
    """Atomically persist auth.json at 0600 (temp file + rename, mode re-asserted)."""
    dest = auth_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".auth-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)  # set on the temp file, before any content lands in it
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)  # atomic: readers see old or new, never partial
        os.chmod(dest, 0o600)  # re-assert; `replace` keeps our mode, a prior file's may differ
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _request_refresh(refresh_token: str) -> dict:
    """POST the refresh, classifying failures the way `classify_refresh_token_failure` does."""
    last: Exception | None = None
    for attempt in range(1, _RETRIES + 2):
        if attempt > 1:
            time.sleep(_BACKOFF * (attempt - 1))
        try:
            r = httpx.post(REFRESH_URL, timeout=_TIMEOUT,
                           headers={"content-type": "application/json"},
                           json={"client_id": CLIENT_ID, "grant_type": "refresh_token",
                                 "refresh_token": refresh_token})
        except httpx.HTTPError as e:  # network blip — transient
            last = e
            continue
        if r.status_code == 200:
            body = r.json()
            if not body.get("access_token"):
                raise CodexAuthError(f"refresh returned no access token: {r.text[:200]}")
            return body
        blob = r.text[:400]
        if any(code in blob for code in _PERMANENT_CODES) or r.status_code in (400, 401):
            raise CodexAuthError(
                f"Codex refresh token is no longer valid ({r.status_code}: {blob[:160]}) — "
                "run `codex login` to sign in again.")
        last = RuntimeError(f"{r.status_code} {blob[:160]}")  # 5xx/429 — transient
    raise RuntimeError(f"token refresh failed after {_RETRIES + 1} tries: {last}")


def refresh(data: dict | None = None, *, force: bool = False) -> dict:
    """Refresh and persist, guarding against a concurrent refresh by another process.

    `force` refreshes even when the token looks fresh — the reactive path after a 401,
    where the server has revoked or rotated it ahead of its `exp`.
    """
    before = data if data is not None else _load()
    stale_token = (before.get("tokens") or {}).get("refresh_token")
    if not stale_token:
        raise CodexAuthError(
            f"{auth_path()} has no refresh token (API-key-only login?) — run `codex login`")

    with _lock():
        current = _load()  # guarded reload: someone may have refreshed while we waited
        tokens = current.get("tokens") or {}
        if tokens.get("refresh_token") != stale_token:
            if _account_id(current) != _account_id(before) and _account_id(before):
                raise CodexAuthError(
                    "the Codex account changed under us — another process logged in as a "
                    "different account. Re-run to pick up the new credentials.")
            log("token was refreshed by another process — using theirs", 3)
            return current
        if not force and not should_refresh(current):
            return current  # someone refreshed and we already have the result

        log("refreshing Codex access token", 2)
        body = _request_refresh(tokens["refresh_token"])
        merged = dict(current)
        merged["tokens"] = {
            **tokens,
            "access_token": body["access_token"],
            # rotation: keep the new refresh token, or the old one if none came back
            "refresh_token": body.get("refresh_token") or tokens["refresh_token"],
            "id_token": body.get("id_token") or tokens.get("id_token"),
        }
        merged["tokens"]["account_id"] = _account_id(merged) or tokens.get("account_id")
        merged["last_refresh"] = (datetime.now(timezone.utc)
                                  .isoformat(timespec="microseconds").replace("+00:00", "Z"))
        _save(merged)
        exp = expires_at(merged["tokens"]["access_token"])
        rotated = merged["tokens"]["refresh_token"] != tokens["refresh_token"]
        log(f"token refreshed (expires {exp.isoformat() if exp else 'unknown'}, "
            f"refresh token {'rotated' if rotated else 'reused'})", 3)
        return merged


def auth(*, force_refresh: bool = False) -> tuple[str, str]:
    """(access_token, account_id) — the CLI's `AuthManager::auth()` entry point.

    Refreshes first when the token is close to expiry, so callers never think about it.
    """
    data = _load()
    if force_refresh or should_refresh(data):
        data = refresh(data, force=force_refresh)
    tokens = data.get("tokens") or {}
    access = tokens.get("access_token")
    if not access:
        raise CodexAuthError(
            "no ChatGPT access token in Codex credentials (API-key-only login?) — run "
            f"`codex login` to sign in with a ChatGPT account [{auth_path()}]")
    exp = expires_at(access)
    if exp and exp <= datetime.now(timezone.utc):
        raise CodexAuthError(
            f"Codex access token expired at {exp.isoformat()} and could not be refreshed "
            "— run `codex login`.")
    return access, _account_id(data)


def ensure_fresh() -> None:
    """Refresh if due AND confirm the credentials are usable, raising if they aren't.

    Optional — `auth()` does the same lazily on every call. Calling it up-front turns
    "no Codex login" into an error at stage start instead of mid-run.
    """
    auth()


def status() -> dict:
    data = _load()
    tokens = data.get("tokens") or {}
    exp = expires_at(tokens.get("access_token") or "")
    claims = (_claims(tokens.get("id_token") or "")
              .get("https://api.openai.com/auth", {}) or {})
    return {
        "path": str(auth_path()),
        "auth_mode": data.get("auth_mode"),
        "account_id": _account_id(data),
        "plan": claims.get("chatgpt_plan_type"),
        "expires": exp.isoformat() if exp else None,
        "expires_in_hours": round((exp - datetime.now(timezone.utc)).total_seconds() / 3600, 2)
        if exp else None,
        "last_refresh": data.get("last_refresh"),
        "refresh_due": should_refresh(data),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Show / refresh the Codex credentials the "
                                             "asset stage borrows.")
    ap.add_argument("--refresh", action="store_true",
                    help="force a refresh now instead of only when due")
    args = ap.parse_args()
    if args.refresh:
        refresh(force=True)
    print(json.dumps(status(), indent=2))


if __name__ == "__main__":
    main()
