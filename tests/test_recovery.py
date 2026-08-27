"""F06: Recovery Loop + WA + Polling — caps, idempotency, STOP, poll fallback."""

import pytest

import recovery.orchestrator as orch


@pytest.fixture()
def store(tmp_path):
    return orch.RecoveryStore(str(tmp_path / "recovery.db"))


@pytest.fixture()
def fake_rzp(monkeypatch):
    """Fake Razorpay + WA drivers; capture calls."""
    calls = {"links": [], "wa": [], "cancelled": [], "polls": []}
    _n = {"v": 0}

    def create_payment_link(amount_paise, customer_contact, notes, expire_minutes=15):
        _n["v"] += 1
        calls["links"].append(
            {
                "amount": amount_paise,
                "contact": customer_contact,
                "notes": notes,
                "expire_minutes": expire_minutes,
            }
        )
        return {"id": f"plink_{_n['v']}", "short_url": "https://rzp.io/test"}

    def cancel_payment_link(link_id):
        calls["cancelled"].append(link_id)

    def send_wa(to, text):
        calls["wa"].append({"to": to, "text": text})

    def fetch_payment_link(link_id):
        calls["polls"].append(link_id)
        return {"id": link_id, "status": "paid"}

    monkeypatch.setattr(orch, "create_payment_link", create_payment_link)
    monkeypatch.setattr(orch, "cancel_payment_link", cancel_payment_link)
    monkeypatch.setattr(orch, "send_wa", send_wa)
    monkeypatch.setattr(orch, "fetch_payment_link", fetch_payment_link)
    return calls


# --- rule-based classifier ---


def test_classify_error_codes():
    assert orch.classify("bank_decline") == "retry"
    assert orch.classify("timeout") == "retry"
    assert orch.classify("insufficient_funds") == "wa"
    assert orch.classify("random_gateway_error") == "wa"


# --- schema per spec 4.5 ---


def test_schema_columns(store):
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(recovery_attempts)")}
    assert {
        "id",
        "original_payment_id",
        "error_code",
        "attempts",
        "wa_sent",
        "status",
        "stop_requested",
        "wa_to",
        "idempotency_key",
    } <= cols


# --- retry path ---


def test_bank_decline_retries_with_fresh_15m_link(store, fake_rzp):
    res = orch.handle_payment_failed(
        "pay_1", "pay_1", "bank_decline", 499900, retry_delay=0, store=store
    )
    assert res["decision"] == "retry_link"
    link = fake_rzp["links"][0]
    assert link["amount"] == 499900
    assert link["expire_minutes"] == 15
    assert "idempotency_key" in link["notes"]
    assert fake_rzp["wa"] == []  # retry first, WA only if retry fails/skipped


# --- WA path ---


def test_insufficient_funds_goes_straight_to_wa(store, fake_rzp):
    res = orch.handle_payment_failed("pay_2", "pay_2", "insufficient_funds", 199900, store=store)
    assert res["decision"] == "wa_sent"
    msg = fake_rzp["wa"][0]
    assert msg["to"] == orch.RECOVERY_CONTACT
    assert "https://rzp.io/test" in msg["text"]
    assert "STOP" in msg["text"]
    assert store.wa_sent_total("pay_2") == 1


