# 网页端实时查看 / 控制手机 —— 方案调研与解包分析

> 调研目的：评估把「浏览器实时看手机 + 控制手机」引入本项目的可行性与代价。
> 分析对象：[firerpa/lamda](https://github.com/firerpa/lamda)（FIRERPA）、
> [hqw700/ScrcpyOverWebRTC](https://github.com/hqw700/ScrcpyOverWebRTC)。
> 本文只记录结论与依据，不含实现改动。

## 0. 结论摘要

1. **现有链路的天花板是 ~0.5 fps**：`dashboard.py` 的 `capture_phone()` 走
   `adb exec-out screencap -p`，无线 adb 下实测单帧 **1.65–1.92 s / 1.5 MB**，
   离 60fps 差两个数量级。这条路再怎么优化都到不了实时，必须换成「设备端持续编码 + 流式传输」。
2. **真正的低延迟方案必须把 agent 放在设备端**。两个成熟项目都这么做：
   - FIRERPA：设备端常驻服务（APK/Magisk，root 或 Shizuku）+ 内置 WebRTC；
   - ScrcpyOverWebRTC：设备端 Go agent + **改造版 scrcpy-server** + Pion WebRTC。
   Mac/PC 中转方案只能优化「最后一跳」，而本项目最慢的一跳是「手机 → Mac」。
3. **浏览器的安全上下文限制决定路线**（本项目 dashboard 是 `http://<ip>:8787`）：
   - **WebCodecs 的 `VideoDecoder` 需要安全上下文（HTTPS）**，http 下不可用；
   - `RTCPeerConnection`（WebRTC）与 MSE **不需要**安全上下文。
4. **建议顺序**：MJPEG（`<img>` + multipart，1–3 fps，纯标准库可行）→
   H.264 over WebSocket + MSE（15–30 fps，需转 fMP4）→ WebRTC（30–60 fps，但需要设备端 agent）。
5. **当前最值得直接借鉴的一条**：`scrcpy-server` 以 **shell(2000)** 身份运行即可做触摸注入
   （反射 `InputManager.injectInputEvent`），**不需要 root、不需要 `/dev/input`**——
   可作为本项目 minitouch 失败回退链之外的第三种注入后端。

## 1. 现状与实测（本项目）

设备：一加 LE2120 / Android 14 / **无线 adb** `192.168.50.40:5555`。

```
adb -s 192.168.50.40:5555 exec-out screencap -p
第1次 1651 ms   1547 KB
第2次 1832 ms   1545 KB
第3次 1922 ms   1549 KB
```

现有实现：`dashboard.py` 的 `capture_phone()`（服务端 5 s 节流缓存）→ `/api/screenshot`
（单张 JPEG）→ 前端 `refreshShot()` 每 15 s 轮询一次。即当前是「低频快照」，非实时。

## 2. FIRERPA（lamda）的控制方案

架构是 C/S：设备端常驻服务（闭源，Python 3.12 打包运行时）+ PC 端 Python 客户端走
**gRPC**（默认端口 65000，同端口还复用 WebUI/WebSocket/内置 ADB 协议/Frida host 协议）。

| 能力 | 实现方式 | 依据 |
| --- | --- | --- |
| 控件树/元素操作 | uiautomator 体系（`Selector`/`ObjInfo` 字段与 `UiSelector`/`AccessibilityNodeInfo` 一一对应），Android 8.0+ 可与其他无障碍服务共存 | `lamda/rpc/uiautomator.proto`、CHANGELOG |
| 坐标触摸注入 | `TouchSequence`：多指 `contact` + 压力 `z`（1–255，默认 128）+ 轨迹录制/回放 | `util.proto`、[multi-touch 文档](https://device-farm.com/docs/en/multi-touch) |
| 触摸底层（推断） | **evdev/uinput**：设备端预装 `evdev 1.9.2`；压力域 1–255 与 `ABS_MT_PRESSURE` 一致；轨迹录制要求真机操作 | 官方 llms-full 库清单 + 接口特征 |
| 投屏 | 设备端编码：WebSocket 推 MJPEG 帧或 H.264 NALU，另有 WebRTC；默认 `scale=0.5`/`quality=50`/`fps=35`（上限 60），软编/硬编可切 | properties 文档 |
| 触控回传 | WebSocket 发 press/move/release；`touch.backend = native(默认) \| system` | properties 文档 |
| 虚拟屏 | `createVirtualDisplay` + 所有 RPC 请求都带 `display` 字段（proto 统一 16 号） | `uiautomator.proto` |

## 3. ScrcpyOverWebRTC 解包分析（v0.3.7）

方法：从 GitHub Release 下载 `cloudphone-v0.3.7.zip`（含 6 平台服务端 + 3 个 ABI 的 agent）
与 `cloudphone-agent-magisk-v0.3.7.zip`；`go version -m` 返回 `unknown`（build info 已 strip），
改用 `strings` 提取符号与日志常量，并解开 `libsys_core.so` 查看 DEX。

### 3.1 架构：数据面与控制面分离

```
设备端 Agent (Go) ──Pion WebRTC(SRTP/UDP)──> 浏览器(Vue + WebCodecs)
     │  ↑ wss 信令                                 ↑
     └──┴──> 服务端(Go 单二进制：信令/设备管理/许可/租约)
                    媒体不经服务端，仅 TURN 兜底中转
```

### 3.2 设备端 Agent：Go 静态二进制，把采集交给改造版 scrcpy-server

部署（`agent-deploy.pkg/run.sh` 原文，**以 adb shell 身份启动即 shell(2000)**，免 root）：

```bash
adb push cloudphone-agent /data/local/tmp/cloudphone-agent
adb push libsys_core.so   /data/local/tmp/libsys_core.so
adb shell "sh -c 'export CP_AGENT_JAR=/data/local/tmp/libsys_core.so; \
  setsid nohup env GODEBUG=asyncpreemptoff=1 /data/local/tmp/cloudphone-agent \
  > /data/local/tmp/cloudphone-agent.log 2>&1 &'"
```

关键点：

1. **`libsys_core.so` 就是改造过的 scrcpy-server**：解包后是 APK 结构
   （`classes.dex` 216 KB + `AndroidManifest.xml`），包名改为 `com.android.helper`，
   入口 `CoreService`，用 `app_process` + `CLASSPATH` 启动；DEX 内残留
   `The server version (3.3.4-2af7ccc1) does not match the client` → fork 自 **scrcpy-server 3.3.4**。
   另有 SHA256 自校验（`INTEGRITY ERROR: libsys_core.so hash mismatch!`）。
2. **单 socket 拆成四条独立 UDS**：`uds_sys_v_ / uds_sys_a_ / uds_sys_c_ / uds_sys_t_`
   = video / audio / control / **touch**。touch 独立通道不与控制命令排队，是其低延迟触控的关键。
3. **注入走反射隐藏 API**：DEX 内有 `com/android/helper/wrappers/InputManager`、
   `getInjectInputEventMethod`、`MotionEvent$PointerCoords/PointerProperties`（多指）。
   二进制内明确要求降权：
   > `Since scrcpy-server is running as root (uid 0), please try running cloudphone-agent with the '-root' flag to drop privileges to shell (uid 2000).`
   → **root 反而注入失败（SecurityException），必须 shell(2000)**。
4. **IDR 缓存 + 按需关键帧**：浏览器发 RTCP PLI → agent 经 control 通道请求关键帧 →
   缓存 IDR，新客户端接入直接注入（`[KeyframeTrace] ... reason=no-cached-idr`），
   多客户端复用同一路编码流（`multiplexing stream directly`）。
5. **动态码率 + 高低 profile 切换**：无观众时跑低码率预览，有 WebRTC 客户端接入切高码率
   （`[Agent] Dynamically adjusted scrcpy-server video bitrate to %d bps`）。
6. 其他：Camera HAL 注入推流、WebADB 终端（agent 提供裸 PTY shell）、文件 HTTP Range 断点续传。

### 3.3 WebRTC 层：Pion（Go）

Pion 独有报错串（`UDPMuxDefault should not listening on unspecified address...`、
`handleNonMediaBandwidthProbe`、`NAT1To1IP`、`goupnp`）确证；SDP 常量：码率提示
`x-google-max-bitrate=10000000;start=5000000;min=2000000`，H.264 profile
`42001f/42e01f/4d001f/64001f`，音频 `opus/G722/PCMU/PCMA`；支持 TWCC 拥塞控制、
IPv6 双栈、UPnP 端口映射、mDNS 候选、TURN 兜底。

### 3.4 服务端（Go 单二进制）

信令 WebSocket + REST（`/api/login`、`/api/shortcuts`、`/api/files/`、`/api/license`），
持久化 `tokens.json`/`leases.json`（设备租约）/`shares.json`（卡密）/`shortcuts.json`；
agent 注册握手（`agent_register` / `agent_register_ok`，超时退出）；`/proc/1/cgroup` 容器探测。

## 4. 方案对比（含安全上下文约束）

| 方案 | 需 HTTPS | 需转码 | 可达帧率 | 工作量 | 备注 |
| --- | --- | --- | --- | --- | --- |
| MJPEG + `<img>` | 否 | 否 | 1–3 fps | 小 | 纯标准库可行，`multipart/x-mixed-replace` |
| H.264 over WS + **MSE** | 否 | 需转 fMP4 | 15–30 fps | 中 | 无需解码，靠浏览器原生解码 |
| H.264 over WS + **WebCodecs** | **是** | 否 | 30–60 fps | 中 | `VideoDecoder` 仅安全上下文 |
| **WebRTC**（设备端 agent 直推） | 否 | 否 | 30–60 fps | 大 | 真正的低延迟方案，但须在设备端有 agent |
| WebRTC（Mac 中转） | 否 | 是或需 GStreamer 透传 | 受第一跳限制 | 大 | aiortc 不支持已编码流直通（PR #434 closed 未合并） |
| 现状：screencap 轮询 | 否 | — | ~0.5 fps | 已有 | 单帧 1.8 s |

## 5. 对本项目的建议

1. **短期**：把 `/api/screenshot` 的轮询升级为 MJPEG `multipart/x-mixed-replace`
   （浏览器 `<img>` 原生支持，零解码、零依赖），可把「15 秒一张」提升到 1–3 fps 的连续预览。
   注意 `BaseHTTPRequestHandler` 默认 HTTP/1.0，流式响应需自行处理长连接与断连。
2. **中期**：若确实需要 15 fps 以上，走 H.264 over WebSocket + MSE（转 fMP4），
   绕开 WebCodecs 的 HTTPS 限制；视频源见下条。
3. **视频源**：优先复用 **scrcpy-server**（macOS 上已装 `scrcpy 4.1`，
   `/opt/homebrew/share/scrcpy/scrcpy-server`）——官方就提供「采集 + 硬编 + 注入」，
   只需自己实现一个最小 scrcpy client 消费其 video/control socket。
   注意官方不提供第三方裸流接口，`--v4l2-sink` 仅 Linux。
4. **注入后端**：验证 `scrcpy-server`（shell 身份）作为第三种注入方式，
   规避非 root 下 minitouch 打不开 `/dev/input` 的问题。
5. **不建议整体移植** ScrcpyOverWebRTC：核心（服务端 + agent + 定制 scrcpy-server）闭源且有
   许可限制；其架构假设「设备端有 agent」，与本项目「Mac 调度 + adb」的形态叠加成本高，
   且 agent 的输入注入会与调度器争抢设备、打乱状态机（需配合 `/api/runner/stop` 接管模式）。

## 6. 验证记录

（本节由后续实测补充：scrcpy-server 启动 / 视频流消费 / 触摸注入 / 与调度器并发影响）
