"""Ledger engine: the payer's running state, rebuilt claim by claim.

Three denial codes depend on other claims, not just the one being checked:

  D02  units exceed the authorization's remaining units
       -> needs units drawn by earlier PAID claims on the same authorization
  D06  daily unit maximum exceeded
       -> needs units already PAID for that member, payer and date of service
  D05  duplicate claim
       -> needs every earlier SUBMISSION with the same payer, member, service, date

The ledger keeps those three tallies. It does not decide anything: before a claim
is judged, `view(claim)` returns what the payer would know at that moment; after
the claim is judged (or its real outcome is known), `record(claim, outcome)` updates
the tallies. Rules live elsewhere and read the view.

Ordering: payers process claims in the order they are submitted. Claims are
replayed by (submitted_date, claim_id). The claim_id tie-break is an assumption:
the data has no submission time of day, and claim IDs are not issued in
submission order. `record` refuses claims that arrive out of this order.

Every `record` returns a LedgerEntry with the balances before and after, and
the view names the exact claims that used up an authorization or a day's cap,
so a D02 or D06 can always be explained by pointing at other claims.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

from .models import Authorization, Claim

PAID, DENIED = "paid", "denied"
ACTUAL, PREDICTED = "actual", "predicted"


class LedgerError(RuntimeError):
    pass


def stream_key(claim: Claim) -> tuple[date, str]:
    """The order in which the payer processes claims."""
    return (claim.submitted_date, claim.claim_id)


def in_stream_order(claims: Iterable[Claim]) -> list[Claim]:
    return sorted(claims, key=stream_key)


def order_sensitive_ties(claims: Iterable[Claim]) -> list[tuple[str, ...]]:
    """Groups of claims submitted on the same day that share ledger state
    (same authorization, or same payer + member + date of service).

    Within such a group the claim_id tie-break decides who draws first, so a
    prediction for these claims rests on an assumption and should say so.
    """
    groups: dict[tuple, list[str]] = defaultdict(list)
    for c in claims:
        if c.auth_id:
            groups[("auth", c.auth_id, c.submitted_date)].append(c.claim_id)
        groups[("day", c.payer_id, c.patient_id, c.date_of_service, c.submitted_date)].append(c.claim_id)
        groups[("dup",) + c.duplicate_key + (c.submitted_date,)].append(c.claim_id)
    return sorted({tuple(sorted(ids)) for ids in groups.values() if len(ids) > 1})


@dataclass(frozen=True)
class Draw:
    """A paid claim that used units from an authorization or from a day's total."""
    claim_id: str
    units: int
    submitted_date: date
    service_code: str


@dataclass(frozen=True)
class LedgerView:
    """What the payer knows about other claims at the moment this claim arrives."""
    claim_id: str
    sequence: int                       # position in the stream (0-based)
    # Authorization balance (the authorization the claim quotes)
    auth_id: str
    auth_found: bool
    units_authorized: int | None
    units_used: int | None
    units_remaining: int | None
    auth_draws: tuple[Draw, ...]        # earlier paid claims on this authorization
    # Daily total for the member with this payer on this date of service, all services
    day_paid_units: int
    day_paid_claims: tuple[Draw, ...]
    # Earlier submissions with the same payer, member, service and date of service
    prior_submissions: tuple[str, ...]


@dataclass(frozen=True)
class LedgerEntry:
    """One line of the ledger's audit trail."""
    sequence: int
    claim_id: str
    dataset: str
    submitted_date: date
    outcome: str                        # paid / denied
    outcome_source: str                 # actual (history) / predicted
    auth_id: str
    units_drawn: int
    auth_remaining_before: int | None
    auth_remaining_after: int | None
    day_paid_before: int
    day_paid_after: int
    prior_submissions: tuple[str, ...]


