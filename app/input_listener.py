# -*- coding: utf-8 -*-
r"""
input_listener.py —— 采集层：全局键盘/鼠标监听 + 当前窗口跟踪 + 装聋过滤

版本记录：
- v2：装聋原则（只记建模窗口前台的操作）、window_enter、离开/回来、不记停顿
- v2.x 新增：
  1. 笔势记录：按住鼠标键拖动（SW 右键笔势导引这类操作），记 gesture 事件：
     8 方向判定 + 起终点 + 时长 + 途经点（可选）；
  2. AI 眼触发点：点击/快捷键/字母键/笔势发生时，把"光标位置 + 原始事件"
     交给 ai_eye 模块（截图→云端识别），结果以 stream2/ai_eye 事件入库。
- v4（v0.2.0）：一次 start()→stop() = 一个独立会话文件夹
  `data\sessions\<yyyy-mm-dd_HHMMSS>\`（images\ + data\events.db + session.json）；
  空会话停止时删除整个文件夹；停止顺序改为"先停采集源 → 排空 AI 眼队列（上限约 5 秒）
  → 对账 → 写 session.json → WAL checkpoint 关库"。
- v0.2.2（2026-10-23 全程序检查修复）：
  1. AI 眼触发时记录前台窗口身份（hwnd + exe/title + 触发时刻）；截图改为
     **按目标窗口抓取（PrintWindow）**，抓取前复核身份、抓取后再复核一次，
     不通过就跳过并丢弃图片（不保存/不上传）；没有整屏截图/裁剪路径；
  2. 装聋只认 exe：exe 已知但不是 CAD → 标题再像也拒绝；exe 取不到才用标题兜底；
  3. 写库失败不再静默：设 self.error + 标记写入失败，停止时保留会话并标
     session.json status="error"（不标 completed、不当空会话删）；
  4. 建 NX 桥时传实际数据根目录，桥默认 journal 目录与界面按钮同源；
  5. NX 录制提醒复用 exe 优先规则：exe 已知非 NX → 标题含 NX 也不提示
     （浏览器/搜索页标题不再误报）；exe 取不到才按标题兜底。

本文件不含界面；流1 桥接在 solidworks_bridge.py，AI 眼在 ai_eye.py。
"""
import ctypes
import ctypes.wintypes as wt
import math
import threading
from pathlib import Path

from pynput import keyboard, mouse

from collector_core import EventStore, now_ms, nx_journal_dir

# 判断"组合键"要看哪些修饰键
MODIFIER_NAMES = {
    keyboard.Key.ctrl: "ctrl",
    keyboard.Key.ctrl_l: "ctrl",
    keyboard.Key.ctrl_r: "ctrl",
    keyboard.Key.alt: "alt",
    keyboard.Key.alt_l: "alt",
    keyboard.Key.alt_r: "alt",
    keyboard.Key.shift: "shift",
    keyboard.Key.shift_l: "shift",
    keyboard.Key.shift_r: "shift",
    keyboard.Key.cmd: "cmd",
    keyboard.Key.cmd_l: "cmd",
    keyboard.Key.cmd_r: "cmd",
}

# 笔势 8 方向名称（按角度扇区顺序：右→逆时针一圈）
GESTURE_DIRECTIONS = ["右", "右上", "上", "左上", "左", "左下", "下", "右下"]

# NX 主程序名（nx_record_hint 的 exe 优先判定）：只有这些 exe 才算 NX。
# 浏览器/聊天窗口的标题里带 "NX" 不能被当成 NX（和装聋 is_modeling_window
# 同一条规则：exe 已知时标题不再"救"它）。
NX_EXECUTABLES = ("ugraf.exe", "nx.exe")


def classify_gesture_direction(dx, dy):
    """把"起点→终点"的位移向量归到 8 个方向之一（屏幕坐标 y 向下）。

    返回方向中文名；位移为 0 时返回 None。纯函数，可单测。
    """
    if dx == 0 and dy == 0:
        return None
    deg = (math.degrees(math.atan2(-dy, dx)) + 360.0) % 360.0
    sector = int(deg / 45.0 + 0.5) % 8
    return GESTURE_DIRECTIONS[sector]


def _window_title(hwnd):
    """读窗口标题（取不到返回 None，绝不抛异常）。"""
    try:
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or None
    except Exception:
        return None


def _window_exe(hwnd):
    """读窗口所属进程的程序名（如 ugraf.exe；取不到返回 None，绝不抛异常）。"""
    try:
        pid = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not handle:
            return None
        try:
            size = wt.DWORD(4096)
            path_buf = ctypes.create_unicode_buffer(size.value)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, path_buf, ctypes.byref(size)
            )
            return Path(path_buf.value).name if ok else None
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return None


