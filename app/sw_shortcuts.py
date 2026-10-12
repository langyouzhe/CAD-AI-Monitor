# -*- coding: utf-8 -*-
"""
sw_shortcuts.py —— SW 快捷键/笔势映射表（v2.x）

数据来源（按优先级）：
1. data\\sw_shortcut_map.json —— 用户/探测脚本写出的映射文件（最高优先）；
2. 注册表 —— SW 的笔势映射（HKCU\\...\\Mouse Gestures\\{Part,Assembly,Drawing,Sketch}），
   每个方向存的是"命令 ID"数字（**版本号自动发现**：枚举 HKCU 下所有 SOLIDWORKS 年份键，
   不再写死 2022）；
3. app\\sw_command_ids.json —— 由 SW 类型库解析出来的"命令ID→命令名"字典
   （**本机生成、不在仓库里分发**：程序第一次运行时从你电脑上装的 SolidWorks 类型库现场生成，
   见 ensure_command_ids）；
4. 内置的常见默认快捷键表（兜底）。

对外两个查询：
- key_command(键名, 修饰键列表) → 命令名字符串或 None
- gesture_command(方向中文名, 上下文) → {"id": int, "name": str|None} 或 None
"""
import json
import sys
import threading
from pathlib import Path

try:
    import winreg
except ImportError:  # 非 Windows 环境（纯逻辑单测）也能导入
    winreg = None

import cad_discovery

# ---------- 命令 ID 表：本机生成，不在仓库分发 ----------
#
# 为什么这样做：注册表里的笔势存的是"命令 ID"数字，要翻成命令名需要一张
# ID→名字对照表，而这张表来自 SolidWorks 自己的类型库（swcommands.tlb）——
# 属于厂商软件内容，**再分发有法律争议**，所以：
#   · 仓库里不放这张表；
#   · 程序第一次运行时，从**用户自己电脑上装的** SolidWorks 现场生成，存到 data\；
#   · 没装 SW 或生成失败 → 只是笔势没有中文名（原始 ID 照记），不影响采集。
#
# 实测提醒：SW2022 的类型库编号与注册表里存的旧编号对不上，所以本机翻不出名字，
# 这属于已知限制，不是 bug（见 README「已知限制」）。

COMMAND_IDS_FILE = "sw_command_ids.json"
_TLB_NAME = "swcommands.tlb"
_gen_lock = threading.Lock()
_gen_attempted = False


def find_command_tlb():
    """找本机 SolidWorks 的 swcommands.tlb；找不到返回 None。"""
    import cad_discovery
    try:
        exe = cad_discovery.sw_exe_path()
    except Exception:
        exe = None
    if not exe:
        return None
    p = Path(exe).parent / _TLB_NAME
    return p if p.is_file() else None


def build_command_ids_from_tlb(tlb_path):
    """解析类型库里的枚举常量 → {命令ID: 常量名}。失败返回 {}（绝不抛异常）。"""
    try:
        import pythoncom
    except Exception:
        return {}
    out = {}
    pythoncom.CoInitialize()
    try:
        lib = pythoncom.LoadTypeLib(str(tlb_path))
        for i in range(lib.GetTypeInfoCount()):
            try:
                ti = lib.GetTypeInfo(i)
                ta = ti.GetTypeAttr()
            except Exception:
                continue
            if getattr(ta, "typekind", None) != 0:   # 0 = TKIND_ENUM
                continue
            for j in range(ta.cVars):
                try:
                    vd = ti.GetVarDesc(j)
                    names = ti.GetNames(vd.memid)
                except Exception:
                    continue
                name = names[0] if names else None
                val = getattr(vd, "value", None)
                if name and isinstance(val, int) and val > 0:   # 负数是占位常量，丢掉
                    out[int(val)] = name
    except Exception:
        return {}
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
    return out


