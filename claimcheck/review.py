"""Manual-review flags: named, deterministic reasons a prediction rests on an assumption.

Each flag says what could change if the assumption is wrong:
  status_may_change   paid vs denied could flip
  code_may_change     the claim is denied either way, but the reported code could differ

Flags (all computed from the evidence and ledger, never from a model):
  PENDING_EVV             a visit on the date of service is awaiting aggregator approval
  CORRECTED_CLAIM         frequency 7: history has no corrected claims to confirm how payers treat them
  ORDER_ASSUMPTION        a same-day claim shares this claim's authorization, daily total or
                          duplicate key, and swapping their order would change this outcome
  DEPENDS_ON_PREDICTED    a balance or daily-cap denial is caused by units drawn by other
                          claims-to-check, whose outcomes are predictions, not facts
  UNOBSERVED_PRECEDENCE   several rules fail and history never showed which of these codes
                          this payer reports first (not even through a chain: A over B, B over C)
  LEARNED_RULE            the reported code comes from a rule learned from history, not a document
  FIX_RIPPLE              set by predict: applying fixes to earlier claims changes this claim's outcome
  CONTRADICTED_BY_HISTORY this claim is predicted paid, but a later real payer decision on the same
                          authorization is only possible if it was not paid (see overdrawn_authorizations)
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .checker import Decision
from .ledger import Ledger, stream_key
from .models import Claim
from .ruleset import RULES_DIR, RuleSet

STATUS, CODE = "status_may_change", "code_may_change"


@dataclass(frozen=True)
class Flag:
    code: str
    effect: str
    reason: str


def transitive_closure(pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """If history shows A over B and B over C, the order of A and C is implied too."""
    closed = set(pairs)
    changed = True
    while changed:
        changed = False
        for a, b in list(closed):
            for c, d in list(closed):
                if b == c and (a, d) not in closed and a != d:
                    closed.add((a, d))
                    changed = True
    return closed


def load_observed_pairs(path: Path = RULES_DIR / "precedence_evidence.json") -> dict[str, set[tuple[str, str]]]:
    """payer -> {(winner, loser)} seen in history, directly or through a chain of observed pairs."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for pid, info in raw["payers"].items():
        out[pid] = transitive_closure({tuple(k.split(" over ")) for k in info["pairs"]})
    return out


def overdrawn_authorizations(ledger: Ledger) -> list[dict]:
    """Authorizations the ledger pushes below zero.

    History outcomes are facts and are recorded as the payer decided, so the
    balance can go negative only when an earlier *predicted* draw was wrong: the
    payer paid a later claim it couldn't have paid if the prediction were true.
    Each error lists the full trace and the predictions it contradicts.
    """
    by_auth: dict[str, list] = defaultdict(list)
    for e in ledger.entries:
        if e.auth_id:
            by_auth[e.auth_id].append(e)
    errors = []
    for auth_id, entries in sorted(by_auth.items()):
        negative = [e for e in entries if e.auth_remaining_after is not None and e.auth_remaining_after < 0]
        if not negative:
            continue
        first = negative[0]
        auth = ledger.authorizations[auth_id]
        used_actual = sum(e.units_drawn for e in entries if e.outcome_source == "actual")
        errors.append({
            "auth_id": auth_id,
            "payer_id": auth.payer_id,
            "patient_id": auth.patient_id,
            "service_code": auth.service_code,
            "units_authorized": auth.units_authorized,
            "overdrawn_by": -min(e.auth_remaining_after for e in negative),
            "used_by_paid_claims": used_actual,
            "left_from_paid_claims_only": auth.units_authorized - used_actual,
            "first_conflict_claim": first.claim_id,
            "contradicted_predictions": [e.claim_id for e in entries if e.sequence < first.sequence
                                         and e.outcome_source == "predicted" and e.units_drawn > 0],
            "trace": [{"claim_id": e.claim_id, "dataset": e.dataset, "submitted_date": e.submitted_date.isoformat(),
                       "outcome": e.outcome, "source": e.outcome_source, "units_drawn": e.units_drawn,
                       "remaining_after": e.auth_remaining_after} for e in entries],
        })
    return errors


