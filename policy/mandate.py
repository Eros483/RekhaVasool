"""Mandate sign/verify — AP2-pattern JWT HS256, stdlib only.

// ponytail: HMAC for demo, Ed25519/JWS per AP2 spec if prod matters
"""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict, secret: str) -> str:
    """Sign a mandate payload → JWT string (header.payload.signature)."""
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}"
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


def verify(token: str, secret: str, now: datetime | None = None) -> tuple[bool, dict | str]:
    """Return (True, payload) or (False, reason) — reason is `bad_sig` or `expired`."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}"
        expected = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            return False, "bad_sig"
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, KeyError, TypeError):
        return False, "bad_sig"

    now = now or datetime.now(UTC)
    expiry = datetime.fromisoformat(payload["expiry"])
    if now >= expiry:
        return False, "expired"
    return True, payload
