# -*- coding: utf-8 -*-
"""
app.py —— 采集器的小窗口界面（v2）

v2 新增（按最新版项目上下文）：
- 界面显示两路状态：流1（SolidWorks 连没连上、当前文档）、流2（是否采集中、
  当前前台窗口是不是建模窗口——不是就显示"装聋中"）；
- 不再显示停顿计数，改为 流1/流2 各自条数 + 离开次数；
- 新增【打开设置】按钮：改 data\\config.json 里的建模窗口名单、离开阈值等。

数据只保存在本地（默认 E:\\CAD-AI-Monitor\\data），不会联网。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from collector_core import (APP_VERSION, SingleInstanceLock, migrate_legacy_data,
                            recover_sessions, resolve_data_dir, session_meta_path)
from input_listener import CollectorSession, foreground_window_info, is_modeling_window

REFRESH_MS = 500  # 界面数字每 0.5 秒刷新一次


def _is_waiting_msg(err: str) -> bool:
    """判断后端报的是不是"正常等待"（软件还没打开），而不是真故障。"""
    t = str(err or "")
    return any(k in t for k in ("未运行", "是否运行过", "等它打开", "还没打开"))


class SettingsDialog(tk.Toplevel):
    """设置窗口：常用项全部图形化，点保存写入 config.json。"""

    PROVIDERS = ["mock（试用流程，不花钱）", "zhipu（智谱）", "qwen（通义千问）",
                 "doubao（豆包）", "custom（自填接口）"]
    PROVIDER_KEYS = {"mock（试用流程，不花钱）": "mock", "zhipu（智谱）": "zhipu",
                     "qwen（通义千问）": "qwen", "doubao（豆包）": "doubao",
                     "custom（自填接口）": "custom"}

    def __init__(self, master, data_dir):
        super().__init__(master)
        from collector_core import ConfigManager
        self.cfg_mgr = ConfigManager(data_dir)
        self.cfg = self.cfg_mgr.load()
        self.cfg_path = self.cfg_mgr.path

        self.title("设置")
        self.geometry("480x430")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        body = tk.Frame(self)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        # ---- AI 眼 ----
        ttk.Label(body, text="AI 眼（视觉识别）", font=("Microsoft YaHei", 12, "bold")) \
            .grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ai = self.cfg.get("ai_eye", {})
        self.v_enabled = tk.BooleanVar(value=bool(ai.get("enabled")))
        ttk.Checkbutton(body, text="启用 AI 眼（点击/快捷键/笔势时截图送云端识别）",
                        variable=self.v_enabled).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(body, text="服务商").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.v_provider = ttk.Combobox(body, values=self.PROVIDERS, width=28, state="readonly")
        cur_p = ai.get("provider", "mock")
        label_p = next((k for k, v in self.PROVIDER_KEYS.items() if v == cur_p),
                       self.PROVIDERS[0])
        self.v_provider.set(label_p)
        self.v_provider.grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(body, text="API Key").grid(row=3, column=0, sticky="w", pady=(6, 0))
        key_row = tk.Frame(body)
        key_row.grid(row=3, column=1, sticky="w", pady=(6, 0))
        self.v_key = tk.Entry(key_row, width=32, show="●")
        self.v_key.insert(0, ai.get("api_key", ""))
        self.v_key.pack(side="left")
        self.v_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_row, text="显示", variable=self.v_show,
                        command=self._toggle_key).pack(side="left", padx=(6, 0))
        ttk.Label(body, text="接口地址（留空=用服务商预设）").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.v_base = tk.Entry(body, width=32)
        self.v_base.insert(0, ai.get("api_base", ""))
        self.v_base.grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Label(body, text="模型（留空=用默认视觉模型）").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.v_model = tk.Entry(body, width=32)
        self.v_model.insert(0, ai.get("model", ""))
        self.v_model.grid(row=5, column=1, sticky="w", pady=(6, 0))
        self.v_saveimg = tk.BooleanVar(value=bool(ai.get("save_images", True)))
        ttk.Checkbutton(body, text="保存截图到本地（不勾选=识别完不留图，省空间；图鉴会越攒越多）",
                        variable=self.v_saveimg).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ---- 通用 ----
        ttk.Label(body, text="通用", font=("Microsoft YaHei", 12, "bold")) \
            .grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 4))
        ttk.Label(body, text="无操作多少分钟算离开").grid(row=9, column=0, sticky="w", pady=(6, 0))
        self.v_idle = tk.Entry(body, width=8)
        self.v_idle.insert(0, str(self.cfg.get("idle_minutes", 5.0)))
        self.v_idle.grid(row=9, column=1, sticky="w", pady=(6, 0))
        self.v_gesture = tk.BooleanVar(value=bool(self.cfg.get("gestures", {}).get("enabled", True)))
        ttk.Checkbutton(body, text="记录笔势（按住鼠标键拖动）",
                        variable=self.v_gesture).grid(row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(body, text="改动保存后，下次点「开始采集」生效。",
                  foreground="#888888").grid(row=11, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # ---- 按钮 ----
        btns = tk.Frame(self)
        btns.pack(pady=(0, 12))
        tk.Button(btns, text="保存", width=10, command=self.on_save,
                  bg="#2e9e4f", fg="white", font=("Microsoft YaHei", 11)) \
            .pack(side="left", padx=6)
        tk.Button(btns, text="取消", width=10, command=self.destroy,
                  font=("Microsoft YaHei", 11)).pack(side="left", padx=6)
        tk.Button(btns, text="高级设置（记事本）", width=18,
                  command=self.on_advanced, font=("Microsoft YaHei", 9)) \
            .pack(side="left", padx=6)

    def _toggle_key(self):
        self.v_key.config(show="" if self.v_show.get() else "●")

    def on_save(self):
        try:
            idle = float(self.v_idle.get().strip())
            if idle <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("设置", "「无操作多少分钟算离开」要填正数，比如 5")
            return
        self.cfg["idle_minutes"] = idle
        self.cfg.setdefault("gestures", {})["enabled"] = self.v_gesture.get()
        ai = self.cfg.setdefault("ai_eye", {})
        ai["enabled"] = self.v_enabled.get()
        ai["provider"] = self.PROVIDER_KEYS.get(self.v_provider.get(), "mock")
        ai["api_key"] = self.v_key.get().strip()
        ai["api_base"] = self.v_base.get().strip()
        ai["model"] = self.v_model.get().strip()
        ai["save_images"] = self.v_saveimg.get()
        try:
            self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
            self.cfg_path.write_text(
                json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("设置", f"保存失败：{exc}")
            return
        self.destroy()
        messagebox.showinfo("设置", "已保存。下次点「开始采集」生效。")

    def on_advanced(self):
        """完整配置文件用记事本打开（建模窗口名单、触发规则等高级项）。"""
        subprocess.Popen(["notepad.exe", str(self.cfg_path)])


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.data_dir = resolve_data_dir()
        self.session = None
        self.last_session_dir = None   # 上次会话文件夹（停止后【打开文件夹】用它）
        self.migration = None
        self.recovery = None
        # v4 启动检查（两件事都只做一次）：
        # 1. 旧版数据整包归档（data\events.db + data\images\ → data\legacy\）；
        # 2. 崩溃半截会话：保留 + WAL checkpoint + 标 aborted；清掉无 db 的空残骸。
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.migration = migrate_legacy_data(self.data_dir)
            self.recovery = recover_sessions(self.data_dir)
        except Exception:
            self.migration = None
            self.recovery = None
        # 界面上的"当前窗口"是独立小线程探测的（不在采集时也能显示）
        self._fw = (None, None)

        root.title(f"CAD-AI 采集器 v{APP_VERSION}")
        root.geometry("480x315")
        root.resizable(False, False)

        # 状态灯
        self.status_var = tk.StringVar(value="未在采集")
        self.status_label = tk.Label(
            root, textvariable=self.status_var, font=("Microsoft YaHei", 16, "bold"),
            fg="#888888",
        )
        self.status_label.pack(pady=(12, 2))

        # 流1 状态行
        self.s1_var = tk.StringVar(value="流1（SW/Creo/NX）：未开始")
        tk.Label(root, textvariable=self.s1_var, font=("Microsoft YaHei", 10),
                 fg="#333333").pack()

        # 流2 状态行
        self.s2_var = tk.StringVar(value="流2（输入）：未开始")
        tk.Label(root, textvariable=self.s2_var, font=("Microsoft YaHei", 10),
                 fg="#333333").pack(pady=(2, 0))

        # 流1 提示行（专门说"为什么流1 没数据"：没找到 trail / trail 没更新 / 等软件打开）
        self.s1_hint_var = tk.StringVar(value="")
        self.s1_hint_label = tk.Label(root, textvariable=self.s1_hint_var,
                                      font=("Microsoft YaHei", 9), fg="#b06a00",
                                      wraplength=440, justify="center")
        self.s1_hint_label.pack(pady=(2, 0))

        # AI 眼状态行
        self.s3_var = tk.StringVar(value="AI 眼：未启用（设置里填 api_key 可开）")
        tk.Label(root, textvariable=self.s3_var, font=("Microsoft YaHei", 10),
                 fg="#8a6d1a").pack(pady=(2, 6))

        # 计数
        self.stats_var = tk.StringVar(value="流1 0 条 · 流2 0 条 · 离开 0 次 · 时长 0 分钟")
        self.stats_label = tk.Label(root, textvariable=self.stats_var,
                                    font=("Microsoft YaHei", 11))
        self.stats_label.pack(pady=(0, 10))

        # 按钮行
        btn_row = tk.Frame(root)
        btn_row.pack()
        self.start_btn = tk.Button(btn_row, text="开始采集", width=14,
                                   command=self.on_start, bg="#2e9e4f",
                                   fg="white", font=("Microsoft YaHei", 12))
        self.start_btn.pack(side="left", padx=8)
        self.stop_btn = tk.Button(btn_row, text="停止采集", width=14,
                                  command=self.on_stop, state="disabled",
                                  bg="#c0392b", fg="white",
                                  font=("Microsoft YaHei", 12))
        self.stop_btn.pack(side="left", padx=8)

        # 底部：数据位置 + 按钮
        loc_row = tk.Frame(root)
        loc_row.pack(pady=(12, 0))
        self.loc_var = tk.StringVar(value=f"数据：{self.data_dir}")
        tk.Label(loc_row, textvariable=self.loc_var, fg="#555555",
                 font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(loc_row, text="打开文件夹", command=self.on_open_dir,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=(8, 0))
        tk.Button(loc_row, text="设置", command=self.on_open_config,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=(8, 0))

        tk.Label(root,
                 text="只记建模软件窗口内的操作；数据只存本机。设置改完，下次点“开始采集”生效。",
                 fg="#999999", font=("Microsoft YaHei", 9)).pack(pady=(8, 0))

        # 升级/恢复提示：旧数据搬走之后旧路径就不存在了，所以只会提示一次
        if self.migration:
            root.after(300, self._notify_migration)
        if self.recovery and self.recovery.get("aborted"):
            self.status_var.set(
                f"未在采集 · 已保留 {len(self.recovery['aborted'])} 个上次未正常结束的会话")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._tick()

    def _notify_migration(self):
        """首次升级到 v4 的提示：旧数据已经整包归档（只移动，没删）。"""
        m = self.migration or {}
        if m.get("ok") is False:
            messagebox.showwarning(
                "旧数据归档失败（旧数据保持原样）",
                "检测到旧版数据，但自动归档没有完成，已经帮你保持原样（没有搬动/删除）。\n\n"
                f"原因：{m.get('error')}\n"
                f"迁移日志：{m.get('log')}\n\n"
                "可以关掉程序后手动把 data\\events.db 和 data\\images\\ 移到 data\\legacy\\。",
            )
            return
        db_line = (f"旧数据库：data\\events.db → {m.get('db')}"
                   if m.get("db") else "旧数据库：无")
        img_line = (f"旧截图：data\\images\\ → {m.get('images')}"
                    if m.get("images") else "旧截图：无")
        messagebox.showinfo(
            "旧数据已归档（升级到 v0.2.0）",
            "检测到旧版本采集的数据，已自动整包归档到 data\\legacy\\"
            "（只移动，没有删除）：\n\n"
            f"{db_line}\n{img_line}\n\n"
            "从这一版开始，每次「开始采集 → 停止采集」都会生成独立的会话文件夹：\n"
            "data\\sessions\\<开始时间>\\（里面是 images\\ + data\\events.db + session.json），\n"
            "多次采集不再混在同一个文件里。\n\n"
            f"迁移日志：{m.get('log')}",
        )

    # ---------- 按钮动作 ----------

    def on_start(self):
        try:
            self.session = CollectorSession(self.data_dir)
            self.session.start()
        except Exception as exc:
            self.session = None
            messagebox.showerror(
                "启动失败",
                "采集没起来，原因：\n\n" + str(exc) +
                "\n\n可以重试一次；如果一直失败，把这段文字发给我。",
            )
            return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("● 采集中")
        self.status_label.config(fg="#2e9e4f")
        # v4：界面显示本次会话文件夹（完整路径在停止时的对话框里）
        session_dir = Path(self.session.session_dir)
        self.last_session_dir = session_dir
        self.loc_var.set(f"本次会话：sessions\\{session_dir.name}")
        self.root.title(f"CAD-AI 采集器 v{APP_VERSION} · {session_dir.name}")

    def on_stop(self):
        if not self.session:
            return
        summary = self.session.stop() or {}
        stats = self.session.stats()
        self.session = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set("未在采集")
        self.status_label.config(fg="#888888")
        self.root.title(f"CAD-AI 采集器 v{APP_VERSION}")
        c = summary.get("counts") or {}
        ai = summary.get("ai_eye") or {}
        ai_line = ""
        if ai.get("pending") or ai.get("in_flight"):
            ai_line = (f"\nAI 眼收尾：等了 {ai.get('drained_s', 0)} 秒，"
                       f"还有 {ai.get('pending', 0)} 张没识别完（结果不写入本次会话）")
        if summary.get("kept"):
            session_dir = Path(summary["session_dir"])
            self.last_session_dir = session_dir
            self.loc_var.set(f"上次会话：sessions\\{session_dir.name}")
            path_line = f"数据已保存到：\n{session_dir}"
        else:
            path_line = "本次没有记录到任何事件 → 未生成会话文件夹（空会话不留痕）。"
            self.loc_var.set("上次会话：空（没有记录到任何事件）")
        messagebox.showinfo(
            "本次小结",
            f"流1（软件事件）{c.get('stream1', 0)} 条\n"
            f"流2（输入操作）{c.get('stream2', 0)} 条\n"
            f"离开/回来边界 {stats.get('leave_count', 0)} 次\n"
            f"时长 {summary.get('duration_s', 0)} 秒{ai_line}\n\n"
            f"{path_line}",
        )

    def on_open_dir(self):
        """打开最近一次会话文件夹；还没采过就打开数据根目录。"""
        target = self.data_dir
        if self.last_session_dir and Path(self.last_session_dir).is_dir():
            target = Path(self.last_session_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target))

    def on_open_config(self):
        """打开设置窗口（图形界面）。"""
        SettingsDialog(self.root, self.data_dir)

    def on_close(self):
        if self.session and self.session.running:
            if messagebox.askyesno(
                "还在采集中",
                "现在关窗口会停止采集并把已录的数据保存下来。确定关闭吗？",
            ):
                self.on_stop()
            else:
                return
        self.root.destroy()

    # ---------- 定时刷新 ----------

    def _tick(self):
        try:
            self._fw = foreground_window_info()
        except Exception:
            pass
        if self.session:
            s = self.session.stats()
            c = s["counts"]
            minutes = 0
            if s["started_at"]:
                minutes = int((time.time() * 1000 - s["started_at"]) / 60000.0)
            self.stats_var.set(
                f"流1 {c.get('stream1', 0)} 条 · 流2 {c.get('stream2', 0)} 条 · "
                f"离开 {s['leave_count']} 次 · 时长 {minutes} 分钟"
            )
            # 流1 状态（多后端）
            st1 = s.get("stream1_status")
            if st1:
                parts = []
                hints = []
                for b in st1:
                    name = b.get("backend", "?")
                    if b.get("connected"):
                        doc = b.get("doc")
                        parts.append(f"{name}✓ {doc or ''}")
                    else:
                        parts.append(f"{name}✗")
                    err = b.get("last_error")
                    # "等软件打开"这种正常等待不刷提示；只有"连上了却没数据"或
                    # 真出错（如 trail 文件不更新）才提醒
                    if err and (b.get("connected") or not _is_waiting_msg(err)):
                        hints.append(f"{name}：{err}")
                self.s1_var.set("流1：" + "  ".join(parts))
                # 有原因就明说（没找到 trail / trail 没更新 / 等软件打开…）
                self.s1_hint_var.set(("⚠ " + " ；".join(hints)) if hints else "")
            else:
                self.s1_var.set("流1：未启用")
                self.s1_hint_var.set("")
            # 流2 状态（装聋指示）
            title, exe = self._fw
            if is_modeling_window((title, exe), self.session.cfg.get("modeling_apps", [])):
                win = f"{title} ✔"
            else:
                win = f"{title or exe or '无窗口'}（非建模窗口，装聋中）"
            self.s2_var.set(f"流2（输入）：采集中 · 当前窗口：{win}")
            # AI 眼状态
            if s.get("ai_eye_status"):
                ae = s["ai_eye_status"]
                self.s3_var.set(
                    f"AI 眼：{ae['provider']} · 队列{ae['queued']} 成{ae['ok']} "
                    f"败{ae['fail']} 跳{ae['skipped']}"
                    + (f" · 最近: {ae['last_result']}" if ae.get("last_result") else "")
                )
            else:
                self.s3_var.set("AI 眼：未启用（设置里填 api_key 可开）")
        self.root.after(REFRESH_MS, self._tick)


def run_selftest():
    """自检模式：后台跑一段真实采集（不显示窗口），验证整条链路。

    用法：CAD-AI-Collector.exe --selftest
    输出一行 JSON：{"ok": true, ...}；exit 0 = 通过。
    """
    tmp = tempfile.mkdtemp(prefix="cad-ai-selftest-")
    # 自检专用配置：所有窗口都记（测试时前台窗口是控制台，不是建模软件）
    cfg_path = os.path.join(tmp, "config.json")
    test_cfg = {
        "version": 2,
        "idle_minutes": 0.1,
        "debug": {"allow_all_windows": True},
        "stream1": {"enabled": False},
    }
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(test_cfg, f, ensure_ascii=False, indent=2)

    session = CollectorSession(tmp)
    session.start()
    time.sleep(0.8)
    # 注入一条合成按键：不依赖"真人在自检那几秒里正好碰键盘/鼠标"，
    # 保证每次 --selftest 都真的走一遍"事件→会话库→session.json→落盘"链路。
    class _FakeKey(object):   # 普通对象可哈希（types.SimpleNamespace 不可哈希，进不了按键集合）
        char = "a"

    try:
        session._on_key_press(_FakeKey())
    except Exception:
        pass
    time.sleep(0.4)
    summary = session.stop() or {}
    if summary.get("kept"):
        events = session.store.query_session(session.session_id)
        meta = json.loads(session_meta_path(summary["session_dir"])
                          .read_text(encoding="utf-8"))
        session_dir = Path(summary["session_dir"])
        layout_ok = (session_dir / "images").is_dir() and (session_dir / "data" / "events.db").is_file()
    else:  # 理论上合成事件后不该为空；真为空也只报告、不算崩溃
        events, meta, layout_ok = [], None, False
    kinds = {}
    for e in events:
        key = f"{e['source']}/{e['kind']}"
        kinds[key] = kinds.get(key, 0) + 1
    passed = bool(summary.get("kept") and meta and layout_ok and events)
    print(json.dumps({"ok": passed, "event_count": len(events), "kinds": kinds,
                      "data_dir": tmp, "session_dir": summary.get("session_dir"),
                      "session_json": bool(meta), "layout_ok": layout_ok,
                      "kept": summary.get("kept")}, ensure_ascii=False))
    return 0 if passed else 1


def show_already_running():
    """第二个实例的提示（要求：提示"已在采集中"并退出，禁止两个实例同时写）。"""
    msg = ("CAD-AI 采集器已经在运行（已在采集中）。\n\n"
           "同一时间只能开一个程序实例写采集数据，\n"
           "请切换到已经打开的那个窗口操作，不要重复打开。")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "已在采集中", 0x00000040)
    except Exception:
        print(msg)


def main():
    if "--selftest" in sys.argv:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.exit(run_selftest())
    # v4：单实例锁——程序已经在运行时，第二个实例提示后退出
    lock = SingleInstanceLock()
    if not lock.acquire():
        show_already_running()
        sys.exit(0)
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
