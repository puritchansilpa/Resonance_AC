"""Load, validate and fingerprint the payer rule files in rules/.

The rule set is data, not code. It is checked against a fixed schema on load,
and its SHA-256 fingerprint (`version`) is stamped on every prediction, so any
decision can be traced to the exact rules that produced it. Changing a rule
file changes the version.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import config
from .models import Claim
from .rule_kinds import ROUNDINGS, RULE_KINDS

RULES_DIR = config.REPO_ROOT / "rules"
DENIAL_CODES = {"D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10", "D99"}
WEEKDAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
SOURCE_TYPES = {"document", "learned_from_history", "human"}


class RuleSetError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    rule_id: str
    kind: str
    denial_code: str
    params: dict
    source: dict
    effective_from: date | None = None
    effective_to: date | None = None

    def in_force(self, claim: Claim) -> bool:
        d = claim.date_of_service
        return (self.effective_from is None or d >= self.effective_from) and \
               (self.effective_to is None or d <= self.effective_to)


@dataclass(frozen=True)
class PayerRules:
    payer_id: str
    payer_name: str
    precedence: tuple[str, ...]
    rules: tuple[Rule, ...]
    not_applied: tuple[dict, ...] = field(default_factory=tuple)

    def rank(self, code: str) -> int:
        return self.precedence.index(code)


@dataclass(frozen=True)
class RuleSet:
    version: str                       # first 12 hex characters of the SHA-256 fingerprint
    payers: dict[str, PayerRules]

    def for_payer(self, payer_id: str) -> PayerRules:
        if payer_id not in self.payers:
            raise RuleSetError(f"No rule file for payer {payer_id}")
        return self.payers[payer_id]


def _parse_date(value, where):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RuleSetError(f"{where}: bad date {value!r}") from exc


def _validate_rule(raw: dict, payer_id: str) -> Rule:
    rid = raw.get("rule_id", "?")
    where = f"{payer_id}/{rid}"
    for key in ("rule_id", "kind", "denial_code", "params", "source"):
        if key not in raw:
            raise RuleSetError(f"{where}: missing {key}")
    if raw["kind"] not in RULE_KINDS:
        raise RuleSetError(f"{where}: unknown rule kind {raw['kind']!r}")
    if raw["denial_code"] not in DENIAL_CODES:
        raise RuleSetError(f"{where}: unknown denial code {raw['denial_code']!r}")
    kind = RULE_KINDS[raw["kind"]]
    params = raw["params"]
    for name, typ in kind.params.items():
        if name not in params:
            raise RuleSetError(f"{where}: missing parameter {name}")
        if not isinstance(params[name], typ):
            raise RuleSetError(f"{where}: parameter {name} should be {typ.__name__}")
    unknown = set(params) - set(kind.params) - set(kind.optional)
    if unknown:
        raise RuleSetError(f"{where}: unknown parameters {sorted(unknown)}")
    if "rounding" in params and params["rounding"] not in ROUNDINGS:
        raise RuleSetError(f"{where}: unknown rounding {params['rounding']!r}")
    if "days_of_week" in params and not set(params["days_of_week"]) <= WEEKDAYS:
        raise RuleSetError(f"{where}: bad days_of_week {params['days_of_week']}")
    if raw["source"].get("type") not in SOURCE_TYPES:
        raise RuleSetError(f"{where}: source.type must be one of {sorted(SOURCE_TYPES)}")

    eff = raw.get("effective", {})
    if eff and eff.get("basis") != "date_of_service":
        raise RuleSetError(f"{where}: effective dates must be based on date_of_service")
    start = _parse_date(eff["from"], where) if "from" in eff else None
    end = _parse_date(eff["to"], where) if "to" in eff else None
    if start and end and end < start:
        raise RuleSetError(f"{where}: effective 'to' before 'from'")
    return Rule(rid, raw["kind"], raw["denial_code"], dict(params), dict(raw["source"]), start, end)


def _validate_payer(raw: dict) -> PayerRules:
    pid = raw.get("payer_id")
    if pid not in config.PAYER_NAMES:
        raise RuleSetError(f"unknown payer_id {pid!r}")
    rules = tuple(_validate_rule(r, pid) for r in raw.get("rules", []))
    ids = [r.rule_id for r in rules]
    if len(ids) != len(set(ids)):
        raise RuleSetError(f"{pid}: duplicate rule_id")
    precedence = tuple(raw.get("denial_precedence", []))
    if len(precedence) != len(set(precedence)):
        raise RuleSetError(f"{pid}: a code appears twice in denial_precedence")
    missing = {r.denial_code for r in rules} - set(precedence)
    if missing:
        raise RuleSetError(f"{pid}: denial_precedence lacks {sorted(missing)}")
    return PayerRules(pid, raw.get("payer_name", ""), precedence, rules, tuple(raw.get("not_applied", [])))


def load(rules_dir: Path = RULES_DIR) -> RuleSet:
    files = sorted(rules_dir.glob("PY*.json"))
    if not files:
        raise RuleSetError(f"no rule files in {rules_dir}")
    digest = hashlib.sha256()
    payers = {}
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # Fingerprint the parsed content, so line endings and spacing don't change the version.
        digest.update(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())
        payer = _validate_payer(raw)
        payers[payer.payer_id] = payer
    return RuleSet(digest.hexdigest()[:12], payers)
