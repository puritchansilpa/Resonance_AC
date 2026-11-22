"""Rule extractor: Claude reads a payer's documents and drafts its rule file.

This is the only place a language model is used, and it is never in the path of
a claim decision. It runs offline, and its output is a *proposal* that a person
reviews and approves before the engine uses it.

  run       Claude reads the payer's manual and bulletins and fills the rule
            template (a fixed JSON schema built from the engine's rule types).
            One worked example (another payer's documents and approved rule file)
            is included. Lessons from earlier reviewer corrections are included too.
            -> rules/proposed/PY0x.json, rules/proposed/PY0x.meta.json
  evaluate  Extract every payer "leave one out" (the worked example is always a
            different payer), compare each proposal with the approved rule file,
            and backtest the claims history on the proposed rules.
            -> rules/proposed/evaluation.json
  approve   Promote a (reviewed, possibly hand-edited) proposal to rules/PY0x.json,
            keeping the learned rules and code order, and log every difference
            between the model's draft and what was approved as a correction.
            Corrections are fed into later extraction prompts.

Guardrails
- Structured output: the response must match the template's JSON schema, then
  passes the same validation as a hand-written rule file.
- Every rule must quote the document word for word; the quote is checked
  against the source text and unverifiable quotes are reported.
- Requirements the documents mention but don't state (Summit 9.5 "additional
  billing requirements may apply") go to open_questions, never into a rule.
- Repeatability: each response is cached under the SHA-256 of the exact request
  (model, prompt, schema). Re-running uses the cache, so the same documents always
  give the same proposal; --refresh calls the model again.

Run: python -m claimcheck.extract run PY03
     python -m claimcheck.extract evaluate
     python -m claimcheck.extract approve PY03 --by "Reviewer name" --note "why"
Needs ANTHROPIC_API_KEY for any request not already in the cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from . import config
from .rule_kinds import ROUNDINGS, RULE_KINDS
from .ruleset import RULES_DIR, RuleSetError, _validate_payer

MODEL = "claude-opus-5-5"
EFFORT = "high"
PROPOSED_DIR = RULES_DIR / "proposed"
CACHE_DIR = PROPOSED_DIR / "cache"
CORRECTIONS_FILE = RULES_DIR / "extraction_corrections.jsonl"
PAYERS_DIR = config.DATA_DIR / "payers"

DENIAL_CODES = ["D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10", "D99"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PARAM_SCHEMAS = {
    "rounding": {"type": "string", "enum": sorted(ROUNDINGS)},
    "days": {"type": "integer"},
    "service_codes": {"type": "array", "items": {"type": "string", "enum": sorted(config.SERVICE_CODES)}},
    "modifier": {"type": "string", "enum": sorted(config.MODIFIERS)},
    "days_of_week": {"type": "array", "items": {"type": "string", "enum": WEEKDAYS}},
    "max_units": {"type": "integer"},
    "units_per_hour": {"type": "integer"},
}
# Which payer's approved file serves as the worked example when extracting each payer.
# Never the payer being extracted, so the evaluation is leave-one-out.
EXAMPLE_FOR = {"PY01": "PY02", "PY02": "PY01", "PY03": "PY02"}


class ExtractionError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

def payer_documents(payer_id: str, payers_dir: Path = PAYERS_DIR) -> list[tuple[str, str]]:
    docs = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(payers_dir.glob(f"{payer_id}_*.md"))]
    if not docs:
        raise ExtractionError(f"No documents for {payer_id} in {payers_dir}")
    return docs


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def output_schema() -> dict:
    """The rule template as a JSON schema (structured output)."""
    params = {name: _nullable(s) for name, s in PARAM_SCHEMAS.items()}
    rule = {
        "type": "object",
        "additionalProperties": False,
        "required": ["rule_id", "kind", "denial_code", "params", "effective_from", "effective_to",
                     "source_file", "source_section", "quote"],
        "properties": {
            "rule_id": {"type": "string"},
            "kind": {"type": "string", "enum": sorted(RULE_KINDS)},
            "denial_code": {"type": "string", "enum": DENIAL_CODES},
            "params": {"type": "object", "additionalProperties": False,
                       "required": sorted(params), "properties": params},
            "effective_from": _nullable({"type": "string"}),
            "effective_to": _nullable({"type": "string"}),
            "source_file": {"type": "string"},
            "source_section": {"type": "string"},
            "quote": {"type": "string"},
        },
    }
    quoted = lambda extra: {  # noqa: E731
        "type": "object", "additionalProperties": False,
        "required": sorted(["quote", *extra]),
        "properties": {"quote": {"type": "string"}, **{k: {"type": "string"} for k in extra}},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["payer_id", "rules", "not_applied", "open_questions"],
        "properties": {
            "payer_id": {"type": "string"},
            "rules": {"type": "array", "items": rule},
            "not_applied": {"type": "array", "items": quoted(["topic"])},
            "open_questions": {"type": "array", "items": quoted(["question"])},
        },
    }


def system_prompt() -> str:
    kinds = "\n".join(
        f"- {name} -> parameters: {', '.join(list(k.params) + [p + ' (optional)' for p in k.optional]) or 'none'}. "
        f"{k.description}"
        for name, k in sorted(RULE_KINDS.items()))
    codes = (config.DATA_DIR / "denial_codes.md").read_text(encoding="utf-8")
    return f"""You turn a health plan's billing documents into a rule file for a home-care claims checker.
