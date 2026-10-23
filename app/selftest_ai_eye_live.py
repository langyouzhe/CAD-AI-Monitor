# -*- coding: utf-8 -*-
"""
selftest_ai_eye_live.py —— AI 眼真实云端首测（用你自己配置的 key）

流程与正式运行完全一致：
  截图（所有显示器→按前台窗口裁剪）→ 送云端多模态 → 解析 JSON → 落库格式校验。
会真实调用一次云端（花费约几分钱），结果打印出来供人工判断。

用法：python selftest_ai_eye_live.py [data_dir]
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai_eye import AiEye  # noqa: E402


def main():
    from collector_core import resolve_data_dir
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else resolve_data_dir()
    cfg_path = data_dir / "config.json"
    if not cfg_path.exists():
        print(f"找不到配置: {cfg_path}")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")).get("ai_eye", {})
    print(f"服务商={cfg.get('provider')} | 模型={cfg.get('model')} | "
          f"接口={cfg.get('api_base')} | max_tokens={cfg.get('max_tokens')}")
    if cfg.get("provider") == "mock":
        print("当前是 mock 模式，不会真调云端。要真测请先把 provider 改成 custom/zhipu/qwen/doubao。")
        return 1

    results = []
    eye = AiEye(cfg=cfg, on_result=lambda d, ts: results.append((d, ts)),
                is_running=lambda: True,
                images_dir=Path(tempfile.mkdtemp(prefix="cad-ai-eye-live-")) / "images")
    eye.start()
    ts = int(time.time() * 1000)
    print("触发一次截图识别（目标：当前前台窗口）…")
    t0 = time.time()
    eye.trigger("click", {"button": "left"}, ts, (400, 400))
    deadline = time.time() + 120
    while time.time() < deadline and not results:
        time.sleep(0.2)
    eye.stop()

    if not results:
        print("超时：120 秒内没有结果")
        return 1
    detail, got_ts = results[0]
    dt = time.time() - t0
    res = detail.get("result") or {}
    print(f"\n耗时 {dt:.1f}s | 时间戳对齐: {'✓' if got_ts == ts else '✗'}")
    print(f"图片落盘: {detail.get('image')}")
    print(f"识别成功: {res.get('ok')}")
    if res.get("ok"):
        ans = res.get("answer") or {}
        print("识别结论:")
        for k in ("summary", "ui_state", "error"):
            print(f"  {k}: {ans.get(k)}")
        if res.get("truncated"):
            print("  ⚠ 回答被 max_tokens 截断（可调大 max_tokens）")
        return 0
    print("失败原因:", res.get("error"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
