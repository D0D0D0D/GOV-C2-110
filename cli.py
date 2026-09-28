"""AGENTIC STAR Marketplace entry adapter for GOV-C2-110."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for pre-1.0.2 wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.graph.graph import ContractVariationEvidenceBriefAgent
from src.services import contracts
from src.services.egress import is_valid_identifier

_NAMESPACE = "gov"
_AGENT_NAME = "GOV-C2-110"
_PROVIDER_AGENT_NAME = "gov_c2_110"
_MARKETPLACE_MODE = contracts.MODE_INVOKE

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_ALLOWED_ENVIRONMENT_KEYS = frozenset(_AZURE_OPENAI_SECRET_KEYS)
_MARKETPLACE_INPUT_KEYS = (
    "case_ref",
    "variation_rationale",
    "evidence",
    "versions",
    "authority",
)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's Azure values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_ENVIRONMENT_KEYS:
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without trimming or repair."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _require_case_ref(payload: dict[str, Any]) -> str:
    """Validate the request-owned opaque case handle before graph invocation."""

    value = payload.get("case_ref")
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise ValueError("Marketplace case_ref must be a clean non-empty string")
    if not is_valid_identifier(value):
        raise ValueError("Marketplace case_ref must be an opaque identifier")
    return value


class _AzureOpenAIClientCompat(BaseLLM):
    """Small Azure chat-completion client for AgentCore before 1.0.2."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = dict(config)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions" "?api-version=2024-10-21"
        request = Request(
            url,
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={
                "api-key": self._config["api_key"],
                "content-type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - operator-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        model = payload.get("model", self._config["azure_deployment"])
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": model,
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        if tools:
            raise NotImplementedError("Azure compatibility client does not support tool binding")
        return self


class MarketplaceAzureLLM(BaseLLM):
    """Resolve Azure values and create a fresh client for every operation."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        self._tools = tools

    def _new_client(self) -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Azure is the sole invocation-time import. Framework, graph and
        # security imports remain eager for Marketplace's to_thread dispatch.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        client = cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )
        return client.bind_tools(list(self._tools)) if self._tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "MarketplaceAzureLLM":
        return type(self)(tools=tuple(tools))


def _marketplace_user_input(user_input: str) -> str:
    """Build the invoke-only packet accepted by the existing S-2 boundary."""

    if not isinstance(user_input, str) or not user_input.strip():
        raise ValueError("Marketplace input must be a non-empty JSON object")
    try:
        supplied = json.loads(user_input)
    except json.JSONDecodeError as exc:
        raise ValueError("Marketplace input must be a JSON object") from exc
    if not isinstance(supplied, dict):
        raise ValueError("Marketplace input must be a JSON object")

    requested_mode = supplied.get("mode")
    if requested_mode is not None and requested_mode != _MARKETPLACE_MODE:
        raise ValueError(f"Marketplace mode {requested_mode!r} is not available")

    case_ref = _require_case_ref(supplied)
    packet = {
        "mode": _MARKETPLACE_MODE,
        **{key: supplied[key] for key in _MARKETPLACE_INPUT_KEYS if key in supplied},
    }
    packet["case_ref"] = case_ref
    return json.dumps(packet, ensure_ascii=False, sort_keys=True)


class MarketplaceGraph(ContractVariationEvidenceBriefAgent):
    """Marketplace-only read boundary and per-call Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # Nodes capture llm during graph construction, so install the
        # credential-free wrapper before the runner compiles the graph.
        super().__init__(config=dict(config or {}), llm=MarketplaceAzureLLM())

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner conversation_history is free text outside S-2. Rebuild from
        # nothing and put only the adapter-owned mode in the scanned JSON.
        del input_context
        base_ctx = ctx if ctx is not None else InvocationContext()
        resolved_session_id = base_ctx.session_id or session_id or str(uuid4())
        marketplace_ctx = replace(
            base_ctx,
            session_id=resolved_session_id,
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        )
        return cast(
            dict[str, Any],
            super().invoke(
                _marketplace_user_input(user_input),
                session_id=resolved_session_id,
                ctx=marketplace_ctx,
                input_context={},
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore broader/dotenv-capable runner providers and fail during
        # startup unless every allowlisted Azure value is usable.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
