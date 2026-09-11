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
4. `src/scenario.py` — **新版 QQ（9.3.6x）回主页兼容修复**（真机实测发现并验证）：
   - 现象：打工/学习面板（QPublicFragmentActivity）返回主页时，3D 主页需 1~2 秒重渲染；
     原逻辑按 back 后立即检查、未命中就再按 back，会连锁把宠物模块整个退回关闭，一路退回
     系统桌面，`ensure_main_page` 抛"无法回到主页面"；恢复重试同样失败 → 告警退出调度器。
   - 修复 A：每次 back 后先等 `BACK_SETTLE_SECONDS`(2s) 再检查，避免没等到渲染就连环按退。
   - 修复 B：连续 `SCHEME_FALLBACK_BACKS`(3) 次 back 未回主页时，改用官方 scheme
     （`mqqapi://qpet/open`，零点击、无需 root）重开宠物页并等渲染兜底；最多兜底 2 次。
   - 实测：打工面板→主页 3.3s 成功；从系统桌面（最坏状态）14.3s 自愈成功。
5. `config.yaml` — 本地配置（不入库）：`adb.path=/opt/homebrew/bin/adb`、`device_serial=1f584daf`、
   `recover.method=重启游戏`（真机重启后若设锁屏密码，恢复链会卡解锁，故不用 adb reboot）。

未涉及：模拟器管理（winreg 探测在 Mac 自然失效）、`src/opener.py` 的模拟器门禁注入路径（真机不用）、
`main.py` GUI（win32 + scrcpy 嵌入，Windows 专用）。

## 环境与运行

- Python 3.13（`.venv/` 已建好，依赖已装：uiautomator2 / rapidocr / onnxruntime / Pillow / onepush / ruamel.yaml / PyYAML）
- 真机准备：USB 调试打开；QQ 已登录且 ≥ 9.3.25；充电时不熄屏
  （`adb shell settings put global stay_on_while_plugged_in 7`）
- 启动：`./run.sh`（= `.venv/bin/python scenarios/runner.py`），Ctrl+C 停止
- 单测：`./run.sh --test coins` 等

## 真机实测记录（一加 LE2120 / Android 14 / QQ 9.3.60）

- u2 连接、截图、整屏 OCR、金币识别（1600）、主页面判定均通过
- 已实际跑通：护理检查、好友踩踩（10/10）、帮好友照顾（经验日常）、PK（15/15，含卡局自愈）、
  打工开始 + 鼓励宠物 10 连击
- 告警链路：macOS 通知 + 截图存档实测生效（`runs/alert_*.png`）
- 待观察：学习（school）流程、打工结算收取、被雇佣、长时间连续运行的稳定性

## 与上游同步

`origin` 即上游仓库；`mac-port` 分支只有上述小改动。
`git fetch origin && git merge origin/main`，冲突预计只落在改动过的源文件。