def test_retry_failure_falls_back_to_wa(store, fake_rzp, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("razorpay down")

    monkeypatch.setattr(orch, "create_payment_link", boom)
    res = orch.handle_payment_failed(
        "pay_5", "pay_5", "bank_decline", 499900, retry_delay=0, store=store
    )
    assert res["decision"] == "wa_sent"  # retry failed -> WA nudge
    assert len(fake_rzp["wa"]) == 1


# --- idempotency / dedup ---


def test_duplicate_webhook_is_idempotent(store, fake_rzp):
    orch.handle_payment_failed("pay_3", "pay_3", "timeout", 499900, retry_delay=0, store=store)
    res = orch.handle_payment_failed(
        "pay_3", "pay_3", "timeout", 499900, retry_delay=0, store=store
    )
    assert res["decision"] == "duplicate"
    assert len(fake_rzp["links"]) == 1
    assert store.count_attempts("pay_3") == 1


def test_idempotency_keys_differ_per_attempt(store, fake_rzp):
    orch.handle_payment_failed("pay_7", "pay_7", "bank_decline", 499900, retry_delay=0, store=store)
    # a second distinct failure for the same original advances to attempt 1
    orch.handle_payment_failed(
        "pay_7", "pay_7b", "bank_decline", 499900, retry_delay=0, store=store
    )
    keys = {r["idempotency_key"] for r in store.rows("pay_7")}
    assert len(keys) == 2


# --- hard caps: attempts <= 2, wa_sent <= 1 ---


def test_attempts_capped_at_two(store, fake_rzp):
    orch.handle_payment_failed("pay_4", "pay_4", "bank_decline", 499900, retry_delay=0, store=store)
    orch.handle_payment_failed(
        "pay_4", "pay_4b", "bank_decline", 499900, retry_delay=0, store=store
    )
    res = orch.handle_payment_failed(
        "pay_4", "pay_4c", "bank_decline", 499900, retry_delay=0, store=store
    )
    assert res["decision"] == "cap_reached"
    assert len(fake_rzp["links"]) == 2


def test_wa_sent_capped_at_one(store, fake_rzp):
    orch.handle_payment_failed("pay_6", "pay_6", "insufficient_funds", 499900, store=store)
    res = orch.handle_payment_failed("pay_6", "pay_6b", "insufficient_funds", 499900, store=store)
    assert res["decision"] == "link_only"  # no WA (cap) and no retry (non-retry code)
    assert len(fake_rzp["wa"]) == 1
    assert store.wa_sent_total("pay_6") == 1


# --- STOP ---


def test_stop_cancels_link_and_silences_nudges(store, fake_rzp):
    orch.handle_payment_failed("pay_8", "pay_8", "insufficient_funds", 499900, store=store)
    res = orch.handle_stop("pay_8", store=store)
    assert res["decision"] == "stopped"
    assert fake_rzp["cancelled"] == ["plink_1"]  # latest link cancelled

    res = orch.handle_payment_failed(
        "pay_8", "pay_8b", "bank_decline", 499900, retry_delay=0, store=store
    )
    assert res["decision"] == "stopped"
    assert len(fake_rzp["links"]) == 1  # no new link after STOP
    assert len(fake_rzp["wa"]) == 1  # no new WA after STOP


# --- recovered + poll fallback ---


def test_payment_link_paid_marks_recovered(store, fake_rzp):
    orch.handle_payment_failed("pay_9", "pay_9", "bank_decline", 499900, retry_delay=0, store=store)
    res = orch.on_payment_link_paid("plink_1", store=store)
    assert res["decision"] == "recovered"
    row = store.find_by_link("plink_1")
    assert row["status"] == "recovered"


def test_poll_fallback_recovers_when_webhook_missed(store, fake_rzp):
    orch.handle_payment_failed(
        "pay_10", "pay_10", "bank_decline", 499900, retry_delay=0, store=store
    )
    res = orch.poll_payment_link("plink_1", store=store)
    assert res["decision"] == "recovered"
    assert fake_rzp["polls"] == ["plink_1"]


# --- webhook dispatcher ---


def test_handle_webhook_paid(store, fake_rzp):
    orch.handle_payment_failed(
        "pay_11", "pay_11", "bank_decline", 499900, retry_delay=0, store=store
    )
    payload = {
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {"id": "plink_1", "status": "paid"}}},
    }
    res = orch.handle_webhook(payload, store=store)
    assert res["decision"] == "recovered"


def test_handle_webhook_failed_retries(store, fake_rzp):
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {"entity": {"id": "pay_12", "amount": 499900, "error_code": "timeout"}}
        },
    }
    res = orch.handle_webhook(payload, store=store)
    assert res["decision"] == "retry_link"


def test_webhook_failed_on_retry_link_groups_to_original(store, fake_rzp):
    # first failure -> retry link plink_1; that link's payment fails too ->
    # webhook must map back to the same original to keep the attempts chain.
    orch.handle_payment_failed(
        "pay_13", "pay_13", "bank_decline", 499900, retry_delay=0, store=store
    )
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {"entity": {"id": "pay_14", "amount": 499900, "error_code": "timeout"}},
            "payment_link": {"entity": {"id": "plink_1"}},
        },
    }
    res = orch.handle_webhook(payload, store=store)
    assert res["decision"] == "retry_link"
    assert store.count_attempts("pay_13") == 2


# --- dashboard stats ---


def test_dashboard_stats(store, fake_rzp):
    orch.handle_payment_failed("pay_15", "pay_15", "insufficient_funds", 499900, store=store)
    orch.handle_payment_failed(
        "pay_16", "pay_16", "bank_decline", 199900, retry_delay=0, store=store
    )
    orch.on_payment_link_paid("plink_1", store=store)
    orch.on_payment_link_paid("plink_2", store=store)
    s = orch.dashboard_stats(store=store)
    assert s["total_failed"] == 2
    assert s["retried"] == 1
    assert s["wa_sent"] == 1
    assert s["recovered"] == 2
    assert s["amount_recovered_paise"] == 499900 + 199900
