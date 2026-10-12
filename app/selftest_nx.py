# -*- coding: utf-8 -*-
"""
selftest_nx.py —— NX(Designcenter 2512) 真机联调：启动 NX → 找 syslog → 验证解析

NX 冷启动很慢（1~3 分钟）。测试流程：
1. 启动 ugraf.exe（Designcenter NX 主程序）；
2. 等它跑起来后，在 TEMP/用户目录 扫描 *.syslog；
3. 启动采集器（仅 nx 后端），验证桥挂上 syslog 并收到事件；
4. 结束关闭 NX。

注意：syslog 的路径/格式是探测版——如果扫描不到，本测试会打印诊断
（NX 进程状态、TEMP 下的新文件），供后续校准解析器。
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

NX_EXE = r"D:\Program Files\Siemens\DesigncenterNX2512\NXBIN\ugraf.exe"
WAIT_S = 300


def scan_syslogs(dirs):
    out = []
    for d in dirs:
        try:
            for p in Path(d).glob("*.syslog"):
                if p.is_file():
                    out.append(p)
        except Exception:
            continue
    return out


def main():
    if not Path(NX_EXE).exists():
        print(f"找不到 NX: {NX_EXE}")
        return 1
    procs = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ugraf.exe"],
                           capture_output=True, text=True).stdout
    if "ugraf.exe" not in procs.lower():
        subprocess.Popen([NX_EXE], cwd=str(Path(NX_EXE).parent))
        print("已启动 NX（测试结束会关闭）…")
    else:
        print("NX 已在运行")

    # 等 NX 进程内存起来（说明主程序已加载）
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        procs = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process ugraf -ErrorAction SilentlyContinue | Measure-Object WorkingSet64 -Sum).Sum"],
            capture_output=True, text=True).stdout.strip()
        try:
            mem_mb = int(procs) / 1048576 if procs else 0
        except ValueError:
            mem_mb = 0
        if mem_mb > 200:
            print(f"NX 已加载（内存 {mem_mb:.0f}MB）")
            break
        time.sleep(5)
    else:
        print("NX 启动超时（可能许可弹窗卡住）")
        subprocess.run(["taskkill", "/F", "/IM", "ugraf.exe"], capture_output=True)
        return 1

    time.sleep(10)  # 等它写 syslog
    dirs = [tempfile.gettempdir(), str(Path.home()),
            r"D:\Program Files\Siemens\DesigncenterNX2512\ProgramData"]
    found = scan_syslogs(dirs)
    print("找到的 syslog:", [str(p) for p in found])

    tmp = tempfile.mkdtemp(prefix="cad-ai-nx-")
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "version": 2,
            "idle_minutes": 10.0,
            "debug": {"allow_all_windows": True},
            "stream1": {"enabled": True, "backends": ["nx"]},
            "nx": {"syslog_dirs": [str(d) for d in dirs], "poll_interval_s": 1.0},
        }, f, ensure_ascii=False, indent=2)

    session = CollectorSession(tmp)
    session.start()
    try:
        deadline = time.monotonic() + 60
        status = None
        while time.monotonic() < deadline:
            sts = session.stats()["stream1_status"] or []
            for st in sts:
                if st.get("backend") == "nx" and st.get("connected"):
                    status = st
                    break
            if status:
                break
            time.sleep(2)
        print("NX 桥状态:", status or session.stats()["stream1_status"])
        time.sleep(10)
    finally:
        session.stop()
        subprocess.run(["taskkill", "/F", "/IM", "ugraf.exe"], capture_output=True)
        print("已关闭 NX")

    events = session.store.query_session(session.session_id)
    nx_events = [e for e in events if e["source"] == "stream1"]
    print(f"流1(NX) 事件 {len(nx_events)} 条")
    for e in nx_events[:6]:
        print(f"  样例 {e['kind']}: {json.dumps(e['detail'], ensure_ascii=False)[:150]}")
    if not nx_events:
        print("\nNX syslog 未捕获——记录诊断信息，解析器待校准")
        print("（详见最终报告；如需继续，下次会话按实际 syslog 路径/格式校准）")
    else:
        print("\nNX 真机联调通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
