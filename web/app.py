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
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import recovery.orchestrator as orch
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


def build_mandate(user_id="u_42"):
    """Spec §4.2 — signed downstream by policy.mandate.sign."""
    now = datetime.now(UTC)
    return {
        "id": "mand_" + secrets.token_hex(2),
        "user_id": user_id,
        "max_amount": MAX_MANDATE_PAISE,
        "currency": "INR",
        "allowlist": [CATALOG["merchant"]],
        "expiry": (now + timedelta(minutes=MANDATE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    user_id = parse_qs(body).get("user_id", ["u_42"])[0]
    mandate = build_mandate(user_id)
    token = sign(mandate, settings.mandate_secret)
    logger.info("mandate approved user_id=%s mandate_id=%s", user_id, mandate["id"])
    return templates.TemplateResponse(
        request=request,
        name="approve.html",
        context={"token": token, "mandate": mandate},
    )


@app.get("/audit", response_class=HTMLResponse)
def audit(request: Request):
    return templates.TemplateResponse(
        request=request, name="audit.html", context={"entries": read_audit()}
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    stats = orch.dashboard_stats(store=get_store())
    return templates.TemplateResponse(
        request=request, name="dashboard.html", context={"stats": stats}
    )
