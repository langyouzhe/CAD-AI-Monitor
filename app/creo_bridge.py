# -*- coding: utf-8 -*-
"""
creo_bridge.py —— 流1 Creo 桥：解析 Creo 的 trail 操作记录文件（v2.x 新增）

原理（大白话）：
- Creo 运行时会把你的每一步操作（点菜单、改数值、启动命令）写进一个
  "trail.txt.N" 的流水账文件里——这是 Creo 的核心功能，免费、和许可无关。
- 本桥找到最新的 trail 文件，像"追更小说"一样只读新加的行，把里面的
  菜单事件解析成结构化事件，交给采集器落库（时间戳用采集器自己的时钟，
  天然和流2 对齐）。

trail 行格式举例：
    !%CI0.731852                       ← 会话内相对时间戳
    ~ Activate `main_dlg_cur` `main_dlg_cur`
    ~ Select `main_dlg_cur` `MenuBar1` 1 `File`
    !Command ProeWin spawned: ...     ← 一个命令被启动
"""
import re
import threading
import time
from pathlib import Path

import cad_discovery

# 解析 trail 里 "~ Action `comp` `item` values..." 这行的正则
TRAIL_LINE = re.compile(r"^~\s+(\w+)\s+`([^`]*)`\s+`([^`]*)`\s*(.*)$")
# "!%CI" 时间戳行（两种格式：老版相对秒数 / Creo 12 的钟表时间）
TRAIL_TS = re.compile(r"^!%CI(\d+(?:\.\d+)?)\s*$")
TRAIL_CLOCK = re.compile(r"^!%C[DI](.+)$")
# "!Command ProCmdXxx was pushed from the software."
TRAIL_CMD_PUSHED = re.compile(r"^!Command\s+(\S+)\s+was pushed")
# 高频噪音动作（鼠标轨迹占真实 trail 的 80%，必须丢）
NOISE_ACTIONS = {"Move", "Timer", "Trail", "Sync"}


def parse_trail_line(line):
    """把一行 trail 文本解析成事件字典；不认识的返回 None。纯函数，可单测。

    真实 Creo 12 trail 里各行的价值差别极大：
      !Command ProCmdSketSetup was pushed   ← 命令事件（高价值）
      ~ Activate/Select/Close/Command ...   ← 对话框交互（有价值）
      ~ Move `main_dlg_cur` `proe_win` ...  ← 鼠标轨迹（噪音，丢弃）
    """
    line = line.strip()
    m = TRAIL_TS.match(line)
    if m:
        return {"event_type": "trail_timestamp", "session_s": float(m.group(1))}
    m = TRAIL_CLOCK.match(line)
    if m:
        return {"event_type": "trail_clock", "clock": m.group(1).strip()[:60]}
    if line.startswith("!Command"):
        body = line[len("!Command"):].strip()
        m = TRAIL_CMD_PUSHED.match(line)
        if m:
            return {"event_type": "commandPushed", "command": m.group(1)}
        if body.startswith("ProeWin spawned"):
            return {"event_type": "commandLaunched",
                    "command": body.split(":", 1)[-1].strip()}
        return {"event_type": "commandOther", "text": body[:200]}
    m = TRAIL_LINE.match(line)
    if m:
        action = m.group(1)
        if action in NOISE_ACTIONS:
            return None            # 鼠标轨迹等噪音，直接丢
        return {"event_type": "ui", "action": action,
                "component": m.group(2), "item": m.group(3),
                "values": m.group(4).strip()[:200]}
    return None


def scan_trail_files(dirs, max_depth=6, max_dirs=4000):
    """在候选目录里递归找 trail.txt* 文件。

    两道限制防卡死：`max_depth`（最多往下 6 层）+ `max_dirs`（最多看 4000 个目录）。
    就算配置里误填了 `C:\\` 这种超宽目录，也只会慢一下，不会把采集器拖死。
    """
    out = []
    for d in dirs:
        try:
            root = Path(d)
            visited = 0
            stack = [root]
            while stack:
                p = stack.pop()
                visited += 1
                if visited > max_dirs:
                    break
                try:
                    entries = list(p.iterdir())
                except OSError:
                    continue
                for e in entries:
                    try:
                        if e.is_dir():
                            if len(e.relative_to(root).parts) < max_depth:
                                stack.append(e)
                        elif e.name.startswith("trail.txt"):
                            out.append(e)
                    except OSError:
                        continue
        except Exception:
            continue
    return out


def find_latest_trail(dirs):
    """在候选目录里找最新的 trail.txt.N 文件。返回 Path 或 None。"""
    best, best_mtime = None, -1
    for p in scan_trail_files(dirs):
        try:
            mt = p.stat().st_mtime
            if mt > best_mtime:
                best, best_mtime = p, mt
        except OSError:
            continue
    return best


def parse_mapkeys(config_pro_paths):
    """解析 config.pro 里的 mapkey 定义 → {按键: 命令描述}。

    Creo 两种常见写法：
        mapkey d #VIEW;#PREVIOUS;
        mapkey d @MAPKEY_LABEL前一个视图;#VIEW;#PREVIOUS;
    """
    out = {}
    for path in (config_pro_paths or []):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r"^mapkey\s+(\S+)\s+(.+?)\s*$", text, re.M):
            key, definition = m.group(1).lower(), m.group(2)
            label = None
            lm = re.search(r"@MAPKEY_LABEL([^;]+);", definition)
            if lm:
                label = lm.group(1).strip()
            out[key] = label or "自定义 mapkey"
    return out


