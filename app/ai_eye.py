# -*- coding: utf-8 -*-
"""
ai_eye.py —— AI 眼模块（v2.x，实现文档「v3 规格」的视觉维度）

按 2026-10-11 定案的 v3 规格：
1. 触发：点击 / 选中特征变化 / 快捷键 / 笔势 发生时，截**触发时那一个建模窗口**
   一张图（悬停触发、视频片段：后续可选项，暂不做）；
   **隐私红线（2026-10-23 全程序检查阻断项，2026-10-23 二次复验加固）**：
   触发时记录窗口身份（hwnd + exe/title + 触发时刻）；**抓取时用 Windows
   PrintWindow 直接取该窗口自己的画面**（不再整屏 grab 再裁剪，也没有整屏
   兜底）。抓取前复核：hwnd 存活、仍在前台、exe/title 与触发时一致；抓取后
   **再复核一次**（窗口仍在/没被切走），任何一次不通过都跳过并丢弃图片——
   不保存、不上传、不写识别结果。窗口最小化/矩形无效在抓取前就拒绝。
2. 解读：BYOK——设置里填 api_base / api_key / model（OpenAI 兼容），送云端
   多模态模型，返回一句话结论（点了什么 / 界面状态 / 有无报错），按时间戳
   与流1 事件配对落库；
3. BYOK：不填 key → 视觉功能整体关闭（零成本）；
4. 存储：图存**本次会话**的 images\\（v4：data\\sessions\\<开始时刻>\\images\\，不再按日期分层；
   save_images 默认 true，攒"特征视觉图鉴"）；不勾 save_images = 截图只在内存里转一圈，
   磁盘上从头到尾不留图；
5. 停止时**先排空队列**（等在途云端请求做完，上限 drain_timeout_s 默认 5 秒）再关库，
   保证识别结果归入本次会话（v4 施工注意事项）；
6. 原则：视觉不做数值判断（见文档 5.2）。
"""
import base64
import ctypes
import io
import json
import queue
import threading
import time
import urllib.request
from pathlib import Path

