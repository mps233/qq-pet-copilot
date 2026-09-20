"""手机屏幕实时流：scrcpy-server 按需启停 + ffmpeg 转 HLS(fMP4)，供 dashboard 浏览器播放与控制。

设计要点
--------
* **纯标准库**：只用 socket / subprocess / threading；外部依赖仅系统 `ffmpeg` 与
  `scrcpy-server` 二进制（scrcpy 自带，macOS 在 /opt/homebrew/share/scrcpy/）。
* **免 root**：scrcpy-server 由 `app_process` 以 shell(2000) 身份运行，采集走
  SurfaceControl/MediaCodec，注入走反射 `InputManager.injectInputEvent`
  —— 不需要 root，也不碰 /dev/input（minitouch 在非 root/SELinux 下的替代路径）。
* **按需启停**：有观众才启动，空闲 `IDLE_STOP_SECONDS` 自动停 —— 挂机期间零编码开销。
* **播放双路径**：ffmpeg 输出 fMP4 型 HLS，iOS Safari 用原生 `<video>` 直接播；
  桌面浏览器（Chrome 不支持原生 HLS）由前端 MSE 拉同一份 fMP4 分片播放。
* **坐标换算**：scrcpy-server 的 PositionMapper 要求触摸事件的坐标系等于当前视频尺寸，
  不匹配会**静默丢弃**事件，因此对外统一收设备坐标、内部换算到视频坐标系。

协议依据 scrcpy 官方 doc/develop.md（dummy byte / 64B 设备名 / codec / session packet /
12B 帧头）。已知坑见 tools/scrcpy_agent_probe.py docstring。
"""
from __future__ import annotations

import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from .config import APP_ROOT, load_config

# ---- scrcpy-server ----
SCID = '2f99977e'                       # 31 位内十六进制，固定值便于复用 adb forward
VIDEO_PORT, CTRL_PORT = 27183, 27184
DEVICE_JAR = '/data/local/tmp/scrcpy-server.jar'
DEVICE_LOG = '/data/local/tmp/scrcpy_srv.log'
SCRCPY_VERSION = '4.1'
MAX_SIZE = '720'                        # 预览够用且省带宽；改大需同步注意坐标换算（自动处理）
MAX_FPS = '30'
SERVER_CANDIDATES = [
    Path('/opt/homebrew/share/scrcpy/scrcpy-server'),
    APP_ROOT / 'resources' / 'scrcpy-win64' / 'scrcpy-server',
]

# ---- HLS 输出 ----
STREAM_DIR = APP_ROOT / 'runs' / 'stream'
IDLE_STOP_SECONDS = 60                  # 无访问多久后自动停止（前端还有 3 秒一次的 ping 保活）
START_TIMEOUT = 20.0
HLS_SEGMENT_SECONDS = 0.3               # 分片时长 = 固有延迟下限（0.3s ≈ 10 帧 @30fps）

# ---- scrcpy 控制消息（control_msg.c）----
T_KEYCODE, T_TOUCH = 0, 2
AKEY_DOWN, AKEY_UP = 0, 1
AMOTION_DOWN, AMOTION_UP, AMOTION_MOVE = 0, 1, 2


def _log(msg: str) -> None:
    print(f'[stream {time.strftime("%H:%M:%S")}] {msg}', flush=True)


