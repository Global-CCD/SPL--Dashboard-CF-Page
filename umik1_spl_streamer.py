#!/usr/bin/env python3
"""
UMIK-1 Real-Time SPL Streamer
Publishes calibrated Sound Pressure Level to Cloudflare Worker WebSocket relay.
"""

import asyncio
import json
import sys
import time
from collections import deque

import numpy as np
import sounddevice as sd
import websockets


# ── CONFIGURATION ───────────────────────────────────────────────────────────
SAMPLE_RATE = 48000          # UMIK-1 native
BLOCK_DURATION = 1.0         # Seconds per SPL measurement
DEVICE_NAME_SUBSTRING = "UMIK"  # Auto-detect; change to "miniDSP" if needed
CALIBRATION_SENSITIVITY_DB = -11.0  # Your UMIK-1's 1kHz sensitivity (dBFS @ 94 dB SPL)
                                    # Found on your miniDSP calibration certificate
WS_URI = "wss://YOUR_WORKER.YOUR_SUBDOMAIN.workers.dev"
API_KEY = "YOUR_SECRET_API_KEY"     # Set in Worker secrets
WEIGHTING = "Z"                   # "Z" = flat, "A" = A-weighted (requires scipy)
# ─────────────────────────────────────────────────────────────────────────────


def find_umik1_device():
    """Auto-detect UMIK-1 input device index."""
    devices = sd.query_devices()
    for idx, dev in enumerate(devices):
        name = dev.get("name", "")
        if DEVICE_NAME_SUBSTRING in name and dev.get("max_input_channels", 0) > 0:
            print(f"[+] Found UMIK-1 at index {idx}: {name}")
            return idx
    print("[-] UMIK-1 not found. Available input devices:")
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) > 0:
            print(f"    {idx}: {dev['name']}")
    raise RuntimeError("UMIK-1 not detected. Check connection or adjust DEVICE_NAME_SUBSTRING.")


def calculate_spl(block: np.ndarray, sensitivity_db: float) -> float:
    """
    Convert normalized float32 audio block to SPL (dB re 20 µPa).
    sensitivity_db: the UMIK-1's calibration value (e.g., -11.0 dBFS = 94 dB SPL).
    """
    # block is float32 [-1.0, 1.0] where 1.0 = 0 dBFS
    rms = np.sqrt(np.mean(block ** 2))
    # Prevent log(0)
    rms = max(rms, 1e-10)
    dbfs = 20.0 * np.log10(rms)
    # 94 dB SPL corresponds to sensitivity_db dBFS
    spl = 94.0 + dbfs - sensitivity_db
    return float(spl)


class LeqCalculator:
    """Running equivalent continuous sound level (LEQ) over a sliding window."""
    def __init__(self, window_seconds: int = 60):
        self.window = window_seconds
        # Store (timestamp, pressure_squared)
        self.buffer = deque()

    def add(self, spl: float):
        p_ref = 20e-6
        p_sq = (p_ref ** 2) * 10 ** (spl / 10.0)
        now = time.time()
        self.buffer.append((now, p_sq))
        # Evict old samples
        cutoff = now - self.window
        while self.buffer and self.buffer[0][0] < cutoff:
            self.buffer.popleft()

    def current_leq(self) -> float:
        if not self.buffer:
            return 0.0
        p_ref = 20e-6
        avg_p_sq = np.mean([p for _, p in self.buffer])
        leq = 10.0 * np.log10(avg_p_sq / (p_ref ** 2))
        return float(leq)


class SplStreamer:
    def __init__(self):
        self.device_idx = find_umik1_device()
        self.leq = LeqCalculator(window_seconds=60)
        self.peak_hold = 0.0
        self.ws = None
        self.queue = asyncio.Queue()
        self._running = True

    def audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice in a separate thread every BLOCK_DURATION."""
        if status:
            print(f"[audio status] {status}", file=sys.stderr)

        # UMIK-1 is mono; take first channel
        mono = indata[:, 0]
        spl = calculate_spl(mono, CALIBRATION_SENSITIVITY_DB)

        self.leq.add(spl)
        if spl > self.peak_hold:
            self.peak_hold = spl

        # Push to async queue safely from callback thread
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(
            self.queue.put({
                "timestamp": int(time.time() * 1000),
                "spl": round(spl, 2),
                "leq60": round(self.leq.current_leq(), 2),
                "peak": round(self.peak_hold, 2),
                "weighting": WEIGHTING,
                "device": "UMIK-1"
            }),
            loop
        )

    async def websocket_handler(self):
        """Maintain WebSocket connection to Cloudflare Worker with auto-reconnect."""
        while self._running:
            try:
                print(f"[+] Connecting to {WS_URI} ...")
                async with websockets.connect(WS_URI, ping_interval=20, ping_timeout=10) as ws:
                    self.ws = ws
                    print("[+] WebSocket connected. Streaming SPL...")
                    while self._running:
                        reading = await self.queue.get()
                        payload = {
                            "apiKey": API_KEY,
                            **reading
                        }
                        await ws.send(json.dumps(payload))
                        # Console telemetry
                        print(f"SPL: {reading['spl']:.1f} dB | "
                              f"LEQ60: {reading['leq60']:.1f} dB | "
                              f"Peak: {reading['peak']:.1f} dB")
            except (websockets.ConnectionClosed, websockets.InvalidStatusCode, OSError) as e:
                print(f"[!] WebSocket error: {e}. Reconnecting in 5s...")
                self.ws = None
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[!] Unexpected error: {e}")
                await asyncio.sleep(5)

    async def run(self):
        blocksize = int(SAMPLE_RATE * BLOCK_DURATION)
        # sounddevice stream runs in a background thread
        stream = sd.InputStream(
            device=self.device_idx,
            channels=1,
            samplerate=SAMPLE_RATE,
            blocksize=blocksize,
            dtype="float32",
            callback=self.audio_callback
        )

        with stream:
            try:
                await self.websocket_handler()
            except asyncio.CancelledError:
                self._running = False
                print("\n[+] Shutting down streamer.")


if __name__ == "__main__":
    streamer = SplStreamer()
    try:
        asyncio.run(streamer.run())
    except KeyboardInterrupt:
        print("\n[+] Interrupted by user.")
        sys.exit(0)
