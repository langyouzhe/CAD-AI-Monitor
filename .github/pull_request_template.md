# Pull request / 合并请求

## What does this change? / 这个改动做了什么

<!-- One or two sentences. Link the issue it fixes if there is one. -->

## Type / 类型

- [ ] Bug fix / 修 bug
- [ ] New feature / 新功能
- [ ] New CAD backend / 新 CAD 后端
- [ ] Documentation only / 只改文档
- [ ] Refactor / 重构

## Checklist / 自查清单

- [ ] `python app\selftest.py` passes（16 项全绿，退出码 0）
- [ ] If it touches packaging: `app\build.bat` succeeds and `release\CAD-AI-Collector.exe --selftest` exits 0
- [ ] **No personal data committed**（`data\`、`config.json`、截图、真实会话、API key 都没有进来）
- [ ] **No hard-coded paths or CAD versions**（用 `app\cad_discovery.py` 去发现，而不是写死 `C:\Users\...` 或 `SldWorks.Application.30`）
- [ ] `.bat` / `.cmd` files stay **ASCII-only**（cmd.exe parses them with the OEM codepage; non-ASCII breaks the script）
- [ ] Tested on a real CAD install — say which one below / 在真机 CAD 上验证过，说明是哪个

## Tested on / 测试环境

| Item | Value |
|---|---|
| CAD and version | |
| Windows version | |
| Python version (if from source) | |
