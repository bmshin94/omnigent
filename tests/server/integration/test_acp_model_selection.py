"""Exercise ACP model policy through persisted sessions and runner launch wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.runner.app import _build_spawn_env_from_spec
from omnigent.runtime import get_agent_store, get_conversation_store
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.helpers import _load_agent_spec_for_session
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_DEFAULT = "catalog-model-default"
_ALTERNATE = "catalog-model-alternate"
_CHILD_DEFAULT = "worker-model-default"
_CHILD_ALTERNATE = "worker-model-alternate"
_UNLISTED = "another-model"


@pytest.fixture(autouse=True)
def _isolated_provider_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Use synthetic provider configuration without ambient credentials or network discovery."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    providers: dict[str, Any] = {}
    for name, default, alternate in (
        ("curated", _DEFAULT, _ALTERNATE),
        ("worker", _CHILD_DEFAULT, _CHILD_ALTERNATE),
        ("default-only", _DEFAULT, None),
    ):
        models = {"default": default}
        if alternate is not None:
            models["alternate"] = alternate
        providers[name] = {
            "kind": "gateway",
            "default": name == "curated",
            "openai": {
                "base_url": "https://gateway.example.invalid/v1",
                "api_key": "fake-test-key",
                "models": models,
            },
        }
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": providers,
                "acp": {"agents": [{"name": "Synthetic", "command": "synthetic-acp"}]},
            }
        )
    )


def _executor(provider: str | None = "curated") -> dict[str, Any]:
    """Build an ACP executor whose command is never started by these metadata-only tests."""
    executor: dict[str, Any] = {
        "type": "omnigent",
        "config": {"harness": "acp:synthetic"},
    }
    if provider is not None:
        executor["auth"] = {"type": "provider", "name": provider}
    return executor


async def _session(client: httpx.AsyncClient, *, provider: str | None = "curated") -> str:
    """Create a real stored ACP bundle and return its owning session id."""
    agent = await create_test_agent(client, executor=_executor(provider), include_llm=False)
    return str(agent["_session_id"])


def _launch_env(session_id: str) -> dict[str, str]:
    """Build the runner environment from the same stored spec and override the API uses."""
    conv = get_conversation_store().get_conversation(session_id)
    assert conv is not None
    spec = _load_agent_spec_for_session(conv, get_agent_store())
    assert spec is not None
    env = _build_spawn_env_from_spec(spec, "acp:synthetic", model_override=conv.model_override)
    assert env is not None
    return env


async def test_approved_model_patch_reaches_runner_without_changing_catalog(
    client: httpx.AsyncClient,
) -> None:
    """A picker selection persists and changes launch model without replacing the default."""
    session_id = await _session(client)
    before = await client.get(f"/v1/sessions/{session_id}")
    assert before.status_code == 200, before.text
    options = before.json()["model_options"]
    assert [option["id"] for option in options] == [_DEFAULT, _ALTERNATE]
    assert options[0]["isDefault"] is True

    response = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert response.status_code == 200, response.text
    orchestration._model_options_cache.pop(session_id, None)
    after = await client.get(f"/v1/sessions/{session_id}")
    assert after.status_code == 200, after.text
    assert after.json()["model_override"] == _ALTERNATE
    assert after.json()["model_options"] == options

    env = _launch_env(session_id)
    assert env["HARNESS_ACP_MODEL"] == _ALTERNATE
    assert env["HARNESS_ACP_DEFAULT_MODEL"] == _DEFAULT
    assert env["HARNESS_ACP_MODEL_LIST"].split(",") == [_DEFAULT, _ALTERNATE]


async def test_unlisted_model_patch_does_not_mutate_session_metadata(
    client: httpx.AsyncClient,
) -> None:
    """An ordinary but unconfigured model id is rejected before any PATCH fields are persisted."""
    session_id = await _session(client)
    selected = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert selected.status_code == 200, selected.text
    before = (await client.get(f"/v1/sessions/{session_id}")).json()

    rejected = await client.patch(
        f"/v1/sessions/{session_id}",
        json={
            "model_override": _UNLISTED,
            "title": "This title should not be stored",
            "labels": {"test.acp-rejected": "true"},
            "silent": True,
        },
    )
    assert rejected.status_code == 400, rejected.text
    assert "curated" in rejected.text
    after = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert after["model_override"] == _ALTERNATE
    assert after["title"] == before["title"]
    assert after["labels"] == before["labels"]
    assert after["model_options"] == before["model_options"]


async def test_model_reset_restores_provider_default(client: httpx.AsyncClient) -> None:
    """Reset clears persistence and subsequent launches use the original provider default."""
    session_id = await _session(client)
    selected = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert selected.status_code == 200, selected.text
    reset = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": "default", "silent": True}
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["model_override"] is None
    env = _launch_env(session_id)
    assert env["HARNESS_ACP_MODEL"] == _DEFAULT
    assert env["HARNESS_ACP_DEFAULT_MODEL"] == _DEFAULT
    assert env["HARNESS_ACP_MODEL_LIST"].split(",") == [_DEFAULT, _ALTERNATE]


async def test_child_session_uses_its_own_provider_catalog(client: httpx.AsyncClient) -> None:
    """A bundled worker advertises and validates its own provider rather than its parent's."""
    agent = await create_test_agent(
        client,
        executor=_executor(),
        include_llm=False,
        sub_agents=[
            {"name": "worker", "executor": {**_executor("worker"), "model": _CHILD_DEFAULT}}
        ],
    )
    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": agent["_session_id"],
            "sub_agent_name": "worker",
            "initial_items": [],
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert [option["id"] for option in snapshot["model_options"]] == [
        _CHILD_DEFAULT,
        _CHILD_ALTERNATE,
    ]

    rejected = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _ALTERNATE, "silent": True}
    )
    assert rejected.status_code == 400, rejected.text
    accepted = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"model_override": _CHILD_ALTERNATE, "silent": True},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["model_override"] == _CHILD_ALTERNATE


@pytest.mark.parametrize("provider", [None, "default-only"])
async def test_unrelated_or_default_only_config_keeps_model_selection_unrestricted(
    client: httpx.AsyncClient, provider: str | None
) -> None:
    """Existing vendor-owned agents and default-only providers retain arbitrary model selection."""
    session_id = await _session(client, provider=provider)
    response = await client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": _UNLISTED, "silent": True}
    )
    assert response.status_code == 200, response.text
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert snapshot["model_override"] == _UNLISTED
    assert snapshot["model_options"] == []
    env = _launch_env(session_id)
    assert env["HARNESS_ACP_MODEL"] == _UNLISTED
    assert "HARNESS_ACP_MODEL_LIST" not in env


@pytest.mark.parametrize("model, expected_status", [(_ALTERNATE, 201), (_UNLISTED, 400)])
async def test_create_session_validates_model_override(
    client: httpx.AsyncClient, model: str, expected_status: int
) -> None:
    """The create endpoint applies the same curated selection policy before storing an override."""
    agent = await create_test_agent(client, executor=_executor(), include_llm=False)
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "model_override": model, "initial_items": []},
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 201:
        session_id = response.json()["id"]
        snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
        assert snapshot["model_override"] == _ALTERNATE
        env = _launch_env(session_id)
        assert env["HARNESS_ACP_MODEL"] == _ALTERNATE
        assert env["HARNESS_ACP_MODEL_LIST"].split(",") == [_DEFAULT, _ALTERNATE]
