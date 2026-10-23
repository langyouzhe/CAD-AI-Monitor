# -*- coding: utf-8 -*-
"""
selftest_creo.py —— Creo 真机联调：启动 Creo → 桥跟踪 trail 文件 → 验证事件入库

Creo 启动时就会往 trail 文件写一堆 UI 事件（Activate/Select…），足够验证
解析链路。测试结束关闭由本脚本拉起的 Creo（parametric.exe）。
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

from input_listener import CollectorSession  # noqa: E402
import cad_discovery  # noqa: E402

# 自动发现 Creo 主程序（不限版本/盘符；别人电脑也能找到），也可用 argv[1] 指定
CREO_EXE = cad_discovery.creo_exe_path(sys.argv[1] if len(sys.argv) > 1 else None)
WAIT_S = 240


def close_started(proc):
    """只关闭**本脚本自己启动**的进程（按 PID；绝不按镜像名 /IM 全杀）。

    Creo 启动慢，用户很可能本来就开着 Creo 在干活：测试脚本只能关自己 Popen
    起来的那一个进程树，否则会把用户没保存的工作一起杀掉。
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


def main():
    if not CREO_EXE or not Path(CREO_EXE).exists():
        print("找不到 Creo 主程序 parametric.exe（未安装/未发现）。")
        print("可以手动指定：python app\\selftest_creo.py <parametric.exe 路径>")
        return 1
    # 确认没有已在跑的 Creo：只有本脚本自己拉起的实例，测试结束才关
    procs = subprocess.run(["tasklist", "/FI", "IMAGENAME eq parametric.exe"],
                           capture_output=True, text=True).stdout
    launched_proc = None
    if "parametric.exe" not in procs.lower():
        launched_proc = subprocess.Popen([CREO_EXE], cwd=str(Path(CREO_EXE).parent))
        print("已启动 Creo Parametric（本次脚本启动，测试结束只关自己拉起的进程）…")
    else:
        print("Creo 已在运行（测试结束不会关闭它）")

    tmp = tempfile.mkdtemp(prefix="cad-ai-creo-")
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "version": 2,
            "idle_minutes": 10.0,
            "debug": {"allow_all_windows": True},
            "stream1": {"enabled": True, "backends": ["creo"]},
            "creo": {"trail_dirs": [], "config_pro_paths": [], "poll_interval_s": 1.0},
        }, f, ensure_ascii=False, indent=2)

    session = CollectorSession(tmp)
    session.start()
    try:
        # 等 creo 桥挂上 trail 文件
        deadline = time.monotonic() + WAIT_S
        status = None
        while time.monotonic() < deadline:
            sts = session.stats()["stream1_status"] or []
            for st in sts:
                if st.get("backend") == "creo" and st.get("connected"):
                    status = st
                    break
            if status:
                break
            time.sleep(2)
        assert status, f"Creo 桥未连接: {session.stats()['stream1_status']}"
        print(f"桥已挂上 trail: {status['trail_file']}")
        print(f"mapkey 数量: {status['mapkeys']}")
        # 再等几秒收事件（Creo 启动过程会持续写 trail）
        time.sleep(15)
    finally:
        session.stop()
        # 测试工具：只关自己启动的 Creo（用户原本开着的实例一律不动）
        if close_started(launched_proc):
            print("已关闭本次脚本启动的 Creo")
        else:
            print("Creo 原本就在运行 → 保持打开（不会关掉你自己的实例）")

    events = session.store.query_session(session.session_id)
    creo_events = [e for e in events if e["source"] == "stream1"]
    print(f"流1(Creo) 事件 {len(creo_events)} 条")
    kinds = {}
    for e in creo_events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print("事件分布:", json.dumps(kinds, ensure_ascii=False))
    for e in creo_events[:8]:
        print(f"  样例 [{e['ts_iso'][11:19]}] {e['kind']}: "
              f"{json.dumps(e['detail'], ensure_ascii=False)[:160]}")
    assert creo_events, "应至少收到 Creo trail 事件"

    print("\nCreo 真机联调通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
