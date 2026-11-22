"""A stand-in for the Anthropic client: answers with whatever `respond(kwargs)` returns."""
import json
from types import SimpleNamespace


class FakeClient:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn", model=kw["model"], _request_id="req_test",
                               content=[SimpleNamespace(type="text", text=json.dumps(self.respond(kw)))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
