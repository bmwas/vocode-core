"""
A small websockets server for testing twilio streaming capability - see the streaming/listening action.

"""

import asyncio
import websockets
import json
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def log_event(event_type, details=None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"{timestamp} - {event_type}"
    if details:
        log_message += f": {details}"
    print(log_message)
    logger.info(log_message)

async def handle_call(websocket, path):
    client_ip = websocket.remote_address[0]
    log_event("Connection Established", f"Client IP: {client_ip}, Path: {path}")

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                log_event("Message Received", f"Data: {data}")
                
                # You can add more specific logging based on the message content
                if 'event' in data:
                    log_event(f"Twilio Event", f"Event Type: {data['event']}")
                
            except json.JSONDecodeError:
                log_event("Error", f"Invalid JSON received: {message}")

    except websockets.exceptions.ConnectionClosedOK:
        log_event("Connection Closed", "Client disconnected normally")
    except websockets.exceptions.ConnectionClosedError as e:
        log_event("Connection Error", f"Abnormal closure: {e}")
    except Exception as e:
        log_event("Error", f"Unexpected error: {str(e)}")
    finally:
        log_event("Connection Terminated", f"Client IP: {client_ip}")

async def main():
    server = await websockets.serve(handle_call, "localhost", 8000)
    log_event("Server Started", "WebSocket server is running on ws://localhost:8000")
    
    try:
        await server.wait_closed()
    except KeyboardInterrupt:
        log_event("Server Shutdown", "Keyboard interrupt received")
    finally:
        server.close()
        await server.wait_closed()
        log_event("Server Shutdown", "WebSocket server has been shut down")

if __name__ == "__main__":
    asyncio.run(main())
