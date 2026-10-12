# -*- coding: utf-8 -*-
"""
input_listener.py —— 采集层：全局键盘/鼠标监听 + 当前窗口跟踪 + 装聋过滤

版本记录：
- v2：装聋原则（只记建模窗口前台的操作）、window_enter、离开/回来、不记停顿
- v2.x 新增：
  1. 笔势记录：按住鼠标键拖动（SW 右键笔势导引这类操作），记 gesture 事件：
     8 方向判定 + 起终点 + 时长 + 途经点（可选）；
  2. AI 眼触发点：点击/快捷键/字母键/笔势发生时，把"光标位置 + 原始事件"
     交给 ai_eye 模块（截图→云端识别），结果以 stream2/ai_eye 事件入库。

本文件不含界面；流1 桥接在 solidworks_bridge.py，AI 眼在 ai_eye.py。
"""
import ctypes
import ctypes.wintypes as wt
import math
import threading
from pathlib import Path

from pynput import keyboard, mouse

from collector_core import EventStore, now_ms

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


def classify_gesture_direction(dx, dy):
    """把"起点→终点"的位移向量归到 8 个方向之一（屏幕坐标 y 向下）。

    返回方向中文名；位移为 0 时返回 None。纯函数，可单测。
    """
    if dx == 0 and dy == 0:
        return None
    deg = (math.degrees(math.atan2(-dy, dx)) + 360.0) % 360.0
    sector = int(deg / 45.0 + 0.5) % 8
    return GESTURE_DIRECTIONS[sector]


def foreground_window_info():
    """拿到"当前最前面的窗口"的 (标题, 程序名)。

    取不到时返回 (None, None)，绝不因为一个窗口报错就停下整个采集。
    """
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None, None
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or None

        pid = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = None
        if pid.value:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
            )
            if handle:
                size = wt.DWORD(4096)
                path_buf = ctypes.create_unicode_buffer(size.value)
                ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    handle, 0, path_buf, ctypes.byref(size)
                )
                ctypes.windll.kernel32.CloseHandle(handle)
                if ok:
                    exe = Path(path_buf.value).name
        return title, exe
    except Exception:
        return None, None


def is_modeling_window(info, modeling_apps, allow_all=False):
    """判断一个 (标题, 程序名) 是不是建模软件窗口。

    - allow_all=True（调试开关）：什么窗口都算。
    - exe 完全匹配，或标题包含关键词，都算命中。
    """
    if allow_all:
        return True
    title, exe = info
    if not title and not exe:
        return False
    for app in modeling_apps:
        app_exe = (app.get("exe") or "").lower()
        app_title = (app.get("title") or "").lower()
        if app_exe and exe and exe.lower() == app_exe:
            return True
        if app_title and title and app_title in title.lower():
            return True
    return False


