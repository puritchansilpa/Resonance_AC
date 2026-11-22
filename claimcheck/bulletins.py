"""Bulletin step: when a payer issues a bulletin, Claude drafts only the change.

Payers change rules through short bulletins. Re-extracting a whole manual for
every bulletin risks changing rules nobody touched, so this step shows Claude the
payer's current approved rule file plus the new bulletin, and asks only for:
  end_rules   existing rules the bulletin replaces, with the last date they apply
  add_rules   new rules the bulletin introduces, quoting the bulletin
  open_questions  wording a reviewer should confirm
Code applies the change to the current file, validates it, checks every quote
against the bulletin, and writes a proposal for the usual approval.

Evaluation uses the two real bulletins: each payer's file is rebuilt as it stood
before its bulletin, Claude drafts the change (the other bulletin is the worked
example), and the result is compared with today's approved file.

Run: python -m claimcheck.bulletins draft PY02 Data/payers/PY02_harbor_bulletin_2026-08.md
     python -m claimcheck.bulletins evaluate
     python -m claimcheck.extract approve PY02 --by "Name" --proposal rules/proposed/bulletins/<name>.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

from . import extract
from .llm import LLM_CACHE_DIR, OPUS, LLMError, cached_call, make_request, missing_credentials, \
    request_key, strict_object
from .ruleset import RULES_DIR, RuleSetError, _validate_payer

BULLETIN_DIR = extract.PROPOSED_DIR / "bulletins"
CACHE_DIR = LLM_CACHE_DIR / "bulletins"
# Real bulletins in the data pack, and which one is the worked example for the other.
KNOWN_BULLETINS = {"PY01": "PY01_northwind_bulletin_2026-07.md", "PY02": "PY02_harbor_bulletin_2026-08.md"}
EXAMPLE_FOR = {"PY01": "PY02", "PY02": "PY01"}


def _copy(x):
    return json.loads(json.dumps(x))


def pre_bulletin_state(approved: dict, bulletin_name: str) -> dict:
    """The rule file as it stood before a bulletin: its rules removed, the rules it ended reopened."""
    raw = _copy(approved)
    from_bulletin = [r for r in raw["rules"] if r["source"]["ref"].startswith(bulletin_name)]
    starts = {r.get("effective", {}).get("from") for r in from_bulletin}
    day_before = {(date.fromisoformat(s) - timedelta(days=1)).isoformat() for s in starts if s}
    kept = []
    for r in raw["rules"]:
        if r in from_bulletin:
            continue
        eff = r.get("effective")
        if eff and eff.get("to") in day_before:
            eff.pop("to")
            if set(eff) == {"basis"}:
                r.pop("effective")
        kept.append(r)
    raw["rules"] = kept
    return raw


def expected_delta(approved: dict, bulletin_name: str) -> dict:
    """The change a bulletin made, in the delta shape (used as the worked example)."""
    before = {r["rule_id"]: r for r in pre_bulletin_state(approved, bulletin_name)["rules"]}
    added = [r for r in approved["rules"] if r["source"]["ref"].startswith(bulletin_name)]
    quote = added[0]["source"]["quote"] if added else ""
    ended = []
    for r in approved["rules"]:
        to = r.get("effective", {}).get("to")
        if r["rule_id"] in before and to and before[r["rule_id"]].get("effective", {}).get("to") != to:
            ended.append({"rule_id": r["rule_id"], "effective_to": to, "quote": quote,
                          "reason": "The bulletin replaces this rule from the following day."})
    adds = extract.approved_to_extraction({"payer_id": approved["payer_id"], "rules": added})["rules"]
    return {"payer_id": approved["payer_id"], "end_rules": ended, "add_rules": adds, "open_questions": []}


def delta_schema() -> dict:
    end_rule = strict_object({"rule_id": {"type": "string"}, "effective_to": {"type": "string"},
                              "quote": {"type": "string"}, "reason": {"type": "string"}})
    question = strict_object({"quote": {"type": "string"}, "question": {"type": "string"}})
    return strict_object({"payer_id": {"type": "string"},
                          "end_rules": {"type": "array", "items": end_rule},
                          "add_rules": {"type": "array", "items": extract.rule_schema()},
                          "open_questions": {"type": "array", "items": question}})


def system_prompt() -> str:
    return extract.system_prompt() + """

