"""
iDotMatrix 64x64 — System Metrics Dashboard (Grafana Alloy edition)
====================================================================
Reads directly from the local Alloy component API — no psutil, no
standalone node_exporter, no Prometheus server needed.

Alloy endpoint (confirmed working):
  http://localhost:12345/api/v0/component/
    prometheus.exporter.unix.integrations_node_exporter/metrics

Display layout (64x64):
  ┌──────────────────────────────────────────┐
  │ CPU  [████████████░░░░░░░░░] 58%         │
  │ RAM  [████░░░░░░░░░░░░░░░░░]  5% (2/32G) │
  │ ROOT [████████████████░░░░░] 82% (80G fr)│
  │ ─────────────────────────────────────────│
  │ CPU sparkline (rolling 50 samples)        │
  └──────────────────────────────────────────┘

Usage:
  pip install idotmatrix-api-client pillow requests
  python metrics_dashboard.py
  python metrics_dashboard.py --interval 15
  python metrics_dashboard.py --mac AA:BB:CC:DD:EE:FF
  python metrics_dashboard.py --fs /srv/media   # watch a different mountpoint
  python metrics_dashboard.py --mode text       # scrolling text fallback
"""

import asyncio
import argparse
import time
import re
from collections import deque

import requests
from PIL import Image, ImageDraw

from idotmatrix.client import IDotMatrixClient
from idotmatrix.screensize import ScreenSize

# ── Alloy config ─────────────────────────────────────────────────────────────
ALLOY_METRICS_URL = (
    "http://localhost:12345/api/v0/component/"
    "prometheus.exporter.unix.integrations_node_exporter/metrics"
)

# ── Display constants ────────────────────────────────────────────────────────
W, H          = 64, 64
BAR_H         = 12      # height per bar row — fits size-10 font + 1px padding each side
BAR_ROWS      = 3       # CPU / RAM / chosen filesystem
GAP           = 2       # gap between rows
SPARK_Y       = BAR_ROWS * (BAR_H + GAP) + 2   # = 3*(12+2)+2 = 44
SPARK_H       = H - SPARK_Y - 1                # = 19

# Grafana-ish palette
BG            = (12, 12, 22)
TRACK         = (35, 35, 55)
LABEL_C       = (130, 130, 160)
VALUE_C       = (220, 220, 220)
DIVIDER_C     = (45, 45, 70)
SPARK_C       = (80, 160, 255)
GREEN         = (70, 210, 70)
YELLOW        = (240, 190, 0)
RED           = (220, 55, 55)
ORANGE        = (255, 140, 0)


def bar_color(pct: float) -> tuple:
    if pct < 60:   return GREEN
    if pct < 80:   return YELLOW
    if pct < 92:   return ORANGE
    return RED


# ── Prometheus text format parser ────────────────────────────────────────────

def parse_prom_text(text: str) -> list[tuple[str, dict, float]]:
    """
    Returns list of (metric_name, labels_dict, value).
    Handles both labelled lines and bare lines.
    """
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Split off the value (last whitespace-separated token)
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
            labels = {}
            for m in re.finditer(r'(\w+)="([^"]*)"', label_str):
                labels[m.group(1)] = m.group(2)
        else:
            name   = key_part
            labels = {}

        results.append((name.strip(), labels, value))
    return results


def index_metrics(parsed: list) -> dict:
    """Build a lookup: metric_name -> list of (labels, value)."""
    idx: dict[str, list] = {}
    for name, labels, value in parsed:
        idx.setdefault(name, []).append((labels, value))
    return idx


# ── Metric collection ────────────────────────────────────────────────────────

_prev_cpu: dict = {}   # {(cpu, mode): seconds}
_prev_ts:  float = 0.0


