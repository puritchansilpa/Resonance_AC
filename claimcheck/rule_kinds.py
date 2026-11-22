"""The rule types the engine understands.

A payer's rule file (rules/PY0x.json) picks rule types from this list and fills
in their parameters. Each rule type is a plain function:

    check(claim, evidence, params) -> Outcome(passed, detail, facts)

`passed` is True or False; `detail` is one sentence a biller can read; `facts`
holds the numbers behind it. The same inputs always give the same outcome.

Adding a payer means writing a rule file, not code, as long as its rules fit
these types. A rule that doesn't fit is a new type here, reviewed like any code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .evidence import Evidence
from .models import Claim

UNIT_MINUTES = 15


@dataclass(frozen=True)
class Outcome:
    passed: bool
    detail: str
    facts: dict = field(default_factory=dict)


def units_from_minutes(minutes: int, rounding: str) -> int:
    """Convert verified minutes to billable 15-minute units under a payer's rounding."""
    full, remainder = divmod(minutes, UNIT_MINUTES)
    if rounding == "whole_units_only":
        return full
    if rounding == "eight_minute_rule":
        return full + (1 if remainder >= 8 else 0)
    raise ValueError(f"unknown rounding {rounding!r}")


def _evv_required(claim: Claim, ev: Evidence, p: dict) -> Outcome:
    n = len(ev.verified_visits)
    facts = {"verified_visits": [v.visit_id for v in ev.verified_visits],
             "unverified_visits": [f"{v.visit_id} ({v.verification_status})" for v in ev.unverified_visits]}
    if n:
        return Outcome(True, f"{n} verified EVV visit(s) on {claim.date_of_service}.", facts)
    if ev.unverified_visits:
        states = ", ".join(sorted({v.verification_status for v in ev.unverified_visits}))
        return Outcome(False, f"Visits on {claim.date_of_service} exist but none is verified ({states}).", facts)
    return Outcome(False, f"No EVV visit for {claim.patient_id} {claim.service_code} on {claim.date_of_service}.", facts)


def _units_within_verified_time(claim: Claim, ev: Evidence, p: dict) -> Outcome:
    if not ev.verified_visits:
        return Outcome(True, "No verified time to compare (covered by the EVV rule).", {"not_applicable": True})
    supported = units_from_minutes(ev.verified_minutes, p["rounding"])
    facts = {"verified_minutes": ev.verified_minutes, "rounding": p["rounding"],
             "supported_units": supported, "billed_units": claim.units}
    if claim.units <= supported:
        return Outcome(True, f"{claim.units} units billed; {ev.verified_minutes} verified minutes support {supported}.", facts)
    return Outcome(False, f"{claim.units} units billed but {ev.verified_minutes} verified minutes "
                          f"support only {supported} ({p['rounding'].replace('_', ' ')}).", facts)


def _auth_required(claim: Claim, ev: Evidence, p: dict) -> Outcome:
    if claim.auth_id:
        return Outcome(True, f"Quotes authorization {claim.auth_id}.")
    return Outcome(False, "No prior authorization number on the claim.")


def _auth_matches(claim: Claim, ev: Evidence, p: dict) -> Outcome:
    if ev.auth is None:
        return Outcome(True, "No authorization quoted (covered by the missing-authorization rule).", {"not_applicable": True})
    facts = {"auth_id": ev.auth.auth_id, "mismatches": list(ev.auth.mismatches)}
    if not ev.auth.mismatches:
        return Outcome(True, f"{ev.auth.auth_id} covers this member, service and date.", facts)
    return Outcome(False, f"{ev.auth.auth_id} does not fit the claim: " + "; ".join(ev.auth.mismatches) + ".", facts)


def _auth_balance(claim: Claim, ev: Evidence, p: dict) -> Outcome:
    led = ev.ledger
    if not led.auth_found:
        return Outcome(True, "No authorization on file to draw from (covered by other rules).", {"not_applicable": True})
    facts = {"auth_id": led.auth_id, "units_authorized": led.units_authorized,
             "units_used": led.units_used, "units_remaining": led.units_remaining,
             "used_by": [f"{d.claim_id}:{d.units}" for d in led.auth_draws], "billed_units": claim.units}
    if claim.units <= led.units_remaining:
        return Outcome(True, f"{led.units_remaining} of {led.units_authorized} units left on {led.auth_id}; "
                             f"{claim.units} billed.", facts)
    if not led.auth_draws:
        return Outcome(False, f"{claim.units} units billed but {led.auth_id} is for only "
                              f"{led.units_authorized} units in total.", facts)
    used_by = ", ".join(f"{d.claim_id} ({d.units})" for d in led.auth_draws)
    return Outcome(False, f"{claim.units} units billed but only {led.units_remaining} of {led.units_authorized} "
                          f"left on {led.auth_id}; already used by paid claims {used_by}.", facts)


