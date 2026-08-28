"""F04: Web — catalog view, approve mandate (₹5k/15m JWT), audit view, /dashboard.

Thin layer: validate → delegate to policy.mandate / recovery.orchestrator → render.
"""

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import recovery.orchestrator as orch
from agent.buyer import buy as buyer_buy
from policy.authorize import authorize
from policy.mandate import sign
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

with open("catalog/freshmart.json") as _f:
    CATALOG = json.load(_f)

MAX_MANDATE_PAISE = 500000  # ₹5k fixed mandate (spec §1 / §4.2)
MANDATE_MINUTES = 15
AUDIT_PATH = "audit.jsonl"

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def inr(paise):
    return f"₹{paise / 100:,.0f}"


templates.env.filters["inr"] = inr

app = FastAPI(title="RekhaVasool")


def build_mandate(user_id="u_42", max_amount_paise=MAX_MANDATE_PAISE, minutes=MANDATE_MINUTES):
    """Spec §4.2 — signed downstream by policy.mandate.sign. Limit + duration editable."""
    now = datetime.now(UTC)
    return {
        "id": "mand_" + secrets.token_hex(2),
        "user_id": user_id,
        "max_amount": max_amount_paise,
        "currency": "INR",
        "allowlist": [CATALOG["merchant"]],
        "expiry": (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iat": int(now.timestamp()),
        "catalog_version": CATALOG["catalog_version"],
    }


def get_store():
    return orch.RecoveryStore()


def read_audit():
    """Parse audit.jsonl and verify each entry's prev_hash chain (spec §4.4)."""
    if not os.path.exists(AUDIT_PATH):
        return []
    entries = []
    prev_raw = None
    with open(AUDIT_PATH) as f:
        for raw in f.read().splitlines():
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue  # skip malformed line, chain continues from last good one
            if prev_raw is None:
                entry["_chain_ok"] = True  # genesis — nothing to verify against
            else:
                entry["_chain_ok"] = (
                    entry.get("prev_hash") == hashlib.sha256(prev_raw.encode()).hexdigest()
                )
            entries.append(entry)
            prev_raw = raw
    return entries


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"catalog": CATALOG, "max_amount": MAX_MANDATE_PAISE, "minutes": MANDATE_MINUTES},
    )


@app.post("/approve", response_class=HTMLResponse)
async def approve(request: Request):
    # stdlib form parse — urlencoded only, avoids python-multipart dep
    body = (await request.body()).decode()
    qs = parse_qs(body)
    user_id = qs.get("user_id", ["u_42"])[0]
    # editable limit (₹) + duration (minutes); fall back to defaults
    try:
        amount_rs = float(qs.get("max_amount", [""])[0]) if qs.get("max_amount", [""])[0] else None
    except ValueError:
        amount_rs = None
    max_amount_paise = int(amount_rs * 100) if amount_rs else MAX_MANDATE_PAISE
    try:
        minutes = int(qs.get("minutes", [""])[0]) if qs.get("minutes", [""])[0] else None
    except ValueError:
        minutes = None
    minutes = minutes or MANDATE_MINUTES
    mandate = build_mandate(user_id, max_amount_paise, minutes)
    token = sign(mandate, settings.mandate_secret)
    logger.info(
        "mandate approved user_id=%s mandate_id=%s max=%s min=%s",
        user_id,
        mandate["id"],
        max_amount_paise,
        minutes,
    )
    return templates.TemplateResponse(
        request=request,
        name="approve.html",
        context={"token": token, "mandate": mandate, "catalog": CATALOG},
    )


@app.post("/shop", response_class=HTMLResponse)
async def shop(request: Request):
    """Post-approve landing: catalog + buy form, mandate token rides hidden."""
    body = (await request.body()).decode()
    token = parse_qs(body).get("mandate_token", [""])[0]
    if not token:
        return HTMLResponse("missing mandate token", status_code=400)
    return templates.TemplateResponse(
        request=request, name="shop.html", context={"token": token, "catalog": CATALOG}
    )


