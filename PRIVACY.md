# Privacy Policy

**Applies to:** CAD-AI Collector (this repository) — a Windows desktop tool that records how you model in SolidWorks / Creo / NX.
**Last updated:** 2026-10-12

> Short version: this tool is, technically, a **keyboard + mouse recorder**. It is designed so that it only records while a CAD window is in front, everything is stored in **local files**, and the **only** network activity is an *optional* AI-vision feature that you enable with **your own** API key.

## TL;DR

| Question | Answer |
|---|---|
| Does it upload my data anywhere? | **No.** There is no server, no account, no telemetry. The only network call is the optional AI Eye, which you must enable yourself. |
| Does it record my keyboard everywhere? | **No.** It is "deaf" (装聋, *zhuāng lóng*) unless one of the CAD windows you configured is in the foreground. |
| Where is the data? | Local files only: one SQLite file + an optional screenshots folder. |
| Can I delete everything? | Close the app and delete the data folder. Nothing is left behind elsewhere. |
| Is my API key safe? | It is stored **in plain text** inside your local `config.json`. Protect that file; never commit it, never share it. |

## 1. What is recorded (only while a CAD window is in front)

| Category | Examples |
|---|---|
| Keyboard | key down / key up, modifier state, whether it was a shortcut (e.g. `Ctrl+S`) |
| Mouse | click press/release positions, and "gestures" — button drags, with direction and path |
| Window context | when a configured CAD window is entered/left, and leave/return markers based on idle time |
| CAD software events (stream 1) | document opened, selection changed, feature tree added/removed (via the CAD's own interface), Creo trail-file commands, NX syslog lines |
| Optional AI vision (stream 2 · AI Eye) | a screenshot of the **foreground CAD window**, plus a short fixed prompt, sent to the endpoint *you* configure; the returned text is stored locally next to the triggering event |

Also stored: session start/stop markers, and reconciliation results that flag missing/extra evidence as `meta/anomaly`.

## 2. What is NOT recorded

- **Nothing at all** while a non-CAD window is in the foreground — browsers, chat apps, password managers, the desktop. The input hooks drop these events before they are stored.
- **No continuous screen recording.** Screenshots happen only when the AI Eye is enabled *and* one of your configured triggers fires (click / shortcut / letter key / gesture / selection change, each individually switchable).
- No clipboard contents, no file contents, no browsing history, no process list, no network traffic, no microphone, no camera, no keystroke *content* beyond the key events listed above.
- The tool never modifies, instruments or bypasses your CAD software, and it never writes to the CAD installation.

## 3. Where your data is stored

Resolution order (first match wins):

1. `CAD_AI_MONITOR_DATA` environment variable, if set
2. `data\` next to the executable (portable mode)
3. `Documents\CAD-AI-Monitor\data\` (default)

Inside it: `events.db` (SQLite, WAL mode) and, if `save_images` is on, `images\<YYYYMMDD>\*.png`.
There are no hidden uploads, no analytics endpoints, and no third-party SDKs bundled for tracking.

## 4. The only network activity: the optional AI Eye

- **Off by default.** The default provider is `mock`, which performs no network call and returns a fake result so you can try the workflow for free.
- To use it you register with a vision-model provider (Zhipu, Alibaba Bailian, Volcano Engine, or any OpenAI-compatible endpoint), copy **your own** API key into the settings, and switch `ai_eye.enabled` to `true`.
- When it runs, it sends: one PNG screenshot of the foreground CAD window + a short fixed instruction prompt. It sends nothing else — no event database, no file paths, no user identity.
- Costs are billed to **your** account by **your** provider.
- The response text is stored locally next to the triggering event, so you can audit exactly what the model saw and answered.
- If you never fill in an API key, the program makes **zero** network connections.

## 5. Sensitive content warning (read this if you model for an employer)

Screenshots and event logs can contain **company IP**: part geometry, dimensions, feature names, project names, customer data, sometimes a colleague's screen if the CAD window spans displays.

- Do **not** enable the AI Eye on confidential work unless your employer allows sending those images to a third-party model provider.
- Do **not** publish or upload a collected `data\` folder without reviewing and sanitizing it first.
- If you contribute data to any community dataset, you are responsible for having the right to share it.

## 6. Retention and deletion

- The program keeps nothing after you close it except the files in the data folder.
- Data lives until **you** delete it. To erase everything: close the app, then delete the data folder (see §3).
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
