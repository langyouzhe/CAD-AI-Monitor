---
name: Bug report / 缺陷报告
about: Something does not work — the CAD is not detected, no data is recorded, the app crashes / 有东西不工作：没检测到 CAD、没记到数据、程序出错
title: "[Bug] "
labels: bug
---

<!--
  Please fill in every field below. Most bugs in this project are caused by a
  CAD version or a file location we have never seen, so the environment block
  is the most important part.
  请尽量把下面每一项都填上：本项目的 bug 大多来自"我们没见过的 CAD 版本或文件位置"，
  所以"环境"那一块最关键。
-->

## What happened / 发生了什么

<!-- One or two sentences. Screenshots are welcome, but see the privacy note at the bottom. -->

## Steps to reproduce / 复现步骤

1.
2.
3.

## Your environment / 你的环境（必填 / required）

| Item | Your answer |
|---|---|
| Collector version（窗口标题里的 `vX.Y.Z`，例如 `v0.1.0`） | |
| CAD and version（例如 SolidWorks 2022 / Creo 12.4.4.0 / Designcenter NX 2512） | |
| Windows version（例如 Windows 11 23H2） | |
| Which stream is broken / 哪一路坏了 | stream 1 (CAD events) / stream 2 (input) / AI Eye / all |
| Backend / 后端 | solidworks / creo / nx |

## Status line text / 界面上"流1"那行的文字

<!-- The app now prints the reason, e.g. "creo: trail file has not updated for 27 minutes".
     Copy it here verbatim if there is one. 界面上的橙色提示原文照抄过来，非常有用。 -->

```
(paste here / 粘贴在这里)
```

## What you expected / 期望结果

## Anything else / 其它线索

- [ ] I read the "常见问题 / Troubleshooting" section of the README.
- [ ] I ran `release\CAD-AI-Collector.exe --selftest`（exit code 0 = the program itself is fine）.

## ⚠ Privacy before attaching files / 附文件前必读

Do **not** paste your `data\` folder as-is: it contains your real modeling sessions,
screenshots, and (in `config.json`) **your own API key**.

- [ ] Any file I attach has been reviewed and sanitized (no company geometry, part names, project names, or API keys).
- [ ] I did **not** attach `config.json`（or I removed the `api_key` line）.

See [PRIVACY.md](../blob/main/PRIVACY.md).
