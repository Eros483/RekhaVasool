"""F03: Buyer Agent (Gemini + MCP) — emits {sku,qty,merchant,raw_text} only, no amount.

Rule-based fallback must work with GEMINI_ENABLED=false (like the harness).
When Gemini is enabled, its output is parsed but never trusted for amount or
non-canonical SKUs — canonical price comes from catalog only (§4.3 / SC4).
"""

import json

import pytest

from agent import buyer
from utils.config import settings

with open("catalog/freshmart.json") as _f:
    CATALOG = json.load(_f)


def make_intent(**overrides):
    intent = {
        "sku": "sony_ch510",
        "qty": 1,
        "merchant": "freshmart",
        "raw_text": "user said headphones under 5k NC",
    }
    intent.update(overrides)
    return intent


@pytest.fixture()
def disable_gemini(monkeypatch):
    monkeypatch.setattr(settings, "gemini_enabled", False)


@pytest.fixture()
def enable_gemini(monkeypatch):
    monkeypatch.setattr(settings, "gemini_enabled", True)


# --- rule-based fallback (GEMINI_ENABLED=false) ---


def test_rule_based_matches_by_keyword(disable_gemini):
    intent = buyer.buy("noise cancelling headphones under 5k", CATALOG)
    assert intent["sku"] == "sony_ch510"  # NC true, under budget
    assert intent["merchant"] == "freshmart"
    assert intent["qty"] == 1
    assert "raw_text" in intent


def test_rule_based_boat_match(disable_gemini):
    intent = buyer.buy("boAt rockerz 450 please", CATALOG)
    assert intent["sku"] == "boat_450"


def test_rule_based_jbl_picked_even_if_over_budget(disable_gemini):
    # buyer picks the SKU; authorize() (F02) is what blocks amount>max
    intent = buyer.buy("JBL tune 710", CATALOG)
    assert intent["sku"] == "jbl_tune"


def test_rule_based_unknown_returns_none(disable_gemini):
    assert buyer.buy("give me an iphone", CATALOG) is None


# --- Gemini enabled: output parsed, but no amount / non-canonical SKU trusted ---


def test_gemini_emits_intent_no_amount(enable_gemini, monkeypatch):
    fake = {"sku": "boat_450", "qty": 2, "merchant": "freshmart", "amount": 1}
    monkeypatch.setattr(buyer, "_gemini_intent", lambda text, catalog: fake)
    intent = buyer.buy("boAt", CATALOG)
    assert intent["sku"] == "boat_450"
    assert intent["qty"] == 2
    assert "amount" not in intent  # LLM amount never passes through (§4.3)


def test_gemini_unknown_sku_dropped_to_none(enable_gemini, monkeypatch):
    fake = {"sku": "not_a_real_sku", "qty": 1, "merchant": "freshmart"}
    monkeypatch.setattr(buyer, "_gemini_intent", lambda text, catalog: fake)
    assert buyer.buy("whatever", CATALOG) is None


def test_gemini_failure_falls_back_to_rules(enable_gemini, monkeypatch):
    def boom(text, catalog):
        raise RuntimeError("gemini down")

    monkeypatch.setattr(buyer, "_gemini_intent", boom)
    intent = buyer.buy("noise cancelling under 5k", CATALOG)
    assert intent["sku"] == "sony_ch510"


def test_rule_based_intent_has_no_amount(disable_gemini):
    intent = buyer.buy("sony headphones", CATALOG)
    assert "amount" not in intent
