#!/usr/bin/env python3
"""scrcpy-server 设备端能力探测：验证「H.264 视频流消费」与「触摸/按键注入」。

官方 scrcpy-server（scrcpy 自带，macOS 在 /opt/homebrew/share/scrcpy/scrcpy-server）
本身就是一个「设备端采集 + 硬编 + 输入注入」服务：以 shell(2000) 身份经
`app_process` 启动，通过 abstract socket 输出 H.264，并通过 control socket
反射调用 `InputManager.injectInputEvent` 注入事件——**不需要 root、不碰 /dev/input**。
（这正是 minitouch 在非 root/SELinux 受限时的替代注入路径。）

协议依据 scrcpy 官方 doc/develop.md：
  [dummy byte 1B]（仅 tunnel_forward）+ [设备名 64B] + [codec 4B] + [session packet 12B]
  之后每帧 = 12B 头（media/config/keyframe 标志位 + PTS 61b + size 32b）/ 裸 H.264

用法（注入前请先停调度器：curl -X POST localhost:8787/api/runner/stop）：
    python3 tools/scrcpy_agent_probe.py probe [秒数]      # 统计帧率/码率（只读）
    python3 tools/scrcpy_agent_probe.py tap X Y           # 设备坐标点击（自动换算坐标系）
    python3 tools/scrcpy_agent_probe.py key 4             # 注入按键（4=BACK）
    python3 tools/scrcpy_agent_probe.py probe --keep      # 保留已启动的 server

坑位记录（实测踩过）：
1. 触摸坐标必须落在**视频尺寸**坐标系里且 screen_size 传视频尺寸——scrcpy server 的
   PositionMapper 会校验，不匹配则**静默丢弃**事件（无任何报错）。
2. server 必须 setsid/nohup 启动，否则 adb shell 会话结束会连带被杀。
3. scid 是 31 位十六进制整数，超范围 server 直接抛 NumberFormatException。
"""
from __future__ import annotations

import argparse
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

SERVER_CANDIDATES = [
    Path('/opt/homebrew/share/scrcpy/scrcpy-server'),
    APP_ROOT / 'resources' / 'scrcpy-win64' / 'scrcpy-server',
]
DEVICE_JAR = '/data/local/tmp/scrcpy-server.jar'
DEVICE_LOG = '/data/local/tmp/scrcpy_srv.log'
SCID = '2f99977e'                      # 固定值便于复用 forward；31 位内十六进制
VIDEO_PORT, CTRL_PORT = 27183, 27184

# scrcpy 控制消息
T_KEYCODE, T_TOUCH = 0, 2
AKEY_DOWN, AKEY_UP = 0, 1
AMOTION_DOWN, AMOTION_UP, AMOTION_MOVE = 0, 1, 2


def load_device() -> tuple[str, str]:
    """从 config.yaml 读 (adb 路径, 设备序列号)，失败则回退 PATH 里的 adb。"""
    try:
        import yaml
        cfg = yaml.safe_load((APP_ROOT / 'config.yaml').read_text('utf-8')) or {}
        adb_cfg = cfg.get('adb') or {}
        return (adb_cfg.get('path') or 'adb'), (adb_cfg.get('device_serial') or '')
    except Exception:
        return 'adb', ''


