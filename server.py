"""server.py - Local Development Version"""

import os
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

load_dotenv(override=True)

# In-memory store for body data by call SID
call_body_data = {}

def generate_twiml(host: str, body_data: dict = None) -> str:
    """Generate TwiML response with WebSocket streaming using Twilio SDK."""
    
    # Check if we're in production
    base_url = os.getenv("BASE_URL")
    if base_url:
        # Production - use BASE_URL
        websocket_url = base_url.replace("https://", "wss://") + "/ws"
    else:
        # Local - use ngrok URL
        ngrok_url = os.getenv("NGROK_URL")
        if ngrok_url:
            websocket_url = ngrok_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        else:
            websocket_url = f"wss://{host}/ws"
    
    print(f"DEBUG - WebSocket URL: {websocket_url}")
    
    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=websocket_url)

    if body_data:
        for key, value in body_data.items():
            stream.parameter(name=key, value=value)

    connect.append(stream)
    response.append(connect)
    response.pause(length=20)
    
    return str(response)

def make_twilio_call(to_number: str, from_number: str, twiml_url: str):
    """Make an outbound call using Twilio's REST API."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        raise ValueError("Missing Twilio credentials")

    client = TwilioClient(account_sid, auth_token)
    call = client.calls.create(to=to_number, from_=from_number, url=twiml_url, method="POST")
    return {"sid": call.sid}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def health_check():
    return {"status": "healthy", "message": "Twilio phone bot is running"}

@app.post("/start")
async def initiate_outbound_call(request: Request) -> JSONResponse:
    """Handle outbound call request and initiate call via Twilio."""
    print("Received outbound call request")

    try:
        data = await request.json()
        if not data.get("phone_number"):
            raise HTTPException(status_code=400, detail="Missing 'phone_number' in the request body")

        phone_number = str(data["phone_number"])
        body_data = data.get("body", {})
        
        # Extract configuration parameters and add them to body_data
        config_params = {
            "llm_context": data.get("llm_context", "You are a friendly assistant making an outbound phone call. Your responses will be read aloud, so keep them concise and conversational. Avoid special characters or formatting. Begin by politely greeting the person and explaining why you're calling."),
            "session_duration": data.get("session_duration", 180),
            "idle_warning_timeout": data.get("idle_warning_timeout", 8),
            "idle_disconnect_timeout": data.get("idle_disconnect_timeout", 30)
        }
        
        # Merge config into body_data
        body_data.update(config_params)
        print(f"Body data with config: {list(body_data.keys())}")

        # Rest of your existing code...
        base_url = os.getenv("BASE_URL")
        if base_url:
            twiml_url = f"{base_url}/twiml"
        else:
            ngrok_url = os.getenv("NGROK_URL")
            if not ngrok_url:
                raise HTTPException(status_code=500, detail="NGROK_URL not set")
            twiml_url = f"{ngrok_url}/twiml"

        call_result = make_twilio_call(
            to_number=phone_number,
            from_number=os.getenv("TWILIO_PHONE_NUMBER"),
            twiml_url=twiml_url,
        )
        call_sid = call_result["sid"]

        if body_data:
            call_body_data[call_sid] = body_data

        return JSONResponse(
            {"call_sid": call_sid, "status": "call_initiated", "phone_number": phone_number}
        )

    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/twiml")
async def get_twiml(request: Request) -> HTMLResponse:
    """Return TwiML instructions for connecting call to WebSocket."""
    print("Serving TwiML for outbound call")

    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")

    body_data = call_body_data.get(call_sid, {})

    if call_sid and call_sid in call_body_data:
        del call_body_data[call_sid]

    try:
        host = request.headers.get("host")
        if not host:
            raise HTTPException(status_code=400, detail="Unable to determine server host")

        twiml_content = generate_twiml(host, body_data)
        return HTMLResponse(content=twiml_content, media_type="application/xml")

    except Exception as e:
        print(f"Error generating TwiML: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate TwiML: {str(e)}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket connection from Twilio Media Streams."""
    try:
        await websocket.accept()
        print("WebSocket connection accepted for outbound call")
        
        # Wait for the actual start message with call SID and custom parameters
        import asyncio
        import json
        print("Waiting for Twilio start message...")
        
        call_sid = None
        stream_sid = None
        custom_parameters = {}
        max_attempts = 10
        attempt = 0
        start_message_data = None
        
        while attempt < max_attempts and call_sid is None:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=2.0)
                print(f"Received WebSocket message #{attempt + 1}: {message.get('text', '')[:200]}...")
                
                if message.get("type") == "websocket.receive" and "text" in message:
                    try:
                        data = json.loads(message["text"])
                        
                        # Look for the start event which contains call SID and parameters
                        if data.get("event") == "start":
                            start_data = data.get("start", {})
                            call_sid = start_data.get("callSid")
                            stream_sid = start_data.get("streamSid")
                            custom_parameters = start_data.get("customParameters", {})
                            start_message_data = data  # Store the entire start message
                            
                            print(f"Found call SID: {call_sid}")
                            print(f"Found stream SID: {stream_sid}")
                            print(f"Found custom parameters: {list(custom_parameters.keys())}")
                            break
                        elif data.get("event") == "connected":
                            print("Received connected event, waiting for start event...")
                            
                        attempt += 1
                    except json.JSONDecodeError as e:
                        print(f"Failed to parse JSON: {e}")
                        attempt += 1
                        
            except asyncio.TimeoutError:
                print(f"Timeout on attempt {attempt + 1}, retrying...")
                attempt += 1
            except Exception as e:
                print(f"Error receiving message on attempt {attempt + 1}: {e}")
                attempt += 1

        from bot import bot
        from pipecat.runner.types import WebSocketRunnerArguments

        # Create a new WebSocket that includes the start message we already consumed
        class ReplayWebSocket:
            def __init__(self, original_websocket, start_message):
                self.original_websocket = original_websocket
                self.start_message = start_message
                self.start_sent = False
                
            async def accept(self):
                # Already accepted
                pass
                
            async def receive(self):
                if not self.start_sent and self.start_message:
                    self.start_sent = True
                    return {"type": "websocket.receive", "text": json.dumps(self.start_message)}
                return await self.original_websocket.receive()
                
            async def send_text(self, data):
                return await self.original_websocket.send_text(data)
                
            async def close(self):
                return await self.original_websocket.close()
                
            def __getattr__(self, name):
                return getattr(self.original_websocket, name)

        # Create replay websocket that will replay the start message
        replay_websocket = ReplayWebSocket(websocket, start_message_data)
        
        runner_args = WebSocketRunnerArguments(websocket=replay_websocket)
        runner_args.handle_sigint = False
        
        # Set custom parameters as body data
        runner_args.body = custom_parameters
        runner_args.call_data = {
            "stream_id": stream_sid,
            "call_id": call_sid
        }
        print(f"Setting runner_args.body with custom parameters: {list(custom_parameters.keys())}")

        print("About to call bot() function...")
        await bot(runner_args)

    except Exception as e:
        print(f"Error in WebSocket endpoint: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
    finally:
        print("WebSocket connection handler completed")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"Starting Twilio outbound chatbot server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)