def foreground_window_identity():
    """当前前台窗口身份：(hwnd, 标题, 程序名)。

    取不到 hwnd 时返回 (0, None, None)——AI 眼拿到 hwnd=0 会直接跳过截图，
    绝不"猜一个窗口"或退化成整屏（隐私红线）。
    """
    try:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        hwnd = 0
    if not hwnd:
        return 0, None, None
    return hwnd, _window_title(hwnd), _window_exe(hwnd)


def window_state(hwnd):
    """查询某个窗口**此刻**的状态（AI 眼截图前复核用）。

    返回 {"alive": bool, "foreground": bool, "title": str|None, "exe": str|None}。
    hwnd 为 0/无效 → alive=False（调用方据此跳过截图，不截任何东西）。
    """
    try:
        hwnd = int(hwnd or 0)
    except (TypeError, ValueError):
        hwnd = 0
    state = {"alive": False, "foreground": False, "title": None, "exe": None}
    if not hwnd:
        return state
    try:
        user32 = ctypes.windll.user32
        state["alive"] = bool(user32.IsWindow(hwnd))
        state["foreground"] = int(user32.GetForegroundWindow() or 0) == hwnd
        if state["alive"]:
            state["title"] = _window_title(hwnd)
            state["exe"] = _window_exe(hwnd)
    except Exception:
        pass
    return state


def foreground_window_info():
    """拿到"当前最前面的窗口"的 (标题, 程序名)。

    取不到时返回 (None, None)，绝不因为一个窗口报错就停下整个采集。
    """
    _, title, exe = foreground_window_identity()
    return title, exe


def is_modeling_window(info, modeling_apps, allow_all=False):
    """判断一个 (标题, 程序名) 是不是建模软件窗口。

    - allow_all=True（调试开关）：什么窗口都算。
    - **exe 取得到**：只认 exe 完全匹配——标题再像也不算。
      这是"装聋"的隐私边界：浏览器标题里带 NX / SOLIDWORKS（搜索页、网盘
      文件名、聊天窗口标题）不能被当成 CAD 窗口，否则会把浏览器里的操作记下来。
    - **exe 取不到（空）**：才退回标题关键词匹配（兼容权限不足拿不到进程名的
      情况；这是唯一的兜底路径）。
    """
    if allow_all:
        return True
    title, exe = (info or (None, None))[:2]
    exe_l = str(exe or "").strip().lower()
    if exe_l:
        # exe 已知 → 只按 exe 规则判定，标题不再"救"它
        return any((app.get("exe") or "").lower() == exe_l for app in modeling_apps)
    title_l = str(title or "").strip().lower()
    if not title_l:
        return False
    return any((app.get("title") or "").lower() and
               (app.get("title") or "").lower() in title_l
               for app in modeling_apps)


def nx_record_hint(current_window, stream1_status, cfg, data_dir=None):
    r"""NX 手动录制提醒（纯函数：不读全局、不碰 Windows API，可直接单测）。

    为什么只做"提醒"、不做自动开录制：NX 的录制 API 只能在 NX 进程内调用——
    `run_journal.exe -nx` 是批处理（脚本跑完 NX 就退出，不能交互建模）；
    startup 目录只读菜单文件、不执行 Python。外部程序没法让正在运行的 NX
    开始录制，所以这里只管"用户切到 NX、却忘了点录制"时提醒一句，
    并给出录制日志该放哪。**不做任何界面自动化/模拟点击。**

    命中条件（全部满足才返回提示，否则返回 None）：
    1. 本次会话启用了 nx 后端（stream1.enabled 且 stream1.backends 含 "nx"）；
    2. nx.hint_when_not_recording 没被关掉（默认开）；
    3. 前台窗口是 NX：**exe 优先**——exe = ugraf.exe / nx.exe 才算 NX；
       exe 已知但不是 NX（例如浏览器 chrome.exe）→ 标题再像也不提示；
       **只有 exe 取不到（空）时**才退回标题匹配（含 NX / Designcenter）；
    4. nx 后端没有 connected（还没找到录制日志；状态还没生成时按未连接算）。

    参数：
        current_window: (窗口标题, 程序名) 二元组，如 ("NX 2512", "ugraf.exe")
        stream1_status: 各流1 后端 status() 组成的列表；None/空列表 = 还没状态
        cfg: 合并后的配置 dict（DEFAULT_CONFIG + 用户 config.json）
        data_dir: 数据根目录（可省略，仅用于拼 journal_dir）

    返回 {"text": 提醒文字, "journal_dir": "<数据目录>\\journals"}。
    """
    cfg = cfg or {}
    stream1 = cfg.get("stream1") or {}
    if not stream1.get("enabled", True):
        return None
    backends = stream1.get("backends")
    if backends is None:                       # 没配就按默认三个后端算
        backends = ["solidworks", "creo", "nx"]
    if "nx" not in [str(b).strip().lower() for b in backends]:
        return None
    nx_cfg = cfg.get("nx") or {}
    if not nx_cfg.get("hint_when_not_recording", True):
        return None
    title, exe = (current_window or (None, None))[:2]
    exe_l = str(exe or "").strip().lower()
    title_l = str(title or "").lower()
    # exe 优先（和装聋同一规则）：exe 已知时，只有 NX 主程序才算 NX；
    # 否则标题里带 "NX" 的浏览器/搜索页会被误提示。
    if exe_l:
        is_nx = exe_l in NX_EXECUTABLES
    else:
        is_nx = ("nx" in title_l or "designcenter" in title_l)
    if not is_nx:
        return None
    for st in stream1_status or []:
        try:
            if str(st.get("backend", "")).lower() == "nx" and st.get("connected"):
                return None                    # 已经挂上 journal，不用再提醒
        except AttributeError:
            continue
    journal_dir = nx_journal_dir(data_dir)
    return {
        "text": "NX 已在前台，但还没找到录制日志：请在 NX 里点 File → Utilities → "
                "Record Journal（或设置 nx.journal_path）",
        "journal_dir": str(journal_dir),
    }


