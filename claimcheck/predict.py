"""Run every claim through evidence -> rules -> decision, backtest on history, write predictions.

One stream in submission order, history and claims-to-check together:
- a history claim is judged on exactly the ledger state the payer saw, the
  prediction is compared with the real outcome, then the real outcome is recorded
- a claim-to-check is judged the same way and its prediction is recorded

Out: output/predictions.csv                 claim_id,status,denial_code (claims to check)
     output/predictions_meta.json           rule set version + input fingerprints
     output/decisions/to_check.jsonl        full decision per claim: every rule result + evidence
     output/backtest/history_decisions.csv  actual vs predicted per history claim
     output/backtest/summary.json           accuracy, confusion matrix, misses by category

Run: python -m claimcheck.predict
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from . import config
from .checker import Checker, Decision
from .clean import sha256
from .evidence import EvidenceBuilder, _jsonable
from .ledger import simulate
from .models import Claim, load_authorizations, load_claims, load_visits
from .ruleset import RULES_DIR, load

# Remarks on D99 denials that carry no rule information.
OPAQUE_D99_REMARKS = {"Denied - contact payer", "Under review - see payer correspondence"}


def decision_to_dict(d: Decision) -> dict:
    return _jsonable({
        "claim_id": d.claim_id,
        "payer_id": d.payer_id,
        "status": d.status,
        "denial_code": d.denial_code,
        "reported_rule": d.reported_rule,
        "explanation": d.explain(),
        "failed_rules": [r.rule_id for r in d.failed],
        "flags": list(d.flags),
        "rule_set_version": d.rule_set_version,
        "results": [asdict(r) for r in d.results],
        "evidence": d.evidence.to_dict(),
    })


def run_all(clean_dir: Path = config.CLEAN_DIR, rules_dir: Path = RULES_DIR):
    ruleset = load(rules_dir)
    history = load_claims("claims_history", clean_dir)
    to_check = load_claims("claims_to_check", clean_dir)
    auths = load_authorizations(clean_dir)
    checker = Checker(ruleset, EvidenceBuilder(load_visits(clean_dir), auths))
    hist_dec: dict[str, Decision] = {}
    check_dec: dict[str, Decision] = {}

    def decide(claim, view):
        d = checker.check(claim, view)
        check_dec[claim.claim_id] = d
        return d.status

    def observe(claim, view):
        hist_dec[claim.claim_id] = checker.check(claim, view)

    ledger, _ = simulate(history, to_check, auths, decide, observe)
    return ruleset, history, to_check, hist_dec, check_dec, ledger


def classify_miss(claim: Claim, d: Decision, predicted_ids: set[str]) -> str:
    if claim.denial_code == "D99" and claim.remark in OPAQUE_D99_REMARKS:
        return "opaque_d99"              # payer gave no reason; no rule can predict it
    if claim.denial_code == "D99" and d.denial_code == "D10" and "amendment 3" in claim.remark:
        return "d99_with_d10_remark"     # payer's remark is the D10 rule; coded D99
    # A ledger rule failed because of units drawn by a claim-to-check whose
    # outcome is our prediction, not a fact: the miss starts upstream.
    led = d.evidence.ledger
    draws = led.auth_draws + led.day_paid_claims
    if any(r.rule_id.endswith(("AUTH-BALANCE", "DAILY-MAX")) for r in d.failed) and \
            any(x.claim_id in predicted_ids for x in draws):
        return "depends_on_predicted_claim"
    return "engine_miss"


def backtest(history: list[Claim], decisions: dict[str, Decision],
             predicted_ids: set[str] = frozenset()) -> tuple[list[dict], dict]:
    rows, confusion = [], defaultdict(Counter)
    by_payer = defaultdict(Counter)
    misses = Counter()
    for c in history:
        d = decisions[c.claim_id]
        actual = c.denial_code or "paid"
        pred = d.denial_code or "paid"
        match = actual == pred
        category = "" if match else classify_miss(c, d, predicted_ids)
        confusion[actual][pred] += 1
        by_payer[c.payer_id]["claims"] += 1
        by_payer[c.payer_id]["exact"] += match
        by_payer[c.payer_id]["status"] += (c.status == d.status)
        if category:
            misses[category] += 1
        rows.append({"claim_id": c.claim_id, "payer_id": c.payer_id, "actual": actual,
                     "predicted": pred, "match": str(match).lower(), "miss_category": category,
                     "failed_rules": " ".join(r.rule_id for r in d.failed), "remark": c.remark})
    n = len(history)
    exact = sum(r["match"] == "true" for r in rows)
    status_ok = sum(c.status == decisions[c.claim_id].status for c in history)
    opaque = misses["opaque_d99"]
    summary = {
        "claims": n,
        "status_accuracy": round(status_ok / n, 4),
        "exact_code_accuracy": round(exact / n, 4),
        "exact_code_accuracy_excluding_opaque_d99": round(exact / (n - opaque), 4),
        "misses_by_category": dict(sorted(misses.items())),
        "by_payer": {p: {"claims": v["claims"], "status_accuracy": round(v["status"] / v["claims"], 4),
                         "exact_code_accuracy": round(v["exact"] / v["claims"], 4)}
                     for p, v in sorted(by_payer.items())},
        "confusion_actual_to_predicted": {a: dict(sorted(p.items())) for a, p in sorted(confusion.items())},
    }
    return rows, summary


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def run(clean_dir: Path = config.CLEAN_DIR, out_dir: Path = config.OUTPUT_DIR) -> dict:
    ruleset, history, to_check, hist_dec, check_dec, _ = run_all(clean_dir)
    rows, summary = backtest(history, hist_dec, set(check_dec))
    summary["rule_set_version"] = ruleset.version

    write_csv(out_dir / "backtest" / "history_decisions.csv", rows)
    (out_dir / "backtest" / "summary.json").write_text(json.dumps(summary, indent=2) + "\n",
                                                        encoding="utf-8", newline="\n")
    write_csv(out_dir / "predictions.csv",
              [{"claim_id": c.claim_id, "status": check_dec[c.claim_id].status,
                "denial_code": check_dec[c.claim_id].denial_code} for c in to_check])
    meta = {"rule_set_version": ruleset.version,
            "clean_inputs_sha256": {p.name: sha256(p) for p in sorted(clean_dir.glob("*.csv"))
                                    if p.name != "cleaning_log.csv"},
            "predicted": dict(Counter(d.denial_code or "paid" for d in check_dec.values()))}
    (out_dir / "predictions_meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n",
                                                   encoding="utf-8", newline="\n")
    dec_dir = out_dir / "decisions"
    dec_dir.mkdir(parents=True, exist_ok=True)
    with open(dec_dir / "to_check.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for c in to_check:
            fh.write(json.dumps(decision_to_dict(check_dec[c.claim_id]), sort_keys=True) + "\n")
    return {"summary": summary, "meta": meta}


if __name__ == "__main__":
    out = run()
    s = out["summary"]
    print(f"Rule set {s['rule_set_version']}  backtest on {s['claims']} history claims")
    print(f"  paid/denied correct:          {s['status_accuracy']:.1%}")
    print(f"  exact code correct:           {s['exact_code_accuracy']:.1%}")
    print(f"  exact, excluding opaque D99:  {s['exact_code_accuracy_excluding_opaque_d99']:.1%}")
    print(f"  misses: {s['misses_by_category']}")
    for p, v in s["by_payer"].items():
        print(f"  {p}: status {v['status_accuracy']:.1%}  exact {v['exact_code_accuracy']:.1%}")
    print(f"Predictions for {sum(out['meta']['predicted'].values())} claims: {out['meta']['predicted']}")
