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
   v0.2.3：config 里 nx.replay_existing=true 时，"挂上/文件被截断后重挂"会把
   已有行从头补读一次（补回"先录 journal、后点开始采集"的那段操作）；只在
   挂上那一次补读，不是每轮轮询都补。默认 false = 旧行为。
3. 解析成事件：
   - stream1/nx_session  {"event_type": "journal_attached", "journal_file": ...}
   - stream1/nx_journal  {"type": "menu"|"api", ...}
4. 编码：UTF-16 / UTF-8 自动识别（NX 默认 UTF-16）。
"""
import threading
import time
from pathlib import Path

import journal_parser
from collector_core import nx_journal_dir


class NxBridge:
    """后台线程：跟踪最新 journal，把新增的 menu/api 事件回调出去。"""

    def __init__(self, cfg: dict, on_event, is_running):
        self.cfg = cfg or {}
        self.journal_path = self.cfg.get("journal_path") or ""
        self.journal_dirs = list(self.cfg.get("journal_dirs") or [])
        # 实际数据根目录（input_listener 会把会话的数据目录传进来）。
        # 默认 journal 目录必须和界面【打开 NX 日志目录】按钮同源：
        #   <data_dir>\journals（见 collector_core.nx_journal_dir）
        self.data_dir = self.cfg.get("data_dir") or None
        self.poll_s = float(self.cfg.get("poll_interval_s", 1.0))
        # v0.2.3：挂上 journal 时是否补读已有内容（默认关 = 旧行为）。
        # 只决定"挂上/截断重挂"那一次要不要从头解析，不是每轮轮询都补读。
        self.replay_existing = bool(self.cfg.get("replay_existing", False))
        self.on_event = on_event
        self.is_running = is_running
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = {"backend": "nx", "connected": False, "journal_file": None,
                       "journal_dir": str(nx_journal_dir(self.data_dir)),
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
        """候选目录：显式配置优先；否则 桌面/文档/下载 + `<data_dir>\\journals`。

        最后一个候选必须和界面【打开 NX 日志目录】按钮、NX 提醒里的
        journal_dir 是**同一个文件夹**（以前桥用的是源码仓库的 data\\journals，
        打包成 exe 或自定义数据目录时和按钮对不上）。
        """
        if self.journal_dirs:
            return self.journal_dirs
        home = Path.home()
        out = []
        for d in (home / "Desktop", home / "Documents", home / "Downloads",
                  nx_journal_dir(self.data_dir)):
            try:
                if d.is_dir():
                    out.append(str(d))
            except OSError:
                continue
        return out

    def _attach(self, target: Path):
        """挂上一个 journal：记住当前行数（旧内容默认不重放），回调通知。

        v0.2.3：`nx.replay_existing=true` 时，先把文件里已有的行从头解析、
        按顺序回调（补回"先录 journal、后点开始采集"的那段操作），再把行数
        记为当前行数——只在挂上/截断重挂时做这一次，之后轮询仍只发新增行。
        """
        text = journal_parser.read_journal_text(target)
        lines = text.splitlines()
        # 先记住行数：即使补读时某个回调抛异常，也不会在下一轮重复补读同一批旧行
        self._last_line_no = len(lines)
        self._current = str(target)
        with self._lock:
            self._state.update(connected=True, journal_file=str(target),
                               last_error=None)
        self.on_event("nx_session", {"event_type": "journal_attached",
                                     "journal_file": str(target)})
        if self.replay_existing:
            self._replay_lines(lines, target)

    def _replay_lines(self, lines, target: Path):
        """把已有 journal 行按顺序解析回调（只在 _attach 里调用，整份只补一次）。"""
        for idx, line in enumerate(lines, 1):
            ev = journal_parser.parse_journal_line(line)
            if ev:
                self.on_event("nx_journal", {
                    "journal_file": target.name,
                    "line_no": idx,
                    **ev,
                })

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
