Here is a complete, production-ready `README.md` and an accompanying GitHub Actions release workflow. You can drop these directly into your repository root and `.github/workflows/`.

---

## `README.md`

```markdown
# UMIK-1 SPL Telemetry Streamer

Real-time Sound Pressure Level (SPL) acquisition from the miniDSP UMIK-1 measurement microphone, streamed via WebSocket to a Cloudflare Worker relay and visualised on a Cloudflare Pages dashboard.

**Primary use case:** Forensic acoustic monitoring, calibrated SPL logging, and remote real-time telemetry.

---

## Architecture

```
┌─────────────┐     WebSocket      ┌─────────────────────┐     WebSocket      ┌──────────────┐
│  UMIK-1     │ ──────────────────>│ Cloudflare Worker   │ <─────────────────│  Dashboard   │
│  Python     │   SPL + LEQ + Peak │ (Durable Object Relay)│   Broadcast        │  (Pages)     │
│  Streamer   │                    │                     │                    │              │
└─────────────┘                    └─────────────────────┘                    └──────────────┘
```

---

## Hardware Requirements

| Component | Specification | Notes |
|-----------|---------------|-------|
| Microphone | miniDSP UMIK-1 | USB Audio Class 1.0, 48 kHz / 16-bit |
| Host | macOS, Linux, or Windows | Python 3.10+ required |
| Calibration File | `.txt` from miniDSP | Needed for absolute SPL accuracy |

---

## Repository Structure

```
.
├── umik1_spl_streamer.py      # Main acquisition & streamer script
├── requirements.txt           # Python dependencies
├── worker.js                  # Cloudflare Worker (Durable Object relay)
├── wrangler.toml              # Worker deployment config
├── dashboard/
│   └── index.html             # Cloudflare Pages static dashboard
├── .github/
│   └── workflows/
│       └── release.yml        # GitHub Actions: build & release
└── README.md                  # This file
```

---

## 1. Local Development Setup

### 1.1 Clone & Install

```bash
git clone https://github.com/YOUR_USERNAME/umik1-spl-streamer.git
cd umik1-spl-streamer

# Create isolated environment
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

### 1.2 Verify UMIK-1 Detection

```bash
python -c "import sounddevice as sd; [print(f'{i}: {d['name']}') for i,d in enumerate(sd.query_devices()) if d.get('max_input_channels',0)>0]"
```

You should see an entry containing `UMIK` or `miniDSP`. If not, check the USB connection and ensure no other application has exclusive control of the device.

### 1.3 Configure Calibration

Open `umik1_spl_streamer.py` and edit:

```python
CALIBRATION_SENSITIVITY_DB = -11.0   # <-- Replace with your certificate value
```

**Where to find this:** Your UMIK-1 ships with a unique calibration certificate (paper or PDF from miniDSP). Locate the **1 kHz sensitivity** value (e.g., `-11.3 dB`). This is the reference point mapping 0 dBFS → 94 dB SPL.

> **Forensic note:** Record the calibration serial number in your chain-of-custody documentation. The script logs `device: "UMIK-1"` but does not embed the serial by default; add it to the payload if required for evidentiary admissibility.

---

## 2. Cloudflare Infrastructure Deployment

You need two Cloudflare services: a **Worker** (WebSocket relay) and **Pages** (static dashboard).

### 2.1 Install Wrangler CLI

```bash
npm install -g wrangler
wrangler login
```

### 2.2 Deploy the Worker Relay

```bash
# From repo root
cd worker/   # or wherever you placed worker.js & wrangler.toml
wrangler deploy
```

You will receive a Worker URL:
```
https://umik1-spl-relay.YOUR_SUBDOMAIN.workers.dev
```

### 2.3 Set the API Secret

The Python streamer and Worker share a secret to prevent unauthorised publishes:

```bash
wrangler secret put API_KEY
# Enter a strong random string (e.g., 32+ chars)
```

