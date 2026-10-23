# -*- coding: utf-8 -*-
"""
collector_core.py —— 核心模块：配置 + 会话文件夹 + 本地存储（v4，v0.2.0）

v4 与 v3 的区别（按《项目上下文》第 6 节「会话存储规格（v4）」定案终版）：
1. 一次【开始采集】→【停止采集】= **一个独立会话文件夹**：
       data\\sessions\\<yyyy-mm-dd_HHMMSS>\\        （本地时间；重名自动加 _2、_3）
   文件夹里固定三样东西：
       images\\            本次 AI 眼截图（直接放，不再按日期分层）
       data\\events.db     本次采集的数据（表结构不变：sessions + events）
       session.json        不开库就能扫的摘要（版本号/开始停止时间/软件/事件数/schema 版本）
2. 空会话不留痕：正常停止且"除开始/停止标记外一条事件都没有"→ 停止时删除整个会话文件夹；
3. 崩溃半截会话：启动时扫描 data\\sessions\\，凡 sessions.stopped_at 为空的一律**保留**，
   做 WAL checkpoint 并在 session.json 标 status="aborted"；只清理"没有 db 也没有任何文件的空目录"；
4. 旧数据整包归档：旧的 data\\events.db + data\\images\\ 一起移到 data\\legacy\\
   （库改名 events_legacy.db，图保持相对位置 legacy\\images\\，库里的图片绝对路径同步改写，不断链）；
5. 单实例锁：程序已在运行时，第二个实例直接退出，禁止两个实例同时写数据；
6. 跨会话分析：不再假设只有一个 data\\events.db，改为遍历 data\\sessions\\*\\data\\events.db
   （iter_session_databases / iter_all_events / session_summaries，命令行见文件末尾）。

数据全部只存在本地文件里，不会联网、不会上传。
"""
import argparse
import ctypes
import json
import os
import shutil
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
# 注意：文档/注释里出现的 v2、v3、v4 是**开发内部迭代编号**（第 2 代架构、第 3 代加视觉、
# 第 4 代会话存储），不是发布版本号——两者不要混。
APP_VERSION = "0.2.0"

# events.db 的表结构版本（表结构不变时这个数字不动）
SCHEMA_VERSION = 2
# 存储布局版本（v4 = 每次采集独立会话文件夹）
STORAGE_VERSION = 4

# ---------- v4 会话文件夹约定 ----------

SESSIONS_DIRNAME = "sessions"          # data\\sessions\\
LEGACY_DIRNAME = "legacy"              # data\\legacy\\（旧数据整包归档）
SESSION_DATA_DIRNAME = "data"          # 会话里的数据子文件夹：<会话>\\data\\
SESSION_IMAGES_DIRNAME = "images"      # 会话里的截图子文件夹：<会话>\\images\\
SESSION_DB_FILENAME = "events.db"      # 会话里的数据库：<会话>\\data\\events.db
SESSION_META_FILENAME = "session.json"  # 会话摘要：<会话>\\session.json
LEGACY_DB_FILENAME = "events_legacy.db"  # 旧库归档后的名字：data\\legacy\\events_legacy.db
LEGACY_LOG_FILENAME = "migration.log"   # 迁移日志：data\\legacy\\migration.log（每次启动可查）
SESSION_NAME_FORMAT = "%Y-%m-%d_%H%M%S"  # 会话文件夹名（本地时间，年月日_时分秒）

# 单实例锁（Windows 命名互斥体；Local = 当前登录用户会话内唯一）
DEFAULT_INSTANCE_NAME = "Local\\CAD-AI-Collector"
ERROR_ALREADY_EXISTS = 183

