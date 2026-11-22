"""Fixer: turn a predicted denial into the edits that would get the claim paid.

For each failed rule there is a fix action (by rule type). The fixer applies the
actions, re-runs the full checker on the edited claim against the same ledger
state, and repeats until the claim passes, a blocker is hit, or nothing changes.
Re-running everything matters: cutting units to clear D09 can expose D10
(Summit whole hours); swapping the authorization brings in a new balance (D02).

Each edit is labelled by what it means for the agency:
  correction          fixes a data error, no change in money (modifier, authorization
                      number, marking a resubmission as a corrected claim)
  required_reduction  bill only what the evidence supports (D09 verified time, D10 whole hours)
  business_choice     bill fewer units than were delivered because a limit is reached
                      (D02 authorization balance, D06 daily cap); the alternative is to
                      ask the payer for more units, so a person decides

Some denials can't be fixed by editing the claim (blockers): late filing, no
verified visit, an authorization that doesn't exist, a duplicate of a paid claim.

Plan outcomes:
  payable_as_is   predicted paid, nothing to do
  fixable         edits are corrections / required reductions only, and the edited claim passes
  needs_decision  passes only with a business_choice edit
  not_fixable     a blocker remains
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import config
from .checker import Checker, Decision, RuleResult
from .ledger import PAID, Ledger
from .models import Claim

MAX_ROUNDS = 6
CORRECTION, REQUIRED, CHOICE = "correction", "required_reduction", "business_choice"
PAYABLE, FIXABLE, NEEDS_DECISION, NOT_FIXABLE = "payable_as_is", "fixable", "needs_decision", "not_fixable"


@dataclass(frozen=True)
class Change:
    rule_id: str
    denial_code: str
    field: str
    old: object
    new: object
    kind: str
    reason: str


@dataclass(frozen=True)
class FixPlan:
    claim_id: str
    outcome: str
    changes: tuple[Change, ...]
    blockers: tuple[str, ...]
    rounds: int
    fixed_claim: Claim
    final_decision: Decision
    billed_before: float
    billed_after: float

    def summary(self) -> str:
        if self.outcome == PAYABLE:
            return "Predicted paid; no change needed."
        edits = "; ".join(f"{c.field} {c.old or 'blank'} -> {c.new}" for c in self.changes)
        if self.outcome == NOT_FIXABLE:
            also = f" Also needed: {edits}." if edits else ""
            return "Cannot be fixed by editing the claim: " + " ".join(self.blockers) + also
        money = ""
        if self.billed_after != self.billed_before:
            money = f" Billed amount {self.billed_before:.2f} -> {self.billed_after:.2f}."
        lead = "Decide before resubmitting:" if self.outcome == NEEDS_DECISION else "Fix:"
        return f"{lead} {edits}.{money}"


@dataclass(frozen=True)
class _Action:
    field: str
    new: object
    kind: str
    reason: str


@dataclass(frozen=True)
class _Blocker:
    reason: str


def _amount(units: int, service: str) -> float:
    return round(units * config.SERVICE_RATES[service], 2)


class Fixer:
    def __init__(self, checker: Checker):
        self.checker = checker

    # ------------------------------------------------------------- actions
    def _find_auth(self, claim: Claim, ledger: Ledger):
        return sorted(
            (a for a in ledger.authorizations.values()
             if a.patient_id == claim.patient_id and a.payer_id == claim.payer_id
             and a.service_code == claim.service_code and a.covers(claim.date_of_service)),
            key=lambda a: a.auth_id)

    def _action(self, claim: Claim, r: RuleResult, d: Decision, ledger: Ledger):
        kind = next(x.kind for x in self.checker.ruleset.for_payer(claim.payer_id).rules if x.rule_id == r.rule_id)
        f = r.facts

        if kind == "modifier_required":
            return _Action("modifier", f["required"], CORRECTION,
                           f"{claim.service_code} on a {f['day_of_week']} needs modifier {f['required']}.")

        if kind in ("auth_required", "auth_matches"):
            found = self._find_auth(claim, ledger)
            if len(found) == 1:
                return _Action("auth_id", found[0].auth_id, CORRECTION,
                               f"{found[0].auth_id} covers this member, service and date "
                               f"({found[0].start_date} to {found[0].end_date}).")
            if not found:
                return _Blocker("No authorization on file covers this member, service and date; "
                                "request one from the payer before billing.")
            return _Blocker(f"Several authorizations could apply ({', '.join(a.auth_id for a in found)}); "
                            "choose one.")

        if kind == "units_within_verified_time":
            if f["supported_units"] <= 0:
                return _Blocker(f"Verified time ({f['verified_minutes']} min) supports no billable unit.")
            return _Action("units", f["supported_units"], REQUIRED,
                           f"{f['verified_minutes']} verified minutes support {f['supported_units']} units "
                           f"({f['rounding'].replace('_', ' ')}).")

        if kind == "whole_hours":
            per = f["units_per_hour"]
            units = (claim.units // per) * per
            if units <= 0:
                return _Blocker(f"Less than one whole hour billable; Summit pays whole hours only.")
            return _Action("units", units, REQUIRED, f"Round down to whole hours ({units // per} h = {units} units).")

        if kind == "auth_balance":
            left = f["units_remaining"]
            if left <= 0:
                return _Blocker(f"{f['auth_id']} is used up (0 of {f['units_authorized']} left); "
                                "ask the payer for more units.")
            return _Action("units", left, CHOICE,
                           f"Only {left} units left on {f['auth_id']}: bill {left} now, or ask the payer "
                           f"to increase the authorization.")

        if kind == "daily_unit_max":
            room = f["max_units"] - f["already_paid"]
            if room <= 0:
                return _Blocker(f"Member already has {f['already_paid']} paid units that day "
                                f"(max {f['max_units']}).")
            return _Action("units", room, CHOICE,
                           f"Daily maximum {f['max_units']} with {f['already_paid']} already paid: "
                           f"bill {room} units, or bill a different day's care.")

        if kind == "timely_filing":
            return _Blocker(f"Submitted {f['days_to_submit']} days after care; the limit is {f['limit_days']}. "
                            "Editing the claim can't fix this.")

        if kind == "evv_required":
            pending = [v for v in d.evidence.unverified_visits if v.verification_status == "manual_edit_pending"]
            if pending:
                ids = ", ".join(v.visit_id for v in pending)
                return _Blocker(f"Visit {ids} is awaiting aggregator approval (manual_edit_pending); "
                                "hold the claim until it is verified.")
            if d.evidence.unverified_visits:
                ids = ", ".join(v.visit_id for v in d.evidence.unverified_visits)
                return _Blocker(f"Visit {ids} was rejected; correct the EVV record before billing.")
            return _Blocker("No EVV visit recorded for this date; do not bill unless the visit is found.")

        if kind == "no_duplicates":
            prior = f["prior_submissions"]
            outcomes = {p: ledger.outcome_of(p) for p in prior}
            if any(o is not None and o.outcome == PAID for o in outcomes.values()):
                paid = [p for p, o in outcomes.items() if o is not None and o.outcome == PAID]
                return _Blocker(f"Already billed and paid (or predicted paid) as {', '.join(paid)}; do not submit.")
            last = prior[-1]
            return _Action("frequency_code", 7, CORRECTION,
                           f"{last} was denied: resubmit as a corrected claim (frequency 7, original {last}).")

        return _Blocker(f"No fix known for rule type {kind}.")

    # ---------------------------------------------------------------- loop
    def plan(self, claim: Claim, decision: Decision, ledger: Ledger) -> FixPlan:
        """Build a fix plan. `ledger` must be at the state the claim was judged on."""
        before = _amount(claim.units, claim.service_code)
        if decision.status == PAID:
            return FixPlan(claim.claim_id, PAYABLE, (), (), 0, claim, decision, before, before)

        current, d = claim, decision
        changes: list[Change] = []
        blockers: list[str] = []
        rounds = 0
        while d.status != PAID and rounds < MAX_ROUNDS:
            rounds += 1
            edits: dict[str, tuple[_Action, RuleResult]] = {}
            for r in d.failed:
                act = self._action(current, r, d, ledger)
                if isinstance(act, _Blocker):
                    if act.reason not in blockers:
                        blockers.append(act.reason)
                    continue
                prev = edits.get(act.field)
                # Several rules may cut units: keep the lowest, so all of them are cleared.
                if prev is None or (act.field == "units" and act.new < prev[0].new):
                    edits[act.field] = (act, r)
            # Keep applying editable fixes even when a blocker exists, so the plan
            # lists everything to correct once the blocker is resolved.
            if not edits:
                break
            updates = {}
            for fld, (act, r) in sorted(edits.items()):
                old = getattr(current, fld)
                if old == act.new:
                    continue
                updates[fld] = act.new
                changes.append(Change(r.rule_id, r.denial_code, fld, old, act.new, act.kind, act.reason))
                if fld == "frequency_code":
                    updates["original_claim_id"] = r.facts["prior_submissions"][-1]
            if not updates:
                break
            if "units" in updates:
                updates["billed_amount"] = _amount(updates["units"], current.service_code)
            current = replace(current, **updates)
            d = self.checker.check(current, ledger.view(current))

        if d.status == PAID and not blockers:
            outcome = NEEDS_DECISION if any(c.kind == CHOICE for c in changes) else FIXABLE
        else:
            outcome = NOT_FIXABLE
            if not blockers:
                blockers.append("Edits did not clear every rule: " + "; ".join(r.detail for r in d.failed))
        return FixPlan(claim.claim_id, outcome, tuple(changes), tuple(blockers), rounds, current, d,
                       before, _amount(current.units, current.service_code))
