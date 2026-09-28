"""Shared test fixtures for GOV-C2-110."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

# Make the repo root importable so `src.*` resolves without an editable install.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

QUOTE = "掘削中に設計図書に記載のない埋設管を発見"
ADJUDICATIVE_TEXT = "本変更は約款上許容される"


class FakeLLM:
    """Deterministic stand-in for an injected BaseLLM."""

    def __init__(self, brief: dict | None = None, raise_on_call: bool = False) -> None:
        self._brief = brief
        self._raise = raise_on_call
        self.calls: list = []

    def complete(self, messages):
        self.calls.append(messages)
        if self._raise:
            raise RuntimeError("provider exploded with case text: " + QUOTE)
        return {"content": json.dumps(self._brief or {}, ensure_ascii=False)}


class CapturingKB:
    """Knowledge-base double that records what would be persisted."""

    def __init__(self, rows: list | None = None) -> None:
        self.rows: list = []
        self._search_rows = rows or []

    def write(self, record: dict) -> None:
        self.rows.append(record)

    def search(self, query: str = "", top_k: int = 8):
        return list(self._search_rows)[:top_k]


def make_brief(**overrides) -> dict:
    """A schema-valid brief; override any section to build a negative case."""
    brief = {
        "perspectives": [{"perspective_id": "P-01", "source_id": "S-01-01"}],
        "stated_facts": [
            {
                "fact_id": "F-01",
                "perspective_id": "P-01",
                "source_id": "S-01-01",
                "span_ref": "p1-L8",
                "verbatim_quote": QUOTE,
            }
        ],
        "evidence_sources": [{"source_id": "S-01-01", "doc_ref": "D-1", "provenance": {}}],
        "gaps": [],
        "contradictions": [],
        "verification_questions": [{"question_template_id": "Q-07", "perspective_id": "P-01"}],
        "unknowns": [],
    }
    brief.update(overrides)
    return brief


@pytest.fixture
def agent_config() -> dict:
    """Mirrors the production config/config.yaml shape."""
    return {
        "top_k": 8,
        "evidence_confidence_threshold": 0.75,
        "baseline": {
            "contract_version": "v3",
            "authority_matrix_version": "a1",
            "effective_date": "2026-04-01",
        },
        "authority_matrix": {"3": "課長"},
        "perspectives": [
            {
                "perspective_id": "P-01",
                "perspective_code": "change_reason_basis",
                "required_fact_codes": ["reason"],
                "question_template_ids": ["Q-07"],
                "source_id": "S-01-01",
                "provenance": {},
                "approved_by_role": "課長",
                "approved_at": "2026-04-01",
                "record_version": 1,
            }
        ],
    }


@pytest.fixture
def case_payload() -> dict:
    return {
        "mode": "invoke",
        "case_ref": "CASE-1",
        "variation_rationale": "掘削中に設計図書に記載のない埋設管を発見した。",
        "versions": {
            "contract_version": "v3",
            "authority_matrix_version": "a1",
            "effective_date": "2026-04-01",
        },
        "authority": {"approver_role": "課長", "authority_band": "3"},
        "evidence": [
            {
                "doc_ref": "D-1",
                "doc_type": "kyogi",
                "spans": [{"span_ref": "p1-L8", "text": QUOTE}],
            }
        ],
    }
