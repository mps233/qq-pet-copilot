"""任务队列状态缓存：调度器（task_queue 引擎）每轮写一次，GUI 日志页状态条显示用。

runs/queue_status.json:
{"current": "当前正在执行的任务名", "pending": "主任务组进行中活动（延时收尾）",
 "pending_key": pending 对应的**任务键**（school/work/hire_friend/adventure）——
   延时收尾期间 current 是空的，仪表盘靠它高亮"宠物其实正在做哪一行"，
 "next": "下一任务名", "next_at": "HH:MM:SS", "next_ts": 下一任务时间戳（算剩余秒数用）,
 "ready": 待执行数量（在等退避/每日窗口/pending 收尾时间）,
 "waiting": 等待中数量（现在就可执行、等调度器轮到）, "updated": "HH:MM:SS",
 "pid": 写这份状态的调度器 PID（判"是不是本次运行写的"）,
 "tasks": {"<任务键>": {"state": "disabled/dead/ready/waiting",
           "next": "YYYY-MM-DD HH:MM:SS 下次执行时间（可能为空）"}}}

**状态新鲜度**（用户实报："手动把宠物召回了，停掉调度器再启动，显示还是在上课"）：
旧的 queue_status.json 会一直躺在盘上，重启后的新进程在跑完第一个任务之前不写新的，
GUI/仪表盘就读到上一轮的 `pending: 上课` —— 于是重启后依旧显示"上课（进行中）"，
而宠物其实早被召回、什么都没在做。所以：
  * 调度器进程一起来就先 `mark_starting()`（`starting: true` + 自己的 pid），
    消费者看到"这份状态不属于当前进程"就显示"启动检查中"，不再展示上一轮的活动；
  * 停止时 `mark_stopped()`，别的入口（GUI/仪表盘）读完知道这是"已停止"而不是"在进行"。

只是展示用途，读写失败都不影响调度/界面。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

from .config import PROJECT_ROOT

QUEUE_STATUS_FILE = PROJECT_ROOT / 'runs' / 'queue_status.json'


def save_queue_status(state: dict) -> None:
    """调度器每轮调度后写一次队列状态（写失败只影响展示，不抛异常）。"""
    _write(state)


def mark_starting(pid: int | None = None) -> None:
    """调度器刚起来：清掉上一轮的活动/队列信息，标记"启动检查中"。

    由 `run_scheduler()` 在最前面调用（设备连接、启动检查都还没做），
    这样重启后的那一两秒里 GUI/仪表盘也不会继续显示上一轮的"上课（进行中）"。
    """
    _write({'current': '', 'pending': '', 'next': '', 'next_at': '', 'next_ts': 0,
            'ready': 0, 'waiting': 0, 'tasks': {},
            'starting': True, 'pid': os.getpid() if pid is None else int(pid),
            'started': time.time(),
            'updated': datetime.now().strftime('%H:%M:%S')})


def mark_stopped() -> None:
    """调度器已停止（GUI/仪表盘停进程后、调度器自己优雅退出时调用）。

    只记"已停止"，不删文件：消费端（GUI/仪表盘）据此显示"已停止"，
    而不是把上一轮遗留的"进行中"当成还在跑。
    """
    _write({'current': '', 'pending': '', 'next': '', 'next_at': '', 'next_ts': 0,
            'ready': 0, 'waiting': 0, 'tasks': {},
            'stopped': True, 'pid': None,
            'updated': datetime.now().strftime('%H:%M:%S')})


def _write(state: dict) -> None:
    try:
        QUEUE_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUEUE_STATUS_FILE.write_text(
            json.dumps(state, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


def load_queue_status() -> dict | None:
    """GUI 读队列状态（文件不存在/损坏返回 None）。"""
    try:
        data = json.loads(QUEUE_STATUS_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def status_is_current(state: dict | None, pid: int | None) -> bool:
    """这份队列状态是否属于"当前正在跑的这个调度器"（消费端判新鲜度用）。

    False = 是上一轮进程留下的（或刚启动/已停止的占位状态），调用方应显示
    "启动检查中/按配置推算"而不是把里面的 current/pending 当现状（否则重启后
    会继续显示上一轮的"上课（进行中）"）。pid=None（不知道是谁在跑）时只排除
    starting/stopped 这两种显式状态。
    """
    if not isinstance(state, dict) or state.get('starting') or state.get('stopped'):
        return False
    if pid is None:
        return True
    return state.get('pid') == pid


#: "启动检查中"占位的宽限期（秒）：task_queue 引擎起来后 1~2 分钟内就会写出
#: 自己的状态（启动实测一次出门）。超过这个时间还读不到当前进程的状态，说明
#: 这个引擎根本不写队列状态（`runner.engine: legacy`）或调度器卡住了，消费端
#: 应退回"按配置推算/等待中"，而不是永远显示"启动中"。
STARTUP_PLACEHOLDER_GRACE_SECONDS = 600


def startup_placeholder_active(state: dict | None, pid: int | None,
                               age_seconds: float | None) -> bool:
    """是否该显示"启动检查中"占位（而不是上一轮的活动/任务）。

    = 状态不属于当前调度器进程 **且** 这个调度器进程还很年轻（age_seconds 为
    None 时按"年轻"处理——GUI 自己拉起的子进程总是知道自己启动了多久）。
    """
    if status_is_current(state, pid):
        return False
    if age_seconds is None:
        return True
    return age_seconds < STARTUP_PLACEHOLDER_GRACE_SECONDS
