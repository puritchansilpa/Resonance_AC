"""Evidence checker: gathers the facts a claim rests on, before any rule is applied.

For each claim it records:
- the EVV visits for that member, service and date of service (verified and not),
  and the total verified minutes
- the authorization the claim quotes, and each way it fails to match the claim
- days from date of service to submission, and the weekday of the date of service
- the ledger view: remaining authorization units, units already paid that day,
  and earlier submissions of the same claim

Evidence is structured data with IDs (visit IDs, authorization ID, claim IDs) so
a reviewer can go back to the source rows. It is stored next to every decision.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable, Mapping

from .ledger import LedgerView
from .models import Authorization, Claim, Visit

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class VisitRef:
    visit_id: str
    verification_status: str
    clock_in: str
    clock_out: str
    minutes: int


@dataclass(frozen=True)
class AuthCheck:
    auth_id: str
    found: bool
    patient_id: str | None = None
    payer_id: str | None = None
    service_code: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    units_authorized: int | None = None
    mismatches: tuple[str, ...] = ()     # e.g. "service: authorization is for T1005"


@dataclass(frozen=True)
class Evidence:
    claim_id: str
    verified_visits: tuple[VisitRef, ...]
    unverified_visits: tuple[VisitRef, ...]
    verified_minutes: int
    auth: AuthCheck | None              # None when the claim quotes no authorization
    days_to_submit: int
    day_of_week: str
    ledger: LedgerView

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))


def _jsonable(x):
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, date):
        return x.isoformat()
    return x


class EvidenceBuilder:
    def __init__(self, visits: Iterable[Visit], authorizations: Mapping[str, Authorization]):
        self._visits: dict[tuple, list[Visit]] = defaultdict(list)
        for v in sorted(visits, key=lambda v: (v.clock_in, v.visit_id)):
            # A visit belongs to the date the caregiver clocked in.
            self._visits[(v.patient_id, v.service_code, v.service_date)].append(v)
        self._auths = dict(authorizations)

    def visits_for(self, patient_id: str, service_code: str, d: date) -> list[Visit]:
        return list(self._visits.get((patient_id, service_code, d), ()))

    def auth_check(self, claim: Claim) -> AuthCheck | None:
        if not claim.auth_id:
            return None
        a = self._auths.get(claim.auth_id)
        if a is None:
            return AuthCheck(claim.auth_id, found=False, mismatches=("authorization ID not on file",))
        mism = []
        if a.patient_id != claim.patient_id:
            mism.append(f"member: authorization is for {a.patient_id}")
        if a.payer_id != claim.payer_id:
            mism.append(f"payer: authorization is from {a.payer_id}")
        if a.service_code != claim.service_code:
            mism.append(f"service: authorization is for {a.service_code}")
        if not a.covers(claim.date_of_service):
            mism.append(f"date: authorization runs {a.start_date} to {a.end_date}")
        return AuthCheck(a.auth_id, True, a.patient_id, a.payer_id, a.service_code,
                         a.start_date, a.end_date, a.units_authorized, tuple(mism))

    def build(self, claim: Claim, view: LedgerView) -> Evidence:
        refs = [VisitRef(v.visit_id, v.verification_status, v.clock_in.strftime("%Y-%m-%d %H:%M"),
                         v.clock_out.strftime("%Y-%m-%d %H:%M"), v.duration_minutes)
                for v in self.visits_for(claim.patient_id, claim.service_code, claim.date_of_service)]
        verified = tuple(r for r in refs if r.verification_status == "verified")
        return Evidence(
            claim_id=claim.claim_id,
            verified_visits=verified,
            unverified_visits=tuple(r for r in refs if r.verification_status != "verified"),
            # Payers add together same-day visits for the same member and service.
            verified_minutes=sum(r.minutes for r in verified),
            auth=self.auth_check(claim),
            days_to_submit=(claim.submitted_date - claim.date_of_service).days,
            day_of_week=WEEKDAYS[claim.date_of_service.weekday()],
            ledger=view,
        )
