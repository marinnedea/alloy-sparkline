"""
iDotMatrix 64x64 — System Metrics Dashboard (Grafana Alloy edition)
====================================================================
Reads live metrics from a local Grafana Alloy node_exporter component.
No psutil, no standalone node_exporter, no Prometheus server required.

Alloy endpoint used:
  http://localhost:12345/api/v0/component/
    prometheus.exporter.unix.integrations_node_exporter/metrics

Display layout (64×64):
  ┌──────────────────────────────┐
  │ 20:15               24/02   │  ← warm-yellow clock, muted date
  ├──────────────────────────────┤
  │ CPU [███████░░░░░░░░]  42 %  │  ← cyan label + status-colour fill
  │ RAM [██░░░░░░░░░░░░░]  14 %  │  ← purple label
  │ /   [████████████░░░]  85 G  │  ← teal label, free GB right
  ├──────────────────────────────┤
  │  ╱╲___╱╲_____/‾‾╲__         │  ← CPU sparkline (cyan)
  │ ──────────────────────────── │
  │  ___╱‾╲___╱╲___/‾‾          │  ← RAM sparkline (purple)
  └──────────────────────────────┘

Features
--------
  • Supersampled sparklines via numpy (4× scale, then LANCZOS downsample)
  • Floyd-Steinberg dithering on final frame
  • Gamma-corrected output (compensates LED panel non-linearity)
  • Frame deduplication — skips BLE upload when pixels are unchanged
  • BLE connect-with-retry — infinite back-off, never crashes
  • Auto-reconnect on mid-loop BLE failure
  • Lightweight HTTP health server on :9876 for external probes

Usage
-----
  pip install idotmatrix-api-client pillow requests numpy
  python metrics_dashboard.py                          # defaults
  python metrics_dashboard.py --interval 15
  python metrics_dashboard.py --mac AA:BB:CC:DD:EE:FF
  python metrics_dashboard.py --fs /srv/media          # watch different mount
  python metrics_dashboard.py --mode text              # scrolling-text fallback
  python metrics_dashboard.py --timezone America/New_York
  python metrics_dashboard.py --alloy-url http://10.0.0.5:12345/...
  python metrics_dashboard.py --brightness 60
  python metrics_dashboard.py --health-port 9877       # change probe port
"""

# ── Standard library ─────────────────────────────────────────────────────────
import asyncio
import argparse
import hashlib
import http.server
import re
import threading
import time
from collections import deque
from datetime import datetime
from io import BytesIO

# ── Third-party ──────────────────────────────────────────────────────────────
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

# ── iDotMatrix (imported lazily so render-test works without the library) ───
try:
    from idotmatrix.client import IDotMatrixClient
    from idotmatrix.screensize import ScreenSize
    _IDOTMATRIX_AVAILABLE = True
except ImportError:
    _IDOTMATRIX_AVAILABLE = False
    IDotMatrixClient = None   # type: ignore[assignment,misc]
    ScreenSize       = None   # type: ignore[assignment]


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_ALLOY_URL = (
    "http://localhost:12345/api/v0/component/"
    "prometheus.exporter.unix.integrations_node_exporter/metrics"
)
DEFAULT_HEALTH_PORT = 9876
DEFAULT_BRIGHTNESS  = 80
DEFAULT_INTERVAL    = 10
DEFAULT_TIMEZONE    = "Europe/Bucharest"
DEFAULT_FS          = "/"
DEFAULT_MAC         = None

# LED panel gamma. Values > 1.0 brighten midtones, compensating for the linear
# LED response looking darker than expected (typical value: 1.8–2.2).
DISPLAY_GAMMA = 2.2

# Sparkline supersample factor — drawn at (W*S × H*S) then downscaled.
# Higher = smoother curves; 4 is a good balance of quality vs. speed.
SPARK_SCALE = 4

# ── Display geometry ─────────────────────────────────────────────────────────
W, H         = 64, 64
TIME_H       = 11             # header bar height (last row = divider)
BAR_H        = 11             # height of each metric bar row
BAR_ROWS     = 3              # CPU / RAM / filesystem
BAR_GAP      = 1              # vertical gap between bar rows
BARS_Y       = TIME_H + 1     # bars start just below the header divider
SPARK_Y      = BARS_Y + BAR_ROWS * (BAR_H + BAR_GAP)   # ≈ 48
EACH_SPARK_H = (H - SPARK_Y - 2) // 2                   # ≈ 7 px each
CPU_SPARK_Y  = SPARK_Y + 1
RAM_SPARK_Y  = CPU_SPARK_Y + EACH_SPARK_H + 1