class CreoBridge:
    """后台线程：跟踪最新 trail 文件，把新增的 Creo 操作事件回调出去。"""

    def __init__(self, cfg: dict, on_event, is_running):
        self.cfg = cfg or {}
        self.trail_dirs = list(self.cfg.get("trail_dirs") or [])
        self.config_pro_paths = list(self.cfg.get("config_pro_paths") or [])
        self.poll_s = float(self.cfg.get("poll_interval_s", 1.0))
        self.stale_s = float(self.cfg.get("stale_minutes", 10) or 10) * 60.0
        self.on_event = on_event        # (kind, detail) -> None
        self.is_running = is_running
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = {"connected": False, "trail_file": None,
                       "mapkeys": 0, "last_error": None}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def status(self):
        with self._lock:
            return dict(self._state)

    def _check_stale(self, trail_file):
        """确认挂上的 trail 是"活的"：超过 STALE_S 没更新就明说，别让用户猜。"""
        try:
            age = time.time() - Path(trail_file).stat().st_mtime
        except OSError:
            return
        if age <= self.stale_s:
            return
        mins = int(age // 60)
        with self._lock:
            self._state.update(last_error=f"trail 文件 {mins} 分钟没更新，"
                                          f"流1 可能收不到新操作（Creo 是否在写？）")
        self.on_event("creo_trail_stale", {
            "trail_file": str(trail_file),
            "idle_s": round(age, 1),
            "hint": "这个文件很久没变了：要么 Creo 没在跑，要么真正的 trail 在别的目录",
        })

    # ---------- 后台主循环 ----------
    def _worker(self):
        fh = None
        trail_file = None
        last_pos = 0
        # 候选 trail 目录：配置给的 + **自动发现**（快捷方式记录的"启动目录"、
        # config.pro 的 trail_dir、用户目录/文档/桌面兜底）。
        # 实测本机 Creo 快捷方式的启动目录是 C:\Users\Public\Documents，trail.txt.N
        # 就写在那里；别的机器上 Creo 可能装在任何位置、任何版本，所以这里不写死。
        dirs = set(self.trail_dirs)
        try:
            for extra in cad_discovery.creo_trail_dirs(self.trail_dirs):
                dirs.add(extra)
        except Exception:
            home = Path.home()
            for extra in (str(home), str(home / "Documents"), str(home / "Desktop"),
                          r"C:\Users\Public\Documents"):
                dirs.add(extra)
        # 全目录递归扫描较贵：缓存结果，30 秒刷新一次
        # fallback_dirs = 安装目录（PTC 自带一个"样板 trail.txt.1"，是**假的**！
        # 曾经因为这个文件比真 trail 旧、却被当成最近文件挂上，导致流1 全空。
        # 所以正常只在"启动目录/用户目录"里找，实在找不到才去安装目录碰运气。）
        fallback_dirs = set()
        try:
            for extra in cad_discovery.creo_install_dirs(self.trail_dirs):
                if extra not in dirs:
                    fallback_dirs.add(extra)
        except Exception:
            pass
        candidates = []
        next_scan = 0.0

        while not self._stop.is_set():
            now_m = time.monotonic()
            if now_m >= next_scan or not candidates:
                candidates = scan_trail_files(dirs)
                if not candidates and fallback_dirs:
                    candidates = scan_trail_files(fallback_dirs)
                next_scan = now_m + 30.0
            latest = None
            best_mtime = -1
            for p in candidates:
                try:
                    mt = p.stat().st_mtime
                    if mt > best_mtime:
                        latest, best_mtime = p, mt
                except OSError:
                    continue
            if latest is None:
                with self._lock:
                    self._state.update(connected=False,
                                       last_error="没找到 trail 文件（Creo 是否运行过？）")
                self._stop.wait(5)
                continue
            # 换了新 trail 文件（Creo 重新启动了）
            if latest != trail_file:
                try:
                    if fh:
                        fh.close()
                    fh = open(latest, "r", encoding="utf-8", errors="ignore")
                    # 只读"从现在起"的新行：挂上旧 trail 时跳到末尾，
                    # 否则会把上次 Creo 会话的历史操作当成刚刚发生的事件重放
                    fh.seek(0, 2)
                    trail_file = latest
                    last_pos = fh.tell()
                except Exception as exc:
                    with self._lock:
                        self._state.update(connected=False, last_error=str(exc))
                    fh = None
                    trail_file = None
                    self._stop.wait(5)
                    continue
                # 报告 mapkey 数量
                mapkeys = parse_mapkeys(self.config_pro_paths)
                with self._lock:
                    self._state.update(connected=True, trail_file=str(latest),
                                       mapkeys=len(mapkeys), last_error=None)
                self.on_event("creo_session", {
                    "event_type": "trail_attached",
                    "trail_file": str(latest),
                    "mapkeys": mapkeys,
                })
                self._check_stale(latest)
            # 追读新增的行
            try:
                for line in fh:
                    ev = parse_trail_line(line)
                    if ev is None:
                        continue
                    kind = ev.pop("event_type", "ui")
                    if kind in ("trail_timestamp", "trail_clock"):
                        continue  # 时间戳我们用自己的时钟，跳过
                    # 命令类（!Command …）单独成一种事件，其余 UI 交互归 creo_trail
                    if kind in ("commandPushed", "commandLaunched", "commandOther"):
                        self.on_event("creo_command",
                                      {"trail_file": trail_file.name,
                                       "event_type": kind, **ev})
                    else:
                        self.on_event("creo_trail",
                                      {"trail_file": trail_file.name, **ev})
            except Exception as exc:
                with self._lock:
                    self._state.update(connected=False, last_error=str(exc))
                fh = None
                trail_file = None
            self._stop.wait(self.poll_s)
        if fh:
            try:
                fh.close()
            except Exception:
                pass
