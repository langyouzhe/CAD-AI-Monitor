# Changelog

All notable changes to this project are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/) —
`0.x` means "early, interfaces may still change".

## [0.2.3] - 2026-10-27

NX 2406 field-test preparation: replay operations that were journaled **before** collection
started, parse the Python/VB comment flavours, make the journal file pickable from the UI, and
add an **experimental** "record automatically at NX startup" script generator (whether NX
executes it is not verified yet).

### Added

- **`nx.replay_existing` (default `false`).** When `true`, `NxBridge` replays every line that
  was already in the journal when it attaches (or re-attaches after the file is truncated),
  emitting the same `nx_journal` menu/API events in line order, then keeps tailing only new
  lines. The replay happens **once per attach**, not on every poll. The default stays `false`
  (old behaviour); reusing one journal file across sessions can duplicate the previous
  session's operations during replay (documented in both READMEs).
- **Choose NX journal file** button in the main window: opens a file dialog for
  `.cs`/`.py`/`.vb`, writes `nx.journal_path` into `config.json` (other keys preserved,
  `AI_EYE_KEY` never persisted) and tells the user it takes effect on the next Start. While an
  NX journal is attached, the status area also shows `已接入：<journal path>`.
- **Generate NX auto-record script (experimental)** button: writes
  `<data_dir>\nx\autorecord_startup.py` (`import NXOpen` +
  `Session.GetSession().JournalManager.RecordJournal(...)` targeting
  `<data_dir>\journals\nx_<timestamp>.cs`, wrapped in `try/except`) and explains that the file
  must be copied to `%UGII_USER_DIR%\startup\` and NX restarted. Whether NX executes startup
  `*.py` files is **unverified**, so the button, dialog and docs label it experimental.
- New `tests/fixtures/journal_sample.py` (UTF-8 Python journal) and self-tests covering journal
  replay (replay on / off / append-only-new / truncate-reattach), the generated auto-record
  script, and journal-path saving.

### Changed

- `journal_parser.MENU_RE` now accepts `//`, `#` and `'` comment prefixes with optional
  leading whitespace (`.cs` / `.py` / `.vb` journals) while still matching only `Menu:` lines;
  `API_RE` is unchanged. Previously only `// Menu:` was recognised, so NX's Python/VB journals
  produced no menu events.
- `config.example.json` documents `nx.replay_existing`.
- README / README.en NX sections document replay (and its cross-session duplication risk), the
  file-picker button and the unverified experimental startup script.

### Tests

- `python app\selftest.py` — 40 test functions, all green (42 OK lines).
- `python app\selftest.py --ci` — 29 test functions, all green (31 OK lines).
- New deterministic coverage: replay on/off/append-only/truncate-reattach, auto-record script
  generation, journal-path saving, Python/VB menu parsing.

## [0.2.2] - 2026-10-24

Portability cleanup (no machine-specific paths/versions in shipped code) plus the security and
robustness fixes from the 2026-10-23 full-program review (1 blocker + 6 high-severity items),
followed by the 2026-10-23 second-review fixes: window-only AI Eye captures (no full-screen grab
and crop), full feature-tree COM failure handling, live-test instance ownership, honest docs and
self-test assertion propagation.

### Added

- **NX manual-recording hint + one-click journal folder.** While NX is in the foreground and
  stream 1 has not attached a recorded journal yet, the UI shows a hint telling the user to
  start `File → Utilities → Record Journal` (or set `nx.journal_path`); a new
  **Open NX journal folder** button creates and opens `<data_dir>\journals`. Recording stays
  manual on purpose: NX's recording API is in-process only (`run_journal.exe -nx` is batch
  mode and exits when the script ends; the startup folder only reads menu files), so an
  external process cannot make a running NX start recording. Turn the hint off with
  `nx.hint_when_not_recording: false` in `config.json`.

### Changed

- `resolve_data_dir()` no longer special-cases `E:\CAD-AI-Monitor`; source runs use
  the repo's `data\`, packaged runs use the exe-side `data\` or `Documents\CAD-AI-Monitor\data`.
