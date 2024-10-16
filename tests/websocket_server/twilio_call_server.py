import asyncio
import websockets
import json
import base64
import audioop
import pyaudio
from loguru import logger

# Audio parameters
SAMPLE_RATE = 8000  # Hz
CHANNELS = 1
SAMPLE_WIDTH = 2  # Bytes per sample after decoding (16-bit PCM)
FRAMES_PER_BUFFER = 512  # Reduced from 1024 to decrease latency
SCALE_FACTOR = 0.5  # Scaling factor to prevent clipping when mixing

# Initialize PyAudio
p = pyaudio.PyAudio()

stream = p.open(format=p.get_format_from_width(SAMPLE_WIDTH),
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                output=True,
                frames_per_buffer=FRAMES_PER_BUFFER)

# Buffers to hold inbound and outbound audio data
inbound_buffer = b''
outbound_buffer = b''

async def handle_connection(websocket, path):
    logger.info("Client connected")
    global inbound_buffer, outbound_buffer
    try:
        async for message in websocket:
            data = json.loads(message)

            if data.get('event') == 'media':
                media = data.get('media', {})
                payload = media.get('payload', '')
                track = media.get('track', '')

                if not payload:
                    continue  # Skip if payload is empty

                # Decode base64 payload
                try:
                    mulaw_audio = base64.b64decode(payload)
                except base64.binascii.Error as e:
                    logger.error(f"Base64 decoding error: {e}")
                    continue

                # Convert mulaw to linear PCM (16-bit signed integers)
                try:
                    pcm_audio = audioop.ulaw2lin(mulaw_audio, SAMPLE_WIDTH)
                except audioop.error as e:
                    logger.error(f"Audio decoding error: {e}")
                    continue

                # Append to the appropriate buffer
                if track == 'inbound':
                    inbound_buffer += pcm_audio
                elif track == 'outbound':
                    outbound_buffer += pcm_audio

                # Determine the minimum length available in both buffers
                min_length = min(len(inbound_buffer), len(outbound_buffer))

                if min_length >= FRAMES_PER_BUFFER * SAMPLE_WIDTH:
                    # Extract chunks of FRAMES_PER_BUFFER
                    chunk_size = FRAMES_PER_BUFFER * SAMPLE_WIDTH
                    inbound_chunk = inbound_buffer[:chunk_size]
                    outbound_chunk = outbound_buffer[:chunk_size]

                    # Remove the used data from the buffers
                    inbound_buffer = inbound_buffer[chunk_size:]
                    outbound_buffer = outbound_buffer[chunk_size:]

                    # Scale each chunk to prevent clipping
                    inbound_scaled = audioop.mul(inbound_chunk, SAMPLE_WIDTH, SCALE_FACTOR)
                    outbound_scaled = audioop.mul(outbound_chunk, SAMPLE_WIDTH, SCALE_FACTOR)

                    # Mix the two chunks
                    try:
                        mixed_chunk = audioop.add(inbound_scaled, outbound_scaled, SAMPLE_WIDTH)
                    except audioop.error as e:
                        logger.error(f"Audio mixing error: {e}")
                        continue

                    # Write mixed audio data to the stream
                    try:
                        stream.write(mixed_chunk)
                    except Exception as e:
                        logger.error(f"Audio playback error: {e}")
                else:
                    # If not enough data to fill the buffer, skip to prevent lag
                    continue

            elif data.get('event') == 'start':
                logger.info("Stream started")
            elif data.get('event') == 'stop':
                logger.info("Stream stopped")
                break
    except websockets.exceptions.ConnectionClosed:
        logger.info("Client disconnected")
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()

async def main():
    server = await websockets.serve(handle_connection, '0.0.0.0', 8080)
    logger.info("WebSocket server started on ws://0.0.0.0:8080")
    await server.wait_closed()

if __name__ == '__main__':
    asyncio.run(main())
