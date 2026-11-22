import json
import shutil
from datetime import date, datetime

import pytest

from claimcheck.checker import Checker
from claimcheck.evidence import EvidenceBuilder
from claimcheck.ledger import ACTUAL, PAID, Ledger
from claimcheck.models import Authorization, Claim, Visit
from claimcheck.rule_kinds import units_from_minutes
from claimcheck.ruleset import RULES_DIR, RuleSetError, load


@pytest.mark.parametrize("minutes,rounding,units", [
    (127, "eight_minute_rule", 8),   # 8 full units + 7 min -> no extra unit
    (128, "eight_minute_rule", 9),   # 8 full units + 8 min -> one more
    (8, "eight_minute_rule", 1),
    (7, "eight_minute_rule", 0),
    (134, "whole_units_only", 8),
    (135, "whole_units_only", 9),
])
def test_rounding(minutes, rounding, units):
    assert units_from_minutes(minutes, rounding) == units


def test_rule_files_load_and_version_is_stable():
    assert load().version == load().version
    assert set(load().payers) == {"PY01", "PY02", "PY03"}


def test_editing_a_rule_changes_the_version(tmp_path):
    for f in RULES_DIR.glob("PY*.json"):
        shutil.copy(f, tmp_path / f.name)
    before = load(tmp_path).version
    raw = json.loads((tmp_path / "PY03.json").read_text())
    raw["rules"][5]["params"]["days"] = 90
    (tmp_path / "PY03.json").write_text(json.dumps(raw))
    assert load(tmp_path).version != before


def test_bad_rule_file_is_rejected(tmp_path):
    for f in RULES_DIR.glob("PY*.json"):
        shutil.copy(f, tmp_path / f.name)
    raw = json.loads((tmp_path / "PY01.json").read_text())
    raw["rules"][1]["params"]["rounding"] = "round_up"
    (tmp_path / "PY01.json").write_text(json.dumps(raw))
    with pytest.raises(RuleSetError):
        load(tmp_path)


# --------------------------------------------------------------------------- #
# Checker on hand-built claims
# --------------------------------------------------------------------------- #

def make(payer="PY02", svc="T1019", dos=date(2026, 9, 5), sub=date(2026, 9, 10), units=8,
         auth="A1", modifier="U3", freq=1, cid="C1"):
    return Claim(cid, "to_check", "AG1", payer, "PT1", svc, dos, sub, units, auth, modifier, freq,
                 "C0" if freq == 7 else "", 0.0, "", "", "")


def visit(minutes, status="verified", d=date(2026, 9, 5), svc="T1019", vid="V1"):
    start = datetime(d.year, d.month, d.day, 9, 0)
    from datetime import timedelta
    return Visit(vid, "CG1", "PT1", svc, start, start + timedelta(minutes=minutes), status, d, minutes, False)


def checker_for(visits, auth_units=100, payer="PY02", svc="T1019"):
    auths = {"A1": Authorization("A1", "PT1", payer, svc, date(2026, 9, 1), date(2026, 11, 30), auth_units)}
    return Checker(load(), EvidenceBuilder(visits, auths)), Ledger(auths)


def test_clean_claim_is_paid_with_every_rule_listed():
    chk, led = checker_for([visit(120)])
    d = chk.check(make(), led.view(make()))  # 2026-09-05 is a Saturday: U3 required
    assert d.status == "paid" and d.denial_code == ""
    assert {r.rule_id for r in d.results} >= {"PY02-EVV", "PY02-UNITS", "PY02-MOD-U3-WEEKEND"}


def test_same_day_visits_are_added_together():
    chk, led = checker_for([visit(60, vid="V1"), visit(60, vid="V2")])
    d = chk.check(make(units=8), led.view(make()))
    assert d.evidence.verified_minutes == 120 and d.status == "paid"


def test_pending_visit_is_not_evidence():
    chk, led = checker_for([visit(120, status="manual_edit_pending")])
    d = chk.check(make(), led.view(make()))
    assert d.denial_code == "D01"
    assert "manual_edit_pending" in d.failed[0].detail


def test_harbor_reports_d09_before_d04():
    chk, led = checker_for([visit(60)])
    claim = make(units=8, modifier="U2")  # 4 units supported; weekend needs U3
    d = chk.check(claim, led.view(claim))
    assert [r.denial_code for r in d.failed] == ["D09", "D04"]
    assert d.denial_code == "D09"


def test_summit_reports_missing_auth_before_d09():
    chk, led = checker_for([visit(60, svc="T1005")], payer="PY03", svc="T1005")
    claim = make(payer="PY03", svc="T1005", units=8, auth="", modifier="")
    d = chk.check(claim, led.view(claim))
    assert d.denial_code == "D07" and "PY03-UNITS" in {r.rule_id for r in d.failed}


def test_summit_whole_hours_only_from_july():
    june = make(payer="PY03", svc="T1005", dos=date(2026, 6, 20), sub=date(2026, 6, 25), units=6, modifier="")
    sept = make(payer="PY03", svc="T1005", units=6, modifier="")
    auths = {"A1": Authorization("A1", "PT1", "PY03", "T1005", date(2026, 6, 1), date(2026, 11, 30), 100)}
    chk = Checker(load(), EvidenceBuilder([visit(120, d=date(2026, 6, 20), svc="T1005"),
                                           visit(120, svc="T1005", vid="V2")], auths))
    led = Ledger(auths)
    assert chk.check(june, led.view(june)).status == "paid"
    assert chk.check(sept, led.view(sept)).denial_code == "D10"


def test_northwind_filing_limit_follows_bulletin():
    auths = {"A1": Authorization("A1", "PT1", "PY01", "T1005", date(2026, 6, 1), date(2026, 11, 30), 100)}
    chk = Checker(load(), EvidenceBuilder([visit(120, d=date(2026, 7, 20), svc="T1005"),
                                           visit(120, d=date(2026, 8, 20), svc="T1005", vid="V2")], auths))
    july = make(payer="PY01", svc="T1005", dos=date(2026, 7, 20), sub=date(2026, 8, 29), modifier="")  # 40 days
    aug = make(payer="PY01", svc="T1005", dos=date(2026, 8, 20), sub=date(2026, 9, 29), modifier="")   # 40 days
    led = Ledger(auths)
    assert chk.check(july, led.view(july)).denial_code == "D03"
    assert chk.check(aug, led.view(aug)).status == "paid"


def test_balance_denial_names_the_claims_that_used_it():
    chk, led = checker_for([visit(120, d=date(2026, 9, 3), vid="V0"), visit(120)], auth_units=12)
    first = make(cid="C0", dos=date(2026, 9, 3), sub=date(2026, 9, 4), modifier="U2")
    led.record(first, PAID, ACTUAL)
    d = chk.check(make(), led.view(make()))
    assert d.denial_code == "D02" and "C0 (8)" in d.failed[0].detail


def test_corrected_claim_is_not_a_duplicate():
    chk, led = checker_for([visit(120)])
    led.record(make(cid="C0", sub=date(2026, 9, 6), modifier="U2"), "denied", ACTUAL)
    assert chk.check(make(), led.view(make())).denial_code == "D05"
    fixed = make(cid="C2", freq=7)
    assert chk.check(fixed, led.view(fixed)).status == "paid"
