"""F03: Buyer Agent — Gemini 2.0 Flash emits intent {sku,qty,merchant,raw_text} only.

Never emits an `amount`; canonical price comes from catalog at pinned version
(§4.3 / SC4). Rule-based fallback when GEMINI_ENABLED=false (like the harness).

    # line 12: inline MCP config to Razorpay @razorpay/mcp
    # create_payment_link(amount=<canonical_price>, currency="INR",
    #                     expire_by=now+15m, notes={mandate_id, catalog_version,
    #                                               idempotency_key, sku})
    # Buyer agent calls the MCP tool with the CANONICAL price only — never an
    # LLM-emitted amount. See policy/authorize.create_payment_link for the wire call.
"""

import json
import re

from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

MERCHANT = "freshmart"


def _keyword_for(sku: dict) -> list[str]:
    """Lowercased tokens of the SKU name+desc used for rule-based matching."""
    return re.findall(r"[a-z0-9]+", (sku["name"] + " " + sku.get("desc", "")).lower())


def _rule_based(user_text: str, catalog: dict) -> dict | None:
    """Pick the best-known SKU from the user's words. No LLM. No amount."""
    lower = user_text.lower()
    tokens = set(re.findall(r"[a-z0-9]+", lower))
    # ponytail: parse cheap semantic filters — under Xk + NC — without an LLM
    max_price = None
    m = re.search(r"under\s*(?:rs\.?|₹)?\s*(\d+)\s*k", lower)
    if m:
        max_price = int(m.group(1)) * 100000  # k → paise (5k = 500000)
    else:
        m2 = re.search(r"under\s*(?:rs\.?|₹)?\s*(\d+)", lower)
        if m2:
            v = int(m2.group(1))
            max_price = v * 100 if v < 1000 else v  # heuristic: small number = rupees
    nc_wanted = bool(re.search(r"\bnc\b|noise\s*cancelling|noise\s*cancel", lower))
    # filter candidates by price/NC before scoring
    candidates = catalog.get("skus", [])
    if max_price is not None:
        candidates = [s for s in candidates if s.get("price", 0) <= max_price]
        if not candidates:
            candidates = catalog.get("skus", [])  # don't over-filter to empty on parse error
    if nc_wanted:
        nc_candidates = [s for s in candidates if s.get("nc")]
        if nc_candidates:
            candidates = nc_candidates
    best, best_score = None, 0
    for sku in candidates:
        kw = set(_keyword_for(sku))
        # also score nc token overlap
        score = len(tokens & kw)
        if nc_wanted and sku.get("nc"):
            score += 1
        if score > best_score:
            best, best_score = sku, score
    # if no keyword overlap but we have filtered candidates (e.g. "headphones under 5k NC"), pick cheapest match
    if best is None or best_score == 0:
        if candidates and (nc_wanted or max_price is not None):
            best = min(candidates, key=lambda s: s.get("price", 0))
            best_score = 1
        else:
            return None
    return {
        "sku": best["sku"],
        "qty": 1,
        "merchant": catalog.get("merchant", MERCHANT),
        "raw_text": user_text,
    }


def _gemini_intent(user_text: str, catalog: dict) -> dict:
    """Gemini 2.0 Flash → intent. Output is UNTRUSTED: `buy` validates it."""
    import google.generativeai as genai

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = (
        "You are a buyer agent for merchant 'freshmart'. Choose one SKU from this "
        "catalog that best matches the user's request, and reply as JSON ONLY with "
        "keys: sku, qty, merchant. Never include an amount or price.\n"
        f"catalog: {json.dumps([s['sku'] for s in catalog['skus']])}\n"
        f"user: {user_text}"
    )
    resp = model.generate_content(prompt, request_options={"timeout": 5})
    text = (getattr(resp, "text", "") or "").strip()
    # strip code fences, take first balanced {...}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON in Gemini response")
    return json.loads(m.group(0))


def buy(user_text: str, catalog: dict) -> dict | None:
    """Turn user text into {sku,qty,merchant,raw_text}. None if no known SKU."""
    if settings.gemini_enabled:
        try:
            raw = _gemini_intent(user_text, catalog)
            raw.pop("amount", None)  # LLM amount never trusted (§4.3)
            sku = next((s for s in catalog["skus"] if s["sku"] == raw.get("sku")), None)
            if sku is None:
                return None  # only canonical SKUs pass
            return {
                "sku": sku["sku"],
                "qty": int(raw.get("qty") or 1),
                "merchant": catalog.get("merchant", MERCHANT),
                "raw_text": user_text,
            }
        except Exception:  # noqa: BLE001 — Gemini failure → rule-based fallback
            logger.info("gemini failed, falling back to rule-based buyer")
    return _rule_based(user_text, catalog)
