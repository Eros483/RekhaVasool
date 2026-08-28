import pytest

from utils.config import settings


@pytest.fixture(autouse=True)
def _test_isolation(monkeypatch):
    # 20/20 harness passes with regex alone per spec §7 — never hit live Gemini quota during pytest
    monkeypatch.setattr(settings, "gemini_enabled", False)
    # never place real Twilio voice calls during tests
    monkeypatch.setattr(settings, "voice_wss_url", "")
    import recovery.orchestrator as orch

    monkeypatch.setattr(orch, "dial_voice", lambda store, original_payment_id: False)
