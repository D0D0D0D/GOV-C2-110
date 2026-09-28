# GOV-C2-110 — MunicipalContractVariationEvidenceBriefAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: GOV

## Overview

MunicipalContractVariationEvidenceBriefAgent organises the evidence behind a public-works contract
variation before it goes up for approval: which document supports which review perspective, what
is missing, what contradicts what, and what the approver should ask.

It is for a municipality's procurement and contract-management staff preparing a design, schedule
or cost variation, and for the approver receiving it. The officer writes the rationale in their
own words and supplies the evidence they have — estimates, schedule-impact records, approval
history. The agent matches that against a set of **fact-check perspectives a human approved in
advance**, and returns a brief where every stated fact is a verbatim quotation with its source
and span.

**It reaches no legal conclusion, and it structurally cannot.** "Permitted under the terms",
"lawful", "constitutes a material change", "reasonable to approve", "the estimate is sound" —
none of these appear, because the output schema has no field that could hold one. The decision
stays with the person who holds the authority.

That guarantee is stronger than "we told the model not to". Two things enforce it:

- **The output is closed down to the values, not just the key names.** Fields hold identifiers,
  closed enumerations, and verbatim quotations. There is no free-text field the model can write
  into, so a conclusion has nowhere to go. Prose a human reads is rendered deterministically from
  the typed fields.
- **A conclusion already written in the evidence is not quotable.** Stage 1 assigns every span a
  `span_kind` deterministically — `quantity`, `date`, `identifier`, `observed_condition`,
  `procedural_record` are quotable; `adjudicative` and `opinion` are not, and anything the
  classifier cannot place becomes `unclassified`, which is also not quotable. So a past decision
  memo saying "permitted under the terms" cannot reach the brief by being quoted. The classifier
  is deterministic — the model is never asked to decide what kind of statement something is. The
  cost is real and accepted: a perspective whose only support is an unquotable span comes back as
  a gap rather than a fact.

Verification is fail-closed in the same direction. Below `evidence_confidence_threshold`, or when
the contract, authority-matrix or effective-date versions do not line up, **no brief is returned
at all** — only typed codes saying what was missing. The officer fixes the input and invokes
again. There is no partial brief and no suspended session to resume.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- caller authentication in front of the entry adapter;
- a `BaseLLM` client and a knowledge base holding the approved perspectives, injected by the
  deployment wiring.

Left unwired, the agent still refuses to fabricate: `/ingest` and `/feedback` return an error
rather than reporting a write that did not happen, and `/invoke` degrades to the typed-code form
instead of releasing an unchecked brief. It will not do useful work in that state — but it will
not pretend to.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `max_retry` (`3`) / `timeout_s` (`60`) | Backbone retry and timeout budget |
| `memory_enabled` (`false`) | No checkpointer. The agent never waits on a human mid-run |
| `top_k` (`8`) | Approved fact-check perspectives recalled per case |
| `evidence_confidence_threshold` (`0.75`) | Below this the brief is withheld and only gap codes are returned. Tune after measuring in your own environment |

A missing or unreadable `config/config.yaml` is a **start-up failure, not a default** —
constructing the agent with an empty configuration would silently disable the thresholds the
safety gates depend on.

Secrets are declared in `config/agent.yaml` and resolved through the runtime's secret store:
`CONTRACT_VARIATION_DB_DSN` (the record store), `CONTRACT_VARIATION_KB_ENDPOINT` (approved
perspectives), `CONTRACT_VARIATION_LLM_API_KEY` (the model provider).

Input is **lightweight text only** — JSON, CSV, plain text. PDF and Excel are not accepted; a
format the agent never parses carries neither its attack surface nor its misreading risk.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR. Without the `agenticstar-agentcore`
distribution installed, `src/api/server.py` cannot be imported at all — the graph, the state
schema and the security gates all come from it, and there is no vendored substitute in this
repository.

With the framework installed but no platform around it, the standalone server starts — provided
`config/config.yaml` is present and parses — and `/health` answers
`{"status": "ok", "agent": "gov-c2-110"}`. Requests will not get far: the first node requires
verified-external trust, so without either upstream middleware or a matching `INVOKE_AUTH_TOKEN`
the caller stays anonymous and the invocation ends in the closed error envelope. With no
knowledge base and no model injected, an authenticated `/invoke` returns the degradation form and
`/ingest` and `/feedback` return an error. The test suite runs without a platform connection.

## Input

Three routes, one graph — the route fixes the mode, which is never inferred from the text.

| Route | Purpose |
|---|---|
| `POST /ingest` | Register the approved baseline: contract, terms, authority matrix, and the fact-check perspectives. **Do this first** |
| `POST /invoke` | Produce the evidence brief for one variation case |
| `POST /feedback` | Record the perspective a human settled on, so it applies next time |

