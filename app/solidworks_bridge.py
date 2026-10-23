# -*- coding: utf-8 -*-
"""
solidworks_bridge.py —— 流1 桥：把 SolidWorks 里的变化接进采集器（v2 首版后端）

原理（大白话）：
- SolidWorks 对外开了一扇"COM 后门"，别的程序可以隔空问它：现在打开的是什么文档、
  选中了哪些面/边、特征树里有什么。这个文件就是那个"隔空提问的人"。
- 每隔 poll_interval_s 秒抓拍一次 SolidWorks 的状态（活动文档 / 选中 / 特征树），
  和上一次抓拍比对，有变化就生成事件交给采集器落库。
- SolidWorks 没开？桥安静地每隔几秒重试连接，采集器的流2 照常工作。

设计约定：
- 默认只"连接已经打开的 SolidWorks"（allow_launch=false），绝不擅自把 SW 拉起来；
- 所有 COM 调用都包了保护：SW 卡住/退出都不会带崩采集器；
- **断线要能看出来**：`snapshot_sw(strict=True)` 把"没有活动文档"（正常 None）和
  "COM 异常"（抛 ComSnapshotError）分开；而且异常判定覆盖**整棵快照**——
  活动文档、选中、特征树遍历（GetNextFeature/子特征）中途任何一次 COM 读取失败
  都算断线。桥遇到 COM 异常会标 connected=False、释放 COM 对象、下一轮重新
  GetActiveObject；重连后先抓基准再比对，**绝不用不完整快照去 diff**
  （否则会产出假的文档/特征删除事件）；
- 比对逻辑（diff_snapshot）是纯函数，不依赖 SW，可以单独做单元测试。
"""
import threading
import time

import cad_discovery

# 特征树遍历的安全上限（防止超大树把比对拖慢）
MAX_FEATURES = 1500
MAX_DEPTH = 6

# "没有这个成员"类错误：SW 各版本/各文档类型成员不完全一致，这种不算断线
_MEMBER_MISSING_MARKERS = ("-2147352573", "Member not found", "找不到成员",
                           "does not support", "不支持")

# 可选成员（如选中面的 Name）"没有这个名字"类错误：pywin32 有时抛
# AttributeError("IFace2.Name")，有时抛 DISP_E_UNKNOWNNAME。都属正常缺失。
_UNKNOWN_NAME_MARKERS = ("-2147352570", "DISP_E_UNKNOWNNAME",
                         "Unknown name", "unknown name", "未知名称")


class ComSnapshotError(RuntimeError):
    """SolidWorks COM 读取失败（SW 被关掉 / 卡死 / 拒绝调用）。

    用途：抓拍时区分"**没有打开文档**（正常 None）"和"**COM 断线**（异常）"。
    以前所有异常都被吞成空快照 → 桥以为文档/特征全没了，会产出假的
    documentActivated / featureRemoved 删除事件，而且一直握着失效的 COM 对象
    不重连。现在异常往上抛，桥据此断开、释放对象、下一轮重新 GetActiveObject。
    """


def _is_member_missing(exc):
    """这个异常是不是"该版本没有这个成员"（正常，不该当成断线）。"""
    msg = str(exc)
    return any(m in msg for m in _MEMBER_MISSING_MARKERS)


def _is_optional_member_missing(exc):
    """可选成员（选中对象的 Name 等）是不是"本来就没有这个成员"。

    第三个复验的阻断点：pywin32 对不存在的 dispatch 成员会抛
    `AttributeError("IFace2.Name")`。面/边不保证有 Name，strict 快照把它当
    COM 断线 → connected=False、持续重试、丢采集。这里只对**可选成员**放宽；
    必选成员的读取路径不变（失败仍按断线处理）。
    """
    if isinstance(exc, AttributeError):
        return True
    msg = str(exc)
    return (any(m in msg for m in _MEMBER_MISSING_MARKERS)
            or any(m in msg for m in _UNKNOWN_NAME_MARKERS))


def _is_com_dispatch(obj):
    """pywin32 动态 COM 对象（属性值本身），不是普通 Python 方法。

    这类对象可调用（默认成员），但被当函数调会报 "Member not found"——
    这是属性，不是断线。只对它放行；普通方法（GetTitle/GetSuppression2 等）
    调用失败必须按断线处理，否则会吞掉真实 COM 故障、产生假变化事件。
    """
    return hasattr(obj, "_oleobj_") or type(obj).__name__ in (
        "CDispatch", "Dispatch", "PyIDispatch", "PyIDispatchEx")


