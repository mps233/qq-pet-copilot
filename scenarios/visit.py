"""踩踩场景：访问好友宠物页点"踩踩"。

流程（u2 控件定位，分辨率无关）：
1. 点击 好友（visit_friends）打开好友面板
2. 点击 访问（visit）进入第一个好友的宠物页
   （模拟器模式：门禁已翻转（scheme 路径）时与真机一致；门禁未翻转
   （frida 兜底）时改为 root am start 直开好友页，见 goto_first_friend）
3. 点击 踩踩（visit_step），当天次数 +1 并持久化到 runs/visit_progress.json
   （访问进入时默认就是好友列表的第一个好友）；
   若出现已踩标志（visit_stepped，"已踩"——今天已踩过该好友），
   跳过不计数，直接切换下一个好友
4. 切换下一个好友：重新抓取好友列表（content-desc 以 "好友 " 开头的项，
   注意空格，和入口按钮"好友"区分），按列表顺序点下一个。
   列表是滚动加载的，控件树里只有当前可见项，所以内部维护一份
   累积好友名单：每次抓取只把新出现的好友追加到尾部、不删除滚出
   屏幕的项，切换索引基于累积名单才不会乱；
   重复直到踩满配置次数或没有更多好友
5. 结束：关闭好友页面（点 back 直到 visit/visit_step 都消失）

运行方式：
- run()：独立运行，开头/结尾 ensure_main_page（执行器在主页面调度用）

运行：python scenarios/visit.py            （Ctrl+C 停止）
      python scenarios/visit.py --times 5 （覆盖配置的每天踩踩次数，0 为不限）
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import opener
from src.locators import LOCATORS, see_bounds
from src.ocr import ocr_texts
from src.progress import (
    VISIT_PROGRESS_FILE,
    exp_daily_done,
    load_exp_daily,
    load_progress,
    log,
    log_exp_daily,
    log_history,
    save_exp_daily,
    save_progress,
)
from src.scenario import CLICK_INTERVAL, NAV_TIMEOUT, DeviceScenario
from scenarios.care import ONE_CLICK_PAY_RETRIES

FRIEND_ITEM_XPATH = LOCATORS['visit_friend_item']['xpath'][0]
STEP_RETRIES = 5  # 切换好友后踩踩按钮有几秒加载延迟，重试次数
# 非好友标志文字区（右上角，1080×2412 基准）：好友页="点亮中1/3"，
# 非好友/系统推荐="加好友"（与 gift_bag.FRIEND_MARK_REGION 同区同判据，实测稳定互斥）
FRIEND_MARK_REGION = (700, 160, 980, 280)

PROGRESS_FILE = VISIT_PROGRESS_FILE


# 好友名单缓存（仪表盘下拉用）：调度器扫好友时写，避免手输好友名
from datetime import datetime
from src.config import PROJECT_ROOT

FRIENDS_CACHE_FILE = PROJECT_ROOT / 'runs' / 'friends_cache.json'


def save_friends_cache(names: list[str]) -> None:
    """把累积好友名单写入缓存（供仪表盘下拉选择）。

    数据源是好友列表控件的 content-desc（形如 "好友 墨瞳"）—— 这是游戏给的
    原生描述，不是 OCR 结果，所以准确。**只收带 "好友 " 前缀的项**：
    调用方传的是累积名单，但保险起见仍做前缀校验，避免把其它 content-desc
    混进来（如"当前可见好友"之类的日志文本、纯符号昵称）。

    保留结构：
      {"names": [...], "updated": "...", "source": "friend_list_content_desc"}
    source 标注数据来源，便于前端说明"这是主人昵称，不是宠物名"。
    """
    import json as _json
    clean: list[str] = []
    for raw in names:
        n = str(raw or '').strip()
        if not n.startswith('好友 '):
            continue                    # 只收好友列表项，其余一律丢弃
        n = n[3:].strip()
        if not n or len(n) > 24:
            continue
        # 至少含一个字母/数字/汉字（过滤纯符号）
        if not any(ch.isalnum() or '\u4e00' <= ch <= '\u9fff' for ch in n):
            continue
        if n not in clean:
            clean.append(n)
    if not clean:
        return
    try:
        FRIENDS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        old: list[str] = []
        if FRIENDS_CACHE_FILE.exists():
            try:
                old = _json.loads(FRIENDS_CACHE_FILE.read_text('utf-8')).get('names') or []
            except Exception:  # noqa: BLE001
                old = []
        merged = list(old)
        for n in clean:
            if n not in merged:
                merged.append(n)
        FRIENDS_CACHE_FILE.write_text(
            _json.dumps({
                'names': merged,
                'source': 'friend_list_content_desc',
                'note': '好友列表的主人昵称（非宠物名）；宠物名请在设置页手动输入',
                'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }, ensure_ascii=False, indent=1),
            encoding='utf-8')
    except OSError as e:
        log(f'写好友缓存失败: {e}')


class VisitScenario(DeviceScenario):
    def __init__(self, dev=None):
        super().__init__(dev)
        self.times_per_day = self.cfg.visit.times_per_day
        self._exp_handled = False  # 本轮是否处理过经验照顾（点击/判定完成）
        self._friends_exhausted = False  # 本轮好友名单是否已走完（没有更多可访问）
        log(f'每天踩踩次数: {self.times_per_day if self.times_per_day else "不限"}')

    # ---- 各阶段 ----

    def goto_first_friend(self) -> None:
        """好友面板 -> 访问 -> 第一个好友宠物页（出现踩踩按钮）。

        模拟器模式（opener.EMULATOR_MODE）：门禁已本地翻转（opener.GATE_OPEN，
        scheme 路径）时游戏内"访问"与真机一致直接可用；仅当走了 frida 兜底
        （门禁仍关）才用 root am start 直开好友宠物页
        （petUin=好友入口缓存的 uin + attrs，见 src/opener.py）。
        """
        if opener.EMULATOR_MODE and not opener.GATE_OPEN:
            self._goto_first_friend_emulator()
            return
        self.click_until_gone_or_see('visit_friends', 'visit', '打开好友列表')
        # 不额外等固定 1 秒：点访问靠 click_until_gone_or_see 重试（点不中下一轮再点）
        self.click_until_gone_or_see('visit', 'visit_step', '访问好友')

    def _goto_first_friend_emulator(self) -> None:
        """模拟器 frida 兜底（门禁未翻转）：am start 直开好友宠物页；
        缓存失效则重新捕获一次再试。"""
        adb = self.dev.adb.adb
        serial = self.dev.adb.serial
        for attempt in (1, 2):
            entry = opener.ensure_friend_entry(adb, serial, self.dev)
            opener.am_start_pet_page(adb, serial, entry['uin'], entry['attrs'])
            # 等好友页渲染（踩踩/已踩 按钮出现）。好友页 chrome（按钮/好友列表）
            # 首次加载偏慢（实测可能 >10 秒），等待轮数给足 NAV_TIMEOUT*2
            for _ in range(NAV_TIMEOUT * 2):
                source = self.dev.hierarchy()
                if (self.see('visit_step', source=source)
                        or self.see('visit_stepped', source=source)):
                    return
                time.sleep(CLICK_INTERVAL)
            opener.invalidate_friend_entry()
            log(f'am start 进好友页超时，好友入口缓存可能失效，重新捕获 ({attempt}/2)')
        raise RuntimeError('好友页未找到踩踩按钮（am start 直开 + 重新捕获均失败）')

    def step_once(self) -> str:
        """点一次踩踩，返回 'stepped'；检测到已踩标志（今天踩过该好友）
        返回 'already' 由调用方跳过切换下一个（切换好友后按钮有加载延迟，重试几次）。"""
        for attempt in range(1, STEP_RETRIES + 1):
            source = self.dev.hierarchy()
            if self.see('visit_stepped', source=source):
                return 'already'
            hit = self.see('visit_step', source=source)
            if hit:
                self.click(hit[0], hit[1])
                time.sleep(CLICK_INTERVAL)
                return 'stepped'
            log(f'未找到踩踩按钮，等待重试 ({attempt}/{STEP_RETRIES})')
            time.sleep(CLICK_INTERVAL)
        raise RuntimeError('好友页未找到踩踩按钮')

    def _friend_items(self) -> list[tuple[str, int, int]]:
        """当前可见的好友列表项：[(content-desc, 中心x, 中心y)]，按从上到下排序。"""
        els = self.dev.d.xpath(FRIEND_ITEM_XPATH).all()
        items = []
        for e in els:
            left, top, right, bottom = e.bounds
            items.append((e.attrib.get('content-desc', ''),
                          int((left + right) / 2), int((top + bottom) / 2)))
        items.sort(key=lambda it: (it[2], it[1]))
        log('当前可见好友: ' + (', '.join(f'{d}@({x},{y})' for d, x, y in items) or '无'))
        return items

    def _accumulate_friends(self) -> list[tuple[str, int, int]]:
        """抓取当前可见好友并追加进累积名单（不删除滚出屏幕的项），返回可见项。

        同时把名单持久化到 runs/friends_cache.json，供仪表盘下拉选择
        （避免手输好友名 —— OCR/输入都容易错）。
        """
        visible = self._friend_items()
        new = [desc for desc, _, _ in visible if desc and desc not in self._friends]
        for desc in new:
            self._friends.append(desc)
        log(f'累积好友名单({len(self._friends)}): '
            + (', '.join(self._friends) or '无')
            + (f'（新增: {", ".join(new)}）' if new else ''))
        if new:
            save_friends_cache(self._friends)
        return visible

    def is_non_friend_page(self, screen=None) -> bool:
        """当前好友宠物页的主人不是好友（系统推荐）：右上角出现"加好友"。

        好友页该位置显示"点亮中1/3"（实测稳定互斥，位置约 (800,210)，
        与 gift_bag 的非好友判据同源）。只认读到"加好友"字样为准；
        区域什么都没读到返回 False（页面未加载完不判，避免误停）。
        """
        img = screen if screen is not None else self.screen()
        x1, y1, x2, y2 = FRIEND_MARK_REGION
        h, w = img.shape[:2]
        rx1, ry1 = int(x1 * w / 1080), int(y1 * h / 2412)
        rx2, ry2 = int(x2 * w / 1080), int(y2 * h / 2412)
        return any('加好友' in t for t, *_ in ocr_texts(img[ry1:ry2, rx1:rx2]))

    def next_friend(self, allow_non_friend: bool = False, max_switches: int = 0) -> bool:
        """切换到下一个目标：按累积名单顺序点下一个。

        好友列表滚动加载，控件树里只有当前可见项：每次重新抓取只把
        新出现的目标追加到累积名单尾部（不删除滚出屏幕的项），切换索引
        基于累积名单；点击目标从当前可见项里按 content-desc 找，
        找不到（还没滚出来）视为没有更多（置 _friends_exhausted）。

        allow_non_friend=False（踩踩）：落到非好友/系统推荐页就**认定后面全是非好友**
        —— 实测好友都在轮播前面，后面是每天不同的系统推荐；此时把累积名单**截断到
        分界点**并结束本轮（不往后累积，否则名单无限膨胀、永远切不完）。
        True（PK）：非好友也返回 True 照打（PK 允许打非好友）；名单不截断，
        靠 max_switches + MAX_TARGETS 双重上限保证一定能结束。
        """
        visible = self._accumulate_friends()
        self._friend_index += 1
        if max_switches and self._friend_index >= max_switches:
            log(f'切换已达上限 {max_switches} 个，停止')
            self._friends_exhausted = True
            return False
        if self._friend_index >= len(self._friends):
            self._friends_exhausted = True
            return False
        target = self._friends[self._friend_index]
        hit = next(((x, y) for desc, x, y in visible if desc == target), None)
        if hit is None:
            # 还没滚出来 / 列表没加载完：视为没有更多（置穷尽，避免反复空转）
            self._friends_exhausted = True
            log(f'下一个 {target} 当前不可见，停止切换')
            return False
        log(f'切换第 {self._friend_index + 1} 个: {target} ({hit[0]}, {hit[1]})')
        self.click(hit[0], hit[1])
        time.sleep(CLICK_INTERVAL)
        if self.is_non_friend_page():
            time.sleep(1.0)
            if self.is_non_friend_page():
                if allow_non_friend:
                    log(f'进入非好友/系统推荐页: {target}（PK 允许打非好友，继续）')
                    return True
                # 踩踩：好友都排在前面，此后的都是系统推荐（每天不同）
                # → 截断名单（不累积）并结束，避免"名单永远累积不完"
                self._friends = self._friends[:self._friend_index]
                self._friends_exhausted = True
                log(f'遇到非好友/系统推荐（右上角"加好友"）：{target}，'
                    '后面的都是系统推荐（每天不同），停止切换')
                return False
        return True

    def close(self) -> None:
        """关闭好友相关页面：点 back 直到 踩踩/访问/好友列表 都消失。"""
        for _ in range(5):
            source = self.dev.hierarchy()
            if not (self.see('visit_step', source=source)
                    or self.see('visit', source=source)
                    or self.dev.find_xpath_all(FRIEND_ITEM_XPATH, source=source)):
                return
            if not self.go_back(source=source):
                break
            time.sleep(CLICK_INTERVAL)
        log('关闭好友页面失败（可能未回到进入前的页面）')

    def _visit_all(self, max_times: int, today: str, done: int, history: dict) -> int:
        """从好友面板开始踩满剩余次数，期间顺带做经验日常（好友页照顾区域有 exp 就点）。

        踩踩次数已满但经验日常未完成时，仍继续遍历好友做经验照顾，跳过已踩/踩踩判断。
        """
        self._friends = []        # 累积好友名单（content-desc），只增不减
        self._friend_index = 0    # 访问进入时默认第一个好友
        self._exp_handled = False  # 本轮是否处理过经验照顾（点击/判定完成）
        self._friends_exhausted = False  # 本轮好友名单是否已走完
        self.goto_first_friend()
        if self.is_non_friend_page():
            time.sleep(1.0)
            if self.is_non_friend_page():
                log('好友列表第一个就是非好友（系统推荐），没有可访问的好友')
                self._friends_exhausted = True
                return done
        exp_today, exp_done, exp_history = load_exp_daily(quiet=True)
        while True:
            if not max_times or done < max_times:
                # 已踩/踩踩 判断处理
                if self.step_once() == 'already':
                    log('该好友今天已踩过，跳过')
                else:
                    done += 1
                    save_progress(PROGRESS_FILE, today, done, history)
                    log(f'已踩踩 {done} 次' + (f' / 目标 {max_times} 次' if max_times else ''))
            # 切换好友前：经验日常未完成则先处理照顾
            if not exp_done:
                exp_done = self._try_exp_daily(exp_today, exp_history)
            done_full = bool(max_times) and done >= max_times
            if done_full and exp_done:
                break
            if not self.next_friend():
                log('没有更多好友了')
                break
        return done

    def _try_exp_daily(self, exp_today: str, exp_history: dict) -> bool:
        """当前好友宠物页尝试经验日常：有 one_click_care 时 OCR 其父级"照顾区域"，
        含"exp/经验"就点 one_click_care；区域无则视为当日经验日常完成并持久化。
        返回经验日常是否已完成。"""
        # 一次控件树快照复用：one_click_care 和父级 care_region 同一次 dump 里查
        source = self.dev.hierarchy()
        care = self.see('one_click_care', source=source)
        if not care:
            return False  # 没有照顾按钮，跳过（不标记完成）
        bounds = see_bounds(self.dev, 'care_region', source=source)
        if not bounds:
            return False
        x1, y1, x2, y2 = bounds
        results = ocr_texts(self.screen()[y1:y2, x1:x2])
        log('照顾区域 OCR: '
            + (', '.join(f'{t!r}@({x},{y})' for t, x, y, _ in results) or '无'))
        if any(('exp' in (t or '').lower()) or ('经验' in (t or '')) for t, *_ in results):
            log(f'照顾区域含 exp 经验值，点击一键护理 ({care[0]}, {care[1]})')
            self.click(care[0], care[1])
            self._exp_handled = True
            # 支付确认弹窗可能比护理按钮点击晚一拍出现，短等几次再判断（同 care.py）
            for attempt in range(1, ONE_CLICK_PAY_RETRIES + 1):
                pay = self.see('one_click_pay')
                if pay:
                    log(f'检测到"支付并护理"，点击确认 ({pay[0]}, {pay[1]})')
                    self.click(pay[0], pay[1])
                    time.sleep(CLICK_INTERVAL)
                    break
                if attempt < ONE_CLICK_PAY_RETRIES:
                    time.sleep(CLICK_INTERVAL)
            return False
        log('照顾区域无 exp，经验日常已完成')
        save_exp_daily(True, exp_today, exp_history)
        self._exp_handled = True
        return True

    # ---- 入口 ----

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """独立运行：回主页面后进好友面板踩满剩余次数，再回主页面。

        max_rounds 参数仅为与其他场景签名一致，踩踩一次调用完成整个会话。
        返回本次是否踩了至少一次。
        """
        if max_times is None:
            max_times = self.times_per_day
        today, done, history = load_progress(PROGRESS_FILE)
        log_history(history, today)
        log_exp_daily()  # 显示经验日常当天状态与历史
        if max_times and done >= max_times:
            if exp_daily_done():
                log(f'今天已踩满 {max_times} 次且经验日常已完成，无需再踩')
                return False
            log(f'今天已踩满 {max_times} 次，经验日常未完成，继续处理经验日常')
        start_done = done
        self.ensure_main_page()
        done = self._visit_all(max_times, today, done, history)
        self.close()  # 先点 back 收掉好友相关页面，再确认回主页面
        self.ensure_main_page()
        # 好友已全部走完但没凑满次数（好友数 < 目标次数，如 6 个好友 / 目标 10 次）：
        # 这就是今天的上限，标记"今日完成"（返回 False → runner 置 task.dead），
        # 否则调度器会按 success_interval 反复重进好友面板空转（实测连跑 4 轮）。
        if self._friends_exhausted and (not max_times or done < max_times):
            log(f'踩踩: 好友已全部走完（{done}'
                + (f'/{max_times}' if max_times else '') + ' 次），今天不再重试')
            return False
        # 踩了或处理过经验照顾（点击/判定完成）都算本轮有产出；
        # 不能把"早已完成的经验日常"算产出——那会让调度器反复重跑踩踩空转
        return done > start_done or self._exp_handled

if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='踩踩场景')
    ap.add_argument('--times', type=int, default=None,
                    help='当天踩踩次数上限，0 为不限；不指定则读 config.yaml 的 visit.times_per_day')
    args = ap.parse_args()

    try:
        VisitScenario().run(max_times=args.times)
    except KeyboardInterrupt:
        log('手动停止')