# ── Palette ──────────────────────────────────────────────────────────────────
BG        = (12,  12,  22)
TRACK     = (35,  35,  55)
LABEL_C   = (130, 130, 160)
VALUE_C   = (220, 220, 220)
DIVIDER_C = (45,  45,  70)
GREEN     = (70,  210,  70)
YELLOW    = (240, 190,   0)
ORANGE    = (255, 140,   0)
RED       = (220,  55,  55)
TIME_C    = (255, 220,  80)

# Per-metric accent colors
CPU_C = (100, 200, 255)   # cyan-blue
RAM_C = (180, 100, 255)   # purple
FS_C  = (80,  200, 120)   # teal-green


def bar_color(pct: float) -> tuple:
    """Status-based bar fill color."""
    if pct < 60:  return GREEN
    if pct < 80:  return YELLOW
    if pct < 92:  return ORANGE
    return RED


# ── Gamma LUT (built once at import time) ────────────────────────────────────
_GAMMA_LUT = np.array(
    [int((i / 255.0) ** (1.0 / DISPLAY_GAMMA) * 255 + 0.5) for i in range(256)],
    dtype=np.uint8,
)


# ══════════════════════════════════════════════════════════════════════════════
# PROMETHEUS TEXT-FORMAT PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_prom_text(text: str) -> list[tuple[str, dict, float]]:
    """
    Parse Prometheus exposition format.
    Returns list of (metric_name, labels_dict, float_value).
    """
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        key_part, val_str = parts
        try:
            value = float(val_str)
        except ValueError:
            continue
        if "{" in key_part:
            name, label_str = key_part.split("{", 1)
            label_str = label_str.rstrip("}")
            labels = {m.group(1): m.group(2)
                      for m in re.finditer(r'(\w+)="([^"]*)"', label_str)}
        else:
            name, labels = key_part, {}
        results.append((name.strip(), labels, value))
    return results


def index_metrics(parsed: list) -> dict:
    """Build lookup: metric_name → list of (labels_dict, value)."""
    idx: dict[str, list] = {}
    for name, labels, value in parsed:
        idx.setdefault(name, []).append((labels, value))
    return idx


# ══════════════════════════════════════════════════════════════════════════════
# METRICS COLLECTION
# ══════════════════════════════════════════════════════════════════════════════

_prev_cpu: dict  = {}
_prev_ts:  float = 0.0


def _cpu_percent(idx: dict) -> float:
    """Delta-based CPU% from node_cpu_seconds_total counters."""
    global _prev_cpu, _prev_ts
    now = time.monotonic()
    cur: dict = {}
    for labels, val in idx.get("node_cpu_seconds_total", []):
        cur[(labels.get("cpu", ""), labels.get("mode", ""))] = val

    pct = 0.0
    if _prev_cpu and (now - _prev_ts) > 0:
        d_total = d_idle = 0.0
        for key, val in cur.items():
            delta = val - _prev_cpu.get(key, val)
            d_total += delta
            if key[1] in ("idle", "iowait"):
                d_idle += delta
        if d_total > 0:
            pct = max(0.0, min(100.0, (1 - d_idle / d_total) * 100))

    _prev_cpu = cur
    _prev_ts  = now
    return pct


def _ram(idx: dict) -> tuple[float, float, float]:
    """Returns (used_GB, total_GB, used_pct)."""
    total = next((v for _, v in idx.get("node_memory_MemTotal_bytes",     [])), 1)
    avail = next((v for _, v in idx.get("node_memory_MemAvailable_bytes", [])), 0)
    used  = total - avail
    return used / 1e9, total / 1e9, (used / total) * 100


def _filesystem(idx: dict, mountpoint: str) -> tuple[float, float, float]:
    """Returns (avail_GB, size_GB, used_pct) for the given mountpoint."""
    avail_gb = size_gb = 0.0
    for labels, val in idx.get("node_filesystem_avail_bytes", []):
        if labels.get("mountpoint") == mountpoint:
            avail_gb = val / 1e9
    for labels, val in idx.get("node_filesystem_size_bytes", []):
        if labels.get("mountpoint") == mountpoint:
            size_gb = val / 1e9
    pct = (1 - avail_gb / size_gb) * 100 if size_gb > 0 else 0.0
    return avail_gb, size_gb, pct


