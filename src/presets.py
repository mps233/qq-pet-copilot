# -*- coding: utf-8 -*-
"""一键配置预设（config.yaml 就地修改，保留注释）。

当前预设：
- alt / 小号工具人：给「小号」跑托管时用——只保留供养大号相关的任务
  （护理自己 / 到好友家护理大号 / 定向送 PK / 福袋 / 踩踩 / 轻量打工），
  关掉学习、冒险、雇佣好友（不在 tasks.order 里 = 不调度）；
  被雇佣处理设为「让利雇主（不召回）」——大号雇走小号宠物打工时，小号不做任何
  召回：宠物留在大号那儿继续打工（不掐断打工时间、不浪费），收益按规则归雇主方
  （大号）；小号其它任务撞上被雇佣会自动延后重试。
  参数 main_name = 大号的主人昵称或宠物名（好友护理 / PK 目标都填它）。
"""
from __future__ import annotations

import src.settings as S

ALT_TASK_ORDER = 'care>friend_care>pk>gift_bag>visit>work'
EMPLOYED_ACTION_YIELD = '让利雇主（不召回）'


def alt_preset(main_name: str) -> dict:
    """小号工具人模式要写入的配置键值（dotted key → value）。"""
    name = (main_name or '').strip()
    return {
        # 任务集：保留供养相关，学习/冒险/雇佣好友不进 order = 不调度
        'tasks.order': ALT_TASK_ORDER,
        'tasks.school.enabled': False,
        'adventure.times_per_day': 0,
        'hire_friend.enabled': False,
        # 到大号家护理（访客付费，花小号的金币）
        'friend_care.enabled': True,
        'friend_care.friend_name': name,
        'friend_care.method': 'ocr检测',
        'friend_care.time_range': '08:00-23:59',
        'friend_care.interval_seconds': 600,
        # 被雇佣托管：大号雇走小号宠物打工时不做召回——宠物留那边继续打工，
        # 收益归雇主方（大号）；检查间隔只做监控日志（30 分钟一次足够）
        'employed.enabled': True,
        'employed.action': EMPLOYED_ACTION_YIELD,
        'employed.time_range': '00:01-23:59',
        'employed.interval_seconds': 1800,
        # 定向送 PK：只打大号（大号赢 +金币）
        'pk.only_names': name,
        'pk.skip_names': '',
        'pk.helper_names': '',
        'pk.max_level': 0,
        'pk.times_per_day': 15,
        'pk.start_time': '00:01',
        # 踩踩（顺带和大号刷火花）
        'visit.times_per_day': 10,
        'visit.start_time': '00:01',
        # 福袋照扫：小号自己的进账，用来支付给大号护理的饼干/香皂开销
        'gift_bag.enabled': True,
        'gift_bag.time_range': '08:00-23:59',
        'gift_bag.interval_seconds': 1800,
        # 轻量打工：维持自己买饼干/香皂的钱（10 分钟一轮，尽量常回家当“可被雇佣”状态）
        'work.duration': '10分钟',
    }


def apply_alt_preset(main_name: str, dry_run: bool = False) -> dict:
    """应用小号工具人预设。dry_run=True 只预览不写盘。

    返回 {'applied': {键: 生效值}, 'rejected': [非法项], 'dry_run': bool}。
    """
    data = S.load_raw()
    applied: dict = {}
    rejected: list = []
    for key, value in alt_preset(main_name).items():
        ok, fixed = S.validate_field(key, value)
        if not ok:
            rejected.append(f'{key}: 非法值 {value!r}')
            continue
        S.set_value(data, key, fixed)
        applied[key] = fixed
    if not dry_run and applied:
        S.save_raw(data)
    return {'applied': applied, 'rejected': rejected, 'dry_run': dry_run}
