# Changelog

All notable changes to this project are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/) —
`0.x` means "early, interfaces may still change".

## [0.2.0] - 2026-10-19

Session storage v4: every *start → stop* is now its own folder, so repeated collections
never mix into one shared database.

### Added

- **Per-session storage (v4)** — each collection run creates
  `data\sessions\<yyyy-mm-dd_HHMMSS>\` (local start time; `_2`, `_3` on collision)
  containing:
  - `images\` — this session's AI Eye screenshots, flat (no more per-date subfolders);
  - `data\events.db` — this session's SQLite database (same `sessions` + `events` tables);
  - `session.json` — a summary readable **without opening the database**: app version,
    start/stop time, CAD software (enabled + actually connected), event count,
    schema version and status.
- **Empty sessions leave no trace** — a normal stop that recorded nothing except the
  start/stop markers deletes the whole session folder.
- **Crash recovery** — on startup, unfinished sessions (empty `sessions.stopped_at`) are
  kept, WAL-checkpointed and marked `"status": "aborted"` in `session.json`; only truly
  empty leftovers (no database, no files at all) are removed.
- **Legacy data archive** — on first launch the old `data\events.db` + `data\images\` are
  moved (never deleted) to `data\legacy\`: the database becomes `events_legacy.db`, the
  images keep their relative layout under `legacy\images\`, and the absolute screenshot
  paths inside the database are rewritten so they keep working. The migration is
  idempotent, never overwrites an existing archive, rolls back to the untouched state on
  failure and logs its result to `data\legacy\migration.log`.
- **Single-instance lock** — a second launch shows “已在采集中” and exits, so two processes
  can never write the same data folder at once.
- **AI Eye queue drain on stop** — in-flight cloud requests are awaited (default 5 s cap,
  `ai_eye.drain_timeout_s`) before the database is closed, so recognition results land in
  the session that triggered them.
- **Cross-session helper** — `python app\collector_core.py [--json|--events]` walks
  `data\sessions\*\data\events.db` and prints a per-session summary; `session_summaries()`
  reads `session.json` first, so it still works when a database is damaged.
- New self-tests: session naming/collisions, empty-session removal, crash recovery,
  legacy archive (idempotency + failure rollback), `session.json` fields, cross-session
  traversal, single-instance lock and AI Eye drain.

### Changed

- Version bump to **0.2.0**. The window title shows the session folder name while
  collecting, and the bottom bar shows the current session folder.
- Screenshots are plain JPEGs inside the session's `images\`; event details now also carry
  `image_rel` (path relative to the session folder), so one session folder is a complete,
  movable data package.
- Stop sequence: stop collectors → drain AI Eye → reconcile → write `session.json` →
  WAL checkpoint and close the database → delete the folder if the session was empty.
- `README.md` / `README.en.md` / `PRIVACY.md` / `config.example.json` document the new
  layout, the upgrade path and how to find (or roll back) old data.

### Fixed

- `data\images\` no longer grows as one pile shared by every session, and a single session
  can be archived or shared without breaking its screenshot links.
- Cleaned up a stale import in `ai_eye.py` after screenshots moved to per-session folders.

## [0.1.1] - 2026-10-13

Patch release taken while preparing the first public announcement.

### Fixed

- **The connected Creo backend showed up as `?✓` in the status line.** The Creo
  bridge's state dict did not carry its `backend` name, so the window fell back to its
  default label — and the same bug also mangled the hint line into `?：trail 文件…`.
  Found by inspecting the very screenshot we were taking for the README.
- Self-test now asserts that every bridge reports its own backend name, so this cannot
  come back unnoticed.

### Added

- A screenshot of the running window at the top of both READMEs.

## [0.1.0] - 2026-10-12

First public pre-release. Windows only, tested against SolidWorks 2022, Creo Parametric
12.4.4.0 and Designcenter NX 2512 on Windows 11.

### Added

- **Stream 1 — CAD software events.** Three backends:
  - SolidWorks: COM snapshot + diff (active document, selection, feature tree).
  - Creo: parses the `trail.txt.N` action log and `config.pro` mapkeys.
  - NX: parses syslog (probe stage — attach verified, command parsing still being calibrated).
- **Stream 2 — real input.** Global keyboard/mouse hooks with *deaf mode*: nothing is
  recorded unless a configured modeling window is in the foreground. Clicks, keys,
  shortcuts, window enter/leave, gestures (8-direction drag detection), leave/return
  markers based on a configurable idle threshold.
- **AI Eye (optional, BYOK).** On click / shortcut / letter key / gesture / selection
  change, screenshots the whole modeling window and asks *your* OpenAI-compatible vision
  model what happened; the answer is stored on the triggering event's timestamp. Off by
  default; `mock` provider works without a key and without network access.
- **Three-way reconciliation.** On stop, pairs actions ↔ stream-1 effects ↔ visual
  evidence and flags missing links as `meta/anomaly` (`stream2_no_effect`,
  `stream1_orphan`, `visual_missing`, `visual_failed`). Advisory only — never deletes data.
- **CAD auto-discovery** (`app/cad_discovery.py`). Version-independent SolidWorks ProgID,
  per-version registry enumeration, Creo trail folder found through the shortcut's *Start
  in* directory, NX syslog folders, and a 13-entry modeling-app list. No hard-coded paths
  or versions.
- **Self-tests** — 16 checks (`python app\selftest.py`), plus `--selftest` inside the
  packaged exe, and per-CAD live test scripts.
- **Local-first storage** — SQLite (WAL) with `sessions` / `events` tables; screenshots
  optionally kept under `data\images\<date>\`.
- Portability: data directory resolves from `CAD_AI_MONITOR_DATA` → `data\` next to the
  exe (portable) → `Documents\CAD-AI-Monitor\data`.
- One-click build (`app\build.bat`) that finds Python by itself; pinned dependencies.
- **The SolidWorks command-ID table is no longer redistributed.** It used to be
  extracted from `swcommands.tlb` and committed. It is now generated on first run from
  the type library inside *your own* SolidWorks installation and stored in
  `data\sw_command_ids.json` (also available as `python app\sw_shortcuts.py --generate`).
  Nothing is lost if no SolidWorks is installed — gesture direction and the raw command
  ID are still recorded.
- **Continuous integration** (`.github/workflows/ci.yml`): compile check, import smoke
  test and the deterministic self-test subset run on a clean Windows machine with no CAD
  installed — i.e. exactly the "someone cloned my repo" case.

### Fixed

- **Creo stream 1 stayed empty.** The bridge used to pick the newest `trail.txt*` across
  all candidate folders — including `...\Parametric\bin\`, where PTC ships a *sample*
  `trail.txt.1`. It attached to that dead file while the real trail kept being written in
  the shortcut's start directory (`C:\Users\Public\Documents`). Install folders are now a
  last-resort fallback, and a stale attached file produces an explicit
  `creo_trail_stale` event plus a status hint.
- Screenshot crop on scaled / multi-monitor setups (system DPI + virtual-screen origin).
- SolidWorks feature-tree self-reference producing absurd nested paths.
- Reasoning vision models returning an empty answer (token budget); `reasoning_content`
  fallback added.
- Bridging an old log file replayed history as if it were happening now (seek to EOF).
- Modifier-only keys (a bare Shift/Ctrl) were counted as actions and triggered screenshots.

### Known limitations

- Windows only.
- The NX backend is at probe stage.
- Gesture *direction* is always recorded, but translating SolidWorks gesture command IDs
  to command names needs a lookup table (`data\sw_shortcut_map.json`).
- The exe is unsigned: expect Windows SmartScreen warnings.
- Collected data is **not** de-identified — review before sharing.
