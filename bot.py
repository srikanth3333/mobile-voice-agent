#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os
import sys
import asyncio

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.services.azure.tts import AzureTTSService
from pipecat.frames.frames import EndFrame, TTSSpeakFrame
from pipecat.processors.user_idle_processor import UserIdleProcessor

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

# Default configuration
DEFAULT_CONFIG = {
    "llm_context": "You are a friendly assistant making an outbound phone call. Your responses will be read aloud, so keep them concise and conversational. Avoid special characters or formatting. Begin by politely greeting the person and explaining why you're calling.",
    "session_duration": 180,  # 3 minutes
    "idle_warning_timeout": 8,  # 8 seconds
    "idle_disconnect_timeout": 30  # 30 seconds
}

async def run_bot(transport: BaseTransport, handle_sigint: bool, config: dict = None):
    """Run the bot with configurable parameters."""
    
    # Use provided config or defaults
    if config is None:
        config = DEFAULT_CONFIG
    
    llm_context = config.get("llm_context", DEFAULT_CONFIG["llm_context"])
    session_duration = config.get("session_duration", DEFAULT_CONFIG["session_duration"])
    idle_warning_timeout = config.get("idle_warning_timeout", DEFAULT_CONFIG["idle_warning_timeout"])
    idle_disconnect_timeout = config.get("idle_disconnect_timeout", DEFAULT_CONFIG["idle_disconnect_timeout"])
    
    logger.info(f"Starting bot with session duration: {session_duration}s, idle warning: {idle_warning_timeout}s")
    
    # Initialize services
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"))
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    tts = AzureTTSService(
        api_key=os.getenv("AZURE_SPEECH_API_KEY"),
        region=os.getenv("AZURE_SPEECH_REGION", "eastus"),
        voice="en-US-JennyNeural",
        language="en-US"
    )

    # Session timer task
    session_timer_task = None

    async def end_session_timer(task):
        """End session after specified duration"""
        await asyncio.sleep(session_duration)
        logger.info(f"Session reached {session_duration}s limit")
        try:
            await task.queue_frame(TTSSpeakFrame(
                "Your session time is up. Thank you for calling. Goodbye!"
            ))
            await asyncio.sleep(3)
            await task.queue_frame(EndFrame())
        except Exception as e:
            logger.error(f"Error ending session: {e}")

    # User idle handler
    async def handle_user_idle(user_idle: UserIdleProcessor, retry_count: int) -> bool:
        """Handle user idle with progressive warnings"""
        logger.info(f"User idle detected, retry count: {retry_count}")
        
        if retry_count == 1:
            await user_idle.push_frame(TTSSpeakFrame("Are you still there?"))
            return True
        elif retry_count == 2:
            await user_idle.push_frame(TTSSpeakFrame("Hello? Would you like to continue our conversation?"))
            return True
        else:
            await user_idle.push_frame(TTSSpeakFrame("It seems like you're busy. Thank you for calling. Goodbye!"))
            await asyncio.sleep(3)
            return False 
    
    # Create user idle processor
    user_idle = UserIdleProcessor(
        callback=handle_user_idle,
        timeout=idle_warning_timeout
    )

    # Create context with configurable prompt
    messages = [{"role": "system", "content": llm_context}]
    context = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)

    # Build pipeline with idle processor
    pipeline = Pipeline([
        transport.input(),
        user_idle,  # Add idle processor to pipeline
        stt,
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            enable_metrics=True,
            enable_usage_metrics=True,
            allow_interruptions=True,
        ),
        idle_timeout_secs=idle_disconnect_timeout,
        cancel_on_idle_timeout=False,
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        nonlocal session_timer_task
        # Start session timer
        session_timer_task = asyncio.create_task(end_session_timer(task))
        logger.info("Starting outbound call conversation with session timer")
        from pipecat.frames.frames import LLMRunFrame
        await task.queue_frame(LLMRunFrame())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        nonlocal session_timer_task
        logger.info("Outbound call ended")
        
        # Cancel session timer if still running
        if session_timer_task and not session_timer_task.done():
            session_timer_task.cancel()
        
        await task.cancel()

    @task.event_handler("on_idle_timeout")
    async def on_idle_timeout(task):
        logger.info("Final idle timeout - ending call")
        await task.queue_frame(TTSSpeakFrame("Session timed out. Goodbye!"))
        await asyncio.sleep(3)
        await task.queue_frame(EndFrame())

    runner = PipelineRunner(handle_sigint=handle_sigint)

    try:
        await runner.run(task)
    finally:
        # Cleanup session timer
        if session_timer_task and not session_timer_task.done():
            session_timer_task.cancel()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point compatible with Pipecat Cloud."""
    
    print("Bot function started, waiting for Twilio data...")
    
    # Extract configuration from the body parameter
    body = getattr(runner_args, "body", {})
    logger.info(f"Received body parameters: {list(body.keys()) if body else 'None'}")
    
    # Check if we have pre-extracted call data (from server.py)
    if hasattr(runner_args, 'call_data'):
        call_data = runner_args.call_data
        logger.info(f"Using pre-extracted call data: {call_data}")
    else:
        # Fallback to parsing websocket
        try:
            print("Calling parse_telephony_websocket...")
            transport_type, call_data = await asyncio.wait_for(
                parse_telephony_websocket(runner_args.websocket),
                timeout=30.0
            )
            logger.info(f"Auto-detected transport: {transport_type}")
            logger.info(f"Call data: {call_data}")
        except Exception as e:
            logger.error(f"Error parsing telephony WebSocket: {e}")
            return
    
    # Extract config from body with defaults
    config = {
        "llm_context": body.get("llm_context", DEFAULT_CONFIG["llm_context"]),
        "session_duration": int(body.get("session_duration", DEFAULT_CONFIG["session_duration"])),
        "idle_warning_timeout": int(body.get("idle_warning_timeout", DEFAULT_CONFIG["idle_warning_timeout"])),
        "idle_disconnect_timeout": int(body.get("idle_disconnect_timeout", DEFAULT_CONFIG["idle_disconnect_timeout"]))
    }
    
    logger.info(f"Using config: session_duration={config['session_duration']}s")

    print("Successfully parsed Twilio data, continuing with bot setup...")
    
    serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
        account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
        auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
    )

    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(),
            serializer=serializer,
        ),
    )

    handle_sigint = runner_args.handle_sigint
    await run_bot(transport, handle_sigint, config)