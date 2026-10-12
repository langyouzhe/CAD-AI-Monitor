# CAD-AI Collector — record how you actually model (English)

**English** | [中文](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows-blue)

A local-first collector that records *how* you actually model in SolidWorks / Creo / NX: software events, your real mouse & keyboard actions, and optional AI vision on top. Built so an AI can later learn **your personal modeling style**. All data stays on your own machine.

> ⚖️ **Trademark notice** — This is an independent personal project. It is **not affiliated with, authorized, sponsored or endorsed by** Dassault Systèmes, PTC, Siemens or their affiliates. SolidWorks®, Creo® and NX™ are trademarks of their respective owners and are named here **only to describe compatibility**. The tool reads data that these applications themselves produce or expose locally (COM interface, Creo trail files, NX syslog). It does not modify, crack or bypass any software licensing.

> 🏷️ **Versioning** — Public releases follow semantic versioning; the current release is **v0.1.0** (`0.x` = early, interfaces may still change). Labels such as *v2* / *v3* that appear in code comments or the Chinese documentation are **internal development generations**, not release versions.

> 🔒 **Privacy** — This program is technically a keyboard/mouse recorder, so please read [PRIVACY.md](PRIVACY.md) before using it. Short version: it only records while a CAD window is in front, everything is stored locally, and the only network activity is the optional AI Eye that you enable with your own API key.

## What it does

Double-click it, model normally, and it logs a timestamped, multi-stream record to local files:

| Stream | What it captures | Examples |
|---|---|---|
| **Stream 1 (hands)** | Model changes reported by the CAD's own interface: which document is open, **which face/edge/feature you selected**, feature-tree additions/removals, executed commands | SolidWorks: `selectionChanged`; Creo: `creo_trail` (menu/command log); NX: `nx_syslog` (probe stage) |
| **Stream 2 (eyes)** | What you physically did inside the CAD window: clicks, keys, shortcuts, **gestures (button drags)** | `key_down` (local lookup turns `s` into a command name), `gesture` (direction + command id) |
| **Stream 2 · AI Eye** | On a trigger, screenshots the **whole CAD window** and asks your vision model what happened; the answer is stored on the original event's timestamp | `data\images\` keeps the PNGs |
| **Reconciliation** | On stop, cross-checks action ↔ stream-1 effect ↔ vision result and flags missing evidence as `meta/anomaly` | `stream2_no_effect`, `stream1_orphan`, `visual_missing`, `visual_failed` |
| **meta** | Session start/stop and leave/return markers (no idle events) | idle threshold is configurable |

## Core rules

1. **Deaf mode (装聋)** — input is only recorded while a configured CAD window is in the foreground. Switch to a browser and it stops immediately.
2. **No pause events** — every row carries a timestamp; idle time is just the gap between rows.
3. **Screenshots are your call** — with `save_images: true` (default) PNGs are kept in `data\images\<date>\`; set it to `false` and screenshots never touch the disk.
4. **Local only** — no server, no account, no telemetry. The single optional network call is the AI Eye (see [PRIVACY.md](PRIVACY.md)).

## How to use

1. Start SolidWorks (or Creo / NX — whatever is running gets picked up automatically).
2. Run `release\CAD-AI-Collector.exe` → click **开始采集 (Start)** → model normally → **停止采集 (Stop)**.
3. The window shows, live: which backends connected, whether the foreground window counts as a modeling window, and AI Eye statistics.

No paths or versions to configure: the program **auto-discovers** your installation (see below).

## Where your data lives

First match wins: `CAD_AI_MONITOR_DATA` env var → `data\` next to the exe (portable) → `Documents\CAD-AI-Monitor\data\`.

Inside: `events.db` (SQLite; tables `sessions` and `events` with `ts_ms | ts_iso | source | kind | detail`) and optionally `images\<YYYYMMDD>\`.

## Supported versions (auto-discovery, no hard-coded versions)

| Software | Status | How it connects |
|---|---|---|
| SolidWorks **2022** | ✅ tested by the author | COM; tries the version-independent `SldWorks.Application` first, then enumerates `SldWorks.Application.*` |
| Creo Parametric **12.4** | ✅ tested by the author | parses the Creo trail file, located through the shortcut's *Start in* folder |
| Designcenter NX **2512** | 🟡 probe stage | reads syslog; attach verified, command-line parsing still being calibrated |
| Other SolidWorks versions (2015–2025) | ⚪ should work, unverified | same mechanism; shortcut/gesture registry keys are enumerated per version |
| Other Creo versions (7–11) | ⚪ should work, unverified | same mechanism; trail format is stable across versions |
| Inventor / AutoCAD / Solid Edge / FreeCAD / Fusion | ⚪ stream 2 only | keyboard/mouse + AI Eye still work; stream 1 needs a dedicated bridge |

What auto-discovery looks at:

- **SolidWorks**: `SldWorks.Application` (version independent) → `SldWorks.Application.NN` newest first → registry `HKCU\Software\SolidWorks\SOLIDWORKS <year>`
- **Creo**: the shortcut's working directory (that is where trail files are written) → `trail_dir` from `config.pro` → home/Documents/Desktop
- **NX**: `%TEMP%` → home directory → install path from the uninstall registry
- **Modeling-window list**: 13 mainstream CAD executables built in; editable via `modeling_apps`

## Settings

`config.json` is created automatically in the data folder on first run; `config.example.json` in the repo shows every option. The most useful ones:

| Setting | Default | Meaning |
|---|---|---|
| `modeling_apps` | 13 CAD entries | which windows count as modeling windows |
| `idle_minutes` | `5.0` | idle time before a "leave" marker |
| `stream1.backends` | `["solidworks","creo","nx"]` | which CAD backends to run |
| `stream1.prog_id` | empty = auto | SolidWorks COM ProgID override |
| `creo.trail_dirs` / `nx.syslog_dirs` | empty = auto | manual overrides if discovery fails |
| `creo.stale_minutes` | `10` | warn when the attached trail file stops updating |
| `ai_eye.enabled` | `false` | master switch for the AI Eye |
| `ai_eye.provider` | `mock` | `mock` / `zhipu` / `qwen` / `doubao` / `custom` |
| `ai_eye.api_key` | empty | **your** API key (BYOK) |
| `ai_eye.save_images` | `true` | keep screenshots locally |
| `ai_eye.triggers` | left click, shortcut, letter key, gesture on | what fires a screenshot |
| `ai_eye.rate_per_min` | `60` | screenshot rate limit |
| `reconcile.window_ms` | `2000` | how long after an action a stream-1 event still counts as its effect |

## Enabling the AI Eye (your key, your bill)

1. Register with a vision-model provider (Zhipu `open.bigmodel.cn`, Alibaba Bailian, Volcano Engine, or any OpenAI-compatible service) and copy an API key.
2. Open the settings dialog (or `config.json`): set `ai_eye.enabled` to `true`, fill `api_key`, set `provider` accordingly.
3. Start collecting. Cost is roughly ¥0.01–0.05 per image, capped at `rate_per_min`.

Note: reasoning models (e.g. `deepseek-flash`) spend the token budget thinking before answering — if the answer comes back empty, raise `ai_eye.max_tokens` (default 4000). Some models do not accept images at all.

## Run or build from source

```bat
git clone https://github.com/langyouzhe/CAD-AI-Monitor.git
cd CAD-AI-Monitor
python -m pip install -r app\requirements.txt

