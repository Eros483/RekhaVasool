"""F04: Web — catalog view, approve mandate (₹5k/15m JWT), audit view, /dashboard."""

import hashlib
import html
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import recovery.orchestrator as orch
import web.app as web_app
from policy.mandate import verify
from utils.config import settings

JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


@pytest.fixture()
def client():
    return TestClient(web_app.app)


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "recovery.db")


@pytest.fixture()
def audit(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(web_app, "AUDIT_PATH", str(path))
    return path


# --- catalog view ---


def test_catalog_view_lists_three_skus(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "FreshMart" in html
    assert "Sony WH-CH510" in html
    assert "₹4,999" in html  # 499900 paise
    assert "boAt Rockerz 450" in html
    assert "₹1,999" in html
    assert "JBL Tune 710BT" in html
    assert "₹6,500" in html  # jbl_tune over ₹5k budget on purpose


# --- approve mandate flow ---


def test_approve_mandate_returns_valid_5k_15m_jwt(client):
    r = client.post("/approve", data={"user_id": "u_42"})
    assert r.status_code == 200
    token = JWT_RE.search(r.text).group(0)
    ok, payload = verify(token, settings.mandate_secret)
    assert ok is True
    assert payload["user_id"] == "u_42"
    assert payload["max_amount"] == 500000  # ₹5k fixed mandate
    assert payload["currency"] == "INR"
    assert payload["allowlist"] == ["freshmart"]
    assert payload["catalog_version"] == "1.0"
    assert payload["id"].startswith("mand_")
    expiry = datetime.fromisoformat(payload["expiry"])
    now = datetime.now(UTC)
    assert timedelta(minutes=14) <= expiry - now <= timedelta(minutes=16)


def test_approve_mandate_defaults_user_id(client):
    r = client.post("/approve", data={})
    token = JWT_RE.search(r.text).group(0)
    ok, payload = verify(token, settings.mandate_secret)
    assert ok is True
    assert payload["user_id"] == "u_42"


# --- audit view ---


def test_audit_view_empty_when_no_file(client, audit):
    r = client.get("/audit")
    assert r.status_code == 200
    assert "No audit entries" in r.text


def test_audit_view_shows_entries_and_chain(client, audit):
    e1 = {
        "ts": "2026-08-27T10:00:00Z",
        "prev_hash": "GENESIS",
        "mandate_id": "mand_8f3a",
        "intent": {"sku": "sony_ch510"},
        "decision": "PAID",
        "reason": None,
        "canonical_price": 499900,
        "payment_link_id": "plink_1",
        "source": "webhook",
    }
    raw1 = json.dumps(e1)
    e2 = {
        **e1,
        "intent": {"sku": "jbl_tune"},
        "decision": "DENIED",
        "reason": "amount>max",
        "prev_hash": hashlib.sha256(raw1.encode()).hexdigest(),
    }
    audit.write_text(raw1 + "\n" + json.dumps(e2) + "\n")

    r = client.get("/audit")
    assert r.status_code == 200
    body = html.unescape(r.text)  # Jinja autoescapes `>` in "amount>max"
    assert "sony_ch510" in body
    assert "PAID" in body
    assert "amount>max" in body
    assert "DENIED" in body
    assert r.text.count('class="ok"') == 2  # both links of the chain verify


def test_audit_view_marks_broken_chain(client, audit):
    e1 = {
        "ts": "2026-08-27T10:00:00Z",
        "prev_hash": "GENESIS",
        "intent": {"sku": "sony_ch510"},
        "decision": "PAID",
        "reason": None,
        "canonical_price": 499900,
        "payment_link_id": "plink_1",
        "source": "webhook",
    }
    raw1 = json.dumps(e1)
    e2 = {
        **e1,
        "intent": {"sku": "boat_450"},
        "decision": "DENIED",
        "reason": "allowlist",
        "prev_hash": "0" * 64,  # tampered — does not match sha256(raw1)
    }
    audit.write_text(raw1 + "\n" + json.dumps(e2) + "\n")

    r = client.get("/audit")
    assert r.text.count('class="ok"') == 1  # genesis ok, link broken
    assert 'class="bad"' in r.text


@pytest.fixture()
def fake_rzp(monkeypatch):
    """Fake Razorpay + WA drivers so seeding never hits the network."""
    n = {"v": 0}

    def create_payment_link(amount_paise, customer_contact, notes, expire_minutes=15):
        n["v"] += 1
        return {"id": f"plink_{n['v']}", "short_url": "https://rzp.io/test"}

    def cancel_payment_link(link_id):
        pass

    def send_wa(to, text):
        pass

    def fetch_payment_link(link_id):
        return {"id": link_id, "status": "paid"}

    monkeypatch.setattr(orch, "create_payment_link", create_payment_link)
    monkeypatch.setattr(orch, "cancel_payment_link", cancel_payment_link)
    monkeypatch.setattr(orch, "send_wa", send_wa)
    monkeypatch.setattr(orch, "fetch_payment_link", fetch_payment_link)


# --- dashboard ---


def test_dashboard_shows_recovery_counters(client, db_path, monkeypatch, fake_rzp):
    # fresh connection per request (TestClient runs app in another thread); same DB file
    monkeypatch.setattr(web_app, "get_store", lambda: orch.RecoveryStore(db_path))
    store = orch.RecoveryStore(db_path)
    orch.handle_payment_failed("pay_15", "pay_15", "insufficient_funds", 499900, store=store)
    orch.handle_payment_failed(
        "pay_16", "pay_16", "bank_decline", 199900, retry_delay=0, store=store
    )
    orch.on_payment_link_paid("plink_1", store=store)
    orch.on_payment_link_paid("plink_2", store=store)

    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Payments failed" in r.text
    assert "Retried" in r.text
    assert "WhatsApp nudges" in r.text
    assert "Recovered" in r.text
    assert "₹6,998" in r.text  # 499900 + 199900 paise recovered


def test_dashboard_empty_state(client, db_path, monkeypatch):
    monkeypatch.setattr(web_app, "get_store", lambda: orch.RecoveryStore(db_path))
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "₹0" in r.text