class CollectorSession:
    """一次采集会话：存储 + 流2监听 + 装聋过滤 + 笔势 + 离开/回来 + 流1桥 + AI眼。

    用法（界面代码就是这么用的）：
        s = CollectorSession(data_dir)
        s.start()          # 开始记录（重新读一遍配置）
        ...（期间所有合规事件自动落库）...
        s.stop()           # 停止记录
    """

    def __init__(self, data_dir):
        from collector_core import ConfigManager

        self.store = EventStore(Path(data_dir) / "events.db")
        self.cfg_mgr = ConfigManager(Path(data_dir))
        self.cfg = self.cfg_mgr.load()

        self.session_id = None
        self.running = False
        self.started_at = None
        self.error = None
        self.stream1 = None          # 第一个流1 桥（保留给笔势取文档类型用）
        self.stream1s = []           # 全部流1 桥（solidworks/creo/nx）
        self.ai_eye = None           # AI 眼（截图→云端识别），start() 里按配置创建
        self.shortcut_map = None     # SW 快捷键/笔势映射表（本地翻译用）

        self._lock = threading.Lock()
        self._seq = 0
        self._counts = {"stream1": 0, "stream2": 0, "meta": 0}
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

    # ---------- 对外：开始 / 停止 ----------

    def start(self):
        try:
            self.cfg = self.cfg_mgr.load()  # 每次开始都重读设置，改动即时生效
            idle_minutes = float(self.cfg.get("idle_minutes", 5.0))
            self._idle_ms = max(idle_minutes, 0.1) * 60_000
            self.session_id = self.store.start_session()
            self.started_at = now_ms()
            self._last_event_ts = self.started_at
            # 快捷键/笔势映射表（本地翻译：d→拉伸 这类）
            try:
                from sw_shortcuts import ShortcutMap
                self.shortcut_map = ShortcutMap(Path(self.store.db_path).parent)
            except Exception:
                self.shortcut_map = None
            self._record("meta", "session_start", {"data_dir": str(self.store.db_path.parent)})

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
                            bridge = NxBridge(
                                cfg=self.cfg.get("nx", {}),
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
                        images_dir=Path(self.store.db_path).parent / "images",
                    )
                    self.ai_eye.start()
                except Exception as exc:
                    self.error = f"AI眼启动失败: {exc}"

            self.running = True
        except Exception as exc:  # 启动失败（少见，一般是系统权限问题）
            self.error = str(exc)
            raise

    def stop(self):
        if not self.running:
            return
        self.running = False
        self._stop_flag.set()
        # 1. 停掉所有采集来源
        if self.ai_eye:
            try:
                self.ai_eye.stop()
            except Exception:
                pass
        for bridge in self.stream1s:
            try:
                bridge.stop()
            except Exception:
                pass
        if self._kb_listener:
            self._kb_listener.stop()
        if self._ms_listener:
            self._ms_listener.stop()
        if self._win_thread:
            self._win_thread.join(timeout=2)
        if self._idle_thread:
            self._idle_thread.join(timeout=2)
        # 2. 三方对账（按文档 5.2：缺票自动标异常），结果写成 meta/anomaly
        if self.cfg.get("reconcile", {}).get("enabled", True):
            try:
                from reconcile import run_on_session
                n = run_on_session(self, self.cfg)
                if n:
                    self._last_error_note = f"对账标出 {n} 条异常"
            except Exception:
                pass
        # 3. 写结束标记并结算
        self._record("meta", "session_stop", {"event_count": self._total()})
        self.store.end_session(self.session_id, self._total())

    def stats(self):
        """给界面读的实时数字。"""
        with self._lock:
            return {
                "running": self.running,
                "counts": dict(self._counts),
                "leave_count": self._leave_count,
                "started_at": self.started_at,
                "session_id": self.session_id,
                "current_window": self._current_window,
                "in_leave": self._in_leave,
                "stream1_status": [b.status() for b in self.stream1s] if self.stream1s else None,
                "ai_eye_status": self.ai_eye.status() if self.ai_eye else None,
            }

    # ---------- 内部：流1 事件落库（v3：选中变化还要喂给 AI 眼） ----------

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
        """
        ts = ts or now_ms()
        with self._lock:
            # 之前记过"离开"，现在有活动了 → 先补一条"回来"
            if self._in_leave:
                self._seq += 1
                self.store.add_event(self.session_id, self._seq, "meta", "return",
                                     {"since_ts": self._last_event_ts}, ts)
                self._counts["meta"] += 1
                self._in_leave = False
            self._seq += 1
            self.store.add_event(self.session_id, self._seq, source, kind, detail, ts)
            self._counts[source if source in self._counts else "meta"] += 1
            # 注意：AI 眼结果挂的是原事件的时间戳（可能早于当前），
            # 时钟只能前进不能倒退，否则离开检测会被回拨干扰。
            if self._last_event_ts is None or ts > self._last_event_ts:
                self._last_event_ts = ts
            return ts

    # ---------- 内部：AI 眼触发 ----------

    def _maybe_ai_eye(self, kind, detail, ts, pos):
        """决定这次事件要不要触发 AI 眼截图。"""
        if not self.ai_eye:
            return
        try:
            self.ai_eye.trigger(kind, detail, ts, pos)
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