class Ledger:
    def __init__(self, authorizations: Mapping[str, Authorization]):
        self._auths = dict(authorizations)
        self._auth_draws: dict[str, list[Draw]] = defaultdict(list)
        self._day_draws: dict[tuple, list[Draw]] = defaultdict(list)
        self._submissions: dict[tuple, list[str]] = defaultdict(list)
        self._last_key: tuple | None = None
        self._outcomes: dict[str, LedgerEntry] = {}
        self.entries: list[LedgerEntry] = []

    @property
    def authorizations(self) -> Mapping[str, Authorization]:
        return self._auths

    def outcome_of(self, claim_id: str) -> LedgerEntry | None:
        """The recorded outcome of an earlier claim (actual or predicted), if any."""
        return self._outcomes.get(claim_id)

    # ------------------------------------------------------------------ reads
    @staticmethod
    def day_key(claim: Claim) -> tuple:
        return (claim.payer_id, claim.patient_id, claim.date_of_service)

    def units_remaining(self, auth_id: str) -> int | None:
        auth = self._auths.get(auth_id)
        if auth is None:
            return None
        return auth.units_authorized - sum(d.units for d in self._auth_draws[auth_id])

    def view(self, claim: Claim) -> LedgerView:
        auth = self._auths.get(claim.auth_id) if claim.auth_id else None
        draws = tuple(self._auth_draws[claim.auth_id]) if auth else ()
        used = sum(d.units for d in draws) if auth else None
        day = tuple(self._day_draws[self.day_key(claim)])
        return LedgerView(
            claim_id=claim.claim_id,
            sequence=len(self.entries),
            auth_id=claim.auth_id,
            auth_found=auth is not None,
            units_authorized=auth.units_authorized if auth else None,
            units_used=used,
            units_remaining=(auth.units_authorized - used) if auth else None,
            auth_draws=draws,
            day_paid_units=sum(d.units for d in day),
            day_paid_claims=day,
            prior_submissions=tuple(self._submissions[claim.duplicate_key]),
        )

    # ----------------------------------------------------------------- writes
    def record(self, claim: Claim, outcome: str, source: str) -> LedgerEntry:
        if outcome not in (PAID, DENIED):
            raise LedgerError(f"{claim.claim_id}: outcome must be paid or denied, got {outcome!r}")
        if source not in (ACTUAL, PREDICTED):
            raise LedgerError(f"{claim.claim_id}: source must be actual or predicted, got {source!r}")
        if claim.claim_id in self._outcomes:
            raise LedgerError(f"{claim.claim_id} recorded twice")
        key = stream_key(claim)
        if self._last_key is not None and key < self._last_key:
            raise LedgerError(f"{claim.claim_id} recorded out of submission order")

        before = self.view(claim)
        drawn = 0
        if outcome == PAID:
            if not before.auth_found:
                raise LedgerError(f"{claim.claim_id}: a paid claim must quote a known authorization")
            draw = Draw(claim.claim_id, claim.units, claim.submitted_date, claim.service_code)
            self._auth_draws[claim.auth_id].append(draw)
            self._day_draws[self.day_key(claim)].append(draw)
            drawn = claim.units
        # Every submission counts for duplicate checks, whatever its outcome.
        self._submissions[claim.duplicate_key].append(claim.claim_id)

        entry = LedgerEntry(
            sequence=before.sequence,
            claim_id=claim.claim_id,
            dataset=claim.dataset,
            submitted_date=claim.submitted_date,
            outcome=outcome,
            outcome_source=source,
            auth_id=claim.auth_id,
            units_drawn=drawn,
            auth_remaining_before=before.units_remaining,
            auth_remaining_after=self.units_remaining(claim.auth_id) if before.auth_found else None,
            day_paid_before=before.day_paid_units,
            day_paid_after=before.day_paid_units + drawn,
            prior_submissions=before.prior_submissions,
        )
        self.entries.append(entry)
        self._outcomes[claim.claim_id] = entry
        self._last_key = key
        return entry


def simulate(history: Iterable[Claim], to_check: Iterable[Claim],
             authorizations: Mapping[str, Authorization], decide,
             observe=None, ledger: Ledger | None = None) -> tuple[Ledger, dict[str, LedgerView]]:
    """Run history and claims-to-check through one ledger, in submission order.

    History claims are recorded with their real outcome. For each claim-to-check,
    `decide(claim, view)` returns "paid" or "denied", and that prediction is
    recorded so later claims see it. The two files overlap in time, so this
    must be one stream: a claim-to-check submitted in August must see history
    claims from July, and history claims from September already reflect it.

    If `observe(claim, view)` is given, it is called for each history claim
    before its real outcome is recorded (used by the backtest to judge history
    claims on exactly the state the payer saw).

    Pass an empty `ledger` to keep a handle on it while the stream runs (the
    fixer uses it to re-check an edited claim against the same state).

    Returns the ledger (with its audit trail) and the view each claim-to-check
    was judged on.
    """
    ledger = ledger if ledger is not None else Ledger(authorizations)
    if ledger.entries:
        raise LedgerError("simulate needs an empty ledger")
    views: dict[str, LedgerView] = {}
    for claim in in_stream_order(list(history) + list(to_check)):
        if claim.dataset == "history":
            if observe is not None:
                observe(claim, ledger.view(claim))
            ledger.record(claim, PAID if claim.status == PAID else DENIED, ACTUAL)
        else:
            view = ledger.view(claim)
            views[claim.claim_id] = view
            result = decide(claim, view)
            # decide may return (edited_claim, outcome) to record a fixed version of the claim.
            recorded, outcome = result if isinstance(result, tuple) else (claim, result)
            if recorded.claim_id != claim.claim_id or stream_key(recorded) != stream_key(claim):
                raise LedgerError(f"{claim.claim_id}: an edited claim must keep its ID and submission date")
            ledger.record(recorded, outcome, PREDICTED)
    return ledger, views
