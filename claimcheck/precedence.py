"""Learn which denial code each payer reports when a claim breaks several rules.

No document states this order. We find it in history: for every denied history
claim that fails two or more rules, the code the payer reported "beat" each of
the other failed codes. Counting those wins per payer gives a pairwise table;
an order is consistent if no pair was ever seen both ways.

This writes evidence and a proposed order. It does not edit the rule files: a
person copies an approved order into rules/PY0x.json (denial_precedence).

Run: python -m claimcheck.precedence
"""
from __future__ import annotations

import json
from collections import defaultdict
from itertools import permutations

from . import config
from .checker import Checker
from .evidence import EvidenceBuilder
from .ledger import simulate
from .models import load_authorizations, load_claims, load_visits
from .ruleset import RULES_DIR, load


def pairwise_wins(history_decisions, history_by_id) -> dict[str, dict[tuple, list[str]]]:
    """payer -> (winner, loser) -> claim IDs where the payer reported winner over loser."""
    wins: dict[str, dict[tuple, list[str]]] = defaultdict(lambda: defaultdict(list))
    for cid, dec in history_decisions.items():
        actual = history_by_id[cid].denial_code
        codes = {r.denial_code for r in dec.failed}
        if len(codes) < 2 or actual not in codes:
            continue
        for loser in codes - {actual}:
            wins[dec.payer_id][(actual, loser)].append(cid)
    return wins


def contradictions(wins_for_payer) -> list[tuple[str, str]]:
    return sorted({tuple(sorted(p)) for p in wins_for_payer if (p[1], p[0]) in wins_for_payer})


def check_order(order, wins_for_payer) -> list[str]:
    """Return claim IDs where the given order disagrees with what the payer reported."""
    bad = []
    for (w, l), ids in wins_for_payer.items():
        if w in order and l in order and order.index(w) > order.index(l):
            bad.extend(ids)
    return sorted(bad)


def run() -> dict:
    ruleset = load()
    history = load_claims("claims_history")
    to_check = load_claims("claims_to_check")
    auths = load_authorizations()
    checker = Checker(ruleset, EvidenceBuilder(load_visits(), auths))
    decisions = {}
    simulate(history, to_check, auths,
             decide=lambda c, v: checker.check(c, v).status,
             observe=lambda c, v: decisions.__setitem__(c.claim_id, checker.check(c, v)))
    wins = pairwise_wins(decisions, {c.claim_id: c for c in history})

    report = {"rule_set_version": ruleset.version, "payers": {}}
    for pid in sorted(ruleset.payers):
        w = wins.get(pid, {})
        order = list(ruleset.for_payer(pid).precedence)
        report["payers"][pid] = {
            "current_order": order,
            "pairs": {f"{a} over {b}": {"claims": len(ids), "examples": ids[:5]}
                      for (a, b), ids in sorted(w.items())},
            "pairs_seen_both_ways": [f"{a}/{b}" for a, b in contradictions(w)],
            "claims_where_current_order_disagrees": check_order(order, w),
            "pairs_never_observed": [f"{a}/{b}" for a, b in permutations(order, 2)
                                     if a < b and (a, b) not in w and (b, a) not in w],
        }
    out = RULES_DIR / "precedence_evidence.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


if __name__ == "__main__":
    rep = run()
    for pid, r in rep["payers"].items():
        print(f"{pid} order {' > '.join(r['current_order'])}")
        for pair, info in r["pairs"].items():
            print(f"    {pair}: {info['claims']}")
        print(f"    seen both ways: {r['pairs_seen_both_ways'] or 'none'}; "
              f"claims the order gets wrong: {len(r['claims_where_current_order_disagrees'])}")
