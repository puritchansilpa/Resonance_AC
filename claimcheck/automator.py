"""Automator: draft claims for verified visits that are on no claim.

A verified EVV visit is evidence of billable care. When no claim (past or
waiting) covers its member, service and date of service, the automator drafts
one, using only the rules and records already on file:

  payer, authorization  the authorization covering the member, service and date
  units                 same-day verified minutes, converted with the payer's rounding,
                        then rounded down to whole hours where a whole-hours rule is in force
  modifier              the modifier a modifier rule in force requires for that day
  file by               date of service + the filing limit in force (none for Harbor)

Each draft then goes through the same checker as any claim, dated AS_OF (the
day the drafts would be filed), against the current authorization balances.
Nothing is submitted: drafts are proposals for a biller.

Run: python -m claimcheck.automator   -> output/automator/drafts.csv
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from . import config
from .checker import Checker, Decision
from .ledger import Ledger
from .models import Authorization, Claim, Visit
from .rule_kinds import units_from_minutes

# The day drafts are dated, fixed so results are repeatable. The data pack has no "today";
# this is the date the exercise was run.
AS_OF = date(2026, 11, 22)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Draft:
    key: str                    # PT0021|S5125|2026-10-08
    patient_id: str
    service_code: str
    date_of_service: date
    visit_ids: tuple[str, ...]
    minutes: int
    payer_id: str | None
    auth_id: str | None
    units: int
    units_note: str
    modifier: str
    amount: float
    file_by: date | None
    days_left: int | None
    decision: Decision | None   # None when no authorization covers the visit
    outcome: str                # ready | late | auth_short | other_denial | no_auth | no_units

    @property
    def claim(self) -> Claim | None:
        if not self.payer_id:
            return None
        return Claim(f"DRAFT-{self.key}", "draft", "", self.payer_id, self.patient_id, self.service_code,
                     self.date_of_service, AS_OF, self.units, self.auth_id or "", self.modifier, 1, "",
                     self.amount, "", "", "")


def unbilled_groups(visits: list[Visit], claims: list[Claim]) -> dict[tuple, list[Visit]]:
    """Verified visits whose member, service and date appear on no claim, grouped by that key."""
    billed = {(c.patient_id, c.service_code, c.date_of_service) for c in claims}
    groups: dict[tuple, list[Visit]] = defaultdict(list)
    for v in visits:
        if v.verification_status == "verified" and (v.patient_id, v.service_code, v.service_date) not in billed:
            groups[(v.patient_id, v.service_code, v.service_date)].append(v)
    return groups


def _rules_in_force(checker: Checker, payer_id: str, claim: Claim):
    return [r for r in checker.ruleset.for_payer(payer_id).rules if r.in_force(claim)]


def draft_one(key: tuple, group: list[Visit], authorizations: dict[str, Authorization],
              checker: Checker, ledger: Ledger, as_of: date = AS_OF) -> Draft:
    pid, svc, dos = key
    minutes = sum(v.duration_minutes for v in group)
    label = f"{pid}|{svc}|{dos.isoformat()}"
    vids = tuple(sorted(v.visit_id for v in group))
    auth = next((a for a in sorted(authorizations.values(), key=lambda a: a.auth_id)
                 if a.patient_id == pid and a.service_code == svc and a.covers(dos)), None)
    if auth is None:
        return Draft(label, pid, svc, dos, vids, minutes, None, None, 0, "", "", 0.0, None, None, None, "no_auth")

    probe = Claim("probe", "draft", "", auth.payer_id, pid, svc, dos, as_of, 1, auth.auth_id, "", 1, "", 0, "", "", "")
    rules = _rules_in_force(checker, auth.payer_id, probe)
    rounding = next((r.params["rounding"] for r in rules if r.kind == "units_within_verified_time"), "whole_units_only")
    units = units_from_minutes(minutes, rounding)
    note = f"{minutes} min, {rounding.replace('_', ' ')} = {units} units"
    hours = next((r for r in rules if r.kind == "whole_hours"), None)
    if hours:
        per = hours.params["units_per_hour"]
        if units % per:
            units = units // per * per
            note += f", whole hours = {units}"
    modifier = ""
    day = WEEKDAYS[dos.weekday()]
    for r in rules:
        if r.kind == "modifier_required" and svc in r.params["service_codes"]:
            if "days_of_week" not in r.params or day in r.params["days_of_week"]:
                modifier = r.params["modifier"]
    limit = next((r.params["days"] for r in rules if r.kind == "timely_filing"), None)
    file_by = dos + timedelta(days=limit) if limit is not None else None
    amount = round(units * config.SERVICE_RATES[svc], 2)
    if units == 0:
        return Draft(label, pid, svc, dos, vids, minutes, auth.payer_id, auth.auth_id, 0, note, modifier, 0.0,
                     file_by, (file_by - as_of).days if file_by else None, None, "no_units")
    claim = Claim(f"DRAFT-{label}", "draft", "", auth.payer_id, pid, svc, dos, as_of, units, auth.auth_id,
                  modifier, 1, "", amount, "", "", "")
    decision = checker.check(claim, ledger.view(claim))
    codes = {r.denial_code for r in decision.failed}
    outcome = ("ready" if decision.status == "paid" else "late" if "D03" in codes
               else "auth_short" if decision.denial_code == "D02" else "other_denial")
    return Draft(label, pid, svc, dos, vids, minutes, auth.payer_id, auth.auth_id, units, note, modifier, amount,
                 file_by, (file_by - as_of).days if file_by else None, decision, outcome)


def draft_all(visits, claims, authorizations, checker, ledger, as_of: date = AS_OF) -> list[Draft]:
    """Drafts for every unbilled group, those due soonest first (no filing limit last)."""
    drafts = [draft_one(k, g, authorizations, checker, ledger, as_of)
              for k, g in unbilled_groups(visits, claims).items()]
    return sorted(drafts, key=lambda d: (d.outcome != "ready", d.days_left if d.days_left is not None else 10**6,
                                         d.date_of_service, d.key))


def summary(drafts: list[Draft]) -> dict:
    out = {}
    for d in drafts:
        s = out.setdefault(d.outcome, {"drafts": 0, "amount": 0.0})
        s["drafts"] += 1
        s["amount"] = round(s["amount"] + d.amount, 2)
    return out


def main() -> int:
    from .predict import run_all
    from .evidence import EvidenceBuilder
    from .models import load_authorizations, load_visits

    r = run_all()
    visits, auths = load_visits(), load_authorizations()
    checker = Checker(r.ruleset, EvidenceBuilder(visits, auths))
    drafts = draft_all(visits, r.history + r.to_check, auths, checker, r.ledger)
    out = config.OUTPUT_DIR / "automator"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "drafts.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["key", "visits", "payer_id", "auth_id", "units", "units_note", "modifier", "amount",
                    "file_by", "days_left", "outcome", "predicted_code", "reason"])
        for d in drafts:
            w.writerow([d.key, " ".join(d.visit_ids), d.payer_id or "", d.auth_id or "", d.units, d.units_note,
                        d.modifier, f"{d.amount:.2f}", d.file_by or "", "" if d.days_left is None else d.days_left,
                        d.outcome, d.decision.denial_code if d.decision else "",
                        d.decision.failed[0].detail if d.decision and d.decision.failed else ""])
    print(f"{len(drafts)} drafts as of {AS_OF}: {summary(drafts)} -> {out / 'drafts.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
