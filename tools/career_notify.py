#!/usr/bin/env python3
"""QQ宠物·职业解锁哨兵 → 通知投递脚本（供 Hermes cron no_agent 任务使用）。

读 ~/Tools/qq-pet-copilot-mac/runs/career_unlock.json：
- 有未通知事件（notified_at 为空）→ 格式化成消息打印（cron 原样投递到 Telegram），
  并标记 notified_at；事件带的树页截图以 MEDIA: 行附带（投递端不支持则当普通文本）；
- 没有任何新事件 → 不输出（cron 静默跳过）。

仅用标准库；写入用 tmp+replace 原子替换。
"""
import json
from datetime import datetime
from pathlib import Path

BASE = Path.home() / 'Tools' / 'qq-pet-copilot-mac'
UNLOCK_FILE = BASE / 'runs' / 'career_unlock.json'


def main() -> None:
    try:
        data = json.loads(UNLOCK_FILE.read_text('utf-8'))
    except Exception:
        return
    events = data.get('events') or []
    fresh = [e for e in events if not e.get('notified_at')]
    if not fresh:
        return
    lines = ['🎉 QQ宠物·隐藏职业解锁！']
    medias = []
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for e in fresh:
        lines.append('')
        lines.append(f"「{e.get('career') or '?'}」已解锁（见习：{e.get('name') or '?'}）")
        a = e.get('attrs') or {}
        if a:
            lines.append('当前三维：力 {} / 智 {} / 魅 {}'.format(
                a.get('力量', a.get('力', '?')), a.get('智力', a.get('智', '?')),
                a.get('魅力', a.get('魅', '?'))))
        if e.get('stopped'):
            lines.append('已自动停止学习（职业哨兵触发）——要接着学，在仪表盘设置里改回')
        if e.get('shot'):
            medias.append(str(BASE / 'runs' / e['shot']))
        e['notified_at'] = now
    txt = '\n'.join(lines)
    if medias:
        txt += '\n' + '\n'.join(f'MEDIA:{m}' for m in medias)
    print(txt)
    try:
        tmp = UNLOCK_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), 'utf-8')
        tmp.replace(UNLOCK_FILE)
    except OSError:
        pass


if __name__ == '__main__':
    main()
