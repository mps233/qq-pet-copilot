"""adb 设备封装：设备检测与连接管理、屏幕属性读取、adb 命令管道。

画面截图与点击/滑动等操控已改由 uiautomator2 负责（见 src/u2dev.py），
这里只保留 u2 连接前的 adb server/设备在线管理，以及 main.py
嵌入 scrcpy 时需要的屏幕宽高比读取。
"""
from __future__ import annotations

import re
import socket
import subprocess
import sys
import time

from ..progress import log

# Windows 下隐藏 adb 子进程的命令行窗口（exe 无控制台模式下每次调用都会闪窗）
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# adb connect 输出里代表"adb server 自身网络栈异常"的关键词（而不是目标设备不在线）。
# 长期运行的 adb server 会这样：连任何局域网目标都报 No route to host，只有回环上的
# 模拟器还能用（实测跑满 5 天的 server 连手机和路由器都报不可达，kill-server 才好）。
# 单看这个词不足以定罪——手机真的关机/掉出 Wi-Fi 时内核也会回 no route to host
# ——所以还要配合 _tcp_reachable() 直连目标端口交叉验证（见 _recover_unreachable）。
_SERVER_BROKEN_HINTS = ("no route to host", "cannot connect to daemon", "protocol fault")

# 判断"adb 连不上但目标本身可达"用的 TCP 探测超时（秒）
_TCP_PROBE_TIMEOUT = 3.0


# 模拟器 ro.hardware 的已知取值（真机是 qcom/mtXXXX 等平台名，不会是这些）
_EMU_HARDWARE = ("goldfish", "ranchu", "vbox86", "vbox86p", "nox", "ttvm_hdragon", "houdini")
# 模拟器 ro.product.model 中的特征词
_EMU_MODEL_WORDS = ("sdk", "emulator", "google_sdk", "droid4x", "nox",
                    "mumu", "ldplayer", "bluestacks", "memu")
# 模拟器 ro.product.manufacturer 的已知取值
_EMU_MANUFACTURERS = ("genymotion",)


class AdbError(RuntimeError):
    pass


