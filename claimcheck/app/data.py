"""App data layer: runs the pipeline once, then answers the screens' questions.

Everything the screens show is computed by the same code as the command-line
tools (predict, fixer, review, automator, extract, bulletins, hypotheses,
messages). Actions that change rules (approve a payer, a notice or a learned
rule) write the rule files and then rebuild everything, so every number on
every screen reflects the new rule-set version.
"""
from __future__ import annotations

import json
import re
import threading
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from .. import automator, bulletins, config, extract, hypotheses, messages, onboard
from ..checker import Checker
from ..evidence import EvidenceBuilder, _jsonable
from ..fixer import Fixer
from ..ledger import Ledger, simulate
from ..llm import LLMError, missing_credentials
from ..models import load_authorizations, load_visits
from ..predict import backtest, decision_to_dict, fix_summary, plan_to_dict, run_all
from ..ruleset import RULES_DIR

PAGE_SIZE = 50
NOTICE_DIR = config.ONBOARDING_DIR / "notices"
DEMO_DIR = config.REPO_ROOT / "demo"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(md|txt)$")
FLAG_LABELS = {
    "DEPENDS_ON_PREDICTED": "Depends on a prediction", "LEARNED_RULE": "Learned rule", "FIX_RIPPLE": "Fix ripple",
    "CORRECTED_CLAIM": "Corrected claim", "PENDING_EVV": "Visit pending", "ORDER_ASSUMPTION": "Order assumption",
    "UNOBSERVED_PRECEDENCE": "Code order unknown", "CONTRADICTED_BY_HISTORY": "Contradicted by history",
}


class ActionError(ValueError):
    """A user-facing error from an action (shown in the app)."""


def _page(rows: list, page: int) -> dict:
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    return {"total": len(rows), "page": page, "pages": pages,
            "rows": rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]}


def _match(q: str, *fields) -> bool:
    q = q.strip().lower()
    return not q or any(q in str(f).lower() for f in fields)


def _payer_short(pid: str) -> str:
    return config.PAYER_NAMES.get(pid, pid).split(" ")[0]


