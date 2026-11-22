"""Data pipeline: raw exports in, clean tables out, with a log of every change.

In:  Data/claims_history.csv, claims_to_check.csv, authorizations.csv, evv_visits.csv
Out: output/clean/*.csv              clean tables (ISO dates, canonical codes)
     output/clean/cleaning_log.csv   one row per changed or dropped value: raw -> clean + rule
     output/clean/data_quality.json  input file hashes, counts per rule, validation warnings

Principles
- Nothing is guessed. A value that matches no known format raises CleaningError.
- Cleaning only changes how a value is written, never what it means. A blank
  modifier or a blank authorization stays blank: those are evidence, not dirt.
- Output is byte-identical between runs (no timestamps, stable ordering).

Run: python -m claimcheck.clean
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import config


class CleaningError(ValueError):
    """Raised when a raw value can't be cleaned without guessing."""


@dataclass
class CleaningLog:
    entries: list[dict] = field(default_factory=list)

    def record(self, table, record_id, source_row, field_name, raw, clean, rule):
        self.entries.append({
            "table": table,
            "record_id": record_id,
            "source_row": source_row,
            "field": field_name,
            "raw_value": raw,
            "clean_value": clean,
            "rule": rule,
        })

    def rule_counts(self) -> dict[str, int]:
        counts = Counter(f"{e['table']}.{e['field']}:{e['rule']}" for e in self.entries)
        return dict(sorted(counts.items()))


# --------------------------------------------------------------------------- #
# Value-level cleaners. Each returns (clean_value, [rule names applied]).
# --------------------------------------------------------------------------- #

def clean_code(raw: str) -> tuple[str, list[str]]:
    """Trim and upper-case a code such as a service code or modifier."""
    value, rules = raw, []
    if value != value.strip():
        value = value.strip()
        rules.append("strip_whitespace")
    if value != value.upper():
        value = value.upper()
        rules.append("uppercase")
    return value, rules


def clean_payer(raw: str) -> tuple[str, list[str]]:
    key = raw.strip().upper()
    if key not in config.PAYER_ALIASES:
        raise CleaningError(f"Unknown payer name {raw!r}; add it to PAYER_ALIASES")
    payer_id = config.PAYER_ALIASES[key]
    return payer_id, ([] if raw == payer_id else ["payer_alias"])


def parse_date(raw: str) -> tuple[date, list[str]]:
    value = raw.strip()
    for fmt in config.DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        rules = []
        if value != raw:
            rules.append("strip_whitespace")
        if fmt != "%Y-%m-%d":
            rules.append("date_mdy_to_iso")
        return parsed, rules
    raise CleaningError(f"Unrecognised date {raw!r}")


def parse_datetime(raw: str) -> datetime:
    try:
        return datetime.strptime(raw.strip(), config.DATETIME_FORMAT)
    except ValueError as exc:
        raise CleaningError(f"Unrecognised timestamp {raw!r}") from exc


