# CAD-AI 采集器 v0.1.0 —— 使用说明（大白话版）

**中文** | [English](README.en.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows-blue)
[![CI](https://github.com/langyouzhe/CAD-AI-Monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/langyouzhe/CAD-AI-Monitor/actions/workflows/ci.yml)

**English** — A local-first collector that records *how* you actually model in SolidWorks / Creo / NX: software events, your real mouse & keyboard actions, and optional AI vision on top. Built so an AI can later learn **your personal modeling style**. All data stays on your own machine.

**中文一句话** — 把你在这三个 CAD 里的真实建模过程（软件事件 + 你的键鼠操作 + 可选 AI 视觉）记成结构化数据，全部存在本地，用来让 AI 学会**你自己的建模逻辑**。

> ⚖️ **商标与关系声明**：本项目由个人开发者独立编写，与 Dassault Systèmes、PTC、Siemens 及其关联公司**没有任何关系**，未获其授权、赞助或认可。SolidWorks®、Creo®、NX™ 等名称是各自权利人的商标，本文档提到它们**仅为说明兼容性**。本工具只读取你本机上这些软件自身产生或对外公开的数据（COM 接口、trail 流水账、syslog），**不修改、不破解、不绕过任何软件授权**。

## 这是干嘛的

一个双击就能用的小程序。你正常建模，它在后台自动把**多路数据**按时间戳落到本地文件：

| 流 | 记什么 | 例子 |
|---|---|---|
| **流1（手）** | 软件接口报出的模型变化：打开什么文档、**选中了哪个面/边/特征**、特征树增删改、执行了什么命令 | SW：`selectionChanged`（选中了面）；Creo：`creo_trail`（菜单操作流水）；NX：`nx_syslog`（探测版） |
| **流2（眼）** | 你在建模软件窗口里的实际操作：点击、按键、快捷键、**笔势（按住拖动）** | `key_down`（按了 s，本地翻译成命令名）、`gesture`（右上笔势，带方向和命令 ID） |
| **流2·AI 眼** | 点击/快捷键/笔势/选中变化瞬间，截**整个建模窗口**一张图→云端视觉模型识别，结果挂回原事件时间戳 | 识别"点了什么/界面状态/有无报错"，结果入库，图存 `data\images\` |
| **对账（meta）** | 停止采集时自动**三方对账**：动作↔流1效果↔视觉结论，缺票自动标异常 | `meta/anomaly`（stream2_no_effect / stream1_orphan / visual_missing / visual_failed） |
| **meta** | 会话开始/停止、离开/回来 | 无操作超 N 分钟记 `leave`，回来补 `return` |

> **版本号怎么看**：公开发布用**语义化版本**，当前是 **v0.1.0**（`0.x` 表示"早期版本、接口可能变"；等它稳定到别人可以放心用了，才发 `1.0.0`）。文档和代码注释里偶尔出现的 **v2 / v3** 是开发过程中的**内部迭代编号**（第 2 代架构 → 第 3 代加视觉），**不是发布版本号**，别混淆。

**当前已实现**：笔势记录、SW 快捷键本地翻译、AI 眼（视觉）、Creo 桥、NX 桥（探测版）、三方对账、CAD 自动发现。

## 核心规矩（不变）

1. **装聋**：只记建模窗口（SolidWorks / Creo / NX，名单在设置里可改）在前台时的操作；切走不记、不截屏。
2. **不记停顿**：时间戳差自算。
3. **截图去向由你说了算**：AI 眼默认把截图存到本地 `data\images\<日期>\`（方便你事后核对"AI 当时看到什么"）；在设置里把 `save_images` 关掉，截图就只在内存里转一圈、**磁盘不留图**。只有你打开 AI 眼时才会产生截图。
4. **数据全本地**：除了"AI 眼把截图发给云端识别"这一步（你自己选的），其余数据永不离开本机。

## 怎么用

1. 先打开 SolidWorks（或 Creo/NX——它们开着就会被自动接上）
2. 双击 `release\CAD-AI-Collector.exe` → 点【开始采集】→ 正常建模 → 【停止采集】
3. 界面实时显示：三个软件后端谁连上了、当前窗口是不是建模窗口、AI 眼识别统计

> 不挑版本、不挑安装位置：程序会**自动发现**你机器上的 SW/Creo/NX（见下文"支持哪些版本"），
> 你不需要改任何路径。

## 数据在哪里

**默认**：`文档\CAD-AI-Monitor\data\events.db`（SQLite 单文件）。表：`sessions`（每次采集）+ `events`（每条事件，字段：时间戳 | source | kind | detail）。

找数据的规则（从上往下，谁先命中用谁）：

| 顺序 | 位置 | 什么时候用 |
|---|---|---|
| 1 | 环境变量 `CAD_AI_MONITOR_DATA` 指的目录 | 你自己指定的（服务器/多份数据） |
| 2 | **exe 旁边**的 `data\` 文件夹 | 绿色便携模式：把 exe 和 data 一起拷到 U 盘 |
| 3 | `文档\CAD-AI-Monitor\data\` | 默认（装在 Program Files 里也能写） |

## 支持哪些版本？（自动发现，不写死版本）

| 软件 | 状态 | 怎么连上的 |
|---|---|---|
| SolidWorks **2022** | ✅ 本机实测 | COM 接口，优先用**不限版本**的 `SldWorks.Application`，再按版本倒序枚举 |
| Creo Parametric **12.4** | ✅ 本机实测 | 读 trail 操作流水账文件（自动找到你的 Creo 启动目录） |
| Designcenter NX **2512** | 🟡 探测版 | 读 syslog；已能挂上，操作命令行格式待真实使用校准 |
| SolidWorks 其它版本（2015~2025） | ⚪ 理论支持、待你验证 | 同上（ProgID 自动发现；快捷键/笔势注册表也按版本自动枚举） |
| Creo 其它版本（7/8/9/10/11…） | ⚪ 理论支持、待你验证 | 同上（trail 格式各版本基本一致） |
| Inventor / AutoCAD / Solid Edge / FreeCAD / Fusion | ⚪ 只能记"你在用这个软件" | 流2（键鼠+AI 眼）照常；流1 需要有人再写一个桥 |

自动发现具体找什么：

- **SW**：`SldWorks.Application`（不限版本）→ `SldWorks.Application.NN` 按版本倒序 → 注册表 `HKCU\Software\SolidWorks\SOLIDWORKS <年份>`
- **Creo**：快捷方式记录的**启动目录**（trail 就写在启动目录里）→ `config.pro` 的 `trail_dir` → 用户目录/文档/桌面
- **NX**：`%TEMP%` → 用户目录 → 注册表里的安装路径
- **建模窗口识别**：内置 13 个主流 CAD 程序名，可在 `modeling_apps` 里增删

## 设置项（data\config.json，改完下次开始采集生效）

| 设置 | 默认 | 意思 |
|---|---|---|
| `modeling_apps` | 13 个主流 CAD | 哪些窗口算建模窗口（exe 名或标题关键词命中） |
| `idle_minutes` | `5.0` | 无操作多少分钟算"离开" |
| `stream1.backends` | `["solidworks","creo","nx"]` | 启用哪些软件后端；删掉哪个就不连哪个 |
| `stream1.prog_id` | 空=自动发现 | SW 的 COM 名；一般不用填，填了就以填的为准 |
| `gestures.*` | 开 | 笔势记录：最小拖动距离 20px、是否记途经点 |
| `ai_eye.enabled` | `false` | **AI 眼总开关**（填好 key 再开） |
| `ai_eye.provider` | `mock` | `mock`=假识别（试用流程）/ `zhipu` / `qwen` / `doubao` / `custom` |
| `ai_eye.api_key` | 空 | **← 你的云端密钥**（mock 模式下不调用云端，不花钱） |
| `ai_eye.triggers` | 左键/快捷键/字母键/笔势开，右键关 | AI 眼截图触发规则 |
| `ai_eye.rate_per_min` | `60` | 限流：每分钟最多截几张 |
| `creo.trail_dirs` / `nx.syslog_dirs` | 空=自动发现 | 手动指定 trail/syslog 位置（一般不用填） |
| `creo.stale_minutes` | `10` | trail 超过这么久没更新就提示"流1 可能收不到" |
| `reconcile.window_ms` | `2000` | 对账窗口：动作之后多久内的流1 事件算它的"效果" |
| `reconcile.ignore_middle` | `true` | 中键/滚轮不算动作（旋转视图本来没有流1 事件） |

## 开 AI 眼的三步（要你自己的 key）

1. 去智谱（open.bigmodel.cn）/ 阿里云百炼 / 火山引擎注册，开通视觉模型，复制 API Key；
2. 用【打开设置】打开 `config.json`：`ai_eye.enabled` 改 `true`，`api_key` 填你的 key，`provider` 改成对应名字（`zhipu`/`qwen`/`doubao`），保存；
3. 重新点【开始采集】。费用：每张图约 1~5 分钱，每分钟上限 60 张。

## SW 快捷键与笔势的本地翻译（零 AI）

- 内置了常见默认快捷键表（Ctrl+S=保存、s=快捷栏…）；
- **笔势方向 → 命令 ID** 自动从 SW 注册表读（`HKCU\...\Mouse Gestures`，Part/Assembly/Drawing/Sketch 四套都有）；
- **命令 ID → 命令名**：程序第一次运行时，从**你本机装的 SolidWorks 类型库**（`swcommands.tlb`）现场生成对照表，存到 `data\sw_command_ids.json`（约 3600 条）。这张表**不在仓库里分发**——它属于厂商软件内容，再分发有法律争议；没装 SW 或生成失败只是少了中文名，不影响记录；
- 想自己补充/覆盖：在 `data\` 放一个 `sw_shortcut_map.json`：
  ```json
  {"key_map": {"d": "拉伸凸台/基体"},
   "gesture_map": {"Part": {"右上": "正视于", "上": "整屏显示全图"}}}
  ```
- 已知限制：SW2022 本地类型库的命令 ID 是重编号的，注册表笔势存的是旧编号——所以笔势目前记"方向（100%准）+ 原始命令 ID"，中文名靠下面那张对照表；对不上时，你可以按上面的格式手动填。

## 常见问题

| 问题 | 答案 |
|---|---|
| 怎么删数据？ | 关掉程序，删除数据文件夹（见上文"数据在哪里"） |
| Creo 的 trail 文件在哪？ | **在你 Creo 快捷方式的"起始位置"目录里**（本机实测 `C:\Users\Public\Documents`）；程序会自动解析快捷方式找出来。⚠️ 注意 `D:\CREO\...\Parametric\bin\` 下那个 `trail.txt.1` 是 PTC 自带的**样板文件**，不是你的操作记录——已修好，不会再认错 |
| 流1 是空的怎么办？ | 先看界面/状态里的提示：① "没找到 trail 文件" = Creo 没运行过；② "trail 文件 N 分钟没更新" = 桥挂的文件不是活的（真 trail 在别的目录，可在 `creo.trail_dirs` 里手动指定）③ SW/NX 没数据先确认软件真的打开了 |
| NX 后端没数据？ | NX(Designcenter) 后端是**探测版**：syslog 挂接已验证（TEMP 下 `*.syslog`），但操作命令行格式要等你真实用一次 NX 后校准 |
| 换电脑？ | 拷 exe 过去即可（数据默认写 `文档\CAD-AI-Monitor\data`；exe 旁有 `data` 文件夹时优先用它）。流1 需要那台电脑装了对应 CAD，版本不限 |
| 验证程序没坏？ | `CAD-AI-Collector.exe --selftest`；各软件联调：`python app\selftest_solidworks.py` / `selftest_creo.py` / `selftest_nx.py` |

## 已知限制（记在案）

1. **AI 眼**：mock 模式与**真实云端都已验证**（2026-10-12 用你自己的 deepseek-flash key 首测通过：6 秒返回标准 JSON、图落盘、时间戳对齐）。注意推理型模型（deepseek-flash）会先思考再回答，`max_tokens` 给小了回答会是空的——默认 4000，可在设置里调大；`deepseek-v4-pro` 实测**不支持图片**。
2. **NX 后端探测版**：本机实测 syslog 存在且桥能挂上；`run_journal.exe` 与 Journal 测试例程都在，Journal 路线成立；命令行解析待真实使用后校准。
3. **笔势命令名**：方向 100% 准确 + 原始命令 ID 已记。"ID→命令名"对照表**不在仓库里分发**（它来自 SolidWorks 自己的类型库，再分发有法律争议）：程序第一次运行时会**从你本机装的 SW 现场生成**一张表存到 `data\sw_command_ids.json`。⚠ 本机实测 SW2022 的类型库编号与注册表里的旧编号对不上，所以这台机器上翻不出名字——这是已知限制，不是 bug；中文名可以手动填 `data\sw_shortcut_map.json`。想提前生成可以运行 `python app\sw_shortcuts.py --generate data`。
4. **对账是软提示**：`stream2_no_effect` 可能是纯视图操作（如点一下空白处），`stream1_orphan` 可能是外部程序改动——标出来供你判断，不自动删数据。
5. **截图范围**：已适配**双屏/多显示器**（抓所有显示器）与 **DPI 缩放**（按系统 DPI 换算、减虚拟屏原点），窗口在主屏或副屏都能截对。
6. **认错 trail 文件（已修）**：Creo 安装目录里 PTC 自带一个样板 `trail.txt.1`。早期版本把它和真 trail 混在一起按"最新修改时间"挑，结果挂上了这个死文件 → **表现为"流1 一直是空的"**。现在安装目录降级为"最后的备胎"，只在启动目录/用户目录都找不到时才用，并且明确提示"trail 文件 N 分钟没更新"。

## 隐私与安全（重要，必读）

> 英文版（给外国用户/企业合规看）：[PRIVACY.md](PRIVACY.md)

这个程序**本质上是一个键盘/鼠标记录器**（这是它的工作原理），请务必知情：

- **只在你指定的建模软件窗口处于前台时才记录**（叫"装聋"：切到浏览器/微信/桌面，一律不记）；
- 记录内容包括：按键、点击、拖动轨迹、窗口切换、以及（可选）**截图**；
- **所有数据只存在你自己电脑的本地文件里**（SQLite + 图片文件夹），程序**没有**任何上传、统计、账号功能；
- **唯一的联网行为**是"AI 眼"：你自己填 key 才生效，它把**截图**发给你自己选的服务商（智谱/阿里/火山/自定义），费用记在你账上。不填 key = 完全不联网；
- 关掉程序 = 停止记录；删掉数据文件夹 = 抹掉全部痕迹；
- 建议：不要把采集数据（尤其截图）直接传到公网；要分享给别人时先自己删掉敏感内容（这就是"数据脱敏"）。

## 开发者角

| 文件 | 职责 |
|---|---|
| `app\collector_core.py` | 核心：配置 + 本地存储 |
| `app\cad_discovery.py` | **CAD 自动发现**：不限版本 ProgID / 注册表版本枚举 / trail·syslog 目录探测 / 建模程序名清单 |
| `app\input_listener.py` | 流2：钩子 + 装聋 + 笔势 + 离开/回来 + AI 眼触发 |
| `app\solidworks_bridge.py` | 流1·SW：COM 抓拍比对 |
| `app\creo_bridge.py` | 流1·Creo：trail 解析 + mapkey |
| `app\nx_bridge.py` | 流1·NX：syslog 解析（探测版） |
| `app\ai_eye.py` | AI 眼：截图→云端→入库 |
| `app\reconcile.py` | 三方对账：缺票自动标异常 |
| `app\app.py` | 界面 |
| `app\selftest*.py` | 自检与各软件真机联调脚本 |
| `app\probe_sw_shortcuts*.py` | SW 接口探测工具（开发用） |
| `app\sw_shortcuts.py` | SW 快捷键/笔势映射；"命令 ID→命令名"表由它在本机从 SW 类型库生成（不分发） |

## 参与贡献 / 版本历史

- **想帮忙最简单的方式**：用它正常建模一段时间，然后开个 issue 说"**哪个 CAD / 哪个版本 / 哪一路有数据（或没数据）**"——一句话就能把支持矩阵里的一行从"待验证"变成"实测"。
- 开发环境、代码规矩（不许写死路径和版本、`.bat` 不许写中文、不许提交个人数据）、怎么加一个新 CAD 后端：见 [CONTRIBUTING.md](CONTRIBUTING.md)
- 每个版本改了什么：[CHANGELOG.md](CHANGELOG.md)
- 发现 bug 请用 [问题模板](.github/ISSUE_TEMPLATE/bug_report.md)（会让你填 CAD 版本和界面提示原文，这样我才能复现）
