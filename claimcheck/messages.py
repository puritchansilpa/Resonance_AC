"""Message drafts: Claude writes the note a person needs to act on a claim.

Code decides everything that matters: who the message is for, what it asks, and
the facts it may use, all taken from the decision record (prediction, fix plan,
evidence). Claude (Haiku, one short call per claim, as the brief suggests for
per-claim work) only turns those facts into a clear message.

Who gets a message (routing is a fixed table, not a model choice):
  care_coordinator    no verified visit: a visit pending approval, rejected, or missing
  payer_care_manager  authorization used up, too small, or not on file: ask for units
  billing_lead        decisions and write-offs: bill fewer units, late filing, duplicates

Guard: every claim, visit, authorization, member ID and date in the draft must
appear in the facts given. Drafts that mention anything else are flagged, never
silently sent. Messages are drafts for a person to send; nothing is sent.

Run: python -m claimcheck.messages draft C019213 C020532   (or --all, --limit N)
     -> output/messages/messages.jsonl, output/messages/messages.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

from . import config
from .llm import HAIKU, LLM_CACHE_DIR, LLMError, cached_call, make_request, missing_credentials, strict_object

DECISIONS_FILE = config.OUTPUT_DIR / "decisions" / "to_check.jsonl"
MESSAGES_DIR = config.OUTPUT_DIR / "messages"
CACHE_DIR = LLM_CACHE_DIR / "messages"
ID_PATTERN = re.compile(r"\b(?:C\d{6}|V\d{6}|AUTH\d{5}|PT\d{4}|AG\d{3}|\d{4}-\d{2}-\d{2})\b")


def load_decisions(path: Path = DECISIONS_FILE) -> dict[str, dict]:
    with open(path, encoding="utf-8") as fh:
        return {d["claim_id"]: d for d in map(json.loads, fh)}


def _blocker_text(d: dict) -> str:
    return " ".join((d.get("fix") or {}).get("blockers", []))


def route(d: dict) -> tuple[str, str] | None:
    """(recipient, purpose) for a decision, or None when no message is needed."""
    fix = d.get("fix") or {}
    outcome, code = fix.get("outcome"), d["denial_code"]
    blockers = _blocker_text(d)
    flags = {f["code"] for f in d.get("flags", [])}
    if code == "D01":
        if "awaiting aggregator approval" in blockers:
            return "care_coordinator", "Ask them to follow up the pending EVV visit with the aggregator; hold the claim until it is verified."
        if "was rejected" in blockers:
            return "care_coordinator", "Ask them to correct the rejected EVV visit record so the claim can be billed."
        return "care_coordinator", "Ask them to confirm whether this visit took place and was recorded; without a verified visit the claim cannot be billed."
    if code == "D02" or "is used up" in blockers:
        return "payer_care_manager", "Request an increase to the authorization so the remaining units can be billed."
    if code in ("D07", "D08") and outcome == "not_fixable":
        return "payer_care_manager", "Request a prior authorization covering this member, service and date of service."
    if code == "D06":
        return "billing_lead", "Decide whether to bill fewer units for this day; the member's daily maximum would be exceeded."
    if code == "D03":
        return "billing_lead", "Tell them the claim is past the payer's filing limit and cannot be fixed by editing; it needs a write-off decision."
    if code == "D05" and outcome == "not_fixable":
        return "billing_lead", "Tell them not to submit this claim; it duplicates one already billed."
    if outcome == "needs_decision":
        return "billing_lead", "Ask them to decide on the proposed change before resubmitting."
    if "PENDING_EVV" in flags:
        return "care_coordinator", "Ask them to follow up the pending EVV visit; the claim's outcome depends on it."
    return None


def facts_for(d: dict) -> dict:
    """Only what the message may use."""
    ev = d["evidence"]
    led = ev["ledger"]
    auth = ev.get("auth") or {}
    fix = d.get("fix") or {}
    return {
        "claim_id": d["claim_id"],
        "payer_id": d["payer_id"],
        "payer_name": config.PAYER_NAMES[d["payer_id"]],
        "predicted": d["denial_code"] or "paid",
        "reason": d["explanation"],
        "fix": fix.get("summary"),
        "blockers": fix.get("blockers", []),
        "billed_before": fix.get("billed_before"),
        "billed_after_fix": fix.get("billed_after") if fix.get("outcome") != "not_fixable" else None,
        "visits": [{k: v[k] for k in ("visit_id", "verification_status", "clock_in", "clock_out", "minutes")}
                   for v in ev["verified_visits"] + ev["unverified_visits"]],
        "authorization": {"auth_id": auth.get("auth_id"), "member": auth.get("patient_id"),
                          "start_date": auth.get("start_date"), "end_date": auth.get("end_date"),
                          "units_authorized": led.get("units_authorized"),
                          "units_remaining": led.get("units_remaining")} if auth else None,
        "days_to_submit": ev["days_to_submit"],
        "review_flags": [f["reason"] for f in d.get("flags", [])],
    }


def claim_facts(claim: dict) -> dict:
    return {k: claim[k] for k in ("claim_id", "agency_id", "patient_id", "service_code", "date_of_service",
                                  "submitted_date", "units", "billed_amount")}


SYSTEM = """You write short work messages for a home-care agency's billing team about a claim that is about to be submitted.
Write to the named recipient, for the stated purpose, using only the facts given. Do not add IDs, dates, amounts or reasons that are not in the facts, and do not promise outcomes.
Plain, polite and specific: the subject names the claim; the body is at most 120 words, says what is wrong, what you need from the recipient and by when it matters (for example before the filing limit). No greeting placeholders like [Name]."""


def build_request(claim: dict, d: dict, recipient: str, purpose: str, model: str = HAIKU) -> dict:
    content = json.dumps({"recipient": recipient, "purpose": purpose, "claim": claim_facts(claim),
                          "facts": facts_for(d)}, indent=1, sort_keys=True)
    schema = strict_object({"subject": {"type": "string"}, "body": {"type": "string"}})
    return make_request(model, "low", SYSTEM, [{"role": "user", "content": content}], schema)


def unknown_references(text: str, request: dict) -> list[str]:
    allowed = set(ID_PATTERN.findall(request["messages"][0]["content"]))
    return sorted(set(ID_PATTERN.findall(text)) - allowed)


def draft(claim_ids: list[str], claims: dict[str, dict], decisions: dict[str, dict],
          cache_dir: Path = CACHE_DIR, refresh: bool = False, client=None) -> list[dict]:
    out = []
    for cid in claim_ids:
        d = decisions[cid]
        r = route(d)
        if r is None:
            out.append({"claim_id": cid, "recipient": None, "status": "no_message_needed"})
            continue
        recipient, purpose = r
        request = build_request(claims[cid], d, recipient, purpose)
        result, _ = cached_call(request, cache_dir, refresh, client)
        msg = result["data"]
        unknown = unknown_references(msg["subject"] + " " + msg["body"], request)
        out.append({"claim_id": cid, "recipient": recipient, "purpose": purpose,
                    "subject": msg["subject"], "body": msg["body"],
                    "status": "check_references" if unknown else "draft",
                    "unknown_references": unknown, "model": result.get("model"),
                    "request_sha256": result.get("request_sha256")})
    return out


def write(messages: list[dict], out_dir: Path = MESSAGES_DIR) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "messages.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for m in messages:
            fh.write(json.dumps(m, sort_keys=True) + "\n")
    cols = ["claim_id", "recipient", "status", "subject", "body", "unknown_references"]
    with open(out_dir / "messages.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for m in messages:
            w.writerow({**m, "unknown_references": " ".join(m.get("unknown_references", []))})


def load_claims(clean_dir: Path = config.CLEAN_DIR) -> dict[str, dict]:
    with open(clean_dir / "claims_to_check.csv", newline="", encoding="utf-8") as fh:
        return {r["claim_id"]: r for r in csv.DictReader(fh)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m claimcheck.messages")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft", help="draft messages for claims that need someone to act")
    d.add_argument("claims", nargs="*")
    d.add_argument("--all", action="store_true", help="every claim-to-check that needs a message")
    d.add_argument("--limit", type=int, default=None)
    d.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    if not DECISIONS_FILE.exists():
        print("Run python -m claimcheck.predict first.", file=sys.stderr)
        return 1
    decisions, claims = load_decisions(), load_claims()
    ids = [c for c in decisions if route(decisions[c])] if args.all else args.claims
    if args.limit:
        ids = ids[:args.limit]
    if not ids:
        print("No claims given; pass claim IDs or --all.", file=sys.stderr)
        return 1
    try:
        msgs = draft(ids, claims, decisions, refresh=args.refresh)
    except LLMError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if missing_credentials(exc):
            print("No Anthropic credentials found. Set ANTHROPIC_API_KEY and run again.", file=sys.stderr)
            return 1
        raise
    write(msgs)
    by = {}
    for m in msgs:
        by[m["recipient"] or "none"] = by.get(m["recipient"] or "none", 0) + 1
    flagged = [m["claim_id"] for m in msgs if m["status"] == "check_references"]
    print(f"{len(msgs)} claims: {by}; drafts mentioning facts not given: {flagged or 'none'} -> {MESSAGES_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
