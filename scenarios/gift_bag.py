# 福袋场景：遍历好友列表，领取"系着绳结"的福袋。
#
# 机制（2026-09 实测，1080x2412）：
#  - 好友家宠物左侧地板固定位置可能出现福袋（约 x150-390, y1390-1670），
#    系着蓝绳结 = 可领取；敞口/摊平/无袋 = 已领取或没有——只点系绳的；
#  - 点击后弹"5个福袋共 N 金币，已领 M 个"公告弹窗，点小 × 关闭
#    （!! 全屏遮罩节点重心落在公告列表的好友行上，点它会误触跳转到别人家）；
#  - 非好友的福袋点击后弹"加好友"提示——只关闭跳过，绝不点加好友；
#  - 检测 = 袋口两个模板（系绳/敞口）在固定槽位多尺度模板匹配：
#    系绳分 >= TIED_MIN_SCORE 且高于敞口分 → 可领；敞口分高 → 已领跳过。
#    实测 20 家样本全部分类正确（可领 0.68~1.0 / 非可领的系绳分最高 0.53）。
import os
import re
import time
from datetime import datetime

import cv2

from src.progress import log
from src.ocr import ocr_texts
from src.scenario import CLICK_INTERVAL, DeviceScenario
from scenarios.friend_care import in_time_range, parse_time_range  # noqa: F401（供 runner 复用）
from scenarios.visit import VisitScenario

# 袋口检测搜索区（1080x2412 基准坐标，覆盖各房间袋子的上下两种摆位）
BAG_REGION = (100, 1330, 480, 1650)
# 非好友标志文字区：好友页=“点亮中1/3”，非好友=“加好友”（实测稳定互斥）
FRIEND_MARK_REGION = (700, 160, 980, 280)
# 单轮扫描最多检查的好友数（好友轮播内容会轮换、总量不定，防无限循环）
MAX_SWEEP = 45
# 系绳模板判定阈值：可领样本 0.68~1.0，非可领样本的系绳分最高 0.53
TIED_MIN_SCORE = 0.62
# 多尺度（袋子有生动画，渲染尺寸会小幅变化）
MATCH_SCALES = (0.8, 0.9, 1.0, 1.1, 1.2, 1.35, 1.5)
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')


