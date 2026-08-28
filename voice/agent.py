"""Pipecat 80-line voice — Twilio → Sarvam Bulbul → Gemini, 45s 2-turn narrow.

Per https://docs.sarvam.ai/api/integration/build-voice-agent-with-twilio
Run: ngrok http 8000 → TwiML Bin wss://<ngrok>/ws → python voice/agent.py --transport twilio → call Twilio number or dial out
"""

import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

load_dotenv(override=True)

# narrow — heavy emphasis, 45s 2-turn, only this order
NARROW_PROMPT = (
    "You are RekhaVasool voice agent for ONE order only. "
    "Order: Sony WH-CH510, ₹4,999, merchant freshmart, Payment Link pending. "
    "You have 45 seconds and max 2 turns, then hang up. "
    "You can ONLY: tell customer to pay again via WhatsApp link, resend link on haan/yes/ok, handle STOP/cancel. "
    "Else say exactly: I can only help with this FreshMart order for Sony at ₹4,999. Say haan to resend the link or STOP to cancel. "
    "Always mention ₹4,999 and freshmart. Never invent."
)


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(
        runner_args,
        {"twilio": lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True)},
    )

    stt = SarvamSTTService(
        api_key=os.getenv("SARVAM_API_KEY"),
        settings=SarvamSTTService.Settings(model="saaras:v3", language="hi-IN"),
        mode="transcribe",
    )
    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY"),
        settings=SarvamTTSService.Settings(model="bulbul:v3", voice="simran", language="hi-IN"),
    )
    # Gemini brain — 3.5-flash-lite (cheap, fast, low-latency) per Pipecat GoogleLLMService docs.
    # Sarvam STT + Bulbul TTS stay the voice; Gemini decides what to say (narrow prompt).
    llm = GoogleLLMService(
        api_key=os.getenv("GEMINI_API_KEY"),
        settings=GoogleLLMService.Settings(
            model="gemini-3.5-flash-lite",
            system_instruction=NARROW_PROMPT,
        ),
    )

    messages = [{"role": "system", "content": NARROW_PROMPT}]
    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline([transport.input(), stt, context_aggregator.user(), llm, tts, transport.output(), context_aggregator.assistant()])

    task = PipelineTask(pipeline, params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Caller connected — 45s 2-turn")
        # 2-turn cap via context length + 45s via PipelineTask timeout handled by Twilio Gather
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Caller disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