A deterministic engine runs the rule file against claims, and a billing specialist reviews your draft before it is used, so accuracy and traceability matter more than coverage.

The engine understands only these rule types:
{kinds}

Denial codes the payers use:
{codes}
How to fill the rule file:
1. One rule per requirement the documents state. Use only the rule types above. Every parameter the rule type does not use must be null.
2. Effective dates follow the date of service, are inclusive, and use YYYY-MM-DD. A rule with no date limits has both dates null. When a bulletin changes a manual rule from a given date, write two rules: the manual's version with effective_to set to the day before, and the bulletin's version with effective_from set to that date.
3. quote: copy the exact words from the document that state the rule, at most one sentence. Quotes are checked word for word against the document, and a paraphrase is rejected.
4. Extract only what this payer's own documents state. Do not add a rule because other payers have it or because a document is silent.
5. If the payer says a common rule does not apply to these providers (for example no filing limit), list it in not_applied with its quote.
6. If a document refers to requirements without stating them (for example "additional requirements may apply under your contract"), or states something none of the rule types can express, list it in open_questions with its quote and what a reviewer needs to find out. Never invent a rule to cover it.
7. Do not decide which code the payer reports when a claim breaks several rules; that order is learned separately from claims history.
8. rule_id: payer ID plus a short upper-case name, for example PY01-TIMELY-30."""


def _documents_block(payer_id: str, docs: list[tuple[str, str]]) -> str:
    parts = [f"Documents for payer {payer_id}:"]
    for name, text in docs:
        parts.append(f'<document name="{name}">\n{text.strip()}\n</document>')
    return "\n\n".join(parts)


def approved_to_extraction(approved: dict) -> dict:
    """Show an approved rule file in the template's shape (used as the worked example).

    Learned-from-history rules and the code order are left out: they don't come from documents.
    """
    rules = []
    for r in approved["rules"]:
        if r["source"]["type"] != "document":
            continue
        ref = r["source"]["ref"]
        file, _, section = ref.partition(" ")
        eff = r.get("effective", {})
        rules.append({
            "rule_id": r["rule_id"], "kind": r["kind"], "denial_code": r["denial_code"],
            "params": {name: r["params"].get(name) for name in PARAM_SCHEMAS},
            "effective_from": eff.get("from"), "effective_to": eff.get("to"),
            "source_file": file, "source_section": section, "quote": r["source"]["quote"],
        })
    return {
        "payer_id": approved["payer_id"],
        "rules": rules,
        "not_applied": [{"topic": n["topic"], "quote": n["quote"]} for n in approved.get("not_applied", [])
                        if "quote" in n],
        "open_questions": [],
    }


def load_corrections(path: Path = CORRECTIONS_FILE) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_request(payer_id: str, rules_dir: Path = RULES_DIR, payers_dir: Path = PAYERS_DIR,
                  corrections: list[dict] | None = None, model: str = MODEL) -> dict:
    example_id = EXAMPLE_FOR[payer_id]
    example = json.loads((rules_dir / f"{example_id}.json").read_text(encoding="utf-8"))
    corrections = load_corrections() if corrections is None else corrections
    target = _documents_block(payer_id, payer_documents(payer_id, payers_dir))
    if corrections:
        lessons = "\n".join(f"- {c['lesson']}" for c in corrections)
        target = (f"Lessons from earlier drafts that a reviewer corrected:\n{lessons}\n\n" + target)
    messages = [
        {"role": "user", "content": "Worked example.\n\n" + _documents_block(example_id, payer_documents(example_id, payers_dir))},
        {"role": "assistant", "content": json.dumps(approved_to_extraction(example), indent=1)},
        {"role": "user", "content": target + f"\n\nWrite the rule file for {payer_id}."},
    ]
    return {"model": model, "effort": EFFORT, "system": system_prompt(), "messages": messages,
            "schema": output_schema()}


def request_key(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Calling Claude (with a cache keyed by the exact request)
# --------------------------------------------------------------------------- #

def call_claude(request: dict, client=None) -> dict:
    import anthropic

    client = client or anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=request["model"],
            max_tokens=16000,
            # On a safety decline, the API re-runs the request on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": request["system"], "cache_control": {"type": "ephemeral"}}],
            messages=request["messages"],
            output_config={"effort": request["effort"],
                           "format": {"type": "json_schema", "schema": request["schema"]}},
        )
    except anthropic.AuthenticationError as exc:
        raise ExtractionError("The API key was rejected; check ANTHROPIC_API_KEY.") from exc
    except anthropic.RateLimitError as exc:
        raise ExtractionError("Rate limited by the API; try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        raise ExtractionError(f"API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ExtractionError("Could not reach the API (network).") from exc

    if response.stop_reason == "refusal":
        raise ExtractionError("The model declined the request.")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("The response was cut off at max_tokens.")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ExtractionError("No text in the response.")
    return {"text": text, "model": response.model, "stop_reason": response.stop_reason,
            "request_id": getattr(response, "_request_id", None),
            "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}}


def cached_call(request: dict, cache_dir: Path = CACHE_DIR, refresh: bool = False, client=None) -> tuple[dict, bool]:
    key = request_key(request)
    path = cache_dir / f"{key}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8")), True
    result = call_claude(request, client)
    result["request_sha256"] = key
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return result, False


# --------------------------------------------------------------------------- #
# Checking and converting the model's draft
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    text = text.replace("**", "").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def unverified_quotes(draft: dict, docs: list[tuple[str, str]]) -> list[str]:
    corpus = _norm(" ".join(text for _, text in docs))
    items = [(r["rule_id"], r["quote"]) for r in draft["rules"]]
    items += [(f"not_applied:{n['topic']}", n["quote"]) for n in draft["not_applied"]]
    items += [(f"open_question:{i}", q["quote"]) for i, q in enumerate(draft["open_questions"])]
    return [label for label, quote in items if not quote.strip() or _norm(quote).strip('"') not in corpus]


def draft_to_rule_file(draft: dict, payer_id: str, precedence: list[str]) -> dict:
    """Turn the model's draft into the rule-file format the engine loads."""
    if draft["payer_id"] != payer_id:
        raise ExtractionError(f"Draft is for {draft['payer_id']}, expected {payer_id}")
    rules = []
    for r in draft["rules"]:
        rule = {
            "rule_id": r["rule_id"], "kind": r["kind"], "denial_code": r["denial_code"],
            "params": {k: v for k, v in r["params"].items() if v is not None},
            "source": {"type": "document", "ref": f"{r['source_file']} {r['source_section']}".strip(),
                       "quote": r["quote"]},
        }
        eff = {k: v for k, v in (("from", r["effective_from"]), ("to", r["effective_to"])) if v}
        if eff:
            rule["effective"] = {"basis": "date_of_service", **eff}
        rules.append(rule)
    codes = [c for c in precedence] + sorted({r["denial_code"] for r in rules} - set(precedence))
    return {
        "payer_id": payer_id,
        "payer_name": config.PAYER_NAMES[payer_id],
        "denial_precedence": codes,
        "precedence_source": {"type": "learned_from_history",
                              "note": "Copied from the approved rule file; the extractor does not set code order."},
        "rules": rules,
        "not_applied": [{"topic": n["topic"], "note": "Stated in the payer's documents.", "quote": n["quote"]}
                        for n in draft["not_applied"]],
        "open_questions": draft["open_questions"],
    }


