# Privacy Policy

**Applies to:** CAD-AI Collector (this repository) — a Windows desktop tool that records how you model in SolidWorks / Creo / NX.
**Last updated:** 2026-10-23 (privacy hardening: captures are taken directly from the CAD window itself via `PrintWindow` — never a full-screen grab — and the window identity is re-verified before *and* after every capture)

> Short version: this tool is, technically, a **keyboard + mouse recorder**. It is designed so that it only records while a CAD window is in front, everything is stored in **local files**, and the **only** network activity is an *optional* AI-vision feature that you enable with **your own** API key.

## TL;DR

| Question | Answer |
|---|---|
| Does it upload my data anywhere? | **No.** There is no server, no account, no telemetry. The only network call is the optional AI Eye, which you must enable yourself. |
| Does it record my keyboard everywhere? | **No.** It is "deaf" (装聋, *zhuāng lóng*) unless one of the CAD windows you configured is in the foreground. |
| Where is the data? | Local files only: one folder per collection session (`data\sessions\<start time>\`) plus an optional screenshots folder inside it; older data is moved to `data\legacy\`. |
| Can I delete everything? | Close the app and delete the data folder (or a single session folder). Nothing is left behind elsewhere. |
| Is my API key safe? | It is stored **in plain text** inside your local `config.json`. Protect that file; never commit it, never share it. |

## 1. What is recorded (only while a CAD window is in front)

| Category | Examples |
|---|---|
| Keyboard | key down / key up, modifier state, whether it was a shortcut (e.g. `Ctrl+S`) |
| Mouse | click press/release positions, and "gestures" — button drags, with direction and path |
| Window context | when a configured CAD window is entered/left, and leave/return markers based on idle time |
| CAD software events (stream 1) | document opened, selection changed, feature tree added/removed (via the CAD's own interface), Creo trail-file commands, NX **Record Journal** lines (`Menu:` comments / NXOpen calls) |
| Optional AI vision (stream 2 · AI Eye) | an image of the **foreground CAD window**, captured directly from that window's own drawing surface (`PrintWindow`), plus a short fixed prompt, sent to the endpoint *you* configure; the returned text is stored locally next to the triggering event |

Also stored: session start/stop markers, and reconciliation results that flag missing/extra evidence as `meta/anomaly`.

## 2. What is NOT recorded

- **Nothing at all** while a non-CAD window is in the foreground — browsers, chat apps, password managers, the desktop. The input hooks drop these events before they are stored.
- **No continuous screen recording.** Screenshots happen only when the AI Eye is enabled *and* one of your configured triggers fires (click / shortcut / letter key / gesture / selection change, each individually switchable).
- **Never a full-screen screenshot.** A capture is taken **directly from the one CAD window that was in the foreground when the trigger fired** (`PrintWindow`, which asks that window to draw itself into a private bitmap). If both PrintWindow attempts fail, it falls back to a BitBlt from the **same window's device context** — still only that window's pixels, never the screen. The screen is never grabbed and cropped, and there is no screen-wide fallback. Right before the capture the tool re-checks that this exact window is still alive, still in the foreground and that its executable name and title still match the identity recorded at trigger time; it re-checks all of that again right after the capture. If any check fails, the image is discarded (or never taken) and the task is **skipped** — nothing is saved, uploaded or recorded. Invalid or minimized window rectangles are rejected before any capture call happens.
- No clipboard contents, no file contents, no browsing history, no process list, no network traffic, no microphone, no camera, no keystroke *content* beyond the key events listed above.
- The tool never modifies, instruments or bypasses your CAD software, and it never writes to the CAD installation.

## 3. Where your data is stored

Resolution order (first match wins):

1. `CAD_AI_MONITOR_DATA` environment variable, if set
2. `data\` next to the executable (portable mode)
3. `Documents\CAD-AI-Monitor\data\` (default)

Since **v0.2.0**, each *start → stop* collection run gets its own folder (nothing is mixed into one shared database anymore):

```
data\sessions\2026-10-19_122035\     one collection session (name = local start time; _2/_3 on collision)
├─ images\                           AI Eye screenshots of this session (JPEG)
├─ data\events.db                    SQLite (WAL mode), tables `sessions` and `events`
└─ session.json                      summary: app version, start/stop time, CAD software, event count, schema version
```

- **Empty sessions are deleted**: if a normal stop recorded nothing except the start/stop markers, the whole session folder is removed.
- **Interrupted sessions are kept**: on the next start, folders whose `sessions.stopped_at` is empty are preserved, checkpointed and marked `"status": "aborted"` in `session.json` — data is never silently discarded.
- **Older versions' data** (`data\events.db` + `data\images\`) is moved — never deleted — to `data\legacy\` on first launch (`events_legacy.db`, with screenshot paths rewritten so they keep working). A migration log is written to `data\legacy\migration.log`.
- A small helper lists the sessions without opening the GUI: `python app\collector_core.py` (it walks `data\sessions\*\data\events.db`).
- There are no hidden uploads, no analytics endpoints, and no third-party SDKs bundled for tracking.

## 4. The only network activity: the optional AI Eye

- **Off by default.** The default provider is `mock`, which performs no network call and returns a fake result so you can try the workflow for free.
- To use it you register with a vision-model provider (Zhipu, Alibaba Bailian, Volcano Engine, or any OpenAI-compatible endpoint), copy **your own** API key into the settings, and switch `ai_eye.enabled` to `true`.
- Your API key can be kept out of `config.json` entirely by setting the `AI_EYE_KEY` environment variable. It is then only used at runtime; the settings dialog shows the field as disabled and saving settings never writes the environment key to disk.
- When it runs, it sends: one JPEG screenshot of the CAD window that triggered it + a short fixed instruction prompt. It sends nothing else — no event database, no file paths, no user identity.
- Costs are billed to **your** account by **your** provider.
- The response text is stored locally next to the triggering event, so you can audit exactly what the model saw and answered.
- If you never fill in an API key, the program makes **zero** network connections.

## 5. Sensitive content warning (read this if you model for an employer)

Screenshots and event logs can contain **company IP**: part geometry, dimensions, feature names, project names, customer data. Because a capture contains the whole CAD window (including any part of it drawn on a second monitor), review an example image before enabling the AI Eye on confidential work.

- Do **not** enable the AI Eye on confidential work unless your employer allows sending those images to a third-party model provider.
- Do **not** publish or upload a collected session folder (`data\sessions\<start time>\`) or the archived `data\legacy\` without reviewing and sanitizing it first.
- If you contribute data to any community dataset, you are responsible for having the right to share it.

## 6. Retention and deletion

- The program keeps nothing after you close it except the files in the data folder.
- Data lives until **you** delete it. To erase everything: close the app, then delete the data folder (see §3). To erase a single collection run, delete its `data\sessions\<start time>\` folder. Sessions that recorded nothing are removed automatically; interrupted sessions are kept and marked `aborted` so you can decide.
- Uninstalling = deleting the executable and the data folder. The program does not install services, drivers, or scheduled tasks.

## 7. Why antivirus tools may flag it

Global keyboard/mouse hooks are the same OS API that keyloggers use, and the release build is an unsigned PyInstaller executable. Security software may warn or block it.

- The full source is in this repository — you can read exactly what is hooked and what is stored, and you can build it yourself instead of trusting a binary.
- Source builds are reproducible with `app\build.bat`; see the README.

## 8. Children, jurisdiction, changes

- The tool is intended for professional CAD users; it is not directed at children.
- You are responsible for complying with the laws and workplace rules that apply to you (including employee-monitoring and data-protection rules).
- Material changes to this policy will be committed to this file with an updated date.

## 9. Contact

Open an issue in this repository: <https://github.com/langyouzhe/CAD-AI-Monitor/issues>
