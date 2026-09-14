"""游戏疲劳提示状态：按天标记（runs/fatigue_state.json）。

游戏在当日"学习+打工"合计进入疲劳档后，会在学园课程面板 / 打工面板底部显示
"提示：我今天学习/打工太久，要学不进去啦（本次…）"（打工面板同句、结尾
"要干不动啦"）。这是**含玩家手动游玩时间**的权威疲劳信号——工具自身的时长
账本只知道它自己执行的部分（实测踩坑：用户手动打了 2 小时，工具没算到，
没按策略转冒险，被用户抓到）。

检测到即记录当天日期；调度器据此当天不再安排学习/打工（转冒险），隔天自动失效。
手动重置：删除 runs/fatigue_state.json 即可让调度器恢复学习/打工。
"""
from __future__ import annotations

from datetime import date, datetime

from . import progress_store
from .config import PROJECT_ROOT

FATIGUE_FILE = PROJECT_ROOT / 'runs' / 'fatigue_state.json'


def mark_fatigue(source: str) -> None:
    """记录今日已疲劳（source: 检测入口 school / work / hire_friend）。"""
    try:
        progress_store.write_raw(FATIGUE_FILE, {
            'date': date.today().isoformat(),
            'source': source,
            'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
    except OSError:
        pass  # 记不下来不影响主流程（与进度文件同类失败策略）


def fatigue_today() -> bool:
    """今日是否已检测到游戏疲劳提示（学习/打工太久）。"""
    data = progress_store.read_raw(FATIGUE_FILE)
    return data.get('date') == date.today().isoformat()