class ScreenStream:
    """单例式流控制器（模块级 get_stream() 获取）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._watchdog_stop = threading.Event()
        self._stop_flag = threading.Event()
        self._watchdog = None
        self._ffmpeg: subprocess.Popen | None = None
        self._vs: socket.socket | None = None      # video socket
        self._cs: socket.socket | None = None      # control socket
        self.dev_w = self.dev_h = 0
        self.video_w = self.video_h = 0
        self.error = ''
        self.started_at = 0.0
        self.last_access = 0.0

    # ---------------- 设备侧工具 ----------------
    @staticmethod
    def _adb_conf() -> tuple[str, str]:
        cfg = load_config()
        return (cfg.adb.path or 'adb'), (cfg.adb.device_serial or '')

    def _adb(self, *args: str, timeout: int = 60):
        adb_path, serial = self._adb_conf()
        cmd = [adb_path] + (['-s', serial] if serial else []) + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def _find_server() -> Path:
        for p in SERVER_CANDIDATES:
            if p.is_file():
                return p
        raise RuntimeError('未找到 scrcpy-server（brew install scrcpy 或放到 resources/）')

    @staticmethod
    def _find_ffmpeg() -> str:
        exe = shutil.which('ffmpeg')
        if not exe:
            raise RuntimeError('未找到 ffmpeg（转封装 HLS 需要，brew install ffmpeg）')
        return exe

    @staticmethod
    def _rx(sock: socket.socket, n: int) -> bytes:
        buf = b''
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError(f'socket 关闭（{len(buf)}/{n}）')
            buf += chunk
        return buf

    # ---------------- 生命周期 ----------------
    @property
    def running(self) -> bool:
        return bool(self._ffmpeg and self._ffmpeg.poll() is None)

    def touch_access(self) -> None:
        """标记有观众（每次 HTTP 拉流/状态查询时调用），用于空闲回收。"""
        self.last_access = time.time()

    def ensure_running(self) -> None:
        """幂等启动；失败时把原因写进 self.error（不抛给 HTTP 层）。"""
        with self._lock:
            self.last_access = time.time()
            self._start_watchdog()
            if self.running:
                return
            try:
                self._start_locked()
                self.error = ''
            except Exception as e:                        # noqa: BLE001 - 统一转成状态给前端
                self.error = f'{type(e).__name__}: {e}'
                _log(f'启动失败：{self.error}')
                self._cleanup_locked()

    def _start_locked(self) -> None:
        server = self._find_server()
        ffmpeg = self._find_ffmpeg()
        _log(f'启动流（server={server.name} ffmpeg={ffmpeg}）')
        self._cleanup_locked(stop_device=True)

        STREAM_DIR.mkdir(parents=True, exist_ok=True)
        for f in STREAM_DIR.glob('*'):
            f.unlink(missing_ok=True)

        r = self._adb('push', str(server), DEVICE_JAR)
        if r.returncode != 0:
            raise RuntimeError(f'push scrcpy-server 失败：{r.stderr.strip()[:200]}')

        # i-frame-interval 实测被这台设备的硬件编码器忽略（4 秒仍只有 1 个关键帧），
        # 真正让 HLS 按时间切分片的是 ffmpeg 的 split_by_time；这里仍传一份：
        # 支持的设备能顺便压短 GOP（注意必须是整数，写 0.5 会被 server 解析报错）。
        opts = 'video_codec_options=i-frame-interval:int=1'
        shell_cmd = (
            f'CLASSPATH={DEVICE_JAR} setsid nohup app_process / '
            f'com.genymobile.scrcpy.Server {SCRCPY_VERSION} scid={SCID} log_level=info '
            f'video=true audio=false control=true max_size={MAX_SIZE} max_fps={MAX_FPS} '
            f'tunnel_forward=true cleanup=false {opts} > {DEVICE_LOG} 2>&1 < /dev/null &'
        )
        self._adb('shell', f"sh -c '{shell_cmd}'")
        time.sleep(2.0)

        alive = self._adb('shell', 'ps -A -o PID,ARGS | grep -c com.genymobile.scrcpy.Server').stdout
        if alive.strip() in ('', '0'):
            detail = self._adb('shell', f'cat {DEVICE_LOG}').stdout.strip()[-300:]
            raise RuntimeError(f'scrcpy-server 未启动：{detail}')

        for port in (VIDEO_PORT, CTRL_PORT):
            self._adb('forward', f'tcp:{port}', f'localabstract:scrcpy_{SCID}')

        deadline = time.time() + START_TIMEOUT
        while True:
            try:
                self._connect_sockets()
                break
            except OSError as e:
                if time.time() > deadline:
                    raise RuntimeError(f'连接 scrcpy socket 超时：{e}') from e
                time.sleep(0.5)

        self._start_ffmpeg(ffmpeg)
        self._stop_flag.clear()
        threading.Thread(target=self._reader_loop, name='stream-reader', daemon=True).start()
        self.started_at = time.time()
        _log(f'流已就绪：视频 {self.video_w}x{self.video_h}，设备 {self.dev_w}x{self.dev_h}')

    def _connect_sockets(self) -> None:
        vs = socket.create_connection(('127.0.0.1', VIDEO_PORT), timeout=10)
        cs = socket.create_connection(('127.0.0.1', CTRL_PORT), timeout=10)
        self._rx(vs, 1)                                   # dummy byte（tunnel_forward）
        self._rx(vs, 64)                                  # 设备名
        codec = self._rx(vs, 4).decode('ascii', 'replace')
        session = self._rx(vs, 12)
        self.video_w, self.video_h = struct.unpack('>II', session[4:12])
        if codec.lower() not in ('h264', 'h265'):
            raise RuntimeError(f'意外的视频编码：{codec}')
        if not (self.dev_w and self.dev_h):
            for tok in self._adb('shell', 'wm size').stdout.replace(':', ' ').split():
                if 'x' in tok and tok[0].isdigit():
                    self.dev_w, self.dev_h = (int(v) for v in tok.split('x'))
                    break
        self._vs, self._cs = vs, cs

    def _start_ffmpeg(self, ffmpeg: str) -> None:
        cmd = [
            ffmpeg, '-hide_banner', '-loglevel', 'warning', '-y',
            # 裸 H.264（Annex-B）不带时间戳，必须显式生成：否则 hls muxer 判不出分片边界，
            # 实测表现为只出 init.mp4、直到进程结束才 flush 出一个 0 时长的分片
            # （m3u8 里 #EXTINF:0.0006）。-use_wallclock_as_timestamps 按到达时间打戳，
            # 最贴合实时流；单靠 -framerate 在本机 ffmpeg 9.0 的 pipe 输入上无效。
            '-fflags', 'nobuffer+genpts', '-flags', 'low_delay',
            '-use_wallclock_as_timestamps', '1',
            '-framerate', MAX_FPS, '-f', 'h264', '-i', 'pipe:0',
            '-c', 'copy', '-an',
            '-f', 'hls',
            '-hls_time', str(HLS_SEGMENT_SECONDS),
            '-hls_list_size', '10',          # 窗口 0.3s × 10 = 3 秒：再小播放器会拉不到
                                             # 已被删的分片（404 → 断流，踩过）
            '-hls_flags', 'delete_segments+omit_endlist+split_by_time',
            '-hls_segment_type', 'fmp4',
            '-hls_fmp4_init_filename', 'init.mp4',
            '-hls_segment_filename', str(STREAM_DIR / 'seg%04d.m4s'),
            str(STREAM_DIR / 'index.m3u8'),
        ]
        self._ffmpeg = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, bufsize=0)
        # ffmpeg 的 warning 日志单独收集，便于排查（不阻塞主流程）
        threading.Thread(target=self._drain_ffmpeg_log, name='ffmpeg-log', daemon=True).start()

    def _drain_ffmpeg_log(self) -> None:
        proc = self._ffmpeg
        if not proc or not proc.stderr:
            return
        for raw in proc.stderr:
            line = raw.decode('utf-8', 'replace').strip()
            if line:
                _log(f'ffmpeg: {line[:200]}')

    def _reader_loop(self) -> None:
        """剥掉 scrcpy 的 12 字节帧头，把裸 H.264 写进 ffmpeg（config 包也要写，含 SPS/PPS）。"""
        try:
            while not self._stop_flag.is_set():
                hdr = self._rx(self._vs, 12)
                _flags, size = struct.unpack('>QI', hdr)
                data = self._rx(self._vs, size)
                if self._ffmpeg and self._ffmpeg.stdin:
                    self._ffmpeg.stdin.write(data)
        except Exception as e:                            # noqa: BLE001 - 断流/停止都会走到这里
            if not self._stop_flag.is_set():
                _log(f'读流结束：{type(e).__name__}: {e}')

    def stop(self, stop_device: bool = True) -> None:
        with self._lock:
            _log('停止流')
            self._cleanup_locked(stop_device=stop_device)

    def _cleanup_locked(self, stop_device: bool = False) -> None:
        self._stop_flag.set()
        for sock in (self._vs, self._cs):
            try:
                if sock:
                    sock.close()
            except OSError:
                pass
        self._vs = self._cs = None
        if self._ffmpeg:
            try:
                if self._ffmpeg.stdin:
                    self._ffmpeg.stdin.close()
            except OSError:
                pass
            self._ffmpeg.terminate()
            try:
                self._ffmpeg.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._ffmpeg.kill()
            self._ffmpeg = None
        if stop_device:
            try:
                self._adb('shell', 'pkill -f com.genymobile.scrcpy.Server')
                for port in (VIDEO_PORT, CTRL_PORT):
                    self._adb('forward', '--remove', f'tcp:{port}')
                self._adb('shell', f'rm -f {DEVICE_JAR} {DEVICE_LOG}')
            except Exception as e:                        # noqa: BLE001 - 清理失败不影响主流程
                _log(f'设备侧清理失败：{e}')
        self.started_at = self.video_w = self.video_h = 0

    def _start_watchdog(self) -> None:
        if self._watchdog and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()

        def loop() -> None:
            while not self._watchdog_stop.is_set():
                time.sleep(5)
                with self._lock:
                    if self.running and time.time() - self.last_access > IDLE_STOP_SECONDS:
                        _log(f'空闲 {IDLE_STOP_SECONDS}s，自动停止流')
                        self._cleanup_locked(stop_device=True)

        self._watchdog = threading.Thread(target=loop, name='stream-watchdog', daemon=True)
        self._watchdog.start()

    # ---------------- 注入 ----------------
    def _to_video(self, x: int, y: int) -> tuple[int, int]:
        if not (self.dev_w and self.dev_h and self.video_w and self.video_h):
            return x, y
        return (int(round(x * self.video_w / self.dev_w)),
                int(round(y * self.video_h / self.dev_h)))

    def _touch(self, action: int, x: int, y: int) -> None:
        self._cs.sendall(struct.pack('>BBQIIHHHII', T_TOUCH, action, 0, x, y,
                                     self.video_w, self.video_h, 0xFFFF, 1, 1))

    def inject_tap(self, x: int, y: int) -> None:
        with self._lock:
            if not (self.running and self._cs):
                raise RuntimeError('流未运行，无法注入')
            vx, vy = self._to_video(x, y)
            self._touch(AMOTION_DOWN, vx, vy)
            time.sleep(0.06)
            self._touch(AMOTION_UP, vx, vy)

    def inject_swipe(self, x1: int, y1: int, x2: int, y2: int, steps: int = 12) -> None:
        with self._lock:
            if not (self.running and self._cs):
                raise RuntimeError('流未运行，无法注入')
            self._touch(AMOTION_DOWN, *self._to_video(x1, y1))
            for i in range(1, steps + 1):
                self._touch(AMOTION_MOVE,
                            *self._to_video(x1 + (x2 - x1) * i // steps,
                                            y1 + (y2 - y1) * i // steps))
                time.sleep(0.012)
            self._touch(AMOTION_UP, *self._to_video(x2, y2))

    def inject_key(self, keycode: int) -> None:
        with self._lock:
            if not (self.running and self._cs):
                raise RuntimeError('流未运行，无法注入')
            for action in (AKEY_DOWN, AKEY_UP):
                self._cs.sendall(struct.pack('>BBIII', T_KEYCODE, action, keycode, 0, 0))
                time.sleep(0.05)

    # ---------------- 状态 ----------------
    def _probe_device_size(self) -> None:
        """读一次设备分辨率（只读 `wm size`，不会启动流）——供前端占位框按真实比例预留。"""
        try:
            out = self._adb('shell', 'wm size').stdout
            for tok in out.replace(':', ' ').split():
                if 'x' in tok and tok[0].isdigit():
                    self.dev_w, self.dev_h = (int(v) for v in tok.split('x'))
                    return
        except Exception as e:                             # noqa: BLE001 - 探测失败不影响主流程
            _log(f'读取设备分辨率失败：{e}')

    def status(self) -> dict:
        if not (self.dev_w and self.dev_h):
            self._probe_device_size()                      # 只探测一次，之后一直复用
        return {
            'running': self.running,
            'hls_ready': (STREAM_DIR / 'index.m3u8').is_file(),
            'video': f'{self.video_w}x{self.video_h}' if self.video_w else '',
            'device': f'{self.dev_w}x{self.dev_h}' if self.dev_w else '',
            'uptime': int(time.time() - self.started_at) if self.running and self.started_at else 0,
            'idle': int(time.time() - self.last_access) if self.last_access else 0,
            'idle_stop_seconds': IDLE_STOP_SECONDS,
            'error': self.error,
        }


_stream: ScreenStream | None = None


def get_stream() -> ScreenStream:
    """模块级单例（dashboard 多线程共用）。"""
    global _stream
    if _stream is None:
        _stream = ScreenStream()
    return _stream