def fetch_metrics(alloy_url: str, mountpoint: str) -> dict | None:
    """
    Fetch metrics from Alloy and return a flat dict, or None on failure.
    The first call seeds the CPU counter baseline; its cpu value will be ~0.
    """
    try:
        r = requests.get(alloy_url, timeout=4)
        r.raise_for_status()
    except Exception as e:
        print(f"[alloy] fetch failed: {e}")
        return None

    idx = index_metrics(parse_prom_text(r.text))
    cpu = _cpu_percent(idx)
    ram_used, ram_total, ram_pct = _ram(idx)
    fs_avail, fs_size, fs_pct   = _filesystem(idx, mountpoint)

    return {
        "cpu":       cpu,
        "ram_pct":   ram_pct,
        "ram_used":  ram_used,
        "ram_total": ram_total,
        "fs_avail":  fs_avail,
        "fs_size":   fs_size,
        "fs_pct":    fs_pct,
        "fs_mount":  mountpoint,
    }


# ══════════════════════════════════════════════════════════════════════════════
# RENDERING
# ══════════════════════════════════════════════════════════════════════════════

def _get_font() -> ImageFont.ImageFont:
    """Return the best available small bitmap font for Pillow ≥ 9."""
    try:
        return ImageFont.load_default(size=9)   # Pillow ≥ 10
    except TypeError:
        return ImageFont.load_default()          # Pillow ≤ 9 (no size arg)


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    """Text pixel width — compatible with Pillow 9 and 10+."""
    try:
        return int(draw.textlength(text, font=font))
    except AttributeError:
        return len(text) * 6


def _draw_header(draw: ImageDraw.ImageDraw, font, timezone: str) -> None:
    """Render the clock/date header bar."""
    from zoneinfo import ZoneInfo
    now       = datetime.now(ZoneInfo(timezone))
    time_str  = now.strftime("%H:%M")
    date_str  = now.strftime("%d/%m")

    draw.rectangle([(0, 0), (W - 1, TIME_H - 2)], fill=(25, 25, 50))
    draw.text((2, 1), time_str, font=font, fill=TIME_C)
    rw = _text_w(draw, date_str, font)
    draw.text((W - rw - 2, 1), date_str, font=font, fill=LABEL_C)
    draw.line([(0, TIME_H - 1), (W - 1, TIME_H - 1)], fill=DIVIDER_C)


def _draw_bar_row(draw: ImageDraw.ImageDraw, font,
                  y: int, label: str, pct: float, right_text: str,
                  label_color: tuple) -> None:
    """Render a single labeled progress bar."""
    filled = max(0, int(pct / 100 * (W - 2)))
    color  = bar_color(pct)

    # Background track
    draw.rectangle([(1, y), (W - 2, y + BAR_H - 1)], fill=TRACK)
    # Filled portion
    if filled > 0:
        draw.rectangle([(1, y), (filled, y + BAR_H - 1)], fill=color)
    # Dark zones so label + value text are always legible over the fill
    draw.rectangle([(1, y), (22, y + BAR_H - 1)], fill=(20, 20, 40))
    rw = _text_w(draw, right_text, font)
    draw.rectangle([(W - rw - 4, y), (W - 2, y + BAR_H - 1)], fill=(20, 20, 40))
    # Labels
    draw.text((3,             y + 2), label,      font=font, fill=label_color)
    draw.text((W - rw - 3,   y + 2), right_text, font=font, fill=VALUE_C)


def _draw_sparkline_supersampled(
    draw: ImageDraw.ImageDraw,
    history: deque,
    y_top: int,
    height: int,
    color: tuple,
) -> None:
    """
    Render a sparkline at SPARK_SCALE× resolution via numpy, then
    paste the downscaled result onto `draw`'s image.

    Steps:
      1. Build a (W*S) × (height*S) canvas in numpy
      2. Use Bresenham-style thick lines via PIL at the large scale
      3. Scale down with LANCZOS → smooth anti-aliased curve
      4. Paste into the parent image at (0, y_top)
    """
    if len(history) < 2:
        return

    vals = np.asarray(list(history), dtype=float)
    n    = len(vals)
    hi   = vals.max() or 1.0
    lo   = vals.min()
    span = (hi - lo) if hi != lo else 1.0

    S    = SPARK_SCALE
    sw   = W * S
    sh   = height * S
    bot  = sh - 1

    # Draw at high resolution
    big_img  = Image.new("RGB", (sw, sh), BG)
    big_draw = ImageDraw.Draw(big_img)

    pts = []
    for i, v in enumerate(vals):
        x = int(round(i * (sw - 1) / (n - 1)))
        y = bot - int(round((v - lo) / span * (sh - 2 * S)))
        pts.append((x, y))

    for i in range(len(pts) - 1):
        big_draw.line([pts[i], pts[i + 1]], fill=color, width=S)

    # Latest-point dot
    px, py = pts[-1]
    r = S
    big_draw.ellipse([(px - r, py - r), (px + r, py + r)], fill=VALUE_C)

    # Downsample
    small = big_img.resize((W, height), Image.LANCZOS)

    # Paste into the parent draw's image
    draw._image.paste(small, (0, y_top))


