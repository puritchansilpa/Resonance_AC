"""Automator, overdraw detection and the web app's API, on the real data pack."""
import json
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from claimcheck import automator, config
from claimcheck.app.data import ActionError, AppState
from claimcheck.app.server import make_handler
from claimcheck.checker import Checker
from claimcheck.evidence import EvidenceBuilder
from claimcheck.ledger import Ledger
from claimcheck.models import Authorization, Visit
from claimcheck.ruleset import load


@pytest.fixture(scope="module")
def state():
    return AppState()


@pytest.fixture(scope="module")
def url(state):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def get(url, path):
    with urllib.request.urlopen(url + path) as r:
        return json.loads(r.read())


def post(url, path, body):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ------------------------------------------------------------------ automator

def visit(vid, minutes, d, svc, patient="PT1"):
    start = datetime(d.year, d.month, d.day, 9, 0)
    return Visit(vid, "CG1", patient, svc, start, start + timedelta(minutes=minutes), "verified", d, minutes, False)


def drafts_for(visits, auths):
    checker = Checker(load(), EvidenceBuilder(visits, auths))
    return automator.draft_all(visits, [], auths, checker, Ledger(auths), as_of=date(2026, 11, 22))


def test_summit_draft_uses_whole_hours_and_filing_deadline():
    auths = {"A": Authorization("A", "PT1", "PY03", "T1005", date(2026, 9, 1), date(2026, 11, 30), 100)}
    d = drafts_for([visit("V1", 90, date(2026, 10, 1), "T1005")], auths)[0]
    assert d.units == 4 and "whole hours" in d.units_note          # 6 whole units -> 1 hour
    assert d.file_by == date(2026, 11, 30) and d.outcome == "ready"


def test_harbor_weekend_draft_gets_u3_and_no_deadline():
    auths = {"A": Authorization("A", "PT1", "PY02", "T1019", date(2026, 9, 1), date(2026, 11, 30), 100)}
    d = drafts_for([visit("V1", 60, date(2026, 9, 5), "T1019")], auths)[0]   # a Saturday
    assert d.modifier == "U3" and d.file_by is None and d.outcome == "ready"


def test_late_draft_is_reported_not_hidden():
    auths = {"A": Authorization("A", "PT1", "PY01", "T1005", date(2026, 6, 1), date(2026, 8, 31), 100)}
    d = drafts_for([visit("V1", 60, date(2026, 7, 1), "T1005")], auths)[0]
    assert d.outcome == "late" and d.days_left < 0


def test_real_automator_counts(state):
    s = automator.summary(state.drafts)
    assert sum(v["drafts"] for v in s.values()) == 891
    assert s["ready"]["drafts"] == 425


# ------------------------------------------------------------ overdraw check

def test_auth11084_overdraw_found_and_flagged(state):
    errs = state.errors()
    assert [e["auth_id"] for e in errs] == ["AUTH11084"]
    e = errs[0]
    assert e["contradicted_predictions"] == ["C021301"] and e["left_from_paid_claims_only"] == 6
    flags = [f.code for f in state.run.check_dec["C021301"].flags]
    assert "CONTRADICTED_BY_HISTORY" in flags


# ------------------------------------------------------------------ app API

def test_overview_and_lists(url):
    o = get(url, "/api/overview")
    assert o["to_check"]["claims"] == 546 and o["history"]["claims"] == 3830 and o["errors"] == 1
    c = get(url, "/api/claims?tab=awaiting&chip=decision")
    assert c["total"] == 32 and all(r["fix_outcome"] == "needs_decision" for r in c["rows"])
    assert get(url, "/api/claims?tab=denied&q=C000022")["rows"][0]["code"] == "D09"
    v = get(url, "/api/visits?tab=fix")
    assert v["total"] == 87
    a = get(url, "/api/authorizations?filter=error")
    assert [r["id"] for r in a["rows"]] == ["AUTH11084"]


def test_claim_detail_has_rules_evidence_and_message(url):
    c = get(url, "/api/claims/C019361")
    assert c["decision"]["denial_code"] == "D02" and len(c["decision"]["results"]) == 9
    assert [d["claim_id"] for d in c["ledger"]["draws"]] == ["C019471", "C007827", "C015468"]
    assert c["message"]["recipient"] == "payer_care_manager"
    h = get(url, "/api/claims/C000022")
    assert h["history"] and h["actual"]["code"] == "D09" and "units 15 -> 14" in h["decision"]["fix"]["summary"]


def test_rules_review_and_notices(url):
    assert len(get(url, "/api/payers/PY03/rules")["rules"]) == 8
    r = get(url, "/api/review")
    assert r["hypotheses"]["hypotheses"][0]["test"]["start"] == "2026-07-01"
    assert {n["id"] for n in get(url, "/api/notices")} >= {"PY01_northwind_bulletin_2026-07", "PY02_harbor_bulletin_2026-08"}


def test_actions_validate_input(url, state):
    code, body = post(url, "/api/onboarding", {"payer_id": "PY02", "name": "x", "file_name": "a.md", "text": "x"})
    assert code == 400 and "already" in body["error"]                    # data-pack payer
    code, body = post(url, "/api/onboarding", {"payer_id": "PY09", "name": "x", "file_name": "../evil.md", "text": "x"})
    assert code == 400 and "File name" in body["error"]
    code, body = post(url, "/api/hypotheses/H1-PY03-WHOLE-HOURS/approve", {"by": "Tester"})
    assert code == 400 and "already has this rule" in body["error"]     # never duplicates a rule
    with pytest.raises(ActionError):
        state.claim("NOPE")
    assert not (config.ONBOARDING_DIR / "uploads").exists()             # refused uploads leave nothing behind


def test_static_page_served(url):
    with urllib.request.urlopen(url + "/") as r:
        assert b"Claim Check" in r.read()
    with urllib.request.urlopen(url + "/../../README.md") as r:           # no escaping the static folder
        assert b"Claim Check" in r.read()
