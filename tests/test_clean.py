from datetime import date

import pytest

from claimcheck.clean import (
    CleaningError, CleaningLog, clean_code, clean_payer, drop_exact_duplicates, parse_date,
)


def test_clean_code_strips_and_uppercases():
    assert clean_code(" u1 ") == ("U1", ["strip_whitespace", "uppercase"])
    assert clean_code("T1019 ") == ("T1019", ["strip_whitespace"])
    assert clean_code("U2") == ("U2", [])
    assert clean_code("") == ("", [])  # blank stays blank: it is evidence


@pytest.mark.parametrize("raw,expected", [
    ("PY01", "PY01"), ("NORTHWIND HP", "PY01"), ("Northwind Health Plan", "PY01"),
    ("Harbor Medicaid", "PY02"), ("Harbor Medicaid Plan", "PY02"),
    ("SUMMIT CARE", "PY03"), ("Summit Care Partners", "PY03"),
])
def test_payer_aliases(raw, expected):
    assert clean_payer(raw)[0] == expected


def test_unknown_payer_is_an_error_not_a_guess():
    with pytest.raises(CleaningError):
        clean_payer("Northwind")


def test_dates_both_formats():
    assert parse_date("2026-06-09") == (date(2026, 6, 9), [])
    assert parse_date("07/09/2026") == (date(2026, 7, 9), ["date_mdy_to_iso"])
    with pytest.raises(CleaningError):
        parse_date("9 July 2026")
    with pytest.raises(CleaningError):
        parse_date("13/09/2026")  # not month/day/year: refuse rather than swap


def test_exact_duplicate_rows_dropped_and_logged():
    log = CleaningLog()
    rows = [{"claim_id": "C1", "units": "4", "_source_row": 2},
            {"claim_id": "C1", "units": "4", "_source_row": 3},
            {"claim_id": "C2", "units": "8", "_source_row": 4}]
    kept = drop_exact_duplicates(rows, "t", "claim_id", log)
    assert [r["_source_row"] for r in kept] == [2, 4]
    assert log.entries[0]["source_row"] == 3 and log.entries[0]["rule"] == "drop_exact_duplicate_row"


def test_same_id_different_content_is_an_error():
    rows = [{"claim_id": "C1", "units": "4", "_source_row": 2},
            {"claim_id": "C1", "units": "5", "_source_row": 3}]
    with pytest.raises(CleaningError):
        drop_exact_duplicates(rows, "t", "claim_id", CleaningLog())
