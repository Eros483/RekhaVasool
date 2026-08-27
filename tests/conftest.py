import pytest

from utils.config import settings


@pytest.fixture(autouse=True)
def _force_gemini_off_for_harness(monkeypatch):
    # Ensure 20/20 harness passes with regex alone per spec §7 — don't hit live Gemini quota during pytest
    # Tests that need Gemini explicitly enable it via monkeypatch
    monkeypatch.setattr(settings, "gemini_enabled", False)
