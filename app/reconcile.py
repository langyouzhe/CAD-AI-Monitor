# -*- coding: utf-8 -*-
"""
reconcile.py —— 三方对账（v2.x 新增，按文档 5.2 最后一条）

一次"操作" = 流2 记下你点了什么 + 流1 报出模型发生了什么变化；
开了视觉解读（AI 眼）后，截图结论也参与对账。**缺票自动标异常**。

异常事件写成 meta/anomaly 落库（source="meta", kind="anomaly"），
detail 里带 type / 相关事件序号 / 原因，方便以后分析"哪里漏了"。

异常类型：
- stream2_no_effect：一个动作（左键/右键/快捷键/字母键/笔势）之后配对窗口内
  没有任何流1 事件 —— 可能只是纯视图操作，也可能流1 真漏了（软提示）；
- stream1_orphan：流1 效果事件（特征增删改/选中变化/文档切换/Creo 菜单/NX 日志）
  前面找不到任何流2 动作配对 —— 可能是外部程序改的，或流2 漏记（软提示）；
- visual_missing：开了 AI 眼（provider 不是 mock）且该动作按配置应触发视觉，
  但没有对应识别结果（时间戳对齐查找失败）—— 视觉缺票；
- visual_failed：AI 眼识别结果 ok=false —— 云端失败/未填 key 等。

规则参数（config.reconcile）：
- window_ms：动作之后多久内的流1 事件算它的"效果"（默认 2000）
- ignore_middle：中键/滚轮不算动作（旋转视图不产生流1 事件，默认 true）
"""
import json

# 流1 中算"效果"的事件类型（特征树/选中/文档/命令类）
# 注意：creo_session / nx_session 是"后端挂上文件"的通知，不是用户操作的效果
STREAM1_EFFECT_KINDS = {
    "featureAdded", "featureRemoved", "featureChanged",
    "selectionChanged", "documentActivated",
    "creo_trail", "creo_command",
    "nx_journal", "nx_syslog",   # nx_syslog 仅兼容 v0.2.x 旧数据
}


def is_action(ev, ignore_middle=True):
    """判断一条 stream2 事件是不是"动作"（按下左/右键、快捷键、字母键、笔势）。

    返回 (是否动作, 动作名)；非动作返回 (False, None)。
    """
    kind, detail = ev["kind"], ev.get("detail") or {}
    if kind == "click":
        btn = detail.get("button")
        if detail.get("action") != "press":
            return (False, None)  # 松开不算动作（一个点击只算一次）
        if btn in ("left", "right"):
            return (True, f"click_{btn}")
        if btn == "middle" and ignore_middle:
            return (False, None)
        return (True, f"click_{btn}")
    if kind == "key_down":
        if detail.get("is_modifier"):
            return (False, None)  # 单独按 Shift/Ctrl/Alt 不算动作
        if detail.get("mods"):
            return (True, "shortcut")
        char = detail.get("char")
        if isinstance(char, str) and char.isalpha():
            return (True, "letter_key")
        return (False, None)  # 数字/符号是输入数值，不算动作
    if kind == "gesture":
        return (True, "gesture")
    return (False, None)


def is_effect(ev):
    """判断一条事件是不是流1 的"效果"。"""
    return ev.get("source") == "stream1" and ev.get("kind") in STREAM1_EFFECT_KINDS


def should_trigger_visual(ev, triggers):
    """按 AI 眼触发规则判断这条流2 动作是否应触发视觉截图。"""
    kind, detail = ev["kind"], ev.get("detail") or {}
    if kind == "click":
        btn = detail.get("button")
        if btn == "left":
            return bool(triggers.get("click_left"))
        if btn == "right":
            return bool(triggers.get("click_right"))
        return False
    if kind == "key_down":
        if detail.get("is_modifier"):
            return False  # 单独按 Shift/Ctrl/Alt 不算操作，不触发截图
        if detail.get("mods"):
            return bool(triggers.get("shortcut"))
        char = detail.get("char")
        if isinstance(char, str) and char.isalpha():
            return bool(triggers.get("bare_letter"))
        return False
    if kind == "gesture":
        return bool(triggers.get("gesture"))
    return False


