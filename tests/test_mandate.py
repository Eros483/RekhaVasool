"""F01: Catalog + Mandate (HMAC) — sign/verify, tampered sig, expiry."""

import json
from datetime import UTC, datetime, timedelta

from policy.mandate import sign, verify

SECRET = "test-secret-32-bytes-min-characters"


def make_payload(**overrides):
    payload = {
        "id": "mand_8f3a",
        "user_id": "u_42",
        "max_amount": 500000,
        "currency": "INR",
        "allowlist": ["freshmart"],
        "expiry": (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iat": int(datetime.now(UTC).timestamp()),
        "catalog_version": "1.0",
    }
    payload.update(overrides)
    return payload


def test_sign_verify_roundtrip():
    payload = make_payload()
    token = sign(payload, SECRET)
    ok, decoded = verify(token, SECRET)
    assert ok is True
    assert decoded["id"] == "mand_8f3a"
    assert decoded["max_amount"] == 500000
    assert decoded["allowlist"] == ["freshmart"]
    assert decoded["catalog_version"] == "1.0"


def test_catalog_exact_per_spec():
    with open("catalog/freshmart.json") as f:
        catalog = json.load(f)
    assert catalog["merchant"] == "freshmart"
    assert catalog["catalog_version"] == "1.0"
    assert len(catalog["skus"]) == 3
    skus = {s["sku"]: s for s in catalog["skus"]}
    assert skus["sony_ch510"]["price"] == 499900
    assert skus["sony_ch510"]["nc"] is True
    assert skus["boat_450"]["price"] == 199900
    assert skus["jbl_tune"]["price"] == 650000  # over ₹5k budget on purpose


def test_tampered_payload_denied():
    payload = make_payload()
    token = sign(payload, SECRET)
    # flip max_amount 500000 -> 999999 inside payload segment
    header_b64, _unused_payload, sig_b64 = token.split(".")
    tampered = json.dumps({**payload, "max_amount": 999999}, separators=(",", ":")).encode()
    from policy.mandate import _b64url

    forged = f"{header_b64}.{_b64url(tampered)}.{sig_b64}"
    ok, reason = verify(forged, SECRET)
    assert ok is False
    assert reason == "bad_sig"


def test_wrong_secret_denied():
    token = sign(make_payload(), SECRET)
    ok, reason = verify(token, "another-secret-completely-different-")
    assert ok is False
    assert reason == "bad_sig"


def test_expired_denied():
    past = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    token = sign(make_payload(expiry=past), SECRET)
    ok, reason = verify(token, SECRET)
    assert ok is False
    assert reason == "expired"


def test_malformed_token_denied():
    ok, reason = verify("not-a-jwt", SECRET)
    assert ok is False
    assert reason == "bad_sig"
