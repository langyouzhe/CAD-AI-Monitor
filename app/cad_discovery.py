# -*- coding: utf-8 -*-
"""
cad_discovery.py —— CAD 自动发现（开源通用化的核心模块）

设计铁律：**自动发现，不猜目录、不写死版本**。

三家 CAD 的"接口入口"在不同机器/版本上位置不同，本模块负责把它们找出来：
- SolidWorks：不限版本的 COM ProgID（`SldWorks.Application`）+ 各版本 ProgID 枚举
- SolidWorks 设置：注册表里所有 `SOLIDWORKS <年份>` 版本键
- Creo：trail 文件的候选目录（快捷方式工作目录 / 注册表安装路径 / config.pro / 常见目录）
- NX：syslog 的候选目录
- 建模窗口：主流 CAD 程序名清单（装聋过滤用）

所有函数都**不抛异常**（找不到就返回空列表），并且都能被配置覆盖。
"""
import os
import re
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

# 主流 CAD 主程序名（用于"装聋"判定：只有这些窗口在前台时才记录）
DEFAULT_MODELING_APPS = [
    {"exe": "SLDWORKS.exe", "title": "SOLIDWORKS"},
    {"exe": "sldworks.exe", "title": "SolidWorks"},
    {"exe": "parametric.exe", "title": "Creo"},
    {"exe": "xtop.exe", "title": "Creo"},
    {"exe": "ugraf.exe", "title": "NX"},
    {"exe": "nx.exe", "title": "NX"},
    {"exe": "Inventor.exe", "title": "Autodesk Inventor"},
    {"exe": "acad.exe", "title": "AutoCAD"},
    {"exe": "SolidEdge.exe", "title": "Solid Edge"},
    {"exe": "FreeCAD.exe", "title": "FreeCAD"},
    {"exe": "Fusion360.exe", "title": "Fusion"},
    {"exe": "IronCAD.exe", "title": "IRONCAD"},
    {"exe": "catiaintegrat*.exe", "title": "CATIA"},
]

# ProeID 枚举：SldWorks.Application.NN（NN = 年份 - 2000 + 8，越大越新）
SW_PROGID_RE = re.compile(r"^SldWorks\.Application\.(\d+)$")


# ---------- SolidWorks ----------

def sw_progids(cfg_prog_id=None):
    """返回要尝试的 SolidWorks ProgID 列表（按优先级）。

    1. 配置里显式指定的（如果有）
    2. `SldWorks.Application` —— **不限版本**，绝大多数安装都注册了
    3. `SldWorks.Application.NN` —— 从注册表枚举，版本号从高到低
    """
    out = []
    if cfg_prog_id:
        out.append(cfg_prog_id)
    if "SldWorks.Application" not in out:
        out.append("SldWorks.Application")
    versions = []
    if winreg is not None:
        for hive in (winreg.HKEY_CLASSES_ROOT,):
            try:
                key = winreg.OpenKey(hive, "")
            except OSError:
                continue
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                m = SW_PROGID_RE.match(name)
                if m:
                    versions.append((int(m.group(1)), name))
    for _, name in sorted(versions, reverse=True):
        if name not in out:
            out.append(name)
    return out


def sw_registry_versions():
    """注册表里 SolidWorks 的版本键名列表，例如 ['SOLIDWORKS 2022', ...]。"""
    out = []
    if winreg is None:
        return out
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\SolidWorks")
    except OSError:
        return out
    try:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            if re.match(r"^SOLIDWORKS \d{4}$", name):
                out.append(name)
    finally:
        winreg.CloseKey(key)
    # 新版往前排（年份大的优先）
    out.sort(key=lambda s: int(s.split()[-1]), reverse=True)
    return out


