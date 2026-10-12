# -*- coding: utf-8 -*-
"""
nx_bridge.py —— 流1 NX(Designcenter) 桥：解析 NX syslog 操作日志（v2.x 探测版）

背景：经典 NX 会把会话操作写进 syslog 文件（NX*.syslog，带时间戳和命令
行）。Designcenter NX 2512 基于 NX 内核，理论上同源。本桥：
1. 在候选目录（TEMP/用户目录/配置指定）里找最新的 *.syslog；
2. 追读新增行，按"时间戳 + 命令内容"的启发式解析成事件；
3. 解析规则是探测版——真机实测后按实际格式校准（见 README 已知限制）。

事件：source=stream1, kind=nx_syslog, detail={"line": 原文, "parsed": {...}}
"""
import re
import threading
import time
from pathlib import Path

import cad_discovery

# syslog 行示例（经典 NX，供参考；Designcenter 实测后校准）：
#   2025/01/02 10:30:45  INFO  NX session started
#   1234567  2025/01/02 10:31:02  USER  Entered command: FIT
TIME_RE = re.compile(r"(\d{4}/\d{2}/\d{2}[ T]\d{2}:\d{2}:\d{2})")


def parse_syslog_line(line):
    """解析一行 syslog；解析不出时间戳的返回 None。纯函数，可单测。"""
    line = line.rstrip("\n")
    m = TIME_RE.search(line)
    if not m:
        return None
    ts = m.group(1)
    rest = line[m.end():].strip()
    return {"ts": ts, "line": line, "rest": rest}


def find_latest_syslog(dirs):
    best, best_mtime = None, -1
    for d in dirs:
        try:
            for p in Path(d).glob("*.syslog"):
                try:
                    if p.is_file() and p.stat().st_mtime > best_mtime:
                        best, best_mtime = p, p.stat().st_mtime
                except OSError:
                    continue
        except Exception:
            continue
    return best


class NxBridge:
    """后台线程：跟踪最新 syslog，把新增行回调出去。"""

    def __init__(self, cfg: dict, on_event, is_running):
        self.cfg = cfg or {}
        self.syslog_dirs = list(self.cfg.get("syslog_dirs") or [])
        self.poll_s = float(self.cfg.get("poll_interval_s", 1.0))
        self.on_event = on_event
        self.is_running = is_running
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = {"backend": "nx", "connected": False, "syslog_file": None,
                       "last_error": None}

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

    def _worker(self):
        fh = None
        current = None
        import tempfile
        # 候选 syslog 目录：配置给的 + **自动发现**（%TEMP% / 用户目录 / 注册表安装路径）
        dirs = set(self.syslog_dirs)
        try:
            for extra in cad_discovery.nx_syslog_dirs(self.syslog_dirs):
                dirs.add(extra)
        except Exception:
            dirs.add(tempfile.gettempdir())
            dirs.add(str(Path.home()))
        while not self._stop.is_set():
            latest = find_latest_syslog(dirs)
            if latest is None:
                with self._lock:
                    self._state.update(connected=False,
                                       last_error="没找到 *.syslog（NX 是否运行过？）")
                self._stop.wait(5)
                continue
            if latest != current:
                try:
                    if fh:
                        fh.close()
                    fh = open(latest, "r", encoding="utf-8", errors="ignore")
                    # 只关心"从现在起"的新行：挂上旧文件时跳到末尾，
                    # 否则会把上次运行的整份日志当成刚刚发生的事件重放一遍
                    fh.seek(0, 2)
                    current = latest
                except Exception as exc:
                    with self._lock:
                        self._state.update(connected=False, last_error=str(exc))
                    fh = None
                    current = None
                    self._stop.wait(5)
                    continue
                with self._lock:
                    self._state.update(connected=True, syslog_file=str(latest),
                                       last_error=None)
                self.on_event("nx_session", {"event_type": "syslog_attached",
                                             "syslog_file": str(latest)})
            try:
                for line in fh:
                    parsed = parse_syslog_line(line)
                    if parsed:
                        self.on_event("nx_syslog", {
                            "syslog_file": current.name, **parsed})
            except Exception as exc:
                with self._lock:
                    self._state.update(connected=False, last_error=str(exc))
                fh = None
                current = None
            self._stop.wait(self.poll_s)
        if fh:
            try:
                fh.close()
            except Exception:
                pass
