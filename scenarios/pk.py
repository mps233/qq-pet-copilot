"""PK 场景：访问好友宠物页发起 PK 对战。

流程（u2 控件定位，分辨率无关）：
1. 点击 好友（visit_friends）-> 点击 访问（visit）进入第一个好友宠物页
   （好友导航复用 visit.py：累积名单、按顺序切换）
2. 点击 PK（pk）-> 点击 开始（pk_start）
3. 点开始后 1 秒起整屏 OCR 判断"正在PK"：命中按原本 11 秒等 PK 结束，出现 分享（pk_end）
   即计一次，持久化到 runs/pk_progress.json；匹配不到"正在PK"立即按超时处理点 quit 换好友
4. 点击 再来一局（pk_again）-> 再点 开始；每个好友最多 PK 3 次
5. 切换下一个好友继续 PK，直到次数满或没有更多好友
6. 结束：点 back 回主页面

分轮与状态检查：每局消耗 体力/清洁 各 5 点；一次 run() 最多 PK 16 局
（配置次数 > 16 时本轮先做 16 局，剩余由执行器下一轮接着处理）。
开始前检查：体力/清洁 都 >= 本轮计划局数 x 5 才开跑，
不足则先喂食/洗澡补充到所需值（计划局数 x 5）。

目标过滤（可选，config.yaml 的 pk 段）：only_names / skip_names（玩家昵称或
宠物名，逗号分隔，部分匹配）与 max_level（等级过滤：0 = 不限；-1 = 只打等级比
自己低的；-2 = 只打比雇佣打手（helper_names 第一个）等级低的；正数 = 只打等级
≤ N 的好友；等级从主页/好友页的 ⭐ 徽章读取；max_level < 0 且整轮没打成任何一个
时，自动兜底放宽为任意等级再跑一轮）；进入每个好友后、点 PK 前判定，
不符条件直接切下一个。

打手管理（可选，config.yaml 的 pk 段 helper_names）：名单非空时，每次 run() 在
第一个打开的准备页上检查"保镖"行——已雇的不在名单里就点 ✕ 解雇；空位就点 ➕
打开"宠友列表"，雇名单里第一个可雇的宠物（宠物名/主人名匹配，按名单顺序优先）。

运行方式（同 visit.py）：run() 独立运行回主页面。

运行：python scenarios/pk.py            （Ctrl+C 停止）
      python scenarios/pk.py --times 5 （覆盖配置的每天 PK 次数，0 为不限）
"""

import json
import os
import re
import sys
import time

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ocr import find_text, ocr_fullscreen, ocr_rec_only, ocr_texts
from src.progress import (
    PK_PROGRESS_FILE,
    load_progress,
    log,
    log_history,
    save_progress,
)
from src.scenario import CLICK_INTERVAL, DeviceScenario, NAV_TIMEOUT
from scenarios.care import CareScenario
from scenarios.visit import FRIEND_ITEM_XPATH, VisitScenario

PK_PER_FRIEND = 3     # 每个好友可 PK 次数
PK_DURATION = 11.0    # 一局 PK 时长（秒）：确认"正在PK"后按此总时长等 PK 结束（保持原逻辑）
PK_START_CHECK_DELAY = 1.0  # 点开始后多久开始整屏 OCR 判断"正在PK"
PK_END_TIMEOUT = 11.0  # 等 PK 结果（分享按钮）的超时（秒），超时点 quit 换好友
PK_ENTER_TIMEOUT = 3.0  # 点 PK 后等开始按钮的短超时：上限只弹 toast 不跳页，不用长等
PK_ROUND_CAP = 16     # 一次 run() 最多 PK 局数（超出由执行器下一轮接着处理）
# 一次 run() 最多切换几个目标。PK 允许打非好友，而非好友是"系统推荐"（每天不同、
# 理论上可无限往下滑），故设硬上限保证一定能结束；正常好友数远小于此值。
PK_MAX_TARGETS = 40
PK_STAT_COST = 5      # 每局消耗体力/清洁
PK_TIMEOUT_STREAK_LIMIT = 2  # 连续几次"PK 结果超时"就临时推迟 PK 任务

# 好友宠物页顶栏"⭐等级"的 OCR 区域（x1, y1, x2, y2，1080x2412 参考分辨率，运行时按实际尺寸换算）
# 右边界收到 368：只覆盖 ⭐ 星徽章，避免切到右边战力数字（防把战力当等级读）
FRIEND_LEVEL_REGION = (190, 270, 368, 380)

# ⭐ 星徽章兜底识别区域（V 通道整行识别用）：星标主体
# 背景：检测模型对星徽章里细笔画数字（"11"）漏检/误检（糊成"中"）——裁这小块
# 转 HSV 亮度通道放大 3 倍后走整行识别，实测 6/11/12 全通
LEVEL_REC_REGION = (228, 283, 352, 352)

