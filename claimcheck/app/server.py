"""Claim Check web server (standard library only).

Serves the single-page app from claimcheck/app/static/ and a JSON API:

  GET  /api/overview                      headline numbers
  GET  /api/claims?tab=&chip=&payer=&q=&page=
  GET  /api/claims/<id>                   full decision, evidence, fix, flags, message
  GET  /api/visits?tab=&q=&page=
  GET  /api/automator?outcome=&page=
  GET  /api/authorizations?filter=&q=&page=
  GET  /api/authorizations/<id>           ledger trace
  GET  /api/errors                        overdrawn authorizations
  GET  /api/payers  /api/payers/<id>/rules
  GET  /api/review  /api/notices
  GET  /api/onboarding/<id>  /api/demo/<file>
  POST /api/onboarding                    register a payer and its document
  POST /api/onboarding/<id>/extract       Claude drafts its rules (cached)
  POST /api/onboarding/<id>/approve
  POST /api/notices                       upload a notice
  POST /api/notices/<id>/draft            Claude drafts the change (cached)
  POST /api/notices/<id>/approve
  POST /api/hypotheses/<id>/approve
  POST /api/messages/<claim id>           Claude drafts the message (cached)

Binds to 127.0.0.1 only: it reads and writes local rule files.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .data import ActionError, AppState

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 400_000


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClaimCheck/1.0"

        def log_message(self, fmt, *args):  # quieter console
            if not self.path.startswith("/api/"):
                return
            sys.stderr.write("%s %s\n" % (self.command, self.path))

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ActionError("Request too large.")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise ActionError("Request body must be JSON.") from exc

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC / name).resolve()
            if STATIC not in target.parents or not target.is_file():
                target = STATIC / "index.html"
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def _route(self, method: str):
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                if method == "GET":
                    return self._static(url.path)
                return self._json(404, {"error": "Not found"})
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            page = int(q.get("page", "1") or 1)
            parts = [p for p in url.path.split("/")[2:] if p]
            try:
                if method == "GET":
                    result = self._get(parts, q, page)
                else:
                    result = self._post(parts, self._body())
                if result is None:
                    return self._json(404, {"error": "Not found"})
                return self._json(200, result)
            except ActionError as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:  # keep the app up during a demo; show the error
                traceback.print_exc()
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def _get(self, parts, q, page):
            s = state
            match parts:
                case ["overview"]:
                    return s.overview()
                case ["claims"]:
                    return s.claims_list(q.get("tab", "awaiting"), q.get("chip", "all"), q.get("payer", ""), q.get("q", ""), page)
                case ["claims", cid]:
                    return s.claim(cid)
                case ["visits"]:
                    return s.visits_list(q.get("tab", "all"), q.get("q", ""), page)
                case ["automator"]:
                    return s.automator_list(q.get("outcome", "ready"), page)
                case ["authorizations"]:
                    return s.authorizations_list(q.get("filter", "current"), q.get("q", ""), page)
                case ["authorizations", aid]:
                    return s.authorization(aid)
                case ["errors"]:
                    return s.errors()
                case ["payers"]:
                    return s.payers()
                case ["payers", pid, "rules"]:
                    return s.rules(pid)
                case ["review"]:
                    return s.review()
                case ["notices"]:
                    return s.notices()
                case ["onboarding", pid]:
                    return s.onboarding(pid)
                case ["demo", name]:
                    return s.demo_document(name)
            return None

        def _post(self, parts, b):
            s = state
            match parts:
                case ["onboarding"]:
                    return s.register_payer(b.get("payer_id", "").strip(), b.get("name", ""), b.get("aliases", []),
                                            b.get("file_name", ""), b.get("text", ""))
                case ["onboarding", pid, "extract"]:
                    return s.draft_payer_rules(pid)
                case ["onboarding", pid, "approve"]:
                    return s.approve_payer(pid, b.get("by", ""), b.get("note", ""))
                case ["notices"]:
                    return s.upload_notice(b.get("payer_id", ""), b.get("file_name", ""), b.get("text", ""))
                case ["notices", nid, "draft"]:
                    return s.draft_notice(nid)
                case ["notices", nid, "approve"]:
                    return s.approve_notice(nid, b.get("by", ""), b.get("note", ""))
                case ["hypotheses", hid, "approve"]:
                    return s.approve_hypothesis(hid, b.get("by", ""))
                case ["messages", cid]:
                    return s.draft_message(cid)
            return None

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m claimcheck.app")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    print("Loading claims, rules and the ledger...")
    state = AppState()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    print(f"Claim Check running at http://localhost:{args.port}  (rule set {state.run.ruleset.version})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