class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.reload()

    # ------------------------------------------------------------------ build
    def reload(self) -> None:
        with self.lock:
            config.register_payer_names(config.load_registry())
            r = run_all()
            self.run = r
            self.visits = load_visits()
            self.auths = load_authorizations()
            self.checker = Checker(r.ruleset, EvidenceBuilder(self.visits, self.auths))
            self.claims = {c.claim_id: c for c in r.history + r.to_check}
            self.backtest = backtest(r.history, r.hist_dec, set(r.check_dec))[1]
            self.fixes = fix_summary(r)
            self._history_plans()
            self.drafts = automator.draft_all(self.visits, r.history + r.to_check, self.auths, self.checker, r.ledger)
            self.drafts_by_key = {d.key: d for d in self.drafts}
            self._index_visits()
            self._index_auths()
            self.messages = self._load_messages()

    def _history_plans(self) -> None:
        """Fix plans for denied history claims, judged on the ledger state the payer saw."""
        ledger, fixer, plans = Ledger(self.auths), Fixer(self.checker), {}

        def observe(c, view):
            if c.status == "denied":
                plans[c.claim_id] = fixer.plan(c, self.run.hist_dec[c.claim_id], ledger)

        simulate(self.run.history, self.run.to_check, self.auths,
                 lambda c, v: self.run.check_dec[c.claim_id].status, observe, ledger=ledger)
        self.history_plans = plans

    def _index_visits(self) -> None:
        by_key = defaultdict(list)
        for c in self.claims.values():
            by_key[(c.patient_id, c.service_code, c.date_of_service)].append(c)
        for cs in by_key.values():
            cs.sort(key=lambda c: (c.submitted_date, c.claim_id))
        self.claims_by_key = by_key
        self.visits_by_key = defaultdict(list)
        for v in self.visits:
            self.visits_by_key[(v.patient_id, v.service_code, v.service_date)].append(v)
        self.visit_rows = [self._visit_row(v) for v in sorted(self.visits, key=lambda v: (v.clock_in, v.visit_id), reverse=True)]

    def _claim_label(self, c) -> dict:
        if c.dataset == "to_check":
            d = self.run.check_dec[c.claim_id]
            return {"id": c.claim_id, "kind": "waiting",
                    "label": "To submit · likely " + ("paid" if d.status == "paid" else d.denial_code)}
        return {"id": c.claim_id, "kind": c.status, "label": "Paid" if c.status == "paid" else f"Denied {c.denial_code}"}

    def _visit_row(self, v) -> dict:
        key = (v.patient_id, v.service_code, v.service_date)
        cs = self.claims_by_key.get(key, [])
        if v.verification_status != "verified":
            state = "fix"
            action = ("Waiting for aggregator approval; bill once verified." if v.verification_status == "manual_edit_pending"
                      else "Rejected: correct the EVV record with the coordinator before billing.")
        elif not cs:
            state = "unbilled"
            d = self.drafts_by_key.get(f"{v.patient_id}|{v.service_code}|{v.service_date.isoformat()}")
            action = self._draft_action(d) if d else "Not on any claim."
        elif any(c.dataset == "to_check" for c in cs):
            state = "waiting"
            c = next(c for c in cs if c.dataset == "to_check")
            d, p = self.run.check_dec[c.claim_id], self.run.plans[c.claim_id]
            action = "Ready to submit." if d.status == "paid" else p.summary()
        elif any(c.status == "paid" for c in cs):
            state, action = "paid", "Nothing to do."
        else:
            state = "denied"
            c = cs[-1]
            p = self.history_plans.get(c.claim_id)
            action = f"Denied {c.denial_code}. " + (p.summary() if p else "")
        return {"id": v.visit_id, "caregiver": v.caregiver_id, "member": v.patient_id, "service": v.service_code,
                "date": v.service_date.isoformat(), "clock_in": v.clock_in.strftime("%Y-%m-%d %H:%M"),
                "clock_out": v.clock_out.strftime("%Y-%m-%d %H:%M"), "minutes": v.duration_minutes,
                "crosses_midnight": v.crosses_midnight, "status": v.verification_status, "state": state,
                "claims": [self._claim_label(c) for c in cs], "action": action}

    @staticmethod
    def _draft_action(d) -> str:
        if d.outcome == "ready":
            due = f"file by {d.file_by}" if d.file_by else "no filing limit"
            return f"Draft ready: {d.units} units, ${d.amount:.2f}, {due}."
        if d.outcome == "late":
            return f"Past the filing limit (was {d.file_by}): can no longer be billed."
        if d.outcome == "auth_short":
            return "Draft would be D02: " + d.decision.failed[0].detail
        if d.outcome == "no_auth":
            return "No authorization covers this visit."
        return "Draft would be denied: " + (d.decision.failed[0].detail if d.decision else "")

    def _index_auths(self) -> None:
        entries = defaultdict(list)
        for e in self.run.ledger.entries:
            if e.auth_id:
                entries[e.auth_id].append(e)
        self.auth_entries = entries
        errors = {e["auth_id"] for e in self.run.ledger_errors}
        rows = []
        for a in sorted(self.auths.values(), key=lambda a: a.auth_id):
            es = entries.get(a.auth_id, [])
            paid = sum(e.units_drawn for e in es if e.outcome_source == "actual")
            pred = sum(e.units_drawn for e in es if e.outcome_source == "predicted")
            left = a.units_authorized - paid - pred
            ended = a.end_date < automator.AS_OF
            cat = ("ended" if ended else "error" if a.auth_id in errors else "used" if left <= 0
                   else "low" if left < 0.1 * a.units_authorized else "ok")
            rows.append({"id": a.auth_id, "member": a.patient_id, "payer_id": a.payer_id, "payer": _payer_short(a.payer_id),
                         "service": a.service_code, "start": a.start_date.isoformat(), "end": a.end_date.isoformat(),
                         "authorized": a.units_authorized, "paid": paid, "predicted": pred, "left": left,
                         "denied": sum(1 for e in es if e.outcome == "denied"), "claims": len(es), "category": cat})
        self.auth_rows = rows

    def _load_messages(self) -> dict:
        path = messages.MESSAGES_DIR / "messages.jsonl"
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as fh:
            return {m["claim_id"]: m for m in map(json.loads, fh) if m.get("status") != "no_message_needed"}

    # ----------------------------------------------------------------- reads
    def overview(self) -> dict:
        r, f = self.run, self.fixes
        flagged = sum(1 for d in r.check_dec.values() if d.flags or r.plans[d.claim_id].outcome == "needs_decision")
        drafts = automator.summary(self.drafts)
        return {
            "rule_set_version": r.ruleset.version,
            "payers": [{"id": p, "name": r.ruleset.payers[p].payer_name} for p in sorted(r.ruleset.payers)],
            "agencies": len({c.agency_id for c in r.history + r.to_check}),
            "to_check": {"claims": len(r.to_check), "billed": f["dollars"]["billed_as_submitted"],
                         "paid_as_is": f["dollars"]["predicted_paid_as_submitted"],
                         "paid_claims": sum(1 for d in r.check_dec.values() if d.status == "paid"),
                         "denied_claims": sum(1 for d in r.check_dec.values() if d.status == "denied"),
                         "safe_fixes": f["dollars"]["extra_from_safe_fixes"],
                         "decision_fixes": f["dollars"]["extra_from_fixes_needing_a_decision"],
                         "fix_outcomes": f["fix_outcomes"], "needs_review": flagged},
            "history": {"claims": len(r.history), "paid": sum(1 for c in r.history if c.status == "paid"),
                        "denied": sum(1 for c in r.history if c.status == "denied")},
            "backtest": {k: self.backtest[k] for k in ("status_accuracy", "exact_code_accuracy",
                                                       "exact_code_accuracy_excluding_opaque_d99", "misses_by_category")},
            "errors": len(r.ledger_errors),
            "as_of": automator.AS_OF.isoformat(),
            "automator": drafts,
        }

    def claims_list(self, tab: str, chip: str = "all", payer: str = "", q: str = "", page: int = 1) -> dict:
        r = self.run
        rows = []
        if tab == "awaiting":
            for c in r.to_check:
                d, p = r.check_dec[c.claim_id], r.plans[c.claim_id]
                flags = [f.code for f in d.flags]
                cat = {"denied": d.status == "denied", "decision": p.outcome == "needs_decision",
                       "flagged": bool(flags) or p.outcome == "needs_decision", "paid": d.status == "paid", "all": True}
                if not cat.get(chip, True) or (payer and c.payer_id != payer):
                    continue
                if not _match(q, c.claim_id, c.patient_id, c.auth_id, d.denial_code):
                    continue
                rows.append({"id": c.claim_id, "member": c.patient_id, "payer": _payer_short(c.payer_id),
                             "service": c.service_code, "dos": c.date_of_service.isoformat(),
                             "submitted": c.submitted_date.isoformat(), "units": c.units, "billed": c.billed_amount,
                             "status": d.status, "code": d.denial_code,
                             "reason": d.failed[0].detail if d.failed else "Passes every rule.",
                             "fix_outcome": p.outcome, "fix": "" if p.outcome == "payable_as_is" else p.summary(),
                             "flags": [{"code": x, "label": FLAG_LABELS.get(x, x)} for x in flags]})
            rows.sort(key=lambda x: (x["status"] == "paid", x["fix_outcome"] != "needs_decision", x["id"]))
        else:
            want = "denied" if tab == "denied" else "paid"
            for c in r.history:
                if c.status != want or (payer and c.payer_id != payer):
                    continue
                if not _match(q, c.claim_id, c.patient_id, c.auth_id, c.denial_code, c.remark):
                    continue
                row = {"id": c.claim_id, "member": c.patient_id, "payer": _payer_short(c.payer_id),
                       "service": c.service_code, "dos": c.date_of_service.isoformat(),
                       "submitted": c.submitted_date.isoformat(), "units": c.units, "billed": c.billed_amount}
                if want == "denied":
                    d, p = r.hist_dec[c.claim_id], self.history_plans.get(c.claim_id)
                    row.update(code=c.denial_code, remark=c.remark,
                               engine=d.failed[0].detail if d.failed else "No rule fails: the payer gave no usable reason.",
                               predicted=d.denial_code or "paid",
                               fix_outcome=p.outcome if p else "", fix=p.summary() if p else "")
                rows.append(row)
            rows.sort(key=lambda x: x["submitted"], reverse=True)
        counts = {"awaiting": len(r.to_check),
                  "denied": sum(1 for c in r.history if c.status == "denied"),
                  "paid": sum(1 for c in r.history if c.status == "paid")}
        return {"counts": counts, **_page(rows, page)}

    def claim(self, claim_id: str) -> dict:
        r = self.run
        c = self.claims.get(claim_id)
        if c is None:
            raise ActionError(f"No claim {claim_id}.")
        history = c.dataset == "history"
        d = r.hist_dec[claim_id] if history else r.check_dec[claim_id]
        plan = self.history_plans.get(claim_id) if history else r.plans[claim_id]
        dd = decision_to_dict(d, plan)
        led = d.evidence.ledger
        entry = next((e for e in self.auth_entries.get(c.auth_id, []) if e.claim_id == claim_id), None)
        route = messages.route(dd) if not history else None
        visits = [{"id": v.visit_id, "status": v.verification_status, "clock_in": v.clock_in.strftime("%Y-%m-%d %H:%M"),
                   "clock_out": v.clock_out.strftime("%Y-%m-%d %H:%M"), "minutes": v.duration_minutes}
                  for v in self.visits_by_key.get((c.patient_id, c.service_code, c.date_of_service), [])]
        return {
            "claim": _jsonable(asdict(c)),
            "payer_name": config.PAYER_NAMES.get(c.payer_id, c.payer_id),
            "history": history,
            "actual": {"status": c.status, "code": c.denial_code, "remark": c.remark} if history else None,
            "decision": dd,
            "visits": visits,
            "ledger": {"auth_id": led.auth_id, "found": led.auth_found, "units_authorized": led.units_authorized,
                       "remaining_before": led.units_remaining,
                       "draws": [{"claim_id": x.claim_id, "units": x.units, "submitted": x.submitted_date.isoformat(),
                                  "source": "predicted" if x.claim_id in r.check_dec else "actual"} for x in led.auth_draws],
                       "day_paid_units": led.day_paid_units, "prior_submissions": list(led.prior_submissions),
                       "sequence": led.sequence, "stream_length": len(r.ledger.entries)},
            "auth": next((a for a in self.auth_rows if a["id"] == c.auth_id), None),
            "message": self.messages.get(claim_id),
            "message_route": {"recipient": route[0], "purpose": route[1]} if route else None,
            "in_stream_position": entry.sequence + 1 if entry else None,
        }

    def visits_list(self, tab: str = "all", q: str = "", page: int = 1) -> dict:
        rows = [v for v in self.visit_rows if (tab == "all" or v["state"] == tab)
                and _match(q, v["id"], v["member"], v["caregiver"], *[c["id"] for c in v["claims"]])]
        counts = Counter(v["state"] for v in self.visit_rows)
        status = Counter(v["status"] for v in self.visit_rows)
        return {"counts": {"all": len(self.visit_rows), **counts}, "status": dict(status), **_page(rows, page)}

    def _draft_row(self, d) -> dict:
        return {"key": d.key, "visits": list(d.visit_ids), "member": d.patient_id, "service": d.service_code,
                "dos": d.date_of_service.isoformat(), "payer_id": d.payer_id, "payer": _payer_short(d.payer_id or ""),
                "payer_name": config.PAYER_NAMES.get(d.payer_id or "", ""), "auth_id": d.auth_id,
                "minutes": d.minutes, "units": d.units, "units_note": d.units_note, "modifier": d.modifier or "None needed",
                "amount": d.amount, "file_by": d.file_by.isoformat() if d.file_by else None, "days_left": d.days_left,
                "outcome": d.outcome, "code": d.decision.denial_code if d.decision else "",
                "reason": d.decision.failed[0].detail if d.decision and d.decision.failed else "",
                "rules_checked": len(d.decision.results) if d.decision else 0}

    def automator_list(self, outcome: str = "ready", page: int = 1) -> dict:
        rows = [self._draft_row(d) for d in self.drafts if outcome == "all" or d.outcome == outcome]
        return {"as_of": automator.AS_OF.isoformat(), "summary": automator.summary(self.drafts), **_page(rows, page)}

    def authorizations_list(self, f: str = "current", q: str = "", page: int = 1) -> dict:
        def keep(a):
            if f == "current":
                return a["category"] != "ended"
            return a["category"] == f
        rows = [a for a in self.auth_rows if keep(a) and _match(q, a["id"], a["member"], a["payer"])]
        order = {"error": 0, "used": 1, "low": 2, "ok": 3, "ended": 4}
        rows.sort(key=lambda a: (order[a["category"]], a["left"] / max(a["authorized"], 1), a["id"]))
        counts = Counter(a["category"] for a in self.auth_rows)
        counts["current"] = sum(v for k, v in counts.items() if k != "ended")
        return {"counts": dict(counts), "as_of": automator.AS_OF.isoformat(), **_page(rows, page)}

    def authorization(self, auth_id: str) -> dict:
        row = next((a for a in self.auth_rows if a["id"] == auth_id), None)
        if row is None:
            raise ActionError(f"No authorization {auth_id}.")
        trace = [{"claim_id": e.claim_id, "dataset": e.dataset, "submitted": e.submitted_date.isoformat(),
                  "outcome": e.outcome, "source": e.outcome_source, "units_drawn": e.units_drawn,
                  "remaining_after": e.auth_remaining_after,
                  "code": self.claims[e.claim_id].denial_code if e.outcome_source == "actual"
                  else self.run.check_dec[e.claim_id].denial_code}
                 for e in self.auth_entries.get(auth_id, [])]
        return {**row, "trace": trace}

    def errors(self) -> list:
        return self.run.ledger_errors

    def payers(self) -> dict:
        out = []
        for pid, p in sorted(self.run.ruleset.payers.items()):
            raw = json.loads((RULES_DIR / f"{pid}.json").read_text(encoding="utf-8"))
            learned = sum(1 for x in raw["rules"] if x["source"]["type"] != "document")
            out.append({"id": pid, "name": p.payer_name, "rules": len(p.rules), "learned": learned,
                        "order": list(p.precedence), "onboarded": pid in config.load_registry(),
                        "approval": raw.get("approval")})
        pending = [{"id": pid, "name": e["name"]} for pid, e in sorted(config.load_registry().items())
                   if pid not in self.run.ruleset.payers]
        return {"payers": out, "pending": pending}

    def rules(self, payer_id: str) -> dict:
        path = RULES_DIR / f"{payer_id}.json"
        if not path.exists():
            raise ActionError(f"No approved rules for {payer_id}.")
        raw = json.loads(path.read_text(encoding="utf-8"))
        meta_path = extract.PROPOSED_DIR / f"{payer_id}.meta.json"
        questions = []
        if meta_path.exists():
            questions = json.loads(meta_path.read_text(encoding="utf-8"))["draft"].get("open_questions", [])
        return {**raw, "open_questions": questions}

    def review(self) -> dict:
        def load(p):
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        questions = []
        for meta in sorted(extract.PROPOSED_DIR.glob("PY*.meta.json")):
            m = json.loads(meta.read_text(encoding="utf-8"))
            for q in m["draft"].get("open_questions", []):
                questions.append({"payer_id": m["payer_id"], "where": "Rule documents", **q})
        bev = load(bulletins.BULLETIN_DIR / "evaluation.json") or {}
        for pid, b in sorted(bev.items()):
            for q in b.get("open_questions", []):
                questions.append({"payer_id": pid, "where": b["bulletin"], **q})
        corrections = extract.load_corrections()
        return {"hypotheses": load(hypotheses.HYPOTHESES_DIR / "proposals.json"),
                "extraction": load(extract.PROPOSED_DIR / "evaluation.json"),
                "bulletins": bev, "open_questions": questions, "corrections": len(corrections),
                "payers": self.payers()["payers"]}

    # -------------------------------------------------------------- onboarding
    def onboarding(self, payer_id: str) -> dict:
        registry = config.load_registry()
        if payer_id not in registry:
            return {"payer_id": payer_id, "registered": False}
        out = {"payer_id": payer_id, "registered": True, **registry[payer_id],
               "approved": (RULES_DIR / f"{payer_id}.json").exists()}
        prop, meta = extract.PROPOSED_DIR / f"{payer_id}.json", extract.PROPOSED_DIR / f"{payer_id}.meta.json"
        if prop.exists() and meta.exists():
            out["proposal"] = json.loads(prop.read_text(encoding="utf-8"))
            m = json.loads(meta.read_text(encoding="utf-8"))
            out["meta"] = {k: m.get(k) for k in ("model", "request_sha256", "worked_example", "valid",
                                                 "validation_error", "unverified_quotes", "usage")}
            out["open_questions"] = m["draft"].get("open_questions", [])
            key = self._answer_key(payer_id)
            if key:
                out["answer_key"] = {"file": str(key.relative_to(config.REPO_ROOT)).replace("\\", "/"),
                                     **onboard.check(prop, key)}
        return out

    @staticmethod
    def _answer_key(payer_id: str, suffix: str = "") -> Path | None:
        found = sorted(DEMO_DIR.glob(f"*/expected_{payer_id}{suffix}.json"))
        return found[0] if found else None

    @staticmethod
    def _save_upload(folder: Path, file_name: str, text: str) -> Path:
        if not SAFE_NAME.match(file_name or ""):
            raise ActionError("File name must be letters, digits, dots, dashes or underscores, ending .md or .txt.")
        if not text.strip() or len(text) > 200_000:
            raise ActionError("The document is empty or larger than 200 KB.")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / file_name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def register_payer(self, payer_id: str, name: str, aliases: list[str], file_name: str, text: str) -> dict:
        import tempfile
        with self.lock, tempfile.TemporaryDirectory() as tmp:
            # Staged in a temporary folder: onboard.add copies it into place only if every check passes.
            path = self._save_upload(Path(tmp), file_name, text)
            try:
                onboard.add(payer_id, name, aliases, [path])
            except onboard.OnboardingError as exc:
                raise ActionError(str(exc)) from exc
            return self.onboarding(payer_id)

    def draft_payer_rules(self, payer_id: str) -> dict:
        with self.lock:
            try:
                extract.extract(payer_id)
            except LLMError as exc:
                raise ActionError(str(exc)) from exc
            except Exception as exc:
                if missing_credentials(exc):
                    raise ActionError("No Anthropic API key: set ANTHROPIC_API_KEY before starting the app.") from exc
                raise
            return self.onboarding(payer_id)

    def approve_payer(self, payer_id: str, approver: str, note: str) -> dict:
        if not approver.strip():
            raise ActionError("Enter your name to approve.")
        with self.lock:
            try:
                extract.approve(payer_id, approver.strip(), note.strip())
            except FileNotFoundError as exc:
                raise ActionError("Draft the rules before approving.") from exc
            self.reload()
            return self.onboarding(payer_id)

    # ----------------------------------------------------------------- notices
    def notices(self) -> list:
        out = []
        bev = {}
        ev_path = bulletins.BULLETIN_DIR / "evaluation.json"
        if ev_path.exists():
            bev = json.loads(ev_path.read_text(encoding="utf-8"))
        for pid, name in sorted(bulletins.KNOWN_BULLETINS.items()):
            path = config.DATA_DIR / "payers" / name
            out.append(self._notice(pid, path, applied=True, evaluation=bev.get(pid)))
        if NOTICE_DIR.exists():
            for path in sorted(NOTICE_DIR.glob("*.md")) + sorted(NOTICE_DIR.glob("*.txt")):
                pid = path.name.split("__", 1)[0]
                out.append(self._notice(pid, path, applied=self._notice_applied(pid, path)))
        return out

    def _notice_applied(self, pid: str, path: Path) -> bool:
        rules = RULES_DIR / f"{pid}.json"
        if not rules.exists():
            return False
        raw = json.loads(rules.read_text(encoding="utf-8"))
        return any(r["source"]["ref"].startswith(path.name) for r in raw["rules"])

    def _notice(self, pid: str, path: Path, applied: bool, evaluation: dict | None = None) -> dict:
        n = {"id": path.stem, "payer_id": pid, "payer_name": config.PAYER_NAMES.get(pid, pid), "file": path.name,
             "text": path.read_text(encoding="utf-8"), "applied": applied, "data_pack": evaluation is not None,
             "payer_approved": (RULES_DIR / f"{pid}.json").exists()}
        if evaluation:
            n["evaluation"] = evaluation
        meta = bulletins.BULLETIN_DIR / f"{path.stem}.meta.json"
        if meta.exists() and not n["data_pack"]:
            m = json.loads(meta.read_text(encoding="utf-8"))
            n["draft"] = {"end_rules": m["draft"]["end_rules"], "add_rules": m["draft"]["add_rules"],
                          "open_questions": m["draft"]["open_questions"], "valid": m["valid"],
                          "validation_error": m["validation_error"], "unverified_quotes": m["unverified_quotes"],
                          "model": m.get("model")}
            key = self._answer_key(pid, "_after_notice")
            if key:
                n["answer_key"] = {"file": str(key.relative_to(config.REPO_ROOT)).replace("\\", "/"),
                                   **onboard.check(bulletins.BULLETIN_DIR / f"{path.stem}.json", key)}
        return n

    def upload_notice(self, payer_id: str, file_name: str, text: str) -> dict:
        if not (RULES_DIR / f"{payer_id}.json").exists():
            raise ActionError(f"{payer_id} has no approved rules yet; approve them before applying a notice.")
        with self.lock:
            path = self._save_upload(NOTICE_DIR, f"{payer_id}__{file_name}", text)
            return self._notice(payer_id, path, applied=False)

    def draft_notice(self, notice_id: str) -> dict:
        with self.lock:
            path = next((p for p in NOTICE_DIR.glob(f"{notice_id}.*")), None) if NOTICE_DIR.exists() else None
            if path is None:
                raise ActionError(f"No uploaded notice {notice_id}.")
            pid = path.name.split("__", 1)[0]
            current = json.loads((RULES_DIR / f"{pid}.json").read_text(encoding="utf-8"))
            try:
                bulletins.draft_change(pid, path, current)
            except LLMError as exc:
                raise ActionError(str(exc)) from exc
            except Exception as exc:
                if missing_credentials(exc):
                    raise ActionError("No Anthropic API key: set ANTHROPIC_API_KEY before starting the app.") from exc
                raise
            return self._notice(pid, path, applied=False)

    def approve_notice(self, notice_id: str, approver: str, note: str) -> dict:
        if not approver.strip():
            raise ActionError("Enter your name to approve.")
        with self.lock:
            proposal = bulletins.BULLETIN_DIR / f"{notice_id}.json"
            if not proposal.exists():
                raise ActionError("Draft the change before approving.")
            pid = notice_id.split("__", 1)[0]
            extract.approve(pid, approver.strip(), note.strip(), proposal_path=proposal)
            self.reload()
            path = next(NOTICE_DIR.glob(f"{notice_id}.*"))
            return self._notice(pid, path, applied=self._notice_applied(pid, path))

    # --------------------------------------------------------- other actions
    def approve_hypothesis(self, hypothesis_id: str, approver: str) -> dict:
        if not approver.strip():
            raise ActionError("Enter your name to approve.")
        with self.lock:
            try:
                rule = hypotheses.approve(hypothesis_id, approver.strip())
            except LLMError as exc:
                raise ActionError(str(exc)) from exc
            self.reload()
            return {"approved": rule["rule_id"]}

    def draft_message(self, claim_id: str) -> dict:
        with self.lock:
            c = self.claims.get(claim_id)
            if c is None or c.dataset != "to_check":
                raise ActionError("Messages are drafted for claims waiting to be submitted.")
            decision = decision_to_dict(self.run.check_dec[claim_id], self.run.plans[claim_id])
            if messages.route(decision) is None:
                raise ActionError("This claim needs no message.")
            claim_row = {k: str(v) for k, v in asdict(c).items()}
            claim_row["billed_amount"] = f"{c.billed_amount:.2f}"
            try:
                out = messages.draft([claim_id], {claim_id: claim_row}, {claim_id: decision})[0]
            except LLMError as exc:
                raise ActionError(str(exc)) from exc
            except Exception as exc:
                if missing_credentials(exc):
                    raise ActionError("No Anthropic API key: set ANTHROPIC_API_KEY before starting the app.") from exc
                raise
            self.messages[claim_id] = out
            return out

    def demo_document(self, name: str) -> dict:
        allowed = {p.name: p for p in DEMO_DIR.glob("*/*.md")}
        if name not in allowed:
            raise ActionError("Unknown demo document.")
        return {"file_name": name, "text": allowed[name].read_text(encoding="utf-8")}