def pair_events(events, window_ms=2000, ignore_middle=True,
                visual_enabled=False, triggers=None):
    """对账核心（纯函数）：吃一个会话的事件列表，吐出异常列表。

    每个异常 = {"type", "reason", "ref_seq", "ref_kind", "ref_ts"}
    """
    triggers = triggers or {}
    anomalies = []

    # 1) 动作 → 效果：每个动作找窗口内的流1 事件
    effects_ts = [e["ts_ms"] for e in events if is_effect(e)]
    for ev in events:
        if ev.get("source") != "stream2":
            continue
        ok, name = is_action(ev, ignore_middle)
        if not ok:
            continue
        lo, hi = ev["ts_ms"], ev["ts_ms"] + window_ms
        if not any(lo <= t <= hi for t in effects_ts):
            anomalies.append({
                "type": "stream2_no_effect",
                "reason": f"动作 {name} 之后 {window_ms}ms 内没有流1 效果事件（可能是纯视图操作，也可能流1 漏记）",
                "ref_seq": ev["seq"], "ref_kind": ev["kind"], "ref_ts": ev["ts_ms"],
            })

    # 2) 效果 → 动作：每个流1 效果找前面最近的动作
    actions = [e for e in events
               if e.get("source") == "stream2" and is_action(e, ignore_middle)[0]]
    for ev in events:
        if not is_effect(ev):
            continue
        prev = [a for a in actions
                if 0 <= ev["ts_ms"] - a["ts_ms"] <= window_ms]
        if not prev:
            anomalies.append({
                "type": "stream1_orphan",
                "reason": f"流1 事件 {ev['kind']} 前面没有配对的流2 动作（可能外部程序修改，或流2 漏记）",
                "ref_seq": ev["seq"], "ref_kind": ev["kind"], "ref_ts": ev["ts_ms"],
            })

    # 3) 视觉对账：开了 AI 眼时，该触发的动作必须有识别结果
    if visual_enabled:
        ai_eye_events = [e for e in events if e.get("kind") == "ai_eye"]
        by_ts = {}
        for e in ai_eye_events:
            by_ts.setdefault(e["ts_ms"], []).append(e)
        for ev in events:
            if ev.get("source") != "stream2":
                continue
            ok, name = is_action(ev, ignore_middle)
            if not ok or not should_trigger_visual(ev, triggers):
                continue
            matches = by_ts.get(ev["ts_ms"], [])
            if not matches:
                anomalies.append({
                    "type": "visual_missing",
                    "reason": f"动作 {name} 按配置应触发视觉截图，但没有对应识别结果（缺票）",
                    "ref_seq": ev["seq"], "ref_kind": ev["kind"], "ref_ts": ev["ts_ms"],
                })
            elif any((m.get("detail") or {}).get("result", {}).get("ok") is False
                     for m in matches):
                anomalies.append({
                    "type": "visual_failed",
                    "reason": f"动作 {name} 的视觉识别失败（云端错误/未填 key）",
                    "ref_seq": ev["seq"], "ref_kind": ev["kind"], "ref_ts": ev["ts_ms"],
                })
    return anomalies


def stream1_available(session):
    """本次会话的流1 有没有活过来（任意后端 connected）。

    v0.2.x 的对账是"每个动作往后找一条流1 效果"，一旦流1 没数据源，
    所有动作都会被判成 stream2_no_effect（2026-10-23 实测 158 条全误报）。
    所以对账前先问一句：尺子（流1）在不在。
    """
    try:
        statuses = session.stats().get("stream1_status") or []
    except Exception:
        return False
    return any(bool(st.get("connected")) for st in statuses)


def run_on_session(session, cfg):
    """对一次采集会话跑对账，把异常写成 meta/anomaly 事件落库。

    返回异常条数。在 stop() 里、写 session_stop 之前调用。

    流1 不可用时**跳过对账**，只写一条 meta/stream1_unavailable 汇总——
    否则会产出"所有动作都缺效果"的假异常（问题清单 P1-3）。
    """
    events = session.store.query_session(session.session_id)
    rc = cfg.get("reconcile", {})
    ai_cfg = cfg.get("ai_eye", {})
    if not stream1_available(session):
        session._record("meta", "stream1_unavailable", {
            "reason": "本次会话流1 无数据源（后端未连接），已跳过对账，避免误报异常",
            "hint": "检查采集器界面『流1』：NX 需先录制 journal 或配置 nx.journal_path；"
                    "SW/Creo 需先打开对应软件",
        })
        return 0
    visual_enabled = bool(ai_cfg.get("enabled")) and ai_cfg.get("provider") != "mock"
    anomalies = pair_events(
        events,
        window_ms=int(rc.get("window_ms", 2000)),
        ignore_middle=bool(rc.get("ignore_middle", True)),
        visual_enabled=visual_enabled,
        triggers=ai_cfg.get("triggers", {}),
    )
    for a in anomalies:
        session._record("meta", "anomaly", a)
    return len(anomalies)
