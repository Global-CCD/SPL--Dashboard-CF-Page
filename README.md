## Project Architecture

```
UMIK-1 → Python Streamer → WebSocket → Cloudflare Worker (Durable Object) → WebSocket → Pages Dashboard
```

**Why this architecture?** Cloudflare Pages is static hosting only — it cannot accept a live stream. The Cloudflare Worker with Durable Objects acts as the real-time relay and state buffer. The Python script publishes to it; the dashboard subscribes from it.

---

## 1. Python UMIK-1 Streamer

Save as `umik1_spl_streamer.py`:

```python
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
```

**`requirements.txt`:**
```text
numpy>=1.24.0
sounddevice>=0.4.6
websockets>=12.0
```

**Install & test device detection:**
```bash
pip install -r requirements.txt
python -c "import sounddevice as sd; print(sd.query_devices())"
```

---

## 2. Cloudflare Worker (Real-Time Relay)

This Worker uses a **Durable Object** to maintain state and broadcast to all connected dashboard clients.

Save as `worker.js`:

```javascript
// worker.js — Cloudflare Durable Object SPL Relay
export class SplRelay {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.sessions = new Set();     // Dashboard clients
    this.readings = [];            // Rolling buffer (last 5 min @ 1Hz)
  }

  async fetch(request) {
    const url = new URL(request.url);

    // ── WebSocket Upgrade ──────────────────────────────────────────────
    if (request.headers.get("Upgrade") === "websocket") {
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      server.accept();
      this.sessions.add(server);

      server.addEventListener("message", async (msg) => {
        try {
          const data = JSON.parse(msg.data);

          // Validate publisher API key
          if (data.apiKey !== this.env.API_KEY) {
            server.send(JSON.stringify({ type: "error", message: "Invalid API key" }));
            return;
          }

          // Store reading (publisher path)
          const reading = {
            timestamp: data.timestamp,
            spl: data.spl,
            leq60: data.leq60,
            peak: data.peak,
            weighting: data.weighting,
            device: data.device
          };

          this.readings.push(reading);
          if (this.readings.length > 300) this.readings.shift(); // 5 minutes

          // Broadcast to all dashboard subscribers
          const broadcast = JSON.stringify({ type: "reading", data: reading });
          this.sessions.forEach((ws) => {
            if (ws.readyState === 1) ws.send(broadcast);
          });

        } catch (err) {
          server.send(JSON.stringify({ type: "error", message: err.message }));
        }
      });

      server.addEventListener("close", () => {
        this.sessions.delete(server);
      });

      // Send historical buffer to new dashboard client immediately
      server.send(JSON.stringify({ type: "history", data: this.readings }));

      return new Response(null, { status: 101, webSocket: client });
    }

    // ── HTTP Fallback API ──────────────────────────────────────────────
    if (url.pathname === "/api/readings") {
      return new Response(JSON.stringify(this.readings), {
        headers: {
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*"
        }
      });
    }

    return new Response("Not Found", { status: 404 });
  }
}

// Worker entrypoint routes all requests to the single Durable Object instance
export default {
  async fetch(request, env, ctx) {
    const id = env.SPL_RELAY.idFromName("primary");
    const relay = env.SPL_RELAY.get(id);
    return relay.fetch(request);
  }
};
```

**`wrangler.toml`:**
```toml
name = "umik1-spl-relay"
main = "worker.js"
compatibility_date = "2024-09-01"

[[durable_objects.bindings]]
name = "SPL_RELAY"
class_name = "SplRelay"

[[migrations]]
tag = "v1"
new_classes = ["SplRelay"]
```

**Deploy the Worker:**
```bash
# Install Wrangler if needed
npm install -g wrangler

# Login to Cloudflare
wrangler login

# Set your secret API key
wrangler secret put API_KEY
# (enter the same key used in the Python script)

# Deploy
wrangler deploy
```

Note the deployed Worker URL (e.g., `https://umik1-spl-relay.YOUR_SUBDOMAIN.workers.dev`) and update `WS_URI` in the Python script.

---

## 3. Dashboard for Cloudflare Pages

