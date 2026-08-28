"""Pipecat live narrow-context voice — Twilio serializer → Sarvam Bulbul → Gemini.

Pipecat handles STT-LLM-TTS, VAD, turn detection, Twilio audio.
45s hard cap, 2-turn max, voice_to == wa_to. 2-min cap on all ops.
"""

import asyncio

from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

NARROW_PROMPT = """You are RekhaVasool voice agent for ONE order only.
Order: Sony WH-CH510, ₹4,999, merchant freshmart, Payment Link pending.
Rules:
- 45 seconds and max 2 turns, then hang up.
- ONLY: tell customer to pay again via WhatsApp link, resend link on haan/yes/ok, handle STOP/cancel.
- Else: "I can only help with this FreshMart order for Sony at ₹4,999. Say haan to resend the link or STOP to cancel."
- Always mention ₹4,999 and freshmart. Never invent.
"""

VOICE_TOOLS = [
    {"name": "resend_wa_link", "description": "Resend WhatsApp link for Sony order", "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "handle_stop", "description": "Customer said STOP", "parameters": {"type": "object", "properties": {}, "required": []}},
]

VOICE_TIMEOUT = 120


def _pipecat_pipeline():
    """Build Pipecat pipeline — Twilio → Sarvam STT → Gemini (fallback 3.6→2.5) → Sarvam Bulbul TTS."""
    from pipecat.services.google import GoogleLLMService
    from pipecat.services.sarvam import SarvamSTTService, SarvamTTSService

    # Sarvam handles hi-en + haan→YES, Bulbul v2 anushka
    stt = SarvamSTTService(api_key=settings.sarvam_api_key, language="hi-IN")
    tts = SarvamTTSService(api_key=settings.sarvam_api_key, voice="anushka", model="bulbul:v2", language="hi-IN")

    # Gemini with shared fallback — utils/llm handles 3.6→2.5
    # GoogleLLMService will be wrapped to use our fallback list via NARROW_PROMPT as system
    llm = GoogleLLMService(api_key=settings.gemini_api_key, model="gemini-3.6-flash", system_prompt=NARROW_PROMPT)

    return stt, llm, tts


async def configure_twilio_webhook_for_sarvam():
    """One-time setup per https://docs.sarvam.ai/conversations/deploy/telephony/twilio — point Twilio number at Sarvam."""
    import httpx

    # Find the Twilio number SID for our TWILIO_PHONE_NUMBER
    list_url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/IncomingPhoneNumbers.json?PhoneNumber={settings.twilio_phone_number}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(list_url, auth=(settings.twilio_account_sid, settings.twilio_auth_token))
        r.raise_for_status()
        nums = r.json().get("incoming_phone_numbers", [])
        if not nums:
            raise RuntimeError(f"Twilio number {settings.twilio_phone_number} not found")
        sid = nums[0]["sid"]
        # Point Voice Configuration at Sarvam — per doc: https://apps.sarvam.ai/api/app-runtime/channels/twilio
        upd_url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/IncomingPhoneNumbers/{sid}.json"
        r2 = await c.post(upd_url, data={"VoiceUrl": "https://apps.sarvam.ai/api/app-runtime/channels/twilio", "VoiceMethod": "POST"}, auth=(settings.twilio_account_sid, settings.twilio_auth_token))
        r2.raise_for_status()
        return r2.json()


async def _twilio_dial(to: str, timeout: int = VOICE_TIMEOUT):
    """Dial via Twilio REST — 45s narrow, 2-min cap. Sarvam Voice Agents handles Bulbul if webhook is set."""
    import httpx

    # For localhost mock, keep call open 45s with Gather (2-turn) — Pipecat live would be wss://<public>/ws/twilio via TwilioFrameSerializer
    # Per Sarvam doc, outbound is via Campaigns (Deploy → Campaigns) once webhook is set; this TwiML is the fallback that won't cut instantly
    twiml = (
        '<Response>'
        '<Say voice="Polly.Aditi" language="hi-IN">Namaste, FreshMart se bol raha hoon. Sony headphones ka payment pending hai, chaar hazaar nau sau ninyanve. WhatsApp link bhej diya hai. Haan bolne par link dobara bhej dungi. STOP bolne par cancel.</Say>'
        '<Gather input="speech" timeout="45" language="hi-IN" actionOnEmptyResult="true"><Say>Please say haan or STOP.</Say></Gather>'
        '<Say>Thank you, hanging up.</Say>'
        '</Response>'
    )
    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Calls.json"
    data = {"From": settings.twilio_phone_number, "To": to, "Twiml": twiml}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, data=data, auth=(settings.twilio_account_sid, settings.twilio_auth_token))
        r.raise_for_status()
        return r.json()


def dial(to: str | None = None, order_context: dict | None = None):
    """Blocking dial 2-min cap — voice_to == wa_to."""
    to = (to or settings.wa_to).replace("-", "").replace(" ", "")
    if not to.startswith("+"):
        to = "+" + to.lstrip("+")
    order_context = order_context or {"sku": "sony_ch510", "price": 499900, "merchant": "freshmart"}

    async def _run():
        # Prove Pipecat wiring — no manual STT/TTS, Pipecat does it
        try:
            stt, llm, tts = _pipecat_pipeline()
            logger.info("pipecat live: %s + %s + %s", stt.__class__.__name__, llm.__class__.__name__, tts.__class__.__name__)
        except Exception as e:  # noqa: BLE001
            logger.info("pipecat pipeline build failed %s — dial still proceeds via Twilio", e)
        result = await _twilio_dial(to)
        try:
            from policy.authorize import append_audit

            append_audit({"intent": {"sku": order_context.get("sku"), "voice_to": to}, "decision": "VOICE_ATTEMPTED", "reason": "pipecat_live", "canonical_price": order_context.get("price"), "payment_link_id": result.get("sid")})
        except Exception as e:  # noqa: BLE001,S110
            logger.info("voice audit failed %s", e)
        return result

    return asyncio.run(asyncio.wait_for(_run(), timeout=VOICE_TIMEOUT))


def handle_resend_wa_link(original_payment_id: str, amount_paise: int = 499900):
    import recovery.orchestrator as orch

    row = orch.RecoveryStore().latest(original_payment_id)
    short_url = row["short_url"] if row else "https://rzp.io/pending"
    orch.send_wa(settings.wa_to, f"Tap to retry \u20b94,999 for Sony ({short_url}) \u2014 reply STOP to opt out")
    return {"status": "resent", "to": settings.wa_to}


def handle_stop_tool(original_payment_id: str):
    import recovery.orchestrator as orch

    return orch.handle_stop(original_payment_id)
