"""Typed records loaded from the clean tables in output/clean/."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import config


@dataclass(frozen=True)
class Claim:
    claim_id: str
    dataset: str            # "history" or "to_check"
    agency_id: str
    payer_id: str
    patient_id: str
    service_code: str
    date_of_service: date
    submitted_date: date
    units: int
    auth_id: str            # "" when the claim quotes none
    modifier: str           # "" when the claim carries none
    frequency_code: int     # 1 = new, 7 = corrected
    original_claim_id: str
    billed_amount: float
    status: str             # "paid" / "denied" for history, "" for to_check
    denial_code: str
    remark: str

    @property
    def is_corrected(self) -> bool:
        return self.frequency_code == 7

    @property
    def duplicate_key(self) -> tuple:
        """Claims with the same key are duplicates of each other (billing_basics.md)."""
        return (self.payer_id, self.patient_id, self.service_code, self.date_of_service)


@dataclass(frozen=True)
class Authorization:
    auth_id: str
    patient_id: str
    payer_id: str
    service_code: str
    start_date: date
    end_date: date
    units_authorized: int

    def covers(self, d: date) -> bool:
        return self.start_date <= d <= self.end_date


@dataclass(frozen=True)
class Visit:
    visit_id: str
    caregiver_id: str
    patient_id: str
    service_code: str
    clock_in: datetime
    clock_out: datetime
    verification_status: str
    service_date: date
    duration_minutes: int
    crosses_midnight: bool


def _rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _d(s: str) -> date:
    return date.fromisoformat(s)


def load_claims(name: str, clean_dir: Path = config.CLEAN_DIR) -> list[Claim]:
    return [
        Claim(
            claim_id=r["claim_id"], dataset=r["dataset"], agency_id=r["agency_id"],
            payer_id=r["payer_id"], patient_id=r["patient_id"], service_code=r["service_code"],
            date_of_service=_d(r["date_of_service"]), submitted_date=_d(r["submitted_date"]),
            units=int(r["units"]), auth_id=r["auth_id"], modifier=r["modifier"],
            frequency_code=int(r["frequency_code"]), original_claim_id=r["original_claim_id"],
            billed_amount=float(r["billed_amount"]), status=r["status"],
            denial_code=r["denial_code"], remark=r["remark"],
        )
        for r in _rows(clean_dir / f"{name}.csv")
    ]


def load_authorizations(clean_dir: Path = config.CLEAN_DIR) -> dict[str, Authorization]:
    return {
        r["auth_id"]: Authorization(
            auth_id=r["auth_id"], patient_id=r["patient_id"], payer_id=r["payer_id"],
            service_code=r["service_code"], start_date=_d(r["start_date"]),
            end_date=_d(r["end_date"]), units_authorized=int(r["units_authorized"]),
        )
        for r in _rows(clean_dir / "authorizations.csv")
    }


def load_visits(clean_dir: Path = config.CLEAN_DIR) -> list[Visit]:
    fmt = config.DATETIME_FORMAT
    return [
        Visit(
            visit_id=r["visit_id"], caregiver_id=r["caregiver_id"], patient_id=r["patient_id"],
            service_code=r["service_code"], clock_in=datetime.strptime(r["clock_in"], fmt),
            clock_out=datetime.strptime(r["clock_out"], fmt),
            verification_status=r["verification_status"], service_date=_d(r["service_date"]),
            duration_minutes=int(r["duration_minutes"]), crosses_midnight=r["crosses_midnight"] == "true",
        )
        for r in _rows(clean_dir / "evv_visits.csv")
    ]