def _get_com_member(obj, name, *args, default=None, strict=False, optional=False):
    """安全读取 SW 的 COM 成员（参考 solidworks-automation-skill 的写法）。

    SW 的类型库把很多 GetXxx 声明成"属性"，pywin32 取属性时会自动取值；
    有些又是普通方法。这个函数两者通吃。

    strict=False（默认）：任何异常都返回 default，绝不抛（联调脚本/宽松探测用）。
    strict=True：真正的 COM 异常往上抛 ComSnapshotError，
                 让桥知道断线了；快照里的**每个必选成员**读取（含特征树遍历）都用它。
    optional=True：这是可选成员（如选中对象的 Name）。getattr/调用遇到
                 AttributeError / DISP_E_UNKNOWNNAME / DISP_E_MEMBERNOTFOUND 类
                 "没这个成员"→ 返回 default（通常是 None），快照继续有效。
    **必选成员（optional=False）在 strict 下任何 getattr 异常都必须抛
    ComSnapshotError**，包括 DISP_E_MEMBERNOTFOUND（"Member not found"）——
    否则快照会悄悄变空、connected 仍 True，产生假的 documentActivated(cur=None)。
    只有**调用路径**保留旧的"伪可调用属性"语义（把属性值本身交出去）。
    """
    try:
        member = getattr(obj, name)
    except Exception as exc:
        # getattr 路径：只有可选成员允许"没这个成员"豁免；必选成员任何异常
        # （含 Member not found）在 strict 下都算断线，绝不能吞成空快照。
        if optional and _is_optional_member_missing(exc):
            return default
        if strict:
            raise ComSnapshotError(f"读取 {name} 失败: {exc}") from exc
        return default
    try:
        if args:
            return member(*args)
        return member() if callable(member) else member
    except Exception as exc:
        msg = str(exc)
        # "伪可调用属性"：getattr 拿到的是 pywin32 的 COM dispatch 对象，
        # 被当函数调用时报 "Member not found"——这不是断线，直接把属性值
        # 交出去。**只对真正的 COM 对象放行**；普通方法调用失败（即使报
        # Member not found）在 strict 下必须按断线处理，否则会吞掉真实
        # COM 故障并产生假变化事件。
        if _is_member_missing(exc) and _is_com_dispatch(member):
            return member
        if optional and _is_optional_member_missing(exc):
            return default
        if strict:
            raise ComSnapshotError(f"读取 {name} 失败: {exc}") from exc
        return default


# ---------- 纯函数：抓拍比对（可单独测试） ----------

def diff_snapshot(prev, cur):
    """比对两次抓拍，生成事件列表。每项形如
    {"event_type": "...", ...payload...}，不含 event_type 的字段都是 payload。

    注意：断线时**不要**拿空快照来比（会产生假的删除事件）——桥那边遇到
    COM 异常会直接断开重连，重新连上后先抓基准快照再比对。
    """
    prev = prev or {}
    cur = cur or {}
    events = []

    # 1. 活动文档变了（打开/切换/关闭）
    if prev.get("doc") != cur.get("doc"):
        events.append({
            "event_type": "documentActivated",
            "prev": prev.get("doc"),
            "cur": cur.get("doc"),
        })

    # 2. 选中变了（选了哪个面/边/特征——项目文档里说的"模型引用"）
    if prev.get("selection") != cur.get("selection"):
        events.append({
            "event_type": "selectionChanged",
            "prev": prev.get("selection"),
            "cur": cur.get("selection"),
        })

    # 3. 特征树变了：以 (路径, 类型) 为键，比对新增/删除/变更
    def keyed(feats):
        out = {}
        for f in feats:
            k = (f.get("path"), f.get("type"))
            out.setdefault(k, []).append(f)
        return out

    p_map = keyed(prev.get("features") or [])
    c_map = keyed(cur.get("features") or [])
    p_keys, c_keys = set(p_map), set(c_map)

    added = [f for k in (c_keys - p_keys) for f in c_map[k]]
    removed = [f for k in (p_keys - c_keys) for f in p_map[k]]
    changed = []
    for k in p_keys & c_keys:
        p_list, c_list = p_map[k], c_map[k]
        # 同名同类型的特征数量变化（如草图里加了线段、阵列数量变了）
        if len(p_list) != len(c_list) or any(
            p.get("sup") != c.get("sup") for p, c in zip(p_list, c_list)
        ):
            changed.extend(c_list)

    if added:
        events.append({"event_type": "featureAdded", "features": added})
    if removed:
        events.append({"event_type": "featureRemoved", "features": removed})
    if changed:
        events.append({"event_type": "featureChanged", "features": changed})

    return events


# ---------- SolidWorks COM 抓拍 ----------

