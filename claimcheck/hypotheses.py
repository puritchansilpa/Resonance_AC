"""Remark reader: Claude proposes rules nobody wrote down; the claims history decides.

1. Backtest history on a baseline rule set (default: documents only, i.e. the
   approved files with every learned rule removed).
2. Group the claims the baseline gets wrong by payer, actual code and payer remark,
   with the facts of a few example claims.
3. Claude reads the remarks and proposes testable hypotheses, each written as one
   of the engine's rule types (structured output), or says a group can't be explained.
4. Code tests every hypothesis against all of that payer's history claims:
     - which claims would the rule fail?
     - of those, how many did the payer deny with that code (support), and how many
       did it pay (contradictions)?
     - if the rule started on some date, which start date removes the contradictions?
       Both date bases are tried (date of service, submission date); the one that
       separates paid from denied is reported.
   A supported hypothesis becomes a proposed rule, confirmed by a full backtest
   (claims fixed, claims broken).
5. A person approves it into rules/PY0x.json (source: learned_from_history).

Claude only proposes. Whether a hypothesis is kept is decided by counting claims.

Run: python -m claimcheck.hypotheses propose        -> rules/hypotheses/proposals.json
     python -m claimcheck.hypotheses approve H1 --by "Name"
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from . import config
from .extract import PARAM_SCHEMAS
from .llm import LLM_CACHE_DIR, OPUS, LLMError, cached_call, make_request, missing_credentials, nullable, \
    strict_object
from .rule_kinds import RULE_KINDS, units_from_minutes
from .ruleset import RULES_DIR, _validate_payer

HYPOTHESES_DIR = RULES_DIR / "hypotheses"
CACHE_DIR = LLM_CACHE_DIR / "hypotheses"
# Rule types a hypothesis may use: ones decided by the claim and its evidence alone.
TESTABLE_KINDS = ["timely_filing", "modifier_required", "whole_hours", "units_within_verified_time",
                  "evv_required", "auth_required", "auth_matches"]
EXAMPLES_PER_GROUP = 10
MIN_SUPPORT = 5


# --------------------------------------------------------------------------- #
# Baseline and residuals
# --------------------------------------------------------------------------- #

def documents_only(rules_dir: Path, out: Path) -> None:
    """Copy the rule files without any learned_from_history rule."""
    for f in sorted(rules_dir.glob("PY*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        raw["rules"] = [r for r in raw["rules"] if r["source"]["type"] == "document"]
        (out / f.name).write_text(json.dumps(raw), encoding="utf-8")
    ev = rules_dir / "precedence_evidence.json"
    if ev.exists():
        shutil.copy(ev, out / ev.name)


def _rounding(ruleset, payer_id):
    return next((r.params["rounding"] for r in ruleset.for_payer(payer_id).rules
                 if r.kind == "units_within_verified_time"), "whole_units_only")


def claim_facts(claim, decision, ruleset) -> dict:
    ev = decision.evidence
    return {
        "claim_id": claim.claim_id,
        "service_code": claim.service_code,
        "date_of_service": claim.date_of_service.isoformat(),
        "weekday": ev.day_of_week,
        "submitted_date": claim.submitted_date.isoformat(),
        "days_to_submit": ev.days_to_submit,
        "units": claim.units,
        "modifier": claim.modifier or None,
        "auth_id": claim.auth_id or None,
        "verified_minutes": ev.verified_minutes,
        "units_supported_by_verified_time": units_from_minutes(ev.verified_minutes, _rounding(ruleset, claim.payer_id)),
        "predicted": decision.denial_code or "paid",
        "actual": claim.denial_code or "paid",
    }


def residual_groups(run) -> list[dict]:
    """History claims the baseline gets wrong, grouped by payer, actual outcome and remark."""
    groups: dict[tuple, list] = defaultdict(list)
    for c in run.history:
        d = run.hist_dec[c.claim_id]
        if (c.denial_code or "paid") != (d.denial_code or "paid"):
            groups[(c.payer_id, c.denial_code or "paid", c.remark)].append((c, d))
    out = []
    for (payer, actual, remark), items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        items.sort(key=lambda cd: (cd[0].date_of_service, cd[0].claim_id))
        step = max(1, len(items) // EXAMPLES_PER_GROUP)  # spread examples across the period
        out.append({
            "payer_id": payer,
            "actual": actual,
            "remark": remark,
            "claims": len(items),
            "predicted": dict(Counter(d.denial_code or "paid" for _, d in items)),
            "examples": [claim_facts(c, d, run.ruleset) for c, d in items[::step][:EXAMPLES_PER_GROUP]],
        })
    return out


# --------------------------------------------------------------------------- #
# Asking Claude
# --------------------------------------------------------------------------- #

def output_schema() -> dict:
    params = {name: nullable(s) for name, s in PARAM_SCHEMAS.items()}
    hypothesis = strict_object({
        "hypothesis_id": {"type": "string"},
        "payer_id": {"type": "string", "enum": sorted(config.PAYER_NAMES)},
        "denial_code": {"type": "string", "enum": ["D01", "D02", "D03", "D04", "D05", "D06", "D07",
                                                   "D08", "D09", "D10"]},
        "kind": {"type": "string", "enum": TESTABLE_KINDS},
        "params": strict_object(params),
        "find_start_date": {"type": "boolean"},
        "rationale": {"type": "string"},
        "supporting_claim_ids": {"type": "array", "items": {"type": "string"}},
    })
    unexplained = strict_object({"payer_id": {"type": "string"}, "remark": {"type": "string"},
                                 "reason": {"type": "string"}})
    return strict_object({"hypotheses": {"type": "array", "items": hypothesis},
                          "not_explainable": {"type": "array", "items": unexplained}})


def system_prompt(ruleset) -> str:
    kinds = "\n".join(f"- {k}: {RULE_KINDS[k].description}" for k in TESTABLE_KINDS)
    current = []
    for pid, p in sorted(ruleset.payers.items()):
        rules = "; ".join(f"{r.rule_id} ({r.kind}, {r.denial_code}, {r.params or 'no parameters'})" for r in p.rules)
        current.append(f"{pid} {p.payer_name}: {rules}")
    return f"""You help find billing rules that payers enforce but have not published.