Copy this same string into `umik1_spl_streamer.py`:

```python
WS_URI = "wss://umik1-spl-relay.YOUR_SUBDOMAIN.workers.dev"
API_KEY = "YOUR_SECRET_API_KEY"
```

### 2.4 Deploy the Dashboard to Pages

1. Go to [Cloudflare Dashboard → Pages](https://dash.cloudflare.com) → **Create a project**.
2. Choose **Upload assets** (direct upload) or connect this GitHub repo.
3. Set the build output directory to `dashboard/` (or root if `index.html` is at repo root).
4. Deploy.

Note the Pages URL (e.g., `https://umik1-spl.YOUR_SUBDOMAIN.pages.dev`).

### 2.5 Update Dashboard Endpoint

In `dashboard/index.html`, replace:

```javascript
const WS_URL = "wss://umik1-spl-relay.YOUR_SUBDOMAIN.workers.dev";
```

---

## 3. Running the Streamer

### 3.1 Start Acquisition

```bash
source .venv/bin/activate
python umik1_spl_streamer.py
```

Expected console output:

```
[+] Found UMIK-1 at index 2: UMIK-1 Gain: 0dB
[+] Connecting to wss://umik1-spl-relay....
[+] WebSocket connected. Streaming SPL...
SPL: 42.3 dB | LEQ60: 41.8 dB | Peak: 58.1 dB
SPL: 43.1 dB | LEQ60: 42.0 dB | Peak: 58.1 dB
...
```

### 3.2 View the Dashboard

Open your Pages URL in any modern browser. The dashboard auto-connects via WebSocket and renders:

- **Instant SPL** (1-second blocks)
- **LEQ 60s** (Equivalent continuous sound level, sliding window)
- **Peak Hold** (maximum since streamer start)
- **Telemetry table** (last 50 readings, UTC timestamped)

---

## 4. Configuration Reference

| Variable | File | Description |
|----------|------|-------------|
| `CALIBRATION_SENSITIVITY_DB` | `umik1_spl_streamer.py` | UMIK-1 1kHz sensitivity from certificate |
| `DEVICE_NAME_SUBSTRING` | `umik1_spl_streamer.py` | Auto-detect filter (`"UMIK"` or `"miniDSP"`) |
| `SAMPLE_RATE` | `umik1_spl_streamer.py` | Fixed at 48000 Hz (UMIK-1 native) |
| `BLOCK_DURATION` | `umik1_spl_streamer.py` | Integration period in seconds (default `1.0`) |
| `WEIGHTING` | `umik1_spl_streamer.py` | `"Z"` (flat) or `"A"` (requires `scipy`) |
| `WS_URI` | `umik1_spl_streamer.py` | WebSocket endpoint of Cloudflare Worker |
| `API_KEY` | `umik1_spl_streamer.py` | Must match `wrangler secret put` value |
| `WS_URL` | `dashboard/index.html` | Same WebSocket endpoint for browser clients |

---

## 5. GitHub Releases & Automated Builds

This repository includes a GitHub Actions workflow that:

1. Runs on every **tag push** (`v*.*.*`)
2. Builds cross-platform standalone executables via **PyInstaller**
3. Creates a **GitHub Release** with attached binaries and auto-generated notes

### 5.1 Trigger a Release

```bash
# Ensure all changes are committed and pushed
git add .
git commit -m "feat: v1.0.0 calibrated SPL streamer"
git tag v1.0.0
git push origin v1.0.0
```

The workflow will build:
- `umik1-spl-streamer-macos` (universal2)
- `umik1-spl-streamer-linux` (x86_64)
- `umik1-spl-streamer-windows.exe` (x86_64)

### 5.2 Download & Run (End User)

Users without Python can download the binary from the GitHub Release page:

```bash
# macOS example
chmod +x umik1-spl-streamer-macos
./umik1-spl-streamer-macos
```

> **Note:** Binaries are unsigned. macOS users may need to right-click → Open, or remove quarantine: `xattr -d com.apple.quarantine umik1-spl-streamer-macos`.

---

## 6. Troubleshooting

| Symptom | Likely Cause | Resolution |
|---------|--------------|------------|
| `UMIK-1 not detected` | Device in use by another app | Close REW, Audacity, or DAW; re-plug USB |
| `WebSocket error` | Worker URL or API key mismatch | Verify `WS_URI` and `wrangler secret` values match exactly |
| `SPL values unrealistic` | Wrong calibration sensitivity | Double-check certificate value; sign matters (`-11.0` not `11.0`) |
| Dashboard blank / "Waiting..." | Pages URL not pointing to live Worker | Check browser DevTools → Network → WS for connection errors |
| High CPU usage | `BLOCK_DURATION` too small | Increase to `1.0` or `2.0` seconds |
| No audio on Linux | PulseAudio / ALSA permissions | Add user to `audio` group; `sudo usermod -a -G audio $USER` |

---

## 7. Forensic & Compliance Notes

- **Weighting:** The default `Z-weighting` (flat, 10 Hz – 20 kHz) is unfiltered and objective. For environmental noise ordinances, implement A-weighting via `scipy.signal` before the RMS calculation.
- **Timestamps:** All payloads use JavaScript epoch milliseconds (`int(time.time() * 1000)`). The dashboard renders UTC.
- **Chain of Custody:** The Durable Object buffer is memory-only and resets on Worker restart. For permanent logging, extend the Worker to write to R2, D1, or forward to an external SIEM.
- **Calibration Drift:** UMIK-1 capsules are electret and stable, but annual re-calibration against a pistonphone (e.g., B&K 4220) is recommended for legal-grade evidence.

---

## License

MIT — See `LICENSE`.
```

---

## `.github/workflows/release.yml`

Create the directory structure `.github/workflows/` in your repo and save this as `release.yml`:

```yaml
name: Build & Release

on:
  push:
    tags:
      - 'v*.*.*'

permissions:
  contents: write

jobs:
  build:
    strategy:
      matrix:
        include:
          - os: macos-latest
            target: macos
            asset_name: umik1-spl-streamer-macos
          - os: ubuntu-latest
            target: linux
            asset_name: umik1-spl-streamer-linux
          - os: windows-latest
            target: windows
            asset_name: umik1-spl-streamer-windows.exe

    runs-on: ${{ matrix.os }}

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install pyinstaller
          pip install -r requirements.txt

      - name: Build binary with PyInstaller
        run: |
          pyinstaller --onefile --name ${{ matrix.asset_name }} umik1_spl_streamer.py

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: ${{ matrix.asset_name }}
          path: |
            dist/${{ matrix.asset_name }}*
          if-no-files-found: error

  release:
    needs: build
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Download all build artifacts
        uses: actions/download-artifact@v4
        with:
          path: dist
          merge-multiple: true

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          files: dist/*
          generate_release_notes: true
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

---

## Quick Setup Commands (Copy-Paste Block)

```bash
# 1. Scaffold repo
mkdir umik1-spl-streamer && cd umik1-spl-streamer
git init

# 2. Add the files from above (README.md, umik1_spl_streamer.py, requirements.txt, worker.js, wrangler.toml, dashboard/index.html, .github/workflows/release.yml)

# 3. Commit
git add .
git commit -m "init: UMIK-1 SPL telemetry streamer v1.0.0"

# 4. Push to GitHub (create repo first on github.com)
git remote add origin https://github.com/YOUR_USERNAME/umik1-spl-streamer.git
git branch -M main
git push -u origin main

# 5. Tag and release
git tag v1.0.0
git push origin v1.0.0
```

Once the tag is pushed, GitHub Actions will compile the binaries and publish them under **Releases** automatically within ~5 minutes.
