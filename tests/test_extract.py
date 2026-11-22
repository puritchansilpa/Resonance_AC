"""Rule extractor tests. A stand-in client replaces Claude, so no API key or network is needed."""
import json
import re
import shutil
from datetime import date
from types import SimpleNamespace

import pytest

from claimcheck import extract
from claimcheck.ruleset import RULES_DIR


def approved(pid):
    return json.loads((RULES_DIR / f"{pid}.json").read_text(encoding="utf-8"))


class FakeClient:
    """Answers like the Messages API with a draft chosen per payer; counts calls."""

    def __init__(self, drafts):
        self.drafts = drafts
        self.calls = 0
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        self.requests.append(kw)
        pid = re.search(r"Write the rule file for (PY\d\d)", kw["messages"][-1]["content"]).group(1)
        text = json.dumps(self.drafts[pid])
        return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", _request_id="req_test",
                               content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=3000, output_tokens=1500))


def perfect(pid):
    """What a perfect extraction looks like: the approved file's document rules in template shape."""
    d = extract.approved_to_extraction(approved(pid))
    if pid == "PY03":
        d["open_questions"] = [{"quote": "Additional billing requirements may apply under your agency's contract with Summit.",
                                "question": "What does the agency's contract with Summit require?"}]
    return d


@pytest.fixture
def dirs(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    for f in RULES_DIR.glob("*.json"):
        shutil.copy(f, rules / f.name)
    return rules, tmp_path / "proposed"


def test_request_is_leave_one_out_and_stable():
    r1, r2 = extract.build_request("PY03", corrections=[]), extract.build_request("PY03", corrections=[])
    assert extract.request_key(r1) == extract.request_key(r2)
    assert "PY02_harbor_provider_manual.md" in r1["messages"][0]["content"]   # worked example
    assert "PY03_summit_provider_manual.md" not in r1["messages"][0]["content"]
    assert "PY03" in r1["messages"][2]["content"]


def test_schema_is_strict():
    rule = extract.output_schema()["properties"]["rules"]["items"]
    assert rule["additionalProperties"] is False and set(rule["required"]) == set(rule["properties"])


def test_perfect_draft_matches_and_is_cached(dirs):
    rules, out = dirs
    client = FakeClient({"PY01": perfect("PY01")})
    res = extract.extract("PY01", rules, out, client=client, corrections=[])
    assert res["meta"]["valid"] and res["meta"]["unverified_quotes"] == []
    cmp = extract.compare(res["proposal"], approved("PY01"))
    assert cmp["exact_match_rate"] == 1.0 and cmp["extra_in_proposal"] == []
    again = extract.extract("PY01", rules, out, client=client, corrections=[])
    assert client.calls == 1 and again["meta"]["from_cache"]               # second run from cache
    assert again["proposal"] == res["proposal"]                            # same proposal


def test_request_uses_structured_output_and_fallbacks(dirs):
    rules, out = dirs
    client = FakeClient({"PY01": perfect("PY01")})
    extract.extract("PY01", rules, out, client=client, corrections=[])
    kw = client.requests[0]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]


def test_paraphrased_quote_is_caught(dirs):
    rules, out = dirs
    d = perfect("PY01")
    d["rules"][0]["quote"] = "Visits need EVV."
    res = extract.extract("PY01", rules, out, client=FakeClient({"PY01": d}), corrections=[])
    assert res["meta"]["unverified_quotes"] == [d["rules"][0]["rule_id"]]


def test_wrong_parameter_and_missing_rule_are_reported(dirs):
    rules, out = dirs
    d = perfect("PY01")
    timely = next(r for r in d["rules"] if r["params"]["days"] == 45)
    timely["params"]["days"] = 60
    d["rules"] = [r for r in d["rules"] if r["kind"] != "no_duplicates"]
    res = extract.extract("PY01", rules, out, client=FakeClient({"PY01": d}), corrections=[])
    cmp = extract.compare(res["proposal"], approved("PY01"))
    assert [x["approved"] for x in cmp["parameter_differences"]] == ["PY01-TIMELY-45"]
    assert cmp["missing_from_proposal"] == ["PY01-DUPLICATE"]


def test_summit_contract_clause_is_an_open_question_and_learned_rule_survives_merge(dirs):
    rules, out = dirs
    res = extract.extract("PY03", rules, out, client=FakeClient({"PY03": perfect("PY03")}), corrections=[])
    assert res["meta"]["unverified_quotes"] == []
    merged = extract.merge_for_use(res["proposal"], approved("PY03"))
    assert "PY03-WHOLE-HOURS" in {r["rule_id"] for r in merged["rules"]}
    assert merged["denial_precedence"] == approved("PY03")["denial_precedence"]


def test_approve_logs_reviewer_edits_and_feeds_them_back(dirs, tmp_path):
    rules, out = dirs
    d = perfect("PY01")
    next(r for r in d["rules"] if r["params"]["days"] == 45)["params"]["days"] = 60
    extract.extract("PY01", rules, out, client=FakeClient({"PY01": d}), corrections=[])
    # Reviewer fixes the proposal file by hand, then approves.
    prop_path = out / "PY01.json"
    prop = json.loads(prop_path.read_text())
    next(r for r in prop["rules"] if r["params"].get("days") == 60)["params"]["days"] = 45
    prop_path.write_text(json.dumps(prop))
    log = tmp_path / "corrections.jsonl"
    res = extract.approve("PY01", "Test Reviewer", "Bulletin 2026-07 says 45 days", rules, out, log,
                          today=date(2026, 11, 22))
    assert len(res["corrections_logged"]) == 1 and "days=60" in res["corrections_logged"][0]["lesson"]
    assert json.loads((rules / "PY01.json").read_text())["approval"]["approved_by"] == "Test Reviewer"
    req = extract.build_request("PY01", rules, corrections=extract.load_corrections(log))
    assert "Bulletin 2026-07 says 45 days" in req["messages"][2]["content"]


def test_evaluate_with_perfect_drafts_reproduces_the_backtest(dirs):
    rules, out = dirs
    client = FakeClient({p: perfect(p) for p in ("PY01", "PY02", "PY03")})
    rep = extract.evaluate(rules, out, client=client)
    assert all(c["exact_match_rate"] == 1.0 for c in rep["payers"].values())
    bt = rep["backtest"]
    assert bt["extracted_rules"] == bt["approved_rules"]
    assert bt["claims_to_check_with_a_different_prediction"] == []