class Probe:
    def __init__(self, args):
        self.args = args
        self.adb_path, serial = load_device()
        self.serial = args.serial or serial
        self.server = Path(args.server) if args.server else self._find_server()
        self.vs = self.cs = None
        self.dev_w = self.dev_h = 0
        self.vw = self.vh = 0

    def _find_server(self) -> Path:
        for p in SERVER_CANDIDATES:
            if p.is_file():
                return p
        raise SystemExit('未找到 scrcpy-server，请用 --server 指定路径')

    def adb(self, *a, timeout=60):
        cmd = [self.adb_path] + (['-s', self.serial] if self.serial else []) + list(a)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    # ---- server 生命周期 ----
    def push_and_start(self):
        print(f'推送 {self.server.name} → {DEVICE_JAR}')
        r = self.adb('push', str(self.server), DEVICE_JAR)
        if r.returncode != 0:
            raise SystemExit(f'push 失败：{r.stderr.strip()}')
        self.adb('shell', f'pkill -f com.genymobile.scrcpy.Server')
        cmd = (f'CLASSPATH={DEVICE_JAR} setsid nohup app_process / '
               f'com.genymobile.scrcpy.Server {self.args.version} scid={SCID} '
               f'log_level=info video=true audio=false control=true '
               f'max_size={self.args.max_size} max_fps={self.args.max_fps} '
               f'tunnel_forward=true cleanup=false > {DEVICE_LOG} 2>&1 < /dev/null &')
        self.adb('shell', f"sh -c '{cmd}'")
        time.sleep(2.5)
        out = self.adb('shell', "ps -A -o PID,ARGS | grep -c com.genymobile.scrcpy.Server").stdout
        if out.strip() in ('', '0'):
            log = self.adb('shell', f'cat {DEVICE_LOG}').stdout
            raise SystemExit(f'server 未启动，日志：\n{log[:500]}')
        for port in (VIDEO_PORT, CTRL_PORT):
            self.adb('forward', f'tcp:{port}', f'localabstract:scrcpy_{SCID}')

    def stop(self, clean: bool):
        self.adb('shell', 'pkill -f com.genymobile.scrcpy.Server')
        for port in (VIDEO_PORT, CTRL_PORT):
            self.adb('forward', '--remove', f'tcp:{port}')
        if clean:
            self.adb('shell', f'rm -f {DEVICE_JAR} {DEVICE_LOG}')

    # ---- 协议 ----
    @staticmethod
    def _rx(sock, n) -> bytes:
        buf = b''
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError(f'连接关闭（{len(buf)}/{n}）')
            buf += chunk
        return buf

    def connect(self):
        self.vs = socket.create_connection(('127.0.0.1', VIDEO_PORT), timeout=10)
        self.cs = socket.create_connection(('127.0.0.1', CTRL_PORT), timeout=10)
        self._rx(self.vs, 1)                                  # dummy byte
        name = self._rx(self.vs, 64).rstrip(b'\x00').decode('utf-8', 'replace')
        codec = self._rx(self.vs, 4).decode('ascii', 'replace')
        spkt = self._rx(self.vs, 12)
        self.vw, self.vh = struct.unpack('>II', spkt[4:12])
        size = self.adb('shell', 'wm size').stdout
        for tok in size.replace(':', ' ').split():
            if 'x' in tok and tok[0].isdigit():
                self.dev_w, self.dev_h = (int(v) for v in tok.split('x'))
                break
        print(f'server 就绪：device={name} codec={codec} '
              f'视频 {self.vw}x{self.vh}（设备 {self.dev_w}x{self.dev_h}）')

    def to_video(self, x: int, y: int) -> tuple[int, int]:
        """设备坐标 → 视频坐标系（触摸事件的坐标系必须与视频尺寸一致）。"""
        return (int(round(x * self.vw / self.dev_w)),
                int(round(y * self.vh / self.dev_h)))

    def inject_key(self, keycode: int):
        for action in (AKEY_DOWN, AKEY_UP):
            self.cs.sendall(struct.pack('>BBIII', T_KEYCODE, action, keycode, 0, 0))
            time.sleep(0.05)

    def inject_touch(self, action: int, x: int, y: int):
        self.cs.sendall(struct.pack('>BBQIIHHHII', T_TOUCH, action, 0, x, y,
                                    self.vw, self.vh, 0xFFFF, 1, 1))

    # ---- 动作 ----
    def probe(self, seconds: float):
        self.vs.settimeout(5)
        frames = nbytes = keys = 0
        first = None
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                hdr = self._rx(self.vs, 12)
            except (EOFError, socket.timeout) as e:
                print(f'读流结束：{type(e).__name__}')
                break
            pts_flags, size = struct.unpack('>QI', hdr)
            if pts_flags & (1 << 63):        # session packet
                self._rx(self.vs, size)
                continue
            if first is None:
                first = time.time() - t0
            self._rx(self.vs, size)
            frames += 1
            nbytes += size
            keys += int(bool(pts_flags & (1 << 62)))
        el = time.time() - t0
        if not frames:
            print('未收到视频帧（屏幕静止时 scrcpy 按需推帧，可先滑动屏幕再测）')
            return
        print(f'首帧 {first * 1000:.0f} ms | {frames} 帧/{el:.1f}s = {frames / el:.1f} fps | '
              f'{nbytes * 8 / el / 1e6:.2f} Mbps | 平均 {nbytes / frames / 1024:.1f} KB/帧 | '
              f'关键帧 {keys}')

    def tap(self, x: int, y: int):
        vx, vy = self.to_video(x, y)
        print(f'点击 设备({x},{y}) → 视频({vx},{vy})')
        self.inject_touch(AMOTION_DOWN, vx, vy)
        time.sleep(0.06)
        self.inject_touch(AMOTION_UP, vx, vy)

    def swipe(self, x1, y1, x2, y2, steps=10):
        self.inject_touch(AMOTION_DOWN, *self.to_video(x1, y1))
        for i in range(1, steps + 1):
            self.inject_touch(AMOTION_MOVE,
                              *self.to_video(x1 + (x2 - x1) * i // steps,
                                             y1 + (y2 - y1) * i // steps))
            time.sleep(0.012)
        self.inject_touch(AMOTION_UP, *self.to_video(x2, y2))


def main():
    ap = argparse.ArgumentParser(description='scrcpy-server 设备端能力探测')
    ap.add_argument('action', choices=['probe', 'tap', 'key', 'swipe'])
    ap.add_argument('value', nargs='*', help='probe: 秒数 / tap: X Y / key: 键码 / swipe: X1 Y1 X2 Y2')
    ap.add_argument('--serial', default='')
    ap.add_argument('--server', default='', help='scrcpy-server 路径')
    ap.add_argument('--version', default='4.1', help='scrcpy 版本号（须与 server 一致）')
    ap.add_argument('--max-size', default='720')
    ap.add_argument('--max-fps', default='30')
    ap.add_argument('--keep', action='store_true', help='结束后保留 server 与 jar')
    args = ap.parse_args()

    p = Probe(args)
    try:
        p.push_and_start()
        p.connect()
        if args.action == 'probe':
            p.probe(float(args.value[0]) if args.value else 5.0)
        elif args.action == 'tap':
            p.tap(int(args.value[0]), int(args.value[1]))
            time.sleep(0.5)
        elif args.action == 'key':
            p.inject_key(int(args.value[0]))
            time.sleep(0.5)
        elif args.action == 'swipe':
            p.swipe(*(int(v) for v in args.value[:4]))
            time.sleep(0.5)
    finally:
        if p.vs:
            p.vs.close()
        if p.cs:
            p.cs.close()
        p.stop(clean=not args.keep)
    return 0


if __name__ == '__main__':
    sys.exit(main())