A deterministic engine checks home-care claims against each payer's known rules. Below are history claims the engine got wrong, grouped by the payer's actual decision and remark, with the facts of example claims.

Propose hypotheses that explain a group, each written as one of these rule types so it can be tested against every claim in the history:
{kinds}

Known rules per payer:
{chr(10).join(current)}

Guidance:
- A hypothesis must be specific enough to test: name the payer, the denial code it would produce, the rule type and its parameters. Unused parameters are null.
- Set find_start_date to true if the remarks or dates suggest the rule began partway through the period; the test will find the date. Otherwise false.
- Use the remark wording and the example facts (units, dates, modifiers, minutes) as evidence. Cite the claim IDs you relied on.
- Some denials carry no usable reason (for example "contact payer"). Put those groups in not_explainable with the reason; do not force a rule onto them.
- Several groups can share one cause, for example the same remark reported under two codes.
- Your hypotheses are tested against all history claims, so a wrong guess is caught; prefer a few precise hypotheses over many vague ones."""


def build_request(groups: list[dict], ruleset, model: str = OPUS) -> dict:
    content = "Claims the engine got wrong, grouped:\n\n" + json.dumps(groups, indent=1)
    return make_request(model, "high", system_prompt(ruleset), [{"role": "user", "content": content}],
                        output_schema())


# --------------------------------------------------------------------------- #
# Testing a hypothesis against history (no model involved)
# --------------------------------------------------------------------------- #

def _basis(claim, basis: str) -> date:
    return claim.date_of_service if basis == "date_of_service" else claim.submitted_date


def failing_claims(h: dict, run) -> list:
    params = {k: v for k, v in h["params"].items() if v is not None}
    check = RULE_KINDS[h["kind"]].check
    out = []
    for c in run.history:
        if c.payer_id != h["payer_id"]:
            continue
        res = check(c, run.hist_dec[c.claim_id].evidence, params)
        if not res.passed and not res.facts.get("not_applicable"):
            out.append(c)
    return out


def _tally(claims, code) -> dict:
    return {
        "denied_with_code": sum(c.denial_code == code for c in claims),
        "denied_d99": sum(c.denial_code == "D99" for c in claims),
        "denied_other_code": sum(c.status == "denied" and c.denial_code not in (code, "D99") for c in claims),
        "paid": sum(c.status == "paid" for c in claims),
    }


def start_date_search(failing, code: str, basis: str) -> dict:
    """Find the start date that leaves no paid claim among those the rule would fail.

    Candidates are the dates of the failing claims; the best keeps the fewest
    contradictions and the most support. The start date reported is the day after
    the last paid failing claim before it.
    """
    best = None
    for d in [None] + sorted({_basis(c, basis) for c in failing}):
        after = [c for c in failing if d is None or _basis(c, basis) >= d]
        t = _tally(after, code)
        score = (t["paid"], -t["denied_with_code"], d or date.min)
        if best is None or score < best[0]:
            best = (score, d, t)
    _, d, t = best
    before = [c for c in failing if d is not None and _basis(c, basis) < d]
    paid_before = [c for c in before if c.status == "paid"]
    start = (max(_basis(c, basis) for c in paid_before) + timedelta(days=1)) if paid_before else d
    return {"basis": basis, "start": start.isoformat() if start else None, "after": t,
            "before": _tally(before, code),
            "last_paid_before": max((_basis(c, basis) for c in paid_before), default=None),
            "first_failing_after": min((_basis(c, basis) for c in failing
                                        if d is None or _basis(c, basis) >= d), default=None)}


def test_hypothesis(h: dict, run) -> dict:
    failing = failing_claims(h, run)
    whole = _tally(failing, h["denial_code"])
    result = {"claims_failing_rule": len(failing), "without_start_date": whole}
    if h["find_start_date"]:
        options = [start_date_search(failing, h["denial_code"], b) for b in ("date_of_service", "submitted_date")]
        best = min(options, key=lambda o: (o["after"]["paid"], -o["after"]["denied_with_code"]))
        for o in options:
            for k in ("last_paid_before", "first_failing_after"):
                o[k] = o[k].isoformat() if o[k] else None
        result["start_date_options"] = options
        chosen = best
    else:
        chosen = {"basis": "date_of_service", "start": None, "after": whole}
    t = chosen["after"]
    if t["paid"] == 0 and t["denied_with_code"] >= MIN_SUPPORT:
        verdict = "supported"
    elif t["paid"] > 0:
        verdict = "contradicted"
    else:
        verdict = "weak"
    result.update({"verdict": verdict, "basis": chosen["basis"], "start": chosen["start"],
                   "support": t["denied_with_code"], "also_denied_d99": t["denied_d99"],
                   "contradictions": t["paid"]})
    if verdict == "supported" and chosen["basis"] != "date_of_service":
        result["verdict"] = "supported_not_expressible"
        result["note"] = "The engine's effective dates follow the date of service; this rule needs a submission-date start."
    return result


def proposed_rule(h: dict, test: dict, remark_examples: list[str]) -> dict:
    rule = {
        "rule_id": f"{h['payer_id']}-LEARNED-{h['hypothesis_id']}",
        "kind": h["kind"],
        "denial_code": h["denial_code"],
        "params": {k: v for k, v in h["params"].items() if v is not None},
        "source": {
            "type": "learned_from_history",
            "ref": f"Hypothesis {h['hypothesis_id']} from payer remarks (claimcheck.hypotheses)",
            "quote": remark_examples[0] if remark_examples else "",
            "evidence": (f"{test['claims_failing_rule']} history claims would fail this rule. "
                         + (f"From {test['start']} (by {test['basis'].replace('_', ' ')}): " if test["start"] else "")
                         + f"{test['support']} denied {h['denial_code']}, {test['also_denied_d99']} denied D99, "
                           f"{test['contradictions']} paid. {h['rationale']}"),
        },
    }
    if test["start"]:
        rule["effective"] = {"basis": "date_of_service", "from": test["start"]}
    return rule


def _with_rule(rules_dir: Path, out: Path, rule: dict, payer_id: str) -> None:
    for f in rules_dir.glob("*.json"):
        shutil.copy(f, out / f.name)
    raw = json.loads((out / f"{payer_id}.json").read_text(encoding="utf-8"))
    raw["rules"].append(rule)
    if rule["denial_code"] not in raw["denial_precedence"]:
        raw["denial_precedence"].append(rule["denial_code"])
    (out / f"{payer_id}.json").write_text(json.dumps(raw), encoding="utf-8")


def confirm(rule: dict, payer_id: str, baseline_dir: Path, baseline_run, clean_dir: Path) -> dict:
    """Full backtest with the proposed rule added: which claims it fixes and which it breaks."""
    from .predict import run_all

    with tempfile.TemporaryDirectory() as tmp:
        trial = Path(tmp)
        _with_rule(baseline_dir, trial, rule, payer_id)
        after = run_all(clean_dir, trial)
    fixed, broken = [], []
    for c in baseline_run.history:
        actual = c.denial_code or "paid"
        before_ok = (baseline_run.hist_dec[c.claim_id].denial_code or "paid") == actual
        after_ok = (after.hist_dec[c.claim_id].denial_code or "paid") == actual
        if after_ok and not before_ok:
            fixed.append(c.claim_id)
        elif before_ok and not after_ok:
            broken.append(c.claim_id)
    n = len(baseline_run.history)
    ok_before = sum((baseline_run.hist_dec[c.claim_id].denial_code or "paid") == (c.denial_code or "paid")
                    for c in baseline_run.history)
    return {"claims_fixed": len(fixed), "claims_broken": len(broken), "broken_examples": broken[:10],
            "exact_accuracy_before": round(ok_before / n, 4),
            "exact_accuracy_after": round((ok_before + len(fixed) - len(broken)) / n, 4)}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def propose(rules_dir: Path = RULES_DIR, out_dir: Path = HYPOTHESES_DIR, cache_dir: Path = CACHE_DIR,
            clean_dir: Path = config.CLEAN_DIR, baseline: str = "documents", refresh: bool = False,
            client=None) -> dict:
    from .predict import run_all

    with tempfile.TemporaryDirectory() as tmp:
        base_dir = Path(tmp)
        if baseline == "documents":
            documents_only(rules_dir, base_dir)
        else:
            for f in rules_dir.glob("*.json"):
                shutil.copy(f, base_dir / f.name)
        run = run_all(clean_dir, base_dir)
        groups = residual_groups(run)
        request = build_request(groups, run.ruleset)
        result, from_cache = cached_call(request, cache_dir, refresh, client)
        answer = result["data"]
        remarks_by_payer = defaultdict(list)
        for g in groups:
            if g["remark"]:
                remarks_by_payer[g["payer_id"]].append(g["remark"])
        tested = []
        for h in answer["hypotheses"]:
            entry = {"hypothesis": h}
            if h["payer_id"] not in run.ruleset.payers:
                entry["test"] = {"verdict": "invalid", "note": f"unknown payer {h['payer_id']}"}
            else:
                entry["test"] = test_hypothesis(h, run)
                if entry["test"]["verdict"] == "supported":
                    rule = proposed_rule(h, entry["test"], remarks_by_payer[h["payer_id"]])
                    entry["proposed_rule"] = rule
                    entry["backtest"] = confirm(rule, h["payer_id"], base_dir, run, clean_dir)
            tested.append(entry)
    report = {
        "baseline": baseline,
        "model": result.get("model"),
        "request_sha256": result.get("request_sha256"),
        "residual_groups": [{k: g[k] for k in ("payer_id", "actual", "remark", "claims", "predicted")} for g in groups],
        "hypotheses": tested,
        "not_explainable": answer["not_explainable"],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "proposals.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    report["from_cache"] = from_cache
    return report


def approve(hypothesis_id: str, approver: str, rules_dir: Path = RULES_DIR, out_dir: Path = HYPOTHESES_DIR,
            today: date | None = None) -> dict:
    report = json.loads((out_dir / "proposals.json").read_text(encoding="utf-8"))
    entry = next((e for e in report["hypotheses"] if e["hypothesis"]["hypothesis_id"] == hypothesis_id), None)
    if entry is None or "proposed_rule" not in entry:
        raise LLMError(f"No supported hypothesis {hypothesis_id} to approve.")
    rule = json.loads(json.dumps(entry["proposed_rule"]))
    pid = entry["hypothesis"]["payer_id"]
    path = rules_dir / f"{pid}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    same = next((r for r in raw["rules"] if (r["kind"], r["denial_code"], r["params"])
                 == (rule["kind"], rule["denial_code"], rule["params"])), None)
    if same:
        raise LLMError(f"{pid} already has this rule as {same['rule_id']}"
                       f" (effective from {same.get('effective', {}).get('from', 'the start')}).")
    rule["approval"] = {"approved_by": approver, "approved_on": (today or date.today()).isoformat()}
    raw["rules"].append(rule)
    if rule["denial_code"] not in raw["denial_precedence"]:
        raw["denial_precedence"].append(rule["denial_code"])
    _validate_payer(raw)
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8", newline="\n")
    return rule


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m claimcheck.hypotheses")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose", help="Claude reads remarks on missed denials; history tests each idea")
    p.add_argument("--baseline", choices=["documents", "current"], default="documents",
                   help="documents: approved files minus learned rules (default); current: as approved")
    p.add_argument("--refresh", action="store_true")
    a = sub.add_parser("approve", help="add a supported hypothesis to its payer's rule file")
    a.add_argument("hypothesis_id")
    a.add_argument("--by", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "propose":
            rep = propose(baseline=args.baseline, refresh=args.refresh)
            print(f"Baseline '{rep['baseline']}': {sum(g['claims'] for g in rep['residual_groups'])} history "
                  f"claims wrong in {len(rep['residual_groups'])} groups ({'cache' if rep['from_cache'] else 'model call'})")
            for e in rep["hypotheses"]:
                h, t = e["hypothesis"], e["test"]
                line = (f"  {h['hypothesis_id']} {h['payer_id']} {h['kind']} -> {h['denial_code']}: {t['verdict']}, "
                        f"support {t.get('support')}, contradictions {t.get('contradictions')}")
                if t.get("start"):
                    line += f", starts {t['start']} by {t['basis']}"
                if "backtest" in e:
                    b = e["backtest"]
                    line += (f"; backtest {b['exact_accuracy_before']:.2%} -> {b['exact_accuracy_after']:.2%} "
                             f"({b['claims_fixed']} fixed, {b['claims_broken']} broken)")
                print(line)
            for n in rep["not_explainable"]:
                print(f"  not explainable: {n['payer_id']} '{n['remark']}': {n['reason']}")
        else:
            rule = approve(args.hypothesis_id, args.by)
            print(f"Added {rule['rule_id']} to its payer's rule file.")
    except LLMError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if missing_credentials(exc):
            print("No Anthropic credentials found. Set ANTHROPIC_API_KEY and run again.", file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
