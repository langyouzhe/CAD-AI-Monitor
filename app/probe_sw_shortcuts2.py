# -*- coding: utf-8 -*-
"""
probe_sw_shortcuts2.py —— 第二轮：挖 ICommandManager 的命令名翻译（开发工具）

上一轮发现 GetCommandManager 要传参数（管理器编号）。这一轮：
1. GetCommandManager(0/1/2/3) 逐个试，拿到对象后枚举成员名；
2. 找 ICommandManager/ICommand 上的 Name/GetName/GetCommandName 类成员；
3. 对注册表笔势 ID 尝试 cm.GetCommandName(id) 等翻译。
"""
import json
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import pythoncom  # noqa: E402
import win32com.client  # noqa: E402
import winreg  # noqa: E402

PROG_ID = "SldWorks.Application.30"
SW_EXE = r"D:\Program Files\SOLIDWORKS Corp22\SOLIDWORKS\SLDWORKS.exe"
GESTURE_IDS = [32777, 32773, 33594, 32771, 32888, 34101, 33580, 55025, 55026, 37995,
               32862, 32851, 32852, 32885, 33065, 33068, 32978, 32973, 32848]


def connect_sw():
    launched = False
    try:
        sw = win32com.client.GetActiveObject(PROG_ID)
        print("已连接运行中的 SolidWorks")
    except Exception:
        subprocess.Popen([SW_EXE])
        launched = True
        print("已正常启动 SolidWorks（测完会关）…")
        sw = None
        deadline = time.monotonic() + 240
        while sw is None and time.monotonic() < deadline:
            time.sleep(5)
            try:
                sw = win32com.client.GetActiveObject(PROG_ID)
            except Exception:
                pass
        if sw is None:
            raise RuntimeError("SW 启动超时")
    return sw, launched


def try_members(obj, patterns, label):
    """对 COM 对象尝试一组成员名，报告哪些存在、调用结果如何。"""
    print(f"--- {label} ---")
    names = []
    # 用 dir + getattr 摸一遍（动态分发的 dir 有限，重点试候选名）
    for cand in ("GetCommandName", "CommandName", "GetName", "Name",
                 "GetCommandID", "CommandID", "UserID", "GetCommandCount",
                 "GetCommandGroup", "GetCommands", "GetCommand"):
        try:
            m = getattr(obj, cand)
            names.append((cand, "callable" if callable(m) else str(m)[:40]))
        except Exception as exc:
            pass
    for cand, info in names:
        print(f"  成员 {cand}: {info}")


def main():
    pythoncom.CoInitialize()
    sw, launched = connect_sw()
    try:
        # 试几个编号的命令管理器
        for idx in (0, 1, 2, 3):
            try:
                cm = sw.GetCommandManager(idx)
                print(f"GetCommandManager({idx}) → {type(cm).__name__}")
                if cm is not None:
                    try_members(cm, None, f"CommandManager[{idx}]")
                    # 对笔势 ID 逐个试翻译
                    for cand in ("GetCommandName", "GetName", "CommandName"):
                        try:
                            fn = getattr(cm, cand)
                            if not callable(fn):
                                continue
                            results = {}
                            for gid in GESTURE_IDS:
                                try:
                                    results[str(gid)] = fn(gid)
                                except Exception as exc:
                                    results[str(gid)] = f"ERR:{str(exc)[:30]}"
                            print(f"  {cand} 批量翻译: {json.dumps(results, ensure_ascii=False)[:600]}")
                            break
                        except Exception:
                            continue
                break  # 拿到一个能用的就够
            except Exception as exc:
                print(f"GetCommandManager({idx}) 失败: {str(exc)[:80]}")
    finally:
        if launched:
            try:
                sw.ExitApp()
                print("已关闭 SolidWorks")
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
