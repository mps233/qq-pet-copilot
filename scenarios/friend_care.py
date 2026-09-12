"""好友护理场景：访问指定好友家，按配置方式护理好友的宠物一次。

流程（好友导航复用 visit.py：累积名单、按顺序切换）：
1. 点击 好友（visit_friends）-> 访问（visit）进入第一个好友宠物页
2. 依次切换好友列表（content-desc "好友 xxx"），直到找到配置的好友名称，点击切换到那个好友家
3. 按护理好友方式（friend_care.method，选项同 care.method）护理好友的宠物一次：
   - ocr检测：展开好友状态面板读体力/清洁，不足则喂食/洗澡到 FRIEND_CARE_TARGET（90）
   - 一键护理：好友页有一键护理按钮就点（含"支付并护理"确认），没有视为状态正常跳过
4. 结束：关闭好友页面（点 back）回主页面。每次调度只做一次护理巡检，
   下次调度间隔由 friend_care.interval_seconds 控制（场景内不再等待/
   切换好友刷新状态——单次巡检无需刷新）

配置（config.yaml 的 friend_care 段）：enabled 开关 / time_range 时间段（HH:MM-HH:MM）/
friend_name 护理好友名称 / method 护理好友方式 / interval_seconds 调度间隔（秒）。

运行：python scenarios/friend_care.py            （Ctrl+C 停止）
"""

import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.progress import log
from src.ocr import ocr_texts
from src.scenario import CLICK_INTERVAL, DeviceScenario
from scenarios.care import CARE_METHODS, ONE_CLICK_PAY_RETRIES, CareScenario
from scenarios.visit import VisitScenario

FRIEND_CARE_TARGET = 90  # ocr检测方式下好友体力/清洁的护理目标值
MAX_FRIEND_SWITCHES = 30  # 查找/切回目标好友时最多切换次数（防无限切换）
FRIEND_CARE_RETRIES = 2   # 进好友家/护理偶发卡顿时回主页面重进好友家再试的次数

# 好友宠物页顶部"主人昵称 + 宠物名"两行大字区域（pk.py 同款；宠物名 = y 靠下的行）
FRIEND_NAME_REGION = (150, 130, 700, 260)
FRIEND_NAME_MIN_Y = 55


def parse_time_range(value: str, name: str = 'friend_care.time_range') -> tuple:
    """解析时间段 'HH:MM-HH:MM' 为 (开始, 结束) time。"""
    try:
        start_s, end_s = str(value).split('-', 1)
        start = datetime.strptime(start_s.strip(), '%H:%M').time()
        end = datetime.strptime(end_s.strip(), '%H:%M').time()
    except ValueError:
        raise ValueError(
            f'config.yaml 中 {name} 格式无效: {value!r}，应为 HH:MM-HH:MM') from None
    return start, end


def in_time_range(now, start, end) -> bool:
    """now 是否在 [start, end) 时间段内（end <= start 视为跨零点，如 22:00-02:00）。"""
    if end > start:
        return start <= now < end
    return now >= start or now < end


class _FriendCare(CareScenario):
    """好友家护理：复用喂食/洗澡/状态面板流程，但不写自己的状态缓存
    （cache_care_items 会把好友的体力/库存写进当前账号缓存，污染 GUI 状态条）。"""

    def cache_care_items(self, anchor: str, **status_fields) -> None:
        pass


