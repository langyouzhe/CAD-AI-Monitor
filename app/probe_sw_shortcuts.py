# -*- coding: utf-8 -*-
"""
probe_sw_shortcuts.py —— 真机探测 SW 的"命令名 ↔ ID"翻译接口（开发工具）

背景：注册表里读到的笔势映射是命令 ID（数字），要翻成命令名。
探测内容：
1. ISldWorks 上有哪些 GetCommand* / *Shortcut* / *Gesture* 成员；
2. 对注册表里读到的真实笔势 ID 逐个尝试翻译；
3. ICommandManager / ICommand 有没有名字翻译。

用法：python probe_sw_shortcuts.py
SW 没开会正常启动一个（测完**只按 PID 关自己 Popen 的进程树**，不调用 COM ExitApp）。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pythoncom  # noqa: E402
import win32com.client  # noqa: E402
import winreg  # noqa: E402

PROG_ID = "SldWorks.Application.30"
SW_EXE = r"D:\Program Files\SOLIDWORKS Corp22\SOLIDWORKS\SLDWORKS.exe"

# 从注册表读到的真实笔势 ID（Part 子键）
GESTURE_IDS = [32777, 32773, 33594, 32771, 32888, 34101, 33580, 55025, 55026]

# 猜测可能存在的翻译接口名
CANDIDATES = [
    "GetCommandName", "GetCommandNameFromID", "GetCommandNameByID",
    "CommandNameFromID", "GetCommandID", "GetCommandId",
    "GetKeyboardShortcuts", "GetKeyboardShortcut",
    "IGetCommandManager", "GetCommandManager",
]


def read_reg_gesture_ids():
    """读注册表笔势映射（确认探测脚本自己也能读）。"""
    out = {}
    for sub in ("Part", "Assembly", "Drawing", "Sketch"):
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\SolidWorks\SOLIDWORKS 2022\User Interface\Settings\Mouse Gestures\{sub}",
            )
        except OSError:
            continue
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            out.setdefault(name, []).append(value)
            i += 1
        winreg.CloseKey(key)
    return out


def close_started(proc):
    """只关闭**本脚本自己启动**的进程（按 PID；绝不按镜像名 /IM 全杀）。

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
    """收尾：只按 PID 关自己 Popen 的进程树；**不调用 COM ExitApp**。

    GetActiveObject 可能拿到用户原本开着的 SW 实例，按 COM 实例关会丢未保存模型。
    无法证明归属（launched_proc=None）→ 什么都不动。
    """
    if launched_proc is not None:
        closed = close_started(launched_proc)
        if closed:
            print("已按 PID 关闭本次脚本启动的 SolidWorks 进程树"
                  f"（PID={launched_proc.pid}，未调用 COM ExitApp）")
        else:
            print(f"关闭本次脚本启动的 SolidWorks 失败（PID={launched_proc.pid}）")
        return closed
    if sw is not None:
        print("SolidWorks 原本就在运行 → 保持打开（不会关掉你自己的实例）")
    else:
        print("未连接 SolidWorks（本脚本没有关闭任何实例）")
    return False


def connect_sw():
    """连接 SW；没开就正常启动一个。返回 (sw, launched_proc)。

    launched_proc=None → 用户原本就开着（绝不能关）；只有自己 Popen 的进程
    才会在收尾时按 PID 关闭。
    """
    launched_proc = None
    try:
        sw = win32com.client.GetActiveObject(PROG_ID)
        print("已连接运行中的 SolidWorks")
    except Exception:
        launched_proc = subprocess.Popen([SW_EXE])
        print("已正常启动 SolidWorks（本次脚本启动，测完只按 PID 关它）…等待就绪")
        sw = None
        deadline = time.monotonic() + 240
        while sw is None and time.monotonic() < deadline:
            time.sleep(5)
            try:
                sw = win32com.client.GetActiveObject(PROG_ID)
            except Exception:
                pass
        if sw is None:
            close_started(launched_proc)
            raise RuntimeError("SW 启动超时")
    rev = sw.RevisionNumber
    print(f"SW 版本: {rev}")
    return sw, launched_proc


def main():
    pythoncom.CoInitialize()
    reg = read_reg_gesture_ids()
    print(f"注册表笔势值: {json.dumps(reg, ensure_ascii=False)}")
    sw, launched_proc = connect_sw()

    # 1) 逐个试候选翻译接口
    for name in CANDIDATES:
        try:
            member = getattr(sw, name)
            if callable(member):
                try:
                    member(32773)
                    print(f"  [可调用] {name}(32773) 执行成功")
                except Exception as exc:
                    print(f"  [可调用] {name}(32773) → {type(exc).__name__}: {str(exc)[:80]}")
            else:
                print(f"  [属性] {name} = {str(member)[:60]!r}")
        except AttributeError:
            print(f"  [不存在] {name}")

    # 2) 对真实笔势 ID 尝试 GetCommandName 类翻译
    for cand in CANDIDATES:
        if "Name" not in cand and "ID" not in cand:
            continue
        try:
            fn = getattr(sw, cand)
        except AttributeError:
            continue
        results = {}
        for gid in GESTURE_IDS:
            try:
                results[str(gid)] = fn(gid)
            except Exception as exc:
                results[str(gid)] = f"ERR:{type(exc).__name__}"
        print(f"  {cand} 批量翻译: {json.dumps(results, ensure_ascii=False)}")
        break  # 第一个能用的 Name 类接口就够

    # 3) CommandManager 看看
    try:
        cm = sw.GetCommandManager()
        print(f"  GetCommandManager() → {type(cm).__name__}")
        for name in ("GetCommandName", "GetCommandID", "GetCommandCount"):
            try:
                m = getattr(cm, name)
                print(f"    CM.{name} 存在: {callable(m)}")
            except AttributeError:
                print(f"    CM.{name} 不存在")
    except Exception as exc:
        print(f"  GetCommandManager 失败: {str(exc)[:100]}")

    cleanup_after_test(sw, launched_proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
