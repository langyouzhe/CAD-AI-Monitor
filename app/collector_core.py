# -*- coding: utf-8 -*-
"""
collector_core.py —— 核心模块：配置 + 本地存储（v2）

v2 与 v1 的区别（按 2026-10-10 最新版项目上下文改）：
1. 不再单独记录"停顿"：每条记录都带时间戳，停顿 = 两条记录的时间差，想算随时算；
2. 每条事件多一列 source（来源）：
     stream1 = 软件接口报出的模型变化（"我做了什么"）
     stream2 = 输入监听记下的操作（"我当时怎么点的"）
     meta    = 会话边界标记（开始/停止/离开/回来）
3. 只盯建模软件窗口：非建模窗口在前台时流2一律装聋不记（判定在 input_listener.py）；
4. 无操作超过 N 分钟（默认 5，设置项可改）记"离开"，回来第一次操作前补记"回来"。

数据全部只存在本地文件里，不会联网、不会上传。
"""
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cad_discovery

# ---------- 版本号 ----------

# 公开发布版本（语义化版本 SemVer）：0.x = 还没正式发布、接口随时可能改；
# 等觉得"别人可以放心用了"再发 1.0.0。
# 注意：文档/注释里出现的 v2、v3 是**开发内部迭代编号**（第 2 代架构、第 3 代加视觉），
# 不是发布版本号——两者不要混。
APP_VERSION = "0.1.1"

# ---------- 配置默认值 ----------

DEFAULT_CONFIG = {
    "version": 2,
    # 哪些窗口算"建模软件窗口"：exe 完全匹配，或窗口标题包含 title 关键词
    # 内置主流 CAD 清单（开源通用版）：SW / Creo / NX / Inventor / AutoCAD / Solid Edge /
    # FreeCAD / Fusion…；想加自己的软件就在这里加一行。
    "modeling_apps": cad_discovery.modeling_apps(),
    # 无操作超过这么多分钟 → 记一次"离开"（回来第一次操作前自动补"回来"）
    "idle_minutes": 5.0,
    # 流1（软件接口）设置：默认启用三个后端，每个后端没连上时会安静重试
    "stream1": {
        "enabled": True,
        "backends": ["solidworks", "creo", "nx"],  # 想关哪个就把哪个删掉
        "backend": "solidworks",           # 兼容旧配置字段
        "prog_id": "",                     # SW COM 注册名：**留空 = 自动发现**
                                           # （优先不限版本的 SldWorks.Application，
                                           #   再枚举 SldWorks.Application.NN 按版本倒序）
                                           # 一般不用填；填了就以填的为准
        "allow_launch": False,             # false = 只连接已打开的 SW，不自己启动它
        "poll_interval_s": 1.0,            # 每隔几秒对 SW 做一次"变化比对"
    },
    # Creo 后端：解析 trail 操作记录 + config.pro 里的 mapkey 定义
    "creo": {
        "enabled": True,
        "trail_dirs": [],                  # 留空 = 自动发现（快捷方式的启动目录 / 用户目录 / config.pro）
        "config_pro_paths": [],            # config.pro 路径；留空 = 自动找
        "poll_interval_s": 1.0,
        "stale_minutes": 10,               # trail 超过这么久没更新就提示"流1 可能收不到"
    },
    # NX(Designcenter) 后端：探测版——解析 NX syslog 操作日志
    "nx": {
        "enabled": True,
        "syslog_dirs": [],                 # 留空 = 自动发现（%TEMP% / 用户目录 / 注册表安装路径）
        "poll_interval_s": 1.0,
    },
    # 笔势（按住鼠标键拖动，如 SW 右键笔势导引）记录
    "gestures": {
        "enabled": True,
        "min_distance_px": 20,             # 拖动小于这个距离不算笔势（算普通点击）
        "record_polyline": True,           # 是否记录途经点（用于以后分析犹豫/抖动）
        "max_points": 60,                  # 途经点最多记多少个
    },
    # AI 眼（v3 规格的视觉维度）：点击/选中变化/快捷键/笔势触发 →
    # 截整个建模窗口一张图 → BYOK 云端多模态识别 → 结论挂原事件时间戳落库
    "ai_eye": {
        "enabled": False,                  # 填好 api_key 后再打开（BYOK：不填=整体关闭，零成本）
        "provider": "mock",                # mock=假识别(测试) / zhipu / qwen / doubao / custom
        "api_base": "",                    # OpenAI 兼容接口地址；留空=用服务商预设；custom 必填
        "api_key": "",                     # ← 你自己注册服务后填这里
        "model": "",                       # 留空 = 用该服务商的默认视觉模型
        "max_tokens": 4000,                # 回答额度：推理型模型（如 deepseek-flash）先思考再回答，
                                           # 给太小会把额度耗在思考上、正式回答变空 → 默认给足
        "http_timeout_s": 90,              # 单次云端调用超时（推理模型可能要十几秒）
        "delay_s": 0.15,                   # 触发后等多久再截（等菜单/对话框弹出来）
        "save_images": True,               # 图存本地 data\\images\\（攒"特征视觉图鉴"）；
                                            # false = 截图只在内存里转一圈，磁盘不留图（省空间）
        "triggers": {
            "click_left": True,            # 左键点击触发
            "click_right": False,          # 右键点击触发（默认关）
            "selection_changed": True,     # 流1 报出"选中特征变化"时触发（v3 规格）
            "shortcut": True,              # Ctrl/Alt 组合键触发
            "bare_letter": True,           # 单个字母键（SW 裸键快捷键）触发
            "gesture": True,               # 笔势触发
        },
        "rate_per_min": 60,                # 限流：每分钟最多截几张
        "max_queue": 20,                   # 待识别队列上限，满了丢弃并计数
        "dedup_window_s": 0.5,             # 连点合并：这个秒数内的重复触发只截一次
    },
    # 三方对账：停止采集时自动把"缺票"标成 meta/anomaly 异常事件
    "reconcile": {
        "enabled": True,
        "window_ms": 2000,      # 动作之后多久内的流1 事件算它的"效果"
        "ignore_middle": True,  # 中键/滚轮不算动作（旋转视图本来就没有流1 事件）
    },
    # 调试开关：true 时所有窗口都记录（自检用，平时保持 false）
    "debug": {"allow_all_windows": False},
}


