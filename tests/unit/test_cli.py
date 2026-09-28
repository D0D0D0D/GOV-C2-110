"""AGENTIC STAR Marketplace adapter tests."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import bound_secrets


class _Provider(SecretProvider):
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)


@pytest.fixture
def azure_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "AZURE_OPENAI_API_KEY": "key-one",
        "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "deployment-one",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _valid_packet(**updates: Any) -> str:
    payload: dict[str, Any] = {
        "case_ref": "CASE-101",
        "variation_rationale": "設計図書にない埋設管を確認した。",
        "evidence": [],
        "versions": {},
        "authority": {},
    }
    payload.update(updates)
    return json.dumps(payload, ensure_ascii=False)


def test_invoke_rebuilds_context_and_forces_read_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_invoke(self: object, user_input: str, **kwargs: Any) -> dict[str, Any]:
        del self
        captured.update(user_input=user_input, **kwargs)
        return {"status": "success"}

    monkeypatch.setattr(cli.ContractVariationEvidenceBriefAgent, "invoke", fake_invoke)
    graph = cli.MarketplaceGraph({})
    runner_ctx = InvocationContext(
        session_id="runner-session",
        caller_trust_level=TrustLevel.ANONYMOUS,
        hitl_allowed=True,
    )

    result = graph.invoke(
        _valid_packet(unused="discarded"),
        session_id="runner-session",
        ctx=runner_ctx,
        input_context={"conversation_history": ["走査されない自由文"], "mode": "ingest"},
    )

    assert result == {"status": "success"}
    assert captured["input_context"] == {}
    packet = json.loads(captured["user_input"])
    assert packet["mode"] == "invoke"
    assert packet["case_ref"] == "CASE-101"
    assert "unused" not in packet
    adapted_ctx = captured["ctx"]
    assert adapted_ctx.caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert adapted_ctx.hitl_allowed is False


@pytest.mark.parametrize("mode", ["ingest", "feedback", "delete"])
def test_marketplace_rejects_non_invoke_modes(mode: str) -> None:
    with pytest.raises(ValueError, match="is not available"):
        cli._marketplace_user_input(_valid_packet(mode=mode))


@pytest.mark.parametrize("case_ref", [None, "", "   "])
def test_marketplace_rejects_missing_or_blank_case_ref(case_ref: object) -> None:
    with pytest.raises(ValueError, match="clean non-empty"):
        cli._marketplace_user_input(_valid_packet(case_ref=case_ref))


def test_marketplace_rejects_non_opaque_case_ref() -> None:
    with pytest.raises(ValueError, match="opaque identifier"):
        cli._marketplace_user_input(_valid_packet(case_ref="案件101"))


def test_provision_secrets_fails_at_startup_on_missing_or_whitespace(
    monkeypatch: pytest.MonkeyPatch,
    azure_environment: dict[str, str],
) -> None:
    graph = cli.MarketplaceGraph({})
    runner_provider = _Provider(dict(azure_environment))

    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    with pytest.raises(Exception, match="AZURE_OPENAI_API_KEY"):
        graph.provision_secrets(runner_provider)

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "   ")
    with pytest.raises(ValueError, match="clean non-empty"):
        graph.provision_secrets(runner_provider)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(
    monkeypatch: pytest.MonkeyPatch,
    azure_environment: dict[str, str],
    endpoint: str,
) -> None:
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    graph = cli.MarketplaceGraph({})
    runner_provider = _Provider(dict(azure_environment))
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        graph.provision_secrets(runner_provider)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "   ")
    with pytest.raises(ValueError, match="clean non-empty"):
        graph.provision_secrets(runner_provider)


def test_provision_secrets_uses_allowlisted_environment(
    azure_environment: dict[str, str],
) -> None:
    graph = cli.MarketplaceGraph({})
    graph.provision_secrets(_Provider({"UNRELATED_SECRET": "runner-value"}))

    provider = graph._secrets_provider
    assert provider.get("AZURE_OPENAI_API_KEY") == azure_environment["AZURE_OPENAI_API_KEY"]
    assert provider.get("UNRELATED_SECRET") is None


def test_azure_client_and_connection_values_are_resolved_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, str]] = []

    class FakeAzureClient:
        def __init__(self, config: dict[str, str]) -> None:
            created.append(dict(config))

        def complete(self, messages: list[Any]) -> dict[str, Any]:
            del messages
            return {"content": "{}"}

    module_name = "shared.services.llm.azure_openai_client"
    fake_module = types.ModuleType(module_name)
    fake_module.AzureOpenAIClient = FakeAzureClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake_module)

    provider = _Provider(
        {
            "AZURE_OPENAI_API_KEY": "key-one",
            "AZURE_OPENAI_ENDPOINT": "https://one.example",
            "AZURE_OPENAI_DEPLOYMENT": "deployment-one",
        }
    )
    llm = cli.MarketplaceAzureLLM()
    with bound_secrets(provider):
        llm.complete([])
        provider.values.update(
            {
                "AZURE_OPENAI_API_KEY": "key-two",
                "AZURE_OPENAI_ENDPOINT": "https://two.example",
                "AZURE_OPENAI_DEPLOYMENT": "deployment-two",
            }
        )
        llm.complete([])

    assert created == [
        {
            "api_key": "key-one",
            "azure_endpoint": "https://one.example",
            "azure_deployment": "deployment-one",
        },
        {
            "api_key": "key-two",
            "azure_endpoint": "https://two.example",
            "azure_deployment": "deployment-two",
        },
    ]
    assert not hasattr(llm, "_client")


def test_marketplace_graph_injects_azure_wrapper() -> None:
    graph = cli.MarketplaceGraph({})
    assert isinstance(graph._llm, cli.MarketplaceAzureLLM)


def test_marketplace_adapter_invokes_the_real_graph(
    azure_environment: dict[str, str],
) -> None:
    graph = cli.MarketplaceGraph({})
    graph.compile()
    graph.provision_secrets(_Provider(azure_environment))

    result = graph.invoke(_valid_packet())

    assert result["status"] == "success"
    assert result["output"] == {
        "kind": "degradation",
        "requires_human_review": True,
        "degradation": {
            "review_reason_code": "version_mismatch",
            "gap_codes": ["version_mismatch"],
            "unknown_codes": ["version_undeclared"],
        },
    }
