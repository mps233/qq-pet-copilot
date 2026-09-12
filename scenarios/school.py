"""学校上课场景。

流程（u2 控件/OCR 文字定位，分辨率无关）：
1. 主页面（main_sign="出门"）-> 点击 leave_home 出门
2. 出门后若正在上课/工作/冒险/被雇佣中（school_in / work_in / adventure_in / employed_in）
   -> 等待结束并退出，等完的课程/工作计入对应场景的当天次数，
      回主页面结束本轮，由执行器重新判断限制条件后再决定下一步
3. 每 1 秒点击一次 school，直到出现 school_start 按钮；
   若出现毕业标志（"去找同学玩"——毕业时学校面板没有"去上课"），
   点"关闭"再点两次 back 回主页面，重新进学校选择下一阶段课程
4. 选课：先 OCR 上半屏识别学园阶段（初级/中级学园课程顺序固定为
   力量/智力/魅力；高级学园/进修学院固定为 魅力/力量/智力，每次上课前重新判断），
   再把轮播归位到第一页，按 school.duration 选课：10分钟课直接点对应框；
   30分钟课小步扫描卡名（COURSE30_NAMES）点击，点后按详情面板"奖励<属性>+N"
   核对（10分+2 / 30分+5），不通过归位重试一次
5. 点击 school_start，直到页面出现 school_in 标志（进入上课）
6. 上课中：按配置的检查间隔（schedule.check_interval）检查，直到出现 school_end 标志
7. 点击 quit 结束，当天已学次数 +1 并持久化到 runs/school_progress.json
   （含 history 字段按日期保存每天的学习次数，跨天自动归档）
8. 一轮只上一节课就返回（供执行器逐节判断金币）；没有更多课程时结束

运行：python scenarios/school.py            （Ctrl+C 停止）
      python scenarios/school.py --times 5  （覆盖配置的每天学习次数，0 为不限）
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ocr import ocr_texts
from src.progress import (
    SCHOOL_PROGRESS_FILE,
    count_cross,
    load_progress,
    log,
    log_history,
    record_study_finish,
    save_progress,
    set_current_school,
    set_current_school_duration,
)
from src.scenario import CLICK_INTERVAL, DeviceScenario, NAV_TIMEOUT

# 属性点 -> 三栏选择框定位名（力量/智力/魅力 对应第一/二/三框；初级/中级学园用）
ATTRIBUTE_COURSES = {
    '力量': 'select_box_1',
    '智力': 'select_box_2',
    '魅力': 'select_box_3',
}

# 高级学园的课程顺序固定为 魅力/力量/智力（与初级/中级不同，
# 选课前需 OCR 上半屏识别学园阶段来决定点哪个框）
ADVANCED_ATTRIBUTE_COURSES = {
    '魅力': 'select_box_1',
    '力量': 'select_box_2',
    '智力': 'select_box_3',
}
# 进修学院的课程顺序固定为 力量/魅力/智力（与高级学园不同）
INSTITUTE_ATTRIBUTE_COURSES = {
    '力量': 'select_box_1',
    '魅力': 'select_box_2',
    '智力': 'select_box_3',
}
ADVANCED_STAGES = ('高级学园', '进修学院')

# 课时时长（school.duration）：学园课程轮播 = 3 张 10 分钟课 + 3 张 30 分钟课
# （个别阶段另有第 7 张），卡序：初级学园 = [10分:力量/智力/魅力] + [30分:力量/智力/魅力]
DURATION_CHOICES = ('10分钟', '30分钟')
# 30 分钟课卡名表（2026-09 初级学园实测，用于翻页后按卡名点击；未知阶段按
# "用时:30分钟"标签位置兜底）。中级/高级/进修学院的名字待毕业后实测补充。
COURSE30_NAMES = {
    '初级学园': {'力量': '田径运动课', '智力': '世界地理课', '魅力': '演说表达课'},
}
# 面板标题识别规则（在选课页检测，页面只有一个学园标题，不需要靠编号/后缀防误判）：
# - 初级/中级/高级学园：形如"初级学园 5年级"（年级可省略）
# - 进修学院：形如"进修学院 研修生12" / "进修学院 研修生Ⅰ"，真实 OCR 常把编号
#   （Ⅰ/Ⅱ 等罗马数字实际ocr可能出现缺读）（如"进修学院研修生"），
#   所以只要求"进修学院"出现即可，"研修生"可选
_STAGE_RE = re.compile(
    r'(初级学园|中级学园|高级学园)(?:\s*[\d一二三四五六七八九十]+\s*年级)?'
    r'|(进修学院)(?:\s*研修生)?')

PROGRESS_FILE = SCHOOL_PROGRESS_FILE


class SchoolScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.attribute = self.cfg.school.attribute
        if self.attribute not in ATTRIBUTE_COURSES:
            raise ValueError(
                f'config.yaml 中 school.attribute 配置无效: {self.attribute!r}，'
                f'可选: {"/".join(ATTRIBUTE_COURSES)}'
            )
        self.duration = self.cfg.school.duration
        if self.duration not in DURATION_CHOICES:
            raise ValueError(
                f'config.yaml 中 school.duration 配置无效: {self.duration!r}，'
                f'可选: {"/".join(DURATION_CHOICES)}'
            )
        # 最近一次选课前识别到的学园阶段（resolve_course_box 里更新，供 30 分钟
        # 课按阶段名字表找卡）
        self._stage: str | None = None
        self.times_per_day = self.cfg.school.times_per_day
        # 毕业处理防循环标志：关闭毕业面板后重新进学校仍出现毕业标志时抛异常，
        # 走重试链而不是无限"毕业->回主页面->再进"空转；成功看到 school_start 时重置
        self._graduated_once = False
        log(f'属性点: {self.attribute}，课时时长: {self.duration}，每天学习次数: '
            f'{self.times_per_day if self.times_per_day else "不限"}')

    # ---- 各阶段 ----

    def goto_school(self) -> str | None:
        """主页面 -> 出门 -> 反复点学校直到出现 school_start。

        出门后若正在上课/工作/冒险/被雇佣中（上次中途停止），等待结束并退出、回主页面，
        返回等完的是哪种（'school' / 'work' / 'adventure' / 'employed'）——此时不再继续进学校，
        由调用方/执行器重新判断限制条件后再决定下一步；
        学校面板出现毕业标志（"去找同学玩"，没有"去上课"）时，点"关闭"再点两次 back
        回主页面，返回 'graduated'，由 run() 重新进学校选择下一阶段课程；
        正常情况返回 None。
        """
        self.leave_home()
        finished = self.wait_busy_end()
        if finished:
            self.ensure_main_page()
            return finished
        try:
            clicked = False
            for attempt in range(1, NAV_TIMEOUT + 1):
                source = self.dev.hierarchy()
                # 体力/清洁不足时提示条会顶掉 school_start：面板已开但开始按钮
                # 不出现时不能干等到超时，同帧检测命中则回主页面护理一次
                self.handle_low_stat_dialog(source)
                if self.see('school_start', None, source):
                    self._graduated_once = False
                    return None
                if self.see('school_graduated', None, source):
                    if self._graduated_once:
                        raise RuntimeError('毕业面板关闭后重新进学校仍出现毕业标志')
                    self._graduated_once = True
                    self._close_graduation()
                    return 'graduated'
                school = self.see('school', None, source)
                if school:
                    self.click(school[0], school[1])
                    clicked = True
                elif clicked:
                    # 学校气泡点完消失但面板标志没识别到：已进入面板，继续选课
                    log('前往学校: school 已消失，进入选课')
                    return None
                else:
                    log(f'前往学校: 未找到 school，等待重试 ({attempt}/{NAV_TIMEOUT})')
                time.sleep(CLICK_INTERVAL)
            raise RuntimeError(f'前往学校: 重试 {NAV_TIMEOUT} 次仍未出现 school_start')
        except RuntimeError:
            # 正在打工/冒险等时点学校入口不会进入选课面板，导航必然超时；
            # 屏幕早已稳定，重新检测一次进行中状态再下结论
            finished = self._recheck_busy_after_nav('前往学校')
            if finished:
                return finished
            raise

    def _close_graduation(self) -> None:
        """毕业面板：点"关闭"按钮，再点两次 back 回主页面。"""
        log('检测到毕业标志（去找同学玩），点关闭并回主页面重新进学校')
        close = self.see('school_graduate_close')
        if not close:
            raise RuntimeError('毕业面板未找到"关闭"按钮')
        self.click(close[0], close[1])
        time.sleep(CLICK_INTERVAL)
        for _ in range(2):
            if not self.go_back():
                raise RuntimeError('毕业面板关闭后未找到 back 按钮')
            time.sleep(CLICK_INTERVAL)

    def select_course(self) -> None:
        """选课：先 OCR 上半屏识别学园阶段（决定属性对应第几张卡），把轮播归位到
        第一页；10 分钟课直接点框，30 分钟课前拖翻页后按卡名点选。

        选完把课时时长写入 school_progress.json 的 duration 字段：一节课结算时
        按它累计学习时长（10分钟=600s / 30分钟=1800s，见 record_study_finish）。
        """
        box = self.resolve_course_box()
        self.reset_select_boxes(drags=3)
        if self.duration == '30分钟':
            name = COURSE30_NAMES.get(self._stage or '', {}).get(self.attribute, '')
            for attempt in (1, 2):
                clicked = bool(name) and self._click_card_by_name(name)
                if not clicked:
                    clicked = self._click_nth_30min(box)
                if clicked and self.verify_course_selected():
                    set_current_school_duration(self.duration)
                    return
                log('点选未通过核对，归位重试' if attempt == 1 else '点选仍未通过核对')
                self.reset_select_boxes(drags=3)
            raise RuntimeError(
                f'30分钟课未定位或未选中（阶段 {self._stage!r}，{self.attribute}），本轮放弃')
        log(f'选择课程: {self.attribute} ({box})')
        hit = self.see(box)
        if not hit:
            raise RuntimeError(f'未定位到课程选择框: {box}')
        time.sleep(CLICK_INTERVAL)
        self.click(hit[0], hit[1])
        if not self.verify_course_selected():
            log('选课核对未通过，重试一次')
            time.sleep(0.5)
            hit = self.see(box)
            if hit:
                self.click(hit[0], hit[1])
            if not self.verify_course_selected():
                raise RuntimeError(
                    f'选课核对未通过（{self.duration} {self.attribute}），本轮放弃')
        set_current_school_duration(self.duration)

    def _drag_card_step(self) -> None:
        """慢速小步前滑约 1 张卡（332px；慢拖惯性小、步进稳定，实测 1 步 1 张）。"""
        self.dev.drag(920, 1336, 588, 1336, 0.8)

    def _card_row_results(self) -> list[tuple[str, int, int, float]]:
        """卡片行区域 OCR：等待惯性滑动结束（连续两帧一致）再返回，避免点空。"""
        prev = None
        results = []
        for _ in range(5):
            results = ocr_texts(self.screen())
            row = tuple(sorted((t, x, y) for t, x, y, _ in results
                               if 1300 < y < 1570))
            if row == prev:
                return results
            prev = row
            time.sleep(0.35)
        return results

    def _click_card_by_name(self, name: str, max_steps: int = 6) -> bool:
        """在课程轮播里小步前滑扫描，找到卡名含 name 的卡就点击。

        每步只滑约 1 张卡（慢拖，惯性小、步进稳定）；两次检测画面不变说明
        已到轮播尽头，放弃返回 False（说明卡名表与游戏不一致，需更新）。
        """
        prev = None
        for i in range(max_steps + 1):
            results = self._card_row_results()
            hits = [(x, y) for t, x, y, _ in results
                    if name in t and 1300 < y < 1570]
            if hits:
                x, y = hits[0]
                log(f'点击课程卡「{name}」({x}, {y + 30})')
                self.click(x, y + 30)
                return True
            sig = tuple(sorted((t, x) for t, x, y, _ in results if 1300 < y < 1570))
            if sig == prev:
                log(f'已滑到轮播尽头仍未见到「{name}」')
                return False
            prev = sig
            log(f'当前页未见「{name}」，小步前滑继续找 ({i + 1})')
            self._drag_card_step()
        return False

    def verify_course_selected(self) -> bool:
        """核对详情面板：奖励<属性>+N 与配置一致（10分钟课+2 / 30分钟课+5）。

        详情面板点选后即时刷新；OCR 会把末尾"+5点"读成"+50/③"等，
        所以只取奖励后的第一位数字比对；偶尔拆行，按相邻行合并后再匹配。
        """
        want = {'10分钟': '2', '30分钟': '5'}[self.duration]
        time.sleep(0.5)
        results = ocr_texts(self.screen())
        for t, x, y, _ in results:
            if '奖励' not in t or not (1400 < y < 2200):
                continue
            band = ''.join(tt.replace(' ', '') for tt, xx, yy, _ in results
                           if abs(yy - y) <= 70)
            m = re.search(r'奖励(力量|智力|魅力)[+＋]?(\d)', band)
            if m and m.group(1) == self.attribute and m.group(2) == want:
                return True
            log(f'选课核对: 详情显示 {band[:60]!r}，与 {self.attribute}+{want} 不符')
            return False
        log('选课核对: 详情面板未找到"奖励"行')
        return False

    def _click_nth_30min(self, box: str) -> bool:
        """未知学园兜底：小步滑到轮播尽头，按"用时:30分钟"标签位置点第 N 张。"""
        n = int(box.rsplit('_', 1)[-1])  # select_box_2 -> 2
        prev = None
        for _ in range(8):
            row = self._card_row_results()
            sig = tuple(sorted((t, x) for t, x, y, _ in row if 1300 < y < 1570))
            if sig == prev:
                break
            prev = sig
            self._drag_card_step()
        results = self._card_row_results()
        labels = sorted((x, y) for t, x, y, _ in results
                        if '30分钟' in t and 1300 < y < 1570)
        if len(labels) >= n:
            x, y = labels[n - 1]
            log(f'按 30 分钟标签点第 {n} 张 ({x}, {y - 40})')
            self.click(x, y - 40)
            return True
        return False


    def resolve_course_box(self) -> str:
        """OCR 上半屏识别学园阶段，返回该点哪个课程选择框。

        初级/中级学园课程顺序固定 力量/智力/魅力 -> 第一/二/三框；
        高级学园固定 魅力/力量/智力；进修学院固定 力量/魅力/智力。
        识别不到阶段回退默认顺序（初级/中级的 力量/智力/魅力）。
        """
        screen = self.screen()
        results = ocr_texts(screen[: screen.shape[0] // 2])
        stage = self._detect_stage(results)
        self._stage = stage
        if stage:
            # 学习开始时把当前学园持久化到 school_progress.json（不一致才更新；
            # 结算时长以"课时时长"（duration 字段）为准，学园仅作旧会话兜底）
            set_current_school(stage)
        if stage == '进修学院':
            box = INSTITUTE_ATTRIBUTE_COURSES[self.attribute]
            log(f'学园阶段: {stage}，课程顺序 力量/魅力/智力，{self.attribute} -> {box}')
            return box
        if stage in ADVANCED_STAGES:
            box = ADVANCED_ATTRIBUTE_COURSES[self.attribute]
            log(f'学园阶段: {stage}，课程顺序 魅力/力量/智力，{self.attribute} -> {box}')
            return box
        box = ATTRIBUTE_COURSES[self.attribute]
        log(f'学园阶段: {stage or "未识别"}，课程顺序 力量/智力/魅力，{self.attribute} -> {box}')
        return box

    @staticmethod
    def _detect_stage(results: list[tuple[str, int, int, float]]) -> str | None:
        """从上半屏 OCR 结果里识别学园阶段，返回匹配到的阶段名或 None。

        用子串包含匹配（不是精确相等）：实际文案带年级后缀（'初级学园 5年级'）、
        图标前缀等都能命中；单个文本块没命中时再拼全部文本兜底（防止拆块）。
        """
        for text, *_ in results:
            m = _STAGE_RE.search(text.replace(' ', ''))
            if m:
                return m.group(1) or m.group(2)
        merged = ''.join(t.replace(' ', '') for t, *_ in results)
        m = _STAGE_RE.search(merged)
        return (m.group(1) or m.group(2)) if m else None

    def wait_class_end(self) -> bool:
        """等待下课并点击 quit。返回 True 表示还能继续学。"""
        self.wait_end('school_in', 'school_end', encourage=True)
        again = self.see('school_start')
        if again:
            log('还可以继续学习')
            return True
        log('没有 school_start 了，返回主页面')
        return False

    def attend_class(self) -> bool:
        """选课 -> 开始学习 -> 等待下课 -> quit。返回 True 表示还能继续学。"""
        self.select_course()
        self.click_until_gone_or_see('school_start', 'school_in', '开始学习')
        log('已进入课堂，等待下课...')
        if self.defer_wait:
            # 延时收尾：进行中登记 pending（到点由调度器 finish_pending 收尾）或
            # 已结束原地收尾，然后回主页面；计数统一走 on_finish（count_cross），
            # 本地 learned 不再自增，防止重复计数
            self.defer_busy_end('school_in', 'school_end',
                                lambda: count_cross('school'), '上课', encourage=True)
            self.ensure_main_page()
            return False
        return self.wait_class_end()

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """max_times: 当天学习次数上限，0 表示不限；None 表示用配置值。
        max_rounds: 最多跑多少轮后返回，0 为不限。
        一轮 = 回主页面进学校上一节课，课后返回主页面（供执行器逐节判断金币）。
        返回本次调用是否完成了至少一次学习。
        """
        if max_times is None:
            max_times = self.times_per_day
        today, learned, history = load_progress(PROGRESS_FILE)
        log_history(history, today)
        start_learned = learned
        if max_times and learned >= max_times:
            log(f'今天已学满 {max_times} 次，无需再学')
            return False
        round_no = 0
        while True:
            round_no += 1
            log(f'===== 第 {round_no} 轮 =====')
            self.ensure_main_page()
            finished = self.goto_school()
            if finished:
                if finished == 'graduated':
                    # 毕业面板已关闭并回主页面：不计数也不算一轮（毕业不是上课），
                    # 立即重新进学校选下一阶段课程，不等下一轮调度（否则要等
                    # success_interval 才重试）；防循环由 goto_school 的
                    # _graduated_once 保证（连续毕业抛异常走重试链）
                    log('毕业处理完成，重新进学校')
                    continue
                if self.pending is not None:
                    # 延时收尾模式：出门检测到的进行中活动已登记 pending，计数由
                    # finish_pending 收尾时统一进行（on_finish），本地不再计数，
                    # 本轮直接结束
                    return True
                if finished == 'school':
                    # 出门时等完了一节上次未结束的课，计入当天次数 + 学习时长
                    learned += 1
                    save_progress(PROGRESS_FILE, today, learned, history)
                    record_study_finish()
                    log(f'已完成第 {learned} 次学习' + (f' / 目标 {max_times} 次' if max_times else ''))
                    if max_times and learned >= max_times:
                        log('达到当天学习次数，结束')
                        return True
                elif finished != 'employed':
                    # 出门时等完的是别的活动（打工/冒险），计入对应次数
                    # （被雇佣在召回点 quit 时已计数）
                    count_cross(finished)
                # 等完了一次活动，计数已变化，本轮结束，
                # 回主页面交由执行器重新判断限制条件
                if max_rounds and round_no >= max_rounds:
                    return True
                log('本轮结束，回主页面重新开始')
                continue
            cont = self.attend_class()
            if self.defer_wait:
                # 延时收尾模式：计数在 pending 收尾时统一进行，本轮直接结束
                return True
            learned += 1
            save_progress(PROGRESS_FILE, today, learned, history)
            record_study_finish()
            log(f'已完成第 {learned} 次学习' + (f' / 目标 {max_times} 次' if max_times else ''))
            if max_times and learned >= max_times:
                log('达到当天学习次数，结束')
                return learned > start_learned
            if not cont:
                log('本次没有更多课程了')
            if max_rounds and round_no >= max_rounds:
                log(f'已跑完 {max_rounds} 轮，返回')
                return learned > start_learned
            log('本轮结束，回主页面重新开始')


if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='学校上课场景')
    ap.add_argument('--times', type=int, default=None,
                    help='当天学习次数上限，0 为不限；不指定则读 config.yaml 的 school.times_per_day')
    args = ap.parse_args()

    try:
        SchoolScenario().run(max_times=args.times)
    except KeyboardInterrupt:
        log('手动停止')
