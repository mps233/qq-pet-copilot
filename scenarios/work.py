"""打工场景。

流程（u2 控件/OCR 文字定位，分辨率无关）：
1. 主页面（main_sign）-> 点击 leave_home 出门
2. 出门后若正在上课/工作/冒险/被雇佣中（school_in / work_in / adventure_in / employed_in）
   -> 等待结束并退出，等完的课程/工作计入对应场景的当天次数，
      回主页面结束本轮，由执行器重新判断限制条件后再决定下一步
3. 点击 town 进入小镇
4. 点 back 重置默认地点，OCR 整屏文字找到配置的打工地点并点击进入；
   没进面板就回主页面重新进小镇再试
5. 把第一框拖到第三框归位（两次），按配置 work.duration 点击对应工作选择框
   （10分钟/45分钟/2小时 -> select_box_1/2/3）
6. 点击 work_outworker 进入雇佣好友界面（OCR 标题确认弹出）：
   - 默认自动选收益最高：读每行"金币+N%"挑加成最大的可雇行（列表不按加成排序，
     必须逐行读）；配置了 work.hire_name（宠物名/主人名，部分匹配）时优先雇该名字，
     不可雇时同样回落"收益最高"，加成都没读出才回落最上面一个（排除"被雇佣中"状态标签），
     点击日志附该行文字（谁被雇了一目了然）
   - 配了 work.hire_name 且开启 work.hire_wait（"等好友空闲再雇佣"）时：目标好友
     状态是"出门中/被雇佣中"（正忙着打工/学习）就先不换人——点他的头像进主页读
     剩余时间（hire 区域内倒计时，如 04:37），抛 TaskDeferred 延后到活动结束再回来雇
     （期间调度器去跑冒险/护理等其他任务，不原地干等）；状态"对方今天很累了"
     是当天不可雇，等待无意义，仍换收益最高的人；面板里找不到目标也回落收益最高
   - 没有可点按钮（当前页好友不可雇佣时不渲染按钮）-> 点工作面板顶部"智力"坐标
     关闭雇佣面板并确认弹层已关，回到打工面板由下一步点 work_start 直接开工（不雇佣）
7. 点击 work_start 开始工作，直到出现 work_in
8. 工作中按配置的检查间隔（schedule.check_interval）检查，直到出现 work_end，点击 quit 退出
9. 当天次数 +1 并持久化到 runs/work_progress.json（含 history 历史记录），
   回主页面重新开始

运行：python scenarios/work.py            （Ctrl+C 停止）
      python scenarios/work.py --times 5  （覆盖配置的每天打工次数，0 为不限）
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fatigue import mark_fatigue
from src.locators import LOCATORS, OCR_MIN_SCORE, locate_cached, see_bounds
from src.ocr import (find_text, ocr_fullscreen, ocr_texts,
                     parse_countdown_seconds, parse_panel_location)
from src.progress import (
    WORK_PROGRESS_FILE,
    count_cross,
    load_progress,
    log,
    log_history,
    record_work_finish,
    save_progress,
    set_current_work_duration,
)
from src.scenario import CLICK_INTERVAL, DeviceScenario, NAV_TIMEOUT, TaskDeferred

# 以下坐标均为 720x1280 参考坐标，运行时按当前分辨率自动换算
PROGRESS_FILE = WORK_PROGRESS_FILE

# 进入打工地点面板的最多重试次数（点空/页面加载慢时快速重试，不再长时间确认）
WORK_PLACE_ATTEMPTS = 3

# 打工时长选择 -> 工作选择框（打工与雇佣好友共用，配置 work.duration）
DURATION_BOXES = {'10分钟': 'select_box_1', '45分钟': 'select_box_2', '2小时': 'select_box_3'}

# 雇佣面板"雇佣"按钮定位（按钮在每行右侧 x >= 屏宽一半）
EMPLOY_ROW_TOL = 90  # 同行文字（名字/主人）与"雇佣"按钮的最大 y 偏差，像素（实测行距约 195）

# ---- 等好友空闲再雇佣（work.hire_wait）----
# 面板行状态文字（canvas 自绘，只能 OCR；实测 1080×2412 状态在 x≈939）：
# "雇佣"=可雇 / "对方今天很累了"=今天不可雇（换人）/ "出门中"、"被雇佣中"=忙碌（可等）
HIRE_STATUS_TIRED = ('很累', '累了')
HIRE_STATUS_BUSY = ('出门', '被雇')
HIRE_FIND_SCROLLS = 6          # 面板里找目标好友时列表下拉次数上限（懒加载，目标常在下方）
HIRE_HOME_WAIT = 3.0           # 点头像进好友主页后的加载等待（秒）
HIRE_TIME_RETRIES = 3          # 好友主页倒计时读取重试次数
HIRE_WAIT_FALLBACK_SECONDS = 60  # 主页读不到剩余时间时的复测间隔（秒）
HIRE_WAIT_MARGIN_SECONDS = 5   # 剩余时间换算空闲时间点的余量（秒）
# 好友主页里倒计时是真实控件（content-desc 恰为 "04:37"），作为 OCR 失败的兜底
COUNTDOWN_DESC_RE = re.compile(r'\d{1,3}:\d{2}(?::\d{2})?')


def _is_employ_noise(text: str) -> bool:
    """雇佣行里的噪声文字（不算名字/主人）：名次数字、"上次雇佣"分组标题、
    "宠友列表"面板标题、"金币+N%"徽章、排行榜标题。

    注意：职业标签（如"入梦旅人"/"魔法学徒"）不在这里过滤——它常与宠物名连成
    一个 OCR 块（"张二狗入梦旅人"），过滤会把宠物名一起丢掉；名字匹配用子串，
    带上职业标签不影响命中。
    """
    t = re.sub(r'\s+', '', text or '')
    if not t or t.isdigit():
        return True
    return bool(_employ_row_bonus(t) is not None
                or '上次雇佣' in t or '宠友列表' in t
                or '金币' in t or '排行' in t)


def parse_employ_rows(items, avatars, buttons, width):
    """把雇佣面板的整屏 OCR 与控件树坐标组装成行（纯函数，便于离线回归）。

    items: 整屏 OCR [(text, x, y, score)]；avatars: 头像中心点（content-desc
    "访问宠物主页"，每行都有）；buttons: 雇佣按钮中心点（content-desc "雇佣"，
    不可雇的行没有该节点）；width: 屏宽。

    返回 [{'x','y','name','owner','text','status','status_xy','button'}]，按 y 排序。
    行的文字分布（实测 1080×2412，行距约 195）：左侧 x<0.6w = 名次数字/宠物名/
    职业标签/主人行；中部 0.55~0.7w = "金币+N%"徽章（不算状态）；右侧 x>0.75w =
    状态文字。"对方今天很累了"常被 OCR 拆成"对方今天很"+"累了"两块，按 y 拼接还原；
    宠物名与职业标签也可能被 OCR 并成一块（如"张二狗入梦旅人"），此时按整块返回。
    """
    left_max = int(width * 0.6)
    right_min = int(width * 0.75)
    rows = []
    for ax, ay in avatars:
        near = [(text.strip(), x, y, score) for text, x, y, score in items
                if abs(y - ay) <= EMPLOY_ROW_TOL and score >= OCR_MIN_SCORE and text.strip()]
        near.sort(key=lambda m: (m[2], m[1]))
        left = [(t, x) for t, x, _y, _s in near
                if x < left_max and not _is_employ_noise(t)]
        left.sort(key=lambda m: m[1])  # 按 x 从左到右：宠物名在最左，职业标签在其右
        right = [(t, x, y) for t, x, y, _s in near if x > right_min]
        btn = None
        if buttons:
            cand = min(buttons, key=lambda b: abs(b[1] - ay))
            btn = cand if abs(cand[1] - ay) <= EMPLOY_ROW_TOL else None
        if not left and not right and btn is None:
            continue  # 屏幕边缘被裁掉一半的空行（无文字/无按钮），跳过
        status_xy = (right[0][1], right[0][2]) if right else None
        names = [t for t, _x in left if not t.startswith('主人')]
        rows.append({
            'x': ax, 'y': ay,
            'name': names[0] if names else '',
            'owner': next((t for t, _x in left if t.startswith('主人')), ''),
            'text': ''.join(t for t, _x in left),
            'status': ''.join(t for t, _x, _y in right),
            'status_xy': status_xy,
            'button': btn,
        })
    rows.sort(key=lambda r: r['y'])
    return rows


def classify_hire_status(row) -> str:
    """行状态：'free' 可雇 / 'tired' 对方今天很累了 / 'busy' 出门中或被雇佣中 / 'unknown'。

    "被雇佣中"含"雇佣"二字，必须先判 busy 再判 free，否则会被当成可雇按钮。
    """
    text = re.sub(r'\s+', '', (row.get('status') or '') + (row.get('text') or ''))
    if any(k in text for k in HIRE_STATUS_TIRED):
        return 'tired'
    if any(k in text for k in HIRE_STATUS_BUSY):
        return 'busy'
    if row.get('button') is not None or '雇佣' in text:
        return 'free'
    return 'unknown'


def pick_employ_button(items, width, prefer_name: str = ''):
    """从雇佣面板的整屏 OCR 结果里挑要点的"雇佣"按钮。

    items: [(text, x, y, score)] 整屏 OCR；width: 屏宽。
    prefer_name: 优先雇佣的名字（宠物名或主人名，去空格部分匹配）；空 = 自动选收益最高。

    返回 (button_x, button_y, row_text, mode, bonus) 或 None：
    - mode='name' ：配了名字且该行可雇（bonus 为 None）；
    - mode='bonus'：自动挑金币加成最高的可雇行（bonus = N）；并列取最上面一行；
    - mode='top'  ：加成都没读出来，回落最上面一个（bonus 为 None）；
    - 没有任何可点按钮 → None（调用方关闭面板直接开工）。
    """
    prefer = (prefer_name or '').replace(' ', '').lower()
    exact, fuzzy, name_hits = [], [], []
    for text, x, y, score in items:
        t = text.replace(' ', '')
        if score < OCR_MIN_SCORE:
            continue
        if prefer and x < width / 2 and prefer in t.lower():
            name_hits.append((x, y))
        if x < width / 2 or '雇佣' not in t:
            continue
        if '被雇佣' in t or '雇佣中' in t:
            continue  # "被雇佣中"状态标签，不是雇佣按钮
        (exact if t == '雇佣' else fuzzy).append((x, y, score))
    buttons = exact or fuzzy
    if not buttons:
        return None
    buttons.sort(key=lambda m: (m[1], m[0]))
    if name_hits:
        for _nx, ny in sorted(name_hits, key=lambda m: m[1]):
            near = [b for b in buttons if abs(b[1] - ny) <= EMPLOY_ROW_TOL]
            if near:
                b = min(near, key=lambda m: abs(m[1] - ny))
                return b[0], b[1], _employ_row_text(items, b[1], width), 'name', None
    # 自动选收益最高：逐按钮读同行"金币+N%"，挑最大；加成都读不出才回落最上面
    scored = []
    for b in buttons:
        bonus = _employ_row_bonus(_employ_row_text(items, b[1], width))
        if bonus is not None:
            scored.append((bonus, b))
    if scored:
        bonus, b = max(scored, key=lambda m: (m[0], -m[1][1]))
        return b[0], b[1], _employ_row_text(items, b[1], width), 'bonus', bonus
    b = buttons[0]
    return b[0], b[1], _employ_row_text(items, b[1], width), 'top', None


EMPLOY_BONUS_RE = re.compile(r'金币\s*[+＋]?\s*(\d{1,3})\s*%')
EMPLOY_ANY_BONUS_RE = re.compile(r'[+＋]\s*(\d{1,3})\s*%')


def _employ_row_bonus(row_text: str) -> int | None:
    """从雇佣行文字里解析金币加成 N（如"金币+22%"）；读不到返回 None。"""
    m = EMPLOY_BONUS_RE.search(row_text or '')
    if not m:
        m = EMPLOY_ANY_BONUS_RE.search(row_text or '')
    return int(m.group(1)) if m else None


def _employ_row_text(items, btn_y: int, width: int) -> str:
    """"雇佣"按钮同排、按钮左侧的文字拼接（名字/职业/金币+N%/主人），日志里看雇的是谁。

    "金币+N%"徽章在屏幕中右侧（x > width/2），所以采集窗口放宽到按钮左边
    （按钮和"出门中/很累了"等状态文字在更右侧，不会被带上）。
    """
    parts = sorted(((y, x, text.strip()) for text, x, y, score in items
                    if x < int(width * 0.85) and abs(y - btn_y) <= EMPLOY_ROW_TOL
                    and score >= OCR_MIN_SCORE and text.strip()),
                   key=lambda m: (m[0], m[1]))
    out, seen = [], set()
    for _y, _x, t in parts:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return ' '.join(out)


class WorkScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.location = self.cfg.work.location
        if not self.location:
            raise ValueError('config.yaml 中 work.location 未配置打工地点')
        self.duration = self.cfg.work.duration
        if self.duration not in DURATION_BOXES:
            raise ValueError(
                f'config.yaml 中 work.duration 配置无效: {self.duration!r}，'
                f'可选: {"/".join(DURATION_BOXES)}')
        self.times_per_day = self.cfg.work.times_per_day
        # employ_scroll_limit 保留配置兼容（旧流程下滑找雇佣按钮已移除，
        # 当前页没有雇佣按钮时直接关闭面板开工），runner 仍会赋值
        self.employ_scroll_limit = self.cfg.work.employ_scroll_limit
        # 优先雇佣的名字（宠物名/主人名，部分匹配）；空 = 自动选金币加成最高的可雇行
        self.hire_name = str(getattr(self.cfg.work, 'hire_name', '') or '').strip()
        # 等好友空闲再雇佣：目标好友"出门中/被雇佣中"时不换人，读剩余时间延后到结束
        self.hire_wait = bool(getattr(self.cfg.work, 'hire_wait', False))
        log(f'打工地点: {self.location}，打工时长: {self.duration}，每天打工次数: '
            f'{self.times_per_day if self.times_per_day else "不限"}'
            + (f'，优先雇佣: {self.hire_name}' if self.hire_name else '，雇佣策略: 自动选收益最高')
            + ('，忙就等好友空闲' if self.hire_wait and self.hire_name else ''))

    # ---- 各阶段 ----

    def goto_town(self) -> str | None:
        """主页面 -> 出门 -> 进小镇。

        出门后若正在上课/工作中（上次中途停止），等待结束并退出、回主页面，
        返回等完的是哪种（'school' / 'work' / 'adventure' / 'employed'）——此时不再继续进小镇，
        由调用方/执行器重新判断限制条件后再决定下一步；正常情况返回 None。
        """
        self.leave_home()
        finished = self.wait_busy_end()
        if finished:
            self.ensure_main_page()
            return finished
        try:
            for attempt in range(1, NAV_TIMEOUT + 1):
                town = self.see('town')
                if town:
                    self.click(town[0], town[1])
                    time.sleep(CLICK_INTERVAL)
                    return None
                log(f'未找到 town 按钮，等待重试 ({attempt}/{NAV_TIMEOUT})')
                time.sleep(CLICK_INTERVAL)
            raise RuntimeError('出门后未找到 town 按钮')
        except RuntimeError:
            # 正在上课/冒险等时点小镇入口可能无响应，导航超时；
            # 屏幕早已稳定，重新检测一次进行中状态再下结论
            finished = self._recheck_busy_after_nav('前往小镇')
            if finished:
                return finished
            raise

    def select_place(self) -> None:
        """确认当前面板是不是配置的打工地点：是就直接用，不是才 back 重置再选。

        进小镇后游戏默认停在上次工作的打工面板，但面板有 1~2 秒加载过渡
        （刚进时力量/智力/魅力还没渲染出来，OCR 判不出地点名）：先等面板
        稳定再 OCR 确认，是配置的地点就直接返回，省掉 back 重置 -> OCR 找地点
        -> 点击的流程；不是才走 back 重置重选（重试时回主页面重新进小镇）。
        点击后要等打工面板（选择框）出现才算进入。
        """
        for _wait in range(1, 4):
            time.sleep(CLICK_INTERVAL)
            if self.is_correct_place_panel():
                log(f'当前面板已是打工地点 {self.location}，直接使用')
                return
        for attempt in range(1, WORK_PLACE_ATTEMPTS + 1):
            if attempt > 1:
                # 回主页面重新进小镇（干净状态），再走 back 重置
                self.ensure_main_page()
                finished = self.goto_town()
                if finished:
                    raise RuntimeError(f'重新进入小镇时检测到进行中状态: {finished}')
            if not self.go_back(source=self.dev.hierarchy()):
                raise RuntimeError('未找到 back 按钮，无法重置打工地点')
            time.sleep(CLICK_INTERVAL)
            results = ocr_fullscreen(self.screen())
            hit = find_text(results, self.location)
            if not hit:
                log(f'未识别到打工地点 {self.location}，回主页面重试 ({attempt}/{WORK_PLACE_ATTEMPTS})')
                continue
            log(f'OCR 找到打工地点 {self.location} '
                f'({hit[0]}, {hit[1]}, score={hit[2]:.2f})')
            self.click(hit[0], hit[1])
            # 等打工面板出现：选择框在 + OCR 确认地点名是配置的打工地点（确认 3 次即可）
            entered = False
            for _wait in range(1, 4):
                time.sleep(CLICK_INTERVAL)
                # select_box_1 由容器推导：容器 cache 后秒回，不重复 dump
                if self.see('select_box_1') and self.is_correct_place_panel():
                    entered = True
                    break
            if entered:
                return
            log(f'点击 {self.location} 后未出现打工面板，回主页面重试 ({attempt}/{WORK_PLACE_ATTEMPTS})')
        raise RuntimeError(f'多次未能进入打工地点面板: {self.location}')

    def panel_location(self, screen=None) -> str | None:
        """当前面板的地点名：力量/智力/魅力属性面板正下方第一串字符（整屏 OCR）。"""
        if screen is None:
            screen = self.screen()
        return parse_panel_location(ocr_fullscreen(screen))

    def is_correct_place_panel(self, screen=None) -> bool:
        """判断当前工作面板是不是配置的打工地点（OCR 地点名 == self.location）。"""
        loc = self.panel_location(screen)
        if loc is None:
            return False
        return loc == self.location.replace(' ', '')

    def select_job(self) -> None:
        """先把第一框拖到第三框归位（两次），按配置 work.duration 点对应工作选择框
        （10分钟/45分钟/2小时 -> select_box_1/2/3），再点 work_outworker 进雇佣界面。

        轮播有 4 个（打工）/7 个（学园）选择框、xpath 只认可见 3 个，
        必须归位到第一页再选，否则可能选到绝对第几个不是第 2 个；
        拖动已提速（duration 0.3，约 2.3s/次），选择框槽位固定（cache 命中秒回）。
        """
        # select_box_N 由容器推导（容器 cache 后秒回）；work_outworker 首次未缓存时
        # 抓一次控件树快照，与容器推导共用，总共只 dump 一次
        source = None if locate_cached('work_outworker') else self.dev.hierarchy()
        self.reset_select_boxes(source=source)
        # 打工开始时把本次打工时长（work.duration）持久化到 work_progress.json，
        # 结算时按它累计打工时长（10分钟/45分钟/2小时）
        set_current_work_duration(self.duration)
        box = DURATION_BOXES[self.duration]
        hit = self.see(box, source=source)
        if not hit:
            raise RuntimeError(f'未定位到工作选择框: {box}')
        time.sleep(CLICK_INTERVAL)
        self.click(hit[0], hit[1])
        # work_outworker 是独立按钮，不依赖选框结果，点完直接进雇佣面板
        self._open_employ_panel(source)

    def _open_employ_panel(self, source=None) -> None:
        """点 work_outworker 进雇佣面板；点击后用 OCR 标题确认面板弹出。

        work_outworker 位置固定且有 cache：首次命中后直接复用缓存点；
        source 传 select_job 共享的控件树快照时，首次也只用这一次 dump。
        """
        target = self.see('work_outworker', source=source)
        if not target:
            raise RuntimeError('未找到雇佣好友按钮')
        self.click(target[0], target[1])
        for _wait in range(1, 4):
            time.sleep(CLICK_INTERVAL)
            if self._employ_panel_open():
                log('选择雇佣好友: 雇佣面板已弹出')
                return
        raise RuntimeError('点击雇佣好友后雇佣面板未弹出')

    def hire_friend(self) -> None:
        """雇佣好友：自动选金币加成最高的可雇行（配了 work.hire_name 则优先该名字，
        不可雇时回落收益最高），加成都读不出才回落最上面一个；都没有则关闭雇佣页面直接开工。

        配了 work.hire_name 且开启 work.hire_wait 时（"等好友空闲再雇佣"）：
        目标好友在面板里的状态是"出门中/被雇佣中"（正忙着打工/学习）就先不换人——
        点他的头像进主页读剩余时间，抛 TaskDeferred 延后到活动结束再回来雇佣
        （期间调度器去跑冒险/护理等其他任务，不原地干等）；状态"对方今天很累了"
        是当天不可雇，等待无意义，按老逻辑换收益最高的人。

        当前页好友不可雇佣时列表不渲染雇佣按钮（纯图片/无按钮）：OCR 检测一次
        没有可点按钮就直接关闭雇佣面板，由 run() 点 work_start 直接开工。
        点击时日志附该行文字（谁被雇了一目了然）。
        """
        if self.hire_wait and self.hire_name:
            self._hire_target_or_wait()
            return
        screen = self.screen()
        picked = pick_employ_button(ocr_fullscreen(screen), screen.shape[1], self.hire_name)
        if not picked:
            log('未找到雇佣按钮，直接关闭雇佣页面')
            self._close_employ_panel()
            return
        bx, by, row_text, mode, bonus = picked
        if mode == 'name':
            prefix = f'优先雇佣「{self.hire_name}」'
        elif mode == 'bonus':
            prefix = f'自动选收益最高（金币+{bonus}%）'
        elif self.hire_name:
            prefix = f'优先雇佣「{self.hire_name}」不可雇，回落最上面'
        else:
            prefix = '加成未读出，回落最上面'
        desc = f' ({bx}, {by})' + (f' [{row_text}]' if row_text else '')
        log(f'{prefix}，点击{desc}')
        self.click(bx, by)
        time.sleep(CLICK_INTERVAL)

    # ---- 等好友空闲再雇佣（work.hire_wait）----

    def employ_rows(self):
        """读当前雇佣面板的所有行（整屏 OCR + 控件树头像/按钮坐标）。

        返回 (rows, items)；rows 见 parse_employ_rows。控件树 dump 较慢（1~4s），
        只在需要识别状态时调用（hire_wait 开启且配了 hire_name 才走这条路）；
        头像与按钮共用同一次 dump。
        """
        screen = self.screen()
        items = ocr_fullscreen(screen)
        source = self.dev.hierarchy()
        avatars = self.dev.find_xpath_all(LOCATORS['employ_avatar']['xpath'][0], source)
        buttons = self.dev.find_xpath_all(LOCATORS['employ']['xpath'][0], source)
        return parse_employ_rows(items, avatars, buttons, screen.shape[1]), items

    def _find_employ_row(self, name: str):
        """在雇佣面板里找目标好友所在行（列表懒加载，必要时向下滚动）。

        匹配宠物名或主人名（去空格后部分匹配，同 work.hire_name 语义）。
        返回 (row, kind)：kind='free'/'tired'/'busy'/'unknown'；找不到返回 (None, None)。
        """
        target_name = re.sub(r'\s+', '', name or '')
        for attempt in range(1, HIRE_FIND_SCROLLS + 1):
            rows, _items = self.employ_rows()
            for row in rows:
                hay = re.sub(r'\s+', '', f"{row['name']}{row['owner']}{row['text']}")
                if target_name and target_name in hay:
                    return row, classify_hire_status(row)
            if attempt < HIRE_FIND_SCROLLS:
                log(f'雇佣面板本屏没有「{name}」，向下滚动查找 ({attempt}/{HIRE_FIND_SCROLLS})')
                w, h = self.dev.window_size()
                self.dev.swipe(w // 2, int(h * 0.82), w // 2, int(h * 0.42), duration=0.4)
                time.sleep(CLICK_INTERVAL)
        return None, None

    def read_friend_busy_seconds(self) -> int | None:
        """在好友主页读忙碌剩余时间（当前页必须是好友宠物页）。

        好友正忙着打工/学习时，主页 hire 按钮位置显示倒计时（实测形如 04:37，
        逐秒递减，归零后变成"雇佣"）。先按 hire 控件 range 裁图 OCR（快），
        OCR 读不到时最后再用控件树兜底（dump 一次 1~4s，只做一次）。
        识别不到返回 None（调用方按兜底间隔复测）。
        """
        for attempt in range(1, HIRE_TIME_RETRIES + 1):
            bounds = see_bounds(self.dev, 'hire')
            if bounds:
                x1, y1, x2, y2 = bounds
                crop = self.screen()[y1:y2, x1:x2]
                for text, *_ in ocr_texts(crop):
                    secs = parse_countdown_seconds(text)
                    if secs is not None:
                        return secs
            if attempt < HIRE_TIME_RETRIES:
                time.sleep(1.0)
        # OCR 三次都读不到：控件树兜底（倒计时是 hire 内的真实控件）
        for desc in self._countdown_descs():
            secs = parse_countdown_seconds(desc)
            if secs is not None:
                return secs
        return None

    def _countdown_descs(self) -> list[str]:
        """控件树里 hire 按钮内 content-desc 恰为倒计时（如 04:32）的节点文字。

        好友主页的忙碌倒计时是真实控件（hire 节点内的 TextView，content-desc 就是
        倒计时文本，实测 bounds 在 hire 范围内）——OCR 读不到时用它兜底；
        限定 hire 子树，避免误取页面上别处的倒计时。
        """
        source = self.dev.hierarchy()
        if source is None:
            return []
        out = []
        for e in source.find_elements('//*[@content-desc="hire"]//*[@content-desc]'):
            desc = str((e.attrib or {}).get('content-desc') or '').strip()
            if COUNTDOWN_DESC_RE.fullmatch(desc):
                out.append(desc)
        return out

    def _friend_busy_until(self, row) -> datetime:
        """点头像进好友主页读忙碌剩余时间，返回空闲时间点（成功后退回雇佣面板）。

        读不到剩余时间按 HIRE_WAIT_FALLBACK_SECONDS 兜底复测（不猜时间、不干等）。
        """
        log(f'「{self.hire_name}」{row["status"] or "忙碌中"}，点头像进主页读剩余时间')
        self.click(row['x'], row['y'])
        time.sleep(HIRE_HOME_WAIT)
        secs = self.read_friend_busy_seconds()
        # 退回雇佣面板（实测 back 一次即回面板，面板仍在原滚动位置附近）
        self.go_back()
        time.sleep(CLICK_INTERVAL)
        if secs is None:
            log(f'未读到忙碌剩余时间，{HIRE_WAIT_FALLBACK_SECONDS} 秒后复测')
            return datetime.now() + timedelta(seconds=HIRE_WAIT_FALLBACK_SECONDS)
        until = datetime.now() + timedelta(seconds=secs + HIRE_WAIT_MARGIN_SECONDS)
        mins, s = divmod(secs, 60)
        log(f'好友忙碌剩余 {mins} 分 {s} 秒，延后到 {until:%H:%M:%S} 再雇佣')
        return until

    def _hire_target_or_wait(self) -> None:
        """等好友空闲再雇佣：目标忙（出门中/被雇佣中）就延后到活动结束。

        - 目标是"雇佣"（可雇）：直接点掉；
        - 目标"对方今天很累了"：当天不可雇，换收益最高的人（等也没用）；
        - 目标"出门中/被雇佣中"：读剩余时间抛 TaskDeferred（调度层到点再调度）；
        - 目标状态没读出来且没有雇佣按钮：按忙碌处理（宁可等，不擅自换人）——
          可雇的行必定有按钮（classify_hire_status 认按钮判 free），
          没按钮又读不出状态说明多半在忙（面板加载中/OCR 抖动）；
        - 面板里没有目标：回落老逻辑（收益最高），日志说明。
        """
        row, kind = self._find_employ_row(self.hire_name)
        if row is not None and kind == 'free':
            btn = row['button'] or (row['x'], row['y'])
            log(f'优先雇佣「{self.hire_name}」可雇 [{row["text"]}]，点击 {btn}')
            self.click(btn[0], btn[1])
            time.sleep(CLICK_INTERVAL)
            return
        if row is not None and kind in ('busy', 'unknown'):
            if kind == 'unknown':
                log(f'「{self.hire_name}」状态未识别到且没有雇佣按钮，按忙碌处理（不换人）')
            until = self._friend_busy_until(row)
            # 收掉雇佣面板回到打工面板（离开页面状态干净，调度层回主页面更稳）；
            # 关闭失败只记日志，不影响延后本身
            try:
                self._close_employ_panel()
            except Exception as e:
                log(f'延后前关闭雇佣面板失败（忽略）: {e}')
            raise TaskDeferred(
                until, f'想雇的「{self.hire_name}」正{row["status"] or "忙碌"}'
                       f'（不换人，等到空闲再雇）')
        if row is not None:
            log(f'优先雇佣「{self.hire_name}」{row["status"]}（今天不可雇），等也没用，换收益最高的人')
        else:
            log(f'雇佣面板里没找到「{self.hire_name}」，回落收益最高的人')
        # 回落选人只要整屏 OCR（不必再 dump 控件树）
        items = ocr_fullscreen(self.screen())
        picked = pick_employ_button(items, self.screen().shape[1], '')
        if not picked:
            log('未找到雇佣按钮，直接关闭雇佣页面')
            self._close_employ_panel()
            return
        bx, by, row_text, _mode, bonus = picked
        log(f'改雇收益最高（金币+{bonus}%） ({bx}, {by})' + (f' [{row_text}]' if row_text else ''))
        self.click(bx, by)
        time.sleep(CLICK_INTERVAL)

    def _employ_panel_open(self) -> bool:
        """雇佣排行榜面板是否还开着：整屏 OCR 检测标题"宠友雇佣加成排行榜"。

        OCR 约 0.5s，比 dump 控件树（4s+）快得多；关闭后标题消失即判已关。
        """
        try:
            results = ocr_fullscreen(self.screen())
            return find_text(results, '宠友雇佣加成排行榜') is not None
        except Exception as e:
            log(f'雇佣面板标题 OCR 失败，按未关闭处理: {e}')
            return True

    def _close_employ_panel(self, max_tries: int = 3) -> None:
        """点工作面板顶部"智力"坐标关闭雇佣面板（面板是弹层，点面板外即关闭）。

        雇佣面板打开时工作面板顶部的"力量/智力/魅力"仍在屏幕上方（面板外），
        整屏 OCR 出"智力"坐标点击即关闭弹层（实测有效，比 × 按钮更稳）；
        关闭后用 OCR 标题确认已关，没关就重试。
        """
        for attempt in range(1, max_tries + 1):
            results = ocr_fullscreen(self.screen())
            hit = find_text(results, '智力')
            if not hit:
                log(f'未找到"智力"坐标，尝试 {attempt}/{max_tries}')
                time.sleep(CLICK_INTERVAL)
                continue
            log(f'点击"智力"关闭雇佣页面 ({hit[0]}, {hit[1]})，尝试 {attempt}/{max_tries}')
            self.click(hit[0], hit[1])
            time.sleep(CLICK_INTERVAL)
            if not self._employ_panel_open():
                log('雇佣页面已关闭')
                return
        raise RuntimeError(f'点击 {max_tries} 次仍未关闭雇佣页面')

    def _recover_work_start(self) -> None:
        """work_start 未出现（被"去照顾一下"弹窗挡住）时的恢复流程。

        点"去照顾一下" -> 一键护理（含"支付并护理"确认）-> back 回工作面板，
        之后 work_start 应重新出现。
        """
        care = self.see('go_care')
        if care:
            log(f'检测到"去照顾一下"，点击 ({care[0]}, {care[1]})')
            self.click(care[0], care[1])
            time.sleep(CLICK_INTERVAL)
        else:
            log('未找到"去照顾一下"按钮，跳过护理恢复')
            return
        hit = self.see('one_click_care')
        if hit:
            self.click(hit[0], hit[1])
            time.sleep(CLICK_INTERVAL)
            pay = self.see('one_click_pay')
            if pay:
                log('检测到"支付并护理"，点击确认')
                self.click(pay[0], pay[1])
                time.sleep(CLICK_INTERVAL)
        else:
            log('未找到一键护理按钮')
        if self.go_back():
            log('已返回工作面板')
            time.sleep(CLICK_INTERVAL)
        else:
            log('未找到 back 按钮')

    def _has_work_start(self) -> bool:
        """工作面板是否有"去打工"按钮：整屏 OCR（约 0.5s），比 dump 控件树快。"""
        return find_text(ocr_fullscreen(self.screen()), '去打工') is not None

    def _start_work(self) -> None:
        """点"去打工"开始工作，OCR 确认已开始（正在打工）或按钮消失。

        OCR 约 0.5s/次，替代原 click_until_gone_or_see 每轮 3 次控件树 dump（十几秒）。
        """
        clicked = False
        for attempt in range(1, 4):
            # 点"去打工"可能触发"体力/清洁不足"弹窗（u2 层，OCR 看不到）：
            # 每轮同时检测，命中回主页面护理一次并抛 StatBlocked（调度器重试当前任务）
            self.handle_low_stat_dialog()
            results = ocr_fullscreen(self.screen())
            if find_text(results, '正在打工'):
                log('开始工作: 已出现 work_in')
                return
            target = find_text(results, '去打工')
            if target:
                log(f'开始工作: 点击"去打工" ({target[0]}, {target[1]})')
                self.click(target[0], target[1])
                clicked = True
                time.sleep(CLICK_INTERVAL)
                continue
            if clicked:
                log('开始工作: work_start 已消失，进入下一阶段')
                return
            if attempt == 1 or attempt == 3:
                log(f'开始工作: 未找到 work_start，等待重试 ({attempt}/3)')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError('开始工作: 重试 3 次仍未出现 work_in')

    def wait_work_end(self) -> None:
        """等待 work_end 出现并点击 quit 退出；等待期间点"鼓励宠物"。"""
        self.wait_end('work_in', 'work_end', encourage=True)

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """max_times: 当天打工次数上限，0 表示不限；None 表示用配置值。
        max_rounds: 最多跑多少轮后返回，0 为不限（供执行器逐轮调度）。
        返回本次调用是否完成了至少一次打工。
        """
        if max_times is None:
            max_times = self.times_per_day
        today, done, history = load_progress(PROGRESS_FILE)
        log_history(history, today)
        start_done = done
        if max_times and done >= max_times:
            log(f'今天已打工满 {max_times} 次，无需再打工')
            return False
        round_no = 0
        while True:
            round_no += 1
            log(f'===== 第 {round_no} 轮 =====')
            self.ensure_main_page()
            finished = self.goto_town()
            if finished:
                if self.pending is not None:
                    # 延时收尾模式：出门检测到的进行中活动已登记 pending，计数由
                    # finish_pending 收尾时统一进行（on_finish），本地不再计数，
                    # 本轮直接结束
                    return True
                if finished == 'work':
                    # 出门时等完了一次上次未结束的工作，计入当天次数 + 打工时长
                    done += 1
                    save_progress(PROGRESS_FILE, today, done, history)
                    record_work_finish()
                    log(f'已完成第 {done} 次打工' + (f' / 目标 {max_times} 次' if max_times else ''))
                    if max_times and done >= max_times:
                        log('达到当天打工次数，结束')
                        return True
                elif finished != 'employed':
                    # 出门时等完的是别的活动（上课/冒险），计入对应次数
                    # （被雇佣在召回点 quit 时已计数）
                    count_cross(finished)
                # 等完了一次活动，计数已变化，本轮结束，
                # 回主页面交由执行器重新判断限制条件
                if max_rounds and round_no >= max_rounds:
                    return True
                log('本轮结束，回主页面重新开始')
                continue
            self.select_place()
            if self._screen_has_fatigue():
                # 游戏疲劳提示：只记录，不据此处拦停——是否停止打工由调度层按
                # 合计时长分层判定（第一层 8~12h 仍可继续，第二层 >=12h 才禁止）
                mark_fatigue('work')
                log('检测到游戏疲劳提示"学习/打工太久"（已记录，'
                    '是否停止由调度层按合计时长分层判定）')
            self.select_job()
            self.hire_friend()
            # work_start 没出现时，先处理"去照顾一下"弹窗（护理 + back 回工作面板）
            if not self._has_work_start():
                self._recover_work_start()
            # work_start 仍未出现（面板没加载出来/被关闭等）：重进打工地点再选一次，
            # 避免白等 3 次后直接重启设备；仍失败才交给上层重启恢复
            if not self._has_work_start():
                log('未回到工作面板，重新进入打工地点再选一次')
                self.select_place()
                self.select_job()
                self.hire_friend()
            self._start_work()
            log('已开始工作，等待结束...')
            if self.defer_wait:
                # 延时收尾：登记 pending（到点由调度器 finish_pending 收尾计数，
                # 本地 done 不再自增）后回主页面，本轮直接结束
                self.defer_busy_end('work_in', 'work_end',
                                    lambda: count_cross('work'), '打工', encourage=True)
                self.ensure_main_page()
                return True
            self.wait_work_end()
            done += 1
            save_progress(PROGRESS_FILE, today, done, history)
            record_work_finish()
            log(f'已完成第 {done} 次打工' + (f' / 目标 {max_times} 次' if max_times else ''))
            if max_times and done >= max_times:
                log('达到当天打工次数，结束')
                return done > start_done
            if max_rounds and round_no >= max_rounds:
                log(f'已跑完 {max_rounds} 轮，返回')
                return done > start_done
            log('本轮结束，回主页面重新开始')


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='打工场景')
    ap.add_argument('--times', type=int, default=None,
                    help='当天打工次数上限，0 为不限；不指定则读 config.yaml 的 work.times_per_day')
    args = ap.parse_args()

    try:
        WorkScenario().run(max_times=args.times)
    except KeyboardInterrupt:
        log('手动停止')