# 好友宠物页顶部"主人昵称 + 宠物名"两行大字区域；宠物名 = 区域里 y >= FRIEND_NAME_MIN_Y 的行
FRIEND_NAME_REGION = (150, 130, 700, 260)
FRIEND_NAME_MIN_Y = 55  # 区域内的局部 y 阈值（上一行是主人昵称，下一行才是宠物名）

# 找打手主页时最多切换的好友数（打手可能排在列表较后/轮播轮换，防死循环）
MAX_HELPER_SEARCH = 60

# PK 准备页"雇佣保镖代打"行的 ➕（打开宠友列表）位置（1080x2412 参考，运行时按实际尺寸换算）
HELPER_ADD_POINT = (296, 1672)
# 已雇保镖头像上的 ✕（取消雇佣）位置（1080x2412 参考）
HELPER_CANCEL_POINT = (335, 1595)
# 宠友列表右上角 ✕（关闭列表）位置（1080x2412 参考）
HELPER_CLOSE_POINT = (976, 736)
# 注：自己主页的 ⭐ 等级徽章与好友页同区域（FRIEND_LEVEL_REGION），不单独定义

PROGRESS_FILE = PK_PROGRESS_FILE
# 打手雇佣状态（谁在任、是不是兜底雇的）——兜底雇的人靠它跨 run 保持，不被当"不在名单"解雇
HELPER_STATE_FILE = PROGRESS_FILE.parent / 'pk_helper_state.json'


