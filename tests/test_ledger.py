from datetime import date

import pytest

from claimcheck.ledger import ACTUAL, DENIED, PAID, PREDICTED, Ledger, LedgerError, simulate
from claimcheck.models import Authorization, Claim

AUTH = Authorization("A1", "PT1", "PY03", "T1005", date(2026, 6, 1), date(2026, 8, 31), 68)


def claim(cid, units, sub, dos=date(2026, 7, 1), svc="T1005", auth="A1", freq=1,
          dataset="history", status="paid", payer="PY03", patient="PT1"):
    return Claim(cid, dataset, "AG1", payer, patient, svc, dos, sub, units, auth, "", freq, "",
                 0.0, status, "", "")


def test_worked_example_from_history():
    """AUTH10512: 68 units; three paid claims use 64; the next three are D02."""
    ledger = Ledger({"A1": AUTH})
    for cid, units, sub in [("C1", 16, date(2026, 6, 30)), ("C2", 24, date(2026, 7, 20)),
                            ("C3", 24, date(2026, 8, 16))]:
        ledger.record(claim(cid, units, sub), PAID, ACTUAL)
    late = claim("C4", 12, date(2026, 8, 20))
    view = ledger.view(late)
    assert view.units_remaining == 4
    assert [d.claim_id for d in view.auth_draws] == ["C1", "C2", "C3"]  # who used it up
    entry = ledger.record(late, DENIED, ACTUAL)
    assert entry.units_drawn == 0 and entry.auth_remaining_after == 4  # denials draw nothing


def test_daily_total_counts_all_services_but_only_paid_claims():
    auths = {"A1": AUTH, "A2": Authorization("A2", "PT1", "PY03", "T1019", AUTH.start_date, AUTH.end_date, 500)}
    ledger = Ledger(auths)
    d = date(2026, 8, 6)
    ledger.record(claim("C1", 10, date(2026, 8, 11), dos=d), PAID, ACTUAL)
    ledger.record(claim("C2", 30, date(2026, 8, 12), dos=d, svc="T1019", auth="A2"), DENIED, ACTUAL)
    v = ledger.view(claim("C3", 5, date(2026, 8, 13), dos=d, svc="T1019", auth="A2"))
    assert v.day_paid_units == 10
    assert [x.claim_id for x in v.day_paid_claims] == ["C1"]


def test_every_submission_counts_for_duplicates_even_if_denied():
    ledger = Ledger({"A1": AUTH})
    ledger.record(claim("C1", 8, date(2026, 7, 5)), DENIED, ACTUAL)
    assert ledger.view(claim("C2", 8, date(2026, 7, 9))).prior_submissions == ("C1",)
    # a different date of service is not a duplicate
    assert ledger.view(claim("C3", 8, date(2026, 7, 9), dos=date(2026, 7, 2))).prior_submissions == ()


def test_out_of_order_and_double_recording_refused():
    ledger = Ledger({"A1": AUTH})
    ledger.record(claim("C2", 4, date(2026, 7, 10)), PAID, ACTUAL)
    with pytest.raises(LedgerError):
        ledger.record(claim("C1", 4, date(2026, 7, 9)), PAID, ACTUAL)
    with pytest.raises(LedgerError):
        ledger.record(claim("C2", 4, date(2026, 7, 10)), PAID, ACTUAL)


def test_paid_claim_must_have_known_authorization():
    with pytest.raises(LedgerError):
        Ledger({"A1": AUTH}).record(claim("C1", 4, date(2026, 7, 9), auth=""), PAID, ACTUAL)


def test_simulate_interleaves_files_and_records_predictions():
    history = [claim("H1", 60, date(2026, 7, 1)), claim("H2", 8, date(2026, 7, 20))]
    to_check = [claim("T1", 4, date(2026, 7, 10), dataset="to_check", status="")]
    seen = {}

    def decide(c, view):
        seen[c.claim_id] = view.units_remaining
        return PAID

    ledger, views = simulate(history, to_check, {"A1": AUTH}, decide)
    assert seen == {"T1": 8}  # sees H1 (July 1) but not H2 (July 20)
    assert [e.claim_id for e in ledger.entries] == ["H1", "T1", "H2"]
    assert ledger.entries[1].outcome_source == PREDICTED
    assert ledger.entries[2].auth_remaining_before == 4  # H2 sees T1's predicted draw
