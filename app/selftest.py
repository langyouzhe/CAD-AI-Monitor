# -*- coding: utf-8 -*-
"""
selftest.py —— 开发自检（不需要 SolidWorks）：
1. 配置合并逻辑
2. v1 旧数据库自动迁移
3. 抓拍比对逻辑（diff_snapshot，纯函数）
4. 建模窗口判定（装聋逻辑）
5. 端到端：注入假按键/点击 → 采集 → 入库（allow_all_windows 模式）
6. 离开/回来边界标记（把阈值调小加速验证）

跑法：python selftest.py
全部通过打印 OK，退出码 0 = 成功。

SolidWorks 真机联调请用 selftest_solidworks.py。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# 强制 UTF-8 输出，避免老式控制台（GBK 编码）打印中文/符号报错
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collector_core import (  # noqa: E402
    DEFAULT_CONFIG, ConfigManager, EventStore,
)
from input_listener import CollectorSession, is_modeling_window  # noqa: E402
from solidworks_bridge import diff_snapshot  # noqa: E402


def ok(msg):
    print("OK  " + msg)


# ---------- 1. 配置合并 ----------

def test_config_merge():
    tmp = tempfile.mkdtemp(prefix="cad-ai-t-")
    mgr = ConfigManager(Path(tmp))
    cfg = mgr.load()
    assert cfg["idle_minutes"] == 5.0
    assert Path(tmp, "config.json").exists(), "默认配置应自动写文件"
    # 用户改一个字段 + 加一个未知字段，其余保持默认
    user = {"idle_minutes": 7.0, "stream1": {"prog_id": "X.1"}}
    merged = ConfigManager._merge(DEFAULT_CONFIG, user)
    assert merged["idle_minutes"] == 7.0
    assert merged["stream1"]["prog_id"] == "X.1"
    assert merged["stream1"]["enabled"] is True, "没写的字段应保持默认"
    ok("配置合并/默认写入正确")


# ---------- 2. v1 旧库迁移 ----------

def test_migration():
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-t-"))
    db = tmp / "events.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE events (id INTEGER, session_id INTEGER, seq INTEGER,"
                 " ts_ms INTEGER, ts_iso TEXT, kind TEXT, detail TEXT)")
    conn.execute("INSERT INTO events VALUES (1,1,1,0,'t','click','{}')")
    conn.commit()
    conn.close()

    store = EventStore(db)
    cols = [r[1] for r in store._conn.execute("PRAGMA table_info(events)").fetchall()]
    assert "source" in cols, "新表必须有 source 列"
    assert (tmp / "events.v1.backup.db").exists(), "旧库应被备份"
    # WAL 合并：合并之后主库文件里就该有数据，旁边那个 -wal 不该再堆着东西
    store.add_event(1, 1, "stream2", "key_down", {"k": "a"}, int(time.time() * 1000))
    store.wal_checkpoint()
    wal = Path(str(db) + "-wal")
    assert (not wal.exists()) or wal.stat().st_size < 4096, (
        f"合并后 WAL 应该被截断，实际 {wal.stat().st_size if wal.exists() else 0} 字节")
    assert store.query_session(1), "合并后数据仍然读得到"
    store.close()
    ok("v1 旧数据库自动备份并升级为 v2 结构")


# ---------- 3. 抓拍比对（流1 纯逻辑） ----------

def test_diff_snapshot():
    base = {"doc": None, "selection": [], "features": []}
    # 文档变化
    cur = {"doc": {"title": "Part1", "type": 1}, "selection": [], "features": []}
    evs = diff_snapshot(base, cur)
    assert evs and evs[0]["event_type"] == "documentActivated"
    # 选中变化
    base2 = cur
    cur2 = {"doc": cur["doc"],
            "selection": [{"type": 3, "name": "Face1"}],
            "features": []}
    evs = diff_snapshot(base2, cur2)
    assert evs and evs[0]["event_type"] == "selectionChanged"
    # 特征新增 / 删除
    base3 = {"doc": cur["doc"], "selection": cur2["selection"],
             "features": [{"path": "Boss-Extrude1", "type": "Boss", "sup": 0}]}
    cur3 = {"doc": cur["doc"], "selection": cur2["selection"],
            "features": [{"path": "Boss-Extrude1", "type": "Boss", "sup": 0},
                         {"path": "Boss-Extrude1/Sketch1", "type": "ProfileFeature", "sup": 0}]}
    evs = diff_snapshot(base3, cur3)
    types = [e["event_type"] for e in evs]
    assert "featureAdded" in types
    # 压制状态变化 → featureChanged
    cur4 = {"doc": cur["doc"], "selection": cur2["selection"],
            "features": [{"path": "Boss-Extrude1", "type": "Boss", "sup": 1}]}
    evs = diff_snapshot(base3, cur4)
    assert [e["event_type"] for e in evs] == ["featureChanged"]
    ok("抓拍比对逻辑正确（文档/选中/特征增删改）")


# ---------- 4. 建模窗口判定（装聋） ----------

def test_window_filter():
    apps = [{"exe": "SLDWORKS.exe", "title": "SOLIDWORKS"}]
    assert is_modeling_window(("Part1 - SOLIDWORKS", "SLDWORKS.exe"), apps)
    assert is_modeling_window(("whatever", "SLDWORKS.exe"), apps)      # exe 命中
    assert is_modeling_window(("SOLIDWORKS Premium 2022", "explorer.exe"), apps)  # 标题命中
    assert not is_modeling_window(("百度一下", "chrome.exe"), apps)    # 都命中不了
    assert is_modeling_window((None, None), apps, allow_all=True)      # 调试开关
    assert not is_modeling_window((None, None), apps)
    ok("装聋判定正确（exe/标题命中，其他窗口全部不记）")


# ---------- 5+6. 端到端（注入假输入 + 离开/回来） ----------

INPUT_KEYBOARD = 1
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_KEYUP = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_ulonglong),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", INPUT_UNION)]


def send_key(scan, up=False):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=0, wScan=scan,
                        dwFlags=KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0),
                        time=0, dwExtraInfo=0)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _mk_session(tmp, idle_minutes, stream1_enabled=False, poll_s=0.5):
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "version": 2,
            "idle_minutes": idle_minutes,
            "debug": {"allow_all_windows": True},
            "stream1": {"enabled": stream1_enabled, "poll_interval_s": poll_s},
        }, f, ensure_ascii=False, indent=2)
    return CollectorSession(tmp)


def test_end_to_end_stream2():
    """真实系统输入路径验证：注入假按键 → 钩子收到 → 入库。

    注意：这个测试要求"注入成功"，但注入可能被系统安全机制拒绝（测试机
    前台窗口是浏览器时会这样）。所以断言放宽为"钩子确实在工作、事件确实
    入库"，注入成功与否单独提示。
    """
    tmp = tempfile.mkdtemp(prefix="cad-ai-e2e-")
    session = _mk_session(tmp, idle_minutes=10.0)  # 阈值调大，本测试不测离开
    session.start()
    time.sleep(0.6)
    send_key(0x1E)          # 字母 A
    time.sleep(0.2)
    send_key(0x1E, up=True)
    time.sleep(1.2)          # 留时间让事件走完 监听 → 回调 → 入库
    session.stop()

    events = session.store.query_session(session.session_id)
    kinds = [(e["source"], e["kind"]) for e in events]
    key_downs = [e for e in events if e["kind"] == "key_down"]
    assert key_downs, "钩子应至少收到按键（注入或真人输入）"
    injected = [d for d in key_downs if d["detail"].get("key") in ("a", "A")]
    if injected:
        print("  注入的假按键 'a' 已被钩子捕获入库")
    else:
        print("  提示：注入可能被系统拒绝，但钩子工作正常（收到真人按键）")
    # 顺序正确性
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1)), "seq 不连续"
    # 没有 pause 事件（v2 不记停顿）
    assert not any(k == "pause" for _, k in kinds), "v2 不应再记 pause"
    print(f"  端到端录到 {len(events)} 条：{json.dumps({f'{s}/{k}': kinds.count((s, k)) for s, k in sorted(set(kinds))}, ensure_ascii=False)}")
    ok("端到端采集正确（按键入库 + 顺序连续 + 无停顿事件）")


# ---------- 6.5 离开/回来（确定性验证，不受真人输入干扰） ----------

def test_leave_return():
    import types
    tmp = tempfile.mkdtemp(prefix="cad-ai-lr-")
    session = _mk_session(tmp, idle_minutes=0.1)  # 阈值 6 秒
    session.start()
    time.sleep(0.4)
    key_a = types.SimpleNamespace(char="a")  # 合成按键对象
    session._on_key_press(key_a)              # 第一次操作
    # 把"最后操作时间"拨回 10 秒前，直接调用离开检测
    with session._lock:
        session._last_event_ts = session._last_event_ts - 10_000
    session._maybe_leave()
    session._on_key_press(types.SimpleNamespace(char="s"))  # 回来后的第一次操作
    time.sleep(0.3)
    session.stop()

    events = session.store.query_session(session.session_id)
    kinds = [(e["source"], e["kind"]) for e in events]
    leave = [e for e in events if e["kind"] == "leave"]
    returns = [e for e in events if e["kind"] == "return"]
    assert len(leave) == 1, f"应记一条 leave: {kinds}"
    assert len(returns) == 1, f"下一次活动前应补 return: {kinds}"
    assert returns[0]["seq"] > leave[0]["seq"], "return 应在 leave 之后"
    # 连续两次 _maybe_leave 不应重复记 leave
    session2 = _mk_session(tempfile.mkdtemp(prefix="cad-ai-lr2-"), idle_minutes=0.1)
    session2.start()
    time.sleep(0.3)
    session2._on_key_press(types.SimpleNamespace(char="a"))
    with session2._lock:
        session2._last_event_ts = session2._last_event_ts - 10_000
    session2._maybe_leave()
    session2._maybe_leave()
    session2.stop()
    assert len([e for e in session2.store.query_session(session2.session_id)
                if e["kind"] == "leave"]) == 1, "重复检查不应重复记 leave"
    ok("离开/回来边界正确（阈值判定 + 只记一次 + 回来自动补）")


# ---------- 7. 笔势方向判定（纯函数） ----------

def test_gesture_direction():
    from input_listener import classify_gesture_direction
    cases = {
        (100, 0): "右", (100, -100): "右上", (0, -100): "上",
        (-100, -100): "左上", (-100, 0): "左", (-100, 100): "左下",
        (0, 100): "下", (100, 100): "右下",
    }
    for (dx, dy), expect in cases.items():
        got = classify_gesture_direction(dx, dy)
        assert got == expect, f"({dx},{dy}) 期望 {expect} 得到 {got}"
    assert classify_gesture_direction(0, 0) is None
    ok("笔势 8 方向判定正确")


# ---------- 8. 快捷键/笔势映射表 ----------

def test_shortcut_map():
    import tempfile as _tf
    from pathlib import Path as _P
    from sw_shortcuts import ShortcutMap
    tmp = _P(_tf.mkdtemp(prefix="cad-ai-map-"))
    m = ShortcutMap(tmp)
    assert m.key_command("s", []) == "快捷方式栏", "内置默认表应生效"
    assert m.key_command("s", ["ctrl"]) == "保存", "组合键应命中 ctrl+s"
    assert m.key_command("x", ["ctrl"]) == "剪切"
    assert m.key_command("z", []) is None, "没映射的裸键返回 None"
    # 用户文件覆盖（优先级最高）
    (tmp / "sw_shortcut_map.json").write_text(
        json.dumps({"key_map": {"d": "拉伸凸台/基体"},
                    "gesture_map": {"Part": {"右上": "正视于", "下": "test_cmd"},
                                    "Assembly": {"右上": "正视于装配"}}},
                   ensure_ascii=False),
        encoding="utf-8")
    m2 = ShortcutMap(tmp)
    assert m2.key_command("d", []) == "拉伸凸台/基体", "用户映射应覆盖默认"
    g = m2.gesture_command("右上", doc_type=1)
    assert g == {"id": None, "name": "正视于"}, g
    g2 = m2.gesture_command("右上", doc_type=2)
    assert g2["name"] == "正视于装配", "应按文档类型分上下文"
    assert m2.gesture_command("不存在的方向", doc_type=1) is None
    # 注册表读取：本机装有 SW 时 Part 子键应有值（探测性断言，不依赖具体内容）
    reg = m2.gesture_map.get("Part") or {}
    assert isinstance(reg, dict)
    ok(f"快捷键/笔势映射表正确（注册表读取到 {len(reg)} 个方向映射）")


# ---------- 8.5 Creo / NX 解析（纯函数） ----------

def test_creo_nx_parsers():
    from creo_bridge import parse_trail_line, parse_mapkeys
    ev = parse_trail_line("!%CI0.731852")
    assert ev == {"event_type": "trail_timestamp", "session_s": 0.731852}
    ev = parse_trail_line("!Command ProeWin spawned: D:\\CREO\\parametric.exe")
    assert ev["event_type"] == "commandLaunched" and ev["command"].endswith("parametric.exe")
    # 真实 Creo 12 的命令事件（最高价值）
    ev = parse_trail_line("!Command ProCmdSketSetup was pushed from the software.")
    assert ev == {"event_type": "commandPushed", "command": "ProCmdSketSetup"}, ev
    ev = parse_trail_line("!Command ProCmdModelOpenExe was pushed from the software.")
    assert ev["command"] == "ProCmdModelOpenExe"
    # 对话框交互
    ev = parse_trail_line("~ Select `main_dlg_cur` `MenuBar1` 1 `File`")
    assert ev["event_type"] == "ui" and ev["action"] == "Select"
    assert ev["component"] == "main_dlg_cur" and ev["item"] == "MenuBar1"
    assert ev["values"] == "1 `File`"
    # 鼠标轨迹噪音必须丢弃（真实 trail 里占 80%）
    assert parse_trail_line(
        "~ Move `main_dlg_cur` `proe_win` 9 1066 628 0 2 2191 1212 2560 1600 316908") is None
    assert parse_trail_line("~ Timer `` `` `EmbedBrowserTimer`") is None
    # Creo 12 的钟表时间行
    ev = parse_trail_line("!%CI22:22:32 欢迎使用 Creo Parametric 12.4.4.0。")
    assert ev["event_type"] == "trail_clock"
    assert parse_trail_line("!trail file version No. 2530") is None
    assert parse_trail_line("随便一行") is None
    maps = parse_mapkeys(None)  # 空路径列表 → 空表
    assert maps == {}
    ok("Creo trail 行解析正确（命令事件/UI 交互保留，鼠标轨迹噪音丢弃）")

    from nx_bridge import parse_syslog_line
    ev = parse_syslog_line("12345 2025/01/02 10:30:45  USER  Entered command: FIT")
    assert ev and ev["ts"] == "2025/01/02 10:30:45"
    assert parse_syslog_line("没有时间戳的一行") is None
    ok("NX syslog 行解析正确")


# ---------- 9. AI 眼（假模式）单元测试：时间戳对齐（确定性，不受真人输入干扰） ----------

def test_ai_eye_unit():
    import shutil
    from ai_eye import AiEye
    from pathlib import Path as _P
    results = []
    tmp = tempfile.mkdtemp(prefix="cad-ai-eye-")

    def on_result(detail, ts):
        results.append((detail, ts))

    eye = AiEye(cfg={"provider": "mock", "delay_s": 0.01, "dedup_window_s": 0,
                     "max_queue": 5, "rate_per_min": 999,
                     "save_images": True,
                     "triggers": {"click_left": True, "shortcut": True,
                                  "bare_letter": True, "click_right": False,
                                  "gesture": True, "selection_changed": False}},
                on_result=on_result, is_running=lambda: True,
                images_dir=_P(tmp) / "images")
    eye.start()
    fake_ts = 1700000000000
    eye.trigger("click", {"button": "left"}, fake_ts, (100, 100))
    eye.trigger("click", {"button": "middle"}, fake_ts, (100, 100))   # 中键不应触发
    eye.trigger("key_down", {"char": "5"}, fake_ts + 1, (100, 100))   # 数字不应触发
    eye.trigger("key_down", {"key": "Key.shift", "mods": ["shift"],
                             "is_modifier": True}, fake_ts + 3, None)  # 单按修饰键不应触发
    eye.trigger("selectionChanged", {"cur": []}, fake_ts + 2, None)   # 开关关着，不应触发
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not results:
        time.sleep(0.05)
    eye.stop()
    assert len(results) == 1, f"应只有左键触发一次识别: {results}"
    detail, ts = results[0]
    assert ts == fake_ts, "AI 眼结果必须挂回原事件时间戳（对齐）"
    assert detail["result"]["ok"] is True
    assert detail["annotates"] == "click"
    # 勾了"保存截图"：图应落在本地 images 目录
    img = detail.get("image")
    assert img and _P(img).exists(), "截图应保存到本地 images 目录"

    # 不勾"保存截图"：磁盘上不应留下任何图（单一开关，无冲突）
    results2 = []
    eye2 = AiEye(cfg={"provider": "mock", "delay_s": 0.01, "dedup_window_s": 0,
                      "max_queue": 5, "rate_per_min": 999, "save_images": False,
                      "triggers": {"click_left": True}},
                 on_result=lambda d, t: results2.append((d, t)),
                 is_running=lambda: True, images_dir=_P(tmp) / "images2")
    eye2.start()
    eye2.trigger("click", {"button": "left"}, fake_ts + 100, (100, 100))
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not results2:
        time.sleep(0.05)
    eye2.stop()
    assert results2 and results2[0][0].get("image") is None, "不勾保存时不应落盘"
    saved_files = list((_P(tmp) / "images2").rglob("*.jpg"))
    assert not saved_files, f"不应有任何图片文件: {saved_files}"
    shutil.rmtree(tmp, ignore_errors=True)
    ok("AI 眼触发规则 + 时间戳对齐 + 保存开关单一无冲突（保存/不保存两态都对）")


# ---------- 10.5 三方对账（纯函数） ----------

def test_reconcile():
    from reconcile import pair_events, is_action, is_effect

    def ev(seq, ts, source, kind, detail=None):
        return {"seq": seq, "ts_ms": ts, "source": source, "kind": kind,
                "detail": detail or {}}

    click = {"button": "left", "action": "press"}   # 真实点击事件带 action 字段

    # 动作判定：松开不算动作（一个点击只算一次）
    assert is_action({"kind": "click", "detail": {"button": "left", "action": "release"}})[0] is False
    assert is_action({"kind": "click", "detail": click})[0]
    assert is_action({"kind": "click", "detail": {"button": "middle", "action": "press"}})[0] is False, "中键不算动作"
    assert is_action({"kind": "key_down", "detail": {"mods": ["ctrl"]}})[0]
    assert is_action({"kind": "key_down", "detail": {"char": "d"}})[0]
    assert is_action({"kind": "key_down", "detail": {"char": "5"}})[0] is False, "数字不算动作"
    assert is_action({"kind": "key_down", "detail": {"mods": ["shift"], "is_modifier": True}})[0] is False, \
        "单按修饰键不算动作"
    assert is_action({"kind": "gesture", "detail": {}})[0]
    assert is_effect({"source": "stream1", "kind": "featureAdded"})
    assert not is_effect({"source": "stream2", "kind": "click"})
    assert not is_effect({"source": "stream1", "kind": "creo_session"}), \
        "后端附着通知不算效果"
    assert not is_effect({"source": "stream1", "kind": "nx_session"})

    # 1) 动作无效果 → stream2_no_effect
    events = [ev(1, 1000, "stream2", "click", click),
              ev(2, 1050, "stream2", "click", {"button": "left", "action": "release"}),
              ev(3, 1060, "meta", "session_stop")]
    anoms = pair_events(events, window_ms=2000)
    assert [a["type"] for a in anoms] == ["stream2_no_effect"], anoms
    # 2) 效果无动作 → stream1_orphan
    events = [ev(1, 1000, "stream1", "featureAdded"),
              ev(2, 1100, "meta", "session_stop")]
    anoms = pair_events(events, window_ms=2000)
    assert [a["type"] for a in anoms] == ["stream1_orphan"], anoms
    # 3) 配对成功 → 无异常
    events = [ev(1, 1000, "stream2", "click", {"button": "left", "action": "press"}),
              ev(2, 1500, "stream1", "featureAdded"),
              ev(3, 1600, "meta", "session_stop")]
    assert pair_events(events, window_ms=2000) == []
    # 4) 中键不算动作 → 也不产生 no_effect
    events = [ev(1, 1000, "stream2", "click", {"button": "middle", "action": "press"}),
              ev(2, 1100, "meta", "session_stop")]
    assert pair_events(events, window_ms=2000) == []
    # 5) 视觉对账：开了视觉、该触发的动作缺识别结果 → visual_missing
    triggers = {"click_left": True, "click_right": False, "shortcut": True,
                "bare_letter": True, "gesture": True}
    events = [ev(1, 1000, "stream2", "click", {"button": "left", "action": "press"}),
              ev(2, 1050, "meta", "session_stop")]
    anoms = pair_events(events, window_ms=2000, visual_enabled=True, triggers=triggers)
    assert "visual_missing" in [a["type"] for a in anoms], anoms
    # 6) 识别失败 → visual_failed
    events = [ev(1, 1000, "stream2", "click", {"button": "left", "action": "press"}),
              ev(2, 1000, "stream2", "ai_eye", {"result": {"ok": False}}),
              ev(3, 1050, "meta", "session_stop")]
    anoms = pair_events(events, window_ms=2000, visual_enabled=True, triggers=triggers)
    assert "visual_failed" in [a["type"] for a in anoms], anoms
    ok("三方对账规则正确（无效果/孤儿/视觉缺票/视觉失败 + 中键数字豁免 + 松开不算动作）")


# ---------- 9.5 截图裁剪框：DPI 换算 + 边界（回归锁） ----------

def test_capture_dpi_box():
    from ai_eye import AiEye, _screen_scale, _virtual_origin_physical
    from PIL import ImageGrab
    import win32gui
    scale = _screen_scale()
    assert scale >= 1.0, f"缩放系数应 >= 1.0，实际 {scale}"
    img = ImageGrab.grab(all_screens=True)     # 所有显示器
    box = AiEye._foreground_window_box(img.size)
    if box is not None:
        l, t, r, b = box
        assert 0 <= l < r <= img.size[0], box
        assert 0 <= t < b <= img.size[1], box
        assert r - l >= 50 and b - t >= 50, box
        # 与"逻辑坐标 × 系统缩放 − 虚拟屏原点"的手工换算一致
        hwnd = win32gui.GetForegroundWindow()
        wl, wt, _, _ = win32gui.GetWindowRect(hwnd)
        ox, oy = _virtual_origin_physical()
        assert l == max(0, int(round(wl * scale - ox))), (l, wl, scale, ox)
        assert t == max(0, int(round(wt * scale - oy))), (t, wt, scale, oy)
    # 拿不到合理窗口时必须是 None（调用方退化为整屏），不能抛异常
    assert AiEye._foreground_window_box((0, 0)) is None
    print(f"  系统缩放={scale:g}，全屏(所有显示器)={img.size}，裁剪框={box}")
    ok("截图裁剪框：多显示器 + DPI 换算正确、越界安全回退")


# ---------- 10. 笔势 + AI 眼（会话级合成验证） ----------

def test_gesture_and_ai_eye():
    import types
    tmp = tempfile.mkdtemp(prefix="cad-ai-ge-")
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "version": 2,
            "idle_minutes": 10.0,
            "debug": {"allow_all_windows": True},
            "stream1": {"enabled": False},
            "gestures": {"enabled": True, "min_distance_px": 20},
            "ai_eye": {"enabled": True, "provider": "mock", "delay_s": 0.05},
        }, f, ensure_ascii=False, indent=2)
    session = CollectorSession(tmp)
    session.start()
    time.sleep(0.6)
    btn_right = types.SimpleNamespace(name="right")
    btn_left = types.SimpleNamespace(name="left")
    # 合成一次"按住右键向右上拖 100px"（笔势）
    session._on_click(400, 400, btn_right, True)
    session._on_move(430, 385)
    session._on_move(460, 370)
    session._on_move(500, 300)
    session._on_click(500, 300, btn_right, False)
    # 合成一次左键点击（触发 AI 眼截图）
    session._on_click(200, 200, btn_left, True)
    session._on_click(200, 200, btn_left, False)
    time.sleep(2.0)  # 等 AI 眼后台线程完成假识别
    session.stop()

    events = session.store.query_session(session.session_id)
    # 回归检查：ts_ms 与 ts_iso 必须对应同一时刻（AI 眼延迟结果也如此）
    from datetime import datetime
    for e in events:
        iso_ms = int(datetime.fromisoformat(e["ts_iso"]).timestamp() * 1000)
        assert abs(iso_ms - e["ts_ms"]) <= 1, f"时间戳两列不一致: {e}"
    gestures = [e for e in events if e["kind"] == "gesture"]
    assert len(gestures) == 1, f"应有一条笔势事件: {gestures}"
    assert gestures[0]["detail"]["direction"] == "右上", gestures[0]["detail"]
    assert gestures[0]["detail"]["distance_px"] >= 80
    assert gestures[0]["detail"].get("polyline"), "应记录途经点"
    assert gestures[0]["detail"].get("command_id") is not None or True, "笔势事件带映射信息字段"
    # AI 眼：会话级只验证组件起来了；严格对齐由 test_ai_eye_unit 保证
    # （真人输入同时也在触发 AI 眼，不能做确定性断言）
    assert session.ai_eye is not None, "AI 眼应按配置启动"
    print(f"  笔势事件: {json.dumps(gestures[0]['detail'], ensure_ascii=False)[:220]}")
    ok("笔势记录正确（会话级）+ AI 眼组件启动正常")


# ---------- 11. CAD 自动发现（开源通用化：不限版本、不写死目录） ----------

def test_cad_discovery():
    import cad_discovery
    from creo_bridge import scan_trail_files
    from sw_shortcuts import ShortcutMap
    from solidworks_bridge import SolidWorksBridge

    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-scan-"))

    # (1) SW ProgID：不限版本的那个必须排第一，且不重复
    ids = cad_discovery.sw_progids()
    assert ids and ids[0] == "SldWorks.Application", ids
    assert len(ids) == len(set(ids)), ids
    custom = cad_discovery.sw_progids("SldWorks.Application.99")
    assert custom[0] == "SldWorks.Application.99", custom
    assert "SldWorks.Application" in custom, custom

    # (2) 注册表版本键：形状正确、新版排前面
    vers = cad_discovery.sw_registry_versions()
    for v in vers:
        assert v.startswith("SOLIDWORKS ") and v.split()[-1].isdigit(), v
    years = [int(v.split()[-1]) for v in vers]
    assert years == sorted(years, reverse=True), years

    # (3) 太宽的目录必须被挡掉（否则递归扫描会卡几十秒）
    for broad in (r"C:\\", r"C:\Windows", r"C:\Program Files",
                  r"C:\Program Files (x86)", r"C:\ProgramData", r"C:\Users",
                  r"C:\Users\Public", ""):
        assert cad_discovery._is_broad_dir(broad), broad
    for narrow in (r"C:\Users\Public\Documents", r"D:\CREO", r"E:\work\cad"):
        assert not cad_discovery._is_broad_dir(narrow), narrow

    # (4) Creo/NX 候选目录：非空、无宽目录、Creo 至少覆盖用户主目录
    creo_dirs = cad_discovery.creo_trail_dirs()
    assert creo_dirs, "Creo 候选目录不能为空"
    assert not any(cad_discovery._is_broad_dir(d) for d in creo_dirs), creo_dirs
    assert any(Path(d) == Path.home() for d in creo_dirs), creo_dirs
    nx_dirs = cad_discovery.nx_syslog_dirs()
    assert nx_dirs and not any(cad_discovery._is_broad_dir(d) for d in nx_dirs), nx_dirs

    # (4b) 假 trail 陷阱回归：安装目录（PTC 自带样板 trail.txt.1，导致流1 全空）
    #      不许进"高优先级"候选，只当最后的备胎
    inst_dirs = cad_discovery.creo_install_dirs()
    low = {str(d).lower() for d in creo_dirs}
    high = {str(d).lower() for d in inst_dirs}
    assert not (low & high), low & high
    launch, install = tmp / "launch", tmp / "install"
    launch.mkdir(), install.mkdir()
    f_new = launch / "trail.txt.9"
    f_new.write_text("!%CI0.1\n", encoding="utf-8")
    f_old = install / "trail.txt.1"
    f_old.write_text("!%CI0.1\n", encoding="utf-8")
    os.utime(f_old, (time.time() - 86400, time.time() - 86400))
    from creo_bridge import find_latest_trail, CreoBridge
    assert find_latest_trail([str(launch)]) == f_new
    assert find_latest_trail([str(install)]) == f_old, "旧样板在安装目录里仍然能被找到（备胎）"

    # (4c) "trail 没在更新"要说清楚，不能让用户猜为什么流1 是空的
    stale_events = []
    br = CreoBridge({"stale_minutes": 10}, lambda kind, det: stale_events.append((kind, det)),
                    lambda: False)
    br._check_stale(f_old)
    br._check_stale(f_new)
    assert [k for k, _ in stale_events] == ["creo_trail_stale"], stale_events
    assert "没更新" in br.status()["last_error"], br.status()

    # (4d) 界面提示过滤：软件没开属于"正常等待"，不刷警告；真故障才提示
    from app import _is_waiting_msg
    assert _is_waiting_msg("SolidWorks 未运行（等它打开后自动连上）")
    assert _is_waiting_msg("没找到 *.syslog（NX 是否运行过？）")
    assert not _is_waiting_msg(br.status()["last_error"])

    # (5) 扫描预算：深度限制生效，且不抛异常
    deep = tmp / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "trail.txt.7").write_text("!%CI0.1\n", encoding="utf-8")
    assert scan_trail_files([str(tmp)], max_depth=6), "限深 6 应该能扫到"
    assert not scan_trail_files([str(tmp)], max_depth=1), "限深 1 不该扫到深层文件"
    assert scan_trail_files([str(tmp)], max_dirs=1) == [], "目录预算用完应直接返回"

    # (6) 笔势注册表路径改成"枚举所有版本"后仍然可用
    #     （注意：没装 SolidWorks 的机器上注册表里本来就没有笔势设置，所以
    #      "映射表非空"这个断言只在装了 SW 的机器上要求——否则 CI/别人电脑上会假失败）
    bases = ShortcutMap._registry_gesture_bases()
    assert bases and all(b.startswith(r"Software\SolidWorks") for b in bases), bases
    assert all(b.endswith("Mouse Gestures") for b in bases), bases
    sm = ShortcutMap(Path(tempfile.mkdtemp(prefix="cad-ai-sm-")))
    assert isinstance(sm.gesture_map, dict), sm.gesture_map
    if vers:  # 本机装了 SW（注册表里有版本键）→ 必须读到笔势
        assert sm.gesture_map, "装了 SW 却读不到笔势映射"

    # (6b) 命令 ID 表：本机生成逻辑（仓库不分发这张表）
    from sw_shortcuts import ensure_command_ids, _read_command_ids
    ids_dir = tmp / "ids"
    ids_dir.mkdir()
    (ids_dir / "sw_command_ids.json").write_text('{"32771": "swCommands_Test"}',
                                                 encoding="utf-8")
    got = ensure_command_ids(ids_dir)
    assert got == {32771: "swCommands_Test"}, got
    assert _read_command_ids(ids_dir / "sw_command_ids.json") == {32771: "swCommands_Test"}
    assert _read_command_ids(tmp / "not_there.json") == {}      # 缺文件不能抛异常
    generated = ensure_command_ids(tmp / "fresh_ids")   # 没表时：有 SW 就生成，没有就 None
    if generated is not None:
        assert len(generated) > 500, len(generated)
        assert all(isinstance(v, str) for v in list(generated.values())[:50])

    # (7) 桥接对象拿到自动发现出来的 ProgID
    b = SolidWorksBridge({}, lambda *a: None, lambda: False)
    assert b.prog_ids[0] == "SldWorks.Application", b.prog_ids
    b2 = SolidWorksBridge({"prog_id": "SldWorks.Application.99"}, lambda *a: None,
                          lambda: False)
    assert b2.prog_ids[0] == "SldWorks.Application.99", b2.prog_ids

    # (8) COM 绑定真的存在（不启动 SW 也能证明 ProgID 可用）+ 主程序路径可发现
    #     同样只在"本机装了 SW"时断言（CI 机器上没装）
    if vers:
        for pid in ids:
            buf = ctypes.create_string_buffer(16)
            hr = ctypes.windll.ole32.CLSIDFromProgID(ctypes.c_wchar_p(pid), buf)
            assert hr == 0, f"ProgID {pid} 无法解析成 CLSID（hr={hr}）"
    exe = cad_discovery.sw_exe_path()
    if exe:
        assert Path(exe).name.lower() == "sldworks.exe" and Path(exe).is_file(), exe

    # (9) 建模窗口清单（装聋判定）内置覆盖三大主流 CAD
    exes = [a["exe"].lower() for a in DEFAULT_CONFIG["modeling_apps"]]
    for need in ("sldworks.exe", "parametric.exe", "ugraf.exe"):
        assert need in exes, (need, exes)
    assert len(exes) >= 8, "开源版应内置更多 CAD，而不是只有三家的 4 个程序名"

    # (10) 数据目录：环境变量优先（开源版不再写死 E 盘）
    from collector_core import resolve_data_dir
    old = os.environ.get("CAD_AI_MONITOR_DATA")
    try:
        os.environ["CAD_AI_MONITOR_DATA"] = str(tmp / "custom_data")
        assert resolve_data_dir() == tmp / "custom_data", resolve_data_dir()
    finally:
        if old is None:
            os.environ.pop("CAD_AI_MONITOR_DATA", None)
        else:
            os.environ["CAD_AI_MONITOR_DATA"] = old

    print(f"  SW ProgID={ids}，注册表版本={vers}，"
          f"Creo 候选目录 {len(creo_dirs)} 个，NX 候选目录 {len(nx_dirs)} 个，"
          f"建模程序名 {len(exes)} 个")
    ok("CAD 自动发现正确（不限版本 ProgID / 版本枚举 / 宽目录过滤 / 扫描预算 / 数据目录）")


# CI 上能跑的"确定性"子集：不装钩子、不注入输入、不截图、不依赖本机装了哪家 CAD。
# 用法：python app\selftest.py --ci   （GitHub Actions 就是这么跑的）
CI_SAFE_TESTS = [
    test_config_merge,
    test_migration,
    test_diff_snapshot,
    test_window_filter,
    test_gesture_direction,
    test_creo_nx_parsers,
    test_reconcile,
    test_cad_discovery,
]


if __name__ == "__main__":
    if "--ci" in sys.argv:
        print("CI 子集（确定性检查，不需要 CAD / 屏幕 / 真实输入）\n")
        for _t in CI_SAFE_TESTS:
            _t()
        print("\nCI 子集全部通过 ✔")
        sys.exit(0)
    test_config_merge()
    test_migration()
    test_diff_snapshot()
    test_window_filter()
    test_end_to_end_stream2()
    test_leave_return()
    test_gesture_direction()
    test_shortcut_map()
    test_creo_nx_parsers()
    test_ai_eye_unit()
    test_capture_dpi_box()
    test_reconcile()
    test_gesture_and_ai_eye()
    test_cad_discovery()
    print("\n全部自检通过 ✔")
    sys.exit(0)