def sw_exe_path(cfg_path=None):
    """找 SolidWorks 主程序 SLDWORKS.exe 的完整路径（找不到返回 None）。

    用途：配置了 allow_launch=true 时，需要"正常启动"SW 才能让它注册 COM。
    顺序：配置指定 → 快捷方式 → 注册表卸载路径 → 常见安装目录。
    """
    if cfg_path and Path(cfg_path).is_file():
        return str(cfg_path)
    for name, target, workdir in iter_shortcuts():
        blob = f"{name} {target} {workdir}".lower()
        if "solidworks" not in blob and "sldworks" not in blob:
            continue
        for cand in (target, os.path.join(workdir or "", "SLDWORKS.exe")):
            if cand and Path(cand).name.lower() == "sldworks.exe" and Path(cand).is_file():
                return str(cand)
    for loc in _uninstall_locations(r"SOLIDWORKS|SolidWorks"):
        base = Path(loc)
        for cand in (base / "SLDWORKS.exe", base / "SOLIDWORKS" / "SLDWORKS.exe",
                     base.parent / "SOLIDWORKS" / "SLDWORKS.exe"):
            if cand.is_file():
                return str(cand)
    # 最后兜底：在 Program Files 下浅层搜一遍（限深度 4、限 2000 个目录）
    for root in (r"C:\Program Files", r"C:\Program Files (x86)"):
        if not os.path.isdir(root):
            continue
        visited = 0
        stack = [(root, 0)]
        while stack:
            cur, depth = stack.pop()
            visited += 1
            if visited > 2000:
                break
            try:
                for e in os.scandir(cur):
                    if e.is_dir() and depth < 4:
                        stack.append((e.path, depth + 1))
                    elif e.is_file() and e.name.lower() == "sldworks.exe":
                        return e.path
            except OSError:
                continue
    return None


# ---------- 快捷方式（.lnk）解析 ----------

_SYSTEM_ROOTS = [
    r"c:\windows",
    r"c:\program files",
    r"c:\program files (x86)",
    r"c:\programdata",
    r"c:\users",
    r"c:\users\public",
]


def _norm(p):
    return os.path.normpath(str(p)).rstrip("\\/").lower()


def _is_broad_dir(p):
    """判断目录"太宽"，不适合递归扫描。

    这类目录（C:\\、C:\\Windows、C:\\Program Files、C:\\ProgramData、C:\\Users…）
    里不可能有 CAD 的运行流水账，但递归扫一遍要几十秒，必须排除。
    注意只做**精确匹配**，所以 `C:\\Users\\Public\\Documents` 仍然保留。
    """
    if not p:
        return True
    n = _norm(p)
    if re.match(r"^[a-z]:$", n):
        return True
    if n in _SYSTEM_ROOTS:
        return True
    if n.startswith("c:\\program files\\") or n.startswith("c:\\windows\\"):
        return True
    return False


def iter_shortcuts(limit=400):
    """遍历开始菜单/桌面的快捷方式，返回 [(名称, 目标, 工作目录), ...]。

    用"快捷方式的工作目录"是找 Creo trail 的关键：Creo 把 trail 写在**启动目录**，
    而启动目录正好记录在 .lnk 里（本机实测 = C:\\Users\\Public\\Documents）。
    """
    roots = [
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
        os.path.join(os.environ.get("PUBLIC", ""), "Desktop"),
    ]
    lnks = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.lower().endswith(".lnk"):
                    lnks.append(os.path.join(dirpath, fn))
                    if len(lnks) >= limit:
                        break
            if len(lnks) >= limit:
                break
    out = []
    if not lnks:
        return out
    try:
        import pythoncom
        import win32com.client
    except Exception:
        return out
    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        for path in lnks:
            sc = None
            try:
                sc = shell.CreateShortcut(path)
                out.append((Path(path).stem, sc.TargetPath or "", sc.WorkingDirectory or ""))
            except Exception:
                continue
            finally:
                del sc
        # 必须在 CoUninitialize 之前把 COM 引用放掉，否则退出时会刷一堆
        # "Win32 exception occurred releasing IUnknown" 噪音
        del shell
    except Exception:
        pass
    finally:
        pythoncom.CoUninitialize()
    return out