def snapshot_sw(sw, strict=False):
    """对 SolidWorks 当前状态做一次抓拍。

    返回 {"doc": {...}|None, "selection": [...], "features": [...]}

    strict=False（默认，给联调脚本用）：任何一步失败都只留空/跳过，绝不抛异常。
    strict=True（给桥的轮询用）：**整棵快照（活动文档 / 选中 / 特征树遍历）**
        任何 COM 读取失败都抛 ComSnapshotError。特征遍历中途 GetNextFeature /
        GetNextSubFeature / Name 等读取失败不能再吞成"特征没了"——那会让桥用
        不完整快照差分，产出假的 featureRemoved/featureAdded，而且一直握着
        失效的 COM 对象不重连。
        **"没有活动文档"仍然是正常的 doc=None，不算异常**——区分开这两者，
        桥才能做到"没文档不误报、断线要重连"。
    """
    snap = {"doc": None, "selection": [], "features": []}
    model = _get_com_member(sw, "ActiveDoc", strict=strict)
    if model is None:
        return snap

    # 活动文档
    title = _get_com_member(model, "GetTitle", strict=strict)
    doc_type = _get_com_member(model, "GetType", strict=strict)  # 1=零件 2=装配体 3=工程图
    if title is not None or doc_type is not None:
        snap["doc"] = {"title": title, "type": doc_type}

    # 选中状态（面/边/特征的"模型引用"）
    # 注意：SW 的类型库把 ISelectionManager 标成只写属性，必须用不带 I 前缀的
    # SelectionManager（IExtension 同理，要用 Extension）。
    sel_mgr = _get_com_member(model, "SelectionManager", strict=strict)
    if sel_mgr is not None:
        count = _get_com_member(sel_mgr, "GetSelectedObjectCount2", -1,
                                strict=strict) or 0
        items = []
        for i in range(1, int(count) + 1):
            obj = _get_com_member(sel_mgr, "GetSelectedObject6", i, -1,
                                  strict=strict)
            # 面/边等选中对象不保证有 Name（pywin32 会抛 AttributeError）：
            # 这是正常缺成员 → name=None，快照继续有效，不能当 COM 断线。
            name = (_get_com_member(obj, "Name", strict=strict, optional=True)
                    if obj is not None else None)
            otype = _get_com_member(sel_mgr, "GetSelectedObjectType3", i, -1,
                                    strict=strict)
            items.append({"type": int(otype) if otype is not None else None,
                          "name": name})
        snap["selection"] = items

    # 特征树（BFS 遍历，带上限；用（名字,类型）去重，防 SW 子特征自引用死循环）。
    # strict=True 时这里**不再**吞异常：任何 COM 读取失败都让整次抓拍作废，
    # 由桥断开重连、重建基准——绝不用残缺特征树去差分。
    feats = []
    seen = set()
    feat = _get_com_member(model, "FirstFeature", strict=strict)
    stack = [(feat, "", 0)]
    while stack and len(feats) < MAX_FEATURES:
        f, path, depth = stack.pop()
        if f is None or depth > MAX_DEPTH:
            continue
        name = _get_com_member(f, "Name", strict=strict)
        ftype = _get_com_member(f, "GetTypeName2", strict=strict)
        sup = _get_com_member(f, "GetSuppression2", strict=strict)
        # SW 的 GetFirstSubFeature 在"没有子特征"时会返回自身/父特征，
        # 之前会拍出 凸台-拉伸1/凸台-拉伸1/凸台-拉伸1/... 几十层垃圾路径。
        key = (name, ftype)
        if key in seen:
            continue
        seen.add(key)
        full_path = (path + "/" + name) if (path and name) else (name or path)
        feats.append({"path": full_path, "type": ftype, "sup": sup})
        sub = _get_com_member(f, "GetFirstSubFeature", strict=strict)
        while sub is not None:
            stack.append((sub, full_path, depth + 1))
            sub = _get_com_member(sub, "GetNextSubFeature", strict=strict)
        next_feat = _get_com_member(f, "GetNextFeature", strict=strict)
        stack.append((next_feat, path, depth))
    snap["features"] = feats
    return snap


# ---------- 桥本体 ----------

