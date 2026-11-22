"""Remark reader, bulletin step and message drafts, with a stand-in client (no API key, no network)."""
import json
import shutil
from datetime import date

import pytest

from claimcheck import bulletins, config, hypotheses, messages
from claimcheck.extract import PARAM_SCHEMAS
from claimcheck.llm import HAIKU, LLMError
from claimcheck.ruleset import RULES_DIR, load
from fake_llm import FakeClient

NULL_PARAMS = {k: None for k in PARAM_SCHEMAS}
SUMMIT = {"hypothesis_id": "H1", "payer_id": "PY03", "denial_code": "D10", "kind": "whole_hours",
          "params": {**NULL_PARAMS, "units_per_hour": 4}, "find_start_date": True,
          "rationale": "Remarks cite amendment 3: whole hours.", "supporting_claim_ids": []}
WRONG = {**SUMMIT, "hypothesis_id": "H2", "denial_code": "D03", "kind": "timely_filing",
         "params": {**NULL_PARAMS, "days": 20}, "find_start_date": False}


@pytest.fixture
def rules_copy(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    for f in RULES_DIR.glob("*.json"):
        shutil.copy(f, d / f.name)
    return d


# --------------------------------------------------------------------------- remark reader

@pytest.fixture(scope="module")
def proposed(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("hyp")
    answer = {"hypotheses": [SUMMIT, WRONG],
              "not_explainable": [{"payer_id": "PY01", "remark": "Denied - contact payer", "reason": "no reason given"}]}
    client = FakeClient(lambda kw: answer)
    rep = hypotheses.propose(RULES_DIR, tmp / "out", tmp / "cache", client=client)
    return rep, client, tmp


def test_residuals_seen_by_claude_include_the_summit_remarks(proposed):
    rep, client, _ = proposed
    sent = client.calls[0]["messages"][0]["content"]
    assert "amendment 3" in sent and "Denied - contact payer" in sent
    assert "PY03-WHOLE-HOURS" not in client.calls[0]["system"][0]["text"]  # documents-only baseline


def test_right_hypothesis_is_supported_with_its_start_date(proposed):
    rep, _, _ = proposed
    h1 = rep["hypotheses"][0]
    t = h1["test"]
    assert t["verdict"] == "supported" and t["start"] == "2026-07-01" and t["basis"] == "date_of_service"
    assert t["contradictions"] == 0 and t["support"] >= 30
    assert h1["backtest"]["claims_broken"] == 0 and h1["backtest"]["claims_fixed"] >= 30
    assert h1["proposed_rule"]["source"]["type"] == "learned_from_history"


def test_wrong_hypothesis_is_contradicted_by_paid_claims(proposed):
    t = proposed[0]["hypotheses"][1]["test"]
    assert t["verdict"] == "contradicted" and t["contradictions"] > 100
    assert "proposed_rule" not in proposed[0]["hypotheses"][1]


def test_approve_refuses_a_rule_already_known_and_adds_a_new_one(proposed, rules_copy, tmp_path):
    _, _, tmp = proposed
    with pytest.raises(LLMError, match="already has this rule as PY03-WHOLE-HOURS"):
        hypotheses.approve("H1", "Reviewer", rules_copy, tmp / "out")
    docs_only = tmp_path / "docs"
    docs_only.mkdir()
    hypotheses.documents_only(rules_copy, docs_only)
    rule = hypotheses.approve("H1", "Reviewer", docs_only, tmp / "out", today=date(2026, 11, 22))
    assert rule["effective"]["from"] == "2026-07-01"
    assert "PY03-LEARNED-H1" in {r.rule_id for r in load(docs_only).for_payer("PY03").rules}


# --------------------------------------------------------------------------- bulletins

def _perfect_delta(kw):
    target = kw["messages"][-1]["content"]
    pid = "PY01" if "Write the change for PY01" in target else "PY02"
    approved = json.loads((RULES_DIR / f"{pid}.json").read_text())
    return bulletins.expected_delta(approved, bulletins.KNOWN_BULLETINS[pid])


def test_bulletin_evaluation_with_perfect_drafts(tmp_path):
    rep = bulletins.evaluate(RULES_DIR, out_dir=tmp_path, cache_dir=tmp_path / "c", client=FakeClient(_perfect_delta))
    for pid, r in rep.items():
        assert r["missing"] == [] and r["extra"] == [] and r["end_dates_correct"], pid
        n, total = r["rules_left_untouched"].split("/")
        assert n == total and r["unverified_quotes"] == []


def test_bulletin_prompt_uses_the_other_bulletin_as_example(tmp_path):
    client = FakeClient(_perfect_delta)
    bulletins.evaluate(RULES_DIR, out_dir=tmp_path, cache_dir=tmp_path / "c", client=client)
    for kw in client.calls:
        target = kw["messages"][-1]["content"]
        example = kw["messages"][0]["content"]
        if "PY01" in target.split("Write the change for ")[-1]:
            assert "PY02_harbor_bulletin" in example
        else:
            assert "PY01_northwind_bulletin" in example


def test_bulletin_draft_that_ends_an_unknown_rule_is_rejected():
    current = json.loads((RULES_DIR / "PY01.json").read_text())
    bad = {"payer_id": "PY01", "add_rules": [], "open_questions": [],
           "end_rules": [{"rule_id": "PY01-NOPE", "effective_to": "2026-07-31", "quote": "x", "reason": "x"}]}
    with pytest.raises(LLMError):
        bulletins.apply_delta(current, bad)


def test_bulletin_paraphrase_is_caught():
    text = (config.DATA_DIR / "payers" / bulletins.KNOWN_BULLETINS["PY02"]).read_text()
    delta = {"end_rules": [], "open_questions": [],
             "add_rules": [{"rule_id": "PY02-X", "quote": "Weekend care needs U3."}]}
    assert bulletins.unverified_quotes(delta, text) == ["PY02-X"]


# --------------------------------------------------------------------------- messages

def _decision(code, outcome, blockers=(), flags=()):
    return {"claim_id": "C000001", "payer_id": "PY02", "denial_code": code, "explanation": "x",
            "fix": {"outcome": outcome, "blockers": list(blockers), "summary": "x", "billed_before": 10.0,
                    "billed_after": 10.0},
            "flags": [{"code": f, "reason": "x"} for f in flags],
            "evidence": {"verified_visits": [], "unverified_visits": [], "days_to_submit": 5,
                         "ledger": {"units_authorized": 8, "units_remaining": 0},
                         "auth": {"auth_id": "AUTH10001", "patient_id": "PT0001", "start_date": "2026-09-01",
                                  "end_date": "2026-11-30"}}}


@pytest.mark.parametrize("code,outcome,blockers,flags,recipient", [
    ("D01", "not_fixable", ["Visit V000001 is awaiting aggregator approval (manual_edit_pending)"], [], "care_coordinator"),
    ("D01", "not_fixable", ["Visit V000001 was rejected; correct the EVV record"], [], "care_coordinator"),
    ("D02", "needs_decision", [], [], "payer_care_manager"),
    ("D03", "not_fixable", ["Submitted 70 days after care"], [], "billing_lead"),
    ("D04", "fixable", [], [], None),
    ("", "payable_as_is", [], ["PENDING_EVV"], "care_coordinator"),
    ("", "payable_as_is", [], [], None),
])
def test_routing_is_a_fixed_table(code, outcome, blockers, flags, recipient):
    r = messages.route(_decision(code, outcome, blockers, flags))
    assert (r[0] if r else None) == recipient


def test_message_draft_uses_haiku_and_flags_invented_ids(tmp_path):
    claim = {"claim_id": "C000001", "agency_id": "AG001", "patient_id": "PT0001", "service_code": "T1019",
             "date_of_service": "2026-09-05", "submitted_date": "2026-09-10", "units": "8", "billed_amount": "48.80"}
    decisions = {"C000001": _decision("D02", "not_fixable", ["AUTH10001 is used up"])}
    good = FakeClient(lambda kw: {"subject": "C000001: authorization AUTH10001 used up",
                                  "body": "Please increase AUTH10001 for PT0001 (care on 2026-09-05)."})
    out = messages.draft(["C000001"], {"C000001": claim}, decisions, tmp_path / "a", client=good)
    assert out[0]["status"] == "draft" and out[0]["recipient"] == "payer_care_manager"
    assert good.calls[0]["model"] == HAIKU and "fallbacks" not in good.calls[0]
    invented = FakeClient(lambda kw: {"subject": "C000001", "body": "See also claim C999999 and AUTH99999."})
    out = messages.draft(["C000001"], {"C000001": claim}, decisions, tmp_path / "b", client=invented)
    assert out[0]["status"] == "check_references" and out[0]["unknown_references"] == ["AUTH99999", "C999999"]