Your task here is narrower: a payer has issued a bulletin. You are given the payer's current approved rule file and the bulletin. Write only the change the bulletin makes:
- end_rules: existing rules the bulletin replaces or stops, by rule_id, with effective_to set to the last date of service they still apply.
- add_rules: rules the bulletin introduces, with their effective dates, quoting the bulletin.
- Do not restate, rename or edit rules the bulletin does not change.
- If the bulletin's wording leaves something a reviewer should confirm (for example whether a date means "on" or "on or after"), add an open question with the quote."""


def _current_block(rule_file: dict) -> str:
    shown = extract.approved_to_extraction(rule_file)
    return "Current approved rule file:\n" + json.dumps({"payer_id": shown["payer_id"], "rules": shown["rules"]}, indent=1)


def build_request(current: dict, bulletin_name: str, bulletin_text: str, example: tuple | None,
                  model: str = OPUS) -> dict:
    messages = []
    if example:
        ex_file, ex_name, ex_text, ex_delta = example
        messages += [
            {"role": "user", "content": f"Worked example.\n\n{_current_block(ex_file)}\n\n"
                                        f'<document name="{ex_name}">\n{ex_text.strip()}\n</document>'},
            {"role": "assistant", "content": json.dumps(ex_delta, indent=1)},
        ]
    messages.append({"role": "user", "content": f"{_current_block(current)}\n\n"
                                                f'<document name="{bulletin_name}">\n{bulletin_text.strip()}\n</document>'
                                                f"\n\nWrite the change for {current['payer_id']}."})
    return make_request(model, "high", system_prompt(), messages, delta_schema())


def apply_delta(current: dict, delta: dict) -> dict:
    new = _copy(current)
    by_id = {r["rule_id"]: r for r in new["rules"]}
    for e in delta["end_rules"]:
        if e["rule_id"] not in by_id:
            raise LLMError(f"The draft ends rule {e['rule_id']}, which is not in the current file.")
        eff = by_id[e["rule_id"]].setdefault("effective", {"basis": "date_of_service"})
        eff["to"] = e["effective_to"]
    for r in delta["add_rules"]:
        if r["rule_id"] in by_id:
            raise LLMError(f"The draft adds {r['rule_id']}, but that ID already exists.")
        new["rules"].append(extract.rule_from_draft(r))
    codes = {r["denial_code"] for r in new["rules"]}
    new["denial_precedence"] += sorted(codes - set(new["denial_precedence"]))
    return new


def unverified_quotes(delta: dict, bulletin_text: str) -> list[str]:
    corpus = extract._norm(bulletin_text)
    items = [(f"end:{e['rule_id']}", e["quote"]) for e in delta["end_rules"]]
    items += [(r["rule_id"], r["quote"]) for r in delta["add_rules"]]
    items += [(f"open_question:{i}", q["quote"]) for i, q in enumerate(delta["open_questions"])]
    return [label for label, q in items if not q.strip() or extract._norm(q).strip('"') not in corpus]


def _example(payer_id: str, rules_dir: Path, payers_dir: Path):
    ex = EXAMPLE_FOR.get(payer_id)
    if not ex:
        return None
    approved = json.loads((rules_dir / f"{ex}.json").read_text(encoding="utf-8"))
    name = KNOWN_BULLETINS[ex]
    return (pre_bulletin_state(approved, name), name, (payers_dir / name).read_text(encoding="utf-8"),
            expected_delta(approved, name))


