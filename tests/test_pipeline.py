"""End-to-end checks on the real data pack."""
import filecmp

import pytest

from claimcheck import clean, config, predict, replay


@pytest.fixture(scope="module")
def clean_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("clean")
    report = clean.run(config.DATA_DIR, out)
    return out, report


def test_cleaning_counts(clean_dir):
    _, report = clean_dir
    assert report["rows_out"] == {"claims_history": 3830, "claims_to_check": 546,
                                  "authorizations": 780, "evv_visits": 5076}
    assert report["changes_by_rule"]["claims_history.*:drop_exact_duplicate_row"] == 32
    assert report["warnings"] == []


def test_cleaning_is_byte_identical_between_runs(clean_dir, tmp_path):
    first, _ = clean_dir
    clean.run(config.DATA_DIR, tmp_path)
    names = ["claims_history.csv", "claims_to_check.csv", "authorizations.csv",
             "evv_visits.csv", "cleaning_log.csv", "data_quality.json"]
    match, mismatch, errors = filecmp.cmpfiles(first, tmp_path, names, shallow=False)
    assert mismatch == [] and errors == []


def test_ledger_replay_explains_history(clean_dir, tmp_path):
    out, _ = clean_dir
    summary = replay.run(out, tmp_path)
    checks = summary["checks"]
    for name in ["paid_fits_auth", "paid_fits_day_cap", "paid_not_duplicate",
                 "d06_exceeds_day_cap", "d05_has_prior_submission"]:
        assert checks[name]["contradicted"] == 0, name
    # Every D02 the history-only replay can't explain has an earlier claim-to-check
    # on the same authorization: the two files are one timeline.
    assert summary["contradictions_unexplained"] == []
    assert len(summary["contradictions_with_earlier_claim_to_check"]) == checks["d02_exceeds_auth"]["contradicted"]


def test_backtest_misses_are_all_explained(clean_dir, tmp_path):
    out, _ = clean_dir
    summary = predict.run(out, tmp_path)["summary"]
    assert summary["claims"] == 3830
    # Every miss falls in a named category; none is an unexplained engine miss.
    assert "engine_miss" not in summary["misses_by_category"]
    assert summary["exact_code_accuracy_excluding_opaque_d99"] >= 0.995


def test_predictions_are_byte_identical_between_runs(clean_dir, tmp_path):
    out, _ = clean_dir
    predict.run(out, tmp_path / "a")
    predict.run(out, tmp_path / "b")
    for name in ["predictions.csv", "predictions_meta.json", "decisions/to_check.jsonl",
                 "backtest/history_decisions.csv", "backtest/summary.json"]:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    lines = (tmp_path / "a" / "predictions.csv").read_text().splitlines()
    assert lines[0] == "claim_id,status,denial_code" and len(lines) == 547