def _approved(payer_id: str, rules_dir: Path) -> dict | None:
    path = rules_dir / f"{payer_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def extract(payer_id: str, rules_dir: Path = RULES_DIR, out_dir: Path = PROPOSED_DIR,
            payers_dir: Path = PAYERS_DIR, refresh: bool = False, client=None, model: str = MODEL,
            corrections: list[dict] | None = None) -> dict:
    request = build_request(payer_id, rules_dir, payers_dir, corrections, model)
    result, from_cache = cached_call(request, out_dir / "cache", refresh, client)
    try:
        draft = json.loads(result["text"])
    except json.JSONDecodeError as exc:
        raise ExtractionError("The response was not valid JSON.") from exc
    approved = _approved(payer_id, rules_dir)
    precedence = approved["denial_precedence"] if approved else list(DENIAL_CODES)
    proposal = draft_to_rule_file(draft, payer_id, precedence)
    try:
        _validate_payer(proposal)
        valid, error = True, ""
    except RuleSetError as exc:
        valid, error = False, str(exc)
    meta = {
        "payer_id": payer_id,
        "model": result.get("model", model),
        "request_sha256": request_key(request),
        "from_cache": from_cache,
        "worked_example": EXAMPLE_FOR[payer_id],
        "corrections_in_prompt": len(request["messages"][2]["content"].split("\n- ")) - 1
        if "Lessons from earlier drafts" in request["messages"][2]["content"] else 0,
        "valid": valid,
        "validation_error": error,
        "unverified_quotes": unverified_quotes(draft, payer_documents(payer_id, payers_dir)),
        "usage": result.get("usage"),
        "draft": draft,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{payer_id}.json").write_text(json.dumps(proposal, indent=2) + "\n", encoding="utf-8", newline="\n")
    # from_cache is reported to the caller but not saved, so re-runs write byte-identical files.
    saved = {k: v for k, v in meta.items() if k != "from_cache"}
    (out_dir / f"{payer_id}.meta.json").write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {"proposal": proposal, "meta": meta}


# --------------------------------------------------------------------------- #
# Comparing a proposal with the approved rule file
# --------------------------------------------------------------------------- #

def _canon_params(params: dict) -> tuple:
    out = []
    for k, v in sorted(params.items()):
        if v is None:
            continue
        if k == "days_of_week":
            v = tuple(sorted(v, key=WEEKDAYS.index))
        elif isinstance(v, list):
            v = tuple(sorted(v))
        out.append((k, v))
    return tuple(out)


def _shape(rule: dict) -> tuple:
    eff = rule.get("effective", {})
    return (rule["kind"], rule["denial_code"], eff.get("from"), eff.get("to"))


def compare(proposal: dict, approved: dict) -> dict:
    """Match rules by meaning (type, code, effective dates, parameters), not by ID."""
    expected = [r for r in approved["rules"] if r["source"]["type"] == "document"]
    learned = [r["rule_id"] for r in approved["rules"] if r["source"]["type"] != "document"]
    remaining = list(proposal["rules"])
    matched, param_diffs, missing = [], [], []
    for a in expected:
        exact = next((p for p in remaining if _shape(p) == _shape(a)
                      and _canon_params(p["params"]) == _canon_params(a["params"])), None)
        if exact:
            matched.append({"approved": a["rule_id"], "proposed": exact["rule_id"]})
            remaining.remove(exact)
            continue
        near = next((p for p in remaining if _shape(p) == _shape(a)), None)
        if near:
            param_diffs.append({"approved": a["rule_id"], "proposed": near["rule_id"],
                                "approved_params": a["params"], "proposed_params": near["params"]})
            remaining.remove(near)
        else:
            missing.append(a["rule_id"])
    na_expected = {n["topic"] for n in approved.get("not_applied", []) if "quote" in n}
    na_proposed = {n["topic"] for n in proposal.get("not_applied", [])}
    n = len(expected)
    return {
        "document_rules_in_approved_file": n,
        "matched": matched,
        "parameter_differences": param_diffs,
        "missing_from_proposal": missing,
        "extra_in_proposal": [p["rule_id"] for p in remaining],
        "learned_rules_not_expected_from_documents": learned,
        "not_applied_missing": sorted(na_expected - na_proposed),
        "open_questions": proposal.get("open_questions", []),
        "exact_match_rate": round(len(matched) / n, 4) if n else 1.0,
    }


def merge_for_use(proposal: dict, approved: dict | None) -> dict:
    """What would go live: the proposal's document rules + the approved learned rules and code order."""
    merged = json.loads(json.dumps(proposal))
    if approved:
        merged["rules"] += [r for r in approved["rules"] if r["source"]["type"] != "document"]
        merged["denial_precedence"] = list(approved["denial_precedence"])
        merged["precedence_source"] = approved["precedence_source"]
        missing = sorted({r["denial_code"] for r in merged["rules"]} - set(merged["denial_precedence"]))
        merged["denial_precedence"] += missing
    merged.pop("open_questions", None)
    return merged


def evaluate(rules_dir: Path = RULES_DIR, out_dir: Path = PROPOSED_DIR, refresh: bool = False,
             client=None, clean_dir: Path = config.CLEAN_DIR) -> dict:
    from .predict import backtest, run_all

    report = {"model": MODEL, "payers": {}}
    with tempfile.TemporaryDirectory() as tmp:
        trial = Path(tmp)
        if (rules_dir / "precedence_evidence.json").exists():
            shutil.copy(rules_dir / "precedence_evidence.json", trial / "precedence_evidence.json")
        for pid in sorted(EXAMPLE_FOR):
            res = extract(pid, rules_dir, out_dir, refresh=refresh, client=client)
            approved = _approved(pid, rules_dir)
            cmp = compare(res["proposal"], approved)
            cmp["valid"] = res["meta"]["valid"]
            cmp["validation_error"] = res["meta"]["validation_error"]
            cmp["unverified_quotes"] = res["meta"]["unverified_quotes"]
            report["payers"][pid] = cmp
            (trial / f"{pid}.json").write_text(json.dumps(merge_for_use(res["proposal"], approved)), encoding="utf-8")

        def score(rdir):
            r = run_all(clean_dir, rdir)
            return backtest(r.history, r.hist_dec, set(r.check_dec))[1], r
        if all(p["valid"] for p in report["payers"].values()):
            proposed_summary, proposed_run = score(trial)
            approved_summary, approved_run = score(rules_dir)
            changed = [c.claim_id for c in proposed_run.to_check
                       if (proposed_run.check_dec[c.claim_id].denial_code
                           != approved_run.check_dec[c.claim_id].denial_code)]
            report["backtest"] = {
                "approved_rules": {k: approved_summary[k] for k in
                                   ("exact_code_accuracy", "exact_code_accuracy_excluding_opaque_d99")},
                "extracted_rules": {k: proposed_summary[k] for k in
                                    ("exact_code_accuracy", "exact_code_accuracy_excluding_opaque_d99")},
                "claims_to_check_with_a_different_prediction": changed,
            }
        else:
            report["backtest"] = "skipped: a proposal failed validation"
    (out_dir / "evaluation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


# --------------------------------------------------------------------------- #
# Approval: reviewer promotes a proposal and the differences become lessons
# --------------------------------------------------------------------------- #

def _describe(rule: dict) -> str:
    eff = rule.get("effective", {})
    when = ""
    if eff.get("from") or eff.get("to"):
        when = f" for dates of service {eff.get('from') or '...'} to {eff.get('to') or '...'}"
    params = ", ".join(f"{k}={v}" for k, v in _canon_params(rule["params"])) or "no parameters"
    return f"{rule['kind']} ({rule['denial_code']}, {params}){when}"


def corrections_between(draft_file: dict, approved_file: dict, payer_id: str, note: str) -> list[dict]:
    cmp = compare(draft_file, approved_file)
    by_id_d = {r["rule_id"]: r for r in draft_file["rules"]}
    by_id_a = {r["rule_id"]: r for r in approved_file["rules"]}
    suffix = f" Reviewer note: {note}" if note else ""
    out = []
    for d in cmp["parameter_differences"]:
        out.append({"payer_id": payer_id, "type": "parameters",
                    "lesson": f"{payer_id}: drafted {_describe(by_id_d[d['proposed']])}; reviewer approved "
                              f"{_describe(by_id_a[d['approved']])}.{suffix}"})
    for rid in cmp["missing_from_proposal"]:
        out.append({"payer_id": payer_id, "type": "missed",
                    "lesson": f"{payer_id}: the draft missed {_describe(by_id_a[rid])}, stated as "
                              f"\"{by_id_a[rid]['source']['quote']}\".{suffix}"})
    for rid in cmp["extra_in_proposal"]:
        out.append({"payer_id": payer_id, "type": "removed",
                    "lesson": f"{payer_id}: the reviewer removed drafted rule {_describe(by_id_d[rid])}.{suffix}"})
    return out


def approve(payer_id: str, approver: str, note: str = "", rules_dir: Path = RULES_DIR,
            out_dir: Path = PROPOSED_DIR, corrections_file: Path = CORRECTIONS_FILE,
            today: date | None = None) -> dict:
    proposal = json.loads((out_dir / f"{payer_id}.json").read_text(encoding="utf-8"))
    meta = json.loads((out_dir / f"{payer_id}.meta.json").read_text(encoding="utf-8"))
    current = _approved(payer_id, rules_dir)
    approved = merge_for_use(proposal, current)
    approved["approval"] = {
        "approved_by": approver,
        "approved_on": (today or date.today()).isoformat(),
        "extraction_request_sha256": meta["request_sha256"],
        "model": meta["model"],
        "note": note,
    }
    _validate_payer(approved)
    # The model's untouched draft, so edits the reviewer made to the proposal file count as corrections.
    original = draft_to_rule_file(meta["draft"], payer_id, approved["denial_precedence"])
    lessons = corrections_between(original, proposal, payer_id, note)
    if lessons:
        with open(corrections_file, "a", encoding="utf-8", newline="\n") as fh:
            for c in lessons:
                fh.write(json.dumps({**c, "approved_on": approved["approval"]["approved_on"]}, sort_keys=True) + "\n")
    (rules_dir / f"{payer_id}.json").write_text(json.dumps(approved, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {"approved": approved, "corrections_logged": lessons}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m claimcheck.extract")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="draft rule files from payer documents")
    r.add_argument("payers", nargs="*", default=sorted(EXAMPLE_FOR))
    r.add_argument("--refresh", action="store_true", help="ignore the cache and call the model again")
    e = sub.add_parser("evaluate", help="leave-one-out extraction, compare with approved, backtest")
    e.add_argument("--refresh", action="store_true")
    a = sub.add_parser("approve", help="promote a reviewed proposal to rules/")
    a.add_argument("payer")
    a.add_argument("--by", required=True, help="who approved it")
    a.add_argument("--note", default="", help="why the draft was changed, fed into later prompts")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "run":
            for pid in args.payers:
                res = extract(pid, refresh=args.refresh)
                m = res["meta"]
                print(f"{pid}: {len(res['proposal']['rules'])} rules drafted "
                      f"({'cache' if m['from_cache'] else 'model call'}), valid={m['valid']}, "
                      f"unverified quotes={m['unverified_quotes'] or 'none'}, "
                      f"open questions={len(res['proposal']['open_questions'])}")
        elif args.cmd == "evaluate":
            rep = evaluate(refresh=args.refresh)
            for pid, c in rep["payers"].items():
                print(f"{pid}: {len(c['matched'])}/{c['document_rules_in_approved_file']} rules match exactly; "
                      f"param diffs {len(c['parameter_differences'])}, missing {c['missing_from_proposal']}, "
                      f"extra {c['extra_in_proposal']}, unverified quotes {c['unverified_quotes'] or 'none'}, "
                      f"open questions {len(c['open_questions'])}")
            print(f"Backtest: {json.dumps(rep['backtest'], indent=1)}")
        else:
            res = approve(args.payer, args.by, args.note)
            print(f"{args.payer} approved by {args.by}; {len(res['corrections_logged'])} correction(s) logged "
                  f"to {CORRECTIONS_FILE.name}")
    except ExtractionError as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # missing credentials surface here from the SDK constructor
        if type(exc).__name__ in ("AnthropicError",) or "api_key" in str(exc).lower():
            print("No Anthropic credentials found. Set ANTHROPIC_API_KEY (the key from the exercise email) "
                  "and run again. Cached extractions work without it.", file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
