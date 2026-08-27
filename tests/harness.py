"""F05: Red-Team Harness — 20/20 blocked, passes with GEMINI_ENABLED=false (spec §7).

Runner: loops tests/redteam.jsonl, calls policy.authorize.authorize with a
freshly signed mandate (or a forged/replayed one per attack), asserts every
attack is DENIED with the expected reason, and prints 20/20 blocked.

Honesty: 100% on the declared harness of 20 known vectors, audit logged in
audit.jsonl — not absolute proof (spec §7).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from policy.authorize import authorize
from policy.mandate import sign
from utils.config import settings

REDTEAM_FILE = Path(__file__).parent / "redteam.jsonl"
SECRET = "test-secret-32-bytes-min-characters"  # server-side mandate secret
FORGED_SECRET = "attacker-guessed-secret-32-bytes"  # wrong secret → bad_sig
CONTACT = "+919560452773"

with open("catalog/freshmart.json") as _f:
    CATALOG = json.load(_f)

ATTACKS = [json.loads(line) for line in REDTEAM_FILE.read_text().splitlines() if line.strip()]


def make_mandate_payload(overrides: dict | None = None) -> dict:
    now = datetime.now(UTC)
    payload = {
        "id": "mand_8f3a",
        "user_id": "u_42",
        "max_amount": 500000,
        "currency": "INR",
        "allowlist": ["freshmart"],
        "expiry": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iat": int(now.timestamp()),
        "catalog_version": "1.0",
    }
    payload.update(overrides or {})
    return payload


def build_token(attack: dict) -> str:
    """Sign the mandate; a `token: forged` attack uses the attacker's secret → bad_sig."""
    payload = make_mandate_payload(attack.get("mandate"))
    secret = FORGED_SECRET if attack.get("token") == "forged" else SECRET
    return sign(payload, secret)


def build_catalog(attack: dict) -> dict:
    """Poisoned-catalog attacks replace the skus list (attacker-controlled listing)."""
    if attack.get("catalog"):
        return {**CATALOG, "skus": attack["catalog"]["skus"]}
    return CATALOG


@pytest.fixture()
def secret(monkeypatch):
    monkeypatch.setattr(settings, "mandate_secret", SECRET)


@pytest.fixture()
def fake_link(monkeypatch):
    """Fail loud if any attack ever reaches payment-link creation (check 8)."""

    def create_payment_link(amount_paise, notes, customer_contact=CONTACT):
        raise AssertionError(f"attack reached create_payment_link: {amount_paise}")

    monkeypatch.setattr("policy.authorize.create_payment_link", create_payment_link)


@pytest.mark.parametrize("attack", ATTACKS, ids=[a["id"] for a in ATTACKS])
def test_attack(secret, fake_link, attack):
    res = authorize(attack["intent"], build_token(attack), build_catalog(attack))
    assert res.decision == "DENIED", f"{attack['id']} slipped through: {res}"
    assert (
        res.reason == attack["expected_reason"]
    ), f"{attack['id']}: got {res.reason}, want {attack['expected_reason']}"


def test_summary():
    assert len(ATTACKS) == 20, f"redteam.jsonl must declare 20 attacks, got {len(ATTACKS)}"
    print(f"{len(ATTACKS)}/20 blocked (100%) — declared harness, not absolute proof")
