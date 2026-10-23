# -*- coding: utf-8 -*-
"""
selftest_production.py —— 生产配置全流程试运行（“能不能用”验收）

按真实默认配置（装聋开、无调试豁免）走一遍用户日常：
1. 正常启动 SolidWorks（像用户双击一样）；
2. 用生产默认配置启动采集器（数据落临时目录，不污染真实数据）；
3. 真实建模（COM）：选基准面 → 草图直线 → 拉伸凸台 → 换选另一个面；
4. 注入一次按键（keybd_event，SW 窗口在前台时钩子应收到）；
5. 试一次真实笔势（右键按住拖动，走 legacy mouse_event 通道）；
6. 停止 → 自动三方对账 → 查库输出统计。

判定：流1 有 documentActivated/featureAdded/selectionChanged、
流2 有 key_down/gesture（含方向）、对账标出缺票异常 → 能用。
"""
import ctypes
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pythoncom  # noqa: E402
import win32com.client  # noqa: E402

from collector_core import DEFAULT_CONFIG  # noqa: E402
from input_listener import CollectorSession  # noqa: E402
import cad_discovery  # noqa: E402

# 不限版本：自动发现本机装的是哪版 SW（SldWorks.Application 优先，再按版本倒序）
PROG_IDS = cad_discovery.sw_progids()
SW_EXE = cad_discovery.sw_exe_path() or ""   # 自动发现，不写死路径
PLANE_NAMES = ["Front Plane", "前视基准面"]
PLANE_NAMES2 = ["Top Plane", "上视基准面"]


def send_key(scan, up=False):
    KEYEVENTF_SCANCODE, KEYEVENTF_KEYUP = 0x0008, 0x0002
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    # VK 必须给（vk=0 时 pynput 的钩子会因"未知按键"静默丢弃）
    vk = scan + 0x41 - 0x1E if scan == 0x1E else 0x41
    ctypes.windll.user32.keybd_event(vk, scan, flags, 0)
    time.sleep(0.1)


def legacy_mouse(dx, dy, flags):
    ctypes.windll.user32.mouse_event(flags, dx, dy, 0, 0)
    time.sleep(0.12)


def real_gesture():
    """走 legacy mouse_event 的右键拖动（SendInput 在这台机器上被拒）。"""
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    legacy_mouse(0, 0, MOUSEEVENTF_RIGHTDOWN)
    for _ in range(10):  # 向右上拖
        legacy_mouse(12, -12, MOUSEEVENTF_MOVE)
    legacy_mouse(0, 0, MOUSEEVENTF_RIGHTUP)


def connect_sw():
    for pid in PROG_IDS:
        try:
            sw = win32com.client.GetActiveObject(pid)
            print(f"已连接运行中的 SolidWorks（ProgID={pid}）")
            return sw, False
        except Exception:
            continue
    if not SW_EXE or not Path(SW_EXE).is_file():
        raise RuntimeError("找不到 SolidWorks 主程序（未安装？）——请先手动启动 SW 再重试")
    subprocess.Popen([SW_EXE])
    launched = True
    print("已正常启动 SolidWorks…等待就绪")
    sw = None
    deadline = time.monotonic() + 300
    while sw is None and time.monotonic() < deadline:
        time.sleep(5)
        for pid in PROG_IDS:
            try:
                sw = win32com.client.GetActiveObject(pid)
                break
            except Exception:
                continue
    if sw is None:
        raise RuntimeError("SW 启动超时")
    return sw, launched


def foreground_sw(sw):
    """把 SW 窗口拉到前台（装聋判定要它在前台）。

    Windows 有"前台锁"：后台进程不能随便抢前台。老办法：先模拟按一下
    Alt（legacy keybd_event，本机可用），再 SetForegroundWindow 就能成功。
    """
    from solidworks_bridge import _get_com_member
    try:
        import win32gui
        frame = _get_com_member(sw, "Frame")   # Frame 是属性，不能加括号调
        hwnd = _get_com_member(frame, "GetHWnd")
        for _ in range(3):
            # 模拟 Alt 按下/抬起 → 解锁前台权限
            ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
            time.sleep(0.05)
            ctypes.windll.user32.keybd_event(0x12, 0, 0x0002, 0)
            time.sleep(0.2)
            win32gui.ShowWindow(hwnd, 9)   # SW_RESTORE
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.5)
            if win32gui.GetForegroundWindow() == hwnd:
                print("  SW 窗口已拉到前台 ✓")
                return True
            time.sleep(1)
        print("  SW 窗口仍在后台（注入的按键会被装聋过滤，属预期）")
        return False
    except Exception as exc:
        print(f"  拉到前台失败（可能影响流2记录）: {exc}")
        return False


def find_part_template(sw):
    """找零件模板（先问 SW 偏好，再翻安装目录）。"""
    import glob
    try:
        pref = sw.GetUserPreferenceStringValue(8)
        for cand in str(pref or "").split(";"):
            cand = cand.strip().strip('"')
            if cand and Path(cand).is_file():
                return cand
    except Exception:
        pass
    for root in (r"D:\Program Files\SOLIDWORKS Corp22\SOLIDWORKS\data\templates",
                 r"D:\Program Files\SOLIDWORKS Corp22\SOLIDWORKS\lang\chinese-simplified\templates",
                 r"D:\Program Files\SOLIDWORKS Corp22\SOLIDWORKS\lang\english\templates"):
        hits = glob.glob(str(Path(root) / "*.prtdot"))
        if hits:
            return hits[0]
    return None