class CollectorSession:
    """一次采集会话：存储 + 流2监听 + 装聋过滤 + 笔势 + 离开/回来 + 流1桥 + AI眼。

    v4 起一次 start()→stop() 就是一个**独立会话文件夹**：
        data\\sessions\\<开始时刻>\\images\\ + data\\events.db + session.json
    正常停止且除开始/停止标记外一条事件都没有 → 整个会话文件夹删除（空会话不留痕）。

    用法（界面代码就是这么用的）：
        s = CollectorSession(data_dir)
        s.start()          # 开始记录（新建会话文件夹，重新读一遍配置）
        ...（期间所有合规事件自动落库）...
        s.stop()           # 停止记录（排空 AI 眼 → 对账 → 写 session.json → 关库）
    """

    def __init__(self, data_dir):
        from collector_core import ConfigManager

        self.data_dir = Path(data_dir)   # 数据根目录（config.json、sessions\ 都在这）
        # 下面三个在 start() 里创建：本次会话文件夹 / 截图目录 / 数据库
        self.session_dir = None
        self.db_path = None
        self.images_dir = None
        self.store = None                # EventStore（start() 创建，stop() 关闭）

        self.cfg_mgr = ConfigManager(self.data_dir)
        self.cfg = self.cfg_mgr.load()

        self.session_id = None
        self.running = False
        self.started_at = None
        self.error = None
        # 本次会话有没有写库失败（写失败时序号/计数会错位，不能当"正常空会话"删掉）
        self._write_failed = False
        self.stream1 = None          # 第一个流1 桥（保留给笔势取文档类型用）
        self.stream1s = []           # 全部流1 桥（solidworks/creo/nx）
        self.ai_eye = None           # AI 眼（截图→云端识别），start() 里按配置创建
        self.shortcut_map = None     # SW 快捷键/笔势映射表（本地翻译用）

        self._lock = threading.Lock()
        self._seq = 0
        self._counts = {"stream1": 0, "stream2": 0, "meta": 0}
        # 空会话判定用：除 session_start / session_stop 外的事件条数
        # （只看总条数永远删不掉——开始/停止标记本身就把数字撑到 2）
        self._content_events = 0
        self._closed = False         # 数据库是否已关闭（关库后迟到的结果直接丢弃）
        self._leave_count = 0
        self._last_event_ts = None   # 最后一条事件的毫秒时间戳（离开检测用）
        self._in_leave = False       # 是否已记过"离开"且还没"回来"
        self._pressed = set()
        self._current_window = (None, None)  # (标题, 程序名)
        self._cursor_pos = (0, 0)    # 鼠标当前位置（AI 眼截图定位用）
        self._drag = None            # 正在进行的"按住拖动"状态（笔势用）
        self._kb_listener = None
        self._ms_listener = None
        self._win_thread = None
        self._idle_thread = None
        self._stop_flag = threading.Event()
        self._idle_ms = None  # 离开阈值（毫秒），start() 里按配置算好
        self._ai_eye_drain = None    # 停止时 AI 眼排空统计

    # ---------- 对外：开始 / 停止 ----------

    def start(self):
        from collector_core import (create_session_dir, session_db_path,
                                    session_images_dir, session_data_dir)

        try:
            self.cfg = self.cfg_mgr.load()  # 每次开始都重读设置，改动即时生效
            idle_minutes = float(self.cfg.get("idle_minutes", 5.0))
            self._idle_ms = max(idle_minutes, 0.1) * 60_000
            # 同一个对象如果被复用（start→stop→start），计数和桥都要归零重来
            with self._lock:
                self._seq = 0
                self._counts = {"stream1": 0, "stream2": 0, "meta": 0}
                self._content_events = 0
            self.stream1s = []
            self.stream1 = None
            self.ai_eye = None
            self._leave_count = 0
            self._in_leave = False
            self._ai_eye_drain = None
            self._write_failed = False
            self.error = None

            # v4：先建本次会话文件夹（重名自动 _2/_3），再在里面开库
            self.session_dir = create_session_dir(self.data_dir)
            self.images_dir = session_images_dir(self.session_dir)
            self.images_dir.mkdir(parents=True, exist_ok=True)
            session_data_dir(self.session_dir).mkdir(parents=True, exist_ok=True)
            self.db_path = session_db_path(self.session_dir)
            self.store = EventStore(self.db_path)
            self._closed = False
            self._content_events = 0
            self.session_id = self.store.start_session()
            self.started_at = now_ms()
            self._last_event_ts = self.started_at
            # 快捷键/笔势映射表（本地翻译：d→拉伸 这类）放在数据根目录，跨会话共用
            try:
                from sw_shortcuts import ShortcutMap
                self.shortcut_map = ShortcutMap(self.data_dir)
                if not self.shortcut_map.id2name:
                    # "命令ID→命令名"表不在仓库里分发（属于厂商软件内容），
                    # 后台从用户本机装的 SW 类型库现场生成一次
                    threading.Thread(target=self._warm_command_ids, daemon=True).start()
            except Exception:
                self.shortcut_map = None
            self._record("meta", "session_start", {
                "data_dir": str(self.data_dir),
                "session_dir": str(self.session_dir),
            })

            self._kb_listener = keyboard.Listener(
                on_press=self._on_key_press, on_release=self._on_key_release
            )
            self._ms_listener = mouse.Listener(
                on_click=self._on_click, on_move=self._on_move
            )
            self._kb_listener.start()
            self._ms_listener.start()

            self._stop_flag.clear()
            self._win_thread = threading.Thread(target=self._window_loop, daemon=True)
            self._win_thread.start()
            self._idle_thread = threading.Thread(target=self._idle_loop, daemon=True)
            self._idle_thread.start()

            # 流1 桥：按配置启动多个后端（solidworks / creo / nx）
            # （SolidWorks/Creo/NX 没开也不影响流2）
            if self.cfg.get("stream1", {}).get("enabled", True):
                backends = self.cfg.get("stream1", {}).get("backends", ["solidworks"])
                for backend in backends:
                    try:
                        if backend == "solidworks":
                            from solidworks_bridge import SolidWorksBridge
                            bridge = SolidWorksBridge(
                                cfg=self.cfg.get("stream1", {}),
                                on_event=lambda kind, detail: self._on_stream1_event(
                                    kind, {**detail, "backend": "solidworks"}),
                                is_running=lambda: self.running,
                            )
                        elif backend == "creo":
                            from creo_bridge import CreoBridge
                            bridge = CreoBridge(
                                cfg=self.cfg.get("creo", {}),
                                on_event=lambda kind, detail: self._on_stream1_event(
                                    kind, {**detail, "backend": "creo"}),
                                is_running=lambda: self.running,
                            )
                        elif backend == "nx":
                            from nx_bridge import NxBridge
                            # 把**实际数据根目录**传给桥：桥的默认 journal 目录
                            # 必须和界面【打开 NX 日志目录】按钮指向同一个
                            # `<data_dir>\journals`（打包/自定义数据目录时也不跑偏）
                            nx_cfg = dict(self.cfg.get("nx", {}))
                            nx_cfg["data_dir"] = str(self.data_dir)
                            bridge = NxBridge(
                                cfg=nx_cfg,
                                on_event=lambda kind, detail: self._on_stream1_event(
                                    kind, {**detail, "backend": "nx"}),
                                is_running=lambda: self.running,
                            )
                        else:
                            continue
                        bridge.start()
                        self.stream1s.append(bridge)
                        if self.stream1 is None:
                            self.stream1 = bridge
                    except ImportError:
                        pass  # 该后端模块不存在，跳过
                    except Exception as exc:
                        self.error = f"流1后端 {backend} 启动失败: {exc}"

            # AI 眼：按配置启动（BYOK：没填 key 或 enabled=false 就安静不启动）
            # v4：截图存本次会话的 images\（不再按日期分层）
            if self.cfg.get("ai_eye", {}).get("enabled", False):
                try:
                    from ai_eye import AiEye
                    self.ai_eye = AiEye(
                        cfg=self.cfg.get("ai_eye", {}),
                        # 会话停止后仍在路上的识别结果直接丢弃，不写进已结束的会话
                        on_result=lambda detail, ts: (
                            self._record("stream2", "ai_eye", detail, ts)
                            if self.running else None),
                        is_running=lambda: self.running,
                        images_dir=self.images_dir,
                        # 截图前复核窗口身份要用它：和装聋判定同一份建模软件名单
                        modeling_apps=self.cfg.get("modeling_apps", []),
                        allow_all_windows=self.cfg.get("debug", {}).get(
                            "allow_all_windows", False),
                    )
                    self.ai_eye.start()
                except Exception as exc:
                    self.error = f"AI眼启动失败: {exc}"

            self.running = True
        except Exception as exc:  # 启动失败（少见，一般是系统权限问题）
            self.error = str(exc)
            self._abort_start()
            raise

    def _abort_start(self):
        """启动失败时的收尾：停掉已起的线程/桥，关库；还没内容就删掉半截会话文件夹。"""
        try:
            if self.ai_eye is not None:
                try:
                    self.ai_eye.stop(drain_timeout_s=0)
                except Exception:
                    pass
                self.ai_eye = None
            for bridge in self.stream1s:
                try:
                    bridge.stop()
                except Exception:
                    pass
            self.stream1s = []
            self.stream1 = None
            for listener in (self._kb_listener, self._ms_listener):
                try:
                    if listener:
                        listener.stop()
                except Exception:
                    pass
            self._kb_listener = self._ms_listener = None
            self.running = False
            if self.store is not None:
                self.store.close()
                self.store = None
            self._closed = True
            # 半截会话：没有内容才删；**写库失败过也保留**（可能是库写不进去，
            # 删了就把证据/半截数据丢了，和 stop() 的规则保持一致）
            if (self.session_dir is not None and self._content_events == 0
                    and not self._write_failed):
                from collector_core import delete_session_dir
                delete_session_dir(self.session_dir)
                self.session_dir = None
        except Exception:
            pass

    def stop(self):
        """停止采集：排空 AI 眼 → 三方对账 → 写 session.json → 关库 → 空会话删除。

        返回本次会话小结（dict）；没在采集时返回 None。字段：
        session_dir / kept / status / error / event_count / counts / duration_s / ai_eye。
        """
        from collector_core import (APP_VERSION, SCHEMA_VERSION, STORAGE_VERSION,
                                    _iso_from_ms, delete_session_dir, now_iso,
                                    write_session_json)

        if not self.running:
            return None
        self._stop_flag.set()
        # 1. 停掉所有采集来源（先断输入，AI 眼队列留到最后排空）
        for bridge in self.stream1s:
            try:
                bridge.stop()
            except Exception:
                pass
        if self._kb_listener:
            try:
                self._kb_listener.stop()
            except Exception:
                pass
        if self._ms_listener:
            try:
                self._ms_listener.stop()
            except Exception:
                pass
        if self._win_thread:
            self._win_thread.join(timeout=2)
        if self._idle_thread:
            self._idle_thread.join(timeout=2)

        # 2. 排空 AI 眼队列：等在途云端请求做完（上限 drain_timeout_s，默认 5 秒）
        #    此时库还开着，识别结果还能归入本次会话（v4 施工注意事项）
        if self.ai_eye:
            try:
                self._ai_eye_drain = self.ai_eye.stop(
                    drain_timeout_s=float(
                        self.cfg.get("ai_eye", {}).get("drain_timeout_s", 5.0)))
            except Exception:
                self._ai_eye_drain = None
        self.running = False  # 之后迟到的识别结果不再入库

        # 3. 三方对账（按文档 5.2：缺票自动标异常），结果写成 meta/anomaly
        if self.cfg.get("reconcile", {}).get("enabled", True):
            try:
                from reconcile import run_on_session
                n = run_on_session(self, self.cfg)
                if n:
                    self._last_error_note = f"对账标出 {n} 条异常"
            except Exception:
                pass

        # 4. 写结束标记并结算（先把条数算好，session.json / sessions 表都记它）
        self._record("meta", "session_stop", {"event_count": self._total()})
        event_count = self._total()
        try:
            self.store.end_session(self.session_id, event_count)
        except Exception:
            pass

        # 5. 写 session.json（标配摘要：不开库就能扫；库坏了也能看）
        #    写库失败 → status="error"（不标 completed），并把原因写进摘要/界面
        session_status = "error" if self._write_failed else "completed"
        duration_s = round((now_ms() - self.started_at) / 1000.0, 1) if self.started_at else 0.0
        software = self._software_used()
        info = {
            "app_version": APP_VERSION,
            "storage_version": STORAGE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "status": session_status,
            "session_id": self.session_id,
            "session": self.session_dir.name if self.session_dir else None,
            "started_at": _iso_from_ms(self.started_at) if self.started_at else None,
            "stopped_at": now_iso(),
            "duration_s": duration_s,
            "software": software,
            "event_count": event_count,
            "counts": dict(self._counts),
            "db": "data/events.db",
            "images": "images",
            "ai_eye": self._ai_eye_drain,
        }
        if self._write_failed:
            info["error"] = self.error
        try:
            write_session_json(self.session_dir, info)
        except Exception as exc:
            self.error = f"session.json 写入失败: {exc}"

        # 6. 把 WAL 合并回主库再关库：一个会话的 events.db 单文件就是完整数据
        try:
            self.store.close()
        except Exception:
            pass
        self._closed = True

        # 7. 空会话不留痕：正常停止且除开始/停止标记外一条事件都没有 → 删整个文件夹
        #    写库失败时**绝不删**：那可能正是"库写不进去"，删了就把半截数据/证据
        #    一起丢了；状态由 session.json 的 status="error" 体现。
        kept = self._content_events > 0 or self._write_failed
        if not kept and self.session_dir is not None:
            delete_session_dir(self.session_dir)

        summary = {
            "session_dir": str(self.session_dir) if self.session_dir else None,
            "kept": kept,
            "status": session_status,
            "error": self.error if self._write_failed else None,
            "event_count": event_count,
            "counts": dict(self._counts),
            "duration_s": duration_s,
            "software": software,
            "ai_eye": self._ai_eye_drain,
        }
        return summary

    def _software_used(self):
        """本次会话"所用软件"：配置启用的后端 + 实际连上的后端（写进 session.json）。"""
        out = []
        if self.cfg.get("stream1", {}).get("enabled", True):
            out = list(self.cfg.get("stream1", {}).get("backends", []))
        connected = []
        for bridge in self.stream1s:
            try:
                st = bridge.status()
                name = st.get("backend")
                if name and st.get("connected") and name not in connected:
                    connected.append(name)
            except Exception:
                pass
        return {"enabled": out, "connected": connected}

    def stats(self):
        """给界面读的实时数字。"""
        with self._lock:
            stream1_status = ([b.status() for b in self.stream1s]
                              if self.stream1s else None)
            return {
                "running": self.running,
                "counts": dict(self._counts),
                "leave_count": self._leave_count,
                "started_at": self.started_at,
                "session_id": self.session_id,
                "session_dir": str(self.session_dir) if self.session_dir else None,
                "current_window": self._current_window,
                "in_leave": self._in_leave,
                # 写库失败原因（界面/停止小结要显示；None=本次会话一切正常）
                "error": self.error,
                "write_failed": self._write_failed,
                "stream1_status": stream1_status,
                # NX 手动录制提醒：NX 在前台但还没找到 journal 时给界面一句话
                #（纯函数，见 nx_record_hint；配置项 nx.hint_when_not_recording 可关）
                "nx_hint": nx_record_hint(self._current_window, stream1_status,
                                          self.cfg, self.data_dir),
                "ai_eye_status": self.ai_eye.status() if self.ai_eye else None,
            }

    # ---------- 内部：流1 事件落库（v3：选中变化还要喂给 AI 眼） ----------

    def _warm_command_ids(self):
        """后台生成"命令ID→命令名"表（从本机 SolidWorks 类型库）。

        为什么放后台：解析类型库要零点几秒，不能拖慢"开始采集"。
        失败（没装 SW / 权限问题）就静默返回——笔势仍会记录原始命令 ID。
        表放**数据根目录**（跨会话共用），不是某个会话文件夹里。
        """
        try:
            from sw_shortcuts import ensure_command_ids
            ids = ensure_command_ids(self.data_dir)
            if ids and self.shortcut_map is not None:
                self.shortcut_map.id2name = ids
        except Exception:
            pass

    def _on_stream1_event(self, kind, detail):
        """流1 事件统一入口：落库 + （选中变化时）触发 AI 眼。"""
        ts = self._record("stream1", kind, detail)
        if kind == "selectionChanged":
            self._maybe_ai_eye("selectionChanged", detail, ts, self._cursor_pos)
        return ts

    # ---------- 内部：事件落库 ----------

    def _total(self):
        return sum(self._counts.values())

    def _allow_stream2(self):
        """装聋判定：当前前台窗口是不是建模窗口。"""
        return is_modeling_window(
            self._current_window,
            self.cfg.get("modeling_apps", []),
            allow_all=self.cfg.get("debug", {}).get("allow_all_windows", False),
        )

    def _record(self, source, kind, detail, ts=None):
        """统一入口：补"回来"标记 → 写事件 → 更新离开检测时钟。

        返回事件的时间戳（毫秒），AI 眼需要用它把识别结果挂回原事件。
        库关闭之后（停采之后）迟到的结果直接丢弃，不抛异常。
        """
        ts = ts or now_ms()
        with self._lock:
            if self._closed or self.store is None:
                return ts
            try:
                # 之前记过"离开"，现在有活动了 → 先补一条"回来"
                if self._in_leave:
                    self._seq += 1
                    self.store.add_event(self.session_id, self._seq, "meta", "return",
                                         {"since_ts": self._last_event_ts}, ts)
                    self._counts["meta"] += 1
                    self._in_leave = False
                self._seq += 1
                self.store.add_event(self.session_id, self._seq, source, kind, detail, ts)
            except Exception as exc:
                # 写库失败绝不能安静吞掉：记下原因 + 标记本会话写入失败。
                # 否则序号已加、内容计数不加，停止时会被当成"空会话"删掉，
                # 用户只会看到"本次没有记录到任何事件"，问题被掩盖。
                self._write_failed = True
                self.error = f"事件写库失败: {exc}"
                return ts
            self._counts[source if source in self._counts else "meta"] += 1
            # v4 空会话判定：只看用户数据（stream1/stream2）。
            # 所有 meta（开始/停止/离开/对账汇总/异常）都是边界或诊断信息，
            # 不算"有内容"——否则新增的 stream1_unavailable 汇总会让空会话删不掉。
            if source in ("stream1", "stream2"):
                self._content_events += 1
            # 注意：AI 眼结果挂的是原事件的时间戳（可能早于当前），
            # 时钟只能前进不能倒退，否则离开检测会被回拨干扰。
            if self._last_event_ts is None or ts > self._last_event_ts:
                self._last_event_ts = ts
            return ts

    # ---------- 内部：AI 眼触发 ----------

    def _maybe_ai_eye(self, kind, detail, ts, pos):
        """决定这次事件要不要触发 AI 眼截图。

        隐私红线（2026-10-23 全程序检查阻断项，二次复验加固）：触发时记录
        **当前前台窗口身份**（hwnd + exe/title + 触发时刻），一起交给 AI 眼队列。
        后台线程会**按窗口抓取画面（PrintWindow，不是整屏）**，抓取前复核这个
        窗口还活着、还在前台、exe/title 与触发时一致，抓取后再复核一次；任何
        一次不满足就跳过（不保存/不上传/不回调），绝不退化成整屏截图。

        只有 AI 眼开着时才取窗口身份——默认关闭，零额外开销。
        """
        if not self.ai_eye:
            return
        try:
            hwnd, title, exe = foreground_window_identity()
            window = {"hwnd": hwnd, "title": title, "exe": exe, "ts": ts}
            self.ai_eye.trigger(kind, detail, ts, pos, window=window)
        except Exception:
            pass

    # ---------- 内部：键盘 / 鼠标回调（装聋过滤在这里） ----------

    @staticmethod
    def _key_name_and_char(key):
        """把 pynput 给的按键对象翻译成好读的文字。"""
        char = getattr(key, "char", None)
        if isinstance(char, str) and char:
            return char, char
        return str(key), None

    def _current_mods(self):
        """当前按住的修饰键（ctrl/alt/shift/cmd），用于标记组合键。"""
        mods = set()
        for k in self._pressed:
            m = MODIFIER_NAMES.get(k)
            if m:
                mods.add(m)
        return sorted(mods)

    def _on_key_press(self, key):
        try:
            if not self.running:
                return
            # 按键集合无条件维护（跨窗口组合键要数得清），只对"记录"做装聋过滤
            with self._lock:
                self._pressed.add(key)
            if not self._allow_stream2():
                return  # 装聋：非建模窗口的操作不记
            name, char = self._key_name_and_char(key)
            with self._lock:
                mods = self._current_mods()
            is_modifier = key in MODIFIER_NAMES   # 单独按 Shift/Ctrl/Alt 本身不算操作
            detail = {
                "key": name,
                "char": char,
                "mods": mods,
                "is_modifier": is_modifier,
                # 组合键才算快捷键；单按修饰键不算（否则 Shift 连按会被当成一堆快捷键）
                "is_shortcut": bool(mods) and not is_modifier,
                "command": (self.shortcut_map.key_command(name, mods)
                            if self.shortcut_map else None),  # 本地映射翻译
            }
            ts = self._record("stream2", "key_down", detail)
            self._maybe_ai_eye("key_down", detail, ts, self._cursor_pos)
        except Exception:
            pass  # 单条事件失败不影响整体采集

    def _on_key_release(self, key):
        try:
            with self._lock:
                self._pressed.discard(key)
            if not self.running or not self._allow_stream2():
                return
            name, _ = self._key_name_and_char(key)
            with self._lock:
                mods = self._current_mods()
            self._record("stream2", "key_up", {"key": name, "mods": mods})
        except Exception:
            pass

    def _on_move(self, x, y):
        try:
            self._cursor_pos = (int(x), int(y))  # 光标位置永远跟踪（AI 眼定位用）
            drag = self._drag
            if drag is None:
                return
            # 拖动期间按"移动 ≥3 像素"抽稀记录途经点（上限 max_points 个）
            last = drag["points"][-1]
            dist = math.hypot(x - last[0], y - last[1])
            if dist >= 3 and len(drag["points"]) < drag["max_points"]:
                drag["points"].append((int(x), int(y)))
        except Exception:
            pass

    def _on_click(self, x, y, button, pressed):
        try:
            if not self.running:
                return
            bname = getattr(button, "name", str(button))
            # 笔势结算：动作"始于建模窗口"就应结算，不管松开时窗口还在不在
            # （按下后拖出去/切走也要把这次笔势记完整）
            if not pressed:
                drag = self._drag
                self._drag = None
                if (drag and drag["button"] == bname
                        and self.cfg.get("gestures", {}).get("enabled", True)):
                    sx, sy = drag["start"]
                    dx, dy = int(x) - sx, int(y) - sy
                    dist = math.hypot(dx, dy)
                    min_px = float(self.cfg.get("gestures", {}).get("min_distance_px", 20))
                    if dist >= min_px:
                        direction = classify_gesture_direction(dx, dy)
                        ginfo = None
                        if self.shortcut_map:
                            # 按当前 SW 文档类型选映射上下文（零件/装配/工程图）
                            doc_type = None
                            if self.stream1:
                                try:
                                    doc_type = self.stream1.status().get("doc_type")
                                except Exception:
                                    doc_type = None
                            ginfo = self.shortcut_map.gesture_command(direction, doc_type)
                        detail = {
                            "button": bname,
                            "direction": direction,
                            "command": (ginfo or {}).get("name"),
                            "command_id": (ginfo or {}).get("id"),
                            "start": [sx, sy],
                            "end": [int(x), int(y)],
                            "distance_px": round(dist, 1),
                            "duration_ms": now_ms() - drag["start_ts"],
                        }
                        if self.cfg.get("gestures", {}).get("record_polyline", True):
                            detail["polyline"] = drag["points"]
                        ts = self._record("stream2", "gesture", detail)
                        self._maybe_ai_eye("gesture", detail, ts, (int(x), int(y)))
            if not self._allow_stream2():
                return  # 装聋：非建模窗口的点击不记（笔势结算在上面，不受影响）
            if pressed:
                # 开始记录一次"按住拖动"（笔势候选）
                gcfg = self.cfg.get("gestures", {})
                self._drag = {
                    "button": bname,
                    "start": (int(x), int(y)),
                    "start_ts": now_ms(),
                    "points": [(int(x), int(y))],
                    "max_points": int(gcfg.get("max_points", 60)),
                }
            # 点击事件照旧记录（按下/抬起都记，是原始事实层）
            title, exe = self._current_window
            detail = {
                "button": bname,
                "action": "press" if pressed else "release",
                "x": int(x),
                "y": int(y),
                "window": title,
                "exe": exe,
            }
            ts = self._record("stream2", "click", detail)
            if pressed:
                self._maybe_ai_eye("click", detail, ts, (int(x), int(y)))
        except Exception:
            pass

    # ---------- 内部：窗口轮询（只记"进入建模窗口"） ----------

    def _window_loop(self):
        while not self._stop_flag.is_set():
            info = foreground_window_info()
            with self._lock:
                changed = info != self._current_window
                old = self._current_window
                if changed:
                    self._current_window = info
            if changed and info != (None, None):
                apps = self.cfg.get("modeling_apps", [])
                allow_all = self.cfg.get("debug", {}).get("allow_all_windows", False)
                new_is_modeling = is_modeling_window(info, apps, allow_all=allow_all)
                old_is_modeling = is_modeling_window(old, apps, allow_all=allow_all)
                # 只记"从非建模窗口切进建模窗口"的边界；
                # 建模窗口内部标题抖动不算。
                if new_is_modeling and not old_is_modeling:
                    self._record("stream2", "window_enter", {
                        "title": info[0],
                        "exe": info[1],
                        "prev_title": old[0],
                        "prev_exe": old[1],
                    })
            self._stop_flag.wait(0.5)

    # ---------- 内部：离开检测（无操作超过 idle_minutes） ----------

    def _maybe_leave(self):
        """查一次"是不是该记离开了"。_idle_loop 周期调用；测试可直接调用。"""
        if not self.running:
            return
        with self._lock:
            last = self._last_event_ts
            in_leave = self._in_leave
        if last is None or in_leave:
            return
        if (now_ms() - last) < self._idle_ms:
            return
        # 只有"建模窗口在前台"才算离开（切走窗口本来就是装聋，不记）
        if not self._allow_stream2():
            return
        self._record("meta", "leave", {
            "idle_minutes": round((now_ms() - last) / 60000.0, 1),
            "since_ts": last,
        })
        with self._lock:
            self._in_leave = True
            self._leave_count += 1

    def _idle_loop(self):
        # 检查间隔自适应：阈值大就查得稀（最长 15 秒），阈值小就查得密（测试友好）
        wait_s = min(15.0, max(1.0, self._idle_ms / 3000.0))
        while not self._stop_flag.is_set():
            self._stop_flag.wait(wait_s)
            if self._stop_flag.is_set():
                break
            self._maybe_leave()
