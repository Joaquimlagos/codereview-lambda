"""Unit test: the Lambda entry point hands the context's remaining-time function to the LLM
router, which is what lets the router stop before Lambda's own timeout."""

import invoke_llm.handler as handler_module
from integrations.llm_router import StubLlmRouter


class FakeLambdaContext:
    def get_remaining_time_in_millis(self) -> int:
        return 123_456


def test_handler_passes_remaining_time_to_the_router(monkeypatch, pr_event, stub_storage):
    captured = {}

    def fake_default_router(remaining_time_ms=None):
        captured["remaining_time_ms"] = remaining_time_ms
        return StubLlmRouter()

    monkeypatch.setattr(handler_module, "_default_llm_router", fake_default_router)
    monkeypatch.setattr(handler_module, "S3Storage", lambda bucket: stub_storage)
    event = {**pr_event, "routing": {"complexity": "low", "needsContext": False}}

    handler_module.handler(event, FakeLambdaContext())

    assert captured["remaining_time_ms"]() == 123_456