# 这两个是"会话边界标记"，不算用户数据：空会话判定、事件计数都要排除它们
BOUNDARY_KINDS = ("session_start", "session_stop")

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
                                           #   再枚举 SldWorks.Application.NN 按版本降序）
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
    # NX(Designcenter) 后端：读 NX「录制日志（Record Journal）」输出的 .cs/.py/.vb
    # （2026-10-23 换数据源：syslog 是崩溃诊断日志，不含建模操作）
    "nx": {
        "enabled": True,
        "journal_path": "",                # ← 指定 journal 文件（最稳）；留空=自动找
        "journal_dirs": [],                # 留空 = 默认 桌面/文档/下载/data\journals
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
        "max_tokens": 4000,                # 回答额度：推理型多模态模型会先"思考"再回答，
                                           # 给太小会把额度耗在思考上、正式回答变空 → 默认给足
        "http_timeout_s": 90,              # 单次云端调用超时（推理模型可能要十几秒）
        "delay_s": 0.15,                   # 触发后等多久再截（等菜单/对话框弹出来）
        "save_images": True,               # 图存**本次会话**的 images\\（攒"特征视觉图鉴"）；
                                           # false = 截图只在内存里转一圈，磁盘不留图（省空间）
        "drain_timeout_s": 5.0,            # 停止采集时，等在途云端请求做完的上限（秒）；
                                           # 排空后再关库，识别结果才能归入本次会话
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


# ======================================================================
# 会话文件夹（v4）
# ======================================================================

def sessions_root(data_dir) -> Path:
    """所有会话文件夹的根目录：data\\sessions\\。"""
    return Path(data_dir) / SESSIONS_DIRNAME


def session_name(when=None) -> str:
    """会话文件夹的默认名字：本地时间 `2026-10-19_122035`。"""
    return (when or datetime.now()).strftime(SESSION_NAME_FORMAT)


def create_session_dir(data_dir, when=None) -> Path:
    """新建一个会话文件夹并返回路径（重名自动加 _2、_3）。

    注意：这里只创建会话文件夹本身，里面的 images\\ 和 data\\ 由调用方创建。
    并发/重名用"mkdir 必须成功"来判重（不用先 exists 再 mkdir，避免竞态）。
    """
    root = sessions_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    base = session_name(when)
    candidate = root / base
    n = 2
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = root / f"{base}_{n}"
            n += 1


def session_data_dir(session_dir) -> Path:
    """会话里的数据子文件夹：<会话>\\data\\。"""
    return Path(session_dir) / SESSION_DATA_DIRNAME


def session_images_dir(session_dir) -> Path:
    """会话里的截图子文件夹：<会话>\\images\\。"""
    return Path(session_dir) / SESSION_IMAGES_DIRNAME


def session_db_path(session_dir) -> Path:
    """会话里的数据库路径：<会话>\\data\\events.db。"""
    return session_data_dir(session_dir) / SESSION_DB_FILENAME


def session_meta_path(session_dir) -> Path:
    """会话摘要文件路径：<会话>\\session.json。"""
    return Path(session_dir) / SESSION_META_FILENAME


def prepare_session_dir(data_dir, when=None) -> Path:
    """新建会话文件夹并建好 images\\ 与 data\\ 两个子文件夹（开始采集时调用）。"""
    session_dir = create_session_dir(data_dir, when=when)
    session_images_dir(session_dir).mkdir(parents=True, exist_ok=True)
    session_data_dir(session_dir).mkdir(parents=True, exist_ok=True)
    return session_dir


def write_session_json(session_dir, info: dict) -> Path:
    """原子地写 session.json（先写 .tmp 再改名，崩溃也不会留下半个 json）。"""
    path = session_meta_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))
    return path


def read_session_json(session_dir):
    """读 session.json；不存在或坏了返回 None（摘要文件坏了不能挡住开库）。"""
    path = session_meta_path(session_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def delete_session_dir(session_dir) -> bool:
    """删除整个会话文件夹（空会话不留痕）。返回是否删掉了。"""
    try:
        shutil.rmtree(str(session_dir))
        return True
    except OSError:
        return False


def iter_session_dirs(data_dir):
    """按名字顺序列出 data\\sessions\\ 下的会话文件夹（只列文件夹）。"""
    root = sessions_root(data_dir)
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)


def iter_session_databases(data_dir):
    """遍历 `data\\sessions\\*\\data\\events.db`（跨会话分析统一入口）。

    生成 (会话文件夹, 数据库路径)，跳过还没建出 db 的半截文件夹。
    """
    for session_dir in iter_session_dirs(data_dir):
        db = session_db_path(session_dir)
        if db.is_file():
            yield session_dir, db


