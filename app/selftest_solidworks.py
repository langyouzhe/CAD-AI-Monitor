# -*- coding: utf-8 -*-
"""
selftest_solidworks.py —— SolidWorks 真机联调（需要本机装了 SW2022）：

1. 连接 SolidWorks（优先接已打开的实例；没开就正常启动一个，测完**只按 PID
   关掉本脚本自己 Popen 的那一个进程树**。注意：SW 后台模式不注册 COM 接口，
   所以不能隐藏启动）；
2. 启动采集器（流1 桥 + 流2，调试模式放开窗口过滤）；
3. 用 COM 在 SW 里真的建一个零件：新建文档 → 选基准面 → 画草图 → 拉伸凸台，
   同时注入一次假按键；
4. 检查数据库：流1（documentActivated / selectionChanged / featureAdded）
   和流2（key_down）都按时间戳落在同一次会话里 —— 这就是"两路数据对齐"。

跑法：python selftest_solidworks.py
退出码 0 = 通过。**关闭规则**：只关闭**本脚本自己 Popen 的进程树**（taskkill
/PID <pid> /T /F）；**绝不调用 COM `ExitApp()`**（`launched=True` 不能证明
COM 实例属于自己——GetActiveObject 可能拿到用户原本开着的实例），用户实例一律不动。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))

# COM / CAD 发现都**延迟导入**：本文件被自检当模块导入做行为测试时，
# 不要求机器上装了 pywin32/SolidWorks（CI 也能 import）。
from input_listener import CollectorSession  # noqa: E402
from solidworks_bridge import _get_com_member  # noqa: E402  安全读 COM 成员（属性/方法通吃）

PLANE_NAMES = ["Front Plane", "前视基准面"]  # 英文版/中文版都能选中
WAIT_CONNECT_S = 240   # SW 冷启动 + 注册 COM 接口可能要 1~3 分钟


def _prog_ids():
    """SolidWorks COM 名（不限版本，自动发现，不写死）。"""
    import cad_discovery
    return cad_discovery.sw_progids()


def _sw_exe():
    """SolidWorks 主程序路径（找不到返回空串）。"""
    import cad_discovery
    return cad_discovery.sw_exe_path() or ""


def send_key_a():
    """注入一次假按键（字母 A），走系统真实输入通道。

    先用新 API（SendInput），若被系统安全机制拒绝，退回老 API（keybd_event，
    不做完整性检查）。两者都能被 pynput 的全局钩子看见。
    """
    import ctypes
    import ctypes.wintypes as wt
    KEYEVENTF_SCANCODE, KEYEVENTF_KEYUP = 0x0008, 0x0002

    class KI(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_ulonglong)]

    class UI(ctypes.Union):
        _fields_ = [("ki", KI)]

    class IN(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", UI)]

    results = []
    for up in (False, True):
        inp = IN(type=1)
        inp.ki = KI(wVk=0, wScan=0x1E, dwFlags=KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0),
                    time=0, dwExtraInfo=0)
        sent = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(IN))
        results.append(sent)
        if sent == 0:
            err = ctypes.windll.kernel32.GetLastError()
            results.append(f"SendInput失败 err={err}")
            # 老 API 兜底：不受完整性级别限制
            flags = KEYEVENTF_KEYUP if up else 0
            ctypes.windll.user32.keybd_event(0x41, 0x1E, flags, 0)
        time.sleep(0.15)
    return results


def close_started(proc):
    """只关闭**本脚本自己启动**的进程（按 PID；绝不按镜像名 /IM 全杀）。

    SolidWorks 里经常有用户没保存的模型：测试脚本只能关自己 Popen 起来的那一个
    进程树，否则一次"联调"就能把人家一上午的活干掉。
    没启动过（proc=None）→ 什么都不做。
    """
    if proc is None:
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
        return True
    except Exception:
        return False


def cleanup_after_test(sw, launched_proc):
    """测试收尾：**只按 PID 关闭自己 Popen 的进程树，不碰 COM 实例**。

    为什么不调用 `sw.ExitApp()`：`launched=True` 只说明"我们启动过一个进程"，
    并不能证明 COM 实例就属于它——Popen 之后 GetActiveObject 完全可能拿到
    用户原本就开着的 SW 实例。ExitApp 会关掉用户没保存的模型。
    归属无法证明（launched_proc=None）→ **什么都不动**。
    """
    if launched_proc is not None:
        closed = close_started(launched_proc)
        if closed:
            print("已按 PID 关闭本次脚本启动的 SolidWorks 进程树"
                  f"（PID={launched_proc.pid}，未调用 COM ExitApp）")
        else:
            print(f"关闭本次脚本启动的 SolidWorks 失败（PID={launched_proc.pid}），"
                  "请手动检查")
        return closed
    if sw is not None:
        print("SolidWorks 原本就在运行 → 保持打开（不会关掉你自己的实例）")
    else:
        print("未连接 SolidWorks（本脚本没有关闭任何实例）")
    return False


def connect_sw():
    """接已开的 SW；没有就用"正常方式"启动 SLDWORKS.exe（和用户双击一样）。

    为什么不用 COM Dispatch 启动：SW 被 COM 派发启动时不会向系统注册接口表，
    之后 GetActiveObject 永远找不到它；正常启动才会注册（采集器也只连正常打开的 SW）。

    返回 (sw, launched_proc)：
    - sw=None → 找不到 SolidWorks 主程序（调用方返回 1）；
    - launched_proc=Popen 对象 → 这个进程是**本脚本启动**的，测试结束按 PID 关；
    - launched_proc=None → 用户原本就开着（**绝不能关**，否则可能丢未保存的工作）。
      注意：即使我们自己 Popen 过，GetActiveObject 也可能先拿到用户原实例；
      所以关闭只认"我们 Popen 的 PID"，绝不按 COM 实例关。
    """
    import win32com.client
    prog_ids = _prog_ids()                    # 不限版本：自动发现本机 SW 的 COM 名
    sw_exe = _sw_exe()
    launched_proc = None
    sw = None
    for pid in prog_ids:
        try:
            sw = win32com.client.GetActiveObject(pid)
            print(f"已连接到正在运行的 SolidWorks（ProgID={pid}）")
            break
        except Exception:
            continue
    if sw is None:
        if not sw_exe or not Path(sw_exe).is_file():
            print("找不到 SolidWorks 主程序（未安装？）——请先手动启动 SW 再重试")
            return None, None
        launched_proc = subprocess.Popen([sw_exe])
        print("已正常启动 SolidWorks（本次脚本启动，测试结束只按 PID 关它）…等待它就绪")
        deadline = time.monotonic() + WAIT_CONNECT_S
        while sw is None and time.monotonic() < deadline:
            time.sleep(5)
            for pid in prog_ids:
                try:
                    sw = win32com.client.GetActiveObject(pid)
                    break
                except Exception:
                    continue
        if sw is None:
            close_started(launched_proc)      # 只关自己 Popen 的那个进程树
            raise RuntimeError("SolidWorks 启动超时或未注册 COM 接口")
    rev = sw.RevisionNumber
    year = int(rev.split(".")[0]) - 8 + 2000
    print(f"SolidWorks 版本: {rev}（约 {year} 版）")
    return sw, launched_proc


def find_part_template(sw):
    """找零件模板：先问 SW 自己的默认模板设置，再翻安装目录（中英文语言包都找）。"""
    import glob
    # 1) SW 用户偏好里的默认零件模板（偏好编号 8 = swDefaultTemplatePart）
    try:
        pref = sw.GetUserPreferenceStringValue(8)
        for cand in str(pref or "").split(";"):
            cand = cand.strip().strip('"')
            if cand and os.path.isfile(cand):
                return cand
            if cand and os.path.isdir(cand):
                hits = glob.glob(os.path.join(cand, "*.prtdot"))
                if hits:
                    return hits[0]
    except Exception:
        pass
    # 2) 常见安装位置兜底
    for root in (
        r"D:\Program Files\SOLIDWORKS Corp22\SOLIDWORKS\data\templates",
        r"D:\Program Files\SOLIDWORKS Corp22\SOLIDWORKS\lang\chinese-simplified\templates",
        r"D:\Program Files\SOLIDWORKS Corp22\SOLIDWORKS\lang\english\templates",
        r"C:\ProgramData\SolidWorks\SOLIDWORKS 2022\templates",
    ):
        hits = glob.glob(os.path.join(root, "*.prtdot"))
        if hits:
            return hits[0]
    return None


def create_test_part(sw):
    """建一个新零件文档，带重试：SW 刚启动时 COM 成员可能还没就绪。"""
    import pywintypes
    template = None
    last_err = None
    deadline = time.monotonic() + 300
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            # 优先 NewPart（最省事）；失败就退到 NewDocument（自带模板路径，最稳）
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
        # 诊断信息：ActiveDoc 能不能读，能读说明 COM 成员解析已正常
        probe = "?"
        try:
            doc = _get_com_member(sw, "ActiveDoc")
            probe = "ActiveDoc 可读" if doc is None else f"ActiveDoc={_get_com_member(doc, 'GetTitle')}"
        except Exception:
            probe = "ActiveDoc 不可读"
        print(f"  第{attempt}次建文档失败（{str(last_err)[:60]}），{probe}，10 秒后重试…")
        time.sleep(10)
    raise RuntimeError(f"5 分钟内建不出零件文档，最后错误: {last_err}")


def build_test_part(sw):
    """真实建模一次：新零件 → 选基准面 → 草图直线 → 拉伸凸台。"""
    import pythoncom
    import win32com.client

    model = create_test_part(sw)
    time.sleep(1.5)

    selected = False
    empty_callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)  # SelectByID2 的空 Callout 参数（不能传 None）
    for name in PLANE_NAMES:
        if model.Extension.SelectByID2(name, "PLANE", 0, 0, 0, False, 0, empty_callout, 0):
            selected = True
            break
    if not selected:
        raise RuntimeError("找不到基准面（名称与本地化不符）")
    from solidworks_bridge import snapshot_sw
    sel_now = snapshot_sw(sw)["selection"]
    print(f"  选中基准面后抓拍: 选中 {len(sel_now)} 项 {json.dumps(sel_now, ensure_ascii=False)[:160]}")
    time.sleep(1.5)

    model.SketchManager.InsertSketch(True)
    time.sleep(0.5)
    model.SketchManager.CreateLine(0, 0, 0, 0.05, 0, 0)
    time.sleep(0.5)
    model.SketchManager.InsertSketch(True)   # 第二次调用 = 退出并提交草图
    time.sleep(0.8)

    model.FeatureManager.FeatureExtrusion3(
        True, False, True, 0, 0, 0.01, 0.0,
        False, False, False, False, 0.0, 0.0,
        False, False, False, False, True, False, True,
        0, 0.0, False,
    )
    time.sleep(1.5)
    # 拉伸后再选一次基准面，并停 2.5 秒让桥从容采样（验证 selectionChanged 事件）
    model.ClearSelection2(True)
    time.sleep(0.3)
    for name in PLANE_NAMES:
        if model.Extension.SelectByID2(name, "PLANE", 0, 0, 0, False, 0, empty_callout, 0):
            break
    time.sleep(2.5)
    print(f"已在 {_get_com_member(model, 'GetTitle')} 里完成：草图 + 拉伸凸台")


def main():
    import pythoncom

    pythoncom.CoInitialize()
    launched_proc = None
    sw = None
    try:
        sw, launched_proc = connect_sw()
        if sw is None:
            return 1
        return _main_body(sw)
    finally:
        # 收尾：只按 PID 关本脚本自己 Popen 的进程树；**不调用 COM ExitApp**
        # （launched 不能证明 COM 实例归属，ExitApp 可能关掉用户原实例）。
        cleanup_after_test(sw, launched_proc)


def _main_body(sw):
    tmp = tempfile.mkdtemp(prefix="cad-ai-swtest-")
    prog_ids = _prog_ids()
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "version": 2,
            "idle_minutes": 10.0,                     # 测试期间不触发离开
            "debug": {"allow_all_windows": True},     # 让注入的假按键也能入库
            "stream1": {"enabled": True, "prog_id": prog_ids[0],
                        "allow_launch": False, "poll_interval_s": 0.5},
        }, f, ensure_ascii=False, indent=2)

    session = CollectorSession(tmp)
    session.start()
    try:
        # 等流1 桥连上（SW 冷启动 + ROT 注册可能很慢）
        deadline = time.monotonic() + WAIT_CONNECT_S
        last_report = 0
        while time.monotonic() < deadline:
            st = session.stats()["stream1_status"]
            if st and st.get("connected"):
                break
            if time.monotonic() - last_report > 15:
                last_report = time.monotonic()
                print(f"  等待流1 连接…（{st and st.get('last_error') or '重试中'}）")
            time.sleep(1)
        st = session.stats()["stream1_status"]
        assert st and st.get("connected"), f"流1 未连上: {st}"
        print("流1 桥已连接，开始建模测试…")

        time.sleep(3.0)          # 先抓基准快照
        build_test_part(sw)      # 真实建模（产生流1事件）
        # 注入假按键前先把 SW 最小化：SW 窗口在前台时，系统会拒绝测试脚本的
        # "伪造按键"（安全限制，只影响测试；真人敲键盘不受影响）
        try:
            sw.FrameState = 2    # 最小化
            time.sleep(1.0)
        except Exception as exc:
            print(f"  最小化 SW 失败（继续尝试注入）: {exc}")
        st = session.stats()
        print(f"  注入前计数: 流2={st['counts'].get('stream2', 0)}, "
              f"键盘监听器存活={session._kb_listener.is_alive()}")
        ret = send_key_a()       # 注入假按键（流2事件，验证两路同库）
        print(f"  SendInput 返回: {ret}")
        try:
            sw.FrameState = 1    # 还原窗口
        except Exception:
            pass
        time.sleep(2.0)
        st = session.stats()
        print(f"  注入后计数: 流2={st['counts'].get('stream2', 0)}")
        time.sleep(2.0)          # 给桥留抓拍时间
    finally:
        session.stop()

    events = session.store.query_session(session.session_id)
    kinds = {}
    for e in events:
        key = f"{e['source']}/{e['kind']}"
        kinds[key] = kinds.get(key, 0) + 1
    print("事件分布:", json.dumps(kinds, ensure_ascii=False))

    s1_types = {e["kind"] for e in events if e["source"] == "stream1"}
    assert "documentActivated" in s1_types, f"缺 documentActivated: {s1_types}"
    assert "selectionChanged" in s1_types, f"缺 selectionChanged: {s1_types}"
    assert "featureAdded" in s1_types, f"缺 featureAdded: {s1_types}"
    assert any(e["source"] == "stream2" and e["kind"] == "key_down" for e in events), \
        "缺流2 key_down"

    # 两路对齐展示：打印各来源首尾时间戳
    for src in ("stream1", "stream2"):
        es = [e for e in events if e["source"] == src]
        print(f"{src}: {len(es)} 条，时间范围 {es[0]['ts_iso']} ~ {es[-1]['ts_iso']}")

    print("\nSolidWorks 真机联调通过 ✔ 两路数据已按时间戳落在同一次会话")
    return 0


if __name__ == "__main__":
    sys.exit(main())
