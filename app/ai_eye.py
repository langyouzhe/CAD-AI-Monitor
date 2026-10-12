# -*- coding: utf-8 -*-
"""
ai_eye.py —— AI 眼模块（v2.x，实现文档「v3 规格」的视觉维度）

按 2026-10-11 定案的 v3 规格：
1. 触发：点击 / 选中特征变化 / 快捷键 / 笔势 发生时，截**整个建模窗口**一张图
   （悬停触发、视频片段：后续可选项，暂不做）；
2. 解读：BYOK——设置里填 api_base / api_key / model（OpenAI 兼容），送云端
   多模态模型，返回一句话结论（点了什么 / 界面状态 / 有无报错），按时间戳
   与流1 事件配对落库；
3. BYOK：不填 key → 视觉功能整体关闭（零成本）；
4. 存储：图存本地 data\\images\\（save_images 默认 true，攒"特征视觉图鉴"）；
   不勾 save_images = 截图只在内存里转一圈，磁盘上从头到尾不留图；
5. 原则：视觉不做数值判断（见文档 5.2）。
"""
import base64
import io
import json
import queue
import threading
import time
import urllib.request
from datetime import datetime
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


def _virtual_screen_rect():
    """虚拟屏幕矩形（覆盖所有显示器，左上角可能是负坐标）。"""
    try:
        import win32api
        # 76=X 起点 77=Y 起点 78=宽 79=高（虚拟屏幕 = 所有显示器合起来的大矩形）
        return (win32api.GetSystemMetrics(76), win32api.GetSystemMetrics(77),
                win32api.GetSystemMetrics(78), win32api.GetSystemMetrics(79))
    except Exception:
        return (0, 0, 1920, 1080)  # 兜底：主屏分辨率


def _screen_scale():
    """逻辑坐标 → 物理像素的换算系数（本程序是 DPI 不感知进程）。

    为什么不按"窗口所在显示器"的 DPI 算：DPI 不感知进程看到的是被 Windows
    按**系统 DPI（主显示器）**统一缩放过的整个虚拟桌面。用系统 DPI 才和
    GetWindowRect / GetSystemMetrics 的读数一致。
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


def _virtual_origin_physical():
    """截图（all_screens=True）坐标系原点在真实桌面里的位置（物理像素）。

    多显示器时虚拟桌面左上角可能是负坐标（副屏在主屏左/上方），
    裁剪前要减掉这个原点。
    """
    try:
        import win32api
        s = _screen_scale()
        return (win32api.GetSystemMetrics(76) * s, win32api.GetSystemMetrics(77) * s)
    except Exception:
        return (0.0, 0.0)


class AiEye:
    """AI 眼：触发队列 + 后台识别线程。"""

    def __init__(self, cfg: dict, on_result, is_running, images_dir: Path):
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
        # 推理型多模态模型（如 deepseek-flash）会先"思考"再回答：额度给太小，
        # 思考就把额度吃光 → 正式回答为空。所以默认给足，并允许设置里调。
        self.max_tokens = int(self.cfg.get("max_tokens", 4000))
        self.http_timeout_s = float(self.cfg.get("http_timeout_s", 90))
        self.images_dir = Path(images_dir)
        self.on_result = on_result      # (detail, ts) -> None
        self.is_running = is_running    # () -> bool

        self._queue = queue.Queue(maxsize=self.max_queue)
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = {"provider": self.provider, "queued": 0, "ok": 0, "fail": 0,
                       "skipped": 0, "last_result": None, "msg": ""}
        self._last_trigger_ms = 0       # 连点合并用
        self._minute_bucket = [0, 0]    # (窗口起始秒, 张数)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def status(self):
        with self._lock:
            return dict(self._state)

    # ---------- 触发决策 ----------

    def trigger(self, kind, detail, ts_ms, pos=None):
        """采集器每记一条相关事件都会喊一次 trigger，这里决定截不截。"""
        if not self.is_running():
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
                                    "ts": ts_ms, "pos": pos})
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
            image_path = None
            try:
                time.sleep(max(0.0, self.delay_s))  # 等菜单/对话框弹出来
                image_path, img_b64 = self._capture(job)
                result = self._recognize(job, img_b64)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)[:200]}
            detail = {
                "annotates": job["kind"],
                "annotates_key": (job["detail"].get("key")
                                  or job["detail"].get("button")
                                  or job["detail"].get("direction")
                                  or job["detail"].get("event_type")),
                "provider": self.provider,
                "image": image_path,
                "result": result,
            }
            with self._lock:
                if result.get("ok"):
                    self._state["ok"] += 1
                    self._state["last_result"] = str(result.get("answer", {}).get("summary", ""))[:80]
                else:
                    self._state["fail"] += 1
                    self._state["msg"] = str(result.get("error", ""))[:80]
            # 结果挂回原事件时间戳（对齐），由采集器落库
            self.on_result(detail, job["ts"])

    def _capture(self, job):
        """截"整个建模窗口" → 返回 (图片路径或 None, base64)。

        勾了"保存截图"（keep_images=true）：写到本地 images\\<日期>\\ 并返回路径；
        没勾：只留内存里的 base64，磁盘从头到尾不落图。
        """
        from PIL import ImageGrab
        # 抓**所有显示器**（双屏时副屏上的建模窗口也能截到）
        full = ImageGrab.grab(all_screens=True)
        box = self._foreground_window_box(full.size)
        img = full.crop(box) if box else full       # 按窗口裁剪；拿不到窗口就整屏
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        path = None
        if self.keep_images:
            day = datetime.now().strftime("%Y%m%d")
            folder = self.images_dir / day
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{job['ts']}_{job['kind']}.jpg"
            path.write_bytes(buf.getvalue())
        return (str(path) if path else None, img_b64)

    @staticmethod
    def _foreground_window_box(img_size):
        """前台窗口在截图里的裁剪框（**物理像素**，跨显示器通用）。

        换算链：逻辑坐标 ×系统DPI缩放 → 物理桌面坐标 → 减去虚拟屏原点
        → 钳制在截图范围内。窗口最小化/异常时返回 None（调用方退化为整屏）。
        """
        try:
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)   # 逻辑坐标
            scale = _screen_scale()                                   # 系统 DPI 缩放
            ox, oy = _virtual_origin_physical()                       # 虚拟屏原点
            left = int(round(left * scale - ox))
            top = int(round(top * scale - oy))
            right = int(round(right * scale - ox))
            bottom = int(round(bottom * scale - oy))
            w, h = img_size
            left, top = max(0, left), max(0, top)
            right, bottom = min(w, right), min(h, bottom)
            if right - left < 50 or bottom - top < 50:
                return None      # 矩形不合理（最小化/异常）→ 整屏兜底
            return (left, top, right, bottom)
        except Exception:
            return None

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