def _cpu_percent(idx: dict) -> float:
    global _prev_cpu, _prev_ts
    now = time.monotonic()
    cur: dict = {}
    for labels, val in idx.get("node_cpu_seconds_total", []):
        cur[(labels.get("cpu", ""), labels.get("mode", ""))] = val

    pct = 0.0
    if _prev_cpu and (now - _prev_ts) > 0:
        dt = now - _prev_ts
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
    """Returns (used_gb, total_gb, pct)."""
    total = next((v for _, v in idx.get("node_memory_MemTotal_bytes",     [])), 1)
    avail = next((v for _, v in idx.get("node_memory_MemAvailable_bytes", [])), 0)
    used  = total - avail
    pct   = used / total * 100
    return used / 1e9, total / 1e9, pct


def _filesystem(idx: dict, mountpoint: str) -> tuple[float, float, float]:
    """Returns (avail_gb, size_gb, used_pct) for a given mountpoint."""
    avail_gb = size_gb = 0.0
    for labels, val in idx.get("node_filesystem_avail_bytes", []):
        if labels.get("mountpoint") == mountpoint:
            avail_gb = val / 1e9
    for labels, val in idx.get("node_filesystem_size_bytes", []):
        if labels.get("mountpoint") == mountpoint:
            size_gb = val / 1e9
    used_pct = (1 - avail_gb / size_gb) * 100 if size_gb > 0 else 0.0
    return avail_gb, size_gb, used_pct


def fetch_metrics(mountpoint: str) -> dict | None:
    try:
        r = requests.get(ALLOY_METRICS_URL, timeout=4)
        r.raise_for_status()
    except Exception as e:
        print(f"[alloy] fetch failed: {e}")
        return None

    idx  = index_metrics(parse_prom_text(r.text))
    cpu  = _cpu_percent(idx)
    used_gb, total_gb, ram_pct = _ram(idx)
    fs_avail, fs_size, fs_pct  = _filesystem(idx, mountpoint)

    return {
        "cpu":      cpu,
        "ram_pct":  ram_pct,
        "ram_used": used_gb,
        "ram_total":total_gb,
        "fs_avail": fs_avail,
        "fs_size":  fs_size,
        "fs_pct":   fs_pct,
        "fs_mount": mountpoint,
    }


# ── Rendering ────────────────────────────────────────────────────────────────

def get_font():
    """
    Return the best available tiny font.
    PIL's ImageFont.load_default(size=8) needs Pillow >= 10.
    Fall back to the classic 1-arg default (5x8 bitmap) on older builds.
    """
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=9)
    except TypeError:
        return ImageFont.load_default()


def text_w(draw, text, font):
    """Width of a string in pixels (works for both old and new Pillow)."""
    try:
        return draw.textlength(text, font=font)
    except AttributeError:
        return len(text) * 6   # fallback for very old Pillow


def draw_bar_row(draw: ImageDraw.ImageDraw, font,
                 y: int, label: str, pct: float, right_text: str):
    filled = max(0, int(pct / 100 * (W - 2)))
    color  = bar_color(pct)

    # Track (full width background)
    draw.rectangle([(1, y), (W - 2, y + BAR_H - 1)], fill=TRACK)

    # Filled bar
    if filled > 0:
        dark = tuple(max(0, c - 70) for c in color)
        draw.rectangle([(1, y), (1 + filled - 1, y + BAR_H - 1)], fill=dark)
        # Bright 2px bottom strip as indicator line
        draw.rectangle([(1, y + BAR_H - 2), (1 + filled - 1, y + BAR_H - 1)], fill=color)

    # Dark "label zone" on left so text is always readable regardless of fill
    draw.rectangle([(1, y), (22, y + BAR_H - 1)], fill=(20, 20, 40))
    # Dark "value zone" on right
    rw = int(text_w(draw, right_text, font))
    draw.rectangle([(W - rw - 4, y), (W - 2, y + BAR_H - 1)], fill=(20, 20, 40))

    # Text on top
    draw.text((3, y + 2), label, font=font, fill=VALUE_C)
    draw.text((W - rw - 3, y + 2), right_text, font=font, fill=VALUE_C)