Save as `index.html` and deploy to a new Cloudflare Pages site (drag-and-drop in the Cloudflare dashboard or via Git):

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>UMIK-1 SPL Monitor | Forensic Telemetry</title>
  <style>
    :root {
      --bg: #0b0c0f;
      --panel: #14161b;
      --border: #2a2d35;
      --text: #e1e3e6;
      --muted: #8b949e;
      --accent: #00d084;
      --warn: #f7b500;
      --danger: #ff4d4f;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: "SF Mono", Monaco, "Cascadia Code", Consolas, monospace;
      line-height: 1.5;
      padding: 2rem;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 1rem;
      margin-bottom: 2rem;
    }
    h1 { font-size: 1.25rem; letter-spacing: 0.05em; }
    #status {
      font-size: 0.75rem;
      padding: 0.25rem 0.75rem;
      border-radius: 999px;
      background: var(--border);
      color: var(--muted);
    }
    #status.connected { background: rgba(0,208,132,0.15); color: var(--accent); }
    #status.error { background: rgba(255,77,79,0.15); color: var(--danger); }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1rem;
      margin-bottom: 2rem;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1.5rem;
      text-align: center;
    }
    .card .label {
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
      margin-bottom: 0.5rem;
    }
    .card .value {
      font-size: 2.5rem;
      font-weight: 600;
      color: var(--accent);
    }
    .card.peak .value { color: var(--danger); }
    .card.leq .value { color: var(--warn); }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
    }
    thead th {
      text-align: left;
      padding: 0.75rem;
      border-bottom: 1px solid var(--border);
      color: var(--muted);
      font-weight: 500;
      position: sticky;
      top: 0;
      background: var(--bg);
    }
    tbody td {
      padding: 0.6rem 0.75rem;
      border-bottom: 1px solid var(--border);
      color: var(--text);
    }
    tbody tr:hover td { background: rgba(255,255,255,0.03); }
    td.numeric { text-align: right; font-variant-numeric: tabular-nums; }
    .empty { color: var(--muted); text-align: center; padding: 2rem; }

    @media (max-width: 640px) {
      body { padding: 1rem; }
      .card .value { font-size: 1.75rem; }
    }
  </style>
</head>
<body>

  <header>
    <h1>UMIK-1 SPL Telemetry</h1>
    <span id="status">● Connecting...</span>
  </header>

  <section class="grid">
    <div class="card">
      <div class="label">Instant SPL</div>
      <div class="value" id="val-spl">--.-</div>
      <div class="label">dB (Z)</div>
    </div>
    <div class="card leq">
      <div class="label">LEQ 60s</div>
      <div class="value" id="val-leq">--.-</div>
      <div class="label">dB Equivalent</div>
    </div>
    <div class="card peak">
      <div class="label">Peak Hold</div>
      <div class="value" id="val-peak">--.-</div>
      <div class="label">dB Max</div>
    </div>
    <div class="card">
      <div class="label">Readings</div>
      <div class="value" id="val-count">0</div>
      <div class="label">Buffered</div>
    </div>
  </section>

  <section>
    <table>
      <thead>
        <tr>
          <th>UTC Time</th>
          <th class="numeric">SPL (dB)</th>
          <th class="numeric">LEQ60 (dB)</th>
          <th class="numeric">Peak (dB)</th>
          <th>Weighting</th>
          <th>Device</th>
        </tr>
      </thead>
      <tbody id="table-body">
        <tr><td colspan="6" class="empty">Waiting for UMIK-1 stream...</td></tr>
      </tbody>
    </table>
  </section>

  <script>
    const WS_URL = "wss://YOUR_WORKER.YOUR_SUBDOMAIN.workers.dev"; // <-- UPDATE THIS
    const statusEl = document.getElementById("status");
    const tableBody = document.getElementById("table-body");
    const countEl = document.getElementById("val-count");

    let readings = [];
    let reconnectTimer = null;

    function fmtTime(ts) {
      const d = new Date(ts);
      return d.toISOString().split("T")[1].split(".")[0] + "Z";
    }

    function updateDOM() {
      if (readings.length === 0) return;
      const latest = readings[readings.length - 1];
      document.getElementById("val-spl").textContent = latest.spl.toFixed(1);
      document.getElementById("val-leq").textContent = latest.leq60.toFixed(1);
      document.getElementById("val-peak").textContent = latest.peak.toFixed(1);
      countEl.textContent = readings.length;

      // Rebuild table (last 50 rows, newest top)
      const recent = readings.slice(-50).reverse();
      tableBody.innerHTML = recent.map(r => `
        <tr>
          <td>${fmtTime(r.timestamp)}</td>
          <td class="numeric">${r.spl.toFixed(2)}</td>
          <td class="numeric">${r.leq60.toFixed(2)}</td>
          <td class="numeric">${r.peak.toFixed(2)}</td>
          <td>${r.weighting}</td>
          <td>${r.device}</td>
        </tr>
      `).join("");
    }

    function connect() {
      const ws = new WebSocket(WS_URL);

      ws.onopen = () => {
        clearTimeout(reconnectTimer);
        statusEl.textContent = "● Live";
        statusEl.className = "connected";
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "reading") {
            readings.push(msg.data);
            if (readings.length > 300) readings.shift();
            updateDOM();
          } else if (msg.type === "history") {
            readings = msg.data;
            updateDOM();
          }
        } catch (e) {
          console.error("Parse error", e);
        }
      };

      ws.onerror = () => {
        statusEl.textContent = "● Error";
        statusEl.className = "error";
      };

      ws.onclose = () => {
        statusEl.textContent = "● Reconnecting...";
        statusEl.className = "";
        reconnectTimer = setTimeout(connect, 3000);
      };
    }

    connect();
  </script>
