# Contributing / 参与贡献

Thanks for helping. This project exists to collect **real** modeling-process data, so the
most valuable contributions are not code — they are *"I ran it on my CAD version, here is
what happened"*.

感谢出力。这个项目的目的就是收集**真实**建模过程数据，所以最值钱的贡献往往不是代码，
而是"**我在我的 CAD 版本上跑了一遍，结果是……**"。

## The easiest way to help / 最省力的帮忙方式

1. **Run it and report** — use it while modeling normally for a while, then open an issue
   saying which CAD/version worked and which stream produced data (or did not).
   用它正常建模一段时间，然后开个 issue 说：哪个 CAD/版本、哪一路有数据（或没数据）。
2. **Extend the support matrix** — `README.md` marks versions as *tested* vs *theoretical*.
   A short report moves one row from "unverified" to "tested".
   README 里有"实测 / 理论支持"两档，你的一次短报告就能把一行从"待验证"变成"实测"。
3. **Report a confusing message** — if the app tells you something you had to guess about,
   that is a bug in the documentation or the status line.
4. **Code** — see below.

## Development setup / 开发环境

```bat
git clone https://github.com/langyouzhe/CAD-AI-Monitor.git
cd CAD-AI-Monitor
python -m pip install -r app\requirements.txt

python app\app.py            :: run from source
python app\selftest.py       :: 16 self-tests, no CAD needed
app\build.bat                :: build release\CAD-AI-Collector.exe (finds Python itself)
```

Software-specific live tests (need the actual CAD installed):
`python app\selftest_solidworks.py`, `selftest_creo.py`, `selftest_nx.py`,
`selftest_ai_eye_live.py` (the last one needs a real API key).

## Rules we hold to / 必须遵守的几条

| Rule | Why |
|---|---|
| **Never commit personal data** — `data/`, `config.json`, screenshots, real sessions, API keys | It is already in `.gitignore`; do not force-add it |
| **Never hard-code a path or a CAD version** — use `app/cad_discovery.py` | The project must work on any machine and any SW/Creo/NX version |
| **`.bat` / `.cmd` stay ASCII-only** | cmd.exe parses batch files with the OEM codepage; Chinese text silently breaks the script (this already cost us one broken build) |
| **Keep the privacy statement true** — if you change what is recorded, update `PRIVACY.md` and both READMEs in the same PR | The tool is a key/mouse recorder; an inaccurate privacy claim is a trust killer |
| **User-facing behaviour changes need a self-test** | `python app\selftest.py` must stay green (add a test, do not delete one) |

## Adding a CAD backend / 加一个新的 CAD 后端

A stream-1 bridge must supply events the CAD software itself produces. Look for: a log or
trail file that grows while the user works, a COM/API surface, or a journal/scripting hook.

Requirements for a new bridge:

1. **Attach, do not replay** — seek to the end of the file on attach, otherwise you turn
   last week's session into "events from a minute ago".
2. **Discover, do not assume** — find the file through shortcuts, registry, config files or
   common folders; keep user overrides in `config.json`. See `app/cad_discovery.py`.
3. **Never throw** — a missing file or a dead CAD must degrade to a status message, never
   crash the collector.
4. **Emit `kind` + `detail`** — register the new `kind` values in `app/reconcile.py`
   (which kinds count as an "effect") and mention them in the README.
5. **Tell the user when nothing arrives** — set a `last_error` / status hint explaining
   *why* (file not found, file stale, app not running). Silence is the worst UX here.

## Commits and PRs / 提交规范

- Commit subject in English, one line, imperative (`Fix Creo trail discovery on UNC paths`).
- One topic per PR; keep refactors separate from behaviour changes.
- Run `python app\selftest.py` before opening the PR and fill in the PR checklist.
- By contributing you agree your work is released under the [MIT licence](LICENSE).
