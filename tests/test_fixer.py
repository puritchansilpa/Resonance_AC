from datetime import date

import pytest

from claimcheck.checker import Checker
from claimcheck.evidence import EvidenceBuilder
from claimcheck.fixer import CHOICE, CORRECTION, FIXABLE, NEEDS_DECISION, NOT_FIXABLE, PAYABLE, REQUIRED, Fixer
from claimcheck.ledger import ACTUAL, DENIED, PAID, Ledger, LedgerError, simulate
from claimcheck.models import Authorization
from claimcheck.review import Reviewer, transitive_closure
from claimcheck.ruleset import load
from test_rules import make, visit

SEP, NOV = date(2026, 9, 1), date(2026, 11, 30)


def setup(visits, payer="PY02", svc="T1019", auth_units=100, extra_auths=()):
    auths = {"A1": Authorization("A1", "PT1", payer, svc, SEP, NOV, auth_units)}
    auths.update({a.auth_id: a for a in extra_auths})
    checker = Checker(load(), EvidenceBuilder(visits, auths))
    return checker, Fixer(checker), Ledger(auths)


def plan(checker, fixer, ledger, claim):
    return fixer.plan(claim, checker.check(claim, ledger.view(claim)), ledger)


def test_paid_claim_needs_nothing():
    chk, fx, led = setup([visit(120)])
    assert plan(chk, fx, led, make()).outcome == PAYABLE


def test_wrong_weekend_modifier_is_a_correction():
    chk, fx, led = setup([visit(120)])
    p = plan(chk, fx, led, make(modifier="U2"))  # Saturday after 1 Sep needs U3
    assert p.outcome == FIXABLE and p.final_decision.status == PAID
    assert [(c.field, c.old, c.new, c.kind) for c in p.changes] == [("modifier", "U2", "U3", CORRECTION)]
    assert p.billed_after == p.billed_before


def test_summit_cut_to_verified_time_then_whole_hours():
    chk, fx, led = setup([visit(290, svc="T1005")], payer="PY03", svc="T1005")
    p = plan(chk, fx, led, make(payer="PY03", svc="T1005", units=20, modifier=""))
    # 290 min supports 19 units (D09); the re-check then finds 19 isn't whole hours (D10).
    assert [(c.denial_code, c.new) for c in p.changes] == [("D09", 19), ("D10", 16)]
    assert all(c.kind == REQUIRED for c in p.changes) and p.rounds == 2 and p.outcome == FIXABLE


def test_missing_auth_filled_from_file_or_blocked():
    chk, fx, led = setup([visit(120)])
    p = plan(chk, fx, led, make(auth=""))
    assert p.outcome == FIXABLE and p.fixed_claim.auth_id == "A1"
    chk2, fx2, led2 = setup([visit(120)], svc="T1005")  # authorization is for another service
    p2 = plan(chk2, fx2, led2, make(auth=""))
    assert p2.outcome == NOT_FIXABLE and "request one from the payer" in p2.blockers[0]


def test_late_claim_is_blocked_but_lists_other_edits():
    chk, fx, led = setup([visit(120)])
    p = plan(chk, fx, led, make(modifier="U2", dos=date(2026, 9, 5), sub=date(2026, 9, 10)))
    assert p.outcome == FIXABLE  # Harbor has no filing limit
    chk3, fx3, led3 = setup([visit(120, svc="T1005")], payer="PY03", svc="T1005")
    late = make(payer="PY03", svc="T1005", units=7, modifier="", sub=date(2026, 11, 20))  # 76 days
    p3 = plan(chk3, fx3, led3, late)
    assert p3.outcome == NOT_FIXABLE and "76 days" in p3.blockers[0]
    assert [c.denial_code for c in p3.changes] == ["D10"]  # still listed for when it's resolved


def test_balance_shortfall_is_a_business_decision():
    chk, fx, led = setup([visit(120, d=date(2026, 9, 3), vid="V0"), visit(120)], auth_units=12)
    led.record(make(cid="C0", dos=date(2026, 9, 3), sub=date(2026, 9, 4), modifier="U2"), PAID, ACTUAL)
    p = plan(chk, fx, led, make())
    assert p.outcome == NEEDS_DECISION
    assert [(c.field, c.new, c.kind) for c in p.changes] == [("units", 4, CHOICE)]


def test_duplicate_of_denied_claim_becomes_corrected_claim():
    chk, fx, led = setup([visit(120)])
    led.record(make(cid="C0", sub=date(2026, 9, 6), modifier="U2"), DENIED, ACTUAL)
    p = plan(chk, fx, led, make())
    assert p.outcome == FIXABLE
    assert p.fixed_claim.frequency_code == 7 and p.fixed_claim.original_claim_id == "C0"


def test_duplicate_of_paid_claim_is_blocked():
    chk, fx, led = setup([visit(120)])
    led.record(make(cid="C0", sub=date(2026, 9, 6)), PAID, ACTUAL)
    p = plan(chk, fx, led, make())
    assert p.outcome == NOT_FIXABLE and "C0" in p.blockers[0]


def test_pending_visit_blocks_and_is_flagged():
    chk, fx, led = setup([visit(120, status="manual_edit_pending")])
    claim = make()
    d = chk.check(claim, led.view(claim))
    p = fx.plan(claim, d, led)
    assert p.outcome == NOT_FIXABLE and "V1" in p.blockers[0] and "awaiting" in p.blockers[0]
    flags = Reviewer(chk.ruleset, [claim], led, set(), {}).flags_for(claim, d)
    assert [f.code for f in flags] == ["PENDING_EVV"]


def test_precedence_chains_count_as_observed():
    closed = transitive_closure({("D10", "D09"), ("D09", "D03")})
    assert ("D10", "D03") in closed and ("D03", "D10") not in closed


def test_simulate_refuses_an_edit_that_changes_identity():
    auths = {"A1": Authorization("A1", "PT1", "PY02", "T1019", SEP, NOV, 100)}
    claim = make()
    with pytest.raises(LedgerError):
        simulate([], [claim], auths, lambda c, v: (make(cid="OTHER"), PAID))