def read_level_from_image(screen) -> int | None:
    """从好友页截图读 ⭐ 等级（区域 OCR + 星徽章 V 通道兜底），失败返回 None。

    纯函数，便于离线回归。兜底原因：星徽章里的两位数细笔画（典型 "11"）检测
    模型读不出（会和星形轮廓糊成"中"字）——把星徽章裁出来转 HSV 亮度通道、
    放大 3 倍走整行识别即可稳定读出（"11/6/12" 实测全通）。
    """
    h, w = screen.shape[:2]
    x1, y1, x2, y2 = FRIEND_LEVEL_REGION
    x1, x2 = int(x1 * w / 1080), int(x2 * w / 1080)
    y1, y2 = int(y1 * h / 2412), int(y2 * h / 2412)
    nums = [(x, int(t.strip())) for t, x, _, _ in ocr_texts(screen[y1:y2, x1:x2])
            if t.strip().isdigit()]
    if nums:
        return min(nums, key=lambda m: m[0])[1]
    # 兜底：星徽章 V 通道整行识别
    try:
        x1, y1, x2, y2 = LEVEL_REC_REGION
        x1, x2 = int(x1 * w / 1080), int(x2 * w / 1080)
        y1, y2 = int(y1 * h / 2412), int(y2 * h / 2412)
        v = cv2.cvtColor(screen[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)[:, :, 2]
        v = cv2.resize(v, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        for t, _s in ocr_rec_only(cv2.cvtColor(v, cv2.COLOR_GRAY2BGR)):
            d = re.sub(r'[^0-9]', '', t)
            if d and len(d) <= 2:
                n = int(d)
                if 1 <= n <= 20:
                    return n
    except Exception:
        pass
    return None


def pick_fallback_row(items, w: int = 1080) -> dict | None:
    """从宠友列表 OCR 结果挑「战力最高的可雇行」（列表按战力降序 → 最靠上可雇行）。

    items = [(文字, x, y, 置信度)]。可雇 = 行右侧有金币按钮（纯数字文本，x>0.8w）
    且同行没有不可雇状态（不可雇佣/被雇佣中/已达上限）。
    返回 {'name', 'label', 'x', 'y'}（x/y=按钮点击点）；没有可雇的返回 None。
    """
    btns = sorted((y, x) for t, x, y, _ in items
                  if t.strip().isdigit() and x > int(w * 0.8) and 800 < y < 2080)
    bad = ('不可雇佣', '被雇佣中', '已达上限')
    skip = ('上次雇佣', '宠友列表', '被雇次数')
    for by, bx in btns:
        near = [(t2.strip(), ty) for t2, _, ty, _ in items if abs(ty - by) <= 120]
        if any(any(k in t2 for k in bad) for t2, _ in near):
            continue
        names = sorted((ty, t2) for t2, ty in near
                       if t2 and '主人' not in t2 and not t2.isdigit()
                       and '型' not in t2 and not any(k in t2 for k in skip))
        labels = [t2 for t2, ty in near if '型' in t2 and any(c.isdigit() for c in t2)]
        return {'name': names[0][1] if names else '',
                'label': labels[0] if labels else '', 'x': bx, 'y': by}
    return None


class PKDeferred(Exception):
    """PK 结果连续超时：临时推迟 PK 任务（调度器延后重试，不做重启恢复）。"""


class PKScenario(VisitScenario):
    def __init__(self, dev=None):
        DeviceScenario.__init__(self, dev)  # 跳过 VisitScenario 的踩踩字段/日志
        self.times_per_day = self.cfg.pk.times_per_day
        # PK 目标过滤（只打/跳过名单、等级上限）与打手名单；runner 热加载时重新同步
        self.only_names = str(getattr(self.cfg.pk, 'only_names', '') or '').strip()
        self.skip_names = str(getattr(self.cfg.pk, 'skip_names', '') or '').strip()
        self.max_level = int(getattr(self.cfg.pk, 'max_level', 0) or 0)
        self.helper_names = str(getattr(self.cfg.pk, 'helper_names', '') or '').strip()
        self.helper_fallback = bool(getattr(self.cfg.pk, 'helper_fallback', False))
        self._own_level = None        # 自己等级（max_level=-1 时从主页读取）
        self._helper_read_name = None  # 上次 read_helper_level 用的打手名（日志用）
        self._helper_level = None     # 打手等级（max_level=-2 时从打手主页读取）
        self._level_any = False       # 兜底轮：没有"比我低"的可打时放宽为任意等级
        self._helper_checked = False  # 打手管理每次 run 只做一次
        self.sync_filters()
        log(f'每天 PK 次数: {self.times_per_day if self.times_per_day else "不限"}'
            + (f'，只打: {self.only_names}' if self.only_names else '')
            + (f'，跳过: {self.skip_names}' if self.skip_names else '')
            + (f'，只打等级≤{self.max_level}' if self.max_level > 0
               else ('，只打等级比打手低' if self.max_level == -2
                     else ('，只打等级比自己低' if self.max_level < 0 else '')))
            + (f'，打手: {self.helper_names}'
               + ('（含兜底）' if self.helper_fallback else '')
               if self.helper_names else ''))

    def sync_filters(self) -> None:
        """把只打/跳过名单字符串解析为列表（初始化与配置热加载共用）。

        统一去掉所有空白（含全角空格），匹配用。
        """
        self._only_list = [x for x in (''.join(s.split()) for s in str(self.only_names or '').split(',')) if x]
        self._skip_list = [x for x in (''.join(s.split()) for s in str(self.skip_names or '').split(',')) if x]
        self._helper_list = [x for x in (''.join(s.split()) for s in str(self.helper_names or '').split(',')) if x]

    def read_friend_level(self, attempts: int = 3) -> int | None:
        """读当前好友宠物页的等级（顶栏 ⭐ 后数字），识别失败返回 None。

        页面切换后有加载延迟，首次没读到等 0.5 秒重试（最多 attempts 次）。
        识别细节见 read_level_from_image（含星徽章细笔画数字的兜底）。
        """
        for i in range(attempts):
            lv = read_level_from_image(self.screen())
            if lv is not None:
                return lv
            if i < attempts - 1:
                time.sleep(0.5)
        return None

    def read_friend_pet_name(self, attempts: int = 2) -> str | None:
        """读当前好友宠物页的宠物名（顶部大字第二行；上一行是主人昵称）。

        取区域里 y 靠下的那行、长度 >= 2 的文字；识别失败返回 None
        （过滤按只匹配主人昵称处理）。
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

    def read_own_level(self, attempts: int = 3) -> int | None:
        """读自己主页的 ⭐ 等级徽章数字（与好友页徽章同区域同读法），失败返回 None。

        注意：徽章 OCR 区域不能裁得太小（小图 RapidOCR 检测不到），
        直接复用 FRIEND_LEVEL_REGION 的尺寸读自己页面（两页布局一致）。
        """
        return self.read_friend_level(attempts)

    def read_helper_level(self, name: str | None = None) -> int | None:
        """读打手等级：默认取状态文件里记录的当前打手（可能是兜底雇的），
        其次 helper_names 第一个。进其主页读 ⭐ 徽章，失败返回 None。

        用于 max_level=-2（只打比打手低的）。按名字在好友列表里找其主页
        （主人昵称 content-desc 或顶部宠物名匹配）；找不到/读不到按 fail-open
        处理（本轮不做等级过滤）。结束时收起好友页面（后续流程照常从主页面开始）。
        """
        if not name:
            name = str(self._load_helper_state().get('name') or '')
        if not name:
            name = self._helper_list[0] if self._helper_list else ''
        if not name:
            return None
        self._helper_read_name = name
        try:
            self._friends = []
            self._friend_index = 0
            self.goto_first_friend()
            self._accumulate_friends()
            found = False
            for _ in range(MAX_HELPER_SEARCH):
                desc = (self._friends[self._friend_index]
                        if self._friend_index < len(self._friends) else '')
                if name in ''.join(desc.split()):
                    log(f'PK 打手: 已到 {desc} 主页（主人昵称命中）')
                    found = True
                    break
                pet = None
                try:
                    pet = self.read_friend_pet_name()
                except Exception:
                    pet = None
                if pet and name in ''.join(pet.split()):
                    log(f'PK 打手: 已到 {name} 主页（宠物名命中，主人 {desc}）')
                    found = True
                    break
                if not self.next_friend():
                    break
            if not found:
                log(f'PK 打手: 好友列表里没找到 {name}，打手等级读取失败')
                return None
            return self.read_friend_level(attempts=4)
        except Exception as e:
            log(f'PK 打手: 等级读取异常（跳过）: {e}')
            return None
        finally:
            try:
                self.close()
            except Exception:
                pass

    def _friend_allowed(self, desc: str) -> bool:
        """PK 目标过滤：只打/跳过名单（玩家昵称/宠物名部分匹配）+ 等级上限。

        返回 False 表示跳过该好友；等级读取失败按不过滤处理（fail-open）。
        """
        owner = desc.strip()
        if owner.startswith('好友'):
            owner = owner[len('好友'):].strip()
        no = ''.join(owner.split())
        pet = None
        ne = None
        if self._only_list or self._skip_list:
            pet = self.read_friend_pet_name()
            ne = ''.join((pet or '').split())
        if self._only_list:
            if not (any(s in no for s in self._only_list)
                    or (ne and any(s in ne for s in self._only_list))):
                log(f'PK: 跳过 {owner}（宠物: {pet or "读取失败"}，不在只打名单）')
                return False
        if self._skip_list:
            if any(s in no for s in self._skip_list) \
                    or (ne and any(s in ne for s in self._skip_list)):
                log(f'PK: 跳过 {owner}（宠物: {pet or "读取失败"}，命中跳过名单）')
                return False
        cap = self.max_level
        if cap < 0:
            if self._level_any:
                cap = 0  # 兜底轮：不限等级（仍受只打/跳过名单约束）
            elif cap == -2:
                # 只打比打手低；打手等级没读到 → 不过滤（fail-open）
                cap = (self._helper_level - 1) if self._helper_level else 0
            elif self._own_level is not None:
                cap = self._own_level - 1  # 只打等级比自己低的
        if cap > 0:
            lv = self.read_friend_level()
            if lv is None:
                if self.max_level < 0:
                    # "只打比我低/比打手低"模式读不到等级：保守跳过，留给兜底轮打
                    log(f'PK: 跳过 {owner}（等级读取失败，留给兜底轮）')
                    return False
                log(f'PK: {owner} 等级读取失败，按不过滤继续')
            elif lv > cap:
                log(f'PK: 跳过 {owner}（等级 {lv}'
                    + (f'，只打比打手低（<{self._helper_level}）' if self.max_level == -2
                       else f'，只打比我低（<{self._own_level}）' if self.max_level < 0
                       else f' > {cap}') + '）')
                return False
            else:
                log(f'PK: {owner} 等级 {lv} 符合条件，继续')
        return True

    # ---- 打手（保镖）管理 ----

    def _load_helper_state(self) -> dict:
        """读打手雇佣状态（runs/pk_helper_state.json）：{name, source, at}。"""
        try:
            with open(HELPER_STATE_FILE, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_helper_state(self, name: str, source: str) -> None:
        """记住当前打手——兜底雇的人靠它跨 run 保持，不被当"不在名单"解雇。"""
        try:
            HELPER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(HELPER_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump({'name': name, 'source': source,
                           'at': time.strftime('%Y-%m-%d %H:%M:%S')},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            log(f'PK 打手: 状态写入失败（忽略）: {e}')

    def _clear_helper_state(self) -> None:
        """清掉打手状态（解雇后调用）。"""
        try:
            HELPER_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def _helper_ensure(self) -> None:
        """在准备页管理打手（保镖）：名单内保持；名单外但为本工具兜底雇的保持；
        其余解雇后重雇（名单优先；名单都不可雇且开启兜底时取战力最高的可雇宠物）。

        只在名单非空时执行；任何失败只记日志，不影响 PK 主流程。
        """
        if not self._helper_list:
            return
        try:
            texts = [t for t, _, _, _ in ocr_fullscreen(self.screen())]
            joined = ' '.join(texts)
            for n in self._helper_list:
                if any(n in t for t in texts):
                    log(f'PK 打手: 当前保镖已在名单（{n}），保持')
                    self._save_helper_state(n, 'configured')
                    return
            st = self._load_helper_state()
            if st.get('name') and any(st['name'] in t for t in texts):
                log(f'PK 打手: 当前保镖 {st["name"]} 是上次兜底雇佣的，保持')
                self._save_helper_state(st['name'], str(st.get('source') or 'fallback'))
                return
            # 只有「剩余保护时间」能证明有人被雇（空位占位文案本身就含"代打"三个字，
            # 不能拿"代打"当判据——曾因此把空位误判成有保镖，跑去点解雇后中断，永远雇不上）
            if '剩余保护时间' in joined:
                log('PK 打手: 当前保镖不在名单，解雇')
                if not self._helper_cancel():
                    return
                self._clear_helper_state()
                time.sleep(1.0)
                texts = [t for t, _, _, _ in ocr_fullscreen(self.screen())]
                joined = ' '.join(texts)
            if '雇佣保镖' not in joined and '保镖' not in joined:
                log('PK 打手: 未识别到保镖行，跳过')
                return
            hired = self._helper_hire()
            if hired:
                src = 'configured' if hired in self._helper_list else 'fallback'
                self._save_helper_state(hired, src)
            self._ensure_prep_ready()
        except Exception as e:
            log(f'PK 打手: 管理异常（跳过）: {e}')

    def _helper_cancel(self) -> bool:
        """点保镖 ✕ → 确认解雇；成功返回 True。"""
        screen = self.screen()
        h, w = screen.shape[:2]
        x, y = HELPER_CANCEL_POINT
        self.click(int(x * w / 1080), int(y * h / 2412))
        time.sleep(1.2)
        joined = ' '.join(t for t, _, _, _ in ocr_fullscreen(self.screen()))
        if '取消雇佣' not in joined and '解雇' not in joined:
            log('PK 打手: 未出现取消雇佣弹框')
            return False
        res = ocr_fullscreen(self.screen())
        c = find_text(res, '确认解雇') or find_text(res, '解雇')
        if not c:
            log('PK 打手: 没找到"确认解雇"按钮，放弃解雇')
            return False
        self.click(c[0], c[1])
        time.sleep(1.5)
        log('PK 打手: 已解雇旧保镖')
        return True

    def _helper_hire(self) -> str | None:
        """点 ➕ 打开宠友列表雇人：名单里第一个可雇的优先；都不可雇且开了兜底时，
        雇列表里战力最高的可雇宠物（列表按战力降序 = 可雇行里最靠上的）。

        返回雇到的人名；没雇到返回 None。
        """
        screen = self.screen()
        h, w = screen.shape[:2]
        x, y = HELPER_ADD_POINT
        self.click(int(x * w / 1080), int(y * h / 2412))
        time.sleep(1.6)
        res = ocr_fullscreen(self.screen())
        if '宠友列表' not in ' '.join(t for t, _, _, _ in res):
            log('PK 打手: 宠友列表未打开')
            return None
        for name in self._helper_list:
            item = None
            for t, ix, iy, _ in res:
                if name in t and '主人' not in t:
                    item = (t, ix, iy)
                    break
            if not item:
                log(f'PK 打手: 列表里没有 {name}')
                continue
            _, nx, ny = item
            if any(('不可雇佣' in t or '被雇佣中' in t or '已达上限' in t)
                   and abs(iy2 - ny) <= 120 for t, _, iy2, _ in res):
                log(f'PK 打手: {name} 当前不可雇（不可雇佣/被雇佣中/已达上限）')
                continue
            btn = None
            for t, ix2, iy2, _ in res:
                if t.strip().isdigit() and ix2 > int(700 * w / 1080) and abs(iy2 - ny) <= 120:
                    btn = (ix2, iy2)
            if not btn:
                btn = (int(920 * w / 1080), ny)
            return name if self._helper_click_hire(btn, name) else None
        if self.helper_fallback:
            pick = pick_fallback_row(res, w)
            if pick:
                who = pick['name'] or '列表最上可雇行'
                tag = f'{who}（{pick["label"]}）' if pick['label'] else who
                log(f'PK 打手: 名单里的都不可雇，兜底雇战力最高的可雇宠物: {tag}')
                if self._helper_click_hire((pick['x'], pick['y']), who):
                    return pick['name']
                return None
        log('PK 打手: 名单里没有可雇的宠物')
        self._helper_close_picker()
        return None

    def _helper_click_hire(self, btn: tuple[int, int], name: str) -> bool:
        """点雇佣按钮 → 确认弹框 → 验证生效；成功返回 True。"""
        self.click(btn[0], btn[1])
        time.sleep(1.5)
        res2 = ocr_fullscreen(self.screen())
        j2 = ' '.join(t for t, _, _, _ in res2)
        if '宠友列表' in j2 and any(k in j2 for k in ('确认雇佣', '确认', '确定')):
            c = find_text(res2, '确认雇佣') or find_text(res2, '确认') or find_text(res2, '确定')
            if c:
                self.click(c[0], c[1])
                time.sleep(1.5)
        j3 = ' '.join(t for t, _, _, _ in ocr_fullscreen(self.screen()))
        if '剩余保护时间' in j3:
            log(f'PK 打手: 已雇佣 {name} ✅')
            return True
        if '雇佣保镖代打' not in j3 and name and name in j3:
            # 保镖已就位但佣金未付：真正扣费在首次点"支付350开始"（pk_start 已兼容该文案）
            log(f'PK 打手: 已雇佣 {name} ✅（佣金 350 首次开打时支付）')
            return True
        log(f'PK 打手: 点雇 {name} 后未确认成功')
        return False

    def _helper_close_picker(self) -> None:
        """关闭宠友列表回准备页（先试右上角 ✕，不行按 back 再重进准备页）。"""
        screen = self.screen()
        h, w = screen.shape[:2]
        x, y = HELPER_CLOSE_POINT
        self.click(int(x * w / 1080), int(y * h / 2412))
        time.sleep(1.0)
        if '宠友列表' in ' '.join(t for t, _, _, _ in ocr_fullscreen(self.screen())):
            self.go_back()
            time.sleep(0.8)

    def _ensure_prep_ready(self) -> bool:
        """确保回到能看到开始按钮的准备页；不在就重新点 PK 进。"""
        if self.see('pk_start'):
            return True
        hit = self.see('pk', source=self.dev.hierarchy())
        if hit:
            self.click(hit[0], hit[1])
            time.sleep(2.0)
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if self.see('pk_start'):
                return True
            time.sleep(CLICK_INTERVAL)
        log('PK 打手: 结束管理后未回到准备页')
        return False

    def _pk_block_reason(self, early_texts=None) -> str:
        """点 PK 没进准备页的原因：对方是保镖 / 今天已经PK过了 / 未识别。

        early_texts：点击后立即抓的 OCR 文本（toast 只显示 2~3 秒，大概率含原因）。
        """
        joined = ''.join(early_texts or [])
        if '保镖' not in joined and '明天再来' not in joined and 'PK过' not in joined:
            joined = ''.join(t for t, _, _, _ in ocr_fullscreen(self.screen()))
        if '保镖' in joined:
            return '对方是你的保镖，换人试试吧'
        if '明天再来' in joined or 'PK过' in joined:
            return '今天已经PK过了，明天再来'
        return '未弹出准备页（原因未识别，可能已达上限）'

    # ---- 各阶段 ----

    def wait_pk_end(self) -> bool:
        """等 PK 结果页（分享按钮）出现；超时返回 False（调用方点 quit 换好友）。"""
        deadline = time.monotonic() + PK_END_TIMEOUT
        while time.monotonic() < deadline:
            hit = self.see('pk_end', source=self.dev.hierarchy())
            if hit:
                return True
            time.sleep(CLICK_INTERVAL)
        return False

    def _handle_pk_timeout(self, reason: str) -> None:
        """PK 结果超时处理：点 quit 退出该局（不计数）换下一个好友。

        连续达到 PK_TIMEOUT_STREAK_LIMIT 次说明 PK 环境异常（网络/加载卡住），
        抛 PKDeferred 临时推迟整个 PK 任务（调度器延后重试，不做重启恢复）。
        """
        self._pk_timeout_streak += 1
        log(f'{reason}，点 quit 切换下一个好友'
            f'（连续第 {self._pk_timeout_streak}/{PK_TIMEOUT_STREAK_LIMIT} 次）')
        if self._pk_timeout_streak >= PK_TIMEOUT_STREAK_LIMIT:
            raise PKDeferred('PK 结果连续超时，临时推迟 PK 任务')
        quit_hit = self.see('quit')
        if quit_hit:
            self.click(quit_hit[0], quit_hit[1])
            time.sleep(CLICK_INTERVAL)

    def leave_result_page(self) -> None:
        """PK 结束页/准备页点 back 回好友宠物页。

        PK 准备页（开始按钮）和结果页（分享/再来一局）都没有好友列表，
        必须回到好友宠物页（底部好友横排出现）才能切下一个好友。
        """
        for _ in range(5):
            source = self.dev.hierarchy()
            if self.dev.find_xpath_all(FRIEND_ITEM_XPATH, source=source):
                return
            if not self.go_back(source=source):
                return
            time.sleep(CLICK_INTERVAL)

    def _wait_pk_start(self, timeout: float = PK_END_TIMEOUT) -> tuple[int, int, float] | None:
        """等开始按钮出现（再来一局/进 PK 页后有几秒加载延迟，只查一次会误判）。

        超时返回 None，由调用方区分"没跳页（好友已满）"和"跳页了但加载慢"。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            source = self.dev.hierarchy()
            # 点开始可能触发"体力/清洁不足"弹窗：识别 pk_start 的同时同帧检测，
            # 命中回主页面护理一次并抛 StatBlocked（调度器重试当前任务）
            self.handle_low_stat_dialog(source)
            hit = self.see('pk_start', None, source)
            if hit:
                return hit
            time.sleep(CLICK_INTERVAL)
        return None

    def pk_friend(self, max_times: int, today: str, done: int, history: dict) -> int:
        """对当前好友 PK 至多 PK_PER_FRIEND 局，返回新的当天次数。

        点 PK 后 3 秒内没出现开始按钮且 PK 按钮还在（没跳页，只是上限
        toast）= 该好友 PK 次数已达上限，直接返回换下一个好友；
        页面已跳转但开始按钮加载慢的，继续走长等待。
        """
        hit = None
        for attempt in range(1, 4):
            # 访问后好友页有几秒加载延迟，PK 按钮重试 3 次再下结论
            hit = self.see('pk', source=self.dev.hierarchy())
            if hit:
                break
            log(f'未找到 PK 按钮，等待重试 ({attempt}/3)')
            time.sleep(CLICK_INTERVAL)
        if not hit:
            raise RuntimeError('好友页未找到 PK 按钮')
        self.click(hit[0], hit[1])
        # 被拦截时只弹 toast 不跳页（还在好友宠物页），toast 只显示 2~3 秒：
        # 点击后先抓一帧 OCR 留底，供没进准备页时识别原因
        time.sleep(0.6)
        early_texts = [t for t, _, _, _ in ocr_fullscreen(self.screen())]
        start = self._wait_pk_start(PK_ENTER_TIMEOUT)
        if start is None and self.see('pk', source=self.dev.hierarchy()):
            # 不要点 back，页面导航统一交给外层 leave_result_page
            log(f'该好友当前无法 PK（{self._pk_block_reason(early_texts)}），切换下一个好友')
            return done
        if self._helper_list and not self._helper_checked:
            # 打手（保镖）管理：每次 run 只在第一个打开的准备页上做一次
            self._helper_checked = True
            self._helper_ensure()
        for i in range(1, PK_PER_FRIEND + 1):
            if max_times and done >= max_times:
                break
            hit = self._wait_pk_start()
            if not hit:
                raise RuntimeError('PK 页未找到开始按钮')
            self.click(hit[0], hit[1])
            log(f'第 {i}/{PK_PER_FRIEND} 局 PK 中...')
            # 点开始后 PK_START_CHECK_DELAY 秒起整屏 OCR 判断"正在PK"：命中才继续正常
            # 流程（按原本 PK_DURATION 秒等 PK 结束）；匹配不到说明没真正开打
            # （加载卡住/已提前结算），立即按超时处理点 quit 换好友，不等 11 秒
            time.sleep(PK_START_CHECK_DELAY)
            # 体力/清洁不足弹窗：点开始后同帧检测，命中回主页面护理一次并抛
            # StatBlocked（调度器重试当前任务），避免被 _handle_pk_timeout 误判超时
            self.handle_low_stat_dialog()
            if not self.see('pk_in', self.screen()):
                self._handle_pk_timeout('未匹配到"正在PK"')
                return done
            time.sleep(PK_DURATION - PK_START_CHECK_DELAY)
            if not self.wait_pk_end():
                # 超时没出结果：连续出现多次说明 PK 环境异常（网络/加载卡住），
                # 点 quit 退出该局（不计数）换下一个好友；连续达到上限则临时推迟整个 PK 任务
                self._handle_pk_timeout(f'{PK_END_TIMEOUT:.0f}s 未出 PK 结果')
                return done
            done += 1
            self._pk_timeout_streak = 0  # PK 成功，清零连续超时计数
            save_progress(PROGRESS_FILE, today, done, history)
            log(f'已完成 {done} 次 PK' + (f' / 目标 {max_times} 次' if max_times else ''))
            if i < PK_PER_FRIEND and (not max_times or done < max_times):
                self.click_until_gone_or_see('pk_again', 'pk_start', '再来一局')
        return done

    def _pk_all(self, max_times: int, today: str, done: int, history: dict) -> int:
        """从好友面板开始逐好友 PK，直到次数满或没有更多好友，返回新的当天次数。

        每个好友先过目标过滤（pk.only_names / pk.skip_names / pk.max_level），
        不符条件直接切下一个。
        """
        self._friends = []        # 累积好友名单（只增不减），见 visit.py
        self._friend_index = 0    # 访问进入时默认第一个好友
        self._pk_timeout_streak = 0  # 连续 PK 结果超时计数（本轮内连续，成功清零）
        self._helper_checked = False  # 打手管理本轮只做一次
        self.goto_first_friend()
        self._accumulate_friends()  # 记录第一个目标（当前停在名单第 0 个）
        # 第一个就是非好友/系统推荐也照打（PK 允许打非好友）；不再直接 return
        if self.is_non_friend_page():
            time.sleep(1.0)
            if self.is_non_friend_page():
                log('第一个是非好友/系统推荐（PK 允许打非好友，继续）')
        while not max_times or done < max_times:
            desc = self._friends[self._friend_index] if self._friend_index < len(self._friends) else ''
            if self._friend_allowed(desc):
                done = self.pk_friend(max_times, today, done, history)
            if max_times and done >= max_times:
                break
            self.leave_result_page()
            # allow_non_friend=True：非好友页也返回 True，下一轮循环照样对它开打
            # max_switches：非好友是每天不同的系统推荐、可无限下滑，设硬上限保证结束
            if not self.next_friend(allow_non_friend=True, max_switches=PK_MAX_TARGETS):
                log('没有更多目标了')
                break
        return done

    # ---- 入口 ----

    def _prepare_stats(self, planned: int) -> None:
        """PK 前检查体力/清洁：每局各消耗 PK_STAT_COST，
        不足 planned*5 则喂食/洗澡补充到所需值 planned*5（流程同 care.check_and_care）。
        护理方式为"一键护理"时不读状态：有一键护理按钮就点，然后直接开跑。
        """
        care = CareScenario(self.dev)
        if care.method == '一键护理':
            self.ensure_main_page()
            care.one_click_care()
            return
        need = planned * PK_STAT_COST
        source = self.ensure_main_page()
        care.toggle_status(source)
        # 数值异步加载（刚展开可能只有账号/宠物名），重试读到体力/清洁为止
        status = care.read_status_ready()
        source = self.dev.hierarchy()
        energy = status.get('体力')
        clean = status.get('清洁')
        log(f'PK 前状态: 体力={energy} 清洁={clean}，本轮计划 {planned} 局（各需 {need}）')
        cared = False
        if energy is not None and energy < need:
            log(f'体力 {energy} 不足 {need}，喂食到 {need}')
            care.energy_threshold = need
            care.feed(source)
            cared = True
            source = self.dev.hierarchy()
        if clean is not None and clean < need:
            log(f'清洁 {clean} 不足 {need}，洗澡到 {need}')
            care.clean_threshold = need
            care.shower(source)
            cared = True
            source = self.dev.hierarchy()
        if cared:
            source = care.exit_care_mode(source)
        care.toggle_status(source)
        log('PK 前状态检查完成，已收起宠物状态')

    @staticmethod
    def _round_limit(max_times: int, done: int) -> int:
        """本轮 PK 目标次数：一次最多 PK_ROUND_CAP 局。"""
        if max_times:
            return min(max_times, done + PK_ROUND_CAP)
        return done + PK_ROUND_CAP

    def run(self, max_times: int | None = None, max_rounds: int = 0) -> bool:
        """回主页面 -> 检查/补充体力清洁 -> 进好友面板逐好友 PK，结束点 back 回主页面。

        一次最多 PK_ROUND_CAP 局（配置次数更多时由执行器下一轮接着跑）。
        max_rounds 参数仅为与其他场景签名一致。
        返回本次是否 PK 了至少一局。
        """
        if max_times is None:
            max_times = self.times_per_day
        today, done, history = load_progress(PROGRESS_FILE)
        log_history(history, today)
        if max_times and done >= max_times:
            log(f'今天已 PK 满 {max_times} 次，无需再 PK')
            return False
        start_done = done
        round_target = self._round_limit(max_times, done)
        self.ensure_main_page()
        self._level_any = False
        self._helper_level = None
        if self.max_level == -2:
            # 只打比打手低：先读打手等级（fail-open：读不到本轮不过滤）
            self._helper_level = self.read_helper_level()
            if self._helper_level is not None:
                log(f'PK 打手: {self._helper_read_name} 等级 {self._helper_level}'
                    f'（只打等级低于 {self._helper_level} 的好友）')
            else:
                log('PK 打手等级读取失败：本轮不做等级过滤')
        elif self.max_level < 0:
            self._own_level = self.read_own_level()
            if self._own_level is not None:
                log(f'你的宠物等级: {self._own_level}（PK 只打等级低于 {self._own_level} 的好友）')
            else:
                log('你的宠物等级读取失败：本轮不做等级过滤')
        self._prepare_stats(round_target - done)
        done = self._pk_all(round_target, today, done, history)
        if done == start_done and (not max_times or done < max_times):
            if self.max_level == -1:
                # 兜底：整轮没打成任何一个"等级比我低"的（没有这种好友/都被挡），
                # 放宽为打任意等级再跑一轮——避免白白不 PK；仍受只打/跳过名单约束
                log('PK 兜底: 没有等级比我低的可打，本轮放宽为打任意等级')
                self._level_any = True
                self.close()
                self.ensure_main_page()
                done = self._pk_all(round_target, today, done, history)
            elif self.max_level == -2:
                # 比打手低的模式不兜底：打高等级=大概率输还费体力/清洁，宁可本轮不打
                log('PK: 没有等级比打手低的可打，本轮不打（等好友列表轮换后再试）')
        self.close()  # 点 back 收掉好友相关页面，再确认回主页面
        self.ensure_main_page()
        return done > start_done

if __name__ == '__main__':
    import argparse

    ap = argparse.ArgumentParser(description='PK 场景')
    ap.add_argument('--times', type=int, default=None,
                    help='当天 PK 次数上限，0 为不限；不指定则读 config.yaml 的 pk.times_per_day')
    args = ap.parse_args()

    try:
        PKScenario().run(max_times=args.times)
    except KeyboardInterrupt:
        log('手动停止')