def _readonly_uri(db_path) -> str:
    """SQLite 只读 URI：分析旧库/别人的库时不写、不建 -wal。"""
    return Path(db_path).resolve().as_uri() + "?mode=ro"


def _read_events(db_path):
    """只读地把一个会话库的全部事件读出来（字段与 EventStore.query_session 一致）。"""
    conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
    try:
        rows = conn.execute(
            "SELECT seq, ts_ms, ts_iso, source, kind, detail FROM events ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()
    return [{"seq": r[0], "ts_ms": r[1], "ts_iso": r[2], "source": r[3],
             "kind": r[4], "detail": json.loads(r[5])} for r in rows]


def iter_all_events(data_dir):
    """跨会话遍历：按会话顺序把每个会话的事件都吐出来，每条多带 session 字段。

    这就是"跨会话分析"的指定口径：**遍历 data\\sessions\\*\\data\\events.db**，
    不再假设所有数据都在一个 data\\events.db 里。
    """
    for session_dir, db in iter_session_databases(data_dir):
        try:
            events = _read_events(db)
        except Exception:
            continue  # 库坏了就跳过这个会话，不能让整个分析挂掉
        for ev in events:
            ev["session"] = session_dir.name
            ev["session_dir"] = str(session_dir)
            yield ev


def _inspect_session_db(db_path):
    """打开一个会话库，读摘要（会顺便做 WAL checkpoint）。读不了返回 None。"""
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return None
    try:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        rows = conn.execute(
            "SELECT id, started_at, stopped_at, event_count FROM sessions ORDER BY id"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT source, COUNT(*) FROM events GROUP BY source").fetchall()}
        return {
            "session_rows": len(rows),
            "started_at": rows[0][1] if rows else None,
            "stopped_at": rows[-1][2] if rows else None,
            # 有 stopped_at 才算正常结束；一行都没有 = 半截会话
            "all_stopped": bool(rows) and all(r[2] for r in rows),
            "event_count": total,
            "counts": counts,
        }
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def session_summaries(data_dir):
    """扫一遍 data\\sessions\\，给每个会话一份摘要（优先读 session.json，不开库）。

    返回列表，每项：session（文件夹名）、path、status、started_at、stopped_at、
    event_count、has_db、has_images。**不需要打开数据库**就能看全部会话。
    """
    out = []
    for session_dir in iter_session_dirs(data_dir):
        meta = read_session_json(session_dir) or {}
        db = session_db_path(session_dir)
        item = {
            "session": session_dir.name,
            "path": str(session_dir),
            "status": meta.get("status") or ("unknown" if db.is_file() else "empty"),
            "started_at": meta.get("started_at"),
            "stopped_at": meta.get("stopped_at"),
            "event_count": meta.get("event_count", 0),
            "has_db": db.is_file(),
            "has_images": session_images_dir(session_dir).is_dir(),
        }
        # 没有摘要文件（旧会话/摘要丢了）→ 开库补一份，保证"库坏了也能看"
        if not meta and db.is_file():
            info = _inspect_session_db(db)
            if info:
                item["event_count"] = info["event_count"]
                item["started_at"] = info["started_at"]
                item["stopped_at"] = info["stopped_at"]
                item["status"] = "completed" if info["all_stopped"] else "aborted"
        out.append(item)
    return out


# ======================================================================
# 旧数据整包归档（v4 升级迁移）
# ======================================================================

def _unique_path(path: Path) -> Path:
    """名字被占用时自动加 _2、_3（例如已经有 events_legacy.db 时）。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _checkpoint_db(db_path) -> None:
    """把 -wal 合并回主库再关连接（best-effort，绝不抛异常）。"""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _move_tree(src: Path, dst: Path) -> None:
    """把 src 整个搬进 dst（dst 已有同名目录就递归合并，文件重名加序号）。"""
    if not src.exists():
        return
    if not dst.exists():
        src.replace(dst)
        return
    for child in list(src.iterdir()):
        target = dst / child.name
        if child.is_dir() and target.is_dir():
            _move_tree(child, target)
            continue
        if target.exists():
            target = _unique_path(target)
        child.replace(target)
    try:
        src.rmdir()
    except OSError:
        pass


def _rewrite_legacy_image_paths(db_path, old_images: Path, new_images: Path) -> int:
    """把旧库里 detail.image 的绝对路径从旧 images\\ 改到 legacy\\images\\。

    为什么必须改：旧版存的是绝对路径（例 E:\\CAD-AI-Monitor\\data\\images\\20261012\\x.jpg）。
    只把文件夹搬到 legacy 而不改库里路径，图就全"断链"了。改完保证逐条都能点开。
    """
    rewritten = 0
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, detail FROM events WHERE detail LIKE '%\"image\"%'").fetchall()
        for row_id, detail_text in rows:
            try:
                detail = json.loads(detail_text)
            except Exception:
                continue
            if not isinstance(detail, dict):
                continue
            image = detail.get("image")
            if not isinstance(image, str) or not image:
                continue
            try:
                rel = Path(image).relative_to(old_images)
            except ValueError:
                continue  # 不在旧 images\\ 下（用户自己改过的路径）→ 不动
            new_image = str(new_images / rel)
            if new_image != image:
                detail["image"] = new_image
                conn.execute("UPDATE events SET detail=? WHERE id=?",
                             (json.dumps(detail, ensure_ascii=False), row_id))
                rewritten += 1
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return rewritten
    finally:
        conn.close()
    return rewritten


def _log_migration(legacy_dir: Path, message: str) -> None:
    """往 data\\legacy\\migration.log 追加一行迁移记录（best-effort）。"""
    try:
        legacy_dir.mkdir(parents=True, exist_ok=True)
        with open(str(legacy_dir / LEGACY_LOG_FILENAME), "a", encoding="utf-8") as f:
            f.write(f"{now_iso()}  {message}\n")
    except Exception:
        pass


def _rollback_migration(data_dir: Path, legacy_dir: Path, moved_db, moved_images):
    """迁移失败时把已经搬走的东西搬回原地（best-effort，保证旧数据还在）。"""
    if moved_db is not None:
        target, old_db = moved_db
        try:
            if target.exists() and not old_db.exists():
                target.replace(old_db)
        except OSError:
            pass
    if moved_images is not None:
        target_images, old_images = moved_images
        try:
            if target_images.exists():
                if not old_images.exists():
                    target_images.replace(old_images)
                else:
                    # 搬了一半就失败：把已经搬过去的再合并回原位
                    _move_tree(target_images, old_images)
        except OSError:
            pass


def migrate_legacy_data(data_dir):
    """把 v3 及以前的旧数据整包归档到 data\\legacy\\（第一次启动 v4 时调用）。

    - 旧 data\\events.db       → data\\legacy\\events_legacy.db（重名加 _2，绝不覆盖）
    - 旧 data\\images\\         → data\\legacy\\images\\（保持相对位置，日期子文件夹原样）
    - 迁移前先对旧库做 WAL checkpoint，再搬（避免数据散在 -wal 里）
    - 库内 detail.image 的绝对路径同步改写 → 图片引用不断链
    - **只移动、不删除**；没有旧数据时返回 None（幂等：搬完旧路径就不存在了，
      程序启动多少次都只会归档一次）
    - 中途任何一步失败 → 回退成"原样不动"，旧库不会坏；结果写 data\\legacy\\migration.log
    返回：成功 {"ok": True, "legacy_dir", "db", "images", "rewritten", "log"}；
          失败 {"ok": False, "error", "legacy_dir", "log"}；
          没有旧数据 None。
    """
    data_dir = Path(data_dir)
    old_db = data_dir / "events.db"
    old_images = data_dir / "images"
    if not old_db.is_file() and not old_images.is_dir():
        return None

    legacy_dir = data_dir / LEGACY_DIRNAME
    legacy_images = legacy_dir / SESSION_IMAGES_DIRNAME
    log_path = str(legacy_dir / LEGACY_LOG_FILENAME)
    info = {"ok": True, "legacy_dir": str(legacy_dir), "db": None, "images": None,
            "rewritten": 0, "log": log_path}
    moved_db = None
    moved_images = None
    try:
        legacy_dir.mkdir(parents=True, exist_ok=True)
        if old_db.is_file():
            _checkpoint_db(old_db)  # 先把 -wal 合并回主库，再搬
            target = _unique_path(legacy_dir / LEGACY_DB_FILENAME)
            old_db.replace(target)  # 同一磁盘内是原子改名：要么旧位置、要么 legacy，不会丢
            moved_db = (target, old_db)
            for suffix in ("-wal", "-shm"):   # checkpoint 之后一般已无残留；有就一起搬
                side = Path(str(old_db) + suffix)
                if side.exists():
                    try:
                        side.replace(Path(str(target) + suffix))
                    except OSError:
                        pass
            info["db"] = str(target)

        if old_images.is_dir():
            # 已经归档过（legacy\images 已存在）时的合并失败不做"整体搬回"，
            # 否则会把上一次归档的旧图也搬回根目录；那种情况文件不会丢，只是位置分散。
            if not legacy_images.exists():
                moved_images = (legacy_images, old_images)
            _move_tree(old_images, legacy_images)
            info["images"] = str(legacy_images)

        if info["db"]:
            info["rewritten"] = _rewrite_legacy_image_paths(
                Path(info["db"]), old_images, legacy_images)
    except Exception as exc:
        # 关键要求：迁移失败必须回退到"原样不动"，不能把旧库弄坏
        _rollback_migration(data_dir, legacy_dir, moved_db, moved_images)
        info.update({"ok": False, "error": str(exc), "db": None, "images": None})
        _log_migration(legacy_dir, f"FAILED {exc}；已回退，旧数据保持原样")
        return info

    if info["db"]:
        _log_migration(
            legacy_dir,
            f"OK old_db -> {info['db']}；images -> {info['images'] or '(无旧图)'}；"
            f"改写图片路径 {info['rewritten']} 条；旧数据未删除")
    else:
        _log_migration(
            legacy_dir,
            f"OK images -> {info['images'] or '(无)'}（没有旧数据库）；旧数据未删除")
    return info


# ======================================================================
# 崩溃半截会话（启动时扫描修复）
# ======================================================================

def _has_any_file(root: Path) -> bool:
    """目录里（含子目录）有没有任何文件。"""
    try:
        return any(p.is_file() for p in root.rglob("*"))
    except OSError:
        return True  # 读不了就当作有东西，宁可留着不删


def _recovered_meta(session_dir, info, status, existing=None):
    """给一个会话生成/更新 session.json 摘要。"""
    meta = dict(existing or {})
    meta.update({
        "app_version": meta.get("app_version") or APP_VERSION,
        "storage_version": STORAGE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "started_at": info.get("started_at"),
        "stopped_at": info.get("stopped_at") if status == "completed" else None,
        "event_count": info.get("event_count", 0),
        "counts": info.get("counts", {}),
        "db": f"{SESSION_DATA_DIRNAME}/{SESSION_DB_FILENAME}",
        "images": SESSION_IMAGES_DIRNAME,
    })
    if status == "aborted":
        meta["recovered_at"] = now_iso()
        meta["note"] = "程序上次没有正常停止（崩溃/被强杀）；数据已保留，状态标为 aborted"
    return meta


def recover_sessions(data_dir, mark_aborted=True):
    """启动时扫描 data\\sessions\\（v4 施工注意事项：崩溃会话一律保留）。

    规则（严格按需求源）：
    1. 有 data\\events.db 且 sessions.stopped_at 有值 → 正常结束，原样保留
       （摘要文件丢了就补一份 status=completed）；
    2. 有 db 但 stopped_at 为空 → **保留**，做 WAL checkpoint，session.json 标 status="aborted"；
    3. 没有 db、也没有 session.json、也没有任何文件 → 当作崩溃留下的空目录删掉；
       其余（有图/有摘要没库）一律保留，不猜、不删。
    返回 {"checked", "removed", "aborted", "completed", "kept"}，removed/aborted 是路径列表。
    """
    result = {"checked": 0, "removed": [], "aborted": [], "completed": 0, "kept": []}
    root = sessions_root(data_dir)
    if not root.is_dir():
        return result
    try:
        children = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return result

    for session_dir in children:
        result["checked"] += 1
        db = session_db_path(session_dir)
        meta_path = session_meta_path(session_dir)
        if not db.is_file():
            # 没有 db：只清理"什么都没有"的崩溃残骸
            if not meta_path.is_file() and not _has_any_file(session_dir):
                if delete_session_dir(session_dir):
                    result["removed"].append(str(session_dir))
            else:
                result["kept"].append(str(session_dir))
            continue

        info = _inspect_session_db(db)
        if info is None:
            # 库读不了（损坏/权限）→ 保留，绝不删
            result["kept"].append(str(session_dir))
            continue
        existing = read_session_json(session_dir)
        if info["all_stopped"]:
            result["completed"] += 1
            if existing is None:  # 摘要丢了 → 补一份（不开库也能看）
                write_session_json(session_dir, _recovered_meta(
                    session_dir, info, "completed"))
            continue
        # 走到这里 = 崩溃半截会话：保留 + checkpoint（_inspect 已做）+ 标 aborted
        if mark_aborted:
            write_session_json(session_dir, _recovered_meta(
                session_dir, info, "aborted", existing))
            result["aborted"].append(str(session_dir))
        else:
            result["kept"].append(str(session_dir))
    return result


# ======================================================================
# 单实例锁（禁止两个实例同时写数据）
# ======================================================================

class SingleInstanceLock:
    """Windows 命名互斥体实现的单实例锁。

    用法：
        lock = SingleInstanceLock()
        if not lock.acquire():
            ...提示"已在采集中"并退出...
        ...程序运行期间一直持有...
        lock.release()      # 退出时释放（进程死掉系统也会自动释放）
    """

    def __init__(self, name: str = DEFAULT_INSTANCE_NAME):
        self.name = name
        self._handle = None
        self.acquired = False

    def acquire(self) -> bool:
        """尝试拿锁：True=拿到（第一个实例），False=已有实例在运行。"""
        if self.acquired:
            return True
        if os.name != "nt":
            # 非 Windows 平台不做强制（本项目只支持 Windows，自检也不会跑到这里）
            self.acquired = True
            return True
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                return False
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            self._handle = handle
            self.acquired = True
            return True
        except Exception:
            return False

    def release(self):
        """释放锁（程序退出时调用；不调用也会随进程结束自动释放）。"""
        if self._handle and os.name == "nt":
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
        self._handle = None
        self.acquired = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("已有 CAD-AI 采集器实例在运行")
        return self

    def __exit__(self, *exc_info):
        self.release()
        return False


# ======================================================================
# 配置管理
# ======================================================================

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
        # P2-5：环境变量优先于配置文件，避免把 key 明文写进 config.json
        env_key = os.environ.get("AI_EYE_KEY")
        if env_key:
            merged.setdefault("ai_eye", {})["api_key"] = env_key
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


# ======================================================================
# 本地数据库（一个会话一个库）
# ======================================================================

class EventStore:
    """一个会话的本地数据库（v4：位于 <会话>\\data\\events.db）。

    表结构不变（v2 起）：sessions（一次采集一行）+ events（每条事件一行，多一列 source）。
    遇到 v1 的旧库（没有 source 列）自动改名备份、再建新表，旧数据不丢。

    关闭之后仍然允许"读"（query_session 会临时用只读方式打开），
    这样停止采集关库之后，界面/测试/各软件联调脚本照样能查这次会话的数据。
    """

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：允许采集线程写、界面线程读
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")  # 更稳、更快
        self._lock = threading.Lock()
        self.closed = False
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

    def _ensure_open(self):
        if self._conn is None:
            raise RuntimeError("数据库已关闭，不能再写")
        return self._conn

    def start_session(self) -> int:
        """开一次新采集，返回这次采集的编号（v4：一个库只有一条会话记录）。"""
        with self._lock, self._ensure_open() as conn:
            cur = conn.execute("INSERT INTO sessions(started_at) VALUES (?)", (now_iso(),))
            return cur.lastrowid

    def add_event(self, session_id, seq, source, kind, detail: dict, ts_ms: int):
        """记一条事件。detail 是字典，存进去之前转成 JSON 文本。

        注意：ts_iso 由 ts_ms 换算而来（AI 眼结果会挂过去的时间戳，
        两列必须对应同一点时间，不能一个是过去一个是现在）。
        """
        with self._lock, self._ensure_open() as conn:
            conn.execute(
                "INSERT INTO events(session_id, seq, ts_ms, ts_iso, source, kind, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, seq, ts_ms, _iso_from_ms(ts_ms), source, kind,
                 json.dumps(detail, ensure_ascii=False)),
            )

    def end_session(self, session_id: int, event_count: int):
        """采集结束：补上停止时间和事件总数。"""
        with self._lock, self._ensure_open() as conn:
            conn.execute(
                "UPDATE sessions SET stopped_at = ?, event_count = ? WHERE id = ?",
                (now_iso(), event_count, session_id),
            )

    def count_events(self, session_id=None) -> int:
        """事件总条数（session_id 为空 = 整个库）。"""
        with self._lock:
            if self._conn is None:
                conn = sqlite3.connect(_readonly_uri(self.db_path), uri=True)
                try:
                    if session_id is None:
                        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                    return conn.execute(
                        "SELECT COUNT(*) FROM events WHERE session_id = ?",
                        (session_id,)).fetchone()[0]
                finally:
                    conn.close()
            if session_id is None:
                return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            return self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ?",
                (session_id,)).fetchone()[0]

    def query_session(self, session_id: int):
        """把某次采集的全部事件按顺序读出来（测试和以后的分析都用它）。

        关闭数据库之后也能读：临时用只读连接打开（不写、不建 -wal）。
        """
        sql = ("SELECT seq, ts_ms, ts_iso, source, kind, detail FROM events "
               "WHERE session_id = ? ORDER BY seq")
        with self._lock:
            if self._conn is not None:
                rows = self._conn.execute(sql, (session_id,)).fetchall()
            else:
                conn = sqlite3.connect(_readonly_uri(self.db_path), uri=True)
                try:
                    rows = conn.execute(sql, (session_id,)).fetchall()
                finally:
                    conn.close()
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
                if self._conn is not None:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    def close(self):
        """合并 WAL 后关闭数据库（v4：停止采集时关库，一个会话的库就定稿了）。"""
        self.wal_checkpoint()
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
        self.closed = True


# ======================================================================
# 跨会话分析工具（命令行）
# ======================================================================

def _print_session_summary(data_dir, as_json=False):
    """打印 data\\sessions\\ 里所有会话的小结（跨会话分析入口）。"""
    sessions = session_summaries(data_dir)
    total = sum(int(s.get("event_count") or 0) for s in sessions)
    if as_json:
        print(json.dumps({"data_dir": str(data_dir), "session_count": len(sessions),
                          "event_count": total, "sessions": sessions},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"数据文件夹：{data_dir}")
    print(f"会话数：{len(sessions)}    事件总数：{total}")
    print("-" * 78)
    for s in sessions:
        print(f"{s['session']:<24} {s['status']:<10} 事件 {s['event_count']:<6} "
              f"开始 {s['started_at'] or '-'}")
    if not sessions:
        print("（还没有会话；成功采集一次后会出现在 data\\sessions\\ 下）")
    return 0


def main(argv=None):
    """命令行：不开界面也能看所有会话（v4 跨会话分析口径）。"""
    if sys.stdout is not None:
        try:  # 老式控制台用 GBK，中文/符号直接打印会炸；强制 UTF-8 输出
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        description="CAD-AI 采集器 · 跨会话数据小结（遍历 data\\sessions\\*\\data\\events.db）")
    parser.add_argument("--data", default=None,
                        help="数据文件夹；默认按程序规则自动找（环境变量 / 文档 等）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")
    parser.add_argument("--events", action="store_true",
                        help="把每个会话的事件也全部列出来（JSON；数据多时慎用）")
    args = parser.parse_args(argv)
    data_dir = Path(args.data) if args.data else resolve_data_dir()
    if args.events:
        evs = list(iter_all_events(data_dir))
        print(json.dumps({"data_dir": str(data_dir), "event_count": len(evs),
                          "events": evs}, ensure_ascii=False, indent=2))
        return 0
    return _print_session_summary(data_dir, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