def ensure_command_ids(data_dir):
    """确保有"命令ID→命令名"表：已有就加载；没有就从本机 SW 类型库生成。

    返回 {id: name} 字典；没有 SW 或生成失败返回 None。**只在首次调用时尝试生成。**
    """
    global _gen_attempted
    path = Path(data_dir) / COMMAND_IDS_FILE
    ids = _read_command_ids(path)
    if ids:
        return ids
    with _gen_lock:
        if _gen_attempted:
            return None
        _gen_attempted = True
        tlb = find_command_tlb()
        if not tlb:
            return None
        ids = build_command_ids_from_tlb(tlb)
        if not ids:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({str(k): v for k, v in ids.items()},
                                       ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return ids


def _read_command_ids(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {int(k): v for k, v in data.items()}
    except Exception:
        return {}


# SW 常见默认快捷键（兜底表；用户自定义的以映射文件为准）
BUILTIN_KEY_MAP = {
    "ctrl+s": "保存",
    "ctrl+o": "打开",
    "ctrl+n": "新建",
    "ctrl+p": "打印",
    "ctrl+z": "撤销",
    "ctrl+y": "重做",
    "ctrl+c": "复制",
    "ctrl+v": "粘贴",
    "ctrl+x": "剪切",
    "ctrl+a": "全选",
    "ctrl+f": "查找",
    "ctrl+r": "重建",
    "ctrl+q": "强制重建",
    "ctrl+b": "重建模型",
    "s": "快捷方式栏",
    "r": "上一个视图",
    "f": "整屏显示全图",
    "g": "放大镜",
}

# 注册表值名 → 笔势方向
REG_DIRECTION_NAMES = {
    "gestureLeft": "左",
    "gestureRight": "右",
    "gestureUp": "上",
    "gestureDown": "下",
    "gestureDiagUpLeft": "左上",
    "gestureDiagUpRight": "右上",
    "gestureDiagDownLeft": "左下",
    "gestureDiagDownRight": "右下",
}

# 文档类型 → 注册表子键名（SW 文档类型：1=零件 2=装配 3=工程图）
CONTEXT_KEYS = {1: "Part", 2: "Assembly", 3: "Drawing", None: "Part"}


class ShortcutMap:
    """快捷键 + 笔势两张映射表的加载与查询。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.app_dir = Path(__file__).resolve().parent
        self.key_map = dict(BUILTIN_KEY_MAP)
        self.gesture_map = {}     # {context: {方向: 命令ID}}
        self.id2name = {}         # {命令ID: 命令名}
        self.source = "builtin"
        self._load_registry_gestures()   # 先读注册表（SW 自己的设置）
        self._load_json_override()       # 用户/探测文件最后读，优先级最高
        self._load_command_ids()

    def _load_json_override(self):
        p = self.data_dir / "sw_shortcut_map.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                self.key_map.update(data.get("key_map", {}))
                for ctx, m in (data.get("gesture_map") or {}).items():
                    if isinstance(m, dict):
                        self.gesture_map.setdefault(ctx, {}).update(m)
                self.source = str(p)
            except Exception:
                pass

    def _load_registry_gestures(self):
        """从注册表读笔势映射：**枚举所有 SW 版本键**（新版优先），跨版本合并。"""
        if winreg is None:
            return
        for base in self._registry_gesture_bases():
            self._read_gesture_base(base)

    @staticmethod
    def _registry_gesture_bases():
        """所有可能存放笔势设置的注册表路径（新版排前面）。"""
        bases = []
        for ver in cad_discovery.sw_registry_versions():
            bases.append(r"Software\SolidWorks\%s\User Interface\Settings\Mouse Gestures" % ver)
        if not bases:
            # 兜底：注册表里读不到版本键时，仍试一下不限版本的老路径
            bases.append(r"Software\SolidWorks\User Interface\Settings\Mouse Gestures")
        return bases

    def _read_gesture_base(self, base):
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
        except OSError:
            return
        try:
            i = 0
            contexts = []
            while True:
                try:
                    contexts.append(winreg.EnumKey(root, i))
                    i += 1
                except OSError:
                    break
        finally:
            winreg.CloseKey(root)
        for ctx in contexts:
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + "\\" + ctx)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    direction = REG_DIRECTION_NAMES.get(name)
                    if direction and isinstance(value, int) and value != 0:
                        # setdefault：多个版本键都配了时，以新版（先遍历的）为准
                        self.gesture_map.setdefault(ctx, {}).setdefault(direction, value)
                    i += 1
            finally:
                winreg.CloseKey(key)

    def _load_command_ids(self):
        candidates = [self.app_dir / "sw_command_ids.json",
                      self.data_dir / "sw_command_ids.json"]
        # 打包成 exe 后：PyInstaller 的解包目录（--add-data 放这里）和 exe 所在目录
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "sw_command_ids.json")
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent / "sw_command_ids.json")
        for p in candidates:
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    self.id2name = {int(k): v for k, v in data.items()}
                    return
                except Exception:
                    continue

    # ---------- 查询 ----------

    def key_command(self, key_name, mods):
        """按键 → 命令名，查不到返回 None。优先组合键写法（ctrl+d），再裸键。"""
        if mods:
            combo = "+".join(mods + [str(key_name)])
            if combo in self.key_map:
                return self.key_map[combo]
        if key_name in self.key_map:
            return self.key_map[key_name]
        return None

    def gesture_command(self, direction, doc_type=None):
        """笔势方向 + 文档类型 → {"id", "name"} 或 None。

        doc_type: SW 文档类型（1 零件 / 2 装配 / 3 工程图）；None 按零件查。
        """
        ctx = CONTEXT_KEYS.get(doc_type, "Part")
        val = (self.gesture_map.get(ctx) or {}).get(direction)
        if val is None:
            return None
        if isinstance(val, str):
            # 用户映射文件里直接写了命令名
            return {"id": None, "name": val}
        gid = val
        name = self.id2name.get(gid)
        # 名字里带枚举前缀的话，只留最后一段（swCommands_xxx → xxx）
        if name and "." in name:
            name = name.rsplit(".", 1)[-1]
        if name and name.startswith("swCommands_"):
            name = name[len("swCommands_"):]
        return {"id": gid, "name": name}


if __name__ == "__main__":
    # 手动生成命令 ID 表（想提前建好的话）：
    #     python app\sw_shortcuts.py --generate [数据目录]
    _dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data")
    _tlb = find_command_tlb()
    print(f"类型库: {_tlb}")
    if _tlb is None:
        print("没找到 swcommands.tlb（本机装了 SolidWorks 才会有）")
        sys.exit(1)
    _ids = ensure_command_ids(_dir)
    if not _ids:
        print("生成失败")
        sys.exit(1)
    print(f"生成成功：{len(_ids)} 条 → {Path(_dir) / COMMAND_IDS_FILE}")
    sys.exit(0)