def now_ms() -> int:
    """当前时间（毫秒，从 1970 年算起），用来计算两次操作间隔。"""
    return int(time.time() * 1000)


def now_iso() -> str:
    """当前时间（人看得懂的格式，例如 2026-10-10 02:30:00.123+08:00）。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _iso_from_ms(ts_ms: int) -> str:
    """把毫秒时间戳转成人读格式（与 now_iso 同风格）。"""
    return datetime.fromtimestamp(ts_ms / 1000.0).astimezone().isoformat(
        timespec="milliseconds")


def resolve_data_dir() -> Path:
    """决定数据存在哪个文件夹（开源通用版：不再写死 E 盘）。

    规则（从高到低）：
    1. 环境变量 `CAD_AI_MONITOR_DATA` 指定的目录（服务器/多份数据时用）；
    2. 开发机：源码仓库就在 E:\\CAD-AI-Monitor 时，用仓库里的 data
       （保证老用户已有的采集数据还能被找到）；
    3. 打包成 exe 后：
       a. exe 旁边如果有 data 文件夹 → 用它（**绿色便携模式**，U 盘可用）；
       b. 否则用 `文档\\CAD-AI-Monitor\\data`（装在 Program Files 里也能写）；
    4. 源码运行：用仓库里的 data。
    """
    env = os.environ.get("CAD_AI_MONITOR_DATA")
    if env:
        return Path(env).expanduser()
    legacy_repo = Path("E:/CAD-AI-Monitor")
    if (legacy_repo / "app" / "collector_core.py").is_file():
        return legacy_repo / "data"
    if getattr(sys, "frozen", False):  # 打包成 exe 之后走这里
        exe_dir = Path(sys.executable).resolve().parent
        portable = exe_dir / "data"
        if portable.is_dir():
            return portable
        docs = Path.home() / "Documents"
        if docs.is_dir():
            return docs / "CAD-AI-Monitor" / "data"
        return portable
    return Path(__file__).resolve().parent.parent / "data"


class ConfigManager:
    """配置管理：读 data\\config.json；没有就写一份默认的。

    用户在设置文件里改的项，点【开始采集】时重新读入生效。
    """

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "config.json"
        self._values = None

    def load(self) -> dict:
        """读配置：文件不存在→写默认；坏了→备份后写默认；缺的字段→默认补上。"""
        if self.path.exists():
            try:
                user_cfg = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(user_cfg, dict):
                    raise ValueError("config 不是对象")
            except Exception:
                # 配置被改坏：先备份再写默认，别让用户设置白丢
                try:
                    backup = self.path.with_suffix(".json.bad")
                    self.path.replace(backup)
                except Exception:
                    pass
                user_cfg = {}
        else:
            user_cfg = {}
        merged = self._merge(DEFAULT_CONFIG, user_cfg)
        self._values = merged
        self.ensure_file()
        return merged

    @staticmethod
    def _merge(default: dict, user: dict) -> dict:
        """把用户写的配置叠在默认配置上（缺什么补什么，不改用户已有的）。"""
        out = dict(default)
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = ConfigManager._merge(out[key], value)
            else:
                out[key] = value
        return out

    def ensure_file(self):
        """确保设置文件存在（用户点【打开设置】时能找到它）。"""
        try:
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception:
            pass

    @property
    def values(self) -> dict:
        return self._values or self.load()


class EventStore:
    """本地数据库。一个 events.db 文件就是全部数据。

    v2 表结构（events 表多了一列 source）：
      id, session_id, seq, ts_ms, ts_iso, source, kind, detail
    遇到 v1 的旧库（没有 source 列）会自动改名备份、再建新表，旧数据不丢。
    """

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：允许采集线程写、界面线程读
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")  # 更稳、更快
        self._lock = threading.Lock()
        self._migrate_if_needed()
        self._create_tables()

    def _migrate_if_needed(self):
        """v1 旧库 → 改名备份，新表重新建。"""
        with self._lock:
            cols = [
                r[1]
                for r in self._conn.execute("PRAGMA table_info(events)").fetchall()
            ]
        if cols and "source" not in cols:
            backup = self.db_path.with_name("events.v1.backup.db")
            n = 1
            while backup.exists():
                backup = self.db_path.with_name(f"events.v1.backup{n}.db")
                n += 1
            self._conn.close()
            self.db_path.rename(backup)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")

    def _create_tables(self):
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at  TEXT NOT NULL,          -- 开始时间（人读格式）
                    stopped_at  TEXT,                   -- 停止时间（录完后填）
                    event_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,        -- 属于哪一次采集
                    seq        INTEGER NOT NULL,        -- 这次采集里的第几条
                    ts_ms      INTEGER NOT NULL,        -- 时间戳（毫秒，机器读）
                    ts_iso     TEXT    NOT NULL,        -- 时间戳（人读格式）
                    source     TEXT    NOT NULL,        -- stream1 / stream2 / meta
                    kind       TEXT    NOT NULL,        -- 事件类型（见 README）
                    detail     TEXT    NOT NULL         -- 事件细节（JSON 文本）
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq)"
            )

    def start_session(self) -> int:
        """开一次新采集，返回这次采集的编号。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO sessions(started_at) VALUES (?)", (now_iso(),)
            )
            return cur.lastrowid

    def add_event(self, session_id, seq, source, kind, detail: dict, ts_ms: int):
        """记一条事件。detail 是字典，存进去之前转成 JSON 文本。

        注意：ts_iso 由 ts_ms 换算而来（AI 眼结果会挂过去的时间戳，
        两列必须对应同一点时间，不能一个是过去一个是现在）。
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events(session_id, seq, ts_ms, ts_iso, source, kind, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, seq, ts_ms, _iso_from_ms(ts_ms), source, kind,
                 json.dumps(detail, ensure_ascii=False)),
            )

    def end_session(self, session_id: int, event_count: int):
        """采集结束：补上停止时间和事件总数。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET stopped_at = ?, event_count = ? WHERE id = ?",
                (now_iso(), event_count, session_id),
            )

    def query_session(self, session_id: int):
        """把某次采集的全部事件按顺序读出来（测试和以后的分析都用它）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts_ms, ts_iso, source, kind, detail FROM events "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [{"seq": r[0], "ts_ms": r[1], "ts_iso": r[2], "source": r[3],
                 "kind": r[4], "detail": json.loads(r[5])} for r in rows]

    def wal_checkpoint(self):
        """把 WAL 里还没并进主库的内容合并回 events.db（best-effort，绝不抛异常）。

        为什么需要：SQLite 用 WAL 模式时，新数据先写在 events.db-wal 这个旁边的小文件里。
        如果程序是被强杀的（或者只把 events.db 单独拷走），数据就散在两个文件里。
        每次停止采集时合并一次，保证"events.db 一个文件 = 完整数据"。
        """
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    def close(self):
        self.wal_checkpoint()
        with self._lock:
            self._conn.close()
