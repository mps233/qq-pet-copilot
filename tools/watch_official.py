#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方文案巡检：解析规则依赖的锚点还在不在 + 收益解析健康度。

## 为什么需要它

本项目的解析大量依赖官方文案锚点，而这些锚点分两类：

- **客户端硬编码**（`金币` `工分` `学分` `看视频` `成绩` `姓名`…）：写在 Kuikly 页面包里，
  抓一次包就能验证"官方这版还认不认这些词"。
- **服务端下发**（`教师评语` `工资明细` `打工总结` `本金` `限时补贴` `基础工资`…）：
  客户端代码里**根本没有**（官方直接拿 `IncomeExpenseItem.title` 当字典键），
  只能在运行时观察 —— 所以靠**结算记录的字段命中率**间接监控。

实测教训（2026-09-28）：打工结算页的工资构成标题由「本金22，雇佣加成6」变成「基础工资36」，
`pay_detail` 命中率从 23/23 掉到 15/23，直到手工核对才发现。这个脚本就是让这类变化**主动暴露**。

## 用法

    python3 tools/watch_official.py             # 锚点体检（对照最近一次抓取的官方包）
    python3 tools/watch_official.py --health    # 解析健康度（读 runs/session_rewards.jsonl）

锚点体检需要先有官方包：`qqpet_assets/tools/pull_kuikly.py` 抓全量包（需 root 设备），
`qqpet_assets/tools/analyze_kuikly_dex.py` 提取字符串表。
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
STR_DIR = BASE / 'qqpet_assets' / 'kuikly_analysis' / 'strings'
REWARD_FILE = BASE / 'runs' / 'session_rewards.jsonl'

# (锚点, 归属, 用在哪儿) —— 归属决定怎么监控：client 能在官方包里验证，server 只能看数据
ANCHORS = [
    # —— 学习结算页 ——
    ('课程', 'client', 'parse_session_reward：课程名'),
    ('成绩', 'client', 'parse_session_reward：成绩等级'),
    ('学分', 'client', 'parse_session_reward：学分'),
    ('姓名', 'client', 'parse_session_reward：宠物名'),
    ('主人', 'client', 'parse_session_reward：主人名'),
    ('力量', 'client', 'parse_session_reward：属性加成'),
    ('智力', 'client', 'parse_session_reward：属性加成'),
    ('魅力', 'client', 'parse_session_reward：属性加成'),
    # —— 打工结算页 ——
    ('工分', 'client', 'parse_session_reward：工分'),
    ('金币', 'client', 'parse_session_reward：金币'),
    ('看视频', 'client', 'parse_session_reward：广告加成金币'),
    ('继续打工', 'client', '结算页按钮（_detect_settlement 兜底判定用）'),
    ('继续学习', 'client', '结算页按钮'),
    # —— 服务端下发的（客户端无硬编码，只能靠数据监控）——
    ('教师评语', 'server', '学习结算页标题（_detect_settlement 判定）'),
    ('工资明细', 'server', '打工结算页标题'),
    ('打工总结', 'server', '打工结算页标题（雇佣好友计数靠它）'),
    ('本金', 'server', '工资构成行（旧版）'),
    ('雇佣加成', 'server', '工资构成行（旧版）'),
    ('基础工资', 'server', '工资构成行（2026-09-28 起实测出现）'),
    ('限时补贴', 'server', '工资构成行'),
    ('小提示', 'server', 'note 段锚点'),
]

HEALTH_FIELDS = [
    ('credits', '学分'), ('attrs', '属性'), ('course', '课程'), ('grade', '成绩'),
    ('coins', '金币'), ('workpoints', '工分'), ('ad_coins', '看视频加成'),
    ('job', '岗位'), ('pay_detail', '工资构成'), ('pet', '宠物名'),
    ('owner', '主人名'), ('note', '小提示'),
]


