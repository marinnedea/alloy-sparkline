# Changelog

All notable changes to this project are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [2.0.0] — 2026-04-23

Major refactor. Three overlapping script variants consolidated into a single
`metrics_dashboard.py` with every improvement from the live deployed version
plus new reliability and efficiency features.

### Added
- **Threshold-based smart upload** — render + BLE upload only fires when a
  value crosses its threshold; on an idle machine this drops from ~6 uploads/min
  to 1/min (the clock tick), reducing BLE radio usage by ~83 %
  - `--threshold-cpu` (default 1.0 %)
  - `--threshold-ram` (default 0.5 %)
  - `--threshold-fs`  (default 1.0 %)
- **BT adapter self-healing** — `connect_with_retry` now detects stuck-adapter
  errors (`No discovery started`, `org.bluez.Error.*`) and auto power-cycles
  the HCI adapter via `bluetoothctl` (fallback: `hciconfig`); also resets after
  3 consecutive "device not found" misses
- **FrameState reset on every (re-)connect** — guarantees the display always
  gets a fresh frame immediately after first start or a mid-session device
  power-cycle; no more blank screen after a crash or restart
- **Supersampled sparklines** — drawn at 4× resolution via numpy, downscaled
  with LANCZOS for smooth anti-aliased curves
- **Floyd-Steinberg dithering** — applied to every frame to reduce banding
  artefacts on the LED panel
- **Gamma correction** — per-pixel LUT (γ = 2.2 default) compensates for the
  LED panel's linear response appearing darker than expected
- **`--gamma`** CLI flag — override gamma without editing the script
- **`--render-test`** mode — renders a synthetic frame locally with no BLE or
  Alloy required; useful for visual QA and CI
  - `--render-out` — output file path (default `/tmp/idotmatrix_test.png`)
  - `--render-scale` — upscale factor for desktop inspection (default `8`)
- **`--alloy-url`** — point at a remote or differently-labelled Alloy instance
- **`--brightness`** — set display brightness from the CLI
- **`--health-port`** — change the health server port (default `9876`)
- **`--no-health-server`** — disable the health HTTP server entirely
- **Health HTTP server** on `:9876` — background daemon thread, survives BLE
  crashes; `GET /` → `200 OK` for external probes and dashboards
- **Lazy `idotmatrix` import** — `--render-test` and other non-BLE operations
  work even without the iDotMatrix library installed
- **`.gitignore`** — excludes `.venv/`, `__pycache__/`, build artefacts

### Changed
- `connect_with_retry` rebuilt with self-healing logic and clearer per-attempt
  logging (`[BLE] Scan attempt N (looking for MAC)…`)
- `run()` split into `connect_and_reset()` helper so initial connect and
  mid-loop reconnect share the same logic
- Sparklines now use `numpy` for history arrays and 4× supersample rendering
  instead of simple 1 px PIL lines
- `--timezone` now accepted as a CLI flag (was a hardcoded constant)
- README fully rewritten to document all features, options, and new behaviour

### Removed
- `metrics_dashboard_with_timestamp.py` — superseded by consolidated script
- `metrics_dashboard_2_with_timestamp.py` — superseded by consolidated script
- SHA-256 frame-hash dedup — replaced by the more efficient `FrameState`
  threshold approach (avoids rendering at all, not just the upload)

---

## [1.2.0] — 2026-02-25

### Added
- Dual sparklines (CPU + RAM) in the bottom panel
- Per-metric accent colours: CPU cyan, RAM purple, filesystem teal
- `--timezone` constant (hardcoded; made into a CLI flag in v2.0)

### Changed
- Time + date header bar added to the display layout
- Bar labels now rendered in the metric's accent colour instead of plain white

---

## [1.1.0] — 2026-02-24

### Added
- `--mode text` scrolling marquee fallback
- Clock/date header bar (time in warm yellow, date in muted grey)
- Filesystem bar shows available GB on the right instead of used %

### Changed
- Bar label zone and value zone darkened so text is always legible over
  the colour fill, regardless of percentage
- `set_mode(1)` called once at startup instead of every loop iteration —
  eliminates the black-screen flash between updates

---

## [1.0.0] — 2026-02-24

Initial release.

### Added
- Reads CPU, RAM, and filesystem metrics from a local Grafana Alloy
  `prometheus.exporter.unix` component — no psutil, no separate exporter
- Delta-based CPU % calculation (equivalent to PromQL `rate()`)
- 64×64 pixel display with three colour-coded progress bars
- Single CPU sparkline (rolling 50 samples) below the bars
- BLE connection via `bleak` / `idotmatrix-api-client`
- `--mac`, `--interval`, `--fs` CLI flags
- systemd service unit example in README

---

[2.0.0]: https://github.com/marinnedea/alloy-sparkline/compare/v1.2.0...v2.0.0
[1.2.0]: https://github.com/marinnedea/alloy-sparkline/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/marinnedea/alloy-sparkline/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/marinnedea/alloy-sparkline/releases/tag/v1.0.0
