"""Add a new payer: register it, store its documents, then draft its rules.

  python -m claimcheck.onboard add PY04 --name "Lakeside Community Health" \
      --alias "LAKESIDE CH" --doc demo/lakeside/lakeside_billing_manual.md
  python -m claimcheck.extract run PY04          # Claude drafts the rule file
  python -m claimcheck.extract approve PY04 --by "Name"
  python -m claimcheck.bulletins draft PY04 demo/lakeside/lakeside_notice_2026-10.md

Registering records the payer's name and the spellings its claims exports use
(so the cleaning step maps them to the payer ID) in onboarding/payers.json, and
copies its documents to onboarding/payers/ as <payer>_<name>.md, where the
extractor finds them. A new payer has no claims history yet, so its rule file
starts with a default code order and no learned rules; the precedence learner
and remark reader take over once its claims come back.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from . import config

PAYER_ID = re.compile(r"^PY\d{2}$")


class OnboardingError(ValueError):
    pass


def add(payer_id: str, name: str, aliases: list[str], docs: list[Path],
        registry_path: Path | None = None, docs_dir: Path | None = None) -> dict:
    registry_path = registry_path or config.PAYER_REGISTRY
    docs_dir = docs_dir or config.ONBOARDED_DOCS_DIR
    if not PAYER_ID.match(payer_id):
        raise OnboardingError("Payer ID must look like PY04.")
    if not name.strip():
        raise OnboardingError("A payer name is required.")
    if not docs:
        raise OnboardingError("Give at least one document (--doc).")
    registry = config.load_registry(registry_path)
    if payer_id in config.PAYER_NAMES and payer_id not in registry:
        raise OnboardingError(f"{payer_id} is already a payer in the data pack.")
    for alias in [name, *aliases]:
        owner = config.PAYER_ALIASES.get(alias.strip().upper())
        if owner and owner != payer_id:
            raise OnboardingError(f"'{alias}' already names payer {owner}.")
    for d in docs:
        if not d.exists() or d.suffix.lower() not in (".md", ".txt"):
            raise OnboardingError(f"{d}: documents must be existing .md or .txt files.")

    docs_dir.mkdir(parents=True, exist_ok=True)
    stored = []
    for d in docs:
        target = docs_dir / (d.name if d.name.startswith(payer_id + "_") else f"{payer_id}_{d.stem}.md")
        shutil.copy(d, target)
        stored.append(target.name)
    entry = {"name": name.strip(), "aliases": sorted({a.strip() for a in aliases if a.strip()}),
             "documents": sorted(set(registry.get(payer_id, {}).get("documents", [])) | set(stored))}
    registry[payer_id] = entry
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    config.register_payer_names({payer_id: entry})
    return entry


def check(proposal_path: Path, expected_path: Path) -> dict:
    """Score a drafted rule file against a hand-written answer key (used in the demo)."""
    from .extract import compare
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    result = compare(proposal, expected)
    meta_path = proposal_path.with_suffix(".meta.json")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        result["unverified_quotes"] = meta.get("unverified_quotes", [])
        result["open_questions"] = meta.get("draft", {}).get("open_questions", result["open_questions"])
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m claimcheck.onboard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="register a payer and store its documents")
    a.add_argument("payer_id")
    a.add_argument("--name", required=True)
    a.add_argument("--alias", action="append", default=[], help="a spelling used in claims exports (repeatable)")
    a.add_argument("--doc", action="append", type=Path, default=[], help="a rule document (repeatable)")
    sub.add_parser("list", help="show onboarded payers")
    c = sub.add_parser("check", help="score a drafted rule file against an answer key")
    c.add_argument("proposal", type=Path, help="e.g. rules/proposed/PY04.json")
    c.add_argument("expected", type=Path, help="e.g. demo/lakeside/expected_PY04.json")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "check":
            r = check(args.proposal, args.expected)
            print(f"{len(r['matched'])}/{r['document_rules_in_approved_file']} expected rules drafted exactly")
            for d in r["parameter_differences"]:
                print(f"  parameters differ: {d['approved']}: expected {d['approved_params']}, drafted {d['proposed_params']}")
            print(f"  missing: {r['missing_from_proposal'] or 'none'}; extra: {r['extra_in_proposal'] or 'none'}; "
                  f"unverified quotes: {r.get('unverified_quotes') or 'none'}")
            for q in r["open_questions"]:
                print(f"  open question: \"{q['quote']}\" -> {q['question']}")
            return 0
        if args.cmd == "add":
            entry = add(args.payer_id, args.name, args.alias, args.doc)
            print(f"{args.payer_id} {entry['name']} registered; documents: {', '.join(entry['documents'])}")
            print(f"Next: python -m claimcheck.extract run {args.payer_id}")
        else:
            for pid, e in sorted(config.load_registry().items()):
                print(f"{pid} {e['name']} aliases={e['aliases']} documents={e['documents']}")
    except OnboardingError as exc:
        print(f"Onboarding failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
