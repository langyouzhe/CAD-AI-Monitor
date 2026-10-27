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
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# 强制 UTF-8 输出，避免老式控制台（GBK 编码）打印中文/符号报错
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from collector_core import (  # noqa: E402
    APP_VERSION, DEFAULT_CONFIG, ConfigManager, EventStore, SingleInstanceLock,
    create_session_dir, iter_all_events, iter_session_databases,
    migrate_legacy_data, read_session_json, recover_sessions,
    session_db_path, session_meta_path, session_summaries, sessions_root,
    write_session_json,
)
from input_listener import (  # noqa: E402
    CollectorSession, is_modeling_window, nx_record_hint,
)
from solidworks_bridge import diff_snapshot  # noqa: E402

APP_DIR = str(Path(__file__).resolve().parent)


def ok(msg):
    print("OK  " + msg)


class FakeKey:
    """可哈希的合成按键对象（types.SimpleNamespace 不可哈希，放进 _pressed 集合会抛异常）。"""

    def __init__(self, char=None):
        self.char = char


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
    apps = [{"exe": "SLDWORKS.exe", "title": "SOLIDWORKS"},
            {"exe": "ugraf.exe", "title": "NX"}]
    assert is_modeling_window(("Part1 - SOLIDWORKS", "SLDWORKS.exe"), apps)
    assert is_modeling_window(("whatever", "SLDWORKS.exe"), apps)      # exe 命中
    # 隐私回归（2026-10-23 高危 1）：exe 已知但不是 CAD → 标题再像也拒绝。
    # 浏览器标签页标题里出现 "SOLIDWORKS"/"NX"（搜索页、网盘文件名、聊天标题）
    # 以前会被当成建模窗口，导致浏览器里的操作被记录（装聋失效）。
    assert not is_modeling_window(("SOLIDWORKS Premium 2022", "explorer.exe"), apps)
    assert not is_modeling_window(("NX 2406 - 百度搜索", "chrome.exe"), apps)
    assert not is_modeling_window(("SOLIDWORKS 教程 - Edge", "msedge.exe"), apps)
    assert not is_modeling_window(("百度一下", "chrome.exe"), apps)    # 都命中不了
    # exe 取不到（None/空）才用标题兜底（权限不足拿不到进程名的情况）
    assert is_modeling_window(("Designcenter NX 2512", None), apps)
    assert is_modeling_window(("NX 2512", ""), apps)
    assert is_modeling_window((None, None), apps, allow_all=True)      # 调试开关
    assert not is_modeling_window((None, None), apps)
    assert not is_modeling_window(("", ""), apps)
    ok("装聋判定正确（exe 已知时只认 exe；exe 取不到才用标题兜底）")


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

    注意：这个测试要求"注入成功"，但注入可能被系统安全机制拒绝/丢弃
    （实测某些权限/安全软件环境里 SendInput 返回成功、低层键盘钩子却收不到，
    LastInputInfo 也不更新）。所以：
    - 钩子线程必须活着（真实安装成功）；
    - 注入成功 → 验证"真实系统输入 → 钩子 → 回调 → 入库"整条链路；
    - 注入被环境丢弃 → 明确 SKIP 这一段，改用钩子回调路径验证入库（保证自检
      确定性；文档要求自检不依赖真实输入）。
    """
    tmp = tempfile.mkdtemp(prefix="cad-ai-e2e-")
    session = _mk_session(tmp, idle_minutes=10.0)  # 阈值调大，本测试不测离开
    session.start()
    time.sleep(0.6)
    assert session._kb_listener is not None and session._kb_listener.is_alive(), \
        "键盘钩子线程应存活（真实安装成功）"
    assert session._ms_listener is not None and session._ms_listener.is_alive(), \
        "鼠标钩子线程应存活（真实安装成功）"
    send_key(0x1E)          # 字母 A
    time.sleep(0.2)
    send_key(0x1E, up=True)
    time.sleep(1.2)          # 留时间让事件走完 监听 → 回调 → 入库
    with session._lock:
        injected_count = session._counts.get("stream2", 0)
    if injected_count == 0:
        # 环境把合成键盘输入丢了：退回到"钩子回调 → 入库"这段确定性链路
        print("  SKIP 合成键盘输入被当前环境丢弃（低层钩子收不到）；"
              "改用钩子回调路径验证入库（真实注入在普通桌面/无安全拦截时可跑）")
        session._on_key_press(FakeKey("a"))
        session._on_key_press(FakeKey("s"))
        time.sleep(0.2)
    session.stop()

    events = session.store.query_session(session.session_id)
    kinds = [(e["source"], e["kind"]) for e in events]
    key_downs = [e for e in events if e["kind"] == "key_down"]
    assert key_downs, "钩子应至少收到按键（注入或钩子回调）"
    injected = [d for d in key_downs if d["detail"].get("key") in ("a", "A")]
    if injected_count:
        print("  注入的假按键 'a' 已被钩子捕获入库")
    else:
        print("  注入被环境拒绝；已用钩子回调验证入库链路（测试仍有效）")
    # 顺序正确性
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1)), "seq 不连续"
    # 没有 pause 事件（v2 不记停顿）
    assert not any(k == "pause" for _, k in kinds), "v2 不应再记 pause"
    print(f"  端到端录到 {len(events)} 条：{json.dumps({f'{s}/{k}': kinds.count((s, k)) for s, k in sorted(set(kinds))}, ensure_ascii=False)}")
    ok("端到端采集正确（按键入库 + 顺序连续 + 无停顿事件）")


# ---------- 6.5 离开/回来（确定性验证，不受真人输入干扰） ----------

def test_leave_return():
    tmp = tempfile.mkdtemp(prefix="cad-ai-lr-")
    session = _mk_session(tmp, idle_minutes=0.1)  # 阈值 6 秒
    session.start()
    time.sleep(0.4)
    session._on_key_press(FakeKey("a"))       # 第一次操作
    # 把"最后操作时间"拨回 10 秒前，直接调用离开检测
    with session._lock:
        session._last_event_ts = session._last_event_ts - 10_000
    session._maybe_leave()
    session._on_key_press(FakeKey("s"))       # 回来后的第一次操作
    time.sleep(0.3)
    session.stop()

    events = session.store.query_session(session.session_id)
    kinds = [(e["source"], e["kind"]) for e in events]
    leave = [e for e in events if e["kind"] == "leave"]
    returns = [e for e in events if e["kind"] == "return"]
    assert len(leave) == 1, f"应记一条 leave: {kinds}"
    assert len(returns) == 1, f"下一次活动前应补 return: {kinds}"
    assert returns[0]["seq"] > leave[0]["seq"], "return 应在 leave 之后"
    assert any(k == "key_down" for _, k in kinds), "合成按键应真的入库（不是被静默丢掉）"
    # 连续两次 _maybe_leave 不应重复记 leave
    session2 = _mk_session(tempfile.mkdtemp(prefix="cad-ai-lr2-"), idle_minutes=0.1)
    session2.start()
    time.sleep(0.3)
    session2._on_key_press(FakeKey("a"))
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

    # ---- NX journal（2026-10-23 换数据源：syslog → journal）----
    from journal_parser import (find_latest_journal, parse_journal,
                                parse_journal_line, read_journal_text)
    fx = (Path(__file__).resolve().parent.parent / "tests" / "fixtures"
          / "journal_sample.cs")
    assert fx.is_file(), f"缺少测试样本: {fx}"
    parsed = parse_journal(fx)
    menus = [e for e in parsed if e["type"] == "menu"]
    apis = [e for e in parsed if e["type"] == "api"]
    assert len(menus) >= 10, f"menu 事件只有 {len(menus)} 条（应 ≥10）"
    assert len(apis) >= 3, f"api 事件只有 {len(apis)} 条（应 ≥3）"
    assert any("Extrude" in e["menu"] for e in menus), menus
    raw_menu_lines = [ln for ln in read_journal_text(fx).splitlines() if "Menu:" in ln]
    assert len(raw_menu_lines) == len(menus), (len(raw_menu_lines), len(menus))
    for raw, ev in zip(raw_menu_lines, menus):
        assert raw.split("Menu:", 1)[1].strip() == ev["menu"], (raw, ev)
    assert parse_journal_line("var x = 1;") is None
    assert parse_journal_line("//   Menu: Insert->Sketch")["menu"] == "Insert->Sketch"
    # 编码：UTF-16（真实 journal）与 UTF-8 都能读
    jtmp = tempfile.mkdtemp(prefix="cad-ai-journal-")
    u8 = Path(jtmp) / "j8.py"
    u8.write_text("// Menu: Insert->Curve->Line\n"
                  "NXOpen.Session.GetSession()\n", encoding="utf-8")
    assert [e["type"] for e in parse_journal(u8)] == ["menu", "api"]
    # 内容过滤：普通 .py 不能被误当成 journal
    other = Path(jtmp) / "unrelated.py"
    other.write_text("print('hello')\n", encoding="utf-8")
    assert find_latest_journal([jtmp]).name == "j8.py"
    assert find_latest_journal([], explicit=str(fx)) == fx
    shutil.rmtree(str(jtmp), ignore_errors=True)
    ok("NX journal 解析正确（menu/API/UTF-16与UTF-8/内容过滤）")

    # ---- NX Python journal（v0.2.3）：'#   Menu:' 注释 + NXOpen 调用 ----
    # 以前 MENU_RE 只认 "// Menu:"，NX 的 Python journal 用 "#   Menu:" 就一条
    # 菜单也解析不到；这个 fixture 按真实 Python journal 的样子写（UTF-8）。
    from journal_parser import looks_like_journal
    fx_py = (Path(__file__).resolve().parent.parent / "tests" / "fixtures"
             / "journal_sample.py")
    assert fx_py.is_file(), f"缺少 Python journal 测试样本: {fx_py}"
    assert looks_like_journal(fx_py) is True, "Python journal 样本应被识别为 journal"
    py_parsed = parse_journal(fx_py)
    py_menus = [e for e in py_parsed if e["type"] == "menu"]
    py_apis = [e for e in py_parsed if e["type"] == "api"]
    assert len(py_menus) == 3, f"Python journal 应有 3 条 menu，实际 {len(py_menus)}"
    assert len(py_apis) == 2, f"Python journal 应有 2 条 api，实际 {len(py_apis)}"
    assert [e["type"] for e in py_parsed] == ["menu", "menu", "menu", "api", "api"], \
        f"Python journal 事件顺序/条数不对: {py_parsed}"
    assert py_menus[0]["menu"] == "File->New..."
    assert py_menus[-1]["menu"] == "Insert->Design Feature->Extrude..."
    assert (py_apis[0]["class"], py_apis[0]["method"]) == ("Session", "GetSession")
    assert (py_apis[1]["class"], py_apis[1]["method"]) == (
        "Features.FeatureCollection", "CreateExtrude")
    # 三种注释前缀（行首空白任意）都只认 "Menu:" 行；API_RE 行为不变
    assert parse_journal_line("    #   Menu: Insert->Sketch")["menu"] == "Insert->Sketch"
    assert parse_journal_line("'   Menu: Insert->Design Feature")["menu"] == "Insert->Design Feature"
    assert parse_journal_line("// Menu: Insert->Curve->Line")["menu"] == "Insert->Curve->Line"
    assert parse_journal_line("# Menu list: not a menu line") is None, \
        "只匹配 'Menu:' 行，'# Menu list:' 不算"
    assert parse_journal_line("NXOpen.Session.GetSession()") == {
        "type": "api", "class": "Session", "method": "GetSession"}
    ok("NX Python journal 解析正确（# Menu 注释 + NXOpen；VB 的 ' Menu 也认）")


# ---------- 9. AI 眼（假模式）单元测试：时间戳对齐（确定性，不受真人输入干扰） ----------

def _fake_capture(eye):
    """给 AiEye 换一个"假截图"（测试不依赖真实屏幕/锁屏状态）。

    真实的截图裁剪（DPI/多显示器）由 test_capture_dpi_box 单独验证；
    本函数只保证触发规则、时间戳对齐、保存开关这些逻辑可确定性验证。
    """
    def capture(job):
        path = None
        if eye.keep_images:
            eye.images_dir.mkdir(parents=True, exist_ok=True)
            path = eye.images_dir / f"{job['ts']}_{job['kind']}.jpg"
            path.write_bytes(b"fake-jpeg-bytes")
        return (str(path) if path else None,
                "ZmFrZS1qcGVnLWJ5dGVz")  # base64("fake-jpeg-bytes")
    return capture


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
    eye._capture = _fake_capture(eye)   # 不碰真实屏幕（锁屏也能测）
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
    # 勾了"保存截图"：图应落在本地 images 目录（v4：直接放，不再按日期分层）
    img = detail.get("image")
    assert img and _P(img).exists(), "截图应保存到本地 images 目录"
    assert _P(img).parent == _P(tmp) / "images", f"截图应直接放 images\\ 下: {img}"
    assert detail.get("image_rel", "").startswith("images/"), detail.get("image_rel")

    # 不勾"保存截图"：磁盘上不应留下任何图（单一开关，无冲突）
    results2 = []
    eye2 = AiEye(cfg={"provider": "mock", "delay_s": 0.01, "dedup_window_s": 0,
                      "max_queue": 5, "rate_per_min": 999, "save_images": False,
                      "triggers": {"click_left": True}},
                 on_result=lambda d, t: results2.append((d, t)),
                 is_running=lambda: True, images_dir=_P(tmp) / "images2")
    eye2._capture = _fake_capture(eye2)
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


# ---------- 9.5 按窗口抓取：尺寸换算 + 无效矩形拒绝（回归锁） ----------

class _DesktopUnavailable(RuntimeError):
    """自检环境限制：当前桌面/前台窗口拿不到（锁屏/无交互桌面）。

    这是 selftest 里**唯一**允许 SKIP 的原因。任何断言失败都不许伪装成它。
    """


def _env_capture_error_types():
    """明确属于"环境不可用"的异常白名单（Win32/系统抓屏错误）。

    第三轮复验 P2：截图测试只能把这一类转成 SKIP；ValueError/MemoryError/
    RuntimeError 等程序错误必须向外传播（自检失败）。
    """
    import ai_eye
    env_types = [OSError, ai_eye.WindowCaptureUnavailable]
    try:
        import pywintypes
        env_types.append(pywintypes.error)
    except ImportError:
        pass
    return tuple(env_types)


def _probe_real_window_capture():
    """真实探测：取前台窗口矩形 → PrintWindow 按窗口抓一张图（不整屏）。

    只有**明确的环境原因**才转 _DesktopUnavailable：
    - 没有前台窗口 / 前台窗口最小化 / 窗口矩形无效；
    - Win32/系统抓屏错误（OSError、pywintypes.error、WindowCaptureUnavailable）。
    ValueError/MemoryError/RuntimeError 等程序错误照常向外抛 → 自检失败，
    **绝不伪装成 SKIP**。
    """
    import win32gui
    from ai_eye import _grab_window_content, window_capture_size
    hwnd = int(win32gui.GetForegroundWindow() or 0)
    if not hwnd:
        raise _DesktopUnavailable("没有前台窗口")
    try:
        iconic = bool(win32gui.IsIconic(hwnd))
        rect = win32gui.GetWindowRect(hwnd)
    except _env_capture_error_types() as exc:
        raise _DesktopUnavailable(f"拿不到前台窗口矩形: {exc}") from exc
    if iconic:
        raise _DesktopUnavailable("前台窗口处于最小化状态")
    size = window_capture_size(rect)
    if size is None:
        raise _DesktopUnavailable(f"前台窗口矩形无效: {rect!r}")
    try:
        img = _grab_window_content(hwnd, size)
    except _env_capture_error_types() as exc:   # 明确的抓屏系统错误 → 环境限制
        raise _DesktopUnavailable(f"当前桌面/窗口不可抓取: {exc}") from exc
    return {"hwnd": hwnd, "rect": rect, "size": size, "image": img}


def _assert_real_window_capture(info):
    """严格断言：抓到的只能是**窗口尺寸**的图，无效窗口框必须被拒绝。

    没有 try/except —— 断言失败必须让自检失败（旧版把 AssertionError 也当
    "桌面不可截图" SKIP 吞掉，非法裁剪框照样打印 OK；这里就是回归锁）。
    """
    from ai_eye import window_capture_size
    rect = info["rect"]
    size = info["size"]
    img = info["image"]
    assert size is not None, f"窗口矩形非法却算出了抓取尺寸: {rect!r}"
    assert size == window_capture_size(rect), (size, rect)
    w, h = size
    assert w >= 50 and h >= 50, f"窗口抓取尺寸不合理: {size}"
    assert img is not None and tuple(img.size) == (w, h), (
        f"按窗口抓取应只得到窗口尺寸图：image={getattr(img, 'size', None)} size={size}")


def test_capture_dpi_box(desktop_probe=None):
    """按窗口抓取的尺寸换算 / 无效矩形拒绝（**不再有整屏 grab 再裁剪**）。

    2026-10-23 二次复验：截图改为 PrintWindow 按窗口抓画面，没有整屏路径。
    这里验证纯换算与拒绝规则；桌面可抓时再验证"抓到的图 = 窗口尺寸"。
    SKIP 只允许"当前桌面拿不到前台窗口/不可抓取"这一种环境原因；断言失败
    一律向外传播（**绝不能**像旧版那样 except Exception 把 AssertionError
    也吞成 SKIP）。
    """
    from ai_eye import window_capture_size
    # (1) 正常窗口矩形 → 位图尺寸 = 窗口尺寸（逻辑 × 系统 DPI 缩放）
    assert window_capture_size((100, 100, 500, 400)) == (400, 300)
    assert window_capture_size((-100, 50, 500, 400), scale=1.5) == (900, 525)
    # (2) 无效矩形/最小化（宽或高 < 50）→ None（调用方抓取前拒绝，0 次抓取）
    assert window_capture_size((0, 0, 10, 10)) is None
    assert window_capture_size((100, 100, 40, 400)) is None
    assert window_capture_size("不是矩形") is None
    assert window_capture_size(None) is None
    assert window_capture_size((0, 0, 1920, 1080), scale=0) is None
    assert window_capture_size((0, 0, 200000, 100)) is None
    # (3) 真实窗口（本环境能拿到桌面时）：抓到的图必须正好是窗口尺寸
    try:
        info = (desktop_probe or _probe_real_window_capture)()
    except _DesktopUnavailable as exc:
        # 工作站锁屏 / 没有可交互桌面：属于环境限制，不算代码失败。
        print(f"  SKIP 真实按窗口抓取验证（当前桌面不可用：{exc}）；"
              f"解锁屏幕后重跑可覆盖真实窗口抓取")
        ok("按窗口抓取尺寸换算/无效矩形拒绝正确（真实窗口抓取本环境跳过）")
        return
    _assert_real_window_capture(info)     # 严格断言：失败 → 自检失败
    print(f"  真实窗口 hwnd={info['hwnd']} 矩形={info['rect']} "
          f"抓取尺寸={info['size']}（按窗口，不是整屏）")
    ok("按窗口抓取：只得到窗口尺寸图 + 窗口框无效时抓取前拒绝（真实窗口验证）")


def test_invalid_capture_box_fails_selftest():
    """【P2 回归】非法裁剪框/程序错误必须让自检失败，只有环境不可用才 SKIP。

    旧版 test_capture_dpi_box 用 `except Exception` 包住全部断言，AssertionError
    被当成"桌面不可截图"打印 SKIP、非法裁剪框照样 OK。第三轮复验又指出：宽泛
    except 会把 ValueError/MemoryError/RuntimeError 也当环境 SKIP。这里直接调
    **真正的自检函数**验证：
    - 非法窗口框 → 严格断言抛错；
    - 注入 ValueError/MemoryError/RuntimeError → 原样向外传播（自检失败）；
    - 注入"明确的环境不可用" → 才允许 SKIP。
    """
    # 1) 非法窗口框 → 严格断言必须抛
    def bad_probe():
        return {"hwnd": 1, "rect": (100, 100, 40, 400),
                "size": None, "image": None}

    try:
        test_capture_dpi_box(desktop_probe=bad_probe)
    except AssertionError as exc:
        print(f"  非法窗口框 → 自检断言抛出: {str(exc)[:100]}")
    else:
        raise AssertionError("非法裁剪框必须让自检失败"
                             "（旧 bug：被当成 SKIP 还打印 OK）")

    # 2) 程序错误（ValueError/MemoryError/RuntimeError）→ 必须向外传播
    for injected in (ValueError("坏参数"),
                     MemoryError(),
                     RuntimeError("程序 bug")):
        def raising_probe(_exc=injected):
            raise _exc

        try:
            test_capture_dpi_box(desktop_probe=raising_probe)
        except Exception as got:
            assert got is injected, (type(got), got)
        else:
            raise AssertionError(
                f"{type(injected).__name__} 必须让自检失败，不能被当成 SKIP")

    # 3) 明确的环境不可用 → 才允许 SKIP（捕获输出确认真的走了 SKIP 分支）
    import contextlib
    import io

    def unavailable_probe():
        raise _DesktopUnavailable("测试模拟：没有交互桌面")

    skip_out = io.StringIO()
    with contextlib.redirect_stdout(skip_out):
        test_capture_dpi_box(desktop_probe=unavailable_probe)
    assert "SKIP" in skip_out.getvalue(), skip_out.getvalue()

    # 4) 环境白名单本身：只认 OSError / pywintypes.error / WindowCaptureUnavailable
    from ai_eye import WindowCaptureUnavailable
    env_types = _env_capture_error_types()
    for env_err in (OSError("系统错误"), WindowCaptureUnavailable("拿不到窗口")):
        assert isinstance(env_err, env_types), type(env_err)
    try:
        import pywintypes
        assert pywintypes.error in env_types, env_types
    except ImportError:
        pass
    for prog_err in (ValueError("x"), MemoryError(), RuntimeError("x"),
                     KeyError("x"), AttributeError("IFace2.Name")):
        assert not isinstance(prog_err, env_types), type(prog_err)
    ok("非法裁剪框/程序错误会让自检失败（断言向外传播）；只有明确环境不可用才 SKIP")


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
    assert session.ai_eye is not None, "AI 眼应按配置启动"
    # 接线回归：会话创建 AI 眼时必须把建模软件名单/调试开关传进去，
    # 否则"截图前复核"永远判失败 → AI 眼等于被静默关掉。
    assert session.ai_eye.modeling_apps == list(session.cfg.get("modeling_apps") or []), \
        "AI 眼必须拿到与装聋判定同一份 modeling_apps"
    assert session.ai_eye.allow_all_windows is True, \
        "debug.allow_all_windows 要传给 AI 眼（自检模式）"
    if session.ai_eye is not None:
        session.ai_eye._capture = _fake_capture(session.ai_eye)  # 不依赖真实屏幕
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
    # 真人输入可能同时产生别的笔势（本测试开着全局钩子）→ 只认本测试注入的那一条，
    # 否则桌面活跃时自检会随机失败（"1 条笔势"断言被真实鼠标打扰）。
    injected = [e for e in gestures
                if e["detail"].get("start") == [400, 400]
                and e["detail"].get("end") == [500, 300]]
    assert len(injected) == 1, (
        f"应有一条本测试注入的笔势事件: injected={injected} all={gestures}")
    gestures = injected
    assert gestures[0]["detail"]["direction"] == "右上", gestures[0]["detail"]
    assert gestures[0]["detail"]["distance_px"] >= 80
    assert gestures[0]["detail"].get("polyline"), "应记录途经点"
    assert "command_id" in gestures[0]["detail"], "笔势事件必须带 command_id 字段（映射信息）"
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

    # (7b) 每个后端的状态里必须自带 backend 名（界面上"流1"那行靠它显示谁连上了；
    #      曾经 Creo 少了这个字段 → 界面显示成 "?✓"）
    from nx_bridge import NxBridge
    for bridge, name in ((b, "solidworks"),
                         (CreoBridge({}, lambda *a: None, lambda: False), "creo"),
                         (NxBridge({}, lambda *a: None, lambda: False), "nx")):
        assert bridge.status().get("backend") == name, bridge.status()

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


# ======================================================================
# v4 会话存储（v0.2.0）：会话文件夹 / 空会话 / 崩溃会话 / 旧库归档 / 遍历分析
# ======================================================================

# 测试专用"建模软件"名单：保证真人输入一律装聋（否则测试会被前台窗口污染）
TEST_ONLY_APPS = [{"exe": "__cad_ai_no_such_app__.exe",
                   "title": "__CAD_AI_NO_SUCH_WINDOW__"}]


def _mk_session_v4(tmp, stream1_enabled=False, idle_minutes=10.0):
    """建一个 v4 会话对象：只记测试合成的事件，不受真人输入干扰。"""
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "version": 2,
            "idle_minutes": idle_minutes,
            "modeling_apps": TEST_ONLY_APPS,
            "debug": {"allow_all_windows": False},
            "stream1": {"enabled": stream1_enabled, "poll_interval_s": 0.5},
        }, f, ensure_ascii=False, indent=2)
    return CollectorSession(tmp)


def test_v4_session_naming():
    """会话文件夹命名：<yyyy-mm-dd_HHMMSS>，重名自动 _2、_3。"""
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-v4name-"))
    when = datetime(2026, 10, 19, 12, 20, 35)
    a = create_session_dir(tmp, when=when)
    b = create_session_dir(tmp, when=when)
    c = create_session_dir(tmp, when=when)
    assert a.name == "2026-10-19_122035", a.name
    assert b.name == "2026-10-19_122035_2", b.name
    assert c.name == "2026-10-19_122035_3", c.name
    assert a.parent == b.parent == c.parent == sessions_root(tmp)
    for p in (a, b, c):
        assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{6}(_\d+)?$", p.name), p.name
    shutil.rmtree(str(tmp), ignore_errors=True)
    ok("v4 会话命名正确（年月日_时分秒 + 重名自动 _2/_3）")


def test_v4_session_folder_and_json():
    """两次「开始→停止」→ 两个独立会话文件夹，各含 images\\、data\\events.db、session.json。"""
    tmp = tempfile.mkdtemp(prefix="cad-ai-v4layout-")
    dirs = []
    for _ in range(2):
        session = _mk_session_v4(tmp)
        session.start()
        time.sleep(0.05)
        session._record("stream2", "click", {"button": "left", "action": "press",
                                             "x": 10, "y": 20})
        session._record("stream2", "click", {"button": "left", "action": "release",
                                             "x": 10, "y": 20})
        summary = session.stop()
        assert summary and summary["kept"] is True, summary
        session_dir = Path(summary["session_dir"])
        dirs.append(session_dir)
        # 停止后库已关闭，但读取仍必须可用（只读重开）
        events = session.store.query_session(session.session_id)
        assert len(events) == summary["event_count"] >= 2
        assert events[0]["kind"] == "session_start"
        assert events[-1]["kind"] == "session_stop"

    assert len(set(d.name for d in dirs)) == 2, "两次采集必须落在两个不同文件夹"
    expected_keys = ("app_version", "storage_version", "schema_version", "status",
                     "started_at", "stopped_at", "software", "event_count",
                     "counts", "session_id", "db", "images")
    for session_dir in dirs:
        assert session_dir.parent == sessions_root(tmp)
        assert (session_dir / "images").is_dir(), "会话里必须有 images\\"
        db = session_dir / "data" / "events.db"
        assert db.is_file(), "会话里必须有 data\\events.db"
        assert not (Path(tmp) / "events.db").exists(), "v4 不应再写根目录单库"
        meta = read_session_json(session_dir)
        assert meta, "会话里必须有 session.json"
        for key in expected_keys:
            assert key in meta, (key, meta)
        assert meta["app_version"] == APP_VERSION, meta["app_version"]
        assert meta["schema_version"] == 2
        assert meta["storage_version"] == 4
        assert meta["status"] == "completed"
        assert meta["stopped_at"], "正常停止必须有停止时间"
        assert meta["counts"]["stream2"] == 2, meta["counts"]
        assert isinstance(meta["software"], dict)
        assert "enabled" in meta["software"] and "connected" in meta["software"]
        # sessions 表里必须有这条记录、且已结束（表结构不变）
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT started_at, stopped_at, event_count FROM sessions").fetchone()
        finally:
            conn.close()
        assert row and row[1], row
        assert row[2] == meta["event_count"] == summary["event_count"], (row, meta)
    shutil.rmtree(tmp, ignore_errors=True)
    ok("v4 会话文件夹结构正确（images/ + data/events.db + session.json，两次采集互相独立）")


def test_v4_empty_session_removed():
    """空会话不留痕：正常停止且除开始/停止标记外 0 事件 → 整个文件夹删除。"""
    tmp = tempfile.mkdtemp(prefix="cad-ai-v4empty-")
    session = _mk_session_v4(tmp)   # 测试专用假建模名单 → 真人输入全部装聋
    session.start()
    time.sleep(0.3)
    summary = session.stop()
    assert summary is not None, "stop 应返回小结"
    # 空会话里可能还有一条 meta/stream1_unavailable 对账汇总（流1 没数据源的诊断），
    # 但它属于 meta，不算用户数据 → 仍判空会话
    assert summary["counts"]["stream1"] == 0, summary
    assert summary["counts"]["stream2"] == 0, summary
    assert summary["kept"] is False
    assert not Path(summary["session_dir"]).exists(), "空会话必须整个删除"
    assert list((Path(tmp) / "sessions").iterdir()) == [], "sessions\\ 下不该留空会话文件夹"
    shutil.rmtree(tmp, ignore_errors=True)
    ok("v4 空会话不留痕（0 事件正常停止 → 会话文件夹整个删除）")


def test_v4_crash_recovery():
    """崩溃半截会话：一律保留、WAL checkpoint、session.json 标 aborted；只清无 db 空目录。"""
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-v4crash-"))
    root = sessions_root(tmp)
    root.mkdir(parents=True)

    # 1) 真·崩溃会话：子进程写库后 os._exit（不写 stopped_at、不 checkpoint）
    crash_dir = root / "2026-10-19_100000"
    (crash_dir / "data").mkdir(parents=True)
    (crash_dir / "images").mkdir()
    crash_db = crash_dir / "data" / "events.db"
    code = (
        "import os, sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from collector_core import EventStore\n"
        "st = EventStore(%r)\n"
        "sid = st.start_session()\n"
        "st.add_event(sid, 1, 'stream2', 'click', "
        "{'button': 'left', 'action': 'press'}, int(time.time() * 1000))\n"
        "os._exit(3)\n"
    ) % (APP_DIR, str(crash_db))
    proc = subprocess.run([sys.executable, "-c", code], timeout=60)
    assert proc.returncode == 3, f"模拟崩溃的子进程退出码不对: {proc.returncode}"
    wal = Path(str(crash_db) + "-wal")
    assert wal.exists(), "模拟崩溃应留下 -wal（测试前提）"

    # 2) 崩溃留下的空残骸：没有 db、没有任何文件 → 启动清理
    empty_dir = root / "2026-10-19_100001"
    (empty_dir / "data").mkdir(parents=True)
    (empty_dir / "images").mkdir()

    # 3) 正常结束的会话 A：没有 session.json → 启动时补一份 completed
    done_dir = root / "2026-10-19_090000"
    (done_dir / "data").mkdir(parents=True)
    (done_dir / "images").mkdir()
    store = EventStore(session_db_path(done_dir))
    sid = store.start_session()
    store.add_event(sid, 1, "stream2", "key_down", {"char": "a"},
                    int(time.time() * 1000))
    store.end_session(sid, 1)
    store.close()
    # 4) 正常结束的会话 B：已有 session.json → 不许被覆盖
    done2_dir = root / "2026-10-19_090500"
    (done2_dir / "data").mkdir(parents=True)
    (done2_dir / "images").mkdir()
    store2 = EventStore(session_db_path(done2_dir))
    sid2 = store2.start_session()
    store2.end_session(sid2, 0)
    store2.close()
    write_session_json(done2_dir, {"status": "completed", "marker": "keep-me"})

    result = recover_sessions(tmp)
    assert str(crash_dir) in result["aborted"], result
    assert not empty_dir.exists(), "无 db 的空目录应被清理"
    assert done_dir.is_dir() and done2_dir.is_dir(), "正常会话不能被误删"

    # 崩溃会话：保留 + 摘要标 aborted + stopped_at 仍为空（表结构不变）
    meta = read_session_json(crash_dir)
    assert meta and meta["status"] == "aborted", meta
    assert meta["stopped_at"] is None
    assert meta["event_count"] >= 1 and meta["recovered_at"], meta
    assert "强杀" in meta.get("note", "") or "崩溃" in meta.get("note", "")
    conn = sqlite3.connect(str(crash_db))
    try:
        row = conn.execute("SELECT stopped_at FROM sessions").fetchone()
    finally:
        conn.close()
    assert row and row[0] is None, "aborted 会话的 stopped_at 必须保持空"
    assert (not wal.exists()) or wal.stat().st_size < 4096, "崩溃会话应已 WAL checkpoint"

    # 正常会话：摘要丢了就补、已有的不覆盖
    assert read_session_json(done_dir)["status"] == "completed"
    kept = read_session_json(done2_dir)
    assert kept.get("marker") == "keep-me", "已有 session.json 不应被启动扫描覆盖"
    shutil.rmtree(str(tmp), ignore_errors=True)
    ok("v4 崩溃会话保留并标 aborted（WAL checkpoint + 摘要恢复），只清无 db 空目录")


def test_v4_legacy_archive():
    """旧数据整包归档：events.db→legacy\\events_legacy.db、images→legacy\\images，不断链、可幂等。"""
    base = Path(tempfile.mkdtemp(prefix="cad-ai-v4legacy-"))
    data = base / "data"
    data.mkdir()
    img_dir = data / "images" / "20261012"
    img_dir.mkdir(parents=True)
    img = img_dir / "1791814096825_click.jpg"
    img.write_bytes(b"fake-jpeg-bytes")
    store = EventStore(data / "events.db")
    sid = store.start_session()
    store.add_event(sid, 1, "stream2", "ai_eye",
                    {"annotates": "click", "image": str(img)}, 1791814096825)
    store.end_session(sid, 1)
    store.close()

    info = migrate_legacy_data(data)
    assert info and info["ok"] is True, info
    assert not (data / "events.db").exists(), "旧库应已搬走"
    legacy_db = data / "legacy" / "events_legacy.db"
    assert legacy_db.is_file(), "旧库应改名成 legacy\\events_legacy.db"
    assert not (data / "images").exists(), "旧图应已搬走"
    new_img = data / "legacy" / "images" / "20261012" / "1791814096825_click.jpg"
    assert new_img.is_file(), "旧图应保持相对位置放在 legacy\\images 下"
    # 库内图片路径已改写 → 逐条都能点开（不断链）
    conn = sqlite3.connect(str(legacy_db))
    try:
        detail = json.loads(conn.execute(
            "SELECT detail FROM events WHERE kind = 'ai_eye'").fetchone()[0])
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        conn.close()
    assert Path(detail["image"]) == new_img, detail["image"]
    assert Path(detail["image"]).is_file(), "库内图片路径必须真实存在（不能断链）"
    assert info["rewritten"] == 1
    # 迁移日志 + 幂等（再启动一次不会重复搬）
    assert (data / "legacy" / "migration.log").is_file(), "迁移结果应有日志"
    assert migrate_legacy_data(data) is None, "无旧数据时应直接返回 None（幂等）"
    # 重名不覆盖：又出现一份旧库 → events_legacy_2.db，已有归档不动
    store2 = EventStore(data / "events.db")
    sid2 = store2.start_session()
    store2.end_session(sid2, 0)
    store2.close()
    info2 = migrate_legacy_data(data)
    assert info2 and info2["ok"] is True
    assert Path(info2["db"]).name == "events_legacy_2.db", info2["db"]
    assert legacy_db.is_file(), "已迁移的数据不能被覆盖"

    # 迁移失败必须回退成"原样不动"（把 legacy 占成一个文件 → 建目录失败）
    data2 = base / "data_broken"
    data2.mkdir()
    store3 = EventStore(data2 / "events.db")
    sid3 = store3.start_session()
    store3.end_session(sid3, 0)
    store3.close()
    (data2 / "legacy").write_text("i am a file, not a dir", encoding="utf-8")
    bad = migrate_legacy_data(data2)
    assert bad and bad["ok"] is False, bad
    assert (data2 / "events.db").is_file(), "迁移失败时旧库必须原样不动"
    shutil.rmtree(str(base), ignore_errors=True)
    ok("v4 旧数据整包归档正确（改名/图保持相对位置/路径不断链/幂等/失败回退）")


def test_v4_iterate_all_sessions():
    """跨会话分析：遍历 data\\sessions\\*\\data\\events.db（不再假设单库）。"""
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-v4iter-"))
    root = sessions_root(tmp)
    root.mkdir(parents=True)
    names = ["2026-10-19_100000", "2026-10-19_110000"]
    for i, name in enumerate(names):
        d = root / name
        (d / "data").mkdir(parents=True)
        (d / "images").mkdir()
        st = EventStore(session_db_path(d))
        sid = st.start_session()
        st.add_event(sid, 1, "stream2", "key_down", {"char": "a"},
                     1700000000000 + i * 1000)
        st.end_session(sid, 1)
        st.close()
        write_session_json(d, {"app_version": "0.2.0", "schema_version": 2,
                               "status": "completed", "started_at": "x",
                               "stopped_at": "y", "event_count": 1,
                               "software": {"enabled": [], "connected": []}})
    # 数据根目录没有单库，分析也必须能找到两个会话
    assert not (tmp / "events.db").exists()
    dbs = list(iter_session_databases(tmp))
    assert len(dbs) == 2, dbs
    events = list(iter_all_events(tmp))
    assert len(events) == 2
    assert [e["session"] for e in events] == names
    assert all(e["session_dir"] for e in events)
    summaries = session_summaries(tmp)
    assert len(summaries) == 2
    assert all(s["status"] == "completed" and s["event_count"] == 1 for s in summaries)
    # 摘要丢了：不开 session.json 也能从库补出来
    (root / names[1] / "session.json").unlink()
    summaries2 = session_summaries(tmp)
    assert summaries2[1]["event_count"] == 1
    assert summaries2[1]["status"] == "completed"
    shutil.rmtree(str(tmp), ignore_errors=True)
    ok("v4 跨会话分析正确（遍历 sessions\\*\\data\\events.db + 摘要可回填）")


def test_v4_single_instance_lock():
    """单实例锁：第二个实例拿不到锁（含另一个进程），释放后可重新拿。"""
    name = "Local\\CAD-AI-Collector-selftest-%d" % os.getpid()
    first = SingleInstanceLock(name)
    assert first.acquire() is True, "第一个实例应拿到锁"
    second = SingleInstanceLock(name)
    assert second.acquire() is False, "同一进程里第二个实例也必须拿不到锁"
    # 真·另一个进程也拿不到（禁止两个实例同时写数据）
    if not getattr(sys, "frozen", False):
        code = ("import sys; sys.path.insert(0, %r); "
                "from collector_core import SingleInstanceLock; "
                "l = SingleInstanceLock(%r); "
                "print('acquired' if l.acquire() else 'blocked')"
                % (APP_DIR, name))
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=30)
        assert out.stdout.strip() == "blocked", (out.stdout, out.stderr)
    first.release()
    third = SingleInstanceLock(name)
    assert third.acquire() is True, "释放后应能重新拿到锁"
    third.release()
    from collector_core import DEFAULT_INSTANCE_NAME
    assert DEFAULT_INSTANCE_NAME.startswith("Local\\"), DEFAULT_INSTANCE_NAME
    ok("v4 单实例锁正确（第二个实例被拒绝，跨进程有效）")


def test_v4_ai_eye_drain():
    """停止时排空 AI 眼队列（上限 drain_timeout_s），结果不丢；超时有上限。"""
    from ai_eye import AiEye
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-v4drain-"))
    results = []

    def make_eye(images):
        eye = AiEye(cfg={"provider": "mock", "delay_s": 0.01,
                         "dedup_window_s": 0, "max_queue": 10,
                         "rate_per_min": 999, "save_images": False,
                         "drain_timeout_s": 5,
                         "triggers": {"click_left": True}},
                    on_result=lambda d, t: results.append((d, t)),
                    is_running=lambda: True, images_dir=images)
        eye._capture = _fake_capture(eye)   # 不碰真实屏幕
        return eye

    eye = make_eye(tmp / "images")
    eye.start()
    for i in range(3):
        eye.trigger("click", {"button": "left"}, 1700000000000 + i * 1000, (10, 10))
    info = eye.stop()          # 默认排水：把已排队的 3 张都做完
    assert len(results) == 3, f"停止前应排空队列，实际只收到 {len(results)} 条"
    assert info["pending"] == 0, info
    # 停止之后不再接收新触发（避免写进已结束的会话）
    eye.trigger("click", {"button": "left"}, 1700000000999, (10, 10))
    time.sleep(0.2)
    assert len(results) == 3, "停止后不应再接收新截图任务"

    # 排空上限：在途云端请求等太久时，stop 不能无限等（drain 只等 timeout 秒）
    slow = make_eye(tmp / "images_slow")
    slow._recognize = lambda job, b64: (time.sleep(2.0),
                                        {"ok": True, "answer": {}})[1]
    slow.start()
    slow.trigger("click", {"button": "left"}, 1700000000000, (10, 10))
    time.sleep(0.15)           # 让它进入"在途"
    info2 = slow.stop(drain_timeout_s=0.2)
    assert info2["drained_s"] < 1.5, f"排空上限没生效: {info2}"
    shutil.rmtree(str(tmp), ignore_errors=True)
    ok("v4 停止时 AI 眼队列排空正确（结果不丢 + 在途请求有上限）")


def test_reconcile_stream1_guard():
    """P1-3（2026-10-23）：流1 不可用时跳过对账，只写一条 meta/stream1_unavailable。

    以前流1 全空时，每个动作都找不到"效果"，158 条 anomaly 全是误报。
    """
    from reconcile import run_on_session, stream1_available

    class _FakeStore:
        def query_session(self, sid):
            return []

    class _FakeSession:
        def __init__(self, connected):
            self.session_id = 1
            self.store = _FakeStore()
            self.recorded = []
            self._connected = connected

        def stats(self):
            return {"stream1_status": [{"backend": "nx", "connected": self._connected}]}

        def _record(self, source, kind, detail):
            self.recorded.append((source, kind, detail))
            return 0

    cfg = {"reconcile": {"enabled": True}}
    s = _FakeSession(False)
    assert stream1_available(s) is False
    assert run_on_session(s, cfg) == 0
    assert [k for _, k, _ in s.recorded] == ["stream1_unavailable"], s.recorded

    s2 = _FakeSession(True)
    assert stream1_available(s2) is True
    assert run_on_session(s2, cfg) == 0   # 有流1 → 走原逻辑（无事件 → 0 异常）
    assert s2.recorded == [], s2.recorded
    ok("对账前置判断正确（流1 不可用 → 只写 1 条汇总，不产出误报异常）")


def test_nx_journal_bridge():
    """P0-1（2026-10-23）：NxBridge 从 journal 追读到新增 menu/API 事件（不需要 NX）。"""
    from nx_bridge import NxBridge
    tmp = tempfile.mkdtemp(prefix="cad-ai-nxbridge-")
    journal = Path(tmp) / "journal.cs"
    journal.write_text("// NX journal\n// Menu: Insert->Sketch\n", encoding="utf-16")
    events = []

    def on_event(kind, detail):
        events.append((kind, detail))

    bridge = NxBridge({"journal_path": str(journal), "poll_interval_s": 0.2},
                      on_event, lambda: True)
    bridge.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
                k == "nx_session" for k, _ in events):
            time.sleep(0.05)
        assert any(k == "nx_session" for k, _ in events), f"没挂上 journal: {events}"
        assert bridge.status()["connected"] is True
        with open(journal, "ab") as f:      # 追加新操作（UTF-16LE，无 BOM）
            f.write("// Menu: Insert->Design Feature->Extrude...\n".encode("utf-16-le"))
            f.write("NXOpen.Features.FeatureCollection.CreateExtrude(null)\n"
                    .encode("utf-16-le"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
                k == "nx_journal" for k, _ in events):
            time.sleep(0.05)
        nx_ev = [d for k, d in events if k == "nx_journal"]
        assert nx_ev, f"追加后没有 nx_journal 事件: {events}"
        assert any(d.get("type") == "menu" and "Extrude" in d.get("menu", "")
                   for d in nx_ev), nx_ev
        assert any(d.get("type") == "api" for d in nx_ev), nx_ev
        ok("NX journal 桥追读正确（追加即出 menu/API 事件）")
    finally:
        bridge.stop()
        shutil.rmtree(str(tmp), ignore_errors=True)


def test_nx_record_hint():
    """方案 A（2026-10-23）：NX 手动录制提醒（纯函数，4 种情况 + 边界）。

    只喂 (窗口, 后端状态, 配置, 数据目录)，不读全局、不依赖 Windows API、
    不需要 NX 真的开着，所以能进 --ci。
    """
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-nxhint-"))
    nx_window = ("Designcenter NX 2512", "ugraf.exe")
    sw_window = ("Part1 - SOLIDWORKS", "SLDWORKS.exe")
    cfg = {
        "stream1": {"enabled": True, "backends": ["solidworks", "creo", "nx"]},
        "nx": {"hint_when_not_recording": True},
    }
    not_connected = [{"backend": "nx", "connected": False, "last_error": "没找到"}]
    connected = [{"backend": "nx", "connected": True, "journal_file": "x.cs"}]
    try:
        # 1) 命中：启用了 nx 后端 + 前台是 NX + nx 没连上 → 提醒 + 日志目录
        hint = nx_record_hint(nx_window, not_connected, cfg, tmp)
        assert hint is not None, hint
        assert "Record Journal" in hint["text"], hint
        assert "nx.journal_path" in hint["text"], hint
        assert Path(hint["journal_dir"]) == tmp / "journals", hint

        # 2) 已连接（找到 journal）→ 不提醒
        assert nx_record_hint(nx_window, connected, cfg, tmp) is None

        # 3) 配置里把提醒关掉 → 不提醒
        off = {"stream1": cfg["stream1"],
               "nx": {"hint_when_not_recording": False}}
        assert nx_record_hint(nx_window, not_connected, off, tmp) is None

        # 4) 前台不是 NX（SW 窗口）→ 不提醒
        assert nx_record_hint(sw_window, not_connected, cfg, tmp) is None

        # 4b) 【P3 回归】exe 优先：浏览器 exe 已知不是 NX → 标题含 NX 也不提醒
        browser = ("NX 2406 - 百度搜索", "chrome.exe")
        assert nx_record_hint(browser, not_connected, cfg, tmp) is None
        assert nx_record_hint(("NX 2512 - 搜索", "msedge.exe"), not_connected,
                              cfg, tmp) is None
        # exe 已知的非 CAD 程序也不能靠标题"救"成 NX（和装聋同一规则）
        assert nx_record_hint(("NX 2512 - Notepad", "notepad.exe"),
                              not_connected, cfg, tmp) is None
        # exe 正好是 NX 主程序（标题里没有 NX）→ 仍然提醒
        assert nx_record_hint(("Designcenter 2512", "ugraf.exe"),
                              not_connected, cfg, tmp) is not None

        # 边界：没启用 nx 后端 / 没启用流1 / status 还没生成 / 标题命中但拿不到 exe
        no_nx = {"stream1": {"enabled": True, "backends": ["solidworks", "creo"]},
                 "nx": {}}
        assert nx_record_hint(nx_window, not_connected, no_nx, tmp) is None
        assert nx_record_hint(nx_window, not_connected,
                              {"stream1": {"enabled": False}}, tmp) is None
        assert nx_record_hint(("NX 2512", None), None, cfg, tmp) is not None
        assert nx_record_hint((None, None), not_connected, cfg, tmp) is None
        assert DEFAULT_CONFIG["nx"]["hint_when_not_recording"] is True
        ok("NX 录制提醒规则正确（exe 优先：浏览器标题含 NX 不误报；"
           "已连接/关配置/非 NX → 静默）")
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


# ======================================================================
# v0.2.2 安全修复回归（2026-10-23 全程序检查：1 阻断 + 6 高危）
# ======================================================================

def test_ai_eye_capture_guard():
    """【阻断】AI 眼隐私红线：按窗口抓画面 + 抓前/抓后双复核，绝不整屏。

    - 抓取前复核不通过（切窗 / 已关闭 / exe-title 与触发时不一致）→ 跳过，
      抓取函数 0 调用；
    - 窗口矩形无效/最小化 → 抓取前就拒绝（抓取函数 0 调用）；
    - 抓取后复核失败（窗口被切走）→ 丢弃图片，不保存/不上传/不回调；
    - 正常 → 只保存窗口尺寸的图（不是整屏）。
    全部用注入的假探测/假抓取器，不碰真实屏幕。
    """
    from PIL import Image
    import ai_eye
    from ai_eye import AiEye, window_capture_size, verify_capture_window

    apps = [{"exe": "ugraf.exe", "title": "NX"}]
    ident = {"hwnd": 4242, "title": "NX 2512", "exe": "ugraf.exe", "ts": 1}
    good = {"alive": True, "foreground": True, "title": "NX 2512", "exe": "ugraf.exe"}
    assert verify_capture_window(ident, apps, state=good) == (True, "")
    # 1) 没拿到 hwnd（旧路径）→ 跳过，绝不"再读一次当前前台窗口"
    ok_hwnd, why = verify_capture_window({"hwnd": 0, "title": "NX"}, apps, state=good)
    assert ok_hwnd is False and "hwnd" in why, (ok_hwnd, why)
    # 2) 目标窗口已关闭 → 跳过
    ok_alive, why = verify_capture_window(ident, apps, state=dict(good, alive=False))
    assert ok_alive is False and "关闭" in why, (ok_alive, why)
    # 3) 前台已切到别的窗口 → 跳过
    ok_fg, why = verify_capture_window(ident, apps, state=dict(good, foreground=False))
    assert ok_fg is False and "前台" in why, (ok_fg, why)
    # 4) 同一个 hwnd，但 exe/title 被换成浏览器 → 身份不一致（先于 CAD 判定）→ 跳过
    bad = dict(good, exe="chrome.exe", title="NX 2406 - 百度搜索")
    ok_cad, why = verify_capture_window(ident, apps, state=bad)
    assert ok_cad is False and "身份" in why, (ok_cad, why)
    # 4b) 身份一致、但不在建模软件名单里 → 不是 CAD 窗口 → 跳过
    other_id = {"hwnd": 5151, "title": "Foo", "exe": "foo.exe"}
    ok_notcad, why = verify_capture_window(
        other_id, apps, state={"alive": True, "foreground": True,
                               "title": "Foo", "exe": "foo.exe"})
    assert ok_notcad is False and "CAD" in why, (ok_notcad, why)
    # 5) 身份一致性：exe 没变、标题变了（换文档/弹对话框）→ 跳过
    ok_title, why = verify_capture_window(
        ident, apps, state=dict(good, title="NX 2512 - 零件1.prt"))
    assert ok_title is False and "身份" in why, (ok_title, why)
    # 6) 身份一致性：标题没变、exe 变了 → 跳过
    ok_exe, why = verify_capture_window(ident, apps, state=dict(good, exe="ugraf2.exe"))
    assert ok_exe is False and "身份" in why, (ok_exe, why)
    # 7) 调试开关只放行"是不是 CAD"，**不放行身份一致性**（隐私底线）
    non_cad_id = {"hwnd": 77, "title": "随便", "exe": "chrome.exe"}
    non_cad_state = {"alive": True, "foreground": True,
                     "title": "随便", "exe": "chrome.exe"}
    assert verify_capture_window(non_cad_id, apps, state=non_cad_state)[0] is False
    assert verify_capture_window(non_cad_id, apps, state=non_cad_state,
                                 allow_all_windows=True) == (True, "")
    ok_allow_identity, why = verify_capture_window(
        ident, apps, state=bad, allow_all_windows=True)
    assert ok_allow_identity is False and "身份" in why, (ok_allow_identity, why)
    # 8) 纯换算：位图尺寸 = 窗口尺寸（不是整屏）；无效矩形 → None
    assert window_capture_size((100, 100, 500, 400)) == (400, 300)
    assert window_capture_size((0, 0, 10, 10)) is None

    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-guard-"))
    seq = [0]

    def new_eye(results, probe, rect_probe=None, grab=None):
        """造一个只依赖注入探测的 AiEye（fake grab/recognize，不碰真实屏幕）。"""
        seq[0] += 1
        calls = {"grab": 0, "grab_args": [], "recognize": 0}
        images = tmp / f"images{seq[0]}"

        def default_grab(hwnd, size):
            calls["grab"] += 1
            calls["grab_args"].append((hwnd, tuple(size)))
            return Image.new("RGB", tuple(size), (10, 20, 30))

        eye = AiEye({"provider": "mock", "delay_s": 0.01, "dedup_window_s": 0,
                     "rate_per_min": 999, "save_images": True,
                     "triggers": {"click_left": True}},
                    on_result=lambda d, t: results.append((d, t)),
                    is_running=lambda: True, images_dir=images,
                    modeling_apps=apps, window_probe=probe,
                    rect_probe=rect_probe, grab_window=grab or default_grab,
                    scale_probe=lambda: 1.0)
        eye._recognize = lambda job, b64: (
            calls.__setitem__("recognize", calls["recognize"] + 1),
            {"ok": True, "answer": {}})[1]
        return eye, calls, images

    def run(eye, ts):
        eye.start()
        eye.trigger("click", {"button": "left"}, ts, (10, 10), window=ident)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            st = eye.status()
            if st["skipped"] + st["ok"] + st["fail"] >= 1:
                break
            time.sleep(0.02)
        st = eye.status()
        eye.stop()
        return st

    try:
        def assert_skipped_without_capture(calls, st, results, images, why):
            assert calls["grab"] == 0, f"{why}：不允许调用窗口抓取"
            assert calls["recognize"] == 0, f"{why}：不允许上传云端"
            assert st["skipped"] == 1 and st["ok"] == 0 and st["fail"] == 0, st
            assert results == [], f"{why}：不允许写识别结果"
            assert list(images.glob("*.jpg")) == [], f"{why}：不允许落图"

        # A) 抓取前切窗 → 跳过（0 次抓取）
        results = []
        eye, calls, images = new_eye(
            results, probe=lambda _id: dict(good, foreground=False))
        st = run(eye, 1700000000000)
        assert_skipped_without_capture(calls, st, results, images, "切窗")

        # B) 身份不一致（同 hwnd，标题变了）→ 跳过（0 次抓取）
        results = []
        eye, calls, images = new_eye(
            results, probe=lambda _id: dict(good, title="NX 2512 - 零件1.prt"))
        st = run(eye, 1700000000001)
        assert_skipped_without_capture(calls, st, results, images, "身份(title)不一致")

        # C) 窗口矩形无效/最小化 → 抓取前拒绝（抓取函数 0 调用）
        results = []
        eye, calls, images = new_eye(
            results, probe=lambda _id: good,
            rect_probe=lambda _hwnd: (100, 100, 40, 400))
        st = run(eye, 1700000000002)
        assert_skipped_without_capture(calls, st, results, images, "无效矩形")
        results = []
        eye, calls, images = new_eye(
            results, probe=lambda _id: good, rect_probe=lambda _hwnd: None)
        st = run(eye, 1700000000003)
        assert_skipped_without_capture(calls, st, results, images, "最小化/无矩形")

        # D) 正常 → 只抓窗口、只存窗口尺寸图（不是整屏），前后各复核一次
        from PIL import Image as _Image
        results = []
        probe_calls = []

        def good_probe(_id):
            probe_calls.append(1)
            return good

        eye, calls, images = new_eye(
            results, probe=good_probe,
            rect_probe=lambda _hwnd: (100, 100, 500, 400))
        st = run(eye, 1700000000100)
        assert calls["grab"] == 1, f"正常路径只应抓取一次: {calls}"
        assert calls["grab_args"] == [(4242, (400, 300))], calls["grab_args"]
        assert len(probe_calls) == 2, "抓取前后必须各复核一次窗口身份"
        assert calls["recognize"] == 1, calls
        assert st["ok"] == 1 and st["skipped"] == 0 and st["fail"] == 0, st
        assert len(results) == 1, results
        detail = results[0][0]
        assert detail["result"]["ok"] is True
        saved = detail["image"]
        assert saved and Path(saved).exists(), saved
        assert Path(saved).parent == images, saved
        with _Image.open(saved) as im:
            assert im.size == (400, 300), (
                f"只保存窗口尺寸图，实际 {im.size}（整屏=桌面尺寸）")
        assert detail["image_rel"].startswith(f"images{seq[0]}/"), detail["image_rel"]

        # E) 抓取后窗口被切走 → 图丢弃：不保存、不上传、不回调
        results = []
        states = [good, dict(good, foreground=False)]   # 第一次=抓前，第二次=抓后

        def switch_probe(_id):
            return states.pop(0) if states else dict(good, foreground=False)

        eye, calls, images = new_eye(
            results, probe=switch_probe,
            rect_probe=lambda _hwnd: (100, 100, 500, 400))
        st = run(eye, 1700000000200)
        assert calls["grab"] == 1, "抓取前复核通过，所以会抓一次（随后丢弃）"
        assert calls["recognize"] == 0, "抓后复核失败不允许上传云端"
        assert st["skipped"] == 1 and st["ok"] == 0 and st["fail"] == 0, st
        assert results == [], "抓后复核失败不允许回调识别结果"
        assert list(images.glob("*.jpg")) == [], "抓后复核失败不允许保存图片"

        # F) 探测/矩形/抓取阶段抛 OSError → 统一按跳过：不计 fail、不回调、不保存
        results = []

        def broken_probe(_id):
            raise OSError("probe 抓前探测失败（测试模拟）")

        eye, calls, images = new_eye(
            results, probe=broken_probe,
            rect_probe=lambda _hwnd: (100, 100, 500, 400))
        st = run(eye, 1700000000300)
        assert_skipped_without_capture(calls, st, results, images, "抓前探测异常")

        # 抓后探测抛 OSError → 已抓的图丢弃，不算 fail、不回调
        results = []
        post_probes = [good]

        def post_broken_probe(_id):
            if post_probes:
                return post_probes.pop(0)
            raise OSError("probe 抓后探测失败（测试模拟）")

        eye, calls, images = new_eye(
            results, probe=post_broken_probe,
            rect_probe=lambda _hwnd: (100, 100, 500, 400))
        st = run(eye, 1700000000301)
        assert calls["grab"] == 1, "抓前复核通过后会抓一次（随后丢弃）"
        assert calls["recognize"] == 0, "抓后探测异常不允许上传云端"
        assert st["skipped"] == 1 and st["ok"] == 0 and st["fail"] == 0, st
        assert results == [], "抓后探测异常不允许回调识别结果"
        assert list(images.glob("*.jpg")) == [], "抓后探测异常不允许保存图片"

        # 矩形探测抛 OSError → 抓取前跳过（抓取函数 0 调用）
        results = []

        def broken_rect(_hwnd):
            raise OSError("矩形探测失败（测试模拟）")

        eye, calls, images = new_eye(
            results, probe=lambda _id: good, rect_probe=broken_rect)
        st = run(eye, 1700000000302)
        assert_skipped_without_capture(calls, st, results, images, "矩形探测异常")

        # 抓取函数抛 OSError → 跳过（不能变成 fail/回调）
        results = []

        def broken_grab(_hwnd, _size):
            raise OSError("窗口抓取失败（测试模拟）")

        eye, calls, images = new_eye(
            results, probe=lambda _id: good,
            rect_probe=lambda _hwnd: (100, 100, 500, 400),
            grab=broken_grab)
        st = run(eye, 1700000000303)
        assert calls["recognize"] == 0, calls
        assert st["skipped"] == 1 and st["ok"] == 0 and st["fail"] == 0, st
        assert results == [], "抓取异常不允许回调识别结果"
        assert list(images.glob("*.jpg")) == [], "抓取异常不允许保存图片"

        # G) 源码契约：不再有整屏截图/裁剪路径，必须用 PrintWindow 按窗口抓
        src = (Path(APP_DIR) / "ai_eye.py").read_text(encoding="utf-8")
        assert "from PIL import ImageGrab" not in src, "不得再整屏截图"
        assert "ImageGrab.grab(" not in src, "不得再整屏 grab"
        assert ".crop(" not in src, "不得再有'整屏 grab 再裁剪'路径"
        assert "PrintWindow" in src, "必须按窗口抓取画面（PrintWindow）"
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    ok("AI 眼按窗口抓取 + 抓前/抓后双复核（切窗/身份不一致/无效矩形→0 次抓取；"
       "抓后切窗→丢弃；探测/矩形/抓取 OSError→跳过不回调；正常→只存窗口尺寸图）")


def test_config_env_key_not_persisted():
    """【高危 2】AI_EYE_KEY：只在运行时生效，绝不落盘、不污染 DEFAULT_CONFIG。"""
    old = os.environ.get("AI_EYE_KEY")
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-envkey-"))
    try:
        os.environ["AI_EYE_KEY"] = "sk-env-secret-123"
        mgr = ConfigManager(tmp)
        cfg = mgr.load()
        assert cfg["ai_eye"]["api_key"] == "sk-env-secret-123", cfg["ai_eye"]
        assert mgr.env_key_active is True
        assert DEFAULT_CONFIG["ai_eye"]["api_key"] == "", \
            "DEFAULT_CONFIG 不能被环境密钥污染（浅复制 bug 回归）"
        # ensure_file / load 写出的 config.json 里不能有环境密钥
        text = (tmp / "config.json").read_text(encoding="utf-8")
        assert "sk-env-secret-123" not in text, "ensure_file 不得写出环境密钥"
        # 设置界面保存（把带环境密钥的运行值传进去）→ 文件里仍不能有它
        cfg["idle_minutes"] = 3.5
        mgr.save(cfg)
        text = (tmp / "config.json").read_text(encoding="utf-8")
        assert "sk-env-secret-123" not in text, "设置保存不得写回环境密钥"
        saved = json.loads(text)
        assert saved["ai_eye"]["api_key"] == ""
        assert saved["idle_minutes"] == 3.5
        # 深拷贝：改返回值不影响默认配置（嵌套列表 modeling_apps 也不能共享引用）
        cfg["ai_eye"]["api_key"] = "changed"
        assert DEFAULT_CONFIG["ai_eye"]["api_key"] == ""
        merged = ConfigManager._merge(DEFAULT_CONFIG, {"modeling_apps": []})
        assert merged["modeling_apps"] == []
        assert DEFAULT_CONFIG["modeling_apps"], "合并不能把默认名单清空"

        # 文件里原本有用户 key：环境变量生效时保存要保留文件原值（不写环境密钥）
        tmp2 = Path(tempfile.mkdtemp(prefix="cad-ai-envkey2-"))
        try:
            (tmp2 / "config.json").write_text(json.dumps(
                {"ai_eye": {"api_key": "file-key-999"}}), encoding="utf-8")
            mgr2 = ConfigManager(tmp2)
            cfg2 = mgr2.load()
            assert cfg2["ai_eye"]["api_key"] == "sk-env-secret-123"
            assert mgr2.file_api_key == "file-key-999"
            mgr2.save(cfg2)
            saved2 = json.loads((tmp2 / "config.json").read_text(encoding="utf-8"))
            assert saved2["ai_eye"]["api_key"] == "file-key-999", saved2["ai_eye"]
        finally:
            shutil.rmtree(str(tmp2), ignore_errors=True)

        # 取消环境变量后：正常从文件读，保存界面值也正常写
        os.environ.pop("AI_EYE_KEY", None)
        mgr3 = ConfigManager(tmp)
        cfg3 = mgr3.load()
        assert mgr3.env_key_active is False
        cfg3["ai_eye"]["api_key"] = "user-typed-key"
        mgr3.save(cfg3)
        assert json.loads((tmp / "config.json").read_text(
            encoding="utf-8"))["ai_eye"]["api_key"] == "user-typed-key"
    finally:
        if old is None:
            os.environ.pop("AI_EYE_KEY", None)
        else:
            os.environ["AI_EYE_KEY"] = old
        shutil.rmtree(str(tmp), ignore_errors=True)
    ok("AI_EYE_KEY 只用于运行时（load 可见 / 不污染默认值 / 文件与设置保存都不落盘）")


def test_nx_journal_dir_same_source():
    """【高危 3】NX 日志目录同源：桥默认目录 == 提醒 journal_dir == <data_dir>\\journals。"""
    from collector_core import nx_journal_dir
    from nx_bridge import NxBridge
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-nxdir-"))
    try:
        data_dir = tmp / "my_data"      # 故意不是仓库 data\（模拟打包/自定义数据目录）
        (data_dir / "journals").mkdir(parents=True)
        expected = data_dir / "journals"
        assert nx_journal_dir(data_dir) == expected
        # 桥默认目录：显式传 data_dir → 和界面按钮同一个
        bridge = NxBridge({"data_dir": str(data_dir)},
                          lambda *a: None, lambda: False)
        assert bridge.data_dir == str(data_dir)
        assert str(expected) in bridge._dirs(), bridge._dirs()
        assert Path(bridge.status()["journal_dir"]) == expected, bridge.status()
        # 提醒里的 journal_dir 与按钮/桥同源
        hint = nx_record_hint(
            ("Designcenter NX 2512", "ugraf.exe"),
            [{"backend": "nx", "connected": False}],
            {"stream1": {"enabled": True, "backends": ["nx"]},
             "nx": {"hint_when_not_recording": True}},
            data_dir)
        assert hint and Path(hint["journal_dir"]) == expected, hint
        # 没配显式 journal_dirs 时，桥的最后兜底候选就是 <data_dir>\journals
        # （以前是源码仓库 data\journals → 打包后和按钮指向两个文件夹）
        src = (Path(APP_DIR) / "input_listener.py").read_text(encoding="utf-8")
        assert 'nx_cfg["data_dir"] = str(self.data_dir)' in src, \
            "input_listener 创建 NxBridge 时必须传实际数据根目录"
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    ok("NX 日志目录同源（桥默认 / 按钮 / 提醒 = <data_dir>\\journals）")


def test_nx_bridge_data_dir_wired():
    """【高危 3】会话启动时把**实际数据根目录**传给 NX 桥（真实接线验证）。"""
    tmp = tempfile.mkdtemp(prefix="cad-ai-nxwire-")
    (Path(tmp) / "journals").mkdir()
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "version": 2,
            "idle_minutes": 10.0,
            "modeling_apps": TEST_ONLY_APPS,
            "stream1": {"enabled": True, "backends": ["nx"]},
            "nx": {"poll_interval_s": 0.2},
        }, f, ensure_ascii=False, indent=2)
    session = CollectorSession(tmp)
    session.start()
    try:
        assert session.stream1s, "应启动 NX 桥"
        bridge = session.stream1s[0]
        assert bridge.cfg.get("data_dir") == str(Path(tmp)), bridge.cfg
        expected = Path(tmp) / "journals"
        assert str(expected) in bridge._dirs(), bridge._dirs()
        assert str(session.data_dir / "journals") == str(expected)
    finally:
        session.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    ok("会话把实际数据根目录传给 NX 桥（桥默认目录 == 会话数据根\\journals）")


def test_write_failure_keeps_session():
    """【高危 4】写库失败被吞：error 非空、内容计数不加、会话保留、status=error。"""
    tmp = tempfile.mkdtemp(prefix="cad-ai-wfail-")
    session = _mk_session_v4(tmp)      # 测试专用假建模名单 + 流1 关闭
    session.start()
    try:
        def boom(*args, **kwargs):
            raise OSError("磁盘写满（测试模拟）")

        assert session._content_events == 0
        session.store.add_event = boom      # 让本会话之后的事件全写失败
        session._record("stream2", "click", {"button": "left", "action": "press",
                                             "x": 1, "y": 2})
        session._record("stream2", "key_down", {"char": "a"})
        assert session.error and "写库失败" in session.error, session.error
        assert session._content_events == 0, "写失败不能算内容事件（否则空会话删不掉/计数错位）"
        assert session.stats()["write_failed"] is True
    finally:
        summary = session.stop()
    assert summary is not None
    assert summary["counts"]["stream2"] == 0, summary["counts"]
    assert summary["status"] == "error", summary
    assert summary["error"] and "写库失败" in summary["error"], summary
    assert summary["kept"] is True, "写库失败绝不能当空会话删掉"
    session_dir = Path(summary["session_dir"])
    assert session_dir.is_dir(), "写库失败的会话文件夹必须保留"
    meta = read_session_json(session_dir)
    assert meta and meta["status"] == "error", meta
    assert meta.get("error"), meta
    shutil.rmtree(tmp, ignore_errors=True)
    ok("写库失败被正确上报（error / 会话保留 / session.json status=error）")


def test_sw_com_disconnect_reconnect():
    """【高危 5】SolidWorks COM 断线：区分"没文档"与"COM 异常"，断开重连不误报删除。"""
    from solidworks_bridge import (ComSnapshotError, SolidWorksBridge,
                                   _get_com_member, snapshot_sw)

    class _FakeFeature:
        def __init__(self, name, ftype="Boss", sup=0):
            self.Name = name
            self.GetTypeName2 = ftype
            self.GetSuppression2 = sup

        def GetFirstSubFeature(self):
            return None

        def GetNextFeature(self):
            return None

    class _FakeModel:
        def __init__(self, owner):
            self._owner = owner

        def GetTitle(self):
            return "Part1"

        def GetType(self):
            return 1

        @property
        def SelectionManager(self):
            return None

        @property
        def FirstFeature(self):
            return self._owner.first_feature

    class _FakeSw:
        def __init__(self):
            self.fail = False
            self.fail_reason = "RPC server unavailable（测试模拟 COM 断线）"
            self.first_feature = _FakeFeature("Boss-Extrude1")
            self.doc = _FakeModel(self)

        @property
        def ActiveDoc(self):
            if self.fail:
                raise RuntimeError(self.fail_reason)
            return self.doc

    fake = _FakeSw()
    # 1) 正常抓拍
    snap = snapshot_sw(fake, strict=True)
    assert snap["doc"] == {"title": "Part1", "type": 1}, snap
    assert [f["path"] for f in snap["features"]] == ["Boss-Extrude1"], snap
    # 2) "没有活动文档"是正常状态：doc=None，不抛异常
    fake.doc = None
    soft_no_doc = snapshot_sw(fake, strict=True)
    assert soft_no_doc["doc"] is None and soft_no_doc["features"] == []
    fake.doc = _FakeModel(fake)
    # 3) COM 异常：strict 必须抛（不能假装"没文档"）；非 strict 保持旧行为
    fake.fail = True
    try:
        snapshot_sw(fake, strict=True)
    except ComSnapshotError as exc:
        assert "ActiveDoc" in str(exc), exc
    else:
        raise AssertionError("COM 异常必须抛 ComSnapshotError，不能吞成空快照")
    soft = snapshot_sw(fake, strict=False)
    assert soft["doc"] is None and soft["features"] == []
    fake.fail = False

    # 4) 桥：断线 → connected False + 释放 COM 对象；恢复 → 重连；全程无假删除事件
    events = []
    bridge = SolidWorksBridge({"poll_interval_s": 0.01},
                              lambda kind, detail: events.append((kind, detail)),
                              lambda: True)
    connect_calls = {"n": 0}

    def fake_connect(_client):
        connect_calls["n"] += 1
        return fake

    bridge._connect = fake_connect      # 不碰真实 COM
    sw = bridge._tick(None, object())   # 首轮：连上 + 抓基准，不产生事件
    assert sw is fake and bridge.status()["connected"] is True, bridge.status()
    assert events == [], events
    sw = bridge._tick(sw, object())     # 正常轮询：没变化 → 无事件
    assert sw is fake and events == [], events
    fake.fail = True                    # COM 断线
    sw = bridge._tick(sw, object())
    st = bridge.status()
    assert sw is None, "断线必须释放 COM 对象（sw=None），下一轮重连"
    assert st["connected"] is False and st["retrying"] is True, st
    assert "COM" in (st.get("last_error") or ""), st
    assert events == [], f"断线不能产生假的文档/特征删除事件: {events}"
    fake.fail = False                   # SW 恢复
    sw = bridge._tick(None, object())   # 重新 GetActiveObject + 重抓基准
    assert sw is fake and bridge.status()["connected"] is True, bridge.status()
    assert connect_calls["n"] == 2, connect_calls
    sw = bridge._tick(sw, object())
    assert events == [], f"重连后第一轮只抓基准，不能误报变化: {events}"
    fake.first_feature = None           # 恢复后真有变化 → 照常产出事件
    bridge._tick(sw, object())
    assert "featureRemoved" in [k for k, _ in events], events

    # 5) 【第三轮阻断回归】选中对象没有 Name 成员（pywin32 抛 AttributeError）
    #    属于正常缺成员 → 快照继续有效（name=None），不能当 COM 断线。
    class _SelectedNoName:
        @property
        def Name(self):
            raise AttributeError("IFace2.Name")

    class _SelectedUnknownName:
        @property
        def Name(self):
            # pywin32 另一种"没这个成员"：DISP_E_UNKNOWNNAME
            raise RuntimeError("(-2147352570, 'Unknown name.', None, None)")

    class _SelectedRpcBroken:
        @property
        def Name(self):
            raise RuntimeError("RPC server unavailable（测试模拟调用故障）")

    class _SelManager:
        def __init__(self, owner):
            self._owner = owner

        def GetSelectedObjectCount2(self, _flag):
            return 1

        def GetSelectedObject6(self, _i, _flag):
            return self._owner.selected

        def GetSelectedObjectType3(self, _i, _flag):
            return 3

    class _SelModel:
        def __init__(self, owner):
            self._owner = owner

        def GetTitle(self):
            return "Part1"

        def GetType(self):
            return 1

        @property
        def SelectionManager(self):
            return _SelManager(self._owner)

        @property
        def FirstFeature(self):
            return None

    class _SelSw:
        def __init__(self):
            self.selected = _SelectedNoName()
            self.doc = _SelModel(self)

        @property
        def ActiveDoc(self):
            return self.doc

    sel_fake = _SelSw()
    sel_snap = snapshot_sw(sel_fake, strict=True)
    assert sel_snap["doc"] == {"title": "Part1", "type": 1}, sel_snap
    assert sel_snap["selection"] == [{"type": 3, "name": None}], sel_snap
    # DISP_E_UNKNOWNNAME 类"没这个成员"同样算正常缺失
    sel_fake.selected = _SelectedUnknownName()
    assert snapshot_sw(sel_fake, strict=True)["selection"] == [
        {"type": 3, "name": None}], "DISP_E_UNKNOWNNAME 应视为可选成员缺失"

    # 可选成员读取遇到**真正的**调用故障（不是"没这个成员"）→ 仍按断线
    sel_fake.selected = _SelectedRpcBroken()
    try:
        snapshot_sw(sel_fake, strict=True)
    except ComSnapshotError as exc:
        assert "Name" in str(exc), exc
    else:
        raise AssertionError(
            "可选成员读取遇到真 COM 故障（RPC 不可达）必须抛 ComSnapshotError")

    # 必选成员缺失仍按断线处理：可选成员的 AttributeError 豁免不能扩散到必选成员
    class _RequiredMemberMissing:
        @property
        def ActiveDoc(self):
            raise AttributeError("ISldWorks.ActiveDoc")

    try:
        snapshot_sw(_RequiredMemberMissing(), strict=True)
    except ComSnapshotError as exc:
        assert "ActiveDoc" in str(exc), exc
    else:
        raise AssertionError("必选成员缺失必须按断线处理（不能套用可选成员豁免）")

    # 桥：正常基准（Name 缺失）→ 真故障断线 → 恢复后重连重建基准，无假事件
    sel_events = []
    sel_bridge = SolidWorksBridge({"poll_interval_s": 0.01},
                                  lambda kind, detail: sel_events.append((kind, detail)),
                                  lambda: True)
    sel_bridge._connect = lambda _client: sel_fake
    sel_fake.selected = _SelectedNoName()
    sw2 = sel_bridge._tick(None, object())
    st = sel_bridge.status()
    assert sw2 is sel_fake and st["connected"] is True, st
    sel_fake.selected = _SelectedRpcBroken()
    sw2 = sel_bridge._tick(sw2, object())
    st = sel_bridge.status()
    assert sw2 is None and st["connected"] is False, st
    assert sel_events == [], f"真 COM 故障不能产生假事件: {sel_events}"
    sel_fake.selected = _SelectedNoName()
    sw2 = sel_bridge._tick(None, object())   # 下轮重连 + 重建基准
    st = sel_bridge.status()
    assert sw2 is sel_fake and st["connected"] is True and st["retrying"] is False, st
    sel_bridge._tick(sw2, object())
    assert sel_events == [], f"重连后第一轮只抓基准，不能误报变化: {sel_events}"

    # 6) 【第四轮阻断回归】必选成员 ActiveDoc 抛 DISP_E_MEMBERNOTFOUND
    #    （"Member not found" / -2147352573）→ strict 必须断线；不能吞成
    #    doc=None（否则基准正常后会产出假 documentActivated(cur=None)）。
    fake.fail_reason = ("(-2147352573, 'Member not found.', None, None)"
                        "（测试模拟 DISP_E_MEMBERNOTFOUND）")
    fake.fail = True
    try:
        snapshot_sw(fake, strict=True)
    except ComSnapshotError as exc:
        assert "ActiveDoc" in str(exc), exc
    else:
        raise AssertionError(
            "必选成员 Member not found 必须抛 ComSnapshotError，不能吞成空快照")
    soft_member_missing = snapshot_sw(fake, strict=False)   # 宽松模式保持旧行为
    assert soft_member_missing["doc"] is None, soft_member_missing

    mm_events = []
    mm_bridge = SolidWorksBridge({"poll_interval_s": 0.01},
                                 lambda kind, detail: mm_events.append((kind, detail)),
                                 lambda: True)
    mm_bridge._connect = lambda _client: fake
    fake.fail = False                       # 先建一个正常基准
    fake.fail_reason = "RPC server unavailable（测试模拟 COM 断线）"
    sw3 = mm_bridge._tick(None, object())
    st = mm_bridge.status()
    assert sw3 is fake and st["connected"] is True, st
    assert mm_events == [], mm_events
    fake.fail_reason = ("(-2147352573, 'Member not found.', None, None)"
                        "（测试模拟 DISP_E_MEMBERNOTFOUND）")
    fake.fail = True
    sw3 = mm_bridge._tick(sw3, object())
    st = mm_bridge.status()
    assert sw3 is None, "必选成员缺失必须释放实例（sw=None）"
    assert st["connected"] is False and st["retrying"] is True, st
    assert mm_events == [], f"不能产生假 documentActivated/feature 事件: {mm_events}"
    fake.fail = False                       # 恢复 → 重连 + 重建基准，无假事件
    sw3 = mm_bridge._tick(None, object())
    st = mm_bridge.status()
    assert sw3 is fake and st["connected"] is True and st["retrying"] is False, st
    mm_bridge._tick(sw3, object())
    assert mm_events == [], f"重连后第一轮只抓基准，不能误报变化: {mm_events}"
    # 调用路径的"伪可调用属性"语义保持不变：getattr 拿到的是 pywin32 的
    # COM dispatch 对象、调用时报 member-not-found → 直接把属性值交出去。
    # （只对真 COM 对象放行；普通方法失败必须断线，见下一条断言。）
    class _PseudoCallable:
        _oleobj_ = object()   # 模拟 pywin32 dispatch 对象的内部标记

        def __call__(self):
            raise RuntimeError("Member not found（伪可调用属性，测试模拟）")

    class _PseudoHost:
        Pseudo = _PseudoCallable()

    pseudo_host = _PseudoHost()
    got_pseudo = _get_com_member(pseudo_host, "Pseudo", strict=True)
    assert got_pseudo is pseudo_host.Pseudo, (
        "调用路径的伪可调用属性语义不能变（member-not-found 时返回属性本身）")

    # 必选"方法"调用失败不能被伪可调用豁免吞掉：普通方法调用报
    # Member not found 必须抛 ComSnapshotError（否则断线被吞、产生假事件）。
    class _MethodHost:
        def GetTitle(self):
            raise RuntimeError("(-2147352573, 'Member not found.', None, None)")

    try:
        _get_com_member(_MethodHost(), "GetTitle", strict=True)
    except ComSnapshotError:
        pass
    else:
        raise AssertionError("必选方法 Member not found 必须抛 ComSnapshotError")

    ok("SW COM 断线正确重连（没文档≠断线；断线释放对象；不产生假删除事件；"
       "选中对象缺 Name 不算断线、真 RPC 故障仍断线；必选成员 Member not found 仍断线；"
       "伪可调用属性语义不变）")


def test_sw_feature_traversal_error_disconnects():
    """【P1 回归】特征遍历中途 COM 异常 → 整次快照作废 + 断线重连，无假事件。

    以前 GetNextFeature/子特征遍历的异常被吞成"特征没了"：connected 仍 True、
    实例不释放，还会拿**不完整特征树**差分出假的 featureRemoved/featureAdded。
    现在 strict 快照里任何一次 COM 读取失败都抛 ComSnapshotError，桥按断线处理。
    """
    from solidworks_bridge import (ComSnapshotError, SolidWorksBridge,
                                   snapshot_sw)

    class _FakeFeature:
        def __init__(self, name, next_feature=None):
            self.Name = name
            self.GetTypeName2 = "Boss"
            self.GetSuppression2 = 0
            self.next_feature = next_feature
            self.fail_next = False

        def GetFirstSubFeature(self):
            return None

        def GetNextFeature(self):
            if self.fail_next:
                raise RuntimeError(
                    "RPC server unavailable（测试模拟 GetNextFeature 中断线）")
            return self.next_feature

    class _FakeModel:
        def __init__(self, owner):
            self._owner = owner

        def GetTitle(self):
            return "Part1"

        def GetType(self):
            return 1

        @property
        def SelectionManager(self):
            return None

        @property
        def FirstFeature(self):
            return self._owner.first_feature

    class _FakeSw:
        def __init__(self):
            self.first_feature = None
            self.doc = _FakeModel(self)

        @property
        def ActiveDoc(self):
            return self.doc

    feat_b = _FakeFeature("Cut-Extrude1")
    feat_a = _FakeFeature("Boss-Extrude1", feat_b)
    fake = _FakeSw()
    fake.first_feature = feat_a

    # 1) 正常：整棵特征树都能读出来
    snap = snapshot_sw(fake, strict=True)
    assert [f["path"] for f in snap["features"]] == ["Boss-Extrude1", "Cut-Extrude1"], snap
    # 2) 第二棵特征 GetNextFeature 抛异常 → strict 必须抛 ComSnapshotError
    feat_b.fail_next = True
    try:
        snapshot_sw(fake, strict=True)
    except ComSnapshotError as exc:
        assert "GetNextFeature" in str(exc), exc
    else:
        raise AssertionError("特征遍历 COM 异常必须抛 ComSnapshotError，"
                             "不能吞成不完整快照")
    # 非 strict（联调脚本用）保持宽松：不抛，只拿读到的部分
    soft = snapshot_sw(fake, strict=False)
    assert [f["path"] for f in soft["features"]] == ["Boss-Extrude1",
                                                     "Cut-Extrude1"], soft
    feat_b.fail_next = False

    # 3) 桥：连上并抓基准 → 特征遍历中途异常 → connected=False、释放实例、无假事件
    events = []
    bridge = SolidWorksBridge({"poll_interval_s": 0.01},
                              lambda kind, detail: events.append((kind, detail)),
                              lambda: True)
    connect_calls = {"n": 0}

    def fake_connect(_client):
        connect_calls["n"] += 1
        return fake

    bridge._connect = fake_connect
    sw = bridge._tick(None, object())        # 首轮：连上 + 抓基准
    assert sw is fake and bridge.status()["connected"] is True, bridge.status()
    assert events == [], events
    feat_b.fail_next = True                  # 遍历到第二棵特征时 COM 异常
    sw = bridge._tick(sw, object())
    st = bridge.status()
    assert sw is None, "特征遍历 COM 异常必须释放实例（sw=None）"
    assert st["connected"] is False and st["retrying"] is True, st
    assert "COM" in (st.get("last_error") or ""), st
    assert events == [], f"残缺特征树绝不能差分出假事件: {events}"
    # 4) SW 恢复 → 下轮重连并重建基准，不误报
    feat_b.fail_next = False
    sw = bridge._tick(None, object())
    st = bridge.status()
    assert sw is fake and st["connected"] is True and st["retrying"] is False, st
    assert connect_calls["n"] == 2, connect_calls
    sw = bridge._tick(sw, object())
    assert events == [], f"重连后第一轮只抓基准，不能误报变化: {events}"
    # 5) 恢复后真有变化 → 照常产出事件（说明重连后的差分是正常工作的）
    fake.first_feature = None
    bridge._tick(sw, object())
    assert "featureRemoved" in [k for k, _ in events], events
    ok("SW 特征遍历中途 COM 异常 → 快照作废/断线重连（connected=False、无假事件、恢复后重建基准）")


def test_integration_scripts_only_close_owned():
    """【高危 6 + 第四轮回归】真机联调脚本只关自己启动的实例。

    行为验证：
    - `selftest_nx.close_started`：只按 /PID 结束，绝不按镜像名全杀；
    - `selftest_solidworks` / `selftest_production`：假 COM + 假 subprocess →
      用户实例不被 ExitApp；自己 Popen 的 PID 被 taskkill /PID /T /F；
      生产脚本 connect_sw 确实返回自己 Popen 的进程对象；
    - 源码契约：四个 SW 联调/探测脚本 AST 级均无 `.ExitApp(` 调用。
    （几个脚本已改为惰性导入 COM/安装发现，CI 也能 import。）
    """
    import selftest_nx
    calls = []
    real_run = selftest_nx.subprocess.run

    class _FakeProc:
        pid = 424242

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return None

    try:
        # 模块对象是共享的：先换成假 run，用完必须还原
        selftest_nx.subprocess.run = fake_run
        assert selftest_nx.close_started(None) is False, "没启动过就什么都不该做"
        assert calls == [], calls
        assert selftest_nx.close_started(_FakeProc()) is True
    finally:
        selftest_nx.subprocess.run = real_run
    assert calls and calls[0][0] == "taskkill", calls
    assert "/PID" in calls[0] and "424242" in calls[0], calls
    assert "/IM" not in calls[0], f"不能按镜像名全杀: {calls[0]}"

    for name in ("selftest_nx.py", "selftest_creo.py", "selftest_solidworks.py",
                 "selftest_production.py", "probe_sw_shortcuts.py",
                 "probe_sw_shortcuts2.py"):
        src = (Path(APP_DIR) / name).read_text(encoding="utf-8")
        for line in src.splitlines():
            assert not ("taskkill" in line and "/IM" in line), \
                f"{name}: 禁止按镜像名全杀: {line.strip()}"
        assert "close_started(" in src, f"{name} 应使用 close_started 只关自己启动的进程"
        assert '["taskkill", "/PID"' in src, f"{name} 必须按 PID 结束进程"

    # ---- SolidWorks 联调脚本：行为测试（假 COM + 假 subprocess） ----
    # 关键回归：launched=True 不能证明 COM 实例归属 → 绝不能 ExitApp 用户实例；
    # 只关自己 Popen 的 PID（taskkill /PID <pid> /T /F）。
    import ast
    import contextlib
    import io
    import selftest_solidworks as sw_live   # 已改为惰性导入 COM，CI 也能 import
    sw_calls = []
    real_sw_run = sw_live.subprocess.run

    class _FakeSwProc:
        pid = 424243

    class _FakeComInstance:
        def __init__(self):
            self.exit_calls = 0

        def ExitApp(self):
            self.exit_calls += 1
            raise AssertionError("绝不能调用 COM ExitApp 关闭实例（可能关掉用户实例）")

    def fake_sw_run(args, **kwargs):
        sw_calls.append(list(args))
        return None

    fake_com = _FakeComInstance()
    try:
        sw_live.subprocess.run = fake_sw_run
        with contextlib.redirect_stdout(io.StringIO()):   # 脚本的提示文字不混进自检输出
            # 1) 用户原本就开着的实例（没有自己 Popen 的 PID）→ 什么都不动
            assert sw_live.cleanup_after_test(fake_com, None) is False
            assert sw_calls == [], f"不属于自己启动的实例不能杀: {sw_calls}"
            assert fake_com.exit_calls == 0
            # 2) 自己 Popen 的进程 → 只按 PID 关，绝不碰 COM 实例
            assert sw_live.cleanup_after_test(fake_com, _FakeSwProc()) is True
            assert fake_com.exit_calls == 0, "COM 实例（可能是用户的）绝不能被 ExitApp"
    finally:
        sw_live.subprocess.run = real_sw_run
    assert sw_calls, "自己 Popen 的 PID 应该被 taskkill"
    assert sw_calls[0][0] == "taskkill", sw_calls
    assert "/PID" in sw_calls[0] and "424243" in sw_calls[0], sw_calls
    assert "/T" in sw_calls[0] and "/F" in sw_calls[0], sw_calls
    assert "/IM" not in sw_calls[0], f"不能按镜像名全杀: {sw_calls[0]}"

    # ---- 生产联调脚本（selftest_production.py）：Popen 记 PID + 只按 PID 关 ----
    import selftest_production as prod_live   # 已改为惰性导入 COM，CI 也能 import
    prod_calls = []
    real_prod_run = prod_live.subprocess.run
    real_prod_popen = prod_live.subprocess.Popen
    real_prod_sleep = prod_live.time.sleep
    real_prod_win32com = prod_live._win32com
    real_prod_prog_ids = prod_live._prog_ids
    real_prod_sw_exe = prod_live._sw_exe

    class _FakeProdSw:
        pass

    fake_prod_sw = _FakeProdSw()

    class _FakeProdComClient:
        """第一次 GetActiveObject 失败（SW 没开）→ 脚本 Popen；第二次成功。"""

        def __init__(self):
            self.get_calls = 0

        def GetActiveObject(self, _pid):
            self.get_calls += 1
            if self.get_calls == 1:
                raise RuntimeError("SolidWorks 未运行（测试模拟）")
            return fake_prod_sw

    class _FakeProdProc:
        pid = 424244

    popen_args = []

    def fake_popen(args, **kwargs):
        popen_args.append(list(args))
        return _FakeProdProc()

    def fake_prod_run(args, **kwargs):
        prod_calls.append(list(args))
        return None

    try:
        prod_live.subprocess.run = fake_prod_run
        prod_live.subprocess.Popen = fake_popen
        prod_live.time.sleep = lambda _s: None
        prod_live._win32com = lambda: _FakeProdComClient()
        prod_live._prog_ids = lambda: ["SldWorks.Application"]
        prod_live._sw_exe = lambda: sys.executable   # 存在的文件，不需要真装 SW
        with contextlib.redirect_stdout(io.StringIO()):
            got_sw, got_proc = prod_live.connect_sw()
    finally:
        prod_live.subprocess.run = real_prod_run
        prod_live.subprocess.Popen = real_prod_popen
        prod_live.time.sleep = real_prod_sleep
        prod_live._win32com = real_prod_win32com
        prod_live._prog_ids = real_prod_prog_ids
        prod_live._sw_exe = real_prod_sw_exe
    assert got_sw is fake_prod_sw, got_sw
    assert got_proc is not None and got_proc.pid == 424244, got_proc
    assert popen_args == [[sys.executable]], popen_args

    class _FakeProdCom:
        def __init__(self):
            self.exit_calls = 0

        def ExitApp(self):
            self.exit_calls += 1
            raise AssertionError("绝不能调用 COM ExitApp 关闭实例")

    fake_prod_com = _FakeProdCom()
    try:
        prod_live.subprocess.run = fake_prod_run
        with contextlib.redirect_stdout(io.StringIO()):
            assert prod_live.cleanup_after_test(fake_prod_com, None) is False
            assert prod_calls == [], f"用户实例不能杀: {prod_calls}"
            assert fake_prod_com.exit_calls == 0
            assert prod_live.cleanup_after_test(fake_prod_com, got_proc) is True
            assert fake_prod_com.exit_calls == 0, "COM 实例（可能是用户的）绝不能被 ExitApp"
    finally:
        prod_live.subprocess.run = real_prod_run
    assert prod_calls and prod_calls[0][0] == "taskkill", prod_calls
    assert "/PID" in prod_calls[0] and "424244" in prod_calls[0], prod_calls
    assert "/T" in prod_calls[0] and "/F" in prod_calls[0], prod_calls
    assert "/IM" not in prod_calls[0], f"不能按镜像名全杀: {prod_calls[0]}"

    # 源码契约（AST 级）：这四个脚本一个 ExitApp 调用都不许有
    # （注释/docstring 里解释"为什么不用"不算调用）。
    for name in ("selftest_solidworks.py", "selftest_production.py",
                 "probe_sw_shortcuts.py", "probe_sw_shortcuts2.py"):
        src = (Path(APP_DIR) / name).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "ExitApp":
                raise AssertionError(
                    f"{name}:{node.lineno} 仍在调用 COM ExitApp；"
                    "只允许 taskkill /PID 关自己 Popen 的进程")
        assert "cleanup_after_test(" in src, f"{name} 应使用 cleanup_after_test 收尾"
    ok("联调脚本只关自己启动的实例（只 taskkill /PID；四个 SW 脚本均无 ExitApp 调用；"
       "生产脚本 connect_sw 返回自己 Popen 的 PID）")


# ======================================================================
# v0.2.3（2026-10-27）：NX 2406 实测准备
# 补读开始采集前已录的 journal / 选 journal 写配置 / 实验性自动录制脚本
# ======================================================================

def _nx_journal_seed_lines():
    """造一份"点开始采集前已经录好"的 journal：1 行文件头 + 3 条事件。"""
    return ["// NX journal",
            "// Menu: Insert->Sketch",
            "NXOpen.Session.GetSession()",
            "// Menu: Insert->Design Feature->Extrude..."]


def test_nx_replay_existing_on():
    """v0.2.3：`nx.replay_existing=true` → 挂上时把已录的 3 条事件按行序补读一次。"""
    from nx_bridge import NxBridge
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-nxreplay-"))
    journal = tmp / "j.cs"
    journal.write_text("\n".join(_nx_journal_seed_lines()) + "\n", encoding="utf-8")
    events = []
    try:
        bridge = NxBridge({"journal_path": str(journal), "replay_existing": True},
                          lambda kind, detail: events.append((kind, detail)),
                          lambda: True)
        assert bridge.replay_existing is True
        # 直接驱动一次轮询（= 挂上），不启动线程：测试不需要 sleep，完全确定性
        bridge._poll_once()
        assert any(k == "nx_session" for k, _ in events), events
        replayed = [d for k, d in events if k == "nx_journal"]
        assert len(replayed) == 3, f"应补读 3 条，实际 {len(replayed)}: {replayed}"
        assert [d["type"] for d in replayed] == ["menu", "api", "menu"], replayed
        assert [d["line_no"] for d in replayed] == [2, 3, 4], replayed
        assert replayed[0]["menu"] == "Insert->Sketch"
        assert replayed[-1]["menu"] == "Insert->Design Feature->Extrude..."
        assert all(d["journal_file"] == "j.cs" for d in replayed)
        assert bridge._last_line_no == 4
        # 下一轮没有新行：绝不能把旧行再补一遍（只补一次）
        bridge._poll_once()
        assert len([d for k, d in events if k == "nx_journal"]) == 3, events
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    ok("NX 补读正确（replay_existing=true：挂上即补回已有 3 条 menu/API，且只补一次）")


def test_nx_replay_existing_off():
    """v0.2.3：`nx.replay_existing=false`（默认）保持旧行为：已有内容不补读。"""
    from nx_bridge import NxBridge
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-nxreplayoff-"))
    journal = tmp / "j.cs"
    journal.write_text("\n".join(_nx_journal_seed_lines()) + "\n", encoding="utf-8")
    events = []
    try:
        # 故意不传 replay_existing → 默认值必须和 DEFAULT_CONFIG 一致（false）
        bridge = NxBridge({"journal_path": str(journal)},
                          lambda kind, detail: events.append((kind, detail)),
                          lambda: True)
        assert bridge.replay_existing is False
        assert DEFAULT_CONFIG["nx"]["replay_existing"] is False
        # config.example.json 也必须同步（用户照它改配置）
        example = json.loads((Path(__file__).resolve().parent.parent
                              / "config.example.json").read_text(encoding="utf-8"))
        assert example["nx"]["replay_existing"] is False, example["nx"]
        bridge._poll_once()
        assert any(k == "nx_session" for k, _ in events), events
        assert [d for k, d in events if k == "nx_journal"] == [], events
        assert bridge._last_line_no == 4, "旧行为也要记住当前行数（之后只发新增）"
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    ok("NX 补读默认关闭（replay_existing=false：挂上不重放旧内容，与旧版行为一致）")


def test_nx_replay_append_only_new():
    """v0.2.3：补读之后再追加 → 每轮只发新增 1 条，不重复补读旧行。"""
    from nx_bridge import NxBridge
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-nxappend-"))
    journal = tmp / "j.cs"
    journal.write_text("\n".join(_nx_journal_seed_lines()) + "\n", encoding="utf-8")
    events = []
    try:
        bridge = NxBridge({"journal_path": str(journal), "replay_existing": True},
                          lambda kind, detail: events.append((kind, detail)),
                          lambda: True)
        bridge._poll_once()
        assert len([d for k, d in events if k == "nx_journal"]) == 3
        with open(str(journal), "a", encoding="utf-8") as f:
            f.write("// Menu: File->Save\n")
        bridge._poll_once()
        got = [d for k, d in events if k == "nx_journal"]
        assert len(got) == 4, f"补读 3 条 + 新增 1 条，实际 {len(got)}: {got}"
        assert got[-1]["menu"] == "File->Save"
        assert got[-1]["line_no"] == 5, got[-1]
        assert bridge._last_line_no == 5
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    ok("NX 补读后追加正确（只发新增 1 条，不重复补读已有内容）")


def test_nx_replay_truncate_reattach():
    """v0.2.3：journal 被截断重挂 → 不重复发旧行；之后追加只发新的。"""
    from nx_bridge import NxBridge
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-nxtrunc-"))
    journal = tmp / "j.cs"
    journal.write_text("\n".join(_nx_journal_seed_lines()) + "\n", encoding="utf-8")
    events = []
    try:
        bridge = NxBridge({"journal_path": str(journal), "replay_existing": True},
                          lambda kind, detail: events.append((kind, detail)),
                          lambda: True)
        bridge._poll_once()
        assert len([d for k, d in events if k == "nx_journal"]) == 3
        # 文件被截断/重写成只剩文件头（0 条事件）：重挂时按"当前文件内容"补读，
        # 已经发过的旧行不会再发一遍。
        journal.write_text("// NX journal\n", encoding="utf-8")
        bridge._poll_once()
        assert len([d for k, d in events if k == "nx_journal"]) == 3, \
            "截断重挂不能重复发送旧行"
        assert bridge._last_line_no == 1
        # 截断后追加的新操作照常只发新增
        with open(str(journal), "a", encoding="utf-8") as f:
            f.write("// Menu: Insert->Revolve\n")
        bridge._poll_once()
        got = [d for k, d in events if k == "nx_journal"]
        assert len(got) == 4, got
        assert got[-1]["menu"] == "Insert->Revolve" and got[-1]["line_no"] == 2, got[-1]
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    ok("NX 截断重挂正确（不重复发旧行；之后的新增照常只发 1 条）")


def test_nx_autorecord_script():
    """v0.2.3：实验性自动录制脚本写到 <data_dir>\\nx\\，内容含数据目录与 RecordJournal。"""
    from collector_core import (nx_autorecord_script_path, nx_autorecord_script_text,
                                write_nx_autorecord_script)
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-nxauto-"))
    when = datetime(2026, 10, 27, 12, 34, 56)
    try:
        path = write_nx_autorecord_script(tmp, when=when)
        assert path.is_file(), f"脚本文件应真的创建: {path}"
        assert path == tmp / "nx" / "autorecord_startup.py", path
        assert path == nx_autorecord_script_path(tmp)
        text = path.read_text(encoding="utf-8")
        assert "import NXOpen" in text, text
        assert "Session.GetSession().JournalManager.RecordJournal(" in text, text
        assert str(tmp) in text, "脚本内容必须包含本次数据目录"
        expected_journal = tmp / "journals" / "nx_20261027_123456.cs"
        assert str(expected_journal) in text, \
            f"录制目标应是 <data_dir>\\journals\\nx_<时间戳>.cs: {expected_journal}"
        assert "try:" in text and "except Exception" in text, "录制调用必须用 try/except 包住"
        # 纯函数与写盘内容一致；重复生成覆盖同名文件（幂等，不建第二个脚本）
        assert nx_autorecord_script_text(tmp, when=when) == text
        assert write_nx_autorecord_script(tmp, when=when) == path
        assert len(list((tmp / "nx").glob("autorecord_startup.py"))) == 1
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    ok("NX 实验自动录制脚本生成正确（写到 <data_dir>\\nx\\autorecord_startup.py，"
       "内容含数据目录 + RecordJournal + try/except）")


def test_nx_journal_path_setting():
    """v0.2.3：【选择 NX journal 文件】只改 nx.journal_path，保留其它键，密钥不落盘。"""
    from collector_core import ConfigManager, set_nx_journal_path
    tmp = Path(tempfile.mkdtemp(prefix="cad-ai-nxpick-"))
    old_env = os.environ.get("AI_EYE_KEY")
    try:
        mgr = ConfigManager(tmp)
        cfg = mgr.load()
        cfg["idle_minutes"] = 8.5                     # 用户改过的设置
        cfg.setdefault("nx", {})["journal_dirs"] = ["D:\\my_journals"]
        mgr.save(cfg)
        journal = tmp / "my_journal.py"
        journal.write_text("# Menu: File->New\n", encoding="utf-8")
        os.environ["AI_EYE_KEY"] = "sk-test-must-not-land-on-disk"
        merged = set_nx_journal_path(tmp, journal)
        assert merged["nx"]["journal_path"] == str(journal)
        saved = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
        assert saved["nx"]["journal_path"] == str(journal), saved.get("nx")
        assert saved["idle_minutes"] == 8.5, "选择文件不能覆盖其它设置"
        assert saved["nx"]["journal_dirs"] == ["D:\\my_journals"], saved["nx"]
        assert saved["ai_eye"]["api_key"] == "", "环境变量 AI_EYE_KEY 绝不能落盘"
        assert "sk-test-must-not-land-on-disk" not in json.dumps(saved)
        # 再选另一个文件：覆盖同一个键；传空字符串 = 清空回自动发现
        journal2 = tmp / "j2.cs"
        journal2.write_text("// Menu: File->Open\n", encoding="utf-8")
        set_nx_journal_path(tmp, str(journal2))
        saved2 = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
        assert saved2["nx"]["journal_path"] == str(journal2), saved2["nx"]
        assert saved2["idle_minutes"] == 8.5
        set_nx_journal_path(tmp, "")
        saved3 = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
        assert saved3["nx"]["journal_path"] == "", saved3["nx"]
    finally:
        if old_env is None:
            os.environ.pop("AI_EYE_KEY", None)
        else:
            os.environ["AI_EYE_KEY"] = old_env
        shutil.rmtree(str(tmp), ignore_errors=True)
    ok("选择 NX journal 保存正确（只改 nx.journal_path / 保留其它键 / 环境密钥不落盘）")


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
    test_reconcile_stream1_guard,
    test_nx_journal_bridge,
    test_nx_record_hint,
    test_cad_discovery,
    # v0.2.2 安全修复（纯逻辑 / 假对象，不碰屏幕、不碰真 CAD）
    test_ai_eye_capture_guard,
    test_invalid_capture_box_fails_selftest,
    test_config_env_key_not_persisted,
    test_nx_journal_dir_same_source,
    # v0.2.3：NX 2406 实测准备（纯文件/纯逻辑，不碰 tkinter/屏幕/真 NX）
    test_nx_replay_existing_on,
    test_nx_replay_existing_off,
    test_nx_replay_append_only_new,
    test_nx_replay_truncate_reattach,
    test_nx_autorecord_script,
    test_nx_journal_path_setting,
    test_sw_com_disconnect_reconnect,
    test_sw_feature_traversal_error_disconnects,
    test_integration_scripts_only_close_owned,
    # v4：纯文件/纯逻辑，不开钩子不截图
    test_v4_session_naming,
    test_v4_crash_recovery,
    test_v4_legacy_archive,
    test_v4_iterate_all_sessions,
    test_v4_single_instance_lock,
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
    test_invalid_capture_box_fails_selftest()
    test_reconcile()
    test_reconcile_stream1_guard()
    test_nx_journal_bridge()
    test_nx_record_hint()
    test_gesture_and_ai_eye()
    test_cad_discovery()
    # v0.2.2 安全修复（阻断：AI 眼越界；高危：装聋/密钥/NX 目录/写库/SW 重连/联调清理）
    test_ai_eye_capture_guard()
    test_config_env_key_not_persisted()
    test_nx_journal_dir_same_source()
    test_nx_bridge_data_dir_wired()
    # v0.2.3：NX 2406 实测准备（补读已有 journal / 选文件 / 实验自动录制脚本）
    test_nx_replay_existing_on()
    test_nx_replay_existing_off()
    test_nx_replay_append_only_new()
    test_nx_replay_truncate_reattach()
    test_nx_autorecord_script()
    test_nx_journal_path_setting()
    test_write_failure_keeps_session()
    test_sw_com_disconnect_reconnect()
    test_sw_feature_traversal_error_disconnects()
    test_integration_scripts_only_close_owned()
    # v4 会话存储（v0.2.0）用例
    test_v4_session_naming()
    test_v4_session_folder_and_json()
    test_v4_empty_session_removed()
    test_v4_crash_recovery()
    test_v4_legacy_archive()
    test_v4_iterate_all_sessions()
    test_v4_single_instance_lock()
    test_v4_ai_eye_drain()
    print("\n全部自检通过 ✔")
    sys.exit(0)