def _tcp_reachable(host: str, port: int, timeout: float = _TCP_PROBE_TIMEOUT) -> bool:
    """用本机内核直连 host:port，判断目标是否真的可达（绕过 adb server）。

    用来区分"adb server 网络栈坏了"和"手机真的不在线"：TCP 连得上说明网络与
    目标 adb 端口都正常，连不上才可能是设备侧问题。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _split_serial(serial: str) -> tuple[str, int] | None:
    """拆 'host:port' 形式的 serial；不是远程串口（USB 真机/emulator-xxxx）返回 None。"""
    if ":" not in (serial or ""):
        return None
    host, _, port_s = serial.rpartition(":")
    try:
        return host, int(port_s)
    except ValueError:
        return None


class Device:
    def __init__(self, adb_path: str, serial: str = ""):
        self.adb = adb_path
        self.serial = serial

    # ---- 基础命令 ----

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        proc = subprocess.run(cmd, capture_output=True, timeout=60,
                              creationflags=_NO_WINDOW)
        if check and proc.returncode != 0:
            raise AdbError(
                f"adb 命令失败: {' '.join(cmd)}\n{proc.stderr.decode('utf-8', 'replace')}"
            )
        return proc

    # ---- 设备状态 ----

    def online_devices(self) -> list[str]:
        """返回在线设备序列号列表（不依赖 self.serial）。"""
        proc = subprocess.run(
            [self.adb, "devices"], capture_output=True, timeout=30, check=True,
            creationflags=_NO_WINDOW,
        )
        serials = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    def ensure_connected(self) -> str:
        """确认 adb server 已启动且有设备在线；未指定序列号时选中第一台。返回序列号。

        指定了 host:port 形式的远程设备（无线真机/模拟器）而它不在线时会自动补一次
        adb connect；若 connect 报"不可达"但目标端口本机内核直连可达，说明是 adb
        server 自身网络栈坏了（长期运行常见），重启 server 并恢复其它远程设备后再试
        （见 _recover_unreachable）。
        """
        self._run("start-server")
        devices = self.online_devices()
        if self.serial:
            if self.serial not in devices:
                devices = self._recover_unreachable(devices)
            if self.serial not in devices:
                raise AdbError(f"指定设备 {self.serial} 不在线，当前在线: {devices or '无'}")
            return self.serial
        if not devices:
            raise AdbError("没有在线的 adb 设备，请检查 USB 连接与调试授权。")
        self.serial = devices[0]
        return self.serial

    def _recover_unreachable(self, devices: list[str]) -> list[str]:
        """目标远程设备不在线时自救：先 adb connect，必要时重启 adb server。返回最新在线列表。

        只处理 host:port 形式的 serial（USB 真机不适用；emulator-* 别名由 adb 自己
        重连，不需要手动 connect）。

        重启 server 的条件 = adb 侧连不上（报不可达，或 connect 直接超时挂住）
        **且** 本机内核直连目标端口正常：证明网络与设备 adb 端口都没问题，毛病在
        server 自己。设备真的关机/掉出 Wi-Fi 时 TCP 探测同样失败，不会误重启。
        """
        target = _split_serial(self.serial)
        if target is None:
            return devices
        host, port = target
        out, timed_out = self._connect_output(self.serial)
        if not timed_out and out and "failed" not in out.lower():
            return self.online_devices()  # connect 成功（connected to / already connected to）
        broken = timed_out or any(h in (out or "").lower() for h in _SERVER_BROKEN_HINTS)
        if broken and _tcp_reachable(host, port):
            reason = 'adb connect 超时挂住' if timed_out else f'adb 报告不可达（{out}）'
            log(f'{reason}，但本机直连 {self.serial} 正常，'
                f'疑似 adb server 网络栈异常，重启 adb server 后重试')
            self._restart_server_restoring_remotes(devices)
            return self.online_devices()
        return self.online_devices()

    def _connect_output(self, serial: str) -> tuple[str, bool]:
        """执行 adb connect，返回 (输出文本, 是否超时)。

        adb connect 的失败信息走 stdout 且退出码仍为 0，只能靠文本判断；server
        挂起时命令会超时，返回 (空串, True) 让调用方走重启 server 分支。
        """
        try:
            proc = subprocess.run([self.adb, "connect", serial], capture_output=True,
                                  timeout=15, creationflags=_NO_WINDOW, check=False)
        except subprocess.TimeoutExpired:
            return "", True
        except Exception:  # noqa: BLE001 - 其它连接层失败按"connect 未成功"处理
            return "", False
        return (proc.stdout.decode("utf-8", "replace")
                + proc.stderr.decode("utf-8", "replace")).strip(), False

    def _restart_server_restoring_remotes(self, known: list[str]) -> None:
        """重启 adb server，并重新 connect 之前在线过的所有远程设备。

        kill-server 会把本机所有设备（其它模拟器实例、USB 真机）踢下线，而 host:port
        条目不会自动恢复——必须逐个 connect 回来，否则修好一个设备会弄丢其它设备。
        每步都不抛异常：server 已经不正常时，尽力恢复优于中断启动流程。
        """
        remotes = [s for s in known if _split_serial(s) is not None and s != self.serial]
        for args in (("kill-server",), ("start-server",)):
            try:
                subprocess.run([self.adb, *args], capture_output=True, timeout=30,
                               creationflags=_NO_WINDOW, check=False)
            except Exception as e:  # noqa: BLE001 - 卡死时超时，继续走后续 connect
                log(f'adb {" ".join(args)} 执行异常（继续尝试恢复）: {e}')
        if remotes:
            log(f'adb server 已重启，恢复其它远程设备连接: {", ".join(remotes)}')
        for s in [*remotes, self.serial]:
            self._connect_output(s)

    def connect_remote(self, serial: str | None = None) -> None:
        """尝试 adb connect 远程设备（模拟器 127.0.0.1:xxxx）。

        模拟器（MuMu/雷电等）自带 adb 端口，但设备可能尚未出现在 adb devices；
        这里先 connect 一下。adb connect 不区分设备，不能带 -s；失败不抛错
        （可能本来就已连接，交给后续 ensure_connected 判断）。

        注意：这里**故意不吞** subprocess.TimeoutExpired（与 _connect_output 不同）
        ——recover._adb_back_online 靠这个异常统计连续超时次数，判定 adb 服务是否卡死。
        """
        target = serial or self.serial
        if not target or ":" not in target:
            return
        subprocess.run([self.adb, "connect", target], capture_output=True, timeout=10,
                       creationflags=_NO_WINDOW, check=False)

    def getprop(self, name: str) -> str:
        """读设备属性（ro.product.model 等），失败/为空返回空串。"""
        proc = self._run("shell", "getprop", name, check=False)
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", "replace").strip()

    def screen_size(self) -> tuple[int, int]:
        out = self._run("shell", "wm", "size").stdout.decode("utf-8", "replace")
        # 形如 "Physical size: 1080x2400"
        size = out.strip().split(":")[-1].strip().split("x")
        return int(size[0]), int(size[1])

    def reboot_and_wait(self, timeout: float = 180.0, interval: float = 5.0) -> None:
        """重启设备并等待开机完成（sys.boot_completed=1），超时抛 AdbError。"""
        self._run("reboot")
        self.wait_boot_completed(timeout, interval)

    def wait_boot_completed(self, timeout: float = 180.0, interval: float = 5.0) -> None:
        """轮询等开机完成（sys.boot_completed=1），超时抛 AdbError。

        开机/掉线过程中设备反复 offline/online，wait-for-device 容易卡在
        子进程超时上，改为轮询 getprop：未就绪时 adb 直接报错返回，继续等。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(interval)
            proc = self._run("shell", "getprop", "sys.boot_completed", check=False)
            if proc.returncode == 0 and proc.stdout.decode("utf-8", "replace").strip() == "1":
                return
        raise AdbError(f"{timeout:.0f}s 内未完成开机")

    def force_stop_app(self, package: str) -> None:
        """强停应用（重启游戏恢复用，如 QQ）。"""
        self._run('shell', 'am', 'force-stop', package)

    def launch_app(self, package: str) -> None:
        """用 monkey 启动应用主 Activity（无需知道具体 Activity 名）。"""
        self._run("shell", "monkey", "-p", package,
                  "-c", "android.intent.category.LAUNCHER", "1")

    def is_emulator(self) -> bool:
        """根据 getprop 的具体键值判断是否为模拟器。

        不能在整个 getprop 输出里做子串匹配：真机也可能带
        ro.kernel.qemu.gles=0 这类属性（高通内核残留，值为 0 表示非 qemu），
        必须按键解析、按值/已知取值判断。
        """
        out = self._run("shell", "getprop").stdout.decode("utf-8", "replace").lower()
        props = dict(re.findall(r"^\[(.+?)\]: \[(.*)\]$", out, re.M))
        # qemu 标志位必须为 1 才算（值为 0 是"支持但未启用"）
        if props.get("ro.kernel.qemu") == "1" or props.get("qemu.hw.mainkeys") == "1":
            return True
        if "emulator" in props.get("ro.build.characteristics", ""):
            return True
        if props.get("ro.hardware") in _EMU_HARDWARE:
            return True
        if props.get("ro.product.board") == "goldfish":
            return True
        if props.get("ro.product.manufacturer") in _EMU_MANUFACTURERS:
            return True
        model = props.get("ro.product.model", "")
        return any(w in model for w in _EMU_MODEL_WORDS)