def parse_int(raw: str, what: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise CleaningError(f"{what}: expected a whole number, got {raw!r}") from exc


# --------------------------------------------------------------------------- #
# Table cleaners
# --------------------------------------------------------------------------- #

def read_raw(path: Path) -> list[dict]:
    """Read a CSV as strings, tagging each row with its line number in the file."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for i, row in enumerate(rows):
        row["_source_row"] = i + 2  # line 1 is the header
    return rows


def drop_exact_duplicates(rows: list[dict], table: str, id_field: str, log: CleaningLog) -> list[dict]:
    """Drop rows that repeat an earlier row in every column (export artefacts).

    Two rows with the same ID but different content are a real conflict and raise.
    """
    seen: dict[str, tuple] = {}
    kept = []
    for row in rows:
        content = tuple((k, v) for k, v in row.items() if k != "_source_row")
        rid = row[id_field]
        if rid in seen:
            if seen[rid] != content:
                raise CleaningError(f"{table}: {id_field} {rid} appears twice with different content")
            log.record(table, rid, row["_source_row"], "*", "duplicate row", "dropped", "drop_exact_duplicate_row")
            continue
        seen[rid] = content
        kept.append(row)
    return kept


def _apply(log, table, rid, src, field_name, raw, cleaner):
    clean, rules = cleaner(raw)
    if rules:
        log.record(table, rid, src, field_name, raw, str(clean), "+".join(rules))
    return clean


CLAIM_COLUMNS = [
    "claim_id", "dataset", "source_row", "agency_id", "payer_id", "patient_id",
    "service_code", "date_of_service", "submitted_date", "units", "auth_id",
    "modifier", "frequency_code", "original_claim_id", "billed_amount",
    "status", "paid_amount", "denial_code", "remark",
]


def clean_claims(rows: list[dict], table: str, dataset: str, log: CleaningLog, warnings: list[str]) -> list[dict]:
    rows = drop_exact_duplicates(rows, table, "claim_id", log)
    out = []
    for r in rows:
        rid, src = r["claim_id"], r["_source_row"]
        a = lambda name, cleaner: _apply(log, table, rid, src, name, r[name], cleaner)  # noqa: E731

        service = a("service_code", clean_code)
        modifier = a("modifier", clean_code)
        if service not in config.SERVICE_CODES:
            raise CleaningError(f"{rid}: unknown service code {service!r}")
        if modifier and modifier not in config.MODIFIERS:
            warnings.append(f"{rid}: unrecognised modifier {modifier!r} kept as-is")

        units = parse_int(r["units"], f"{rid} units")
        billed = float(r["billed_amount"])
        expected = round(units * config.SERVICE_RATES[service], 2)
        if abs(billed - expected) > 0.005:
            warnings.append(f"{rid}: billed_amount {billed} != units x rate {expected}")

        freq = parse_int(r["frequency_code"], f"{rid} frequency_code")
        if freq not in config.FREQUENCY_CODES:
            raise CleaningError(f"{rid}: unknown frequency code {freq}")
        original = r["original_claim_id"].strip()
        if freq == 7 and not original:
            warnings.append(f"{rid}: corrected claim (frequency 7) has no original_claim_id")
        if freq == 1 and original:
            warnings.append(f"{rid}: new claim (frequency 1) quotes original_claim_id {original}")

        out.append({
            "claim_id": rid,
            "dataset": dataset,
            "source_row": src,
            "agency_id": r["agency_id"].strip(),
            "payer_id": a("payer", clean_payer),
            "patient_id": r["patient_id"].strip(),
            "service_code": service,
            "date_of_service": a("date_of_service", parse_date).isoformat(),
            "submitted_date": a("submitted_date", parse_date).isoformat(),
            "units": units,
            "auth_id": r["auth_id"].strip(),
            "modifier": modifier,
            "frequency_code": freq,
            "original_claim_id": original,
            "billed_amount": f"{billed:.2f}",
            "status": r.get("status", "").strip(),
            "paid_amount": f"{float(r['paid_amount']):.2f}" if r.get("paid_amount", "").strip() else "",
            "denial_code": r.get("denial_code", "").strip(),
            "remark": r.get("remark", "").strip(),
        })
    return out


AUTH_COLUMNS = ["auth_id", "source_row", "patient_id", "payer_id", "service_code",
                "start_date", "end_date", "units_authorized"]


def clean_authorizations(rows: list[dict], log: CleaningLog, warnings: list[str]) -> list[dict]:
    table = "authorizations"
    rows = drop_exact_duplicates(rows, table, "auth_id", log)
    out = []
    for r in rows:
        rid, src = r["auth_id"], r["_source_row"]
        a = lambda name, cleaner: _apply(log, table, rid, src, name, r[name], cleaner)  # noqa: E731
        start, end = a("start_date", parse_date), a("end_date", parse_date)
        if end < start:
            raise CleaningError(f"{rid}: end_date before start_date")
        service = a("service_code", clean_code)
        if service not in config.SERVICE_CODES:
            raise CleaningError(f"{rid}: unknown service code {service!r}")
        out.append({
            "auth_id": rid,
            "source_row": src,
            "patient_id": r["patient_id"].strip(),
            "payer_id": a("payer_id", clean_payer),
            "service_code": service,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "units_authorized": parse_int(r["units_authorized"], f"{rid} units_authorized"),
        })
    return out


EVV_COLUMNS = ["visit_id", "source_row", "caregiver_id", "patient_id", "service_code",
               "clock_in", "clock_out", "verification_status",
               "service_date", "duration_minutes", "crosses_midnight"]


def clean_evv(rows: list[dict], log: CleaningLog, warnings: list[str]) -> list[dict]:
    table = "evv_visits"
    rows = drop_exact_duplicates(rows, table, "visit_id", log)
    out = []
    for r in rows:
        rid, src = r["visit_id"], r["_source_row"]
        clock_in, clock_out = parse_datetime(r["clock_in"]), parse_datetime(r["clock_out"])
        minutes = (clock_out - clock_in).total_seconds() / 60
        if minutes <= 0:
            raise CleaningError(f"{rid}: clock_out is not after clock_in")
        if minutes != int(minutes):
            raise CleaningError(f"{rid}: duration is not a whole number of minutes")
        status = r["verification_status"].strip().lower()
        if status != r["verification_status"]:
            log.record(table, rid, src, "verification_status", r["verification_status"], status, "strip_whitespace+lowercase")
        if status not in config.VERIFICATION_STATUSES:
            raise CleaningError(f"{rid}: unknown verification status {status!r}")
        out.append({
            "visit_id": rid,
            "source_row": src,
            "caregiver_id": r["caregiver_id"].strip(),
            "patient_id": r["patient_id"].strip(),
            "service_code": _apply(log, table, rid, src, "service_code", r["service_code"], clean_code),
            "clock_in": clock_in.strftime(config.DATETIME_FORMAT),
            "clock_out": clock_out.strftime(config.DATETIME_FORMAT),
            "verification_status": status,
            # A visit belongs to the date the caregiver clocked in (billing_basics.md).
            "service_date": clock_in.date().isoformat(),
            "duration_minutes": int(minutes),
            "crosses_midnight": clock_out.date() > clock_in.date(),
        })
    return out


# --------------------------------------------------------------------------- #
# Cross-table checks (warnings only: these are findings, not cleaning)
# --------------------------------------------------------------------------- #

def cross_checks(history, to_check, auths, warnings: list[str]) -> dict:
    auth_ids = {a["auth_id"] for a in auths}
    claim_ids = [c["claim_id"] for c in history + to_check]
    if len(claim_ids) != len(set(claim_ids)):
        raise CleaningError("A claim_id appears in both claims_history and claims_to_check")
    unknown_auth = [c["claim_id"] for c in history + to_check if c["auth_id"] and c["auth_id"] not in auth_ids]
    for cid in unknown_auth:
        warnings.append(f"{cid}: auth_id not found in authorizations.csv")
    known = set(claim_ids)
    for c in to_check:
        if c["original_claim_id"] and c["original_claim_id"] not in known:
            warnings.append(f"{c['claim_id']}: original_claim_id {c['original_claim_id']} not found in any claims file")
    return {"claims_quoting_unknown_auth": len(unknown_auth)}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("true" if v is True else "false" if v is False else v) for k, v in row.items()})


def run(data_dir: Path = config.DATA_DIR, out_dir: Path = config.CLEAN_DIR) -> dict:
    log, warnings = CleaningLog(), []
    raw = {name: read_raw(data_dir / fname) for name, fname in config.RAW_FILES.items()}

    history = clean_claims(raw["claims_history"], "claims_history", "history", log, warnings)
    to_check = clean_claims(raw["claims_to_check"], "claims_to_check", "to_check", log, warnings)
    auths = clean_authorizations(raw["authorizations"], log, warnings)
    visits = clean_evv(raw["evv_visits"], log, warnings)
    findings = cross_checks(history, to_check, auths, warnings)

    write_csv(out_dir / "claims_history.csv", history, CLAIM_COLUMNS)
    write_csv(out_dir / "claims_to_check.csv", to_check, CLAIM_COLUMNS)
    write_csv(out_dir / "authorizations.csv", auths, AUTH_COLUMNS)
    write_csv(out_dir / "evv_visits.csv", visits, EVV_COLUMNS)
    write_csv(out_dir / "cleaning_log.csv", log.entries,
              ["table", "record_id", "source_row", "field", "raw_value", "clean_value", "rule"])

    report = {
        "input_sha256": {fname: sha256(data_dir / fname) for fname in config.RAW_FILES.values()},
        "rows_in": {name: len(rows) for name, rows in raw.items()},
        "rows_out": {"claims_history": len(history), "claims_to_check": len(to_check),
                     "authorizations": len(auths), "evv_visits": len(visits)},
        "changes_by_rule": log.rule_counts(),
        "findings": {
            **findings,
            "evv_visits_crossing_midnight": sum(v["crosses_midnight"] for v in visits),
            "claims_without_auth_id": {
                "history": sum(not c["auth_id"] for c in history),
                "to_check": sum(not c["auth_id"] for c in to_check),
            },
            "corrected_claims": {
                "history": sum(c["frequency_code"] == 7 for c in history),
                "to_check": sum(c["frequency_code"] == 7 for c in to_check),
            },
        },
        "warnings": warnings,
    }
    with open(out_dir / "data_quality.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return report


if __name__ == "__main__":
    rep = run()
    print(f"Clean tables written to {config.CLEAN_DIR}")
    for name, n in rep["rows_out"].items():
        print(f"  {name}: {rep['rows_in'][name]} rows in -> {n} out")
    print(f"  {sum(rep['changes_by_rule'].values())} logged changes, {len(rep['warnings'])} warnings")