def draw_sparkline(draw: ImageDraw.ImageDraw, history: deque):
    if len(history) < 2:
        return
    vals = list(history)
    n    = len(vals)
    hi   = max(vals) or 1
    lo   = min(vals)
    span = hi - lo if hi != lo else 1.0

    bot = H - 2
    pts = []
    for i, v in enumerate(vals):
        x = int(i * (W - 1) / (n - 1))
        y = bot - int((v - lo) / span * (SPARK_H - 3))
        pts.append((x, y))

    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill=SPARK_C, width=1)

    # Highlight latest point
    draw.rectangle([pts[-1][0]-1, pts[-1][1]-1,
                    pts[-1][0]+1, pts[-1][1]+1], fill=VALUE_C)


def render_frame(m: dict, cpu_history: deque) -> Image.Image:
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    font = get_font()

    # Mountpoint label: "/" for root, otherwise last path component max 3 chars
    mp = m["fs_mount"]
    fs_label = "/" if mp == "/" else (mp.split("/")[-1] or "fs")[:3].upper()

    rows = [
        ("CPU", m["cpu"],     f"{m['cpu']:.0f}%"),
        ("RAM", m["ram_pct"], f"{m['ram_pct']:.0f}%"),
        (fs_label, m["fs_pct"], f"{m['fs_avail']:.0f}G"),
    ]
    for i, (label, pct, right) in enumerate(rows):
        draw_bar_row(draw, font, 1 + i * (BAR_H + GAP), label, pct, right)

    # Divider line
    draw.line([(0, SPARK_Y - 1), (W - 1, SPARK_Y - 1)], fill=DIVIDER_C)

    # Sparkline (no label — more room for the waveform)
    draw_sparkline(draw, cpu_history)

    return img


def render_text(m: dict) -> str:
    return (
        f"CPU:{m['cpu']:.0f}%  "
        f"RAM:{m['ram_pct']:.0f}% ({m['ram_used']:.1f}/{m['ram_total']:.0f}G)  "
        f"{m['fs_mount']}:{m['fs_avail']:.0f}G free  "
    )


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run(args):
    client = IDotMatrixClient(
        screen_size=ScreenSize.SIZE_64x64,
        mac_address=args.mac,
    )
    print("Connecting to iDotMatrix 64x64...")
    await client.connect()
    print("Connected!")
    await client.set_brightness(80)
    await client.image.set_mode(1)   # enter DIY image mode once — avoids flash on each update

    cpu_history: deque = deque(maxlen=50)
    tmp = "/tmp/idotmatrix_metrics.png"

    # First scrape seeds the CPU delta baseline (value will be ~0)
    fetch_metrics(args.fs)
    await asyncio.sleep(2)

    while True:
        m = fetch_metrics(args.fs)
        if m is None:
            print("  [!] Could not reach Alloy, retrying next cycle")
            await asyncio.sleep(args.interval)
            continue

        cpu_history.append(m["cpu"])

        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] CPU={m['cpu']:.1f}%  RAM={m['ram_pct']:.1f}%  "
              f"{m['fs_mount']} free={m['fs_avail']:.1f}G ({m['fs_pct']:.0f}% used)")

        if args.mode == "text":
            await client.text.display_text(render_text(m))
        else:
            img = render_frame(m, cpu_history)
            img.save(tmp)
            await client.image.upload_image_file(tmp)

        await asyncio.sleep(args.interval)


def main():
    p = argparse.ArgumentParser(description="iDotMatrix 64x64 — Alloy metrics dashboard")
    p.add_argument("--mac",      default=None,
                   help="Bluetooth MAC (omit to auto-discover)")
    p.add_argument("--interval", default=10, type=int,
                   help="Refresh interval in seconds (default: 10)")
    p.add_argument("--mode",     default="bars", choices=["bars", "text"],
                   help="Display mode: bars (default) or scrolling text")
    p.add_argument("--fs",       default="/",
                   help="Filesystem mountpoint to show (default: /). "
                        "Your system has: /, /home, /srv/k8s, /srv/media, "
                        "/srv/downloads, /var/www, /var/log, /boot")
    args = p.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
