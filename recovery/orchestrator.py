"""F06: Recovery Loop + WA + Polling — idempotent, bounded, rule-based.

No FastAPI imports here. Razorpay/WA calls are module-level functions so tests
monkeypatch them. Spec §8 + §4.5.
"""

import hashlib
import sqlite3
import time

from utils.config import settings

RECOVERY_CONTACT = settings.wa_to or "+91-9560452773"  # whitelisted test number (spec §6) — autoconfigured from WA_TO env
MAX_ATTEMPTS = 2
MAX_WA_SENT = 1
RETRY_DELAY_SECONDS = 45
LINK_EXPIRE_MINUTES = 15
RECOVERY_DB = "recovery.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_attempts (
  id TEXT PRIMARY KEY,
  original_payment_id TEXT,
  payment_id TEXT UNIQUE,
  error_code TEXT,
  attempts INT DEFAULT 0,
  wa_sent BOOL DEFAULT 0,
  status TEXT,
  stop_requested BOOL DEFAULT 0,
  wa_to TEXT,
  idempotency_key TEXT UNIQUE,
  short_url TEXT,
  amount_paise INT DEFAULT 0,
  UNIQUE(original_payment_id, attempts)
);
"""


class RecoveryStore:
    def __init__(self, path=RECOVERY_DB):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def rows(self, original_payment_id):
        return self.conn.execute(
            "SELECT * FROM recovery_attempts WHERE original_payment_id=? ORDER BY attempts",
            (original_payment_id,),
        ).fetchall()

    def find_by_link(self, link_id):
        r = self.conn.execute("SELECT * FROM recovery_attempts WHERE id=?", (link_id,)).fetchone()
        return dict(r) if r else None

    def latest(self, original_payment_id):
        rows = self.rows(original_payment_id)
        return dict(rows[-1]) if rows else None

    def count_attempts(self, original_payment_id):
        return len(self.rows(original_payment_id))

    def wa_sent_total(self, original_payment_id):
        return sum(1 for r in self.rows(original_payment_id) if r["wa_sent"])

    def stop_requested(self, original_payment_id):
        return any(r["stop_requested"] for r in self.rows(original_payment_id))

    def has_payment(self, payment_id):
        return (
            self.conn.execute(
                "SELECT 1 FROM recovery_attempts WHERE payment_id=?", (payment_id,)
            ).fetchone()
            is not None
        )

    def insert(self, **kw):
        keys = ",".join(kw)
        ph = ",".join("?" * len(kw))
        self.conn.execute(
            f"INSERT INTO recovery_attempts ({keys}) VALUES ({ph})", tuple(kw.values())
        )
        self.conn.commit()


# --- driver seams (monkeypatched in tests) ---
# ponytail: single httpx call each, no retry/backoff — add if prod matters.


def _idem(original_payment_id, attempts):
    return hashlib.sha256(f"{original_payment_id}:{attempts}".encode()).hexdigest()


def create_payment_link(amount_paise, customer_contact, notes, expire_minutes=LINK_EXPIRE_MINUTES):
    """POST /v1/payment_links — spec §6. Returns {id, short_url}."""
    import httpx

    body = {
        "amount": amount_paise,
        "currency": "INR",
        "expire_by": int(time.time()) + expire_minutes * 60,
        "customer": {"name": "FreshMart Customer", "contact": customer_contact},
        "notify": {"sms": False, "email": False},
        "notes": notes,
    }
    r = httpx.post(
        "https://api.razorpay.com/v1/payment_links",
        json=body,
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def cancel_payment_link(link_id):
    """POST /v1/payment_links/{id}/cancel — spec §6 (STOP handling)."""
    import httpx

    r = httpx.post(
        f"https://api.razorpay.com/v1/payment_links/{link_id}/cancel",
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
        timeout=10,
    )
    r.raise_for_status()


def send_wa(to, text):
    """Meta WhatsApp Cloud API — raw text inside 24h window, whitelisted number."""
    import httpx

    r = httpx.post(
        f"https://graph.facebook.com/v20.0/{settings.wa_phone_id}/messages",
        json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}},
        headers={"Authorization": f"Bearer {settings.wa_token}"},
        timeout=10,
    )
    r.raise_for_status()


def fetch_payment_link(link_id):
    """GET /v1/payment_links/{id} — polling fallback when webhook missed."""
    import httpx

    r = httpx.get(
        f"https://api.razorpay.com/v1/payment_links/{link_id}",
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def classify(error_code):
    return "retry" if error_code in ("bank_decline", "timeout") else "wa"


# --- orchestration ---


def _make_link(store, original, payment_id, error_code, amount_paise, attempts):
    notes = {
        "original_payment_id": original,
        "attempts": attempts,
        "idempotency_key": _idem(original, attempts),
    }
    link = create_payment_link(amount_paise, RECOVERY_CONTACT, notes)
    store.insert(
        id=link["id"],
        original_payment_id=original,
        payment_id=payment_id,
        error_code=error_code,
        attempts=attempts,
        wa_sent=0,
        status="pending",
        stop_requested=0,
        wa_to=RECOVERY_CONTACT,
        idempotency_key=notes["idempotency_key"],
        short_url=link["short_url"],
        amount_paise=amount_paise,
    )
    return link


def handle_payment_failed(
    original_payment_id,
    payment_id,
    error_code,
    amount_paise,
    retry_delay=RETRY_DELAY_SECONDS,
    store=None,
):
    store = store or RecoveryStore()
    if store.has_payment(payment_id):
        return {"decision": "duplicate"}  # re-delivered webhook
    if store.stop_requested(original_payment_id):
        return {"decision": "stopped"}

    attempts = store.count_attempts(original_payment_id)
    if attempts >= MAX_ATTEMPTS:
        return {"decision": "cap_reached"}

    outcome = "retry_link"
    if classify(error_code) == "retry":
        try:
            _make_link(store, original_payment_id, payment_id, error_code, amount_paise, attempts)
        except Exception:  # noqa: BLE001 — any driver failure falls back to WA
            outcome = "wa_sent"  # retry failed -> WA nudge
    else:
        outcome = "wa_sent"  # skipped retry -> straight to WA
        _make_link(store, original_payment_id, payment_id, error_code, amount_paise, attempts)

    if outcome == "wa_sent" and not _maybe_send_wa(store, original_payment_id, amount_paise):
        outcome = "link_only"  # WA already capped
    return {"decision": outcome}


def _maybe_send_wa(store, original, amount_paise):
    if store.wa_sent_total(original) >= MAX_WA_SENT:
        return False
    link = store.latest(original)
    url = link["short_url"] if link else "your FreshMart order"
    text = (
        f"Tap to retry ₹{amount_paise / 100:,.0f} for FreshMart ({url}) " f"— reply STOP to opt out"
    )
    send_wa(RECOVERY_CONTACT, text)
    if link:
        store.conn.execute("UPDATE recovery_attempts SET wa_sent=1 WHERE id=?", (link["id"],))
        store.conn.commit()
    return True


def handle_webhook(payload, store=None):
    store = store or RecoveryStore()
    event = payload.get("event")
    pay = payload.get("payload", {}).get("payment", {}).get("entity", {})
    link_ent = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
    if event == "payment_link.paid":
        return on_payment_link_paid(pay.get("id") or link_ent.get("id"), store=store)
    if event == "payment.failed":
        # map back to the original that owns this link id
        row = store.find_by_link(link_ent.get("id")) if link_ent.get("id") else None
        original = (row or {}).get("original_payment_id") or pay.get("id")
        amount = pay.get("amount")
        if amount is None:
            return {"decision": "unknown"}
        return handle_payment_failed(
            original,
            pay.get("id"),
            pay.get("error_code", "gateway"),
            amount,
            retry_delay=0,
            store=store,
        )
    return {"decision": "unknown"}


def on_payment_link_paid(link_id, store=None):
    store = store or RecoveryStore()
    row = store.find_by_link(link_id)
    if not row:
        return {"decision": "unknown"}
    store.conn.execute("UPDATE recovery_attempts SET status='recovered' WHERE id=?", (link_id,))
    store.conn.commit()
    return {"decision": "recovered"}


def handle_stop(original_payment_id, store=None):
    store = store or RecoveryStore()
    link = store.latest(original_payment_id)
    if link:
        cancel_payment_link(link["id"])
        store.conn.execute(
            "UPDATE recovery_attempts SET stop_requested=1, status='stopped' WHERE id=?",
            (link["id"],),
        )
        store.conn.commit()
    return {"decision": "stopped"}


def poll_payment_link(link_id, store=None):
    store = store or RecoveryStore()
    link = fetch_payment_link(link_id)
    if link.get("status") == "paid":
        return on_payment_link_paid(link_id, store=store)
    return {"decision": "pending"}


def dashboard_stats(store=None):
    store = store or RecoveryStore()
    all_rows = store.conn.execute("SELECT * FROM recovery_attempts").fetchall()
    originals = {r["original_payment_id"] for r in all_rows}
    retried = sum(
        1
        for o in originals
        if any(
            classify(r["error_code"]) == "retry" for r in all_rows if r["original_payment_id"] == o
        )
    )
    wa_sent = sum(1 for o in originals if store.wa_sent_total(o) > 0)
    recovered_rows = [r for r in all_rows if r["status"] == "recovered"]
    return {
        "total_failed": len(originals),
        "retried": retried,
        "wa_sent": wa_sent,
        "recovered": len(recovered_rows),
        "amount_recovered_paise": sum(r["amount_paise"] for r in recovered_rows),
    }