class SolidWorksBridge:
    """后台线程：连接 SolidWorks → 周期抓拍 → 比对 → 回调事件。

    用法：
        bridge = SolidWorksBridge(cfg, on_event, is_running)
        bridge.start()   # 后台开始（连不上会自己重试）
        bridge.stop()    # 停止
        bridge.status()  # 给界面读状态
    """

    def __init__(self, cfg: dict, on_event, is_running):
        self.cfg = cfg or {}
        # 不限版本的 ProgID 自动发现（SldWorks.Application 优先，再按版本倒序兜底）
        self.prog_ids = cad_discovery.sw_progids(self.cfg.get("prog_id"))
        self.prog_id = self.prog_ids[0]
        self.prog_id_used = None
        self.allow_launch = bool(self.cfg.get("allow_launch", False))
        self.poll_s = float(self.cfg.get("poll_interval_s", 1.0))
        self.on_event = on_event        # (kind, detail) -> None
        self.is_running = is_running    # () -> bool（会话是否还在采）
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = {
            "backend": "solidworks",
            "connected": False,
            "doc": None,
            "doc_type": None,
            "prog_id": self.prog_id,
            "last_error": None,
            "retrying": False,
        }
        self._prev = None        # 上一次抓拍（连上后先抓基准；断开时清空）

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def status(self):
        with self._lock:
            return dict(self._state)

    def _set_state(self, **kw):
        with self._lock:
            self._state.update(kw)

    # ---------- 后台主循环 ----------

    def _run(self):
        import pythoncom  # pywin32：本线程要先用它声明"我要用 COM"
        import win32com.client

        pythoncom.CoInitialize()
        sw = None
        try:
            while not self._stop.is_set():
                was_connected = sw is not None
                sw = self._tick(sw, win32com.client)
                if sw is None:
                    # 连不上 / 刚断开：等 5 秒再重试（不刷屏、不抢 CPU）
                    self._stop.wait(5)
                elif not was_connected:
                    # 刚连上（已抓基准快照）：马上进入正常轮询，不再等
                    continue
                else:
                    self._stop.wait(self.poll_s)
        finally:
            sw = None          # 释放 COM 引用（进程退出/停采时不再握着失效对象）
            pythoncom.CoUninitialize()

    def _tick(self, sw, win32com_client=None):
        """一轮：确保连接 → 抓拍 → 比对 → 回调。返回当前 COM 对象。

        返回 None = 当前未连接（连不上或刚断开），下一轮会重新 GetActiveObject。
        **可直接单测**：注入假 COM 对象 + 覆盖 _connect 就能验证断线重连，
        不需要真的装 SolidWorks。
        """
        # 1) 确保连接
        if sw is None:
            if win32com_client is None:
                import win32com.client as win32com_client
            sw = self._connect(win32com_client)
            if sw is None:
                self._set_state(connected=False, retrying=True)
                return None
            # 先抓拍一次做基准，不算变化
            try:
                baseline = snapshot_sw(sw, strict=True)
            except Exception as exc:
                # 刚连上就读不到（SW 还没就绪）→ 当作没连上，稍后重试
                self._set_state(connected=False, retrying=True,
                                last_error=f"连接后抓拍失败，稍后重试: {exc}",
                                doc=None, doc_type=None)
                return None
            doc = baseline.get("doc") or {}
            self._set_state(connected=True, retrying=False, last_error=None,
                            doc=doc.get("title"), doc_type=doc.get("type"))
            self._prev = baseline
            return sw

        # 2) 已连接：抓拍 → 比对 → 回调
        try:
            cur = snapshot_sw(sw, strict=True)
        except Exception as exc:
            # COM 异常 = SW 被关掉/卡死，不是"文档被删了"。
            # 关键：绝不用空快照去 diff（否则会产出假的文档/特征删除事件）；
            # 直接标记断开、释放 COM 对象，下一轮重新 GetActiveObject。
            self._set_state(connected=False, retrying=True,
                            last_error=f"SolidWorks COM 读取失败，已断开重连: {exc}",
                            doc=None, doc_type=None)
            self._prev = None
            return None
        try:
            for ev in diff_snapshot(self._prev, cur):
                event_type = ev.pop("event_type")
                self.on_event(event_type, ev)
            self._prev = cur
            doc = cur.get("doc") or {}
            self._set_state(doc=doc.get("title"), doc_type=doc.get("type"))
        except Exception as exc:
            # 回调/比对出错不算 SW 断线（别误重连），只记录原因
            self._set_state(last_error=f"事件处理失败: {exc}")
        return sw

    def _connect(self, win32com_client):
        """连接 SolidWorks：先找已经开着的，找不到才按配置决定要不要自己启动。

        依次尝试自动发现出来的 ProgID（不限版本的那个排第一），
        所以同一份程序在 SW2015~SW2025 上都能连上，不需要改配置。
        """
        for pid in self.prog_ids:
            try:
                sw = win32com_client.GetActiveObject(pid)
                self._remember_progid(pid)
                return sw
            except Exception:
                continue
        if not self.allow_launch:
            self._set_state(last_error="SolidWorks 未运行（等它打开后自动连上）")
            return None
        for pid in self.prog_ids:
            try:
                sw = win32com_client.Dispatch(pid)
                # 注意：SW 后台模式（Visible=False）不会向系统注册 COM 接口，
                # 之后 GetActiveObject 永远找不到它。所以自己启动时必须正常显示窗口。
                try:
                    sw.Visible = True
                except Exception:
                    pass
                self._remember_progid(pid)
                return sw
            except Exception as exc:
                last = exc
                continue
        self._set_state(last_error=f"无法启动 SolidWorks: {last}")
        return None

    def _remember_progid(self, pid):
        self.prog_id_used = pid
        with self._lock:
            self._state["prog_id"] = pid