- `cad_discovery.creo_trail_dirs()`: dropped the author-machine hints `D:\CREO` / `E:\creo12`
  (public/user folders, config.pro and registry remain).
- New `cad_discovery.creo_exe_path()`: finds `parametric.exe` via shortcut → uninstall
  registry → bounded install-dir scan (no hardcoded path). `selftest_creo.py` uses it.
- `selftest_solidworks.py` / `selftest_production.py`: removed the hardcoded SolidWorks
  fallback path; they now fail with a clear hint if SW is not installed.
- `selftest_ai_eye_live.py`: default data folder comes from `resolve_data_dir()`.

### Fixed

- **NX journal folder is now one single source.** `input_listener` passes the real data root
  (`<data_dir>`) to `NxBridge`; the bridge's default search folder, the NX hint's `journal_dir`
  and the **Open NX journal folder** button all resolve through
  `collector_core.nx_journal_dir()` → `<data_dir>\journals`. Previously the bridge searched the
  source tree's `data\journals`, so after packaging (or with a custom data folder) the button and
  the bridge pointed at different directories.
- **Database write failures are no longer swallowed.** A failed `add_event` now sets
  `self.error`, marks the session as failed, and does not count as content. `stop()` keeps the
  session folder and writes `"status": "error"` (plus the reason) to `session.json`; it is never
  reported as `completed` and never deleted as an empty session. The stop dialog shows the error.
- **SolidWorks disconnects are detected and recovered across the whole snapshot.**
  `snapshot_sw(strict=True)` separates "no active document" (a normal `None`) from a real COM
  failure (`ComSnapshotError`), and the failure handling now covers the **entire** snapshot,
  including the feature-tree walk: if `FirstFeature` / `GetNextFeature` / sub-feature members raise
  mid-traversal, the partial tree is discarded instead of being diffed. The bridge marks
  `connected: false`, releases the COM object and re-runs `GetActiveObject` on the next poll; after
  reconnecting it re-baselines the snapshot, so no fake `documentActivated` / `featureRemoved` /
  `featureAdded` events are emitted, and `connected` goes back to `true` once SW is reachable
  again.
- **Self-tests no longer swallow assertion failures.** The screenshot/capture test in
  `selftest.py` used to treat *any* exception (including `AssertionError`) as "desktop cannot be
  captured" and print SKIP/OK. Now only a genuine desktop-unavailable condition can be skipped;
  range/size/invalid-handle assertions propagate and make the self-test exit non-zero. A dedicated
  regression test feeds an invalid window rectangle through the real test function and requires the
  assertion to be raised.

### Security

- **AI Eye can no longer capture the full screen or a non-CAD window (privacy blocker).**
  A trigger records the foreground window's identity (HWND + exe/title + trigger time) and the
  queued job carries it. The capture itself is taken **directly from that window's own drawing
  surface with `PrintWindow`** — the full-screen grab + crop path (and its fallback) is gone
  entirely; nothing but the target window's content ever enters the bitmap. The tool re-verifies
  the exact window identity **before** the capture (HWND alive, still in the foreground,
  exe/title unchanged) and **again after** it (window still there / not switched away). If either
  check fails, the image is discarded (or never taken): nothing is saved, uploaded or written as a
  recognition event. Invalid or minimized window rectangles are rejected before the capture
  function is called.
- **The NX recording hint uses the same executable-first rule as deaf mode.** A window whose
  executable is readable but is not an NX executable (`ugraf.exe` / `nx.exe`) never triggers the
  "record a journal" hint, even when its title contains "NX" (e.g. a browser or search tab). Title
  matching remains only as the fallback for when the executable name cannot be read.
- **The `AI_EYE_KEY` environment variable never touches disk.** `ConfigManager._merge()` now
  deep-copies the defaults (a shallow copy let the runtime key pollute `DEFAULT_CONFIG`),
  `ensure_file()` keeps writing a clean default file, and the settings dialog shows the field as
  "from environment" (disabled) and saves the file's original `api_key` instead of the environment
  value.
