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
        # Production - use BASE_URL and force HTTPS/WSS
        if base_url.startswith("http://"):
            websocket_url = base_url.replace("http://", "wss://") + "/ws"
        else:
            websocket_url = base_url.replace("https://", "wss://") + "/ws"
    else:
        # Local - use ngrok URL
        ngrok_url = os.getenv("NGROK_URL")
        if ngrok_url:
            websocket_url = ngrok_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        else:
            # Force WSS even for IP
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

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "config_method": "url_encoded"
    }

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

        # Check if we're in production
        base_url = os.getenv("BASE_URL")
        if base_url:
            # Production
            twiml_url = f"{base_url}/twiml"
        else:
            # Local
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
        print(f"WebSocket headers: {dict(websocket.headers)}")
        print(f"WebSocket query params: {dict(websocket.query_params)}")
        
        # Wait a moment to see if we receive any initial data
        import asyncio
        print("Waiting for initial WebSocket data...")
        
        try:
            # Try to receive the first message with a timeout
            message = await asyncio.wait_for(websocket.receive(), timeout=5.0)
            print(f"Received first WebSocket message: {message}")
        except asyncio.TimeoutError:
            print("No initial message received within 5 seconds")
        except Exception as e:
            print(f"Error receiving initial message: {e}")

        from bot import bot
        from pipecat.runner.types import WebSocketRunnerArguments

        runner_args = WebSocketRunnerArguments(websocket=websocket)
        runner_args.handle_sigint = False

        print("About to call bot() function...")
        await bot(runner_args)

    except Exception as e:
        print(f"Error in WebSocket endpoint: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
    finally:
        print("WebSocket connection handler completed")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "800"))
    print(f"Starting Twilio outbound chatbot server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)