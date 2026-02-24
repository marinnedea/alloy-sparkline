# iDotMatrix 64×64 — Grafana Alloy Metrics Dashboard

A Python script that displays live system metrics on an [iDotMatrix 64×64 pixel display](https://www.amazon.com/iDotMatrix-Programmable-Creative-Animations-Accessories/dp/B0DKP2CTP6) via Bluetooth, reading data directly from a local [Grafana Alloy](https://grafana.com/docs/alloy/latest/) instance.

```
# without time and date - use the metrics_dashboard.py script
┌─────────────────────────────┐
│ CPU  [████░░░░░░░░░] 12 %   │
│ RAM  [█░░░░░░░░░░░░]  5 %   │
│ /    [████████████░] 85 G   │
│─────────────────────────────│
│ ╱╲___╱╲___/‾‾╲__           │  ← sparkline
└─────────────────────────────┘

# with time and date -  use the metrics_dashboard_with_timestamp.py script
# TIMEZONE = "Europe/Bucharest" is hardcoded in the constants .
# Created this version only because my daughter was bugging me that she wants
# to see the time there 😁
┌─────────────────────────────┐
│ 20:15              24/02    │  ← warm yellow time, muted date
│─────────────────────────────│
│ CPU  [████░░░░░░░░░] 12 %   │
│ RAM  [█░░░░░░░░░░░░]  5 %   │
│ /    [████████████░] 85 G   │
│─────────────────────────────│
│ ╱╲___╱╲___/‾‾╲__           │  ← sparkline
└─────────────────────────────┘

```

Bar colors shift green → yellow → orange → red as usage climbs. The bottom panel shows a rolling CPU sparkline across the last 50 samples.

---

## How it works

Grafana Alloy bundles a full Prometheus node exporter (`prometheus.exporter.unix`) and exposes each component's metrics at a local HTTP endpoint. The script:

1. Fetches the raw Prometheus text exposition format from Alloy on each cycle
2. Parses `node_cpu_seconds_total`, `node_memory_*`, and `node_filesystem_*` metrics
3. Computes CPU % as a delta between two consecutive scrapes (same method as `rate()` in PromQL)
4. Renders a 64×64 PNG using Pillow with color-coded bars and a rolling sparkline
5. Pushes the image to the display over BLE using the iDotMatrix library

No Prometheus server, no psutil, no additional exporters needed — just Alloy.

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

**Note:** This script was built and tested against the [markusressel/idotmatrix-api-client](https://github.com/markusressel/idotmatrix-api-client) fork, which is the actively maintained version with 64×64 support. It uses the internal module imports (`idotmatrix.client`, `idotmatrix.screensize`) from this specific fork.

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip poetry
poetry install
pip install pillow requests
```

### 3. Copy the script

```bash
cp metrics_dashboard.py /path/to/idotmatrix-api-client/
```

Or just place it in the same directory as the cloned repo.

---

## Grafana Alloy setup

The script reads from Alloy's local component API on port `12345` - see how-to [here](https://grafana.com/docs/alloy/latest/configure/linux/#pass-additional-command-line-flags). Your `config.alloy` must have the Linux integration (node exporter) enabled.
Please note I haven't tested yet on Windows or MacOS, so not sure what would be the right equivalent for those. Will update once I have more details.

The confirmed working endpoint is:

```
http://localhost:12345/api/v0/component/prometheus.exporter.unix.integrations_node_exporter/metrics
```

A minimal Alloy config that enables this:

```hcl
prometheus.exporter.unix "integrations_node_exporter" {
  include_exporter_metrics = true
}

prometheus.scrape "integrations_node_exporter" {
  targets    = prometheus.exporter.unix.integrations_node_exporter.targets
  forward_to = [prometheus.remote_write.grafana_cloud.receiver]  // or your own target
}
```

Verify it's working before running the script:

```bash
curl -s http://localhost:12345/api/v0/component/prometheus.exporter.unix.integrations_node_exporter/metrics | grep node_memory_MemTotal
# Expected output: node_memory_MemTotal_bytes 3.3412841472e+10
```

If your Alloy label is different from `integrations_node_exporter`, update the `ALLOY_METRICS_URL` constant at the top of `metrics_dashboard.py`.

---

## Bluetooth setup

Make sure the Bluetooth service is running and your user has permission:

```bash
# Check adapter is powered
bluetoothctl show | grep Powered

# Add your user to the bluetooth group if needed
sudo usermod -aG bluetooth $USER
# Log out and back in after this

# Scan to find your display (look for IDM-* devices)
bluetoothctl --timeout 15 scan on
```

# Important!
> The display must not be connected to any other device (e.g. your phone) when you run the script. BLE only allows one connection at a time.

---

## Usage

```bash
# Activate the venv first
source venv/bin/activate

# Auto-discover display, default 10s refresh, watching /
python3 metrics_dashboard.py

# Specify display MAC directly (more reliable)
python3 metrics_dashboard.py --mac AA:BB:CC:DD:EE:FF

# Change refresh interval (30s recommended for desk use)
python3 metrics_dashboard.py --interval 30

# Watch a different filesystem mountpoint
python3 metrics_dashboard.py --fs /srv/media
python3 metrics_dashboard.py --fs /home

# Scrolling text mode instead of graphical bars
python3 metrics_dashboard.py --mode text
```

### All options

| Flag | Default | Description |
|------|---------|-------------|
| `--mac` | auto-discover | Bluetooth MAC address of the display |
| `--interval` | `10` | Refresh interval in seconds |
| `--fs` | `/` | Filesystem mountpoint to display |
| `--mode` | `bars` | Display mode: `bars` or `text` |

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
ExecStart=/PATH/TO/idotmatrix-api-client/venv/bin/python3 metrics_dashboard.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

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

**`ImportError: cannot import name 'IDotMatrixClient'`**
- You likely have the original `derkalle4` library. The imports in this script use the internal module paths (`idotmatrix.client`, `idotmatrix.screensize`) which work with both forks.

**Alloy metrics URL returns nothing**
- Check your component label: `curl -s http://localhost:12345/api/v0/components | python3 -m json.tool | grep exporter.unix`
- Update `ALLOY_METRICS_URL` in the script to match your label

**Black screen flash between updates**
- Make sure `set_mode(1)` is only called once at startup (not inside the loop) — this is already the case in the current version of the script

---

***Some actual images***

![3](https://github.com/user-attachments/assets/92a1f9cf-375d-4336-900d-f1719eae3e1c)
![2](https://github.com/user-attachments/assets/70e8a4e6-37a4-4b61-92b5-f648ad3a6137)
![1](https://github.com/user-attachments/assets/680ecdf1-befd-4833-9ee3-67d041e2448c)