@app.post("/buy", response_class=HTMLResponse)
async def buy_route(request: Request):
    """Happy path + attack demo: user_text + mandate_token → buyer → authorize → Payment Link."""
    body = (await request.body()).decode()
    qs = parse_qs(body)
    user_text = qs.get("user_text", [""])[0]
    mandate_token = qs.get("mandate_token", [""])[0]
    if not user_text or not mandate_token:
        return HTMLResponse("missing user_text or mandate_token", status_code=400)
    intent = buyer_buy(user_text, CATALOG)
    if intent is None:
        # no SKU matched → treat as unknown_sku DENIED
        decision = type(
            "_D",
            (),
            {
                "decision": "DENIED",
                "reason": "unknown_sku",
                "canonical_price": None,
                "payment_link_id": None,
                "mandate_id": None,
            },
        )()
        short_url = None
    else:
        decision = authorize(intent, mandate_token, CATALOG)
        short_url = None
        if decision.decision == "PAID" and decision.payment_link_id:
            # fetch short_url for display — polling fallback will confirm paid status
            try:
                link = orch.fetch_payment_link(decision.payment_link_id)
                short_url = link.get("short_url")
            except Exception:  # noqa: BLE001 — fetch fails → show id only
                short_url = None
    return templates.TemplateResponse(
        request=request,
        name="buy.html",
        context={
            "user_text": user_text,
            "intent": intent,
            "decision": decision,
            "short_url": short_url,
            "catalog": CATALOG,
        },
    )


@app.get("/audit", response_class=HTMLResponse)
def audit(request: Request):
    return _dashboard(request)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    return _dashboard(request)


def _dashboard(request: Request):
    stats = orch.dashboard_stats(store=get_store())
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"stats": stats, "entries": read_audit()},
    )


@app.post("/webhook", response_class=JSONResponse)
async def webhook(request: Request):
    """Razorpay webhooks: payment_link.paid / payment.failed + polling fallback."""
    try:
        payload = json.loads((await request.body()).decode() or "{}")
    except Exception:  # noqa: BLE001 — malformed webhook JSON → 400
        return JSONResponse({"error": "invalid json"}, status_code=400)
    result = orch.handle_webhook(payload, store=get_store())
    return JSONResponse(result)


@app.get("/poll/{link_id}", response_class=JSONResponse)
def poll(link_id: str):
    """Polling fallback GET /v1/payment_links/{id} every 2s/15s if webhook missed."""
    result = orch.poll_payment_link(link_id, store=get_store())
    return JSONResponse(result)


@app.post("/voice-dial", response_class=JSONResponse)
async def voice_dial(request: Request):
    """Live Pipecat voice — Twilio → Sarvam Bulbul → Gemini, 45s 2-turn, 2-min cap."""
    body = (await request.body()).decode()
    qs = parse_qs(body)
    to = qs.get("to", [settings.wa_to])[0]
    try:
        # 2-min timeout wrapper
        import asyncio

        from voice.agent import dial

        result = await asyncio.wait_for(asyncio.to_thread(dial, to), timeout=120)
        return JSONResponse({"status": "dialed", "to": to, "sid": result.get("sid")})
    except Exception as e:  # noqa: BLE001
        logger.error("voice dial failed %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/voice", response_class=FileResponse)
def voice(text: str = "Payment recovered"):
    """gTTS on-demand — 0 bytes in repo, labeled mock — live via Sarvam+Exotel in prod."""
    import tempfile

    from gtts import gTTS

    # ponytail: on-demand generation, no cache — add cache if demo repeats
    fd, path = tempfile.mkstemp(suffix=".mp3")
    import os as _os

    _os.close(fd)
    # cap length to avoid abuse
    clipped = text[:200]
    tts = gTTS(text=clipped, lang="en")
    tts.save(path)
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename="rekhavasool_mock.mp3",
        headers={"X-Mock": "mock - live via Sarvam+Exotel in prod"},
    )