# 服务商预设（OpenAI 兼容接口；api_base 留空时按 provider 取预设）
PROVIDERS = {
    "zhipu": {"api_base": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4v-flash"},
    "qwen": {"api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-vl-plus"},
    "doubao": {"api_base": "https://ark.cn-beijing.volces.com/api/v3", "model": ""},
}

# 给视觉模型的固定指令：只输出 JSON（一句话结论 + 有无报错）
PROMPT = (
    "这是一张机械CAD软件界面的整窗截图。请用 JSON 回答（只输出 JSON，不要任何其他文字）："
    '{"summary": "一句话结论：用户点了什么/正在做什么", '
    '"ui_state": "当前界面状态（什么对话框/工具栏可见、视图方向）", '
    '"error": "有无报错弹窗，有则描述内容，没有写 null"}'
)


# 按窗口抓取位图的最小合法尺寸（更小 = 最小化/异常窗口，抓取前就拒绝）
MIN_CAPTURE_SIZE = 50

# 明显是坏值（异常矩形/内存攻击）的尺寸上限：超过就不抓
MAX_CAPTURE_SIZE = 100000

# PrintWindow 的"渲染完整内容"标志（Win10+ 才认；失败后回退老式调用）
PW_RENDERFULLCONTENT = 0x00000002


def _screen_scale():
    """逻辑坐标 → 物理像素的换算系数（本程序是 DPI 不感知进程）。

    为什么还要它：GetWindowRect 在 DPI 不感知进程里返回**被系统缩放过的逻辑
    尺寸**，而 PrintWindow 把窗口自己的内容渲染进我们给的位图。按物理像素开
    位图，DPI 感知的目标窗口才不会只画左上角（多出来的部分顶多是黑边，不会
    丢内容）。用系统 DPI：和 GetWindowRect 的读数同源。
    （例：150% 缩放时，2560 宽的屏幕在本进程里只报 1707 宽。）
    """
    try:
        import ctypes
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return float(dpi) / 96.0
    except Exception:
        pass
    return 1.0


class CaptureSkipped(Exception):
    """截图任务被跳过：窗口身份复核没通过，或窗口框无效/抓取失败。

    语义：**不抓取、不保存、不上传、不写识别结果、不算识别失败**。
    只是把原因记到状态里给用户看（"宁可不截图，也绝不截错窗口"）。
    """


class WindowCaptureUnavailable(RuntimeError):
    """拿不到窗口画面（窗口句柄无效/系统拒绝），但这不是程序 bug。

    单独一类的原因：自检要把"明确的抓屏系统错误"归为环境不可用（SKIP），
    而 ValueError/MemoryError/RuntimeError 等程序错误必须让自检失败。
    """


def window_capture_size(rect, scale=1.0):
    """纯函数：窗口矩形（逻辑坐标）→ 按窗口抓取位图的像素尺寸 (w, h)。

    只做"这个窗口现在能不能抓"的判定：矩形必须能解包，宽高 ≥
    MIN_CAPTURE_SIZE（更小 = 最小化/异常）。无效矩形 → None，调用方
    **必须在抓取前就拒绝**——绝不允许先抓整屏再裁。

    这里不涉及任何屏幕坐标/裁剪：抓的是窗口自己的内容，只按窗口尺寸开位图。
    可单测，不需要真实屏幕。
    """
    try:
        left, top, right, bottom = rect
        width = int(round((float(right) - float(left)) * float(scale)))
        height = int(round((float(bottom) - float(top)) * float(scale)))
    except Exception:
        return None
    if width < MIN_CAPTURE_SIZE or height < MIN_CAPTURE_SIZE:
        return None
    if width > MAX_CAPTURE_SIZE or height > MAX_CAPTURE_SIZE:
        return None
    return (width, height)


def _grab_window_content(hwnd, size):
    """默认窗口抓取器：Windows PrintWindow 把**该窗口自己的内容**画进位图。

    隐私红线：这里从头到尾没有屏幕截图，也没有"先截整屏再裁剪"的路径；
    拿不到窗口画面就抛异常，由调用方跳过该任务（不保存、不上传）。
    顺序：PrintWindow(PW_RENDERFULLCONTENT) → PrintWindow(0) → 窗口 DC BitBlt
    （最后一种仍是"按窗口取画面"，取的是窗口 DC，不是屏幕 DC）。
    """
    from PIL import Image
    import win32gui
    import win32ui
    import win32con

    w, h = int(size[0]), int(size[1])
    if w <= 0 or h <= 0:
        raise ValueError(f"窗口抓取尺寸无效: {size!r}")
    hwnd_dc = None
    mfc_dc = save_dc = bmp = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        if not hwnd_dc:
            # 窗口句柄无效/系统拒绝：属于环境型抓屏失败（由调用方跳过）
            raise WindowCaptureUnavailable(f"拿不到窗口 DC（hwnd={hwnd}）")
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        # 1) 首选：完整内容（DWM 合成窗口也能拿到）
        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(),
                                              PW_RENDERFULLCONTENT)
        if not ok:
            # 2) 老系统/个别窗口不认新标志 → 退回老式 PrintWindow
            ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        if not ok:
            # 3) 最后兜底：从**窗口 DC** BitBlt（依旧不是屏幕）
            save_dc.BitBlt((0, 0), (w, h), mfc_dc, (0, 0), win32con.SRCCOPY)
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (w, h), bits, "raw", "BGRX", 0, 1).copy()
        if img.size != (w, h):
            raise RuntimeError(f"窗口抓取结果尺寸不对: {img.size} != {(w, h)}")
        return img
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        for dc in (save_dc, mfc_dc):
            try:
                if dc is not None:
                    dc.DeleteDC()
            except Exception:
                pass
        try:
            if hwnd_dc:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def _identity_matches(identity, state):
    """触发时身份 vs 此刻状态：exe/title 必须一致（exe 大小写不敏感）。

    只比 hwnd 还不够：句柄可能被复用，或者同一个进程换了窗口/标题。
    两边都读不到（None/空）时视为一致；只要一边读到了、另一边不同（包括
    一边读不到），就按"不一致"处理——宁可跳过，也不冒"截到别的窗口像素"的
    风险（隐私红线）。
    """
    identity = identity or {}
    state = state or {}
    want_exe = str(identity.get("exe") or "").strip().casefold()
    got_exe = str(state.get("exe") or "").strip().casefold()
    if want_exe != got_exe:
        return False
    want_title = str(identity.get("title") or "").strip()
    got_title = str(state.get("title") or "").strip()
    return want_title == got_title


