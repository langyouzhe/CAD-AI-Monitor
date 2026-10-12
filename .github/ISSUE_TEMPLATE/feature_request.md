---
name: Feature request / 功能建议
about: Suggest an idea, a new CAD backend, or a data stream / 提一个想法、新 CAD 支持、新的数据维度
title: "[Feature] "
labels: enhancement
---

## What do you want to do / 你想做什么

<!-- Describe the goal, not the implementation. 说你想要的"结果"，不用想实现方式。 -->

## Which CAD / 哪个软件

- Software and version:
- Is it one of the supported ones (SolidWorks / Creo / NX) or a new one?

## Why it matters / 为什么有用

<!-- Who benefits, and how often would it be used? 谁会用、多久用一次？ -->

## Ideas / 你想到的做法（可选）

## Notes / 备注

For a **new CAD backend**, the collector needs a local file or API that the CAD itself
produces (for example: Creo writes `trail.txt.N`, NX writes a syslog, SolidWorks exposes
COM). If you know of such a mechanism for your CAD, say so here — that is the hard part.

要给**新 CAD** 加支持，必须能找到那个软件自己产生的本地文件或接口（比如 Creo 写 trail、
NX 写 syslog、SW 有 COM）。如果你知道你的软件有类似机制，请写在这里——这是最难的一步。