def anchor_check() -> int:
    """对照最近抓取的官方包字符串表，看客户端硬编码锚点还在不在。"""
    files = sorted(STR_DIR.glob('*.txt'))
    if not files:
        print(f'没有官方包字符串表（{STR_DIR}）——先跑 qqpet_assets/tools/pull_kuikly.py + '
              f'analyze_kuikly_dex.py --strings-only', file=sys.stderr)
        return 1
    print(f'锚点体检：{len(files)} 个官方包字符串表（{STR_DIR.parent.name}/strings/）\n')
    print(f"{'锚点':<10}{'归属':<8}{'命中包数':<10}用途")
    print('-' * 78)
    miss_client = []
    for word, owner, use in ANCHORS:
        if owner == 'server':
            print(f'{word:<10}{"服务端":<8}{"—":<10}{use}（客户端无硬编码，看 --health）')
            continue
        n = sum(1 for f in files if word in f.read_text('utf-8', errors='replace'))
        flag = '✓' if n else '✗ 官方包里已消失'
        print(f'{word:<10}{"客户端":<8}{n:<10}{use}  {flag}')
        if not n:
            miss_client.append(word)
    print()
    if miss_client:
        print(f'⚠ 有 {len(miss_client)} 个客户端锚点在最新官方包里查不到：{", ".join(miss_client)}')
        print('  → 官方可能换了文案，解析规则要跟着改（否则这些字段会静默解析失败）')
        return 1
    print('✓ 客户端硬编码锚点全部仍在官方包里')
    print('· 服务端下发的锚点无法静态验证，用 --health 看解析命中率')
    return 0


def health_check() -> int:
    """统计收益记录的字段命中率——某个字段突然掉率 = 官方文案变了。"""
    if not REWARD_FILE.exists():
        print(f'没有收益记录（{REWARD_FILE}）', file=sys.stderr)
        return 1
    rows = []
    for ln in REWARD_FILE.read_text('utf-8').splitlines():
        try:
            rows.append(json.loads(ln))
        except Exception:  # noqa: BLE001
            pass
    if not rows:
        print('收益记录为空', file=sys.stderr)
        return 1
    # 用**当前解析器**重放 tokens，而不是读 jsonl 里存的字段 —— 存量是旧版解析器写的，
    # 直接读会把"解析器后来改好了"误判成"官方换文案了"（本脚本第一版就踩了这个坑）。
    import sys as _sys
    _sys.path.insert(0, str(BASE))
    parse_session_reward = None
    try:
        from src.scenario import parse_session_reward  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f'（import 解析器失败，退回读存量字段：{exc}）')
    cache: dict[int, dict] = {}

    def parsed(idx: int, r: dict) -> dict:
        if parse_session_reward is None:
            return r
        if idx not in cache:
            try:
                cache[idx] = parse_session_reward(r.get('kind', 'school'), r.get('tokens') or [])
            except Exception:  # noqa: BLE001
                cache[idx] = {}
        return cache[idx]

    print(f'解析健康度：{len(rows)} 条记录（{REWARD_FILE.name}）· 当前解析器重放 tokens\n')
    for kind, label in (('school', '学习'), ('work', '打工')):
        sub = [(i, r) for i, r in enumerate(rows) if r.get('kind') == kind]
        if not sub:
            continue
        print(f'—— {label}（{len(sub)} 条）——')
        for key, name in HEALTH_FIELDS:
            if kind == 'school' and key in ('coins', 'workpoints', 'ad_coins', 'job', 'pay_detail'):
                continue          # 打工专有字段，学习页本来就没有
            hit = sum(1 for i, r in sub if parsed(i, r).get(key) not in (None, '', {}))
            stored = sum(1 for _i, r in sub if r.get(key) not in (None, '', {}))
            bar = '●' * round(hit / len(sub) * 10)
            note = ''
            # 打工页这几个字段本该条条命中，掉率就是官方换词的信号
            if kind == 'work' and key in ('coins', 'workpoints', 'pay_detail') and hit < len(sub):
                note = f'  ← 掉 {len(sub) - hit} 条，检查官方文案是否改了'
            elif stored < hit:
                note = f'  （存量只 {stored}/{len(sub)}：历史记录是旧解析器写的）'
            print(f'  {name:<8}{hit}/{len(sub)} {bar}{note}')
        print()
    print('提示：`pay_detail` / `note` 依赖服务端下发的标题词，掉率上升通常意味着官方换了文案；')
    print('      新词直接落进记录的 `tokens` 里，照着补进 src/scenario.py 的锚点即可。')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='官方文案巡检（锚点体检 / 解析健康度）')
    ap.add_argument('--health', action='store_true', help='只跑解析健康度（读 runs/session_rewards.jsonl）')
    args = ap.parse_args()
    if args.health:
        return health_check()
    rc = anchor_check()
    print()
    return health_check() or rc


if __name__ == '__main__':
    sys.exit(main())
