"""职业解锁哨兵：读职业树，监控隐藏职业线（武术家/梦境旅人/大明星）是否解锁。

导航链路（2026-09 真机实测 1080×2412，按屏幕宽高比例换算分辨率无关）：
    主页面 → 出门 → 职业小镇（工作页，顶部固定显示三维）→ 右上角树形图标 → 职业树页
职业树页：8 条线 × 5 级（见习→大师），一屏 4 列、横向可滑（慢滑一次 ≈ 移动 3 列）；
顶部同样显示三维读数（一次检查同时完成「属性同步 + 解锁判定」）。

判定：目标列出现在屏幕上时，列头文字（如「武术家」）横向 ±窗口 内、
头部向下固定区间里出现 2 个以上汉字的文本 → 该线已解锁；否则未解锁；
列没露过脸 → unknown。候选解锁会再滑回来复读一遍，两遍一致才算数（防 OCR 抖动）。
已知见习名（习武小童/浅梦行者/偶像练习生）出现在页面上 → 直接判该线解锁（增强信号）。

写盘（全部失败只记日志、不影响调度）：
- runs/career_plan.json  三维合并写回（仪表盘「职业」页数据保鲜，保留工分/毕业键）
- runs/career_tree_last.png  最近一次检查的树页截图
- runs/career_unlock.json  解锁事件（仪表盘横幅 + Hermes 定时通知脚本消费）
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

import cv2

from .config import PROJECT_ROOT
from .ocr import ocr_fullscreen
from .progress import log as _log

RUNS = PROJECT_ROOT / 'runs'
PLAN_FILE = RUNS / 'career_plan.json'
UNLOCK_FILE = RUNS / 'career_unlock.json'
LAST_TREE_SHOT = RUNS / 'career_tree_last.png'

TARGETS_DEFAULT = ('武术家', '梦境旅人', '大明星')
# 树页 8 列顺序（左→右）：状态一律以职业树实测为准——数字阈值只是粗略近似，
# 实测存在「数值达标但未解锁」（浅梦行者@智88）与「数值不达标却已解锁」（大厨）并存。
ALL_LINES = ('流浪散人', '画家', '侦探', '法师', '大厨', '武术家', '梦境旅人', '大明星')
# 树页左侧的层级标签（位置可能落进最左列判定窗口，需排除，防误判"已解锁"）
TIER_LABELS = ('见习', '初级', '中级', '高级', '大师')
# 三条隐藏线「见习」级的已知名字（调研口径；解锁时列格出现该名字）
FIRST_TIER_NAMES = {'武术家': '习武小童', '梦境旅人': '浅梦行者', '大明星': '偶像练习生'}
# 截图文件名用 ASCII slug（仪表盘 /files/ 只放行 ASCII 文件名）
SHOT_SLUG = {'武术家': 'wushujia', '梦境旅人': 'mengjinglvren', '大明星': 'damingxing'}

CJK_RE = re.compile(r'[\u4e00-\u9fff]')
ATTR_RE = re.compile(r'(力量|智力|魅力)\s*([0-9]{1,6})')


# ---------------------------------------------------------------- 纯函数（可离线回归）

def has_cjk(text: str, n: int = 2) -> bool:
    """文本是否含至少 n 个汉字（列格出现真名字的信号；「???」不会命中）。"""
    return len(CJK_RE.findall(text or '')) >= n


def read_attrs(items, ymax: int) -> dict:
    """从一屏 OCR 结果读三维读数（树页/小镇页顶部固定显示）。"""
    got = {}
    for t, x, y, s in items:
        if ymax and y > ymax:
            continue
        m = ATTR_RE.search(t)
        if m:
            got[m.group(1)] = int(m.group(2))
    return got


def _find_header(items, target: str):
    """找目标线的列头（如「武术家」），返回 (x, y) 或 None。"""
    for t, x, y, s in items:
        if target in t.replace(' ', ''):
            return (x, y)
    return None


def eval_tree_items(items, targets, win: int, h: int) -> dict:
    """从一屏 OCR 结果判定各目标线状态。

    返回 {target: {'state': 'unlocked'|'locked'|'unknown', 'name': str|None}}；
    列头没找到（该列不在这一屏）→ unknown。
    """
    states = {}
    y_lo_off, y_hi_off = int(0.05 * h), int(0.62 * h)
    for tgt in targets:
        hdr = _find_header(items, tgt)
        if hdr is None:
            states[tgt] = {'state': 'unknown', 'name': None}
            continue
        hx, hy = hdr
        found = None
        for t, x, y, s in items:
            if (abs(x - hx) <= win and (hy + y_lo_off) <= y <= (hy + y_hi_off)
                    and has_cjk(t) and t.replace(' ', '').strip() not in TIER_LABELS):
                found = t.strip()
                break
        states[tgt] = ({'state': 'unlocked', 'name': found} if found
                       else {'state': 'locked', 'name': None})
    return states


def merge_views(views, targets, win: int, h: int) -> dict:
    """合并多屏 OCR 结果：任一屏判定出 unlocked/locked 即采用（unlocked 优先），
    并叠加「已知见习名出现」增强信号。"""
    merged = {t: {'state': 'unknown', 'name': None} for t in targets}
    for items in views:
        st = eval_tree_items(items, targets, win, h)
        for tgt in targets:
            cur, new = merged[tgt], st[tgt]
            if new['state'] == 'unlocked':
                merged[tgt] = new
            elif new['state'] == 'locked' and cur['state'] == 'unknown':
                merged[tgt] = new
    # 增强信号：任一屏直接出现该线见习名 → 已解锁
    for items in views:
        for t, x, y, s in items:
            tt = t.replace(' ', '')
            for tgt, known in FIRST_TIER_NAMES.items():
                if tgt in targets and known in tt:
                    merged[tgt] = {'state': 'unlocked', 'name': known}
    return merged


# ---------------------------------------------------------------- 写盘

def save_attrs(attrs: dict) -> None:
    """三维合并写回 career_plan.json（保留工分/毕业等手工键）。"""
    try:
        data = json.loads(PLAN_FILE.read_text('utf-8'))
    except Exception:
        data = {}
    data.update({'力': attrs['力量'], '智': attrs['智力'], '魅': attrs['魅力']})
    tmp = PLAN_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), 'utf-8')
    tmp.replace(PLAN_FILE)


def load_unlock_data() -> dict:
    """读解锁事件文件（不存在/损坏回空档）。"""
    try:
        data = json.loads(UNLOCK_FILE.read_text('utf-8'))
        if isinstance(data, dict):
            data.setdefault('events', [])
            return data
    except Exception:
        pass
    return {'events': []}


def save_unlock_data(data: dict) -> None:
    try:
        UNLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = UNLOCK_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), 'utf-8')
        tmp.replace(UNLOCK_FILE)
    except OSError:
        pass


def touch_last_check() -> None:
    """记录最近一次检查时间（仪表盘展示用）。"""
    data = load_unlock_data()
    data['last_check'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_unlock_data(data)


# ---------------------------------------------------------------- 真机检查

def _save_img(img, path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return bool(cv2.imwrite(str(path), img))
    except Exception:
        return False


def check_career_tree(scen, targets=None, log=None) -> dict:
    """导航到职业树检查一遍（调用方保证设备空闲；finally 回主页面）。

    scen: 任一场景实例（借用其设备/导航，建议传调度器的 school 实例）
    返回: {ok, reason, attrs: {力量,智力,魅力}, states: {线: {...}},
           unlocks: {线: 名字}（两遍确认过的）, shot, w, h}
    """
    log = log or _log
    event_targets = list(targets or TARGETS_DEFAULT)  # 报事件/通知的线（默认隐藏三线）
    watch = list(ALL_LINES)  # 实际判定与写回的线：全 8 线（仪表盘板子用）
    result = {'ok': False, 'reason': '', 'attrs': {}, 'states': {},
              'unlocks': {}, 'shot': None}
    started = time.time()
    try:
        scen.ensure_main_page()
        img = scen.screen()
        if hasattr(img, 'shape'):
            h, w = int(img.shape[0]), int(img.shape[1])
        else:  # PIL 兜底
            w, h = img.size
        win = max(60, int(0.088 * w))

        # 出门 → 职业小镇（工作页）
        hit = scen.see('leave_home') or (int(0.498 * w), int(0.862 * h))
        scen.click(hit[0], hit[1])
        time.sleep(1.6)
        town = scen.see('town')
        if not town:
            time.sleep(1.0)
            town = scen.see('town')
        town = town or (int(0.787 * w), int(0.513 * h))
        scen.click(town[0], town[1])
        time.sleep(1.8)

        # 右上角树形图标 → 职业树页（180°第一候选点实测命中；偏一点再试一次）
        items = []
        opened = False
        for cx, cy in ((int(0.787 * w), int(0.081 * h)), (int(0.787 * w), int(0.058 * h))):
            scen.click(cx, cy)
            time.sleep(2.0)
            items = ocr_fullscreen(scen.screen())
            if any('职业树' in t for t, *_ in items):
                opened = True
                break
        if not opened:
            result['reason'] = '未进入职业树页（树形图标点击未生效）'
            return result

        # 顶部三维（数值异步加载，重试）
        attrs = {}
        for _ in range(6):
            attrs = read_attrs(items, ymax=int(0.29 * h))
            if len(attrs) == 3:
                break
            time.sleep(1.0)
            items = ocr_fullscreen(scen.screen())

        # 横滑拍两屏（一屏 4 列）：初始左屏（流浪散人~法师）+ 右滑后的右屏（大厨~大明星）
        y = int(0.62 * h)
        view_imgs, views = [], []
        views.append(items)  # 初始左屏（进树页时已 OCR）
        for _ in range(2):
            scen.dev.d.swipe(int(0.83 * w), y, int(0.24 * w), y, 0.5)
            time.sleep(1.0)
            vimg = scen.screen()
            view_imgs.append(vimg)
            views.append(ocr_fullscreen(vimg))

        states = merge_views(views, watch, win, h)

        # 复读确认（防 OCR 抖动）：滑回左端再滑过来，两遍一致才算数
        if any(v['state'] == 'unlocked' for v in states.values()):
            for _ in range(2):
                scen.dev.d.swipe(int(0.24 * w), y, int(0.83 * w), y, 0.5)
                time.sleep(0.8)
            views2 = [ocr_fullscreen(scen.screen())]  # 左屏复读
            for _ in range(2):
                scen.dev.d.swipe(int(0.83 * w), y, int(0.24 * w), y, 0.5)
                time.sleep(1.0)
                views2.append(ocr_fullscreen(scen.screen()))
            states2 = merge_views(views2, watch, win, h)
            for tgt in watch:
                if states[tgt]['state'] == 'unlocked' and states2[tgt]['state'] != 'unlocked':
                    states[tgt] = {'state': 'unknown', 'name': None}  # 两遍不一致，下轮复查
            log('职业哨兵: 复读确认完成: '
                + '、'.join(f"{t}={'已解锁' if states[t]['state'] == 'unlocked' else states[t]['state']}"
                           for t in watch))

        # 8 线状态写回（树实测为准；解锁永久——不因单次读不到而回退）
        try:
            udata = load_unlock_data()
            lines_db = udata.get('lines') or {}
            now_s = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for tgt in watch:
                st = states.get(tgt) or {}
                if st.get('state') not in ('unlocked', 'locked'):
                    continue
                old = lines_db.get(tgt) or {}
                unlocked = bool(old.get('unlocked')) or st['state'] == 'unlocked'
                name = st.get('name') or (old.get('name') if old.get('unlocked') else None)
                lines_db[tgt] = {'unlocked': unlocked, 'name': name, 'at': now_s}
            udata['lines'] = lines_db
            save_unlock_data(udata)
        except Exception as e:
            log(f'职业哨兵: 8线状态写回失败: {e}')

        # 保存最近树页截图
        if view_imgs and _save_img(view_imgs[-1], LAST_TREE_SHOT):
            result['shot'] = LAST_TREE_SHOT.name

        # 汇总结论 + 解锁截图存档（事件只报隐藏三线；普通线状态在仪表盘 8 线格子里看）
        unlocks = {}
        for tgt in event_targets:
            if tgt in states and states[tgt]['state'] == 'unlocked':
                name = states[tgt]['name'] or FIRST_TIER_NAMES.get(tgt)
                unlocks[tgt] = name
                slug = SHOT_SLUG.get(tgt, 'unlock')
                shot_name = f"career_unlock_{slug}_{datetime.now():%m%d_%H%M}.png"
                if view_imgs and _save_img(view_imgs[-1], RUNS / shot_name):
                    unlocks.setdefault('_shots', {})
                    unlocks['_shots'][tgt] = shot_name
        result['attrs'] = attrs
        result['states'] = {t: {'state': states[t]['state'], 'name': states[t]['name']}
                            for t in watch}
        result['unlocks'] = {k: v for k, v in unlocks.items() if k != '_shots'}
        if '_shots' in unlocks:
            result['unlock_shots'] = unlocks['_shots']
        result['ok'] = True
        result['reason'] = 'ok'
        if len(attrs) == 3:
            try:
                save_attrs(attrs)
            except Exception as e:
                log(f'职业哨兵: 三维写回 career_plan.json 失败: {e}')
        log(f"职业哨兵: 检查完成（{time.time() - started:.0f}s，力{attrs.get('力量','?')}"
            f"/智{attrs.get('智力','?')}/魅{attrs.get('魅力','?')}）")
        return result
    except Exception as e:
        result['reason'] = f'{type(e).__name__}: {e}'
        return result
    finally:
        try:
            scen.ensure_main_page()
        except Exception as e:
            log(f'职业哨兵: 回主页面失败: {e}')