- **Deaf mode can no longer be fooled by window titles.** A window whose executable is readable but
  is not one of the configured CAD executables is rejected even when its title contains
  "NX"/"SOLIDWORKS" (e.g. a browser tab about NX). Title matching remains only as a fallback for
  the case where the executable name cannot be read at all.
- **Live-test scripts no longer kill the user's CAD.** `selftest_nx.py`, `selftest_creo.py` and
  `selftest_solidworks.py` only close the process they started themselves (recorded Popen PID →
  `taskkill /PID <pid> /T /F`). The SolidWorks live test no longer calls COM `ExitApp()` at all:
  `launched=True` cannot prove the COM instance belongs to that Popen, so `GetActiveObject` may
  return the user's already-running instance. Without a provable ownership the script leaves every
  instance alone. An already-running NX / Creo / SolidWorks is never touched.

### Tests

- `python app\selftest.py` — **34 tests**; `python app\selftest.py --ci` — **23 tests**.
  New deterministic regression coverage: AI Eye window-only capture with pre/post identity
  verification (foreground switch / exe-title mismatch / invalid rectangle → zero capture calls;
  post-capture switch → image discarded; normal → only a window-sized image is saved; source
  contains no screen-grab/crop path), `AI_EYE_KEY` runtime-only handling (no pollution,
  `ensure_file` / settings save never persist it), NX journal-dir single source, session kept with
  `status=error` after a write failure, SolidWorks COM disconnect/reconnect without false
  deletions (including a mid-traversal `GetNextFeature` failure), live-test scripts only closing
  their own Popen PID (fake COM instance proves `ExitApp` is never called), NX hint executable
  priority (browser title containing "NX" stays silent), and an invalid capture rectangle making
  the self-test fail instead of being skipped.
- The end-to-end stream-2 test now degrades deterministically when the OS drops synthetic
  keyboard input (common on restricted/secure desktops): it prints an explicit SKIP for the
  injection leg and still verifies the hook-callback → database path.

## [0.2.1] - 2026-10-23

NX stream 1 now reads the **recorded journal** instead of syslog; reconciliation no
longer reports false anomalies when stream 1 has no data source; AI Eye docs and examples
now point at working multimodal providers (the shipped `ai_eye.provider` default stays
`mock`, i.e. no network call until you configure a key).

### Fixed

- **NX stream 1 data source.** `app/journal_parser.py` parses NX Record Journal output
  (`.cs` / `.py` / `.vb`): `// Menu: ...` comments and NXOpen calls, with UTF-16 / UTF-8
  auto-detection and a content check so plain scripts are not mistaken for journals.
  `app/nx_bridge.py` tails `nx.journal_path` (explicit) or the newest content-matching
  journal in Desktop / Documents / Downloads / `data\journals`. Legacy syslog is no longer
  read (it contained only startup/crash diagnostics).
- **Reconciliation false positives.** When no stream-1 backend is connected, reconciliation
  is skipped and a single `meta/stream1_unavailable` marker is written instead of one
  false `stream2_no_effect` per action.
- **AI Eye provider / model.** Docs and example config now point at multimodal providers
  (Zhipu `glm-4v-flash`, Qwen `qwen-vl-plus`). `deepseek-flash` is not a valid public model
  name and DeepSeek's public API does not accept images. `AI_EYE_KEY` environment variable
  overrides the key in `config.json`.
- **NX executable discovery.** `selftest_nx.py` no longer hardcodes a path; it calls
  `cad_discovery.nx_exe_path()` (registry + common install roots).
- **Empty-session rule.** Only stream 1 / stream 2 events count as content, so meta
  markers (including the new `stream1_unavailable`) no longer keep an empty session folder.

### Tests

- `python app\selftest.py` — 25 tests (journal parser, journal tailing, reconcile guard
  added); `--ci` — 17 tests.

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
