# -*- coding: utf-8 -*-
"""
sw_shortcuts.py —— SW 快捷键/笔势映射表（v2.x）

数据来源（按优先级）：
1. data\\sw_shortcut_map.json —— 用户/探测脚本写出的映射文件（最高优先）；
2. 注册表 —— SW 的笔势映射（HKCU\\...\\Mouse Gestures\\{Part,Assembly,Drawing,Sketch}），
   每个方向存的是"命令 ID"数字（**版本号自动发现**：枚举 HKCU 下所有 SOLIDWORKS 年份键，
   不再写死 2022）；
3. app\\sw_command_ids.json —— 由 SW 类型库解析出来的"命令ID→命令名"字典
   （probe 生成，约 3600 条），把注册表里的数字翻译成命令名；
4. 内置的常见默认快捷键表（兜底）。

对外两个查询：
- key_command(键名, 修饰键列表) → 命令名字符串或 None
- gesture_command(方向中文名, 上下文) → {"id": int, "name": str|None} 或 None
"""
import json
import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # 非 Windows 环境（纯逻辑单测）也能导入
    winreg = None

import cad_discovery

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