def _timely_filing(claim: Claim, ev: Evidence, p: dict) -> Outcome:
    facts = {"days_to_submit": ev.days_to_submit, "limit_days": p["days"]}
    if ev.days_to_submit <= p["days"]:
        return Outcome(True, f"Submitted {ev.days_to_submit} days after care (limit {p['days']}).", facts)
    return Outcome(False, f"Submitted {ev.days_to_submit} days after care; limit is {p['days']}.", facts)


def _modifier_required(claim: Claim, ev: Evidence, p: dict) -> Outcome:
    if claim.service_code not in p["service_codes"]:
        return Outcome(True, "Service needs no modifier.", {"not_applicable": True})
    days = p.get("days_of_week")
    if days and ev.day_of_week not in days:
        return Outcome(True, f"Rule is for {'/'.join(days)}; care was on a {ev.day_of_week}.", {"not_applicable": True})
    facts = {"required": p["modifier"], "billed": claim.modifier or None, "day_of_week": ev.day_of_week}
    when = f" on a {ev.day_of_week}" if days else ""
    if claim.modifier == p["modifier"]:
        return Outcome(True, f"{claim.service_code}{when} billed with {p['modifier']} as required.", facts)
    billed = claim.modifier or "no modifier"
    return Outcome(False, f"{claim.service_code}{when} needs modifier {p['modifier']}; billed with {billed}.", facts)


def _daily_unit_max(claim: Claim, ev: Evidence, p: dict) -> Outcome:
    led = ev.ledger
    total = led.day_paid_units + claim.units
    facts = {"max_units": p["max_units"], "already_paid": led.day_paid_units, "billed_units": claim.units,
             "paid_that_day_by": [f"{d.claim_id}:{d.service_code}:{d.units}" for d in led.day_paid_claims]}
    if total <= p["max_units"]:
        return Outcome(True, f"{led.day_paid_units} units already paid that day + {claim.units} = {total} "
                             f"(max {p['max_units']}).", facts)
    return Outcome(False, f"{led.day_paid_units} units already paid for this member that day + {claim.units} "
                          f"= {total}, over the {p['max_units']}-unit daily maximum.", facts)


def _no_duplicates(claim: Claim, ev: Evidence, p: dict) -> Outcome:
    prior = list(ev.ledger.prior_submissions)
    facts = {"prior_submissions": prior, "frequency_code": claim.frequency_code}
    if claim.is_corrected:
        return Outcome(True, f"Corrected claim (frequency 7) for {claim.original_claim_id}; not a duplicate.", facts)
    if not prior:
        return Outcome(True, "No earlier submission for this member, service and date.", facts)
    return Outcome(False, f"Same member, service and date already submitted as {', '.join(prior)}.", facts)


def _whole_hours(claim: Claim, ev: Evidence, p: dict) -> Outcome:
    per_hour = p["units_per_hour"]
    facts = {"billed_units": claim.units, "units_per_hour": per_hour}
    if claim.units % per_hour == 0:
        return Outcome(True, f"{claim.units} units = {claim.units // per_hour} whole hour(s).", facts)
    return Outcome(False, f"{claim.units} units is not a whole number of hours "
                          f"(must be a multiple of {per_hour}).", facts)


@dataclass(frozen=True)
class RuleKind:
    check: Callable[[Claim, Evidence, dict], Outcome]
    params: dict[str, type]           # required parameter -> type
    optional: dict[str, type] = field(default_factory=dict)


RULE_KINDS: dict[str, RuleKind] = {
    "evv_required": RuleKind(_evv_required, {}),
    "units_within_verified_time": RuleKind(_units_within_verified_time, {"rounding": str}),
    "auth_required": RuleKind(_auth_required, {}),
    "auth_matches": RuleKind(_auth_matches, {}),
    "auth_balance": RuleKind(_auth_balance, {}),
    "timely_filing": RuleKind(_timely_filing, {"days": int}),
    "modifier_required": RuleKind(_modifier_required, {"service_codes": list, "modifier": str},
                                  {"days_of_week": list}),
    "daily_unit_max": RuleKind(_daily_unit_max, {"max_units": int}),
    "no_duplicates": RuleKind(_no_duplicates, {}),
    "whole_hours": RuleKind(_whole_hours, {"units_per_hour": int}),
}

ROUNDINGS = {"whole_units_only", "eight_minute_rule"}
