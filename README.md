# iDotMatrix 64×64 — Grafana Alloy Metrics Dashboard

A Python script that displays live system metrics on an [iDotMatrix 64×64 pixel display](https://www.amazon.com/iDotMatrix-Programmable-Creative-Animations-Accessories/dp/B0DKP2CTP6) via Bluetooth, reading data directly from a local [Grafana Alloy](https://grafana.com/docs/alloy/latest/) instance.

```
┌──────────────────────────────┐
│ 20:15               24/02   │  ← warm-yellow clock, muted date
├──────────────────────────────┤
│ CPU [███████░░░░░░░░]  42 %  │  ← cyan label + status-colour fill
│ RAM [██░░░░░░░░░░░░░]  14 %  │  ← purple label
│ /   [████████████░░░]  85 G  │  ← teal label, free GB on right
├──────────────────────────────┤
│  ╱╲___╱╲_____/‾‾╲__         │  ← CPU sparkline (cyan)
│ ──────────────────────────── │
│  ___╱‾╲___╱╲___/‾‾          │  ← RAM sparkline (purple)
└──────────────────────────────┘
```

Bar colors shift **green → yellow → orange → red** as usage climbs.
The bottom panel shows rolling CPU + RAM sparklines across the last 50 samples.

---

## Features

| Feature | Detail |
|---------|--------|
| **Clock + date** header | Configurable timezone via `--timezone` |
| **Three metric bars** | CPU, RAM, and any filesystem mountpoint |
| **Dual sparklines** | CPU (cyan) and RAM (purple), last 50 samples |
| **Supersampled sparklines** | Drawn at 4× then downscaled with LANCZOS for smooth curves |
| **Floyd-Steinberg dithering** | Reduces banding artefacts on the LED panel |
| **Gamma correction** | Compensates for LED linear response (γ = 2.2, `--gamma`) |
| **Threshold-based uploads** | Render + BLE upload only when something meaningfully changes (see below) |
| **BLE connect-with-retry** | Infinite back-off on scan/connect failure — never crashes |
| **BT adapter self-healing** | Auto power-cycles the HCI adapter after stuck errors or 3 consecutive misses |
| **Auto-reconnect** | Recovers from mid-loop BLE drops; resets frame state so display gets a fresh frame immediately |
| **Health HTTP server** | `GET :9876/` → `200 OK` for external probes / dashboards |
| **Scrolling text fallback** | `--mode text` for simple marquee output |

---

## How it works

Grafana Alloy bundles a full Prometheus node exporter (`prometheus.exporter.unix`) and exposes each component's metrics at a local HTTP endpoint. The script:

1. Fetches raw Prometheus exposition format from Alloy on each cycle
2. Parses `node_cpu_seconds_total`, `node_memory_*`, and `node_filesystem_*` metrics
3. Computes CPU % as a delta between two consecutive scrapes (same method as `rate()` in PromQL)
4. Checks whether any value crossed its upload threshold (see table below) — if not, skips rendering entirely
5. Renders a 64×64 PNG using Pillow with colour-coded bars and supersampled sparklines
6. Applies Floyd-Steinberg dithering and gamma correction to the frame
7. Pushes the image to the display over BLE using the iDotMatrix library

No Prometheus server, no psutil, no additional exporters needed — just Alloy.

### Smart upload — threshold-based rendering

Metrics are scraped every `--interval` seconds regardless, so sparkline history stays accurate. But the expensive parts — `render_frame()` and the BLE upload (~200 BLE packets) — only run when something visually meaningful changed:

| Trigger | Default threshold | CLI flag |
|---------|-------------------|----------|
| CPU usage | ≥ 1 % point | `--threshold-cpu` |
| RAM usage | ≥ 0.5 % points | `--threshold-ram` |
| Filesystem usage | ≥ 1 % point | `--threshold-fs` |
| Clock minute rolls over | always | — |

On an idle machine this typically means **one upload per minute** (the clock tick) instead of every 10 seconds — about an 83 % reduction in BLE radio usage. On a busy machine it uploads as frequently as values cross thresholds.

### Bluetooth self-healing

The BLE connect loop detects two failure modes and recovers automatically:

- **Adapter stuck** (`No discovery started`, `org.bluez.Error.*`) — resets the adapter immediately without waiting
- **3 consecutive "device not found"** — resets the adapter as a precaution (covers dirty state left by a previous crash)

Reset is done via `bluetoothctl power off/on` (requires user in the `bluetooth` group) with a fallback to `hciconfig hci0 down/up`. No manual `systemctl restart bluetooth` needed.

---

## Requirements

### Hardware
- iDotMatrix 64×64 pixel display
- Linux machine with a Bluetooth adapter
- Grafana Alloy running locally with the Linux node exporter integration enabled

### Software
- Python 3.10+
- Grafana Alloy with `prometheus.exporter.unix` configured (the Linux integration)

---

## Installation

### 1. Clone the iDotMatrix library

```bash
git clone https://github.com/markusressel/idotmatrix-api-client.git
cd idotmatrix-api-client
```

> **Note:** This script was built and tested against the [markusressel/idotmatrix-api-client](https://github.com/markusressel/idotmatrix-api-client) fork — the actively maintained version with 64×64 support.

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip poetry
poetry install
pip install pillow requests numpy
```

### 3. Copy the script

```bash
cp metrics_dashboard.py /path/to/idotmatrix-api-client/
```

Or place it in the same directory as the cloned repo.

---

## Grafana Alloy setup

The script reads from Alloy's local component API on port `12345`.  
See the [Alloy Linux configuration guide](https://grafana.com/docs/alloy/latest/configure/linux/#pass-additional-command-line-flags).

Your `config.alloy` must have the Linux integration (node exporter) enabled:

```hcl
prometheus.exporter.unix "integrations_node_exporter" {
  include_exporter_metrics = true
}

prometheus.scrape "integrations_node_exporter" {
  targets    = prometheus.exporter.unix.integrations_node_exporter.targets
  forward_to = [prometheus.remote_write.grafana_cloud.receiver]  // or your own target
}
```

Verify it works before running the script:

```bash
curl -s http://localhost:12345/api/v0/component/prometheus.exporter.unix.integrations_node_exporter/metrics \
  | grep node_memory_MemTotal
# Expected: node_memory_MemTotal_bytes 3.3412841472e+10
```

If your Alloy component label differs from `integrations_node_exporter`, pass the full URL with `--alloy-url`.

---

## Bluetooth setup

```bash
# Check adapter is powered
bluetoothctl show | grep Powered

# Add your user to the bluetooth group if needed
sudo usermod -aG bluetooth $USER
# Log out and back in after this

# Scan to find your display (look for IDM-* devices)
bluetoothctl --timeout 15 scan on
```

> **Important:** The display must not be connected to any other device (e.g. your phone) when the script is running. BLE only allows one connection at a time.

---

## Usage

```bash
# Activate the venv first
source venv/bin/activate

# Auto-discover display, 10s refresh, watching /
python3 metrics_dashboard.py

# Specify display MAC directly (more reliable)
python3 metrics_dashboard.py --mac AA:BB:CC:DD:EE:FF

# Change refresh interval
python3 metrics_dashboard.py --interval 30

# Watch a different filesystem mountpoint
python3 metrics_dashboard.py --fs /srv/media

# Set your timezone (default: Europe/Bucharest)
python3 metrics_dashboard.py --timezone America/New_York

# Scrolling text mode instead of graphical bars
python3 metrics_dashboard.py --mode text

# Point at a remote or differently-labelled Alloy instance
python3 metrics_dashboard.py --alloy-url http://10.0.0.5:12345/api/v0/component/prometheus.exporter.unix.mynode/metrics
```

### All options

| Flag | Default | Description |
|------|---------|-------------|
| `--mac` | auto-discover | Bluetooth MAC address of the display |
| `--interval` | `10` | Metrics scrape interval in seconds |
| `--fs` | `/` | Filesystem mountpoint to display |
| `--mode` | `bars` | Display mode: `bars` (graphic) or `text` (scrolling marquee) |
| `--timezone` | `Europe/Bucharest` | IANA timezone name for the clock |
| `--alloy-url` | `http://localhost:12345/…` | Alloy component metrics URL |
| `--brightness` | `80` | Display brightness (0–100) |
| `--gamma` | `2.2` | LED panel gamma correction (1.0 = linear) |
| `--threshold-cpu` | `1.0` | CPU % change needed to trigger an upload |
| `--threshold-ram` | `0.5` | RAM % change needed to trigger an upload |
| `--threshold-fs` | `1.0` | Filesystem % change needed to trigger an upload |
| `--health-port` | `9876` | HTTP port for the `/health` probe endpoint |
| `--no-health-server` | off | Disable the health HTTP server |
| `--render-test` | off | Render a synthetic frame locally, no BLE/Alloy needed |
| `--render-out` | `/tmp/idotmatrix_test.png` | Output path for `--render-test` |
| `--render-scale` | `8` | Upscale factor for `--render-test` preview image |

---

## Running on startup (systemd)

Create `/etc/systemd/system/idotmatrix.service`:

```ini
[Unit]
Description=iDotMatrix metrics dashboard
After=network.target bluetooth.target
Wants=bluetooth.target

[Service]
User=YOUR_USER_HERE
WorkingDirectory=/PATH/TO/idotmatrix-api-client
ExecStartPre=/bin/sleep 15
ExecStart=/PATH/TO/idotmatrix-api-client/venv/bin/python3 metrics_dashboard.py --mac AA:BB:CC:DD:EE:FF
Restart=on-failure
RestartSec=30
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
```

> `StartLimitIntervalSec=0` disables systemd's burst limit — the script handles its own back-off internally, so systemd should always restart it.

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now idotmatrix
sudo systemctl status idotmatrix
```

---

## Troubleshooting

**Display not found during scan**
- Make sure it's powered on and not connected to another device
- Try `sudo hciconfig hci0 up` if the adapter shows as down
- Pass `--mac` explicitly once you've found the address via `bluetoothctl --timeout 15 scan on`
- If it keeps failing, restart the Bluetooth service: `sudo systemctl restart bluetooth` — then let the script retry automatically

**Script crashes and systemd keeps restarting it**
- The script is designed to never crash on BLE errors — it retries internally
- Add `StartLimitIntervalSec=0` to the `[Service]` section to disable systemd's burst limit as a safety net

**`ImportError: cannot import name 'IDotMatrixClient'`**
- You may have the original `derkalle4` library installed. The script uses `idotmatrix.client` and `idotmatrix.screensize` which are available in both the `markusressel` fork and `derkalle4/idotmatrix-library`

**Alloy metrics URL returns nothing**
- Check your component label: `curl -s http://localhost:12345/api/v0/components | python3 -m json.tool | grep exporter.unix`
- Pass the correct URL via `--alloy-url`

**Black screen flash between updates**
- `set_mode(1)` is called once at startup (not inside the loop) — this is the current behaviour
- If you still see flashes, try increasing `--interval` to reduce upload frequency

**`numpy` not found**
- Install it: `pip install numpy`
- Or add it to the poetry project: `poetry add numpy`

---

## Screenshots

![3](https://github.com/user-attachments/assets/92a1f9cf-375d-4336-900d-f1719eae3e1c)
![2](https://github.com/user-attachments/assets/70e8a4e6-37a4-4b61-92b5-f648ad3a6137)
![1](https://github.com/user-attachments/assets/680ecdf1-befd-4833-9ee3-67d041e2448c)
