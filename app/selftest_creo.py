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

CREO_EXE = r"D:\CREO\Creo12.0\Creo 12.4.4.0\Parametric\bin\parametric.exe"
WAIT_S = 240


def main():
    if not Path(CREO_EXE).exists():
        print(f"找不到 Creo: {CREO_EXE}")
        return 1
    # 确认没有已在跑的 Creo
    procs = subprocess.run(["tasklist", "/FI", "IMAGENAME eq parametric.exe"],
                           capture_output=True, text=True).stdout
    launched = False
    if "parametric.exe" not in procs.lower():
        subprocess.Popen([CREO_EXE], cwd=str(Path(CREO_EXE).parent))
        launched = True
        print("已启动 Creo Parametric（测试结束会关闭）…")

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
        # 测试工具：结束后关掉 Creo（测试前确认过本机没有 Creo 在跑）
        subprocess.run(["taskkill", "/F", "/IM", "parametric.exe"],
                       capture_output=True)
        print("已关闭 Creo")

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