class GiftBagScenario(VisitScenario):
    def __init__(self, dev=None):
        DeviceScenario.__init__(self, dev)  # 跳过 VisitScenario 的踩踩字段/日志
        gb = self.cfg.gift_bag
        self.last_sweep_at: datetime | None = None  # 上次扫描完成时间（调度间隔用）
        tpl_tied = cv2.imread(os.path.join(TEMPLATE_DIR, 'gift_bag_tied_neck.png'))
        tpl_open = cv2.imread(os.path.join(TEMPLATE_DIR, 'gift_bag_open_neck.png'))
        if tpl_tied is None or tpl_open is None:
            raise RuntimeError(f'福袋模板缺失（{TEMPLATE_DIR}），无法启用福袋功能')
        self._tpl_tied = tpl_tied
        self._tpl_open = tpl_open
        log(f'福袋: {"启用" if gb.enabled else "未启用"}，时间段 {gb.time_range}，'
            f'扫描间隔 {gb.interval_seconds} 秒')

    # ---- 检测 ----

    def _match(self, img, tpl) -> tuple[float, tuple[int, int], float]:
        """模板在袋口搜索区的多尺度最大相关：返回（分数, 全图坐标 top-left, 尺度）。"""
        x1, y1, x2, y2 = BAG_REGION
        h, w = img.shape[:2]
        rx1, ry1 = int(x1 * w / 1080), int(y1 * h / 2412)
        rx2, ry2 = int(x2 * w / 1080), int(y2 * h / 2412)
        region = img[ry1:ry2, rx1:rx2]
        best, best_loc, best_s = -1.0, (0, 0), 1.0
        for s in MATCH_SCALES:
            t = cv2.resize(tpl, None, fx=s, fy=s)
            if t.shape[0] >= region.shape[0] or t.shape[1] >= region.shape[1]:
                continue
            res = cv2.matchTemplate(region, t, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if mx > best:
                best = float(mx)
                best_loc = (rx1 + loc[0], ry1 + loc[1])  # 换算回全图坐标
                best_s = s
        return best, best_loc, best_s

    def is_friend_page(self, screen=None) -> bool:
        """当前好友页的主人是不是我们的好友：右上角有"加好友"= 非好友，袋子领不了。

        好友页该位置显示"点亮中1/3"（实测两者稳定互斥，位置同为约 (800,210)）。"""
        img = screen if screen is not None else self.screen()
        x1, y1, x2, y2 = FRIEND_MARK_REGION
        h, w = img.shape[:2]
        rx1, ry1 = int(x1 * w / 1080), int(y1 * h / 2412)
        rx2, ry2 = int(x2 * w / 1080), int(y2 * h / 2412)
        return not any('加好友' in t for t, *_ in ocr_texts(img[ry1:ry2, rx1:rx2]))

    def bag_state(self, screen=None) -> str:
        """'tied'=系绳可领 / 'open'=已领（敞口或摊平）/ 'none'=没有袋子。"""
        img = screen if screen is not None else self.screen()
        st, _, _ = self._match(img, self._tpl_tied)
        so, _, _ = self._match(img, self._tpl_open)
        if st >= TIED_MIN_SCORE and st > so:
            return 'tied'
        if so >= TIED_MIN_SCORE and so > st:
            return 'open'
        return 'none'

    def bag_click_point(self, screen=None) -> tuple[int, int] | None:
        """系绳可领时返回袋子的实际点击点（模板命中位中心）。

        袋子位置随房间布局上下左右浮动（实测最左 x150 摆位），不能点固定坐标。"""
        img = screen if screen is not None else self.screen()
        st, loc, s = self._match(img, self._tpl_tied)
        if st < TIED_MIN_SCORE:
            return None
        th, tw = self._tpl_tied.shape[:2]
        cx = loc[0] + int(tw * s / 2)
        cy = loc[1] + int(th * s / 2)
        return cx, cy

    # ---- 领取 ----

    def _dismiss_popup(self, xml: str) -> bool:
        """关闭福袋公告/提示弹窗：只点小号（<300px）的关闭类按钮。

        全屏遮罩节点的 bounds 是整屏、重心落在公告列表的好友行上——点它的
        中心会误触跳转到别人家（实测教训），必须用尺寸过滤掉。"""
        for m in re.finditer(
                r'<node[^>]*content-desc="(关闭|取消|知道了|确定)"[^>]*'
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
            x1, y1, x2, y2 = (int(m.group(2)), int(m.group(3)),
                              int(m.group(4)), int(m.group(5)))
            if x2 - x1 < 300 and y2 - y1 < 300:
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                log(f'福袋: 关闭弹窗「{m.group(1)}」({cx}, {cy})')
                self.click(cx, cy)
                time.sleep(CLICK_INTERVAL)
                return True
        return False

    def claim(self, point: tuple[int, int]) -> str:
        """点击袋子实际位置 -> 读反馈弹窗 -> 关闭。返回 'ok:文本'/'friend:文本'/'none'。"""
        w, h = self.dev.window_size()
        cx, cy = int(point[0] * w / 1080), int(point[1] * h / 2412)
        self.click(cx, cy)
        time.sleep(2.2)
        xml = self.dev.d.dump_hierarchy()
        note = ''
        friend_prompt = False
        for m in re.finditer(r'<node[^>]*>', xml):
            d = re.search(r'content-desc="([^"]*)"', m.group(0))
            if not d:
                continue
            desc = d.group(1)
            if '加好友' in desc:
                friend_prompt = True
                note = note or desc[:80]
            elif re.search(r'福袋', desc) and len(desc) < 120:
                note = desc
        dismissed = self._dismiss_popup(xml)
        if not dismissed and (friend_prompt or note):
            # 兜底：弹窗没有可点的关闭/取消按钮时按系统返回（弹窗一般会被关掉）
            log('福袋: 未找到关闭按钮，按返回兜底')
            self.go_back()
            time.sleep(CLICK_INTERVAL)
        if friend_prompt:
            return f'friend:{note}'
        if note:
            return f'ok:{note}'
        return 'none'

    # ---- 入口 ----

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """一轮 = 遍历好友列表：每停一家检测袋口，系绳的点开领取，结束后回主页面。"""
        log('福袋扫描开始')
        self.ensure_main_page()
        self._friends = []        # 累积好友名单（只增不减），见 visit.py
        self._friend_index = 0
        self.goto_first_friend()
        self._accumulate_friends()
        checked = claimed = 0
        while True:
            time.sleep(0.8)
            desc = (self._friends[self._friend_index]
                    if self._friend_index < len(self._friends) else '')
            screen = self.screen()
            checked += 1
            if not self.is_friend_page(screen):
                # 好友都排在轮播最前面：一旦遇到非好友（右上角"加好友"）就收尾——
                # 后面的全都不是好友、袋子也领不了（用户确认的机制，不再往后点）
                log(f'福袋: {desc} 是非好友，说明好友已轮完，结束本轮')
                break
            state = self.bag_state(screen)
            if state == 'tied':
                point = self.bag_click_point(screen)
                if point is None:
                    log(f'福袋: {desc} 系绳状态但定位不到点击点，跳过')
                else:
                    result = self.claim(point)
                    if result.startswith('ok:'):
                        claimed += 1
                        log(f'福袋: {desc} 领取成功（{result[3:]}）')
                    elif result.startswith('friend:'):
                        log(f'福袋: {desc} 点击弹"加好友"提示（{result[7:]}），跳过不领')
                    else:
                        log(f'福袋: {desc} 点击后无反馈（可能不可领），跳过')
            if checked >= MAX_SWEEP:
                log(f'福袋: 已达单轮检查上限 {MAX_SWEEP} 家，结束本轮')
                break
            if not self.next_friend():
                break
        log(f'福袋扫描完成: 检查 {checked} 家，领取 {claimed} 个')
        self.last_sweep_at = datetime.now()
        self.ensure_main_page()
        return True