class FriendCareScenario(VisitScenario):
    def __init__(self, dev=None):
        DeviceScenario.__init__(self, dev)  # 跳过 VisitScenario 的踩踩字段/日志
        fc = self.cfg.friend_care
        self.last_care_at: datetime | None = None  # 上次巡检完成时间（调度间隔用）
        log(f'好友护理: {"启用" if fc.enabled else "未启用"}，好友: {fc.friend_name or "未配置"}'
            f'，方式: {fc.method}，时间段: {fc.time_range}，调度间隔: {fc.interval_seconds}秒')

    # ---- 好友导航 ----

    def switch_to_friend(self, name: str, max_switches: int = MAX_FRIEND_SWITCHES) -> bool:
        """在好友列表里找到名称含 name 的好友并点击切换，返回是否成功。

        列表滚动加载（控件树里只有当前可见项）：可见项没有目标时按 visit 的
        累积名单逻辑切下一个好友加载更多，直到找到或没有更多好友。
        """
        for _ in range(max_switches):
            visible = self._friend_items()
            if not visible:
                # 点访问后 visit 消失不代表好友家已渲染完（模拟器上加载慢），
                # 列表还是空的就等一轮再抓，避免误判"好友不存在"
                log('好友列表还没加载出来，等待重试')
                time.sleep(CLICK_INTERVAL)
                continue
            new = [desc for desc, _, _ in visible if desc and desc not in self._friends]
            for desc in new:
                self._friends.append(desc)
            for desc, x, y in visible:
                if name in desc:
                    log(f'切换到好友 {desc} ({x}, {y})')
                    self.click(x, y)
                    time.sleep(CLICK_INTERVAL)
                    self._friend_index = self._friends.index(desc)
                    return True
            if not self.next_friend():
                return False
        log(f'切换 {max_switches} 次仍未找到好友 {name}')
        return False

    def read_friend_pet_name(self, attempts: int = 2) -> str | None:
        """读当前好友宠物页顶部的宠物名（大字第二行；上一行是主人昵称）。

        与 pk.py 同款区域，识别失败返回 None（该好友按无宠物名处理）。
        """
        for i in range(attempts):
            screen = self.screen()
            h, w = screen.shape[:2]
            x1, y1, x2, y2 = FRIEND_NAME_REGION
            x1, x2 = int(x1 * w / 1080), int(x2 * w / 1080)
            y1, y2 = int(y1 * h / 2412), int(y2 * h / 2412)
            cands = [(y, x, t.strip()) for t, x, y, s in ocr_texts(screen[y1:y2, x1:x2])
                     if y >= FRIEND_NAME_MIN_Y and len(t.strip()) >= 2
                     and not t.strip().isdigit()]
            if cands:
                cands.sort()
                return cands[-1][2]
            if i < attempts - 1:
                time.sleep(0.5)
        return None

    def goto_friend_home(self, name: str) -> None:
        """好友面板 -> 访问 -> 依次切换好友列表，直到进入目标好友家。

        匹配范围：列表里的主人昵称（content-desc "好友 xxx"）或好友页顶部的
        宠物名（pk.py 同款区域识别）——配置宠物名（如"张二狗"）也能命中。
        """
        def norm(s: str) -> str:
            return re.sub(r'\s+', '', s or '')

        target = norm(name)
        self._friends = []        # 累积好友名单（只增不减），见 visit.py
        self._friend_index = 0    # 访问进入时默认第一个好友
        self.goto_first_friend()
        self._accumulate_friends()
        for _ in range(MAX_FRIEND_SWITCHES + 20):
            desc = (self._friends[self._friend_index]
                    if self._friend_index < len(self._friends) else '')
            if target and target in norm(desc):
                log(f'命中主人昵称: {desc}')
                return
            try:
                pet = self.read_friend_pet_name()
            except Exception:
                pet = None
            if pet and target in norm(pet):
                log(f'命中宠物名: {pet}（主人 {desc}）')
                return
            if not self.next_friend():
                break
        raise RuntimeError(f'好友列表中未找到好友/宠物: {name}')

    # ---- 护理 ----

    def care_friend(self, times: int = 0) -> bool:
        """按护理好友方式护理当前好友家的宠物一次，返回是否执行了护理动作。

        times: 冗余字段"护理次数"（预留给后续按次数限制/计数改造，当前不使用）。
        ocr检测：展开好友状态面板读体力/清洁，不足则喂食/洗澡到 FRIEND_CARE_TARGET；
        一键护理：好友页有一键护理按钮就点，没有视为状态正常跳过。
        """
        if self.method == '一键护理':
            hit = self.see('one_click_care')
            if not hit:
                log('未找到一键护理按钮（好友体力/清洁正常），跳过护理')
                return False
            log('好友护理：使用一键护理')
            self.click(hit[0], hit[1])
            # 支付确认弹窗可能比护理按钮点击晚一拍出现，短等几次再判断（同 care.py）
            for attempt in range(1, ONE_CLICK_PAY_RETRIES + 1):
                pay = self.see('one_click_pay')
                if pay:
                    log('检测到"支付并护理"，点击确认')
                    self.click(pay[0], pay[1])
                    time.sleep(CLICK_INTERVAL)
                    break
                if attempt < ONE_CLICK_PAY_RETRIES:
                    time.sleep(CLICK_INTERVAL)
            return True
        care = _FriendCare(self.dev)
        care.energy_threshold = FRIEND_CARE_TARGET
        care.clean_threshold = FRIEND_CARE_TARGET
        care.toggle_status()
        status = care.read_status_ready()
        source = self.dev.hierarchy()
        energy = status.get('体力')
        clean = status.get('清洁')
        log(f'好友状态: 体力={energy} 清洁={clean}（目标 {FRIEND_CARE_TARGET}）')
        cared = False
        if energy is not None and energy < FRIEND_CARE_TARGET:
            log(f'好友体力 {energy} < {FRIEND_CARE_TARGET}，喂食')
            care.feed(source)
            cared = True
            source = self.dev.hierarchy()
        if clean is not None and clean < FRIEND_CARE_TARGET:
            log(f'好友清洁 {clean} < {FRIEND_CARE_TARGET}，洗澡')
            care.shower(source)
            cared = True
            source = self.dev.hierarchy()
        if cared:
            source = care.exit_care_mode(source)
        care.toggle_status(source)
        log('好友状态检查完成，已收起宠物状态')
        return cared

    # ---- 入口 ----

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """回主页面 -> 进目标好友家 -> 按配置方式护理一次 -> 结束回主页面。

        每次调度只做一次护理巡检（调度间隔 friend_care.interval_seconds 由
        执行器的 friend_care_due() 控制），场景内不再等待/切换好友刷新状态。
        max_times / max_rounds 参数仅为与其他场景签名一致（预留）。
        返回 True 表示完成了一次巡检（无论是否执行护理动作）——返回 False 会被
        任务队列标记当天不可继续，"好友无需护理"也必须返回 True 以便间隔后复查。
        """
        fc = self.cfg.friend_care
        if not fc.enabled:
            log('好友护理未启用，跳过')
            return False
        name = fc.friend_name.strip()
        if not name:
            log('未配置护理好友名称，跳过好友护理')
            return False
        self.method = fc.method
        if self.method not in CARE_METHODS:
            raise ValueError(
                f'config.yaml 中 friend_care.method 配置无效: {self.method!r}，'
                f'可选: {"/".join(CARE_METHODS)}')
        start, end = parse_time_range(fc.time_range)
        if not in_time_range(datetime.now().time(), start, end):
            log(f'当前不在好友护理时间段 {fc.time_range} 内，跳过')
            return False
        log(f'好友护理开始: 好友={name}，方式={self.method}，时间段={fc.time_range}')
        # 好友家有概率卡顿（喂食/洗澡面板打不开、页面卡死）：失败后回主页面
        # 重新进指定好友家再护理一次；仍失败抛给调度器走恢复链路
        cared = False
        for attempt in range(1, FRIEND_CARE_RETRIES + 1):
            try:
                self.ensure_main_page()
                self.goto_friend_home(name)
                cared = self.care_friend()
                break
            except Exception as e:
                if attempt >= FRIEND_CARE_RETRIES:
                    raise
                log(f'好友护理第 {attempt} 次尝试失败: {e}，回主页面重新进好友家再试')
                time.sleep(CLICK_INTERVAL)
        log('好友护理巡检完成' + ('，本次执行了护理' if cared else '，本次无需护理'))
        self.close()  # 先点 back 收掉好友相关页面，再确认回主页面
        self.ensure_main_page()
        self.last_care_at = datetime.now()
        return True


if __name__ == '__main__':
    try:
        FriendCareScenario().run()
    except KeyboardInterrupt:
        log('手动停止')
