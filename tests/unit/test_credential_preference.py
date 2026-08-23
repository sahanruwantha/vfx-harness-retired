from __future__ import annotations

import os

import pytest

from vfx_harness.application.preflight import auth
from vfx_harness.infrastructure.config import (
    API_KEY_VARIABLE,
    CREDENTIAL_VARIABLE,
    OAUTH_TOKEN_VARIABLE,
    _apply_credential_preference,
    credential_preference,
)


def _set(monkeypatch: pytest.MonkeyPatch, **values: str | None) -> None:
    for name in (API_KEY_VARIABLE, OAUTH_TOKEN_VARIABLE, CREDENTIAL_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        if value is not None:
            monkeypatch.setenv(name, value)


def test_oauth_is_selected_by_default_when_both_credentials_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(
        monkeypatch,
        **{API_KEY_VARIABLE: "sk-ant-api-test", OAUTH_TOKEN_VARIABLE: "sk-ant-oat-test"},
    )

    _apply_credential_preference()

    assert API_KEY_VARIABLE not in os.environ
    assert os.environ[OAUTH_TOKEN_VARIABLE] == "sk-ant-oat-test"
    report = auth()
    assert report["using"] == OAUTH_TOKEN_VARIABLE


def test_api_key_preference_keeps_both_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(
        monkeypatch,
        **{
            API_KEY_VARIABLE: "sk-ant-api-test",
            OAUTH_TOKEN_VARIABLE: "sk-ant-oat-test",
            CREDENTIAL_VARIABLE: "api_key",
        },
    )

    _apply_credential_preference()

    assert os.environ[API_KEY_VARIABLE] == "sk-ant-api-test"
    report = auth()
    assert report["using"] == API_KEY_VARIABLE
    assert any("VFXH_CREDENTIAL=api_key" in note for note in report["notes"])


def test_lone_api_key_survives_oauth_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, **{API_KEY_VARIABLE: "sk-ant-api-test"})

    _apply_credential_preference()

    assert os.environ[API_KEY_VARIABLE] == "sk-ant-api-test"
    assert auth()["using"] == API_KEY_VARIABLE


def test_unapplied_selection_with_both_visible_is_a_loud_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set(
        monkeypatch,
        **{API_KEY_VARIABLE: "sk-ant-api-test", OAUTH_TOKEN_VARIABLE: "sk-ant-oat-test"},
    )

    report = auth()

    assert report["using"] == API_KEY_VARIABLE
    assert any("load_environment()" in problem for problem in report["problems"])


def test_invalid_preference_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, **{CREDENTIAL_VARIABLE: "subscription"})

    with pytest.raises(ValueError, match="VFXH_CREDENTIAL"):
        credential_preference()