def draft_change(payer_id: str, bulletin_path: Path, current: dict, rules_dir: Path = RULES_DIR,
                 payers_dir: Path = extract.PAYERS_DIR, out_dir: Path = BULLETIN_DIR,
                 cache_dir: Path = CACHE_DIR, refresh: bool = False, client=None) -> dict:
    text = bulletin_path.read_text(encoding="utf-8")
    request = build_request(current, bulletin_path.name, text, _example(payer_id, rules_dir, payers_dir))
    result, from_cache = cached_call(request, cache_dir, refresh, client)
    delta = result["data"]
    proposal = apply_delta(current, delta)
    try:
        _validate_payer(proposal)
        valid, error = True, ""
    except RuleSetError as exc:
        valid, error = False, str(exc)
    meta = {"payer_id": payer_id, "bulletin": bulletin_path.name, "model": result.get("model"),
            "request_sha256": request_key(request), "valid": valid, "validation_error": error,
            "unverified_quotes": unverified_quotes(delta, text), "usage": result.get("usage"),
            "draft": delta, "original_proposal": proposal}
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = bulletin_path.stem
    (out_dir / f"{stem}.json").write_text(json.dumps(proposal, indent=2) + "\n", encoding="utf-8", newline="\n")
    (out_dir / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {"proposal": proposal, "meta": meta, "from_cache": from_cache}


def evaluate(rules_dir: Path = RULES_DIR, payers_dir: Path = extract.PAYERS_DIR, out_dir: Path = BULLETIN_DIR,
             cache_dir: Path = CACHE_DIR, refresh: bool = False, client=None) -> dict:
    report = {}
    with tempfile.TemporaryDirectory() as tmp:
        for pid, name in sorted(KNOWN_BULLETINS.items()):
            approved = json.loads((rules_dir / f"{pid}.json").read_text(encoding="utf-8"))
            before = pre_bulletin_state(approved, name)
            res = draft_change(pid, payers_dir / name, before, rules_dir, payers_dir, Path(tmp), cache_dir,
                               refresh, client)
            cmp = extract.compare(res["proposal"], approved)
            expected_ends = {e["rule_id"]: e["effective_to"] for e in expected_delta(approved, name)["end_rules"]}
            drafted_ends = {e["rule_id"]: e["effective_to"] for e in res["meta"]["draft"]["end_rules"]}
            untouched = [r["rule_id"] for r in before["rules"]
                         if r["rule_id"] not in drafted_ends
                         and r == next((x for x in res["proposal"]["rules"] if x["rule_id"] == r["rule_id"]), None)]
            report[pid] = {
                "bulletin": name,
                "rules_matching_approved": f"{len(cmp['matched'])}/{cmp['document_rules_in_approved_file']}",
                "parameter_differences": cmp["parameter_differences"],
                "missing": cmp["missing_from_proposal"],
                "extra": cmp["extra_in_proposal"],
                "end_dates_correct": drafted_ends == expected_ends,
                "drafted_end_rules": drafted_ends,
                "rules_left_untouched": f"{len(untouched)}/{len(before['rules']) - len(expected_ends)}",
                "unverified_quotes": res["meta"]["unverified_quotes"],
                "open_questions": res["meta"]["draft"]["open_questions"],
                "valid": res["meta"]["valid"],
            }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evaluation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m claimcheck.bulletins")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft", help="draft the change a new bulletin makes to a payer's rule file")
    d.add_argument("payer")
    d.add_argument("bulletin", type=Path)
    d.add_argument("--refresh", action="store_true")
    e = sub.add_parser("evaluate", help="replay the two real bulletins against the pre-bulletin rule files")
    e.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "draft":
            current = json.loads((RULES_DIR / f"{args.payer}.json").read_text(encoding="utf-8"))
            res = draft_change(args.payer, args.bulletin, current, refresh=args.refresh)
            m = res["meta"]
            print(f"{args.payer}: ends {len(m['draft']['end_rules'])} rule(s), adds {len(m['draft']['add_rules'])}; "
                  f"valid={m['valid']}, unverified quotes={m['unverified_quotes'] or 'none'}, "
                  f"open questions={len(m['draft']['open_questions'])} -> {BULLETIN_DIR / (args.bulletin.stem + '.json')}")
        else:
            for pid, r in evaluate(refresh=args.refresh).items():
                print(f"{pid} {r['bulletin']}: {r['rules_matching_approved']} rules match approved, "
                      f"end dates correct={r['end_dates_correct']}, untouched {r['rules_left_untouched']}, "
                      f"missing {r['missing']}, extra {r['extra']}, unverified quotes {r['unverified_quotes'] or 'none'}, "
                      f"open questions {len(r['open_questions'])}")
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