def creo_trail_dirs(cfg_dirs=None):
    """Creo trail 文件的候选目录（自动发现 + 配置兜底）。"""
    dirs = []

    def add(p):
        if p and not _is_broad_dir(p):
            p = str(p)
            if p not in dirs:
                dirs.append(p)

    for d in (cfg_dirs or []):
        add(d)
    # 1) 快捷方式的工作目录（Creo 把 trail 写在"启动目录"里）
    for name, target, workdir in iter_shortcuts():
        tl = (target or "").lower()
        if "parametric" in tl or "creo" in name.lower() or "creo" in tl:
            add(workdir)
    # 2) 注册表卸载项里的安装路径 → 归到"低优先级"（见 creo_install_dirs），
    #    因为 PTC 安装目录里自带一个样板 trail.txt.1，会冒充"最新 trail"
    # 3) config.pro 里的 trail_dir 设置
    for cp in _config_pro_paths():
        try:
            text = Path(cp).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r"^\s*trail_dir\s+(\S+)", text, re.M):
            add(m.group(1).strip('"'))
    # 4) 常见目录兜底
    home = os.environ.get("USERPROFILE", "")
    add(r"C:\Users\Public\Documents")
    add(home)
    add(os.path.join(home, "Documents"))
    add(os.path.join(home, "Desktop"))
    add(r"D:\CREO")
    add(r"E:\creo12")
    return [d for d in dirs if d]


def _config_pro_paths():
    """常见的 config.pro 位置。"""
    home = os.environ.get("USERPROFILE", "")
    return [
        os.path.join(home, "config.pro"),
        os.path.join(home, "Documents", "config.pro"),
        r"C:\Users\Public\Documents\config.pro",
    ]


def creo_install_dirs(cfg_dirs=None):
    """Creo 安装目录（**低优先级**候选）。

    为什么单独分出来：PTC 安装目录里自带一个样板 `trail.txt.1`，它不是真实操作记录。
    早期版本把安装目录和启动目录混在一起按"最新修改时间"挑文件，结果挂上了这个样板，
    表现为"流1 一直是空的"。现在只在别处都找不到 trail 时才回头看这里。
    """
    dirs = []

    def add(p):
        if p and not _is_broad_dir(p) and str(p) not in dirs:
            dirs.append(str(p))

    for d in (cfg_dirs or []):
        add(d)
    for loc in _uninstall_locations(r"Creo Parametric|Creo \d|PTC Creo"):
        add(loc)
        try:
            add(str(Path(loc) / "Parametric"))
        except Exception:
            pass
    return dirs


def nx_syslog_dirs(cfg_dirs=None):
    """NX syslog 文件的候选目录。"""
    import tempfile
    dirs = []

    def add(p):
        if p and str(p) not in dirs:
            dirs.append(str(p))

    for d in (cfg_dirs or []):
        add(d)
    add(tempfile.gettempdir())
    add(os.environ.get("TEMP", ""))
    add(os.environ.get("USERPROFILE", ""))
    for loc in _uninstall_locations(r"Siemens.*NX|Designcenter|NX \d"):
        add(loc)
        try:
            add(str(Path(loc) / "ProgramData"))
        except Exception:
            pass
    return [d for d in dirs if d]


def _uninstall_locations(name_pattern):
    """从注册表卸载项里找匹配软件的安装路径。"""
    out = []
    if winreg is None:
        return out
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    pat = re.compile(name_pattern, re.I)
    for hive, sub in roots:
        try:
            root = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    k = winreg.OpenKey(root, sub_name)
                except OSError:
                    continue
                try:
                    try:
                        disp = str(winreg.QueryValueEx(k, "DisplayName")[0])
                    except OSError:
                        continue
                    if not pat.search(disp):
                        continue
                    try:
                        loc = str(winreg.QueryValueEx(k, "InstallLocation")[0])
                    except OSError:
                        loc = ""
                    if loc:
                        out.append(loc)
                finally:
                    winreg.CloseKey(k)
        finally:
            winreg.CloseKey(root)
    return out


def modeling_apps(cfg_apps=None):
    """建模窗口清单：配置优先；配置为空则用内置清单。"""
    if cfg_apps:
        return cfg_apps
    return [dict(a) for a in DEFAULT_MODELING_APPS]