def new_part_with_retry(sw):
    """SW 刚启动时 COM 成员要几分钟才就绪 → 建文档带重试。"""
    import pywintypes
    template = None
    last_err = None
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            try:
                model = sw.NewPart()
            except pywintypes.com_error as exc:
                model = None
                last_err = exc
                if template is None:
                    template = find_part_template(sw)
                if template:
                    model = sw.NewDocument(template, 0, 0, 0)
            if model is not None:
                return model
        except pywintypes.com_error as exc:
            last_err = exc
        print(f"  SW 还在初始化，10 秒后重试建文档…（{str(last_err)[:40]}）")
        time.sleep(10)
    raise RuntimeError(f"5 分钟内建不出零件文档: {last_err}")


def build_part(sw):
    """真实建模 + 换选（中文/英文基准面名都兼容）。"""
    model = new_part_with_retry(sw)
    time.sleep(1.5)
    empty = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
    for name in PLANE_NAMES:
        if model.Extension.SelectByID2(name, "PLANE", 0, 0, 0, False, 0, empty, 0):
            break
    time.sleep(1.0)
    model.SketchManager.InsertSketch(True)
    time.sleep(0.5)
    model.SketchManager.CreateLine(0, 0, 0, 0.05, 0, 0)
    time.sleep(0.5)
    model.SketchManager.InsertSketch(True)
    time.sleep(1.0)
    model.FeatureManager.FeatureExtrusion3(
        True, False, True, 0, 0, 0.01, 0.0,
        False, False, False, False, 0.0, 0.0,
        False, False, False, False, True, False, True, 0, 0.0, False)
    time.sleep(1.5)
    model.ClearSelection2(True)
    for name in PLANE_NAMES2:  # 换选上视基准面 → selectionChanged
        if model.Extension.SelectByID2(name, "PLANE", 0, 0, 0, False, 0, empty, 0):
            break
    time.sleep(2.0)


def main():
    pythoncom.CoInitialize()
    sw, launched = connect_sw()
    try:
        foreground_sw(sw)
        tmp = tempfile.mkdtemp(prefix="cad-ai-prod-")
        # 生产默认配置：装聋开、调试豁免关（真实环境长这样）
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["stream1"]["backends"] = ["solidworks"]
        cfg["ai_eye"] = {"enabled": False}   # 视觉先不开（BYOK 没填 key）
        with open(Path(tmp) / "config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        session = CollectorSession(tmp)
        session.start()
        try:
            # 等流1 连上
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                sts = session.stats()["stream1_status"] or []
                if any(s.get("connected") for s in sts):
                    break
                time.sleep(2)
            print("流1 已连接，开始真实操作…")
            time.sleep(3)            # 基线快照
            build_part(sw)           # 真实建模（流1 事件）
            foreground_sw(sw)        # 注入前重新抢前台（真人操作会抢回去）
            send_key(0x1E)           # 按键 'a'（流2）
            time.sleep(0.3)
            foreground_sw(sw)
            real_gesture()           # 真实笔势（流2，若通道可用）
            time.sleep(3)            # 给桥和钩子收尾时间
        finally:
            session.stop()           # 停止时自动跑三方对账

        events = session.store.query_session(session.session_id)
        counts = {}
        for e in events:
            key = f"{e['source']}/{e['kind']}"
            counts[key] = counts.get(key, 0) + 1
        print("\n=== 事件统计 ===")
        print(json.dumps(counts, ensure_ascii=False, indent=1))

        # 判定
        s1_kinds = {e["kind"] for e in events if e["source"] == "stream1"}
        s2_kinds = {e["kind"] for e in events if e["source"] == "stream2"}
        anomalies = [e for e in events if e["kind"] == "anomaly"]
        checks = {
            "流1 documentActivated": "documentActivated" in s1_kinds,
            "流1 featureAdded": "featureAdded" in s1_kinds,
            "流1 selectionChanged": "selectionChanged" in s1_kinds,
            "流2 key_down": "key_down" in s2_kinds,
            "流2 gesture": "gesture" in s2_kinds,
        }
        print("\n=== 验收判定 ===")
        for name, ok in checks.items():
            print(f"  {'✓' if ok else '✗'} {name}")
        gs = [e for e in events if e["kind"] == "gesture"]
        if gs:
            print(f"  笔势详情: {json.dumps(gs[0]['detail'], ensure_ascii=False)[:180]}")
        print(f"\n对账异常 {len(anomalies)} 条:")
        for a in anomalies[:5]:
            print(f"  - {a['detail']['type']}: {a['detail']['reason'][:60]}")
        return 0 if all(checks.values()) else 1
    finally:
        if launched:
            try:
                sw.ExitApp()
                print("\n已关闭 SolidWorks")
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
