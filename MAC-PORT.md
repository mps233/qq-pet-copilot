# macOS 移植说明（本地适配副本）

本目录是 [490720818/qq-pet-copilot](https://github.com/490720818/qq-pet-copilot)（GPL-3.0）的 macOS 本地适配版。
目标运行方式：**控制台调度器 + Android 真机**（不使用 Windows 模拟器链路与 GUI）。

## 相对上游的改动（分支 `mac-port`）

1. `src/emulator.py` — `winreg` 顶层导入改为 try/except 降级（非 Windows 用空实现兜底）。
   原代码在 macOS 上导入即崩；现在模拟器注册表探测安全返回空结果。
2. `src/config.py` — adb 探测加入 macOS 路径（`/opt/homebrew/bin/adb`、`/usr/local/bin/adb`、
   `~/Library/Android/sdk/platform-tools/adb`），支持 `~` 展开；找不到时的报错文案通用化。
3. `src/notify.py` — 新增 `_send_mac_toast`（osascript 桌面通知），复用 `notify.win_toast` 开关；
   Windows Toast 逻辑原样保留。
4. `config.yaml` — 本地配置（不入库）：`adb.path: /opt/homebrew/bin/adb`。

未涉及：模拟器管理（winreg 探测在 Mac 自然失效）、`src/opener.py`（frida 门禁，模拟器专用）、
`main.py` GUI（win32 + scrcpy 嵌入，Windows 专用）。

## 环境与运行

- Python 3.13（`.venv/` 已建好，依赖已装：uiautomator2 / rapidocr / onnxruntime / Pillow / onepush / ruamel.yaml / PyYAML）
- 真机准备：USB 调试打开；QQ 已登录且 ≥ 9.3.25
- 启动：`./run.sh`（= `.venv/bin/python scenarios/runner.py`），Ctrl+C 停止
- 单测：`./run.sh --test coins`（连接后验证金币识别）等

## macOS 已验证

- 全量 py_compile 通过；runner 完整导入链通过（含 emulator/opener/recover/u2dev/ocr）
- adb 探测 → `/opt/homebrew/bin/adb`
- 无设备时优雅报错："没有在线的 adb 设备，请检查 USB 连接与调试授权。"
- macOS 桌面通知链路 OK；OCR 引擎加载 OK（模型自动下载）

## 待真机验证

- u2 连接（首次会自动部署 u2 agent）、宠物页进入、截图/OCR 命中、完整任务链路

## 与上游同步

`origin` 即上游仓库；`mac-port` 分支只有上述小改动。
`git fetch origin && git merge origin/main`，冲突预计只落在改动过的 3 个源文件。
