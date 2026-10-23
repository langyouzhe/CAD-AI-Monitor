# -*- coding: utf-8 -*-
"""
selftest_nx.py —— NX 真机联调（2026-10-23 改为 Journal 数据源）

背景（问题清单 P0-1）：NX 的 *.syslog 是崩溃诊断日志，不含建模操作；
流1 改读 NX「录制日志」输出的 .cs/.py。本脚本验证：
1. 自动发现 NX 主程序（不写死路径，公司机 C:\\Siemens\\NX2406 也能找到）；
2. 启动采集器（仅 nx 后端），桥能挂上 journal；
3. 你手动在 NX 里操作几下 → journal 追加 → 采集器收到 menu/api 事件。

用法：
1. 先在 NX 里【工具 → 日志 → 录制】或 File → Utilities → Record Journal，
   把 journal 保存到 桌面 / 文档 / data\\journals（任选，桥会自动找最新）；
2. 运行本脚本（不需要手动开 NX，脚本会拉起；若 NX 已开则直接用）；
3. 在 NX 里做几个操作（画草图/拉伸），回来看事件计数。
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

import cad_discovery  # noqa: E402
from input_listener import CollectorSession  # noqa: E402

WAIT_S = 300


def journal_dirs():
    home = Path.home()
    out = []
    for d in (home / "Desktop", home / "Documents", home / "Downloads",
              Path(__file__).resolve().parent.parent / "data" / "journals"):
        if d.is_dir():
            out.append(str(d))
    return out


def scan_journals(dirs):
    from journal_parser import looks_like_journal
    out = []
    for d in dirs:
        try:
            for pat in ("*.cs", "*.py", "*.vb"):
                for p in Path(d).glob(pat):
                    try:
                        if p.is_file() and p.stat().st_size > 0 and looks_like_journal(p):
                            out.append(p)
                    except OSError:
                        continue
        except Exception:
            continue
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def main():
    nx_exe = cad_discovery.nx_exe_path()
    if not nx_exe:
        print("找不到 NX（ugraf.exe）。请确认本机装了 NX，或告诉我路径：")
        print("  python app\\selftest_nx.py <ugraf.exe 路径>")
        return 1
    if len(sys.argv) > 1:
        nx_exe = sys.argv[1]
    print(f"NX 主程序：{nx_exe}")

    procs = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ugraf.exe"],
                           capture_output=True, text=True).stdout
    if "ugraf.exe" not in procs.lower():
        subprocess.Popen([nx_exe], cwd=str(Path(nx_exe).parent))
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

    dirs = journal_dirs()
    found = scan_journals(dirs)
    print("找到的 journal：", [str(p) for p in found[:5]] or "（没有）")
    if not found:
        print("提示：先在 NX 里录制一次日志（保存到 桌面/文档/data\\journals），")
        print("      再重新运行本脚本。syslog 不再作为数据源（问题清单 P0-1）。")

    tmp = tempfile.mkdtemp(prefix="cad-ai-nx-")
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "version": 2,
            "idle_minutes": 10.0,
            "debug": {"allow_all_windows": True},
            "stream1": {"enabled": True, "backends": ["nx"]},
            "nx": {"journal_dirs": dirs, "poll_interval_s": 1.0},
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
        print("现在去 NX 里操作几下（画草图/拉伸），10 秒后统计…")
        time.sleep(10)
    finally:
        session.stop()
        subprocess.run(["taskkill", "/F", "/IM", "ugraf.exe"], capture_output=True)
        print("已关闭 NX")

    events = session.store.query_session(session.session_id)
    nx_events = [e for e in events if e["source"] == "stream1"]
    print(f"流1(NX journal) 事件 {len(nx_events)} 条")
    for e in nx_events[:8]:
        print(f"  样例 {e['kind']}: {json.dumps(e['detail'], ensure_ascii=False)[:150]}")
    if not nx_events:
        print("\n没有收到 NX 事件。检查：① NX 里是否正在录制日志；"
              "② 日志保存目录是否在候选目录里；③ 可配置 nx.journal_path 指定文件。")
    else:
        print("\nNX 真机联调通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