class Reviewer:
    def __init__(self, ruleset: RuleSet, all_claims: list[Claim], ledger: Ledger, predicted_ids: set[str],
                 observed_pairs: dict[str, set[tuple[str, str]]] | None = None,
                 ledger_errors: list[dict] | None = None):
        self.ruleset = ruleset
        self.ledger = ledger
        self.contradicted = {cid: err for err in (ledger_errors or []) for cid in err["contradicted_predictions"]}
        self.predicted_ids = predicted_ids
        self.pairs = observed_pairs if observed_pairs is not None else load_observed_pairs()
        self.claims = {c.claim_id: c for c in all_claims}
        self.by_auth_day: dict[tuple, list[Claim]] = defaultdict(list)
        self.by_member_day: dict[tuple, list[Claim]] = defaultdict(list)
        self.by_dup_day: dict[tuple, list[Claim]] = defaultdict(list)
        for c in all_claims:
            if c.auth_id:
                self.by_auth_day[(c.auth_id, c.submitted_date)].append(c)
            self.by_member_day[Ledger.day_key(c) + (c.submitted_date,)].append(c)
            self.by_dup_day[c.duplicate_key + (c.submitted_date,)].append(c)

    def _paid_units(self, claim_id: str) -> int:
        e = self.ledger.outcome_of(claim_id)
        return e.units_drawn if e else 0

    def _order_flags(self, c: Claim, d: Decision) -> list[Flag]:
        flags = []
        view = d.evidence.ledger
        key = stream_key(c)

        def split(group):
            others = [o for o in group if o.claim_id != c.claim_id]
            before = [o for o in others if stream_key(o) < key]
            after = [o for o in others if stream_key(o) > key]
            return before, after

        def check(group, room_before, label):
            before, after = split(group)
            if not before and not after:
                return
            drawn_before = sum(self._paid_units(o.claim_id) for o in before)
            drawn_after = sum(self._paid_units(o.claim_id) for o in after)
            fits = c.units <= room_before
            if fits and room_before - drawn_after < c.units:
                flags.append(Flag("ORDER_ASSUMPTION", STATUS,
                                  f"Same-day claim(s) {', '.join(o.claim_id for o in after)} on the same {label} "
                                  f"are assumed to be processed after this one; if processed first, this claim fails."))
            elif not fits and room_before + drawn_before >= c.units:
                flags.append(Flag("ORDER_ASSUMPTION", STATUS,
                                  f"Same-day claim(s) {', '.join(o.claim_id for o in before)} on the same {label} "
                                  f"are assumed to be processed first; if processed after, this claim fits."))

        if view.auth_found:
            check(self.by_auth_day[(c.auth_id, c.submitted_date)], view.units_remaining, "authorization")
        payer = self.ruleset.for_payer(c.payer_id)
        cap = next((r.params["max_units"] for r in payer.rules if r.kind == "daily_unit_max"), None)
        if cap is not None:
            check(self.by_member_day[Ledger.day_key(c) + (c.submitted_date,)], cap - view.day_paid_units,
                  "member and day")
        dups = [o for o in self.by_dup_day[c.duplicate_key + (c.submitted_date,)] if o.claim_id != c.claim_id]
        if dups and c.frequency_code == 1:
            flags.append(Flag("ORDER_ASSUMPTION", STATUS,
                              f"Submitted the same day as {', '.join(o.claim_id for o in dups)} for the same member, "
                              f"service and date; which one counts as the duplicate depends on processing order."))
        return flags

    def flags_for(self, c: Claim, d: Decision) -> tuple[Flag, ...]:
        flags: list[Flag] = []
        ev = d.evidence
        pending = [v.visit_id for v in ev.unverified_visits if v.verification_status == "manual_edit_pending"]
        if pending:
            flags.append(Flag("PENDING_EVV", STATUS,
                              f"Visit {', '.join(pending)} is awaiting aggregator approval; once verified it "
                              f"counts as evidence and the outcome may change."))
        if c.claim_id in self.contradicted:
            err = self.contradicted[c.claim_id]
            left = err["left_from_paid_claims_only"]
            verdict = (f"With {c.units} units it would be denied D02." if c.units > left
                       else "It still fits in what is left.")
            flags.append(Flag("CONTRADICTED_BY_HISTORY", STATUS,
                              f"Predicted paid, but {err['auth_id']} later paid {err['first_conflict_claim']}, which "
                              f"is only possible if this claim did not draw its units. Counting only claims the "
                              f"payer really paid, {left} of {err['units_authorized']} units are left. {verdict}"))
        if c.is_corrected:
            flags.append(Flag("CORRECTED_CLAIM", STATUS,
                              f"Corrected claim for {c.original_claim_id}. History has no corrected claims, so "
                              f"payer handling is taken from billing_basics.md, not observed."))
        flags.extend(self._order_flags(c, d))

        ledger_rules = [r for r in d.failed if r.rule_id.endswith(("AUTH-BALANCE", "DAILY-MAX"))]
        if ledger_rules:
            led = ev.ledger
            predicted = sorted({x.claim_id for x in led.auth_draws + led.day_paid_claims
                                if x.claim_id in self.predicted_ids})
            if predicted:
                flags.append(Flag("DEPENDS_ON_PREDICTED", STATUS,
                                  f"Units were drawn by claims-to-check {', '.join(predicted)}, which are "
                                  f"predicted paid, not known to be paid."))

        codes = []
        for r in d.failed:
            if r.denial_code not in codes:
                codes.append(r.denial_code)
        if len(codes) > 1:
            seen = self.pairs.get(c.payer_id, set())
            unseen = [o for o in codes[1:] if (codes[0], o) not in seen]
            if unseen:
                flags.append(Flag("UNOBSERVED_PRECEDENCE", CODE,
                                  f"Also fails {', '.join(unseen)}; history never showed whether this payer "
                                  f"reports {codes[0]} or {unseen[0]} first."))

        if d.failed:
            top = next(x for x in self.ruleset.for_payer(c.payer_id).rules if x.rule_id == d.reported_rule)
            if top.source.get("type") == "learned_from_history":
                flags.append(Flag("LEARNED_RULE", CODE,
                                  f"{top.rule_id} is learned from payer remarks and history, not a published "
                                  f"document. Summit sometimes reports this denial as D99."))
        return tuple(flags)
