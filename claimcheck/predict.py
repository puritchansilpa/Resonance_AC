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
     output/review/claims_to_check.csv      per claim: prediction, reason, fix, review flags
     output/review/summary.json             fix outcomes, flag counts, dollars at stake

Run: python -m claimcheck.predict
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import config
from .checker import Checker, Decision
from .clean import sha256
from .evidence import EvidenceBuilder, _jsonable
from .fixer import FIXABLE, NEEDS_DECISION, FixPlan, Fixer
from .ledger import PAID, Ledger, simulate
from .models import Claim, load_authorizations, load_claims, load_visits
from .review import STATUS, Flag, Reviewer, load_observed_pairs, overdrawn_authorizations
from .ruleset import RULES_DIR, load

# Remarks on D99 denials that carry no rule information.
OPAQUE_D99_REMARKS = {"Denied - contact payer", "Under review - see payer correspondence"}


def plan_to_dict(p: FixPlan) -> dict:
    return _jsonable({
        "outcome": p.outcome,
        "summary": p.summary(),
        "changes": [asdict(c) for c in p.changes],
        "blockers": list(p.blockers),
        "rounds": p.rounds,
        "status_after_fix": p.final_decision.status,
        "code_after_fix": p.final_decision.denial_code,
        "billed_before": p.billed_before,
        "billed_after": p.billed_after,
    })


def decision_to_dict(d: Decision, plan: FixPlan | None = None) -> dict:
    return _jsonable({
        "claim_id": d.claim_id,
        "payer_id": d.payer_id,
        "status": d.status,
        "denial_code": d.denial_code,
        "reported_rule": d.reported_rule,
        "explanation": d.explain(),
        "failed_rules": [r.rule_id for r in d.failed],
        "flags": [asdict(f) for f in d.flags],
        "fix": plan_to_dict(plan) if plan else None,
        "rule_set_version": d.rule_set_version,
        "results": [asdict(r) for r in d.results],
        "evidence": d.evidence.to_dict(),
    })


@dataclass
class Run:
    ruleset: object
    history: list[Claim]
    to_check: list[Claim]
    hist_dec: dict[str, Decision]
    check_dec: dict[str, Decision]          # with review flags attached
    plans: dict[str, FixPlan]               # fix plan per claim-to-check, judged in isolation
    scenario_plans: dict[str, FixPlan]      # fix plans when every earlier fix is applied too
    ledger: Ledger
    ledger_errors: list                     # authorizations the ledger overdraws (see review.py)


def _fix_scenario(history, to_check, auths, checker, fixer) -> dict[str, FixPlan]:
    """Replay the stream with every fixable claim-to-check submitted in its fixed form.

    Fixing a claim can change later ones: a claim that now gets paid draws units
    another claim was counting on. Comparing this run with the per-claim plans
    shows those ripples.
    """
    ledger = Ledger(auths)
    plans: dict[str, FixPlan] = {}

    def decide(claim, view):
        p = fixer.plan(claim, checker.check(claim, view), ledger)
        plans[claim.claim_id] = p
        if p.outcome in (FIXABLE, NEEDS_DECISION):
            return p.fixed_claim, PAID
        return p.final_decision.status

    simulate(history, to_check, auths, decide, ledger=ledger)
    return plans


def run_all(clean_dir: Path = config.CLEAN_DIR, rules_dir: Path = RULES_DIR) -> Run:
    ruleset = load(rules_dir)
    history = load_claims("claims_history", clean_dir)
    to_check = load_claims("claims_to_check", clean_dir)
    auths = load_authorizations(clean_dir)
    checker = Checker(ruleset, EvidenceBuilder(load_visits(clean_dir), auths))
    fixer = Fixer(checker)
    ledger = Ledger(auths)
    hist_dec: dict[str, Decision] = {}
    check_dec: dict[str, Decision] = {}
    plans: dict[str, FixPlan] = {}

    def decide(claim, view):
        d = checker.check(claim, view)
        check_dec[claim.claim_id] = d
        plans[claim.claim_id] = fixer.plan(claim, d, ledger)  # ledger is at this claim's moment
        return d.status

    def observe(claim, view):
        hist_dec[claim.claim_id] = checker.check(claim, view)

    simulate(history, to_check, auths, decide, observe, ledger=ledger)
    scenario = _fix_scenario(history, to_check, auths, checker, fixer)

    ledger_errors = overdrawn_authorizations(ledger)
    reviewer = Reviewer(ruleset, history + to_check, ledger, set(check_dec),
                        load_observed_pairs(rules_dir / "precedence_evidence.json"), ledger_errors)
    for c in to_check:
        d = check_dec[c.claim_id]
        flags = list(reviewer.flags_for(c, d))
        alone, together = plans[c.claim_id], scenario[c.claim_id]
        if (alone.outcome, alone.final_decision.denial_code) != (together.outcome, together.final_decision.denial_code):
            flags.append(Flag("FIX_RIPPLE", STATUS,
                              f"If fixes to earlier claims are applied, this claim becomes "
                              f"'{together.outcome}' ({together.final_decision.denial_code or 'paid'}) instead of "
                              f"'{alone.outcome}' ({alone.final_decision.denial_code or 'paid'}): "
                              f"{together.summary()}"))
        check_dec[c.claim_id] = replace(d, flags=tuple(flags))
    return Run(ruleset, history, to_check, hist_dec, check_dec, plans, scenario, ledger, ledger_errors)


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