def _floyd_steinberg(img: Image.Image) -> Image.Image:
    """
    Apply Floyd-Steinberg dithering in-place to an RGB PIL image.
    Reduces banding artefacts on the 64×64 LED panel.
    """
    arr = np.array(img, dtype=np.int16)
    h, w = arr.shape[:2]

    for y in range(h):
        for x in range(w):
            old = arr[y, x].copy()
            # Quantize to 6-bit per channel (64 levels) then back to 8-bit
            new = (old >> 2) << 2
            new = np.clip(new, 0, 255).astype(np.int16)
            arr[y, x] = new
            err = old - new

            if x + 1 < w:
                arr[y,     x + 1] += err * 7 // 16
            if y + 1 < h:
                if x > 0:
                    arr[y + 1, x - 1] += err * 3 // 16
                arr[y + 1, x    ] += err * 5 // 16
                if x + 1 < w:
                    arr[y + 1, x + 1] += err * 1 // 16

    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def _apply_gamma(img: Image.Image) -> Image.Image:
    """
    Apply the global DISPLAY_GAMMA correction lookup table.
    Brightens midtones to compensate for the LED panel's linear response
    appearing darker than expected.
    """
    arr = np.array(img, dtype=np.uint8)
    arr = _GAMMA_LUT[arr]
    return Image.fromarray(arr, "RGB")


def render_frame(
    m: dict,
    cpu_history: deque,
    ram_history: deque,
    timezone: str = DEFAULT_TIMEZONE,
) -> Image.Image:
    """
    Build the full 64×64 RGB frame:
      header → bars → sparklines → Floyd-Steinberg dither → gamma correction
    """
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    font = _get_font()

    # Header
    _draw_header(draw, font, timezone)

    # Metric bars
    mp       = m["fs_mount"]
    fs_label = "/" if mp == "/" else (mp.split("/")[-1] or "fs")[:3].upper()
    rows = [
        ("CPU", m["cpu"],     f"{m['cpu']:.0f} %",     CPU_C),
        ("RAM", m["ram_pct"], f"{m['ram_pct']:.0f} %",  RAM_C),
        (fs_label, m["fs_pct"], f"{m['fs_avail']:.0f} G", FS_C),
    ]
    for i, (label, pct, right, lc) in enumerate(rows):
        _draw_bar_row(draw, font, BARS_Y + i * (BAR_H + BAR_GAP),
                      label, pct, right, lc)

    # Divider above sparklines
    draw.line([(0, SPARK_Y - 1), (W - 1, SPARK_Y - 1)], fill=DIVIDER_C)

    # Supersampled sparklines
    _draw_sparkline_supersampled(draw, cpu_history, CPU_SPARK_Y, EACH_SPARK_H, CPU_C)
    mid = CPU_SPARK_Y + EACH_SPARK_H
    draw.line([(0, mid), (W - 1, mid)], fill=DIVIDER_C)
    _draw_sparkline_supersampled(draw, ram_history, RAM_SPARK_Y, EACH_SPARK_H, RAM_C)

    # Post-processing
    img = _floyd_steinberg(img)
    img = _apply_gamma(img)

    return img


def render_text(m: dict) -> str:
    """One-line summary for scrolling-text mode."""
    return (
        f"CPU:{m['cpu']:.0f}%  "
        f"RAM:{m['ram_pct']:.0f}% ({m['ram_used']:.1f}/{m['ram_total']:.0f}G)  "
        f"{m['fs_mount']}:{m['fs_avail']:.0f}G free  "
    )


# ══════════════════════════════════════════════════════════════════════════════
# FRAME DEDUPLICATION
# ══════════════════════════════════════════════════════════════════════════════

_last_frame_hash: str = ""


