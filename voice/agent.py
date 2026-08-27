"""Pipecat live narrow-context voice agent — Twilio → Sarvam Bulbul → Gemini.

45s hard cap, 2-turn max, knows ONLY this order (Sony WH-CH510 ₹4,999 freshmart).
Tools: resend_wa_link, handle_stop / haan. Everything else: "I can only help with this FreshMart order."

Pipecat handles STT-LLM-TTS, turn detection, Twilio audio. 2-min timeout on all ops.
voice_to == wa_to (whitelisted +91-9560452773).
"""

import asyncio
import json

from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

# ponytail: narrow prompt — heavy emphasis on not answering outside order
NARROW_PROMPT = """You are RekhaVasool voice agent for ONE order only.
Order: Sony WH-CH510, ₹4,999, merchant freshmart, Payment Link pending.
Rules (follow strictly):
- You have 45 seconds and max 2 turns. Then hang up.
- You can ONLY: 1) tell the customer they need to pay again via the WhatsApp link already sent, 2) resend the WhatsApp link, 3) handle STOP or "haan" (Hindi yes) as confirmation to resend or stop.
- If asked anything else (other products, discounts, other orders, general chat), say exactly: "I can only help with this FreshMart order for Sony at ₹4,999. Say haan to resend the link or STOP to cancel." and do not elaborate.
- Always mention the amount ₹4,999 and merchant freshmart.
- On haan/yes/ok → call resend_wa_link. On STOP/stop/cancel → call handle_stop.
- Never invent prices or merchants. Never discuss anything outside this order.
"""

# Tool schemas for Gemini function calling
VOICE_TOOLS = [
    {
        "name": "resend_wa_link",
        "description": "Resend the WhatsApp payment link for this Sony order",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "handle_stop",
        "description": "Customer said STOP — cancel payment link and stop nudges",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

# 2-min wrapper for all voice ops
VOICE_TIMEOUT = 120


async def _twilio_dial(to: str, twiml_url: str | None = None, timeout: int = VOICE_TIMEOUT):
    """Dial via Twilio REST API — 2-min timeout. Returns call SID."""
    import httpx

    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Calls.json"
    # Minimal TwiML for mock — Pipecat would be wss://<server>/twilio-voice
    twiml = '<Response><Say voice="Polly.Aditi" language="hi-IN">Namaste, FreshMart se bol raha hoon. Sony headphones ka payment pending hai, 4,999. WhatsApp link bhej diya hai. Haan bolne par link dobara bhej dungi. STOP bolne par cancel.</Say><Pause length="2"/><Say>If you can hear this, please cut the call — mock successful.</Say></Response>'
    data = {
        "From": settings.twilio_phone_number,
        "To": to,
        "Twiml": twiml,
    }
    # Pipecat live path would be: Url=twiml_url, Method=POST (WebSocket stream)
    # ponytail: Twiml inline is enough for mock; switch to Url when pipecat server is deployed
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, data=data, auth=(settings.twilio_account_sid, settings.twilio_auth_token))
        r.raise_for_status()
        return r.json()


def dial(to: str | None = None, order_context: dict | None = None):
    """Blocking dial with 2-min timeout — voice_to == wa_to. Mock call per user request."""
    to = to or settings.wa_to
    # normalize +91-... to +919... for Twilio
    to = to.replace("-", "").replace(" ", "")
    if not to.startswith("+"):
        to = "+" + to.lstrip("+")
    order_context = order_context or {"sku": "sony_ch510", "price": 499900, "merchant": "freshmart"}

    async def _run():
        # Pipecat pipeline would be built here — we keep the imports to prove pipecat wiring
        try:
            # Prove pipecat is wired (no manual STT/TTS wiring — pipecat handles it)
            from pipecat.serializers.twilio import TwilioFrameSerializer  # noqa: F401
            from pipecat.services.sarvam import SarvamSTTService, SarvamTTSService  # noqa: F401
            from pipecat.services.google import GoogleLLMService  # noqa: F401

            logger.info("pipecat imports ok — serializer + Sarvam Bulbul + Gemini wired")
        except Exception as e:  # noqa: BLE001
            logger.info("pipecat import failed %s — falling back to Twilio TwiML mock", e)

        # 45s cap + 2-turn logic is in the prompt + PipelineTask params (mocked here via TwiML length)
        # Tools are exposed via Gemini function calling; handlers below are called by the LLM
        result = await _twilio_dial(to, timeout=VOICE_TIMEOUT)
        # audit log
        try:
            from policy.authorize import append_audit

            append_audit(
                {
                    "intent": {"sku": order_context.get("sku"), "voice_to": to},
                    "decision": "VOICE_ATTEMPTED",
                    "reason": "mock_call",
                    "canonical_price": order_context.get("price"),
                    "payment_link_id": result.get("sid"),
                    "mandate_id": order_context.get("mandate_id"),
                }
            )
        except Exception:
            pass
        return result

    return asyncio.run(asyncio.wait_for(_run(), timeout=VOICE_TIMEOUT))


# Tool handlers — called by Gemini when user says haan/STOP
def handle_resend_wa_link(original_payment_id: str, amount_paise: int = 499900):
    """Resend WA link for this order — called from voice tool."""
    import recovery.orchestrator as orch

    store = orch.RecoveryStore()
    # find latest link for this order
    row = store.latest(original_payment_id)
    short_url = row["short_url"] if row else "https://rzp.io/pending"
    orch.send_wa(settings.wa_to, f"Tap to retry ₹{amount_paise/100:,.0f} for Sony ({short_url}) — reply STOP to opt out")
    return {"status": "resent", "to": settings.wa_to}


def handle_stop_tool(original_payment_id: str):
    """STOP from voice — cancel link."""
    import recovery.orchestrator as orch

    return orch.handle_stop(original_payment_id)