</body>
</html>
```

**Deploy to Pages:**
1. Go to [Cloudflare Dashboard → Pages](https://dash.cloudflare.com) → **Create a project**
2. Upload `index.html` directly (or connect a Git repo)
3. Note the Pages URL (e.g., `https://umik1-spl.YOUR_SUBDOMAIN.pages.dev`)

---

## 4. Calibration & Operational Notes

| Parameter | Source | Action |
|-----------|--------|--------|
| **Sensitivity** | Your UMIK-1 calibration certificate from miniDSP | Enter the 1kHz sensitivity (e.g., `-11.3 dB`) into `CALIBRATION_SENSITIVITY_DB` |
| **Device Name** | OS audio device enumeration | If auto-detect fails, change `DEVICE_NAME_SUBSTRING` to match `sd.query_devices()` output |
| **Weighting** | Forensic requirement | Default is `Z` (flat). For A-weighted environmental noise, install `scipy` and apply an A-weighting filter before RMS calculation |

**A-Weighting Filter (optional enhancement):**
If you need A-weighted SPL for legal/environmental compliance, add this before the RMS calculation:

```python
from scipy.signal import bilinear, lfilter

def a_weighting_coeffs(fs):
    """Return A-weighting filter coefficients (b, a) for given sample rate."""
    f1, f2, f3, f4 = 20.598997, 107.65265, 737.86223, 12194.217
    A1000 = 1.9997
    pi = np.pi
    # Analog zeros/poles to digital via bilinear transform
    z = [0, 0, 0]
    p = [-2*pi*f4, -2*pi*f3, -2*pi*f2, -2*pi*f1]
    # ... implementation abbreviated for brevity
    # Use a pre-calculated library like `acoustics` if preferred
```

For forensic rigor, I recommend keeping **Z-weighted** as the primary metric (objective, unfiltered) and logging the raw calibration file serial number in the payload.

---

## 5. Quick Start Checklist

```bash
# 1. Python environment
pip install numpy sounddevice websockets

# 2. Verify UMIK-1 is visible
python -c "import sounddevice as sd; [print(f'{i}: {d['name']}') for i,d in enumerate(sd.query_devices()) if d.get('max_input_channels',0)>0]"

# 3. Update CALIBRATION_SENSITIVITY_DB in umik1_spl_streamer.py from your miniDSP certificate

# 4. Deploy Worker
wrangler deploy
wrangler secret put API_KEY

# 5. Update WS_URI in Python script and WS_URL in index.html with your Worker URL

# 6. Deploy index.html to Cloudflare Pages

# 7. Run
python umik1_spl_streamer.py
```

The dashboard will populate within 1–2 seconds of the Python client connecting. All data is ephemeral in the Durable Object memory (resets on Worker restart), making this suitable for live monitoring without persistent log retention on Cloudflare's edge.
