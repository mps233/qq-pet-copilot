"""职业进度自动识别：出门 → 职业小镇，OCR 读取 力量/智力/魅力 → 写入 runs/career_plan.json。

用法: .venv/bin/python tools/career_sync.py
最后一行输出 JSON（供 dashboard「自动识别」按钮解析）：
  {"ok": true, "力": 1, "智": 3, "魅": 3}  或  {"ok": false, "reason": "..."}

机制：职业小镇面板顶部固定显示三项属性（实测 OCR 输出 '力量1' '智力3' '魅力3'）。
轻量导航不调用 goto_town（工作期间会阻塞等结束），用出门→小镇入口的直接点击。
"""
import json
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import cv2  # noqa: E402

from src.ocr import ocr_fullscreen  # noqa: E402
from scenarios.work import WorkScenario  # noqa: E402

PLAN_FILE = BASE / 'runs' / 'career_plan.json'
ATTR_RE = re.compile(r'(力量|智力|魅力)\s*([0-9]{1,6})')
TOWN_FALLBACK = (850, 1238)   # 出门页小镇入口参考坐标（1080 宽）
LEAVE_FALLBACK = (538, 2078)  # 主页面出门按钮参考坐标


def read_attrs(ws) -> dict | None:
    """读职业小镇顶部 力量/智力/魅力（数值异步加载，重试若干次）。"""
    got = {}
    for attempt in range(1, 7):
        res = ocr_fullscreen(ws.screen())
        got = {}
        for t, used_x, y, s in res:
            if y > 600:
                continue
            m = ATTR_RE.search(t)
            if m:
                got[m.group(1)] = int(m.group(2))
        if len(got) == 3:
            return got
        time.sleep(1.0)
    return got if len(got) == 3 else None


def main() -> None:
    ws = None
    try:
        ws = WorkScenario()
        ws.ensure_main_page()
        hit = ws.see('leave_home') or LEAVE_FALLBACK
        ws.click(hit[0], hit[1])
        time.sleep(1.6)
        town = ws.see('town')
        if not town:
            time.sleep(1.0)
            town = ws.see('town')
        town = town or TOWN_FALLBACK
        ws.click(town[0], town[1])
        time.sleep(1.8)
        attrs = read_attrs(ws)
        if not attrs:
            raise RuntimeError('未识别到三项属性（面板未加载或不在小镇页）')
        try:
            data = json.loads(PLAN_FILE.read_text('utf-8'))
        except Exception:
            data = {}
        data.update({'力': attrs['力量'], '智': attrs['智力'], '魅': attrs['魅力']})
        PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PLAN_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), 'utf-8')
        tmp.replace(PLAN_FILE)
        print(json.dumps({'ok': True, '力': attrs['力量'], '智': attrs['智力'],
                          '魅': attrs['魅力']}, ensure_ascii=False), flush=True)
    except Exception as e:
        print(json.dumps({'ok': False, 'reason': str(e)}, ensure_ascii=False), flush=True)
    finally:
        if ws is not None:
            try:
                ws.ensure_main_page()
            except Exception:
                pass


if __name__ == '__main__':
    main()
