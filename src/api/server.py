"""GOV-C2-110 — standalone HTTP entry point.

Entry points are adapters only: no business logic lives here.

Two responsibilities beyond the scaffold default:

* **Single error boundary.** ``error_strategy="propagate"`` means an inner
  failure surfaces as ``SubgraphError``. Returning that to the caller would put
  exception text — which can echo case content — outside the egress contract,
  so every graph/domain exception is caught here and mapped to a closed
  ``error_code``.
* **No ``/resume``.** This template runs no in-graph HITL, so there is no
  suspended session to resume; the endpoint is deliberately not registered.
"""

import os
import pathlib
import secrets
from uuid import uuid4

import yaml

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.graph.graph import ContractVariationEvidenceBriefAgent
from src.services import contracts
from src.services.egress import build_error

app = FastAPI(title="GOV-C2-110 Municipal Contract-Variation Evidence Brief Agent")

def _load_runtime_config() -> dict:
    """Load config/config.yaml.

    Constructing the agent with an empty config would silently disable the
    thresholds and baselines the safety gates depend on, so a missing or
    unreadable file is a startup failure, not a default.
    """
    config_path = pathlib.Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    if not config_path.exists():
        raise RuntimeError(f"config/config.yaml not found at {config_path}")
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError("config/config.yaml must contain a mapping")
    return loaded


# NOTE (deployment): `kb` and `llm` are injected here by the deployment wiring.
# Left as None the agent still refuses to fabricate — /ingest and /feedback
# return an error rather than reporting phantom writes, and /invoke degrades
# instead of releasing an unchecked brief — but it cannot do useful work.
agent = ContractVariationEvidenceBriefAgent(config=_load_runtime_config())
agent.compile()
agent.provision_secrets(
    secrets_factory(namespace="gov", agent_name="gov-c2-110")
)


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


def _authenticated_trust(request: Request) -> TrustLevel:
    """Resolve caller trust, applying the standalone bearer-token gate.

    Applied identically to every write-capable route, not just ``/invoke``:
    ``/ingest`` and ``/feedback`` mutate the knowledge base, so leaving them on
    a weaker path than ``/invoke`` would be the asymmetry that has bitten other
    templates.
    """
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of a clean 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was
            # absent, malformed, or simply wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    return trust


def _run(mode: str, req: InvokeRequest, request: Request) -> dict:
    """Invoke the agent for ``mode`` behind the single error boundary."""
    trust = _authenticated_trust(request)
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        try:
            result = agent.invoke(req.input, ctx=ctx, input_context={"mode": mode})
        except HTTPException:
            raise
        except Exception:
            # Deliberately swallow the exception detail: the message, its
            # arguments and any traceback can carry case content or an
            # evidence-sourced conclusion. Only a closed code leaves here.
            return build_error("upstream_failure")

        # A node can return status=error with an error_log; returning the
        # framework wrapper as-is would put those strings outside the closed
        # error contract. Map every error status to the envelope.
        if isinstance(result, dict) and result.get("status") == "error":
            return build_error("upstream_failure")
        return result


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    return _run(contracts.MODE_INVOKE, req, request)


@app.post("/ingest")
async def ingest(req: InvokeRequest, request: Request):
    return _run(contracts.MODE_INGEST, req, request)


@app.post("/feedback")
async def feedback(req: InvokeRequest, request: Request):
    return _run(contracts.MODE_FEEDBACK, req, request)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "gov-c2-110"}
