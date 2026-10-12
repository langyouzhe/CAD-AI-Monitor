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
- 比对逻辑（diff_snapshot）是纯函数，不依赖 SW，可以单独做单元测试。
"""
import threading
import time

import cad_discovery

# 特征树遍历的安全上限（防止超大树把比对拖慢）
MAX_FEATURES = 1500
MAX_DEPTH = 6


def _get_com_member(obj, name, *args, default=None):
    """安全读取 SW 的 COM 成员（参考 solidworks-automation-skill 的写法）。

    SW 的类型库把很多 GetXxx 声明成"属性"，pywin32 取属性时会自动取值；
    有些又是普通方法。这个函数两者通吃，而且绝不抛异常（桥不能因为
    一个成员读不到就崩）。
    """
    try:
        member = getattr(obj, name)
    except Exception:
        return default
    try:
        if args:
            return member(*args)
        return member() if callable(member) else member
    except Exception as exc:
        msg = str(exc)
        # "伪可调用属性"（例如返回 COM 对象的属性）被当函数调会报找不到成员，
        # 这种情况下直接把属性值本身交出去。
        if "-2147352573" in msg or "Member not found" in msg or "找不到成员" in msg:
            return member
        return default


# ---------- 纯函数：抓拍比对（可单独测试） ----------

def diff_snapshot(prev, cur):
    """比对两次抓拍，生成事件列表。每项形如
    {"event_type": "...", ...payload...}，不含 event_type 的字段都是 payload。
    """
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

def snapshot_sw(sw):
    """对 SolidWorks 当前状态做一次抓拍。

    返回 {"doc": {...}|None, "selection": [...], "features": [...]}
    任何一步失败都只留空/跳过，绝不抛异常。
    """
    snap = {"doc": None, "selection": [], "features": []}
    model = _get_com_member(sw, "ActiveDoc")
    if model is None:
        return snap

    # 活动文档
    title = _get_com_member(model, "GetTitle")
    doc_type = _get_com_member(model, "GetType")  # 1=零件 2=装配体 3=工程图
    if title is not None or doc_type is not None:
        snap["doc"] = {"title": title, "type": doc_type}

    # 选中状态（面/边/特征的"模型引用"）
    # 注意：SW 的类型库把 ISelectionManager 标成只写属性，必须用不带 I 前缀的
    # SelectionManager（IExtension 同理，要用 Extension）。
    sel_mgr = _get_com_member(model, "SelectionManager")
    if sel_mgr is not None:
        try:
            count = _get_com_member(sel_mgr, "GetSelectedObjectCount2", -1) or 0
            items = []
            for i in range(1, int(count) + 1):
                obj = _get_com_member(sel_mgr, "GetSelectedObject6", i, -1)
                name = _get_com_member(obj, "Name") if obj is not None else None
                otype = _get_com_member(sel_mgr, "GetSelectedObjectType3", i, -1)
                items.append({"type": int(otype) if otype is not None else None,
                              "name": name})
            snap["selection"] = items
        except Exception:
            pass

    # 特征树（BFS 遍历，带上限；用（名字,类型）去重，防 SW 子特征自引用死循环）
    feats = []
    seen = set()
    try:
        feat = _get_com_member(model, "FirstFeature")
        stack = [(feat, "", 0)]
        while stack and len(feats) < MAX_FEATURES:
            f, path, depth = stack.pop()
            if f is None or depth > MAX_DEPTH:
                continue
            name = _get_com_member(f, "Name")
            ftype = _get_com_member(f, "GetTypeName2")
            sup = _get_com_member(f, "GetSuppression2")
            # SW 的 GetFirstSubFeature 在"没有子特征"时会返回自身/父特征，
            # 之前会拍出 凸台-拉伸1/凸台-拉伸1/凸台-拉伸1/... 几十层垃圾路径。
            key = (name, ftype)
            if key in seen:
                continue
            seen.add(key)
            full_path = (path + "/" + name) if (path and name) else (name or path)
            feats.append({"path": full_path, "type": ftype, "sup": sup})
            try:
                sub = _get_com_member(f, "GetFirstSubFeature")
                while sub is not None:
                    stack.append((sub, full_path, depth + 1))
                    sub = _get_com_member(sub, "GetNextSubFeature")
            except Exception:
                pass
            try:
                feat = _get_com_member(f, "GetNextFeature")
                stack.append((feat, path, depth))
            except Exception:
                pass
    except Exception:
        pass
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
                # 1) 确保连接
                if sw is None:
                    sw = self._connect(win32com.client)
                    if sw is None:
                        self._set_state(connected=False, retrying=True)
                        self._stop.wait(5)  # 连不上就等 5 秒再试
                        continue
                    self._set_state(connected=True, retrying=False, last_error=None)
                    doc = _get_com_member(sw, "ActiveDoc")
                    title = _get_com_member(doc, "GetTitle") if doc is not None else None
                    self._set_state(doc=title)
                    # 先抓拍一次做基准，不算变化
                    self._prev = snapshot_sw(sw)
                    continue

                # 2) 已连接：抓拍 → 比对 → 回调
                try:
                    cur = snapshot_sw(sw)
                    for ev in diff_snapshot(self._prev, cur):
                        event_type = ev.pop("event_type")
                        self.on_event(event_type, ev)
                    self._prev = cur
                    doc = cur.get("doc") or {}
                    self._set_state(doc=doc.get("title"), doc_type=doc.get("type"))
                except Exception as exc:
                    # SW 被关掉或卡死 → 断开，走重连
                    self._set_state(connected=False, last_error=str(exc), doc=None, doc_type=None)
                    sw = None
                self._stop.wait(self.poll_s)
        finally:
            if sw is not None:
                try:
                    sw = None
                except Exception:
                    pass
            pythoncom.CoUninitialize()

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