def verify_capture_window(identity, modeling_apps, state=None,
                          allow_all_windows=False):
    """抓取前后复核"要抓的还是触发时那一个 CAD 窗口吗"（纯函数，可单测）。

    参数：
        identity: 触发时记录的窗口身份
                  {"hwnd": int, "title": str, "exe": str, "ts": int}
        modeling_apps: 建模软件名单（DEFAULT_CONFIG["modeling_apps"]）
        state: 注入的"此刻窗口状态"（单测用），None = 现查系统：
               {"alive": bool, "foreground": bool, "title": str, "exe": str}
        allow_all_windows: 调试开关（debug.allow_all_windows），与装聋判定同源。
                  只放行"是不是 CAD 窗口"这一条，**不放行身份一致性**。

    返回 (ok, reason)。ok=False → 调用方**必须跳过**（不抓取、不保存、不上传）。
    """
    identity = identity or {}
    try:
        hwnd = int(identity.get("hwnd") or 0)
    except (TypeError, ValueError):
        hwnd = 0
    if not hwnd:
        # 兼容旧路径：调用方没给 hwnd → 跳过。绝不自己猜"当前前台窗口"。
        return False, "触发时没拿到窗口句柄 hwnd，跳过（不猜窗口、不截整屏）"
    if state is None:
        # 延迟导入：窗口 API 统一放在 input_listener（避免模块级循环导入）
        from input_listener import window_state
        state = window_state(hwnd)
    state = state or {}
    if not state.get("alive"):
        return False, f"目标窗口已关闭（hwnd={hwnd}），跳过"
    if not state.get("foreground"):
        return False, "前台已切换到别的窗口，跳过（不截非 CAD 窗口）"
    # 身份一致性先于"是不是 CAD"：标题/exe 与触发时不一致 → 已经不能证明
    # 还是同一个窗口，直接跳过（哪怕它现在仍是个 CAD 窗口）。
    if not _identity_matches(identity, state):
        return False, (
            "窗口身份已变化（exe/title 与触发时不一致：exe=%r/%r，title=%r/%r），跳过"
            % (identity.get("exe"), state.get("exe"),
               identity.get("title"), state.get("title")))
    from input_listener import is_modeling_window
    if not is_modeling_window((state.get("title"), state.get("exe")),
                              modeling_apps or [],
                              allow_all=allow_all_windows):
        return False, (
            "目标窗口已不再是 CAD 窗口（exe=%r，title=%r），跳过"
            % (state.get("exe"), state.get("title")))
    return True, ""