def frame_changed(img: Image.Image) -> bool:
    """
    Return True when the frame differs from the last uploaded frame.
    Uses a SHA-256 digest of raw pixel bytes — fast and collision-free
    for our purposes.
    """
    global _last_frame_hash
    digest = hashlib.sha256(img.tobytes()).hexdigest()
    if digest == _last_frame_hash:
        return False
    _last_frame_hash = digest
    return True


# ══════════════════════════════════════════════════════════════════════════════
# BLE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

async def connect_with_retry(client: IDotMatrixClient, mac: str | None) -> None:
    """
    Try to connect the BLE client indefinitely with 15-second back-off.
    A BleakScanner.discover() pass runs before each connect attempt because
    bleak sometimes fails to find the device without a prior scan.
    Never raises — always returns once connected.
    """
    delay   = 15
    attempt = 0
    while True:
        attempt += 1
        try:
            from bleak import BleakScanner
            label = mac or "nearest device"
            print(f"[BLE] Scan attempt {attempt} (looking for {label})…")
            await BleakScanner.discover(timeout=10.0)
            await client.connect()
            print("[BLE] Connected!")
            return
        except Exception as exc:
            print(f"[BLE] Connection failed (attempt {attempt}): {exc}")
            print(f"[BLE] Retrying in {delay}s…")
            await asyncio.sleep(delay)


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH SERVER
# ══════════════════════════════════════════════════════════════════════════════

