"""调度器单实例守卫：`runs/runner.pid` + `os.kill(pid, 0)`。

**为什么不用 pgrep/ps 判活**（血泪教训，2026-09-28 实测）：

仪表盘/GUI 原来判"调度器是否已在运行"是 `pgrep -f scenarios/runner.py` 拿到 pid，
再用 `ps -p <pid> -o command=` 读命令行复核。可 `ps` 一旦不可用（受限沙箱、
权限被拒、精简环境），复核拿到的命令行是空串 → 所有 pid 都被过滤掉 →
`alive` 恒为 False：
  * "已在运行不重复启动"的守卫失效 → 每点一次「启动」就多起一个调度器；
  * 「停止」也变得不认账（返回"本来就没有在运行"）。
本次实测一口气跑出 **3 个**调度器（1 个孤儿 + 2 个由同一个仪表盘起的），
三个进程同时驱动手机、共写同一份 `runs/queue_status.json`。

改成"内核判活"之后，判断只看 pid 是否还能收到信号（POSIX）/
OpenProcess + GetExitCodeProcess（Windows），**不依赖任何外部命令**。
调度器自己启动时也会写这个 pidfile 并在退出时清掉，所以：
  * 谁启动的都能被认出来（仪表盘 / GUI / 手工命令行）；
  * 被 SIGKILL 打死留下的陈旧 pidfile 会被自动识别为"死进程"并接管。

不用 flock 是为了跨平台一致（Windows 下没有 fcntl；msvcrt 的锁范围语义又很绕），
pidfile + 判活已经够用：误判的唯一来源是 PID 复用，而 pidfile 里同时记了启动时间与
命令行，`read_pidfile()` 会把明显对不上的记录当陈旧处理。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

#: PID 复用窗口内的兜底校验：pidfile 里的启动时间比这个还"未来"就认为不可信
_MAX_CLOCK_SKEW = 300.0


def pid_alive(pid: int) -> bool:
    """pid 对应进程是否还活着（不看它是什么进程，只看内核态度）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 进程存在，只是没权限发信号（例如属于别的用户）
    except OSError:
        return False
    return True


def read_pidfile(path: Path) -> dict | None:
    """读 pidfile；返回 {'pid','started','argv'} 或 None（不存在/坏掉/pid 已死）。"""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        pid = int(data.get('pid'))
    except Exception:
        return None
    if not pid_alive(pid):
        return None
    started = data.get('started')
    if isinstance(started, (int, float)) and started > time.time() + _MAX_CLOCK_SKEW:
        return None          # 时间戳来自"未来"：记录不可信，当陈旧处理
    return {'pid': pid, 'started': started, 'argv': data.get('argv') or []}


def write_pidfile(path: Path, argv: list[str] | None = None) -> None:
    """登记当前进程（原子写：先写 .tmp 再 replace，避免读到半个文件）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'pid': os.getpid(), 'started': time.time(), 'argv': argv or []}
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def clear_pidfile(path: Path, pid: int | None = None) -> None:
    """退出时清掉自己的登记（只清自己的，别把后来者的记录删了）。"""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if pid is not None and int(data.get('pid')) != pid:
            return
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _is_runner_cmdline(cmd: str) -> bool:
    """`pgrep -fl` 给的那行命令行，是不是"真·调度器进程"。

    必须校验（实测踩过两次误配）：
      * 一条 bash 命令里只要**文本**含 `scenarios/runner.py`（例如 curl/测试脚本里写了这个
        路径），`pgrep -f` 就会把那条 shell 当成调度器 → 仪表盘误报"已在运行 PID xxx"；
      * `--test` 是短命单测进程，也不能算。
    规则：① 可执行文件是 python；② 参数里确实有以 `runner.py` 结尾的 token。
    （Windows 上没有 pgrep，这条兜底自然为空，那边由 GUI 自己管子进程句柄。）
    """
    parts = (cmd or '').split()
    if len(parts) < 2:
        return False
    if 'python' not in parts[0].lower():
        return False
    return any(tok.endswith('runner.py') for tok in parts[1:])


def pgrep_runners(pattern: str = 'scenarios/runner.py') -> list[int]:
    """兜底找出"旧版本启动的 / 没写 pidfile 的"调度器（判活仍然只信内核）。"""
    try:
        out = subprocess.run(['pgrep', '-fl', pattern],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    pids: list[int] = []
    for line in (out or '').splitlines():
        head, _, cmd = line.strip().partition(' ')
        if not head.isdigit():
            continue
        pid = int(head)
        if pid == os.getpid() or '--test' in cmd or not _is_runner_cmdline(cmd):
            continue
        if pid_alive(pid):
            pids.append(pid)
    return pids


def find_running(path: Path, pattern: str = 'scenarios/runner.py') -> list[dict]:
    """当前还活着的调度器：[{'pid','via':'pidfile'|'pgrep','started'}]。"""
    found: list[dict] = []
    info = read_pidfile(path)
    if info and info['pid'] != os.getpid():
        found.append({'pid': info['pid'], 'via': 'pidfile', 'started': info.get('started')})
    for pid in pgrep_runners(pattern):
        if pid == os.getpid() or any(pid == f['pid'] for f in found):
            continue
        found.append({'pid': pid, 'via': 'pgrep', 'started': None})
    return found


def acquire(path: Path, argv: list[str] | None = None) -> dict | None:
    """单实例抢占：已有活着的调度器就返回它（调用方自己决定怎么报错/退出）。

    返回 None = 抢占成功（已写入自己的 pid）。**不删别人的活记录**——
    要顶掉旧实例请先正常停它（SIGINT），或者确认它已经死了。
    """
    other = find_running(path)
    if other:
        return other[0]
    try:                     # 陈旧文件（死进程留下的）直接覆盖
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass
    write_pidfile(path, argv)
    return None
