"""Onboarding the synthetic payer Lakeside (PY04) end to end, with a stand-in client."""
import json
import shutil
from pathlib import Path

import pytest

from claimcheck import bulletins, config, extract, onboard
from claimcheck.ruleset import RULES_DIR, _validate_payer
from fake_llm import FakeClient

DEMO = config.REPO_ROOT / "demo" / "lakeside"
MANUAL = DEMO / "lakeside_billing_manual.md"
NOTICE = DEMO / "lakeside_notice_2026-10.md"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated registry, document store and rules folder; config restored afterwards."""
    monkeypatch.setattr(config, "PAYER_REGISTRY", tmp_path / "payers.json")
    monkeypatch.setattr(config, "ONBOARDED_DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(config, "PAYER_NAMES", dict(config.PAYER_NAMES))
    monkeypatch.setattr(config, "PAYER_ALIASES", dict(config.PAYER_ALIASES))
    rules = tmp_path / "rules"
    rules.mkdir()
    for f in RULES_DIR.glob("*.json"):
        shutil.copy(f, rules / f.name)
    return tmp_path, rules


def ideal_draft():
    """What a careful extraction of the Lakeside manual looks like (from the answer key)."""
    d = extract.approved_to_extraction(json.loads((DEMO / "expected_PY04.json").read_text()))
    d["open_questions"] = [
        {"quote": "Respite (T1005) may not exceed 24 units for a member on any one day.",
         "question": "A respite-only daily cap; the daily maximum rule counts all services."},
        {"quote": "Claims for members enrolled in the Lakeside Choice program may be subject to additional review.",
         "question": "What does the extra review check?"},
    ]
    return d


def test_answer_key_quotes_are_verbatim_and_valid():
    for name in ("expected_PY04.json", "expected_PY04_after_notice.json"):
        _validate_payer({**json.loads((DEMO / name).read_text()), "payer_id": "PY01"})  # shape check
    d = ideal_draft()
    assert extract.unverified_quotes(d, [("m", MANUAL.read_text())]) == []


def test_add_registers_payer_and_aliases(env):
    tmp, _ = env
    entry = onboard.add("PY04", "Lakeside Community Health", ["LAKESIDE CH"], [MANUAL])
    assert entry["documents"] == ["PY04_lakeside_billing_manual.md"]
    assert (tmp / "docs" / "PY04_lakeside_billing_manual.md").exists()
    assert config.PAYER_ALIASES["LAKESIDE CH"] == "PY04" and config.PAYER_NAMES["PY04"] == "Lakeside Community Health"
    assert "PY04" in json.loads((tmp / "payers.json").read_text())


def test_add_refuses_clashes(env):
    with pytest.raises(onboard.OnboardingError):
        onboard.add("PY02", "Someone", [], [MANUAL])                      # data-pack payer
    with pytest.raises(onboard.OnboardingError):
        onboard.add("PY05", "New Plan", ["NORTHWIND HP"], [MANUAL])      # alias already Northwind's
    with pytest.raises(onboard.OnboardingError):
        onboard.add("P4", "Bad ID", [], [MANUAL])


def test_new_payer_extract_approve_then_notice(env):
    tmp, rules = env
    onboard.add("PY04", "Lakeside Community Health", ["LAKESIDE CH"], [MANUAL])
    client = FakeClient(lambda kw: ideal_draft())
    res = extract.extract("PY04", rules, tmp / "proposed", client=client, corrections=[])
    assert "PY02_harbor_provider_manual.md" in client.calls[0]["messages"][0]["content"]  # worked example
    assert res["meta"]["valid"] and res["meta"]["unverified_quotes"] == []
    assert res["proposal"]["precedence_source"]["type"] == "default"
    scored = onboard.check(tmp / "proposed" / "PY04.json", DEMO / "expected_PY04.json")
    assert len(scored["matched"]) == 8 and scored["extra_in_proposal"] == [] and len(scored["open_questions"]) == 2

    extract.approve("PY04", "Demo Reviewer", "", rules, tmp / "proposed", tmp / "corrections.jsonl")
    current = json.loads((rules / "PY04.json").read_text())
    delta = {"payer_id": "PY04",
             "end_rules": [{"rule_id": "PY04-TIMELY-90", "effective_to": "2026-10-31",
                            "quote": "The 90-day limit continues to apply to earlier dates of service.",
                            "reason": "Replaced by 60 days from 2026-11-01."}],
             "add_rules": [r for r in extract.approved_to_extraction(
                 json.loads((DEMO / "expected_PY04_after_notice.json").read_text()))["rules"]
                 if r["rule_id"] == "PY04-TIMELY-60"],
             "open_questions": [{"quote": "Personal care (T1019) delivered on a public holiday must be billed with modifier U4.",
                                 "question": "No holiday list yet and U4 is not a known modifier."}]}
    out = bulletins.draft_change("PY04", NOTICE, current, rules, config.DATA_DIR / "payers",
                                 tmp / "bulletins", tmp / "cache", client=FakeClient(lambda kw: delta))
    assert out["meta"]["valid"] and out["meta"]["unverified_quotes"] == []
    after = extract.compare(out["proposal"], json.loads((DEMO / "expected_PY04_after_notice.json").read_text()))
    assert after["exact_match_rate"] == 1.0