class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Tiny HTTP handler: GET / or /health → 200 OK."""

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        body = b"OK\n"
        self.send_response(200)
        self.send_header("Content-Type",   "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):   # suppress per-request noise
        pass


def start_health_server(port: int) -> None:
    """
    Start a background HTTP health server on *port*.
    Runs in a daemon thread so it never blocks the asyncio event loop
    and is automatically cleaned up when the main process exits.
    """
    server = http.server.HTTPServer(("", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[health] HTTP server listening on :{port}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ASYNC LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def run(args: argparse.Namespace) -> None:
    if not _IDOTMATRIX_AVAILABLE:
        raise SystemExit(
            "ERROR: idotmatrix library not found.\n"
            "  Install it from: https://github.com/markusressel/idotmatrix-api-client\n"
            "  Or run with --render-test to preview rendering without BLE."
        )

    tmp = "/tmp/idotmatrix_metrics.png"

    def make_client() -> IDotMatrixClient:
        return IDotMatrixClient(
            screen_size=ScreenSize.SIZE_64x64,
            mac_address=args.mac,
        )

    async def init_client(client: IDotMatrixClient) -> None:
        await connect_with_retry(client, args.mac)
        await client.set_brightness(args.brightness)
        await client.image.set_mode(1)   # DIY image mode — avoids flash on updates

    print("Connecting to iDotMatrix 64x64…")
    client = make_client()
    await init_client(client)

    cpu_history: deque = deque(maxlen=50)
    ram_history: deque = deque(maxlen=50)

    # First scrape seeds the CPU delta baseline; reported value will be ~0
    fetch_metrics(args.alloy_url, args.fs)
    await asyncio.sleep(2)

    while True:
        m = fetch_metrics(args.alloy_url, args.fs)
        if m is None:
            print("  [!] Could not reach Alloy — retrying next cycle")
            await asyncio.sleep(args.interval)
            continue

        cpu_history.append(m["cpu"])
        ram_history.append(m["ram_pct"])

        ts = time.strftime("%H:%M:%S")
        print(
            f"[{ts}] CPU={m['cpu']:.1f}%  RAM={m['ram_pct']:.1f}%  "
            f"{m['fs_mount']} free={m['fs_avail']:.1f}G ({m['fs_pct']:.0f}% used)"
        )

        try:
            if args.mode == "text":
                client.text.image_width  = 8
                client.text.image_height = 16
                client.text.separator    = b"\x02\xff\xff\xff"
                await client.text.show_text(
                    render_text(m),
                    font_size=6,
                    speed=60,
                    text_mode=1,
                    text_color=(220, 220, 220),
                )
            else:
                img = render_frame(m, cpu_history, ram_history, args.timezone)
                if frame_changed(img):
                    img.save(tmp)
                    await client.image.upload_image_file(tmp)
                else:
                    print(f"[{ts}] Frame unchanged — skipping BLE upload")

        except Exception as exc:
            print(f"[BLE] Lost connection: {exc} — reconnecting…")
            try:
                await client.disconnect()
            except Exception:
                pass
            client = make_client()
            await init_client(client)

        await asyncio.sleep(args.interval)


# ══════════════════════════════════════════════════════════════════════════════
# RENDER TEST  (no BLE, no Alloy required)
# ══════════════════════════════════════════════════════════════════════════════

def render_test(out_path: str = "/tmp/idotmatrix_test.png",
                scale: int = 8,
                timezone: str = DEFAULT_TIMEZONE) -> None:
    """
    Render a synthetic frame with fake-but-realistic metrics and save it to
    *out_path* at *scale*× upscaling for easy inspection on a desktop.

    Run with:
        python3 metrics_dashboard.py --render-test
        python3 metrics_dashboard.py --render-test --render-out ~/Desktop/frame.png
        python3 metrics_dashboard.py --render-test --render-scale 12
    """
    import math

    # Synthetic histories — a gentle sine wave so both sparklines look alive
    n = 40
    cpu_hist: deque = deque(maxlen=50)
    ram_hist: deque = deque(maxlen=50)
    for i in range(n):
        cpu_hist.append(35 + 30 * math.sin(i * 0.35))
        ram_hist.append(18 + 8  * math.cos(i * 0.25))

    m = {
        "cpu":       cpu_hist[-1],
        "ram_pct":   ram_hist[-1],
        "ram_used":  11.4,
        "ram_total": 32.0,
        "fs_avail":  142.0,
        "fs_size":   480.0,
        "fs_pct":    70.4,
        "fs_mount":  "/",
    }

    img = render_frame(m, cpu_hist, ram_hist, timezone)

    # Upscale for easy desktop inspection (nearest-neighbor keeps pixel edges sharp)
    big = img.resize((W * scale, H * scale), Image.NEAREST)
    big.save(out_path)
    print(f"[render-test] Saved {W*scale}×{H*scale} preview → {out_path}")

    # Try to open it with the system viewer
    import subprocess, sys
    opener = {"darwin": "open", "linux": "xdg-open"}.get(sys.platform)
    if opener:
        subprocess.Popen([opener, out_path])


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    p = argparse.ArgumentParser(
        description="iDotMatrix 64×64 — Grafana Alloy metrics dashboard",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--mac", default=DEFAULT_MAC,
        help="Bluetooth MAC address of the display. Omit to auto-discover.",
    )
    p.add_argument(
        "--interval", default=DEFAULT_INTERVAL, type=int,
        metavar="SEC",
        help="Metrics refresh interval in seconds.",
    )
    p.add_argument(
        "--mode", default="bars", choices=["bars", "text"],
        help="Display mode: bars (graphic) or text (scrolling marquee).",
    )
    p.add_argument(
        "--timezone", default=DEFAULT_TIMEZONE,
        metavar="TZ",
        help="IANA timezone for the clock (e.g. UTC, America/New_York).",
    )
    p.add_argument(
        "--fs", default=DEFAULT_FS,
        metavar="MOUNT",
        help="Filesystem mountpoint to track (e.g. / or /srv/media).",
    )
    p.add_argument(
        "--alloy-url", default=DEFAULT_ALLOY_URL,
        metavar="URL",
        help="Alloy component metrics URL.",
    )
    p.add_argument(
        "--brightness", default=DEFAULT_BRIGHTNESS, type=int,
        metavar="0-100",
        help="Display brightness percentage.",
    )
    p.add_argument(
        "--health-port", default=DEFAULT_HEALTH_PORT, type=int,
        metavar="PORT",
        help="HTTP port for the /health probe endpoint.",
    )
    p.add_argument(
        "--no-health-server", action="store_true",
        help="Disable the health HTTP server.",
    )
    # ── Render-test mode ──────────────────────────────────────────────────────
    p.add_argument(
        "--render-test", action="store_true",
        help=(
            "Render a synthetic frame with fake metrics and save it locally. "
            "No BLE, no Alloy, no display required. Useful for visual QA."
        ),
    )
    p.add_argument(
        "--render-out", default="/tmp/idotmatrix_test.png",
        metavar="PATH",
        help="Output path for --render-test (default: /tmp/idotmatrix_test.png).",
    )
    p.add_argument(
        "--render-scale", default=8, type=int,
        metavar="N",
        help="Upscale factor for --render-test preview image (default: 8).",
    )
    args = p.parse_args()

    # ── Render-test short-circuit ─────────────────────────────────────────────
    if args.render_test:
        render_test(args.render_out, args.render_scale, args.timezone)
        return

    if not args.no_health_server:
        start_health_server(args.health_port)

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