All three take `{"input": "<JSON string>", "session_id": "..."}` and all three are behind the
same authentication. That symmetry is deliberate: `/ingest` and `/feedback` mutate the knowledge
base, so leaving them on a weaker path than `/invoke` would be exactly the asymmetry worth
avoiding. When `INVOKE_AUTH_TOKEN` is set, a caller with no verified middleware trust must
present a matching `Authorization: Bearer` header or receive **401** with a deliberately generic
body — it does not reveal whether the token was absent, malformed or simply wrong.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "{\"case_ref\":\"CASE-1\",\"variation_rationale\":\"掘削中に設計図書に記載のない埋設管を発見した。管理者協議の結果、線形を東へ1.5m振る必要が生じた。\",\"versions\":{\"contract_version\":\"v3\",\"authority_matrix_version\":\"a1\",\"effective_date\":\"2026-04-01\"},\"authority\":{\"approver_role\":\"課長\",\"authority_band\":\"3\"},\"evidence\":[{\"doc_ref\":\"D-1\",\"doc_type\":\"kyogi\",\"spans\":[{\"span_ref\":\"p1-L8\",\"text\":\"掘削中に設計図書に記載のない埋設管を発見\"}]}]}",
    "session_id": "demo-001"
  }'
```

## Output

One of three shapes, checked in that order: error, degradation, then brief.

**Brief** (`kind: "brief"`) carries `evidence_brief` with `perspectives`, `stated_facts` (each a
`verbatim_quote` with its `source_id` and `span_ref`), `evidence_sources` with provenance,
`gaps` (coded), `contradictions`, `verification_questions` (template IDs, not prose) and
`unknowns`.

```json
{
  "output": {
    "kind": "brief",
    "evidence_brief": {
      "perspectives": [{"perspective_id": "change_reason_basis", "source_id": "S-01-01"}],
      "stated_facts": [{
        "fact_id": "F-01", "perspective_id": "change_reason_basis",
        "source_id": "S-01-01", "span_ref": "p1-L8",
        "verbatim_quote": "掘削中に設計図書に記載のない埋設管を発見"
      }],
      "evidence_sources": [{"source_id": "S-01-01", "doc_ref": "D-1", "provenance": {}}],
      "gaps": [{"perspective_id": "schedule_impact_basis", "gap_code": "schedule_impact_absent"}],
      "contradictions": [],
      "verification_questions": [{"question_template_id": "Q-07", "perspective_id": "schedule_impact_basis"}],
      "unknowns": []
    }
  }
}
```

**Degradation** (`kind: "degradation"`) withholds the brief entirely and returns
`requires_human_review: true` with `review_reason_code`, `gap_codes` and `unknown_codes`:

```json
{
  "output": {
    "kind": "degradation",
    "requires_human_review": true,
    "degradation": {
      "review_reason_code": "version_mismatch",
      "gap_codes": ["version_mismatch"],
      "unknown_codes": ["version_undeclared"]
    }
  }
}
```

**Error** returns a closed `error_code` and nothing else. Every exit — success, degradation and
failure alike — passes through a single sanitiser, so an exception message, a stack trace, a
piece of state or an echo of the input cannot leave by a side door. An internal failure becomes
`upstream_failure` and nothing more.

## Security and Limitations

The first node requires verified-external trust, so an anonymous caller is stopped before any
node body runs, on all three routes equally.

The agent depends on a baseline being in place. Without the approved contract, terms, authority
matrix and fact-check perspectives registered through `/ingest`, it does not reach a conclusion —
it reports the absence. This is the intended behaviour, not a limitation to work around.

Known limits, stated rather than implied:

- The span classifier is deterministic and fail-closed, so a span it cannot place is not quoted.
  Correct, quotable facts will sometimes be reported as gaps. That trade is chosen deliberately:
  the alternative failure — a conclusion reaching an approval file as if the agent had found it —
  is worse.
- The framework's personal-data detector has known gaps for Japanese text. This template
  deliberately does **not** add its own detector; it is one layer among several and expects the
  deploying organisation's own controls alongside it.
- Concurrency has not been load-tested. Nodes hold no mutable instance state and pass everything
  through the state dict, but that is a design property, not a measurement.

Output is machine-generated and assistive. Verifying it, and confirming it meets the standards
that apply, is the responsibility of the deploying organisation's legal and audit functions.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), outer backbone wrapping an inner workflow graph
- Generation mode: `llm`, degrading to the typed-code form when no client is injected
- HITL: not enabled — verification failure returns coded gaps and terminates normally; the caller fixes the input and re-invokes
- Persistence: none — `memory_enabled: false`, no checkpointer
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using fakes for the model and the knowledge base.
Running the agent itself starts the standalone server:

```bash
uvicorn src.api.server:app --reload
```

Set `INVOKE_AUTH_TOKEN` before calling any route, or use real authentication middleware.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the span-kind allowlist, the closed
output contract and the security design. `docs/03_test_spec.md` records the test specification,
the verification results, and what has deliberately not been verified yet.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
