# -*- coding: utf-8 -*-
"""
nx_bridge.py —— 流1 NX 桥：跟踪 NX Journal（录制脚本）的菜单 / API 事件
（2026-10-23 换数据源：syslog → Journal；依据问题清单 P0-1）

为什么换：NX 的 *.syslog 是启动路由 / 崩溃诊断日志，不含建模操作——读了
4 次会话都是 0 条。NX 的「录制日志（Record Journal）」输出的 .cs/.py/.vb
才是"点了哪个菜单"的原文，而且**边操作边追加**，适合追读。

工作方式：
1. 找 journal：
   - 配了 nx.journal_path（显式路径）→ 只盯它（最稳，推荐）；
   - 否则在 nx.journal_dirs（默认 桌面/文档/下载/data\\journals）里按 mtime
     找"内容像 journal"的最新文件（有内容特征过滤，不会乱捡 .py 脚本）。
2. 挂上时先记住文件行数（旧内容不重放）；之后每秒重读，只发新增行。
3. 解析成事件：
   - stream1/nx_session  {"event_type": "journal_attached", "journal_file": ...}
   - stream1/nx_journal  {"type": "menu"|"api", ...}
4. 编码：UTF-16 / UTF-8 自动识别（NX 默认 UTF-16）。
"""
import threading
import time
from pathlib import Path

import journal_parser


class NxBridge:
    """后台线程：跟踪最新 journal，把新增的 menu/api 事件回调出去。"""

    def __init__(self, cfg: dict, on_event, is_running):
        self.cfg = cfg or {}
        self.journal_path = self.cfg.get("journal_path") or ""
        self.journal_dirs = list(self.cfg.get("journal_dirs") or [])
        self.poll_s = float(self.cfg.get("poll_interval_s", 1.0))
        self.on_event = on_event
        self.is_running = is_running
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = {"backend": "nx", "connected": False, "journal_file": None,
                       "last_error": None}
        self._current = None
        self._last_line_no = 0

    # ---------- 生命周期 ----------

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

    # ---------- 内部 ----------

    def _dirs(self):
        if self.journal_dirs:
            return self.journal_dirs
        home = Path.home()
        out = []
        for d in (home / "Desktop", home / "Documents", home / "Downloads",
                  Path(__file__).resolve().parent.parent / "data" / "journals"):
            try:
                if d.is_dir():
                    out.append(str(d))
            except OSError:
                continue
        return out

    def _attach(self, target: Path):
        """挂上一个 journal：记住当前行数（旧内容不重放），回调通知。"""
        text = journal_parser.read_journal_text(target)
        self._last_line_no = len(text.splitlines())
        self._current = str(target)
        with self._lock:
            self._state.update(connected=True, journal_file=str(target),
                               last_error=None)
        self.on_event("nx_session", {"event_type": "journal_attached",
                                     "journal_file": str(target)})

    def _poll_once(self):
        target = journal_parser.find_latest_journal(
            self._dirs(), explicit=self.journal_path or None)
        if target is None:
            with self._lock:
                self._state.update(
                    connected=False, journal_file=None,
                    last_error="没找到 NX journal（先在 NX 里录制日志，"
                               "或配置 nx.journal_path 指向 .cs/.py 文件）")
            self._current = None
            self._last_line_no = 0
            return
        if str(target) != self._current:
            self._attach(target)
            return
        # 同一个文件：只发新增行
        text = journal_parser.read_journal_text(target)
        lines = text.splitlines()
        if len(lines) < self._last_line_no:
            # 文件被重写/截断 → 重新挂
            self._attach(target)
            return
        if len(lines) == self._last_line_no:
            return
        for idx in range(self._last_line_no, len(lines)):
            ev = journal_parser.parse_journal_line(lines[idx])
            if ev:
                self.on_event("nx_journal", {
                    "journal_file": target.name,
                    "line_no": idx + 1,
                    **ev,
                })
        self._last_line_no = len(lines)

    def _worker(self):
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:  # 读文件失败不退出线程
                with self._lock:
                    self._state.update(connected=False, last_error=str(exc))
            self._stop.wait(self.poll_s)
