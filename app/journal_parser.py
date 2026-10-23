# -*- coding: utf-8 -*-
"""
journal_parser.py —— NX Journal（录制脚本）解析器（2026-10-23 新增）

背景（问题清单 P0-1）：NX 的 *.syslog 是启动路由 / 崩溃诊断日志，里面没有
建模操作；真正记录"点了哪个菜单"的是 NX 的「录制日志（Record Journal）」
输出：.cs / .py / .vb 脚本。录制脚本里每条操作前都有注释行：

    //   Menu: Insert->Design Feature->Extrude...
    //   Menu: Insert->Curve->Rectangle...

以及大量 NXOpen API 调用（例如 NXOpen.Features.FeatureCollection.CreateExtrude）。

本模块只做**纯解析**：吃一个 journal 文件，吐出结构化事件。不依赖 NX、
不依赖第三方库，可单独单测（tests/fixtures/journal_sample.cs）。

事件 dict：
- menu: {"type": "menu", "menu": "Insert->Design Feature->Extrude...", "line_no": n}
- api:  {"type": "api", "class": "Features.FeatureCollection", "method": "CreateExtrude",
         "line_no": n}
"""
import re
from pathlib import Path

# "//   Menu: Insert->Design Feature->Extrude..."（// 与 Menu 之间空白任意）
MENU_RE = re.compile(r"//\s*Menu:\s*(.+?)\s*$")
# NXOpen.xxx.yyy 里的类路径与最后一个方法名（只认大驼峰方法名，避开属性）
API_RE = re.compile(r"\bNXOpen\.([A-Za-z0-9_.]+)\.([A-Z][A-Za-z0-9_]*)")
# 判断"这文件像不像 journal"用的特征串（任中一个即可）
JOURNAL_MARKERS = ("NXOpen.", "Menu:", "NXOpen.Session", "Journal")


def _decode(raw: bytes) -> str:
    """把字节解成文本：优先 BOM → 大量 NUL（UTF-16）→ UTF-8 → GBK 兜底。"""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw.count(b"\x00") > max(8, len(raw) // 8):
        # 无 BOM 的 UTF-16：按小端解（NX 在中文 Windows 上默认 UTF-16 LE）
        return raw.decode("utf-16-le", errors="replace")
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_journal_text(path) -> str:
    """读 journal 全文（自动识别 UTF-16 / UTF-8）。"""
    return _decode(Path(path).read_bytes())


def parse_journal_line(line: str):
    """解析一行 journal；解析不出返回 None。纯函数，可单测。

    只认两类：
    1. 注释行里的 `Menu: ...`（用户点了哪个菜单/命令）；
    2. NXOpen API 调用（脚本里真正执行的类.方法）。
    其余（变量声明、注释头、空行）忽略。
    """
    if not line:
        return None
    m = MENU_RE.search(line)
    if m:
        menu = m.group(1).strip()
        return {"type": "menu", "menu": menu} if menu else None
    m = API_RE.search(line)
    if m:
        return {"type": "api", "class": m.group(1), "method": m.group(2)}
    return None


def parse_journal(path):
    """吃一个 journal 文件，返回事件列表（按行号升序）。"""
    events = []
    for i, line in enumerate(read_journal_text(path).splitlines(), 1):
        ev = parse_journal_line(line)
        if ev:
            ev["line_no"] = i
            events.append(ev)
    return events


def looks_like_journal(path, probe_bytes=8192) -> bool:
    """只读文件头部若干字节，判断内容像不像 NX journal（编码无关）。"""
    try:
        raw = Path(path).read_bytes()[:probe_bytes]
    except OSError:
        return False
    if not raw:
        return False
    text = _decode(raw)
    return any(marker in text for marker in JOURNAL_MARKERS)


def find_latest_journal(dirs, explicit=None, globs=("*.cs", "*.py", "*.vb")):
    """找最新的、内容像 journal 的文件。

    - explicit（config nx.journal_path）优先：指定了就只用它，不做猜测；
    - 否则在 dirs 里按扩展名粗筛，再用内容特征过滤，取 mtime 最新的。
    """
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    best, best_mtime = None, -1.0
    for d in dirs or []:
        base = Path(d)
        if not base.is_dir():
            continue
        for pat in globs:
            try:
                files = list(base.glob(pat))
            except OSError:
                continue
            for p in files:
                try:
                    if not p.is_file() or p.stat().st_size <= 0:
                        continue
                    mtime = p.stat().st_mtime
                    if mtime <= best_mtime:
                        continue
                    if looks_like_journal(p):
                        best, best_mtime = p, mtime
                except OSError:
                    continue
    return best