python app\app.py          :: run from source
python app\selftest.py     :: 16 self-tests, no CAD required
app\build.bat              :: build release\CAD-AI-Collector.exe
release\CAD-AI-Collector.exe --selftest   :: exit code 0 = healthy
```

Tested with Python 3.12 on Windows 11. `build.bat` finds Python by itself (override with `set CADAI_PYTHON=...`). Software-specific live tests: `python app\selftest_solidworks.py`, `selftest_creo.py`, `selftest_nx.py`, `selftest_ai_eye_live.py` (the last one needs a real API key).

## Troubleshooting

| Symptom | Answer |
|---|---|
| Windows SmartScreen blocks the exe | Expected for unsigned executables: **More info → Run anyway**. Or build it yourself from source. |
| Antivirus flags it | Global input hooks look like a keylogger to heuristics. The full source is here; build it yourself if you prefer. |
| Stream 1 is empty | The status line now tells you why: "trail file not found" (Creo never ran) or "trail file has not updated for N minutes" (the bridge attached to a dead file — set `creo.trail_dirs` manually). |
| Creo trail file location | It is in the shortcut's *Start in* folder (e.g. `C:\Users\Public\Documents`). ⚠️ The `trail.txt.1` inside the PTC install directory is a **sample shipped by PTC**, not your work — the bridge no longer mistakes it for the real one. |
| NX has no data | The NX backend is at probe stage: syslog attach is verified, command parsing awaits real usage data. |
| Gesture command names are missing | Gesture **direction** is always recorded; the Chinese/English command name needs a command-ID table. A file `data\sw_shortcut_map.json` lets you fill it in. |
| I want to delete my data | Close the app and delete the data folder. |

## Known limitations

1. **Windows only.** It relies on Win32 COM, global input hooks and Windows registry.
2. **NX backend is a probe.** Attach works; the command-line format still needs calibration from real sessions.
3. **Reconciliation is advisory.** `stream2_no_effect` may just be a view-only click; nothing is deleted automatically.
4. **AI Eye quality depends on your model.** Verified live with DeepSeek (`deepseek-flash`) returning schema-conform JSON in ~3–6 s.
5. **No code signature.** Expect SmartScreen warnings; each release lists a SHA-256 checksum so you can verify the download.
6. **Data is not de-identified.** Do not publish a collected `data\` folder without reviewing it.

## Repository layout

| Path | Responsibility |
|---|---|
| `app\collector_core.py` | config + local storage (SQLite) |
| `app\cad_discovery.py` | CAD auto-discovery (ProgIDs, registry versions, trail/syslog folders, CAD app list) |
| `app\input_listener.py` | stream 2: hooks, deaf mode, gestures, leave/return, AI Eye triggers |
| `app\solidworks_bridge.py` | stream 1 · SolidWorks (COM snapshots + diff) |
| `app\creo_bridge.py` | stream 1 · Creo (trail parsing, mapkeys) |
| `app\nx_bridge.py` | stream 1 · NX (syslog parsing, probe) |
| `app\ai_eye.py` | screenshot → your vision model → event store |
| `app\reconcile.py` | three-way reconciliation |
| `app\sw_shortcuts.py` | SolidWorks shortcut/gesture mapping |
| `app\app.py` | Tkinter GUI and settings dialog |
| `app\selftest*.py` | self-tests and per-CAD live tests |
| `config.example.json` | every configuration option, no secrets |

## License

[MIT](LICENSE) © 2026 langyouzhe. Contributions are welcome — please include your CAD name/version in bug reports so issues can be reproduced.
