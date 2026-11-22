"""Claim checker: applies the payer's rules to a claim's evidence and decides.

Every rule in force for the claim's payer and date of service is evaluated, and
every result is kept (passed ones too), so a decision can show both why a claim
fails and what was checked and found fine.

When several rules fail, the payer reports one code. The reported code is the
first failed code in that payer's learned precedence order. The other failed
rules are still listed: a fix has to clear all of them.

No probabilities: rules either pass or fail on the evidence. Uncertainty is
handled by named review flags (see fixer), not by a score.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import Evidence, EvidenceBuilder
from .ledger import DENIED, PAID, LedgerView
from .models import Claim
from .rule_kinds import RULE_KINDS
from .ruleset import RuleSet


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    denial_code: str
    passed: bool
    applicable: bool                 # False when the rule's conditions don't apply to this claim
    detail: str
    facts: dict
    source_ref: str


@dataclass(frozen=True)
class Decision:
    claim_id: str
    payer_id: str
    status: str                      # paid / denied
    denial_code: str                 # "" when paid
    reported_rule: str               # rule_id behind the reported code
    failed: tuple[RuleResult, ...]   # all failed rules, in the payer's precedence order
    results: tuple[RuleResult, ...]  # every rule evaluated
    rule_set_version: str
    evidence: Evidence
    flags: tuple[str, ...] = field(default_factory=tuple)

    def explain(self) -> str:
        if self.status == PAID:
            lines = [f"{self.claim_id}: predicted PAID ({len(self.results)} rules checked)."]
        else:
            top = self.failed[0]
            lines = [f"{self.claim_id}: predicted DENIED {self.denial_code} - {top.detail}"]
            for r in self.failed[1:]:
                lines.append(f"  also fails {r.denial_code} ({r.rule_id}): {r.detail}")
        return "\n".join(lines)


class Checker:
    def __init__(self, ruleset: RuleSet, evidence: EvidenceBuilder):
        self.ruleset = ruleset
        self.evidence = evidence

    def check(self, claim: Claim, view: LedgerView) -> Decision:
        payer = self.ruleset.for_payer(claim.payer_id)
        ev = self.evidence.build(claim, view)
        results = []
        for rule in payer.rules:
            if not rule.in_force(claim):
                continue
            out = RULE_KINDS[rule.kind].check(claim, ev, rule.params)
            results.append(RuleResult(
                rule_id=rule.rule_id, denial_code=rule.denial_code, passed=out.passed,
                applicable=not out.facts.get("not_applicable", False), detail=out.detail,
                facts={k: v for k, v in out.facts.items() if k != "not_applicable"},
                source_ref=rule.source.get("ref", ""),
            ))
        failed = tuple(sorted((r for r in results if not r.passed),
                              key=lambda r: (payer.rank(r.denial_code), r.rule_id)))
        return Decision(
            claim_id=claim.claim_id,
            payer_id=claim.payer_id,
            status=DENIED if failed else PAID,
            denial_code=failed[0].denial_code if failed else "",
            reported_rule=failed[0].rule_id if failed else "",
            failed=failed,
            results=tuple(results),
            rule_set_version=self.ruleset.version,
            evidence=ev,
        )
