import asyncio
import websockets
import json
import logging
from datetime import datetime
import base64
import numpy as np
import sounddevice as sd
import audioop  # Added for μ-law decoding

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
    
    # Set up audio stream for stereo output
    sample_rate = 8000  # μ-law streams typically use 8000 Hz
    channels = 2        # Stereo audio (left and right channels)
    dtype = 'int16'     # PCM 16-bit
    try:
        stream = sd.OutputStream(samplerate=sample_rate, channels=channels, dtype=dtype)
        stream.start()
        log_event("Audio Stream Started", "Audio playback stream has been started")
    except Exception as e:
        log_event("Error", f"Could not open audio stream: {str(e)}")
        return
    
    # Initialize buffers for inbound and outbound audio
    inbound_buffer = []
    outbound_buffer = []
    
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                
                if 'event' in data and data['event'] == 'media':
                    # Extract and decode the audio payload
                    payload = data['media']['payload']
                    audio_data = base64.b64decode(payload)
                    
                    # Decode μ-law to PCM
                    try:
                        decoded_pcm = audioop.ulaw2lin(audio_data, 2)  # 2 bytes per sample for int16
                    except audioop.error as e:
                        log_event("Decoding Error", f"μ-law decoding failed: {str(e)}")
                        continue
                    
                    # Convert to numpy array
                    audio_array = np.frombuffer(decoded_pcm, dtype=np.int16)
                    
                    # Identify the track (inbound or outbound)
                    track = data['media'].get('track', 'unknown')
                    if track == 'inbound':
                        # Append to the inbound buffer
                        inbound_buffer.append(audio_array)
                    elif track == 'outbound':
                        # Append to the outbound buffer
                        outbound_buffer.append(audio_array)
                    else:
                        log_event("Unknown Track", f"Track: {track}")
                        continue

                    # Synchronize and play audio
                    while inbound_buffer and outbound_buffer:
                        # Get the next chunks from both buffers
                        inbound_chunk = inbound_buffer.pop(0)
                        outbound_chunk = outbound_buffer.pop(0)
                        
                        # Make sure both chunks are the same length
                        min_length = min(len(inbound_chunk), len(outbound_chunk))
                        inbound_chunk = inbound_chunk[:min_length]
                        outbound_chunk = outbound_chunk[:min_length]
                        
                        # Stack the chunks to form stereo audio
                        stereo_audio = np.column_stack((inbound_chunk, outbound_chunk))
                        # Write to the audio stream
                        stream.write(stereo_audio)
                    
                    # Handle any remaining chunks in the buffers
                    # If one buffer has more data than the other, pad the missing channel with zeros
                    if inbound_buffer and not outbound_buffer:
                        while inbound_buffer:
                            inbound_chunk = inbound_buffer.pop(0)
                            zero_chunk = np.zeros(len(inbound_chunk), dtype=np.int16)
                            stereo_audio = np.column_stack((inbound_chunk, zero_chunk))
                            stream.write(stereo_audio)
                    elif outbound_buffer and not inbound_buffer:
                        while outbound_buffer:
                            outbound_chunk = outbound_buffer.pop(0)
                            zero_chunk = np.zeros(len(outbound_chunk), dtype=np.int16)
                            stereo_audio = np.column_stack((zero_chunk, outbound_chunk))
                            stream.write(stereo_audio)
                elif 'event' in data:
                    log_event(f"Twilio Event", f"Event Type: {data['event']}")
                else:
                    log_event("Unknown Message", f"Data: {data}")
            except json.JSONDecodeError:
                log_event("Error", f"Invalid JSON received: {message}")
            except Exception as e:
                log_event("Error", f"Error processing message: {str(e)}")
    except websockets.exceptions.ConnectionClosedOK:
        log_event("Connection Closed", "Client disconnected normally")
    except websockets.exceptions.ConnectionClosedError as e:
        log_event("Connection Error", f"Abnormal closure: {e}")
    except Exception as e:
        log_event("Error", f"Unexpected error: {str(e)}")
    finally:
        # Stop and close the audio stream
        stream.stop()
        stream.close()
        log_event("Audio Stream Stopped", "Audio playback stream has been stopped")
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