def review_rows(r: Run) -> list[dict]:
    """One row per claim-to-check: prediction, fix and review flags (what a biller works from)."""
    rows = []
    for c in r.to_check:
        d, p = r.check_dec[c.claim_id], r.plans[c.claim_id]
        rows.append({
            "claim_id": c.claim_id,
            "payer_id": c.payer_id,
            "patient_id": c.patient_id,
            "service_code": c.service_code,
            "date_of_service": c.date_of_service.isoformat(),
            "submitted_date": c.submitted_date.isoformat(),
            "units": c.units,
            "billed_amount": f"{c.billed_amount:.2f}",
            "predicted_status": d.status,
            "predicted_code": d.denial_code,
            "reason": d.failed[0].detail if d.failed else "",
            "fix_outcome": p.outcome,
            "fix": p.summary() if p.outcome != "payable_as_is" else "",
            "billed_after_fix": "" if p.outcome == "not_fixable" else f"{p.billed_after:.2f}",
            "needs_review": str(bool(d.flags) or p.outcome == NEEDS_DECISION).lower(),
            "flags": " ".join(sorted({f.code for f in d.flags})),
        })
    return rows


def fix_summary(r: Run) -> dict:
    out = Counter(p.outcome for p in r.plans.values())
    scen = Counter(p.outcome for p in r.scenario_plans.values())
    flags = Counter(f.code for d in r.check_dec.values() for f in d.flags)
    billed = sum(c.billed_amount for c in r.to_check)
    paid_as_is = sum(c.billed_amount for c in r.to_check if r.check_dec[c.claim_id].status == PAID)

    def recovered(plans, outcomes):
        return round(sum(p.billed_after for p in plans.values() if p.outcome in outcomes), 2)
    return {
        "fix_outcomes": dict(sorted(out.items())),
        "fix_outcomes_if_all_fixes_applied": dict(sorted(scen.items())),
        "claims_needing_review": sum(1 for row in review_rows(r) if row["needs_review"] == "true"),
        "flags": dict(sorted(flags.items())),
        "dollars": {
            "billed_as_submitted": round(billed, 2),
            "predicted_paid_as_submitted": round(paid_as_is, 2),
            "extra_from_safe_fixes": recovered(r.plans, {FIXABLE}),
            "extra_from_fixes_needing_a_decision": recovered(r.plans, {NEEDS_DECISION}),
        },
    }


def run(clean_dir: Path = config.CLEAN_DIR, out_dir: Path = config.OUTPUT_DIR) -> dict:
    r = run_all(clean_dir)
    ruleset, history, to_check, hist_dec, check_dec = r.ruleset, r.history, r.to_check, r.hist_dec, r.check_dec
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
            fh.write(json.dumps(decision_to_dict(check_dec[c.claim_id], r.plans[c.claim_id]), sort_keys=True) + "\n")
    write_csv(out_dir / "review" / "claims_to_check.csv", review_rows(r))
    fixes = fix_summary(r)
    (out_dir / "review" / "summary.json").write_text(json.dumps(fixes, indent=2) + "\n",
                                                     encoding="utf-8", newline="\n")
    (out_dir / "review" / "ledger_errors.json").write_text(json.dumps(r.ledger_errors, indent=2) + "\n",
                                                           encoding="utf-8", newline="\n")
    return {"summary": summary, "meta": meta, "fixes": fixes}


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
    f = out["fixes"]
    print(f"Fixes: {f['fix_outcomes']}")
    print(f"  if every fix is applied: {f['fix_outcomes_if_all_fixes_applied']}")
    print(f"Review: {f['claims_needing_review']} claims flagged  {f['flags']}")
    print(f"Dollars: {f['dollars']}")
