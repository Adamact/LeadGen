from types import SimpleNamespace

import ai


def _response(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class _FakeClient:
    def __init__(self, response):
        self.messages = SimpleNamespace(create=lambda **kw: response)


def test_ask_json_reads_text_after_thinking_block(monkeypatch):
    resp = _response(
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text='```json\n{"contacts": []}\n```'),
    )
    monkeypatch.setattr(ai, "_get_client", lambda: _FakeClient(resp))
    assert ai._ask_json("p", "claude-haiku-5-5", 100, "test") == {"contacts": []}


def test_ask_json_returns_none_on_refusal(monkeypatch):
    resp = _response(stop_reason="refusal")
    monkeypatch.setattr(ai, "_get_client", lambda: _FakeClient(resp))
    assert ai._ask_json("p", "claude-haiku-5-5", 100, "test") is None


def test_parse_contacts_tolerates_nulls():
    contacts = ai._parse_contacts([
        {"name": "Anna Svensson", "title": None, "email": None, "phone": 701234567},
        {"name": None},
        "not a dict",
    ], source="search")
    assert len(contacts) == 1
    assert contacts[0].title == ""
    assert contacts[0].phone == "701234567"


def test_infer_personal_email_handles_swedish_chars():
    candidates = ai.infer_personal_email("Åsa Öberg-Lind", "acme.se")
    assert candidates[0] == "asa.oberglind@acme.se"
    assert ai.infer_personal_email("Madonna", "acme.se") == []


def test_usage_tracker_prices_by_tier():
    tracker = ai.APIUsageTracker()
    tracker.record(_response(), "x", model="claude-haiku-5-5")
    tracker.record(_response(), "y", model="claude-sonnet-5-5")
    assert tracker.calls == 2
    assert set(tracker.usage_by_tier) == {"haiku", "sonnet"}
    assert tracker.estimated_cost > 0