class AiEye:
    """AI 眼：触发队列 + 后台识别线程。"""

    def __init__(self, cfg: dict, on_result, is_running, images_dir: Path,
                 modeling_apps=None, allow_all_windows=False,
                 window_probe=None, rect_probe=None, grab_window=None,
                 scale_probe=None):
        self.cfg = cfg or {}
        self.provider = self.cfg.get("provider", "mock")
        self.api_key = self.cfg.get("api_key", "")
        preset = PROVIDERS.get(self.provider, {})
        # v3 规格命名：api_base / api_key / model（兼容旧的 base_url）
        self.api_base = (self.cfg.get("api_base")
                         or self.cfg.get("base_url")
                         or preset.get("api_base", ""))
        self.model = self.cfg.get("model") or preset.get("model", "")
        self.delay_s = float(self.cfg.get("delay_s", 0.15))
        self.triggers = self.cfg.get("triggers", {})
        self.rate_per_min = int(self.cfg.get("rate_per_min", 60))
        self.max_queue = int(self.cfg.get("max_queue", 20))
        self.dedup_s = float(self.cfg.get("dedup_window_s", 0.5))
        self.keep_images = bool(self.cfg.get("save_images", True))
        # 推理型多模态模型会先"思考"再回答：额度给太小，思考就把额度吃光 →
        # 正式回答为空。所以默认给足，并允许设置里调。
        self.max_tokens = int(self.cfg.get("max_tokens", 4000))
        self.http_timeout_s = float(self.cfg.get("http_timeout_s", 90))
        # 停止采集时等在途云端请求做完的上限（秒）：排空队列后再关库
        self.drain_timeout_s = float(self.cfg.get("drain_timeout_s", 5.0))
        self.images_dir = Path(images_dir)
        self.on_result = on_result      # (detail, ts) -> None
        self.is_running = is_running    # () -> bool
        # 截图前复核用：建模软件名单 + 调试开关（与 input_listener 装聋判定同源）
        self.modeling_apps = list(modeling_apps or [])
        self.allow_all_windows = bool(allow_all_windows)
        # 可注入的系统探测（确定性单测用：不碰真实屏幕/真实窗口）
        self._window_probe = window_probe   # (identity) -> state dict（抓取前、后各查一次）
        self._rect_probe = rect_probe       # (hwnd) -> (left, top, right, bottom) 逻辑坐标
        self._grab_window = grab_window     # (hwnd, (w, h)) -> PIL.Image（**按窗口**抓画面）
        self._scale_probe = scale_probe     # () -> 系统 DPI 缩放系数（单测注入 1.0）

        self._queue = queue.Queue(maxsize=self.max_queue)
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._accepting = False         # 是否还接收新触发（stop 后不再接）
        self._in_flight = False         # 是否正在处理一张图（截图/云端识别）
        self._state = {"provider": self.provider, "queued": 0, "ok": 0, "fail": 0,
                       "skipped": 0, "last_result": None, "msg": ""}
        self._last_trigger_ms = 0       # 连点合并用
        self._minute_bucket = [0, 0]    # (窗口起始秒, 张数)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._accepting = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self, drain_timeout_s=None):
        """停止识别线程：先把已排队的触发做完（在途云端请求等最多 drain_timeout_s 秒）。

        为什么先排空：停止时如果直接杀线程，正在云端识别的结果就丢了，
        "这次会话里点了什么"会缺票。排空之后再让采集器关库，结果才能归入本次会话。
        返回排空统计（drained_s / pending / in_flight），方便界面上说明"还有几张没等回来"。
        """
        self._accepting = False         # 不再接新触发，但队列里已排的继续做完
        timeout = (self.drain_timeout_s if drain_timeout_s is None
                   else float(drain_timeout_s))
        started = time.monotonic()
        quiet = 0
        while (time.monotonic() - started) < max(0.0, timeout):
            with self._lock:
                busy = self._in_flight
            if self._queue.empty() and not busy:
                # 连查两次"确实空了"再收手：避免正好卡在
                # "工作线程已取出任务、还没来得及记 in_flight"的那一下
                quiet += 1
                if quiet >= 2:
                    break
            else:
                quiet = 0
            time.sleep(0.02)
        drained_s = time.monotonic() - started
        self._stop.set()                # 队列空了（或等超时）→ 让线程收尾退出
        if self._thread:
            self._thread.join(timeout=1.5)
        with self._lock:
            pending = self._queue.qsize()
            if pending:
                self._state["skipped"] += pending
            info = {"drained_s": round(drained_s, 2), "pending": pending,
                    "in_flight": bool(self._in_flight)}
        return info

    def status(self):
        with self._lock:
            st = dict(self._state)
            st["queued"] = self._queue.qsize()   # 实时排队数（含正在处理的那张）
            return st

    # ---------- 触发决策 ----------

    def trigger(self, kind, detail, ts_ms, pos=None, window=None):
        """采集器每记一条相关事件都会喊一次 trigger，这里决定截不截。

        window = 触发时的窗口身份 {"hwnd", "title", "exe", "ts"}（由
        input_listener._maybe_ai_eye 提供）。任务队列带着它走；后台截图前
        会拿它复核"还是不是那个 CAD 窗口"。不给 hwnd 的任务会被跳过
        （不截图、不上传），绝不退化成"再读一次当前前台窗口"。
        """
        if not self._accepting or not self.is_running():
            return
        # 1) 事件类型 → 触发开关
        if kind == "click":
            if detail.get("button") == "left" and not self.triggers.get("click_left"):
                return
            if detail.get("button") == "right" and not self.triggers.get("click_right"):
                return
            if detail.get("button") not in ("left", "right"):
                return
        elif kind == "selectionChanged":        # v3 规格：选中特征变化也触发
            if not self.triggers.get("selection_changed", True):
                return
        elif kind == "key_down":
            if detail.get("is_modifier"):
                return  # 单独按 Shift/Ctrl/Alt 不算操作
            is_shortcut = bool(detail.get("mods"))
            if is_shortcut and not self.triggers.get("shortcut"):
                return
            if not is_shortcut:
                char = detail.get("char")
                if not (isinstance(char, str) and char.isalpha()):
                    return  # 数字/符号是输入数值，不截
                if not self.triggers.get("bare_letter"):
                    return
        elif kind == "gesture":
            if not self.triggers.get("gesture"):
                return
        else:
            return
        # 2) 连点合并
        now = time.monotonic()
        with self._lock:
            if now - self._last_trigger_ms < self.dedup_s:
                return
            self._last_trigger_ms = now
        # 3) 限流
        with self._lock:
            if now - self._minute_bucket[0] > 60:
                self._minute_bucket = [now, 0]
            if self._minute_bucket[1] >= self.rate_per_min:
                self._state["skipped"] += 1
                return
            self._minute_bucket[1] += 1
        # 4) 入队
        try:
            self._queue.put_nowait({"kind": kind, "detail": detail,
                                    "ts": ts_ms, "pos": pos,
                                    "window": dict(window) if window else None})
            with self._lock:
                self._state["queued"] += 1
        except queue.Full:
            with self._lock:
                self._state["skipped"] += 1

    # ---------- 后台识别线程 ----------

    def _worker(self):
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                self._in_flight = True
                self._state["queued"] = self._queue.qsize()
            try:
                image_path = None
                skipped_reason = None
                try:
                    time.sleep(max(0.0, self.delay_s))  # 等菜单/对话框弹出来
                    image_path, img_b64 = self._capture(job)
                    result = self._recognize(job, img_b64)
                except CaptureSkipped as exc:
                    # 窗口身份复核没通过 / 窗口框无效 → 跳过该任务。
                    # 不截图、不上传、不算失败、不写识别结果（"没看过"）。
                    skipped_reason = str(exc)
                    result = None
                except Exception as exc:
                    result = {"ok": False, "error": str(exc)[:200]}
                with self._lock:
                    if skipped_reason is not None:
                        self._state["skipped"] += 1
                        self._state["msg"] = f"已跳过：{skipped_reason}"[:120]
                    elif result.get("ok"):
                        self._state["ok"] += 1
                        self._state["last_result"] = str(
                            result.get("answer", {}).get("summary", ""))[:80]
                    else:
                        self._state["fail"] += 1
                        self._state["msg"] = str(result.get("error", ""))[:80]
                if result is None:
                    continue
                detail = {
                    "annotates": job["kind"],
                    "annotates_key": (job["detail"].get("key")
                                      or job["detail"].get("button")
                                      or job["detail"].get("direction")
                                      or job["detail"].get("event_type")),
                    "provider": self.provider,
                    "image": image_path,
                    # 相对会话文件夹的路径（v4）：整包拷走会话文件夹也不会断链
                    "image_rel": self._image_rel(image_path),
                    "result": result,
                }
                # 结果挂回原事件时间戳（对齐），由采集器落库
                self.on_result(detail, job["ts"])
            finally:
                with self._lock:
                    self._in_flight = False
                    self._state["queued"] = self._queue.qsize()

    def _image_rel(self, image_path):
        """截图相对会话文件夹的路径（images\\xxx.jpg）；没存图返回 None。"""
        if not image_path:
            return None
        try:
            return Path(image_path).relative_to(self.images_dir.parent).as_posix()
        except ValueError:
            return None

    def _probe_window_state(self, identity):
        """查一次目标窗口**此刻**状态（注入的探测优先；None = 现查系统）。"""
        if self._window_probe is not None:
            return self._window_probe(identity)
        from input_listener import window_state
        try:
            hwnd = int((identity or {}).get("hwnd") or 0)
        except (TypeError, ValueError):
            hwnd = 0
        return window_state(hwnd)

    def _window_rect(self, hwnd):
        """目标窗口矩形（逻辑坐标）。最小化/无效/已关闭 → None（抓取前就拒绝）。"""
        if self._rect_probe is not None:
            return self._rect_probe(hwnd)       # 单测注入：直接给窗口矩形
        try:
            import win32gui
            if not hwnd:
                return None
            if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
                return None
            return win32gui.GetWindowRect(hwnd)   # 逻辑坐标
        except Exception:
            return None

    def _scale(self):
        if self._scale_probe is not None:
            return float(self._scale_probe())
        return _screen_scale()

    def _probe_and_capture(self, identity):
        """探测 + 按窗口抓取 + 抓后复核 → 返回窗口尺寸的 PIL.Image。

        任何探测/矩形/抓取阶段的异常都向上抛（由 _capture 统一转 CaptureSkipped；
        已归类的 CaptureSkipped 原样保留）。**这里不编码、不落盘**。
        """
        # 1) 抓取前复核：窗口身份不一致/切走 → 一个像素都不抓
        state = self._probe_window_state(identity)
        ok, reason = verify_capture_window(
            identity, self.modeling_apps, state=state,
            allow_all_windows=self.allow_all_windows)
        if not ok:
            raise CaptureSkipped(reason)
        try:
            hwnd = int(identity.get("hwnd") or 0)
        except (TypeError, ValueError):
            hwnd = 0
        # 2) 窗口矩形无效（最小化/异常）→ 抓取前拒绝，绝不先抓整屏
        size = window_capture_size(self._window_rect(hwnd), self._scale())
        if size is None:
            raise CaptureSkipped("窗口框无效（最小化/异常），跳过（不抓取任何画面）")
        # 3) 只抓这个窗口自己的内容（PrintWindow）
        grab = self._grab_window if self._grab_window is not None else _grab_window_content
        img = grab(hwnd, size)
        if img is None or tuple(img.size) != tuple(size):
            raise CaptureSkipped("按窗口抓取结果异常，跳过（不保存/不上传）")
        # 4) 抓完再复核一次：这段时间窗口被关掉/切走 → 丢弃这张图
        post_state = self._probe_window_state(identity)
        ok2, reason2 = verify_capture_window(
            identity, self.modeling_apps, state=post_state,
            allow_all_windows=self.allow_all_windows)
        if not ok2:
            raise CaptureSkipped(f"抓取后复核失败，图已丢弃（不保存/不上传）: {reason2}")
        return img

    def _capture(self, job):
        """抓**触发时那一个建模窗口**的画面 → 返回 (图片路径或 None, base64)。

        隐私红线（2026-10-23 全程序检查阻断项 + 二次复验加固）：
        - **按窗口抓取**：用 PrintWindow 只把目标窗口自己的内容画进位图；
          没有整屏截图、没有"先整屏再裁剪"、没有整屏兜底；
        - 抓取前复核窗口身份（hwnd 存活 / 仍在前台 / exe+title 与触发时一致），
          不满足 → 抛 CaptureSkipped：不抓取、不保存、不上传、不写识别结果；
        - 窗口最小化/矩形无效 → 在抓取前就拒绝（抓取函数 0 调用）；
        - 抓取**再复核一次**（窗口仍在/没被切走）；失败 → 丢弃已抓图片，
          同样不保存、不上传、不回调；
        - **探测/矩形/抓取阶段抛任何未归类异常（如 OSError）也按跳过处理**：
          不保存、不上传、不回调、不算识别失败（第三轮复验 P0 边界）；
        - 旧路径没带 hwnd → 跳过（不猜"当前前台窗口"）。
        勾了"保存截图"（keep_images=true）：v4 写到**本次会话**的 images\\ 下
        （data\\sessions\\<开始时刻>\\images\\xxx.jpg，不再按日期分层）并返回路径；
        没勾：只留内存里的 base64，磁盘从头到尾不落图。
        """
        identity = (job or {}).get("window") or {}
        try:
            img = self._probe_and_capture(identity)
        except CaptureSkipped:
            raise
        except Exception as exc:
            # 探测/矩形/抓取阶段的异常统一按"跳过"：宁可不截，也不产生失败回调。
            raise CaptureSkipped(
                f"窗口探测/抓取异常，跳过（不保存/不上传/不回调）: "
                f"{str(exc)[:120]}") from exc
        # 只有前后两次复核都通过才编码/落盘。
        # 编码/写盘失败属于真实故障，照常上报 fail（不在这里吞）。
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        path = None
        if self.keep_images:
            self.images_dir.mkdir(parents=True, exist_ok=True)
            path = self.images_dir / f"{job['ts']}_{job['kind']}.jpg"
            n = 2
            while path.exists():   # 同一毫秒同类别再触发一次时别覆盖前一张
                path = self.images_dir / f"{job['ts']}_{job['kind']}_{n}.jpg"
                n += 1
            path.write_bytes(buf.getvalue())
        return (str(path) if path else None, img_b64)

    def _recognize(self, job, img_b64):
        """云端识别 → 返回 {"ok": true, "answer": {...}}"""
        if self.provider == "mock":
            return {"ok": True, "answer": {
                "summary": "mock 模式：未调用云端（配置里填 api_key 并把 provider 改为 zhipu/qwen/doubao/custom 后生效）",
                "ui_state": "mock",
                "error": None,
            }}
        if not self.api_key:
            return {"ok": False, "error": "未配置 api_key（AI 眼需要你自己的云端密钥）"}
        if not self.api_base:
            return {"ok": False, "error": f"provider={self.provider} 没有预设地址，请配置 api_base"}
        answer_text, truncated = self._call_vision(img_b64)
        if not (answer_text or "").strip():
            return {"ok": False,
                    "error": "模型没有返回内容（推理额度可能不够：把设置里的 max_tokens 调大）"}
        answer = self._parse_json(answer_text)
        result = {"ok": True, "answer": answer}
        if truncated:
            result["truncated"] = True  # 回答可能不完整（到上限被截断）
        return result

    def _call_vision(self, img_b64):
        """OpenAI 兼容的 /chat/completions 调用（重试一次）。

        返回 (回答文本, 是否被 max_tokens 截断)。
        推理型模型：正式回答在 content；content 为空时退用 reasoning_content，
        保证至少能存下模型的判断，而不是存一个空结果。
        """
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                ],
            }],
            "max_tokens": self.max_tokens,
            "temperature": 0,
        }
        req = urllib.request.Request(
            self.api_base.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        last_err = None
        for _ in range(2):  # 失败重试一次
            try:
                with urllib.request.urlopen(req, timeout=self.http_timeout_s) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                text = (msg.get("content") or "").strip()
                if not text:
                    text = (msg.get("reasoning_content") or "").strip()
                return (text, choice.get("finish_reason") == "length")
            except Exception as exc:
                last_err = exc
                time.sleep(2)
        raise RuntimeError(f"云端调用失败: {last_err}")

    @staticmethod
    def _parse_json(text):
        """把模型的回答抠出 JSON（去掉 ``` 围栏等杂音）。"""
        text = (text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            return json.loads(text)
        except Exception:
            a, b = text.find("{"), text.rfind("}")
            if a >= 0 and b > a:
                try:
                    return json.loads(text[a:b + 1])
                except Exception:
                    pass
        return {"summary": text[:300], "ui_state": "unknown", "error": None}
