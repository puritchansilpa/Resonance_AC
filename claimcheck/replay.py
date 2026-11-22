"""Replay the claims history through the ledger and check it explains the outcomes.

This tests the ledger's mechanics without any rule engine: history claims carry
their real outcomes, so we record those and ask, for every claim, whether the
ledger state at that moment is consistent with what the payer did.

  paid claim         -> its units must fit in the authorization's remaining units,
                        must not push a Harbor member over the daily cap,
                        and (if a new claim) must not repeat an earlier submission
  D02 denial         -> units must exceed the remaining units
  D06 denial         -> the day's paid units plus this claim must exceed the cap
  D05 denial         -> a new claim with an earlier submission of the same key

Out: output/ledger/history_replay.csv   one row per history claim: balances + checks
     output/ledger/replay_summary.json  pass / fail counts per check

Run: python -m claimcheck.replay
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from . import config
from .ledger import ACTUAL, DENIED, PAID, Ledger, in_stream_order, order_sensitive_ties
from .models import Claim, load_authorizations, load_claims

# Provisional: hand-copied from the Harbor manual ("no more than 32 units per day
# across all HCBS services combined") until the versioned rule set exists.
DAILY_CAP = {"PY02": 32}


def check_claim(claim: Claim, view) -> dict[str, bool | None]:
    """Each check returns True (consistent), False (contradiction) or None (not applicable)."""
    paid = claim.status == PAID
    code = claim.denial_code
    fits_auth = None if view.units_remaining is None else claim.units <= view.units_remaining
    cap = DAILY_CAP.get(claim.payer_id)
    fits_day = None if cap is None else view.day_paid_units + claim.units <= cap
    is_dup = claim.frequency_code == 1 and bool(view.prior_submissions)

    return {
        "paid_fits_auth": (fits_auth is not False) if paid else None,
        "paid_fits_day_cap": (fits_day is not False) if paid else None,
        "paid_not_duplicate": (not is_dup) if paid else None,
        "d02_exceeds_auth": (fits_auth is False) if code == "D02" else None,
        "d06_exceeds_day_cap": (fits_day is False) if code == "D06" else None,
        "d05_has_prior_submission": is_dup if code == "D05" else None,
    }


def replay(history: list[Claim], authorizations) -> tuple[Ledger, list[dict]]:
    ledger = Ledger(authorizations)
    rows = []
    for claim in in_stream_order(history):
        view = ledger.view(claim)
        checks = check_claim(claim, view)
        entry = ledger.record(claim, PAID if claim.status == PAID else DENIED, ACTUAL)
        rows.append({
            "sequence": entry.sequence,
            "claim_id": claim.claim_id,
            "submitted_date": claim.submitted_date.isoformat(),
            "payer_id": claim.payer_id,
            "patient_id": claim.patient_id,
            "service_code": claim.service_code,
            "date_of_service": claim.date_of_service.isoformat(),
            "units": claim.units,
            "status": claim.status,
            "denial_code": claim.denial_code,
            "auth_id": claim.auth_id,
            "auth_remaining_before": entry.auth_remaining_before,
            "auth_remaining_after": entry.auth_remaining_after,
            "auth_drawn_by": " ".join(f"{d.claim_id}:{d.units}" for d in view.auth_draws),
            "day_paid_before": entry.day_paid_before,
            "day_paid_by": " ".join(f"{d.claim_id}:{d.service_code}:{d.units}" for d in view.day_paid_claims),
            "prior_submissions": " ".join(entry.prior_submissions),
            **{k: "" if v is None else str(v).lower() for k, v in checks.items()},
        })
    return ledger, rows


def summarise(rows: list[dict], history: list[Claim], to_check: list[Claim]) -> dict:
    check_names = [k for k in rows[0] if k.startswith(("paid_", "d0"))]
    summary = {}
    for name in check_names:
        c = Counter(r[name] for r in rows if r[name])
        summary[name] = {"consistent": c["true"], "contradicted": c["false"]}

    # A history claim can be contradicted because a claim-to-check, submitted
    # earlier on the same authorization or patient-day, is missing from the replay.
    earlier_check = {}
    for t in to_check:
        earlier_check.setdefault(("auth", t.auth_id), []).append(t)
        earlier_check.setdefault(("day", t.payer_id, t.patient_id, t.date_of_service), []).append(t)
    explained, unexplained = [], []
    for r in rows:
        failed = [k for k in check_names if r[k] == "false"]
        if not failed:
            continue
        sub = r["submitted_date"]
        keys = [("auth", r["auth_id"]), ("day", r["payer_id"], r["patient_id"], _date(r["date_of_service"]))]
        candidates = sorted({t.claim_id for k in keys for t in earlier_check.get(k, [])
                             if t.submitted_date.isoformat() < sub})
        (explained if candidates else unexplained).append(
            {"claim_id": r["claim_id"], "failed": failed, "earlier_claims_to_check": candidates})
    ties = order_sensitive_ties(list(history) + list(to_check))
    return {
        "checks": summary,
        "same_day_groups_sharing_state": {
            "groups": len(ties),
            "claims": len({cid for g in ties for cid in g}),
            "claims_to_check_involved": len({cid for g in ties for cid in g} & {t.claim_id for t in to_check}),
        },
        "contradictions_with_earlier_claim_to_check": explained,
        "contradictions_unexplained": unexplained,
    }


def _date(s):
    from datetime import date
    return date.fromisoformat(s)


def run(clean_dir: Path = config.CLEAN_DIR, out_dir: Path = config.LEDGER_DIR) -> dict:
    history = load_claims("claims_history", clean_dir)
    to_check = load_claims("claims_to_check", clean_dir)
    _, rows = replay(history, load_authorizations(clean_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "history_replay.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    summary = summarise(rows, history, to_check)
    with open(out_dir / "replay_summary.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")
    return summary


if __name__ == "__main__":
    s = run()
    print("Ledger replay of claims history (real outcomes recorded in submission order)")
    for name, c in s["checks"].items():
        print(f"  {name:28s} consistent {c['consistent']:5d}   contradicted {c['contradicted']}")
    print(f"  contradictions explained by an earlier claim-to-check: {len(s['contradictions_with_earlier_claim_to_check'])}")
    print(f"  contradictions unexplained: {len(s['contradictions_unexplained'])}")
    t = s["same_day_groups_sharing_state"]
    print(f"  same-day groups where the claim_id tie-break decides order: {t['groups']} "
          f"({t['claims']} claims, {t['claims_to_check_involved']} of them to check)")
