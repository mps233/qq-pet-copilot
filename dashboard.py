#!/usr/bin/env python3
"""QQ 宠物托管 · 手机仪表盘（纯标准库，零依赖）。

读取 runs/ 下的日志、进度、状态、队列文件，提供手机端页面：

    GET /             手机页面（自动刷新）
    GET /api/data     汇总数据 JSON
    GET /api/adventure 冒险记录 JSON（统一数据源，实时曲线）
    GET /api/logs     实时日志尾部 JSON（?tail=250）
    GET /files/<png>  异常截图
    POST /api/runner/start  启动调度器（独立后台进程）
    POST /api/runner/stop   停止调度器（SIGINT 优雅收尾退出）

启动:  python3 dashboard.py [--port 8787]
访问:  http://<本机内网IP>:8787
"""

import errno
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
RUNS = BASE / 'runs'
LOGS = RUNS / 'logs'
ADV_LIVE_FILE = RUNS / 'adventure_live.jsonl'   # 冒险统一记录（实验 300 把 + 日常实时）
DEFAULT_PORT = 8787

# ---- PWA：应用清单 + Service Worker（离线兜底；SW 需 https/localhost 才注册）----
MANIFEST_JSON = json.dumps({
    'name': 'QQ宠物托管', 'short_name': 'QQ宠物',
    'start_url': '/', 'scope': '/', 'display': 'standalone',
    # 暖色房间底（与 CSS --bg / 背景图顶部一致）。历史值是冷灰 #f6f7f9，
    # 是暖色改版前留下的，会让启动闪屏/状态栏与页面割裂。
    'background_color': '#FCF7ED', 'theme_color': '#D5A758',
    'icons': [
        {'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
        {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png',
         'purpose': 'any maskable'},
    ],
}, ensure_ascii=False)

SW_JS = """const CACHE = 'qpet-shell-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request).then(r => {
      const c = r.clone();
      caches.open(CACHE).then(ca => ca.put(e.request, c)).catch(() => {});
      return r;
    }).catch(() => caches.match(e.request))
  );
});"""


# ---------------------------------------------------------------- 数据读取

def list_friends() -> list[str]:
    """读调度器写的好友名单缓存（runs/friends_cache.json）里的名字列表。

    缓存由 scenarios/visit.py 扫好友时写入，数据源是好友列表控件的
    content-desc（"好友 墨瞳"）—— 即**主人昵称**，非宠物名。
    """
    try:
        data = json.loads((RUNS / 'friends_cache.json').read_text('utf-8'))
        names = data.get('names') or []
        return [str(n) for n in names if str(n).strip()]
    except (OSError, ValueError):
        return []


def friends_meta() -> dict:
    """好友缓存的元信息（来源/更新时间/说明），供前端展示。"""
    try:
        data = json.loads((RUNS / 'friends_cache.json').read_text('utf-8'))
        return {'source': data.get('source') or '', 'note': data.get('note') or '',
                'updated': data.get('updated') or ''}
    except (OSError, ValueError):
        return {'source': '', 'note': '', 'updated': ''}


def read_json(name: str) -> dict:
    try:
        return json.loads((RUNS / name).read_text('utf-8'))
    except Exception:
        return {}


_SECRET_KEY_RE = re.compile(
    r'(token|secret|password|passwd|pwd|api_?key|webhook|chat_id|send_?key|bark_?key)',
    re.IGNORECASE,
)
_SECRET_VAL_RE = re.compile(r'(\d{6,}:[A-Za-z0-9_-]{20,})')   # Telegram bot token 形态


def _redact(msg: str) -> str:
    """抹掉审计日志里的凭据（token/webhook/chat_id 等）。

    设置保存会把提交内容整条记进 runs/logs/dashboard.log，凭据因此长期明文落盘
    （曾把 Telegram bot token 完整写进日志）。token 等同渠道控制权，必须脱敏。
    """
    # 形态匹配：Telegram token 这类"数字:长串"无论键名如何都抹掉
    msg = _SECRET_VAL_RE.sub(lambda m: m.group(1)[:4] + '***已隐藏***', msg)
    # 键值匹配：'key': 'value' / key=value / "key": "value"
    def _sub_kv(m):
        return f'{m.group(1)}{m.group(2)}{m.group(3)}***已隐藏***{m.group(4)}'
    return re.sub(
        r"(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(\s*[:=]\s*)(['\"])([^'\"]*)\4",
        lambda m: _sub_kv(m) if _SECRET_KEY_RE.search(m.group(2)) else m.group(0),
        msg,
    )


def audit(msg: str) -> None:
    """关键操作审计日志（谁什么时候改了什么），写 runs/logs/dashboard.log（凭据自动脱敏）。"""
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / 'dashboard.log', 'a', encoding='utf-8') as f:
            f.write(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {_redact(str(msg))}\n')
    except Exception:
        pass


def scheduler_info() -> dict:
    # 走 _runner_pids 的严格复核——裸 pgrep 会误配带 --test 参数的短暂进程，
    # 曾导致 UI 误显示"运行中 PID xxxxx"（踩过坑），此函数已统一改走复核路径
    pids = _runner_pids()
    uptime = ''
    if pids:
        try:
            uptime = subprocess.run(['ps', '-o', 'etime=', '-p', str(pids[0])],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            pass
    return {'alive': bool(pids), 'pid': pids[0] if pids else None, 'uptime': uptime}


def _runner_pids() -> list[int]:
    """调度器进程 PID 列表：pgrep 匹配 + 命令行复核（防 grep/pgrep 自身误配）。"""
    try:
        out = subprocess.run(['pgrep', '-f', 'scenarios/runner.py'],
                             capture_output=True, text=True, timeout=5).stdout
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []
    alive = []
    for pid in pids:
        try:
            cmd = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            cmd = ''
        if 'runner.py' in cmd and 'python' in cmd.lower() and '--test' not in cmd:
            alive.append(pid)
    return alive


def scheduler_start() -> dict:
    """从仪表盘启动调度器：独立会话进程，输出追加到 runs/runner_console.log。

    用项目 venv 的 python（保证 uiautomator2/rapidocr 等依赖可用），
    已在运行时不重复启动（防多实例——曾踩过双开尾巴的坑）。
    """
    info = scheduler_info()
    if info['alive']:
        return {'ok': False, 'msg': f"调度器已在运行（PID {info['pid']}），不重复启动"}
    venv_py = BASE / '.venv' / 'bin' / 'python'
    python = str(venv_py) if venv_py.exists() else sys.executable
    try:
        RUNS.mkdir(exist_ok=True)
        logf = open(RUNS / 'runner_console.log', 'ab')
    except Exception as e:
        return {'ok': False, 'msg': f'无法打开日志文件: {e}'}
    logf.write(f'\n===== {datetime.now():%Y-%m-%d %H:%M:%S} '
               f'由仪表盘启动调度器 =====\n'.encode('utf-8'))
    logf.flush()
    try:
        subprocess.Popen([python, str(BASE / 'scenarios' / 'runner.py')],
                         cwd=str(BASE), stdin=subprocess.DEVNULL,
                         stdout=logf, stderr=subprocess.STDOUT,
                         start_new_session=True)
    except Exception as e:
        logf.close()
        return {'ok': False, 'msg': f'启动失败: {e}'}
    logf.close()   # 父进程关句柄；子进程持有自己的副本继续写
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(0.5)
        info = scheduler_info()
        if info['alive']:
            audit('调度器启动（来自仪表盘）')
            return {'ok': True, 'msg': f"已启动（PID {info['pid']}）", 'scheduler': info}
    return {'ok': False, 'msg': '启动后 8 秒内未检测到进程，请看 runs/runner_console.log'}


def scheduler_stop() -> dict:
    """停止调度器：先 SIGINT 优雅退出（任务收尾），12 秒不退再 SIGTERM/SIGKILL 兜底。"""
    pids = _runner_pids()
    if not pids:
        return {'ok': True, 'msg': '调度器本来就没有在运行', 'scheduler': scheduler_info()}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGINT)
        except Exception:
            pass
    deadline = time.time() + 12
    while time.time() < deadline and _runner_pids():
        time.sleep(0.5)
    left = _runner_pids()
    forced = False
    if left:
        for pid in left:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        deadline = time.time() + 4
        while time.time() < deadline and _runner_pids():
            time.sleep(0.4)
        left = _runner_pids()
    if left:
        forced = True
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        time.sleep(1.0)
        left = _runner_pids()
    audit(f"调度器停止（来自仪表盘）: {pids} → 剩余 {left or '无'}"
          f"{'（含强制）' if forced else ''}")
    if left:
        return {'ok': False, 'msg': f'仍有进程未退出：{left}', 'scheduler': scheduler_info()}
    return {'ok': True,
            'msg': f"已停止（PID {'、'.join(map(str, pids))}）" + ('，有进程被强制结束' if forced else ''),
            'scheduler': scheduler_info()}


def today_log_path():
    p = LOGS / (datetime.now().strftime('%Y-%m-%d') + '.log')
    if p.is_file():
        return p
    files = sorted(LOGS.glob('*.log'))
    return files[-1] if files else None


def cross_day_log_lines(lines: list[str], path) -> list[str]:
    """跨天活动的日志兜底：今天的日志里没有"进行中"登记时，接上前一天的尾部。

    日志按天分文件（`runs/logs/YYYY-MM-DD.log`），而打工/上课常跨零点
    （如 23:39 登记"检测到正在打工，预计…（00:24:37 收尾）"）——跨天后那条记录
    就不在今天文件里了，`work_eta()` 找不到 → 界面误显示"等待中"
    （用户实报"明明还在打工呢"）。只给 work_eta 用，不影响 last_line / 今日时长。
    """
    if path is None or any(re.search(r'进行中，预计|检测到正在', ln) for ln in lines):
        return lines
    prev = path.parent / (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d.log')
    return (tail_lines(prev, 400) + lines) if prev.is_file() else lines


def tail_lines(path, n: int = 250) -> list[str]:
    """高效读取文件尾部（最多回读 400KB）。"""
    size = path.stat().st_size
    chunk = min(size, 400_000)
    with open(path, 'rb') as f:
        f.seek(size - chunk)
        data = f.read().decode('utf-8', 'replace')
    lines = data.splitlines()
    if chunk < size and lines:
        lines = lines[1:]
    return lines[-n:]


def work_eta(lines: list[str]):
    """从日志里找最后一次"进行中"登记，算出剩余秒数 + 场景名（kind）。

    **两种日志格式都要认**（都代表"已登记 pending、先调度其他任务"，只是来源不同）：
      ① `冒险: 进行中，预计 44 秒后结束（…收尾）` —— scenario.defer_busy_end（场景主动延时收尾）
      ② `检测到正在打工，预计 2681 秒后结束（…收尾）` —— scenario.detect_busy_remaining
        （出门预检/被雇佣召回等路径）
    早期只匹配 ①，导致手动切到打工后界面仍停在更早那条"冒险: 进行中"上——
    而那条的结束时间早过了，于是显示"冒险中 / 收尾中"（用户实报的 bug）。
    """
    m = None
    for ln in lines[-400:]:
        mm = re.search(r'(?:([^\[\]:：]{1,8})[:：]\s*进行中|检测到正在([^，,]{1,10}))'
                       r'，预计 (\d+) 秒后结束'
                       r'（(\d{2}):(\d{2}):(\d{2}) 收尾）', ln)
        if mm:
            m = mm
    if not m:
        return None
    kind = (m.group(1) or m.group(2)).strip()
    hh, mi, ss = m.group(4), m.group(5), m.group(6)
    now = datetime.now()
    target = now.replace(hour=int(hh), minute=int(mi), second=int(ss), microsecond=0)
    # 跨天兜底：日志里的收尾时间可能是**次日凌晨**（如 23:39 记录、00:24 收尾），
    # 而 replace 只能拼出"今天 00:24"（已过去近 24 小时），会被下面的 rem<-600 误判成
    # 过期而返回 None。超过 12 小时就当成明天同一时刻。
    if target < now - timedelta(hours=12):
        target += timedelta(days=1)
    rem = int((target - now).total_seconds())
    if rem < -600:
        return None
    return {'eta_clock': f'{hh}:{mi}:{ss}',
            'remaining': max(0, rem),
            'kind': kind}


# 游戏机制：学习+打工合计时长的收益效率档（与 scenarios/runner.py 的
# efficiency_tiers 保持一致；门槛可用 schedule.efficiency_tier*_hours 覆盖）。
# dashboard 是独立进程、不导入 runner，故此处自带一份默认值。
DEFAULT_EFFICIENCY_TIERS = ((12 * 3600, 10), (8 * 3600, 25))


def efficiency_tiers_of(sched: dict):
    """按 config 的 schedule 段生成 (秒门槛, 效率%) 序列；缺失时用默认值。"""
    t1 = sched.get('efficiency_tier1_hours')
    t2 = sched.get('efficiency_tier2_hours')
    if t1 is None and t2 is None:
        return DEFAULT_EFFICIENCY_TIERS
    try:
        t1 = 8 if t1 is None else int(t1)
        t2 = 12 if t2 is None else int(t2)
    except (TypeError, ValueError):
        return DEFAULT_EFFICIENCY_TIERS
    tiers = []
    if t2 > 0:
        tiers.append((t2 * 3600, 10))
    if t1 > 0:
        tiers.append((t1 * 3600, 25))
    return tuple(tiers) or DEFAULT_EFFICIENCY_TIERS


def today_duration(lines: list[str], tiers=None):
    """最后一条“今日时长: 已学习 X 分钟 + 已打工 Y 分钟”的 X/Y + 效率档。

    效率档（游戏机制，与 scenarios/runner.py 的 efficiency_tiers 一致）：
    学习+打工合计 >= tier2 小时 10%、>= tier1 小时 25%、否则 100%。
    门槛可配（schedule.efficiency_tier*_hours），tiers 传入 (秒门槛, 效率%) 序列。
    额外返回距下一档还有多少分钟，供界面提示"还能跑多久降档"。"""
    m = None
    for ln in lines[-400:]:
        mm = re.search(r'今日时长: 已学习 (\d+) 分钟 \+ 已打工 (\d+) 分钟', ln)
        if mm:
            m = mm
    if not m:
        return None
    learn_min, work_min = int(m.group(1)), int(m.group(2))
    total_min = learn_min + work_min
    total_s = total_min * 60
    if tiers is None:
        tiers = DEFAULT_EFFICIENCY_TIERS
    eff = 100
    for threshold_s, pct in tiers:
        if total_s >= threshold_s:
            eff = pct
            break
    nxt = None
    for ts, p in tiers:
        if total_s < ts and (nxt is None or ts < nxt[0]):
            nxt = (ts, p)
    return {
        'learn_min': learn_min, 'work_min': work_min, 'eff_pct': eff,
        'total_min': total_min,
        'next_pct': nxt[1] if nxt else None,
        'next_in_min': (nxt[0] - total_s) // 60 if nxt else None,
    }


def _task_enabled_map(tasks: dict, friend_care: dict, gift_bag: dict,
                      hire_friend: dict) -> dict:
    """各任务的"配置启用状态"，等价于 runner 写队列快照时的 disabled 判定：
    任务级 tasks.<key>.enabled + 场景级开关（好友护理/福袋/雇佣好友还有额外条件：
    好友护理需 friend_name、雇佣好友需 times_per_day+friend_name、福袋看场景 enabled）。
    供调度器停止时队列卡显示——避免用旧快照里的 disabled 状态误导。"""
    out = {}
    for k, v in (tasks or {}).items():
        if not isinstance(v, dict) or 'enabled' not in v:
            continue
        ok = bool(v.get('enabled', True))
        if k == 'friend_care':
            ok = ok and bool(friend_care.get('enabled', False)) \
                 and bool(str(friend_care.get('friend_name') or '').strip())
        elif k == 'gift_bag':
            ok = ok and bool(gift_bag.get('enabled', True))
        elif k == 'hire_friend':
            ok = (ok and bool(hire_friend.get('enabled', False))
                  and bool(int(hire_friend.get('times_per_day') or 0))
                  and bool(str(hire_friend.get('friend_name') or '').strip()))
        out[k] = ok
    return out


def config_summary() -> dict:
    try:
        import yaml  # venv 里有；缺失时返回空
        cfg = yaml.safe_load((BASE / 'config.yaml').read_text('utf-8')) or {}
    except Exception:
        return {}
    tasks = cfg.get('tasks') or {}
    sched = cfg.get('schedule') or {}
    work = cfg.get('work') or {}
    adv = cfg.get('adventure') or {}
    visit = cfg.get('visit') or {}
    pk = cfg.get('pk') or {}
    care = cfg.get('care') or {}
    friend_care = cfg.get('friend_care') or {}
    gift_bag = cfg.get('gift_bag') or {}
    hire_friend = cfg.get('hire_friend') or {}
    recover = cfg.get('recover') or {}
    adb = cfg.get('adb') or {}
    control = cfg.get('control') or {}
    notify = cfg.get('notify') or {}
    runner = cfg.get('runner') or {}

    # 通知渠道摘要（设置页"通知"行）：列出真正已启用的渠道 + 事件开关状态
    def _notify_channels_label(nt: dict) -> str:
        chans = []
        if nt.get('feishu_enabled') and str(nt.get('feishu_webhook') or '').strip():
            chans.append('飞书')
        if nt.get('telegram_enabled') and str(nt.get('telegram_token') or '').strip():
            chans.append('Telegram')
        if nt.get('win_toast', True):
            chans.append('桌面通知')
        if str(nt.get('onepush_config') or '').strip():
            chans.append('OnePush')
        head = ' + '.join(chans) if chans else '未启用任何渠道'
        kinds = []
        if nt.get('quota_done', True) and nt.get('event_notify', True):
            kinds.append('配额达成')
        if nt.get('career_notify', True):
            kinds.append('职业解锁')
        return head + ('（推送：' + '、'.join(kinds) + '）' if kinds else '（不推送事件）')
    school_enabled = bool((tasks.get('school') or {}).get('enabled', True))

    def n_per_day(n):
        return '不限次' if not n else f'{n} 次/天'

    # 今日策略：按**配额**判断，而不是只看 school_enabled——配额才是当天实际安排
    # （work_quota=0 就是"今天不打工"，此时不该再写"学习 + 打工"）
    sq = sched.get('study_quota_hours', 0) or 0
    wq = sched.get('work_quota_hours', 0) or 0
    work_on = bool((tasks.get('work') or {}).get('enabled', True))
    if wq <= 0 or not work_on:
        strategy = '只学习' if (sq > 0 and school_enabled) else '不学习不打工'
    elif sq <= 0 or not school_enabled:
        strategy = '只打工'
    else:
        strategy = f'学习 {sq}h + 打工 {wq}h'

    rows = [
        ['调度策略', strategy],
        ['调度引擎', str(runner.get('engine', 'task_queue'))],
        ['打工', f"{work.get('location', '')} · {work.get('duration', '')} · {n_per_day(work.get('times_per_day'))}"],
        ['金币阈值', f"{sched.get('coin_threshold', '-')}（低于优先打工）"],
        ['时长上限', f"{sched.get('daily_hour_limit', '-')} 小时/天"],
        ['打工停止', f"{sched.get('work_stop_hours', '-')} 小时/天（避 10% 效率档）"],
        ['福袋', f"{'启用' if (cfg.get('gift_bag') or {}).get('enabled', True) else '未启用'}"
                 f" · 每 {(cfg.get('gift_bag') or {}).get('interval_seconds', '-')} 秒扫描"],
        ['踩踩', f"{visit.get('times_per_day', '-')} 次/天 @ {visit.get('start_time', '')}"],
        ['PK', f"{pk.get('times_per_day', '-')} 次/天 @ {pk.get('start_time', '')}"],
        ['冒险', f"{adv.get('times_per_day', '-')} 次/天 @ {adv.get('start_time', '')}"],
        ['护理', f"{care.get('method', '')} · 阈值 {care.get('energy_threshold', '-')}/{care.get('clean_threshold', '-')}"],
        ['异常恢复', str(recover.get('method', ''))],
        ['设备', f"{adb.get('device_serial') or '自动'} · {control.get('method', '')}"],
        ['通知', _notify_channels_label(notify)],
    ]
    return {
        'strategy': strategy,
        'school_enabled': school_enabled,
        'work_enabled': work_on,
        'work_location': work.get('location'),
        'work_duration': work.get('duration'),
        'coin_threshold': sched.get('coin_threshold'),
        # 今日配额（小时）：首页「今日学习/打工」瓦片按配额显示进度（如 8.2/12h）
        'study_quota_hours': sched.get('study_quota_hours', 0),
        'work_quota_hours': sched.get('work_quota_hours', 0),
        'visit_per_day': visit.get('times_per_day'),
        'pk_per_day': pk.get('times_per_day'),
        'adventure_times': adv.get('times_per_day'),
        'adventure_start': adv.get('start_time'),
        'task_order': [x.strip() for x in str(tasks.get('order') or '').split('>') if x.strip()],
        'tasks_enabled': _task_enabled_map(tasks, friend_care, gift_bag, hire_friend),
        'rows': rows,
    }


def load_progress() -> dict:
    """今日进度：按进度文件里的 date 过滤——不是今天的（调度器停跑时残留的昨天数据）
    按 0 / 未完成显示，避免把昨天的次数当成今天（用户曾问"今日PK为什么显示 15/15 明明一次没打"）。"""
    today = datetime.now().strftime('%Y-%m-%d')

    def today_of(name: str, zero: dict) -> dict:
        d = read_json(name)
        return d if d.get('date') == today else zero

    return {
        'visit': today_of('visit_progress.json', {'learned': 0}),
        'pk': today_of('pk_progress.json', {'learned': 0}),
        'work': today_of('work_progress.json', {'learned': 0}),
        'school': today_of('school_progress.json', {'learned': 0, 'study_secs': 0}),
        'adventure': today_of('adventure_progress.json', {'learned': 0}),
        'exp_daily': today_of('exp_daily_progress.json', {'done': False}),
    }


def adventure_data(sel_date: str | None = None) -> dict:
    """冒险记录（统一数据源：runs/adventure_live.jsonl——实验期 300 把 + 日常实时，
    由 src/scenario.record_adventure_live 在每把结算时记录）。

    sel_date: 统计范围——'YYYY-MM-DD' 看指定某天，'all' 看全部历史，
    空/'today' = 当天（当天还没有记录时回落到最近有记录的一天）。
    每条记录归属哪天优先取结算页内容里的日期（跨零点补录归前一天，
    如 00:00 才检测到的"昨天 23:59"结算），识别不到再用记录时间 ts。
    """
    rows = []
    try:
        for _line in ADV_LIVE_FILE.read_text('utf-8').splitlines()[-1500:]:
            try:
                rows.append(json.loads(_line))
            except Exception:
                pass
    except Exception:
        pass
    if not rows:
        return {'ok': False}
    rows.sort(key=lambda d: str(d.get('ts') or ''))
    today = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    for r in rows:
        day = ''
        for t in (r.get('settle') or []):
            m = re.search(r'(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})', str(t))
            if m:
                day = f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
                break
        r['_day'] = day or str(r.get('ts') or '')[:10] or today
    date_n: dict = {}
    for r in rows:
        date_n[r['_day']] = date_n.get(r['_day'], 0) + 1
    dates = sorted(date_n, reverse=True)
    if not sel_date or sel_date == 'today':
        sel = today if today in date_n else (dates[0] if dates else today)
    elif sel_date == 'all':
        sel = 'all'
    elif sel_date in date_n:
        sel = sel_date
    else:
        sel = today
    view = rows if sel == 'all' else [r for r in rows if r['_day'] == sel]
    coins = [int(r.get('coins') or 0) for r in view]
    n = len(coins)
    net = sum(coins)
    win = sum(1 for c in coins if c > 0)
    zero = sum(1 for c in coins if c == 0)
    dist = {}
    for c in coins:
        dist[str(c)] = dist.get(str(c), 0) + 1
    cum, s = [], 0
    for i, c in enumerate(coins, 1):
        s += c
        cum.append([i, s])
    pts = [[i, c] for i, c in enumerate(coins, 1)]
    tn = tnet = 0
    for r in rows:
        if r['_day'] == today:
            tn += 1
            tnet += int(r.get('coins') or 0)
    gains = {}
    recent = []
    for i, r in enumerate(view, 1):
        toks = [str(t).strip() for t in (r.get('settle') or [])]
        gs = []
        for t in toks:
            m = re.fullmatch(r'(心情|体力|清洁)值\+(\d+)', t)
            if m:
                g = gains.setdefault(m.group(1), [0, 0])
                g[0] += 1
                g[1] += int(m.group(2))
                gs.append(f'{m.group(1)}+{m.group(2)}')
        ts = str(r.get('ts') or '')
        c = int(r.get('coins') or 0)
        recent.append([i, ts[5:16] or ts[:5], c, ' '.join(gs), 0])
    return {
        'ok': True, 'n': n,
        'net': net, 'avg': round(net / n, 2) if n else 0,
        'dist': dist, 'win': win, 'zero': zero, 'loss': 0,
        'gains': [[k, v[0], v[1]] for k, v in sorted(gains.items())],
        'cum': cum, 'pts': pts, 'stats': [], 'recent': recent[-800:],
        'today_n': tn, 'today_net': tnet,
        'date': sel, 'dates': dates, 'date_n': date_n,
        'today': today, 'yesterday': yesterday, 'all_n': len(rows),
        'updated': str(view[-1].get('ts') or '')[11:16] if view else '',
    }


PLAN_FILE = RUNS / 'career_plan.json'
_PLAN_DEFAULTS = {'力': 0, '智': 0, '魅': 0, '工分': 0, '金币': 0,
                  '初级毕业': False, '中级毕业': False}

# (线名, 见习条件: 三元组 或 0/1/2=比例专修轴, 初级三元组)
_PLAN_LINES = [
    ('流浪散人', None, None),
    ('画家', (7, 3, 11), (225, 113, 412)),
    ('侦探', (11, 5, 5), (350, 200, 200)),
    ('法师', (3, 11, 7), (113, 412, 225)),
    ('大厨', (11, 7, 3), (412, 225, 113)),
    ('武术家', 0, (750, 0, 0)),
    ('梦境旅人', 1, (0, 750, 0)),
    ('大明星', 2, (0, 0, 750)),
]


def _plan_values() -> dict:
    try:
        data = json.loads(PLAN_FILE.read_text('utf-8'))
    except Exception:
        data = {}
    v = dict(_PLAN_DEFAULTS)
    for k in v:
        if k in data:
            v[k] = data[k]
    return v


def _triple_ok(V, tri):
    return all(V[i] >= tri[i] for i in range(3))


def plan_data() -> dict:
    """职业解锁计划进度（runs/career_plan.json + 规则换算）。"""
    v = _plan_values()
    V = [int(v['力']), int(v['智']), int(v['魅'])]

    def ratio_ok(axis):
        return V[axis] >= 24 and V[axis] >= 2.5 * (sum(V) - V[axis])

    # 职业树实测状态（career_unlock.json 的 lines 段，哨兵每节课后写回）
    ud0 = read_json('career_unlock.json') or {}
    tree_lines = (ud0.get('lines') or {}) if isinstance(ud0, dict) else {}

    def jr_state(name, fallback_numeric: bool) -> bool:
        """见习状态：职业树实测优先；没测到过的隐藏线按未解锁（宁可不勾），普通线按数值兜底。"""
        st = tree_lines.get(name) or {}
        if 'unlocked' in st:
            return bool(st.get('unlocked'))
        return fallback_numeric

    gates = {'工分': int(v['工分']) >= 360, '金币': int(v['金币']) >= 1000,
             '初级毕业': bool(v['初级毕业']), '中级毕业': bool(v['中级毕业'])}
    gates_ok = gates['工分'] and gates['金币'] and gates['初级毕业']
    lines = []
    for name, jr, ch in _PLAN_LINES:
        if jr is None and ch is None:
            lines.append({'name': name, 'jr': True, 'ch': False})
            continue
        jr_num = ratio_ok(jr) if isinstance(jr, int) else _triple_ok(V, jr)
        hidden = isinstance(jr, int)  # 隐藏线（单属性）：解锁有概率性，不能按数字勾
        jr_ok = jr_state(name, False if hidden else jr_num)
        ch_attr = _triple_ok(V, ch)
        lines.append({'name': name, 'jr': jr_ok, 'ch': ch_attr and gates_ok and jr_ok,
                      'ch_attr': ch_attr})
    # 阶梯路线（2026-09-13 调整：先解锁浅梦行者——智力专修最先，魅力/力量按 ×2.5 链递增）
    # 隐藏线三步的"完成"以职业树实测为准（解锁有概率性：数值达标≠已解锁）
    r_mind = max(24, int(2.5 * (V[0] + V[2])))
    r_char = max(24, int(2.5 * (V[0] + V[1])))
    r_force = max(24, int(2.5 * (V[1] + V[2])))

    def hidden_step(line: str, axis: int, need: int):
        num_ok = V[axis] >= 24 and V[axis] >= 2.5 * (sum(V) - V[axis])
        done = jr_state(line, False)
        pr = f'{V[axis]}（需≥{need}）' if (done or not num_ok) else f'{V[axis]} 达标·待触发'
        return done, pr

    s1_done, s1_pr = hidden_step('梦境旅人', 1, r_mind)
    s2_done, s2_pr = hidden_step('大明星', 2, r_char)
    s3_done, s3_pr = hidden_step('武术家', 0, r_force)
    steps = [
        ('S1', '智力专修，解锁 浅梦行者（≥其余两和的2.5倍）', s1_done, s1_pr),
        ('S2', '魅力专修，解锁 偶像练习生', s2_done, s2_pr),
        ('S3', '力量专修，解锁 习武小童', s3_done, s3_pr),
        ('S4', '补 力11·智11，解锁 侦探/法师/画家/大厨（三维覆盖自动达成）',
         V[0] >= 11 and V[1] >= 11 and V[2] >= 24, f'{V[0]}/11 · {V[1]}/11'),
        ('S5', '魅力补到 225（混合线初级前置）', V[2] >= 225, f'{V[2]}/225'),
        ('S6', '三维各 750 → 全 8 线初级', min(V) >= 750, f'最低 {min(V)}/750'),
    ]
    # 隐藏职业哨兵状态（runs/career_unlock.json + config.yaml 的 career 段；展示用）
    try:
        import yaml as _yaml
        _rawcfg = _yaml.safe_load((BASE / 'config.yaml').read_text('utf-8')) or {}
        ccfg = _rawcfg.get('career') or {}
    except Exception:
        ccfg = {}
    ud = read_json('career_unlock.json')
    try:
        alive = bool(scheduler_info().get('alive'))
    except Exception:
        alive = False
    watch = {
        'enabled': bool(ccfg.get('watch', True)),
        'stop_study': bool(ccfg.get('stop_study_on_unlock', True)),
        'interval': ccfg.get('check_interval_min', 60),
        'last_check': ud.get('last_check'),
        'alive': alive,
        'events': [{'career': e.get('career'), 'name': e.get('name'), 'ts': e.get('ts'),
                    'stopped': bool(e.get('stopped')), 'shot': e.get('shot')}
                   for e in (ud.get('events') or [])[-6:]],
    }
    total = sum(V)
    last_at = max((str((st or {}).get('at') or '') for st in tree_lines.values()), default='')
    lines_meta = f'职业树实测 · {last_at[5:16]}' if last_at else ''
    return {
        'ok': True, 'values': v, 'total': total, 'total_target': 2250,
        'jr_n': sum(1 for l in lines if l['jr']),
        'ch_n': sum(1 for l in lines if l['ch']),
        'lines': lines,
        'steps': [[s[0], s[1], s[2], s[3]] for s in steps],
        'lines_meta': lines_meta,
        'gates': gates,
        'updated': datetime.now().strftime('%H:%M:%S'),
        'watch': watch,
    }


def apply_plan(updates: dict) -> dict:
    v = _plan_values()
    applied, rejected = {}, []
    int_limits = {'力': 99999, '智': 99999, '魅': 99999, '工分': 9999999, '金币': 99999999}
    for k, val in (updates or {}).items():
        if k in int_limits:
            try:
                n = int(val)
            except Exception:
                rejected.append(f'{k}: 需要数字')
                continue
            if not (0 <= n <= int_limits[k]):
                rejected.append(f'{k}: 超范围')
                continue
            v[k] = n
            applied[k] = n
        elif k in ('初级毕业', '中级毕业'):
            if not isinstance(val, bool):
                rejected.append(f'{k}: 需要布尔')
                continue
            v[k] = val
            applied[k] = val
        else:
            rejected.append(f'{k}: 不支持')
    if applied:
        RUNS.mkdir(parents=True, exist_ok=True)
        tmp = PLAN_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(v, ensure_ascii=False, indent=1), 'utf-8')
        tmp.replace(PLAN_FILE)
    return {'ok': not rejected, 'applied': applied, 'rejected': rejected}


_SYNC = {'busy': False}


def career_sync() -> dict:
    """运行 tools/career_sync.py 自动识别游戏里的属性（占用设备约 20 秒）。"""
    if _SYNC['busy']:
        return {'ok': False, 'reason': '正在同步中，请稍候'}
    script = BASE / 'tools' / 'career_sync.py'
    if not script.is_file():
        return {'ok': False, 'reason': '未找到 tools/career_sync.py'}
    _SYNC['busy'] = True
    try:
        proc = subprocess.run([sys.executable, str(script)],
                              capture_output=True, text=True, timeout=150, cwd=str(BASE))
    except subprocess.TimeoutExpired:
        return {'ok': False, 'reason': '识别超时（150 秒）'}
    finally:
        _SYNC['busy'] = False
    for line in reversed((proc.stdout or '').splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                return json.loads(line)
            except Exception:
                continue
    return {'ok': False, 'reason': '脚本无有效输出', 'stderr': (proc.stderr or '')[-300:]}


def list_shots() -> list[dict]:
    out = []
    for p in sorted(RUNS.glob('*.png'), key=lambda x: x.stat().st_mtime, reverse=True)[:12]:
        out.append({'name': p.name,
                    'mtime': datetime.fromtimestamp(p.stat().st_mtime).strftime('%m-%d %H:%M')})
    return out


_shot_cache = {'ts': 0.0, 'data': b'', 'at': ''}


def capture_phone(width: int = 390, min_interval: float = 5.0):
    """adb 截取手机当前画面：缩放 + JPEG（带节流缓存）。

    返回 (jpeg_bytes, 拍摄时间字符串, 是否来自缓存)。
    """
    if _shot_cache['data'] and time.time() - _shot_cache['ts'] < min_interval:
        return _shot_cache['data'], _shot_cache['at'], True
    adb_path, serial = 'adb', ''
    try:
        import yaml
        cfg = yaml.safe_load((BASE / 'config.yaml').read_text('utf-8')) or {}
        adb_path = (cfg.get('adb') or {}).get('path') or 'adb'
        serial = (cfg.get('adb') or {}).get('device_serial') or ''
    except Exception:
        pass
    cmd = [adb_path] + (['-s', serial] if serial else []) + ['exec-out', 'screencap', '-p']
    proc = subprocess.run(cmd, capture_output=True, timeout=30)
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b'').decode('utf-8', 'replace').strip()[:200]
        raise RuntimeError(detail or 'adb screencap 失败')
    from PIL import Image
    img = Image.open(io.BytesIO(proc.stdout))
    w, h = img.size
    if w > width:
        img = img.resize((width, max(1, round(h * width / w))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'JPEG', quality=82)
    data = buf.getvalue()
    at = datetime.now().strftime('%H:%M:%S')
    _shot_cache.update(ts=time.time(), data=data, at=at)
    return data, at, False


def editable_snapshot() -> dict:
    """设置卡可编辑字段的当前值（供表单回填）。"""
    try:
        import yaml
        cfg = yaml.safe_load((BASE / 'config.yaml').read_text('utf-8')) or {}
    except Exception:
        return {}
    try:
        from src.settings import WORK_LOCATIONS
        locations = list(WORK_LOCATIONS)
    except Exception:
        locations = []
    tasks = cfg.get('tasks') or {}
    sched = cfg.get('schedule') or {}
    work = cfg.get('work') or {}
    visit = cfg.get('visit') or {}
    pk = cfg.get('pk') or {}
    adv = cfg.get('adventure') or {}
    care = cfg.get('care') or {}
    fc = cfg.get('friend_care') or {}
    emp = cfg.get('employed') or {}
    school = cfg.get('school') or {}
    career = cfg.get('career') or {}
    notify = cfg.get('notify') or {}
    return {
        # 连接层：ADB 路径与设备序列号（设置页「连接手机」卡片；改完需重启调度器）
        'adb_path': str((cfg.get('adb') or {}).get('path') or ''),
        'adb_serial': str((cfg.get('adb') or {}).get('device_serial') or ''),
        'school_enabled': bool((tasks.get('school') or {}).get('enabled', True)),
        'school_attribute': str(school.get('attribute') or '力量'),
        'school_duration': str(school.get('duration') or '10分钟'),
        'school_times': school.get('times_per_day', 0),
        'work_location': work.get('location'),
        'work_locations': locations,
        'work_duration': work.get('duration'),
        'hire_name': str(work.get('hire_name') or ''),
        'hire_wait': bool(work.get('hire_wait', False)),
        'coin_threshold': sched.get('coin_threshold', 2000),
        'daily_hour_limit': sched.get('daily_hour_limit', 8),
        'work_stop_hours': sched.get('work_stop_hours', 12),
        'study_quota_hours': sched.get('study_quota_hours', 8),
        'work_quota_hours': sched.get('work_quota_hours', 8),
        'efficiency_tier1_hours': sched.get('efficiency_tier1_hours', 8),
        'efficiency_tier2_hours': sched.get('efficiency_tier2_hours', 12),
        'main_order': str(tasks.get('main_order') or ''),
        'visit_times': visit.get('times_per_day', 10),
        'pk_times': pk.get('times_per_day', 15),
        'pk_only': str(pk.get('only_names') or ''),
        'pk_skip': str(pk.get('skip_names') or ''),
        'pk_max_level': pk.get('max_level', 0) or 0,
        'pk_helper': str(pk.get('helper_names') or ''),
        'pk_helper_fallback': bool(pk.get('helper_fallback', False)),
        'adventure_times': adv.get('times_per_day', 1),
        'care_energy': care.get('energy_threshold', 60),
        'care_clean': care.get('clean_threshold', 60),
        'care_method': care.get('method', '一键护理'),
        'care_exchange': care.get('exchange_count', 20),
        'friend_care_enabled': bool(fc.get('enabled', False)),
        'friend_care_name': str(fc.get('friend_name') or ''),
        'friend_care_interval': fc.get('interval_seconds', 120),
        'friend_care_method': str(fc.get('method') or 'ocr检测'),
        'employed_enabled': bool(emp.get('enabled', False)),
        'employed_action': str(emp.get('action') or '等到25/75（小于45min）'),
        'employed_interval': emp.get('interval_seconds', 60),
        'gift_bag_enabled': bool((cfg.get('gift_bag') or {}).get('enabled', True)),
        'gift_bag_interval': (cfg.get('gift_bag') or {}).get('interval_seconds', 1800),
        'career_watch': bool(career.get('watch', True)),
        'career_stop_study': bool(career.get('stop_study_on_unlock', True)),
        'career_interval': career.get('check_interval_min', 60),
        # 通知渠道（飞书群机器人 / Telegram Bot）
        'notify_feishu_enabled': bool(notify.get('feishu_enabled', False)),
        'notify_feishu_webhook': str(notify.get('feishu_webhook') or ''),
        'notify_feishu_secret': str(notify.get('feishu_secret') or ''),
        'notify_telegram_enabled': bool(notify.get('telegram_enabled', False)),
        'notify_telegram_token': str(notify.get('telegram_token') or ''),
        'notify_telegram_chat_id': str(notify.get('telegram_chat_id') or ''),
        'notify_career': bool(notify.get('career_notify', True)),
        'notify_quota_done': bool(notify.get('quota_done', True)),
        'notify_event_notify': bool(notify.get('event_notify', True)),
        'notify_error_notify': bool(notify.get('error_notify', True)),
    }


def apply_settings(updates: dict) -> dict:
    """把设置卡改动写入 config.yaml（ruamel 往返保留注释，复用 src/settings 校验）。

    调度器每轮重读配置（含 tasks.* 任务级设置），保存后下一轮调度自动生效。
    """
    import src.settings as S
    # 复合任务（好友护理/福袋/雇佣好友）同时有任务级 tasks.<k>.enabled 与场景级
    # <k>.enabled 两个开关，前端勾选态 = 两者 AND（见 _task_enabled_map）。仪表盘
    # 任务队列勾选框与设置页开关共用同一字段名，只写其中一个会让另一个残留 false，
    # 表现为"点了勾选/开关却启不起来"（单向失效）。两处一起写，保持一致。
    task_scene_pairs = {
        'friend_care_enabled': ('tasks.friend_care.enabled', 'friend_care.enabled'),
        'gift_bag_enabled': ('tasks.gift_bag.enabled', 'gift_bag.enabled'),
        'hire_friend_enabled': ('tasks.hire_friend.enabled', 'hire_friend.enabled'),
    }
    mapping = {
        # 连接层（改完需重启调度器才生效，设置页卡片里有提示）
        'adb_path': ('adb.path', 'str'),
        'adb_serial': ('adb.device_serial', 'str'),
        'school_enabled': ('tasks.school.enabled', 'bool'),
        'care_enabled': ('tasks.care.enabled', 'bool'),
        'adventure_enabled': ('tasks.adventure.enabled', 'bool'),
        'visit_enabled': ('tasks.visit.enabled', 'bool'),
        'pk_enabled': ('tasks.pk.enabled', 'bool'),
        'work_enabled': ('tasks.work.enabled', 'bool'),
        # 复合任务主键（实际写入见 task_scene_pairs 的双写）
        'friend_care_enabled': ('tasks.friend_care.enabled', 'bool'),
        'gift_bag_enabled': ('tasks.gift_bag.enabled', 'bool'),
        'hire_friend_enabled': ('tasks.hire_friend.enabled', 'bool'),
        'work_location': ('work.location', None),
        'work_duration': ('work.duration', None),
        'hire_name': ('work.hire_name', None),
        'hire_wait': ('work.hire_wait', 'bool'),
        'coin_threshold': ('schedule.coin_threshold', 'int'),
        'daily_hour_limit': ('schedule.daily_hour_limit', 'int'),
        'work_stop_hours': ('schedule.work_stop_hours', 'int'),
        'study_quota_hours': ('schedule.study_quota_hours', 'int'),
        'work_quota_hours': ('schedule.work_quota_hours', 'int'),
        'efficiency_tier1_hours': ('schedule.efficiency_tier1_hours', 'int'),
        'efficiency_tier2_hours': ('schedule.efficiency_tier2_hours', 'int'),
        'main_order': ('tasks.main_order', None),
        'school_attribute': ('school.attribute', None),
        'school_duration': ('school.duration', None),
        'school_times': ('school.times_per_day', 'int'),
        'visit_times': ('visit.times_per_day', 'int'),
        'pk_times': ('pk.times_per_day', 'int'),
        'pk_only': ('pk.only_names', None),
        'pk_skip': ('pk.skip_names', None),
        'pk_max_level': ('pk.max_level', 'int'),
        'pk_helper': ('pk.helper_names', None),
        'pk_helper_fallback': ('pk.helper_fallback', 'bool'),
        'adventure_times': ('adventure.times_per_day', 'int'),
        'care_energy': ('care.energy_threshold', 'int'),
        'care_clean': ('care.clean_threshold', 'int'),
        'care_method': ('care.method', None),
        'care_exchange': ('care.exchange_count', 'int'),
        'friend_care_name': ('friend_care.friend_name', None),
        'friend_care_interval': ('friend_care.interval_seconds', 'int'),
        'friend_care_method': ('friend_care.method', None),
        'employed_enabled': ('employed.enabled', 'bool'),
        'employed_action': ('employed.action', None),
        'employed_interval': ('employed.interval_seconds', 'int'),
        'gift_bag_interval': ('gift_bag.interval_seconds', 'int'),
        'career_watch': ('career.watch', 'bool'),
        'career_stop_study': ('career.stop_study_on_unlock', 'bool'),
        'career_interval': ('career.check_interval_min', 'int'),
        # 通知渠道（飞书群机器人 / Telegram Bot）——凭据类走 str，服务端 validate_field 校验
        'notify_feishu_enabled': ('notify.feishu_enabled', 'bool'),
        'notify_feishu_webhook': ('notify.feishu_webhook', 'str'),
        'notify_feishu_secret': ('notify.feishu_secret', 'str'),
        'notify_telegram_enabled': ('notify.telegram_enabled', 'bool'),
        'notify_telegram_token': ('notify.telegram_token', 'str'),
        'notify_telegram_chat_id': ('notify.telegram_chat_id', 'str'),
        'notify_career': ('notify.career_notify', 'bool'),
        'notify_quota_done': ('notify.quota_done', 'bool'),
        'notify_event_notify': ('notify.event_notify', 'bool'),
        'notify_error_notify': ('notify.error_notify', 'bool'),
    }
    data = S.load_raw()
    applied, rejected = {}, []
    for field, value in (updates or {}).items():
        if field not in mapping:
            rejected.append(f'{field}: 不支持')
            continue
        key, kind = mapping[field]
        if kind == 'bool':
            if not isinstance(value, bool):
                rejected.append(f'{field}: 需要布尔值')
                continue
            # 复合任务：任务级 + 场景级两个开关同时写，避免另一个残留 false
            for k in task_scene_pairs.get(field, (key,)):
                S.set_value(data, k, value)
            applied[field] = value
            continue
        ok, fixed = S.validate_field(key, value)
        if not ok:
            rejected.append(f'{field}: 非法值 {value!r}')
            continue
        if kind == 'int':
            fixed = int(fixed)
        S.set_value(data, key, fixed)
        applied[field] = fixed
    if applied:
        S.save_raw(data)
    return {'ok': not rejected, 'applied': applied, 'rejected': rejected}


def _adb_settings() -> tuple[str, str]:
    """读 config.yaml 的 adb 段 → (配置的 adb 路径, 配置的设备序列号)。"""
    try:
        import yaml
        cfg = yaml.safe_load((BASE / 'config.yaml').read_text('utf-8')) or {}
    except Exception:
        return '', ''
    adb_cfg = cfg.get('adb') or {}
    return str(adb_cfg.get('path') or ''), str(adb_cfg.get('device_serial') or '')


def _resolve_adb(configured: str) -> tuple[str, str]:
    """解析出实际可用的 adb 可执行文件路径 → (路径, 错误说明)。"""
    try:
        import sys as _sys
        if str(BASE) not in _sys.path:
            _sys.path.insert(0, str(BASE))
        from src.config import find_adb
        return find_adb(configured), ''
    except Exception as e:  # noqa: BLE001
        return '', str(e)


def adb_info() -> dict:
    """ADB 配置 + 在线设备列表（设置页「连接手机」卡片用）。

    `adb devices -l` 解析出 serial / state / model。adb 不可用、超时、命令失败都
    只记进 error 字段，不抛异常——界面显示空列表即可，不影响仪表盘其他功能。
    """
    import subprocess
    configured, serial = _adb_settings()
    resolved, err = _resolve_adb(configured)
    devices: list[dict] = []
    if resolved:
        try:
            out = subprocess.run([resolved, 'devices', '-l'], capture_output=True,
                                 text=True, timeout=10)
            for ln in (out.stdout or '').splitlines()[1:]:
                ln = ln.strip()
                if not ln or ln.startswith('*'):
                    continue
                # 用正则而不是 split()：`(no serial number)   device usb:…` 这类行
                # 的"序列号"自带空格，split 会把它拆成 serial="(no" / state="serial"
                # （实测踩过）。\S+ 匹配不到带空格的伪序列号，正好跳过。
                mo = re.match(r'^(\S+)\s+(device|offline|unauthorized|no permissions'
                              r'|bootloader|recovery|sideload)\b(.*)$', ln)
                if not mo:
                    continue
                d = {'serial': mo.group(1), 'state': mo.group(2)}
                for p in mo.group(3).split():
                    if p.startswith('model:'):
                        d['model'] = p[6:].replace('_', ' ')
                    elif p.startswith('product:'):
                        d['product'] = p[8:]
                devices.append(d)
            if out.returncode != 0 and not devices:
                err = (out.stderr or '').strip() or f'adb 返回码 {out.returncode}'
        except subprocess.TimeoutExpired:
            err = 'adb devices 超时（10s）——adb server 可能卡住，可试 adb kill-server'
        except Exception as e:  # noqa: BLE001
            err = f'执行 adb 失败：{e}'
    return {'path': configured, 'resolved': resolved, 'serial': serial,
            'devices': devices, 'error': err}


def adb_connect(addr: str) -> dict:
    """`adb connect <addr>`（无线调试 / 模拟器）。返回 {ok, msg}。"""
    import subprocess
    addr = (addr or '').strip()
    if not addr:
        return {'ok': False, 'msg': '请填写地址，如 192.168.1.5:5555 或 127.0.0.1:7555'}
    configured, _ = _adb_settings()
    resolved, err = _resolve_adb(configured)
    if not resolved:
        return {'ok': False, 'msg': f'找不到 adb：{err}'}
    if ':' not in addr:
        addr = f'{addr}:5555'          # 省略端口时按 adb 默认无线调试端口补全
    try:
        r = subprocess.run([resolved, 'connect', addr], capture_output=True,
                           text=True, timeout=20)
        out = ((r.stdout or '') + (r.stderr or '')).strip()
        ok = 'connected' in out and 'cannot' not in out.lower() and 'failed' not in out.lower()
        return {'ok': ok, 'msg': out or f'返回码 {r.returncode}', 'addr': addr}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'msg': 'adb connect 超时（20s）'}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'msg': f'执行失败：{e}'}


def build_data() -> dict:
    log_lines = []
    p = today_log_path()
    if p:
        log_lines = tail_lines(p, 400)
    accounts = (read_json('status_cache.json').get('accounts') or {})
    try:
        import yaml
        sched_cfg = (yaml.safe_load((BASE / 'config.yaml').read_text('utf-8')) or {}).get('schedule') or {}
    except Exception:
        sched_cfg = {}
    return {
        'now': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'scheduler': scheduler_info(),
        'queue': read_json('queue_status.json'),
        'status': accounts.get('default') or {},
        'progress': load_progress(),
        'config': config_summary(),
        'shots': list_shots(),
        'work_eta': work_eta(cross_day_log_lines(log_lines, p)),
        'today_duration': today_duration(log_lines, efficiency_tiers_of(sched_cfg)),
        'last_line': log_lines[-1] if log_lines else '',
        'editable': editable_snapshot(),
        'friends': list_friends(),
    }


# ---------------------------------------------------------------- HTTP

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<!-- theme-color：iOS 独立 Web App（添加到主屏）下状态栏那条带的颜色。
     取房间背景图最顶部实测色（room-main.jpg 顶部 #D5A758 / room-main-dark.jpg
     顶部 #A9722D），与总览页暖色房间无缝。历史值 #f6f7f9/#111318 是暖色改版
     **之前**的冷灰，改版时漏改（用户反馈"顶部安全区是白的"）。
     注意 iOS 26 起 theme-color 支持被移除，状态栏改为采样页面背景色 ——
     所以 html 的 background-color（见 --bg）必须一起对齐，两条腿都要有。
     切内页时由 syncThemeColor() 改成顶栏白（见 JS）。 -->
<meta name="theme-color" content="#D5A758" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#A9722D" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/png" href="/icon-192.png">
<!-- iOS 主屏图标：**必须换 URL 才会更新** —— iOS 按 URL 缓存 apple-touch-icon，
     内容换了但 URL 不变时，重新"添加到主屏幕"仍是旧图标（实测踩坑）。
     故用独立的 apple-touch-icon.png（180×180，iOS 标准尺寸）而不是复用 icon-192.png；
     以后换图标同样要换文件名（如 -v2）。 -->
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="QQ宠物">
<!-- **故意不写 apple-mobile-web-app-status-bar-style** ——
     它的 `default` 值语义是"内容显示在状态栏下方"（Apple 官方文档
     Safari HTML Reference / Supported Meta Tags），于是状态栏那条带完全由系统绘制
     （浅色模式 = 白底黑字），网页既画不上去也改不了色，真机就是顶部一条白带。
     而 `black-translucent` 已被 WebKit 废弃（语义上与深色模式冲突），
     WebKit 工程师在 bug 317153 里明确答复："Please remove this key,
     and the status bar should automatically appear in the same color as the webpage."
     移除后状态栏改为采样网页背景色 —— 这正是我们要的效果，
     配合下面的 theme-color 与 html 的 background-color（--qp-statusbar）一起生效。 -->
<title>QQ宠物托管</title>
<style>
/* ==========================================================================
   QQ 宠物托管 · 仪表盘样式（重写版）
   --------------------------------------------------------------------------
   设计依据：qqpet_assets/web/UI_TREE_OFFICIAL.md
     · 官方运行时 uiautomator dump（Kuikly 暴露的 content-desc + bounds）
     · 逻辑画布 480dp；边距 20dp；圆钮 42dp；胶囊高 28dp；
       左列圆钮 y=28/86/146/206（步进 60dp）；胶囊行 y=86/126（步进 40dp）
     · 配色取自 Kuikly 反编译代码里的 ARGB 常量（非目测）
   组织原则：token 只定义一次；结构分层（token → 基础 → 骨架 → 总览 → 卡片
             → 表单 → 响应式 → 深色），避免历史版本的选择器重复覆盖。
   ========================================================================== */

/* ---------- 1. 设计 token（唯一数据源） ---------- */
:root{
  /* 画布与间距（官方 dp） */
  --side:20px;          /* 左右边距（官方 x=20dp） */
  --rbtn:42px;          /* 圆钮尺寸（官方 42×42dp） */
  --cap-h:28px;         /* 胶囊高（官方 28dp） */
  --cap-r:14px;         /* 胶囊圆角 = 高/2（全圆角） */
  --step-btn:60px;      /* 左列圆钮步进（官方 y 28→86→146→206） */
  --step-cap:40px;      /* 胶囊两行步进（官方 y 86→126） */
  --gap-card:10px;      /* 卡片间距 */

  /* 顶部三块（资料卡 / 胶囊两行 / 任务面板）的纵坐标与**统一间距**。
     改 --gap-top 一个值，三处间隙同步变化，不会各改各的又跑偏。
     历史值：资料卡→胶囊 12.2 / 胶囊两行 17 / 胶囊→面板 6.5（三个不等，
     视觉上疏密不一：面板被胶囊贴住，两行胶囊又散开）。 */
  --top-card:34.3;      /* 资料卡顶 y（官方 y=26.2，本机下移让开顶部安全区） */
  --h-card:46;          /* 资料卡高（官方 46.2） */
  --h-cap:23;           /* 胶囊行高：图标 23dp 外凸于 20dp 胶囊体（官方图标 22.3） */
  --gap-top:12;         /* 顶部区块统一间距（TDesign 间距阶梯 12；官方资料卡→胶囊实测 13.8） */
  /* 由上面几个值推出后续纵坐标（不要再手写数字，否则改了间距就对不上）：
     胶囊区顶 = 资料卡顶 + 资料卡高 + 间距
     任务面板顶 = 胶囊区顶 + 胶囊行高 + 间距 + 胶囊行高 + 间距 */
  --top-caps:calc(var(--top-card) + var(--h-card) + var(--gap-top));
  --top-scene:calc(var(--top-caps) + var(--h-cap) + var(--gap-top) + var(--h-cap) + var(--gap-top));

  /* 暖色拟物底（官方房间背景的同色系） */
  --bg:#FCF7ED;
  /* 状态栏带采样色：iOS 独立 Web App（添加到主屏）的状态栏那条带由**系统**绘制，
     DOM 够不到（WebKit bug 301994：内容不能画到状态栏下方）。它的颜色取
     "页面背景色" —— iOS 18 及更早读 <meta name="theme-color">，iOS 26 起
     theme-color 支持被移除、改为直接采样 html/body 的 background-color
     （WebKit bug 309956 / 317153）。两条路都要对齐，否则顶部就是一条
     与页面割裂的白带。取值 = 各房间背景图最顶部实测色。
     按场景拆成独立 token（--qp-sb-*）而不是直接写 --qp-statusbar：
     深色模式的覆盖写在 :root 里，而 html[data-scene] 特异性更高会把它压掉，
     拆开后深色只需覆盖 --qp-sb-*，由 data-scene 那条统一取值。 */
  --qp-sb-main:#D5A758;
  --qp-sb-feed:#CA9F5B;
  --qp-sb-shower:#E7BC6C;
  --qp-sb-record:#CAA05C;
  --qp-statusbar:var(--qp-sb-main);
  --qp-bg2:#EDD18F;
  --card:#F9EFDE;       /* 卡片/资料卡底（官方 #F9EFDE） */
  --line:#EFE3CF;
  --text:#723900;       /* 主文字：深棕（官方 #FF723900，出现 11 次） */
  --sub:#9A7A4A;        /* 次级文字（暖色系降饱和） */
  --strong:#521C00;     /* 强调文字（官方暖色池最深的 #FF521C00） */
  --accent:#FF990F;     /* 主强调橙（官方 #FF990F） */
  --ok:#64C900;         /* 清洁绿（官方 #FF64C900） */
  --warn:#FF8000;       /* 心情橙（官方 #FFFF8000） */
  --gold:#CA810D;       /* 金币（官方 #FFCA810D） */

  /* 圆钮（左=功能入口 / 右=装饰入口，官方是两套） */
  --btn-l-bg:#F9F1E2;   /* 左圆钮底（官方 #F9F1E2） */
  --btn-l-fg:#BE6321;   /* 左圆钮图标棕（官方 #FFBE6321） */
  --btn-r-bg:rgba(0,0,0,.28);       /* 右圆钮底：纯黑蒙版（只压暗、不改色相） */
  --btn-r-fg:#FFEA70;   /* 右圆钮图标亮黄 */

  /* 胶囊（官方 #99E1B053 → alpha 0.6，本色 #E1B053） */
  --cap-bg:#B69251;
  --cap-fg:#FFFFFF;

  /* 进度条 */
  --track:rgba(222,184,110,.55);
  --energy:#0099FF;     /* 体力（官方 #FF0099FF） */
  --clean:#64C900;      /* 清洁 */
  --mood:#FF8000;       /* 心情 */

  /* 圆角阶梯（TDesign token，官方设计体系） */
  --r-sm:6px; --r-md:10px; --r-lg:14px; --r-xl:18px;

  /* 阴影（TDesign shadow-1，最轻一档 */
  --sh-1:0 1px 10px rgba(120,85,30,.06),0 4px 5px rgba(120,85,30,.06);
  --sh-2:0 2px 12px rgba(120,85,30,.12);

  /* 房间背景（由 body[data-scene] 切换） */
  --qp-room-main:url('/qp-icons/bg/room-main.jpg');
  --qp-room-feed:url('/qp-icons/bg/room-feed.jpg');
  --qp-room-shower:url('/qp-icons/bg/room-shower.jpg');
  --qp-room-record:url('/qp-icons/bg/room-record.jpg');
  --qp-room:var(--qp-room-main);
  /* 1dp = 可用宽度/360（全局：设置页等非 .home 页面也要用） */
  --u:var(--vu, calc(100vw / 360));
}

/* ---------- 2. 基础 ---------- */
*{box-sizing:border-box}
html{
  margin:0;padding:0;height:100%;min-height:100%;
  /* 用状态栏采样色而不是 --bg（米白）：总览页这张背景图是 cover + center top，
     必然盖满视口，所以这个底色在页面上**看不见**，只被系统拿去画状态栏那条带；
     而 iOS 26+ 采样它、iOS 18- 读 theme-color meta，两边取值保持一致。
     内页另有 html[data-page]:not([data-page="main"]){background-color:#fff} 覆盖。 */
  background-color:var(--qp-statusbar);
  background-image:var(--qp-room);
  background-position:center top;background-size:cover;
  background-repeat:no-repeat;background-attachment:fixed;
  scrollbar-width:thin;scrollbar-color:#cfd3db transparent;
  /* ---------- 滚动架构：html 只当画布，不滚动；真正滚的是 body ----------
     （用户要求"把 PWA 上下滑的弹性回弹删了"）
     为什么必须让**文档层不可滚**：iOS 独立 Web App 里，只要根滚动器还有可滚内容，
     整页就会橡皮筋回弹，而 `overscroll-behavior` **管不到 iOS 的根滚动器**
     （WebKit 把该属性映射到 UIScrollView 的 elasticity，根滚动器是特例）。
     把 html 锁成 overflow:hidden，文档层就没有可滚内容，整页回弹直接失去来源。
     下面 body 才是滚动容器，并显式 overscroll-behavior:none。
     另：`contain` 只挡"链式传递"，**回弹照旧**，所以这里必须写 none。 */
  overflow:hidden;
  overscroll-behavior:none;
}
body{
  margin:0;padding:0;background:transparent;
  /* 顶部安全区：避免内容被状态栏/灵动岛遮挡（PWA 全屏时必需）。
     只加上边距 —— 底部不加，否则会露白条（实测踩坑）。 */
  padding-top:env(safe-area-inset-top, 0px);
  color:var(--text);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",Roboto,sans-serif;
  -webkit-text-size-adjust:100%;
  /* 唯一的滚动容器（原因见 html 处注释）：
     - height:100% = 一屏（`*{box-sizing:border-box}` 已生效，padding-top 吃在里面，
       所以内容区正好是"可视高 - 安全区"，与 .home 的 100dvh - 安全区 对齐，不会多出一条）
     - -webkit-overflow-scrolling:touch 保留 iOS 的惯性滚动（只去掉边缘回弹，不去惯性）
     - overscroll-behavior:none：WebKit 映射到 ScrollElasticityNone → 内层也不回弹
     别改成 position:fixed 方案：那会让 body 成为固定后代的包含块，影响面更大。 */
  height:100%;
  overflow-y:auto;overflow-x:hidden;
  -webkit-overflow-scrolling:touch;
  overscroll-behavior:none;
}
::-webkit-scrollbar{width:7px;height:7px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#cfd3db;border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:#b4bac6}
::-webkit-scrollbar-corner{background:transparent}
.hide{display:none!important}

/* ---------- 3. 骨架：480dp 浮动层舞台 ---------- */
/* 单位系统：--u = 1dp = 屏宽/480。官方逻辑画布就是 480dp，所有坐标直接用它。 */
.home{
  /* 单位基准：1dp = 可用宽度 / 360。
     **基准是 360 不是 480** —— 官方手机版逻辑画布是 360dp（真机 density480）：
     按钮 42dp 占屏宽 11.67%。若按 480dp（平板/模拟器版）做，按钮只占 8.75%，
     会明显偏小（实测小了 25%）。
     不用 100vw：它含滚动条宽度，会让坐标偏大约 2%。由 JS 写入 --vu。 */

  position:relative;
  width:100%;max-width:100vw;
  box-sizing:border-box;
  overflow-x:hidden;
  /* 官方画布 853dp；用 --u 乘出来即可。
     注意不要写 calc(100% * N) —— 百分比在 min-height 里会被当相对高度算。 */
  /* 高度 = 一屏 - 顶部安全区。
     **两条的顺序不能反**：同优先级声明后写的胜出，所以 dvh 必须写在 vh 后面。
     历史版本写成了「dvh 在前、vh 在后」，等于 dvh 是死代码，所有浏览器
     实际都走 vh —— iOS 独立 Web App（添加到主屏）里 vh = 物理整屏高
     （含顶部状态栏那条带），而 dvh = 该带以下的可见高度，于是 .home 比可视区
     整整高出一条状态栏，底部 .deck（弧形面板/任务抽屉）被顶到折叠线以下，
     真机表现就是「下面显示不全、要往下滑才看得全」。
     （WebKit 相关 bug：254868 / 301994） */
  min-height:calc(100vh - env(safe-area-inset-top, 0px));
  min-height:calc(100dvh - env(safe-area-inset-top, 0px));
  margin:0;padding:0;
}
/* 浮动元素通用：position:absolute；坐标由各具体类给出（写死 dp 字面量，
   不用 CSS 变量传坐标 —— 变量在内联 style 里对 calc 的替换曾出现失败） */
.flt{position:absolute;left:0;top:0}
.flt-l{display:contents}

/* ---------- 3b. 左右两列圆钮（官方坐标，逐项对齐） ---------- */
/* 左列：x=20，y=28/86/146/206 */
/* 官方手机版（360dp）实测：左列 x=20 y=36.3/94.3/154.3/214.3 */
.col-l .rbtn:nth-child(1){left:calc(var(--u)*20);top:calc(var(--u)*36.3)}
.col-l .rbtn:nth-child(2){left:calc(var(--u)*20);top:calc(var(--u)*94.3)}
.col-l .rbtn:nth-child(3){left:calc(var(--u)*20);top:calc(var(--u)*154.3)}
.col-l .rbtn:nth-child(4){left:calc(var(--u)*20);top:calc(var(--u)*214.3)}
/* 右列：x=298 y=36.3/94.3/154.3（官方手机版） */
.col-r .rbtn:nth-child(1){left:calc(var(--u)*298);top:calc(var(--u)*36.3)}
.col-r .rbtn:nth-child(2){left:calc(var(--u)*298);top:calc(var(--u)*94.3)}
.col-r .rbtn:nth-child(3){left:calc(var(--u)*298);top:calc(var(--u)*154.3)}
.rbtn{
  position:absolute;
  width:calc(var(--u)*42);height:calc(var(--u)*42);
  border:0;border-radius:50%;
  background:var(--btn-l-bg);
  display:flex;align-items:center;justify-content:center;
  padding:0;cursor:pointer;
  box-shadow:var(--sh-1);
  transition:transform .12s;
}
/* 左侧圆钮：米白底 + 棕色图标（官方 #F9F1E2 / #BE6321） */
.col-l .rbtn img{width:55%;height:55%;object-fit:contain}
/* 右侧圆钮：**纯黑半透明蒙版** + 原色图标（用户要求"改成纯黑色，然后变透明"）。
   为什么用纯黑而不是调一个"深褐色"：
   - 纯黑蒙版只做一件事 —— 把背景**压暗**，不引入任何自己的色相。
     于是它永远与暖黄房间背景协调（同色系明度变化），不会"跟背景一点都不搭"。
   - 历史踩坑：曾按官方合成色 #B18E49 反解出 rgba(154,144,72,.45)（橄榄绿），
     虽数学上叠回官方值，但那是在"纯色叠加"假设下成立的；实际背景是带渐变+
     条纹的暖黄，橄榄绿与它并置就发脏发怪（用户反馈"变得更怪了"）。
     教训：**给半透明元素选底色时，用中性色（黑/白）最稳**，
     想让底色"带点颜色"就得接受它在复杂背景上不可控。
   - alpha 取 0.28：实测 0.15 太淡（圆钮轮廓几乎看不出、图标像浮在空中），
     0.42 太重（职业那个纯黑帽子会糊进底色，只剩火苗和帽檐）。0.28 居中。
     合成后约 #8D6535（背景 #C48C4A 压暗 45%），层次够、又不抢图标。
   - 保留 backdrop-filter 与 .qpanel/.fb-group 质感统一。 */
.col-r .rbtn{
  background:var(--btn-r-bg);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
}
/* 右列图标尺寸：**逐个设**，不要统一一个百分比（用户要求"3 个图标都大一点"）。
   原因：三张图的"可见内容占画布比例"差很多（实测 alpha/几何包围盒）：
     指南针  88% × 88%     内容基本占满
     公文包  91% × 79%     横向满、纵向扁
     手机    66.5% × 93.3% 竖长条（卡通版重绘后的实测值，旧线性版是 64.6% × 89.6%）
   若统一 43%，可见内容高度分别只有 15.89 / 14.27 / 14.45dp —— 看起来都偏小，
   且彼此不等大（公文包最扁、最显小）。
   故按"**可见内容高度统一到 18dp**"反解各自盒子尺寸（= 18 / 内容高占比）：
     指南针 18/0.88   = 20.45dp = 48.7%
     公文包 18/0.79   = 22.78dp = 54.2%
     手机   见下方（**唯一例外**，不按 18dp，用户要求再大一点）
   这样三者可见高度都是 18dp（比原来 15.9dp 放大 13%），宽度也接近（13~21dp），
   视觉体量一致；18/42 = 43% 的占空比，圆钮内留白仍然充足。
   **手机是唯一例外（52.5%）**：按 18dp 反解是 45.9%（= 18/0.9333/42），
   但手机是**竖长条**，同样"可见高度"下面积只有指南针的一半（12.8×18 vs 18×18），
   用户实测反馈"有点小，放大一点"→ 提到 52.5%（可见 14.7 × 20.6dp，约 +14%）。
   教训：**"统一可见高度"对长宽比差很多的图标不够用**，窄图要按面积/宽度补一点。
   改尺寸时**要重新量包围盒**，别直接改百分比 —— 各图留白不同，同百分比不等大
   （手机图标换成卡通版时包围盒从 89.6% 变 93.3%，百分比就跟着从 47.8% 降到 45.9%）。 */
.col-r .rbtn img{object-fit:contain}
.col-r .rbtn:nth-child(1) img{width:48.7%;height:48.7%}   /* 冒险：指南针 */
.col-r .rbtn:nth-child(2) img{width:54.2%;height:54.2%}   /* 职业：公文包（最扁，需最大盒子） */
.col-r .rbtn:nth-child(3) img{width:52.5%;height:52.5%}   /* 画面：手机（唯一例外，见上：竖长条要按面积补） */
.rbtn:active{transform:scale(.94)}
/* 官方圆钮没有"选中态"：所有钮同底色 + 原色图标。
   选中仅用轻微白色描边提示，不改底色（改底色与官方观感差很远）。 */
.rbtn.on{box-shadow:0 0 0 calc(var(--u) * 2) rgba(255,255,255,.85),var(--sh-1)}
.rbtn.on img{filter:none}

/* 禁用态（总览页左上角那个"返回"）。
   为什么禁用：它是 data-tab="main"，而 showTab() 在目标页 == 当前页时直接
   `if(name===curTab) return;` 早退 —— 总览页点它什么都不会发生。
   而 #tabbar 挂在 .home 里，切到设置/日志页时整个 .home 隐藏
   （实测 getBoundingClientRect() 全为 0×0），所以它**只在总览页可见**，
   偏偏在总览页永远无效 —— 两头堵死，是个纯粹的死键。
   置灰表达"此处没有上一级可返回"（总览页本来就是根页面）。

   **必须在这里压掉 .on 的白环**：JS 会按 data-tab===当前页 给按钮加 .on，
   总览页时它必然带环；而那个环是 box-shadow 外扩 2dp，把 42dp 撑成 46dp ——
   这正是"它比其他钮大一圈"的原因（实测 45.4dp vs 其余 41.8dp）。
   选择器特异性同为 (0,2,0)，靠**书写顺序在后**胜出，故本块必须留在 .rbtn.on 之后。 */
.rbtn:disabled{
  opacity:.42;cursor:default;
  box-shadow:var(--sh-1);          /* 退回普通圆钮的阴影，不要选中环 */
}
.rbtn:disabled:active{transform:none}

/* ---------- 4. 资料卡（官方 x=74 y=26 207×46） ---------- */
.idcard{
  left:calc(var(--u) * 74);
  top:calc(var(--u) * var(--top-card));
  width:calc(var(--u) * 207);
  height:calc(var(--u) * var(--h-card));
  display:flex;align-items:center;gap:calc(var(--u) * 6);
  padding:calc(var(--u) * 3) calc(var(--u) * 8) calc(var(--u) * 3) calc(var(--u) * 3);
  background:var(--card);border-radius:999px;
  box-shadow:var(--sh-1);
  box-sizing:border-box;
}
.avatar{
  width:calc(var(--u) * 40);height:calc(var(--u) * 40);
  border-radius:50%;flex:none;background:#ffedd5;
  overflow:hidden;display:flex;align-items:center;justify-content:center;
}
.avatar svg{display:block;width:100%;height:100%}
/* 头像用官方表情图（static/qp-icons/official/mood_smile.png，160×160，四周留白约 5%）：
   略微放大让表情填满圆形框，同时保留一点呼吸感（留白比 cap_paw 小，故只放大 5%） */
.avatar img{display:block;width:106%;height:106%;object-fit:contain}
.avatar.on{box-shadow:0 0 0 2px var(--ok)}
.avatar.off{box-shadow:0 0 0 2px #ef4444}
.idtxt{flex:1;min-width:0}
.idname{
  font-weight:680;font-size:calc(var(--u) * 13);line-height:1.15;
  color:var(--strong);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.idsub{
  font-size:calc(var(--u) * 10);color:var(--sub);line-height:1.2;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
/* 状态环：官方三层同心环（蓝/橙/绿），中心留白 */
.idring{
  width:calc(var(--u) * 26);height:calc(var(--u) * 26);
  border-radius:50%;flex:none;
  background:conic-gradient(var(--ok) 0 70%,var(--accent) 70% 88%,#0EA5E9 88% 100%);
  -webkit-mask:radial-gradient(circle,transparent 38%,#000 38%);
  mask:radial-gradient(circle,transparent 38%,#000 38%);
}
.idring.off{background:conic-gradient(#EF4444 0 100%)}

/* ---------- 5. 胶囊两行（官方 y=86 三颗 + y=126 两颗，每颗 67×28 间距 4） ---------- */
.caps{
  /* 与资料卡同宽同左右缘（官方：资料卡 74~281，胶囊区 74~283，右缘基本齐平）。
     行内三颗等分（官方每颗 67.1dp），gap 4dp -> (207-8)/3 = 66.3dp。
     纵向位置由 --top-caps 推出，行间距 = --gap-top（与资料卡→胶囊、
     胶囊→任务面板同一个值，见 token 区）。 */
  left:calc(var(--u) * 74);
  width:calc(var(--u) * 207);
  top:calc(var(--u) * var(--top-caps));
  display:flex;flex-direction:column;
  gap:calc(var(--u) * var(--gap-top)) 0;
}
.caps-row{display:flex;gap:calc(var(--u) * 4)}
/* 两行都等分，保证左右缘与资料卡对齐（不再按内容自适应 -> 右边参差） */
.caps-row .cap{flex:1 1 0;min-width:0}

.cap{flex:0 1 auto;min-width:calc(var(--u) * 63)}
.caps-row:nth-child(2) .cap{flex:0 0 auto}

.cap{
  /* 官方结构：图标在胶囊【外面】且比胶囊大（22.3 vs 19.7dp），上下凸出；
     图标左缘 = 胶囊左缘（胶囊左端被图标压住）。
     .cap 高 = 图标高 23dp；::before 画 20dp 胶囊体，垂直居中。 */
  position:relative;
  display:flex;align-items:center;
  height:calc(var(--u) * var(--h-cap));
  padding:0;box-sizing:border-box;
  color:var(--cap-fg);
}
.cap::before{
  content:"";position:absolute;
  left:0;right:0;
  top:50%;transform:translateY(-50%);
  height:calc(var(--u) * 20);
  background:var(--cap-bg);
  border-radius:calc(var(--u) * 10);
  box-shadow:inset 0 calc(var(--u)*1) calc(var(--u)*2) rgba(255,255,255,.28);
  z-index:0;
}
/* 文字层：占满胶囊体区域，水平+垂直居中（底部进度条由 .bar 绝对定位） */
.cap .capbody{
  position:relative;z-index:1;
  flex:1;min-width:0;height:100%;
  display:flex;align-items:center;justify-content:center;
  gap:calc(var(--u) * 2);
  padding-left:calc(var(--u) * 2);
  padding-right:calc(var(--u) * 4);
}
.cap .cico{
  width:calc(var(--u) * 23);height:calc(var(--u) * 23);
  flex:none;position:relative;z-index:2;object-fit:contain;
  /* 图标圆心 与 胶囊圆头圆心 对齐：
       胶囊高 20dp（半径 10），left=0 -> 圆头圆心在 10dp
       图标直径 23dp -> margin-left = 10 - 23/2 = -1.5dp
     这样图标正好把胶囊左端的圆头完全盖住。 */
  margin-left:calc(var(--u) * -1.5);
}
.cap .cval{
  font-weight:680;font-size:calc(var(--u) * 10.5);color:#fff;
  font-variant-numeric:tabular-nums;white-space:nowrap;
  flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;
  letter-spacing:-.02em;text-align:center;line-height:1;
}
.cap .cunit{
  font-size:calc(var(--u) * 8.5);color:rgba(255,255,255,.85);
  white-space:nowrap;flex:0 1 auto;min-width:0;overflow:hidden;
  text-overflow:ellipsis;max-width:46%;line-height:1;
}
/* 胶囊内进度条（官方等级胶囊那条"橙色斜纹"）。

   **左右内缩必须与 .capbody 的 padding 一致**（left=padding-left=2dp，
   right=padding-right=4dp）—— 这样进度条的水平中心**恒等于**文字中心，
   不需要靠调数值去"凑"居中：
     文字居中基准 = capbody 内容盒 = 23.5dp ~ 62.3dp（胶囊坐标），中心 42.9dp
     条 = 同样的 23.5 ~ 62.3dp                      中心 42.9dp  ✅ 恒等
   历史 bug：写的是 left:17dp / right:4dp，与 padding-left(2dp) 不一致，
   于是条的范围是 38.5~62.3dp、中心 50.4dp —— **比文字中心偏右 7.5dp**，
   且条只有 23.85dp 宽（比文字窄一截），看起来就是"贴在字下面的一小截、还歪"。
   改成 2/4 后条宽 38.8dp（加长 63%），成为官方那种贯穿式轨道。

   **定位用 left+right（相对 .capbody），不要写固定 width** ——
   .bar 的包含块是 .capbody，其宽度是**被左侧图标（.cico 23dp +
   margin-left:-1.5dp）挤过之后**的剩余宽度，且各胶囊不等宽（实测
   金币 66.3 / 踩踩 66.3 / 冒险 41.9 / 学习打工 74.0dp），写死 width 必然对不上：
   曾改成 width:45.3dp，结果 left(17)+width(45.3)=62.3dp 超出右缘 17.5dp，
   进度条直接画到胶囊外面。

   **曾经的误判（记下来避免重犯）**：早期看到 track=33.1px、以为"应该是 62.9px"，
   就断言比例错了（"9/10 只画出 47%"）。其实 62.9px 是**胶囊**宽不是轨道宽，
   正确算法 fill/track = 29.8/33.1 = 90.0%，**本来就是对的**。
   > 判断比例时，分母必须取**同一元素**的实测值，别拿父级的宽度去除。

   轨道色是"深色凹槽"：官方实测 #9A8353（亮度 132）**深于**胶囊底（179），
   是挖进去的槽；改前用 rgba(255,255,255,.30) 反而比胶囊底亮 +32，像贴白胶带。

   填充用官方条纹素材：static/qp-icons/official/cap_bar_fg.png
   （源：cdn_assets 的 pet_level_progress_fg.png，688×32，官方等级进度条前景图）。
   素材左右边缘色一致可无缝平铺、上下自带渐变，故不需再叠渐变；
   background-size:auto 100% 保证条高变化时条纹不被拉伸。 */
.cap .bar{
  position:absolute;z-index:1;
  left:calc(var(--u) * 2);    /* = .capbody padding-left，见上：保证与文字同中心 */
  right:calc(var(--u) * 4);   /* = .capbody padding-right */
  bottom:calc(var(--u) * 3.5);
  height:calc(var(--u) * 2.5);
  margin:0;
  background:rgba(90,70,35,.38);
  border-radius:calc(var(--u) * 1.25);
  overflow:hidden;
}
.cap .bar>i{
  display:block;height:100%;width:0;
  background-image:url('/qp-icons/official/cap_bar_fg.png');
  background-repeat:repeat-x;
  background-size:auto 100%;
  background-position:left center;
  border-radius:calc(var(--u) * 1.25);
}
/* 长数值单独收小，避免裁字 */
.cap .cval#coins,.cap .cval#advTxt{font-size:calc(var(--u) * 9)}
.cap .cval#swTxt,.cap .cval#expTxt,.cap .cval#visitTxt,.cap .cval#pkTxt{font-size:calc(var(--u) * 9)}
/* 金币的最后更新时刻在 67dp 胶囊里放不下（官方金币胶囊也只放数值），隐藏 */
.cap .cunit#coinsAt{display:none}

/* ---------- 5b. 中部场景层（官方是 3D 宠物；这里放任务队列） ---------- */
.scene{
  /* 内容区（74~281，与资料卡同宽）。高度按内容自适应 —— 原来用 bottom 固定
     撑到页面底部，任务少时下方留大片空白（截图反馈）。
     顶 = --top-scene（= 胶囊区底 + --gap-top），与上面两处间距一致。 */
  left:calc(var(--u) * 74);
  width:calc(var(--u) * 207);
  top:calc(var(--u) * var(--top-scene));
  display:flex;flex-direction:column;
  overflow:hidden;
}
/* 任务队列面板：占据场景层剩余空间，可滚动 */
.qpanel{
  display:flex;flex-direction:column;
  background:rgba(255,255,255,.62);
  border-radius:calc(var(--u) * 14);
  padding:calc(var(--u) * 8) calc(var(--u) * 9);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
  box-shadow:var(--sh-1);
  overflow:hidden;
  z-index:2;                 /* 面板在下 */
}

/* ---------- 5c. 右功能栏（官方 x=414，y=335/405/495，50×70） ---------- */
.funcbar{
  position:absolute;
  left:calc(var(--u) * 294);
  top:calc(var(--u) * 190);
  width:calc(var(--u) * 50);
  z-index:5;
  display:flex;flex-direction:column;
  gap:calc(var(--u) * 20);   /* 官方：两段之间空 20dp */
}
/* 分组容器承载磨砂底与圆角：组内按钮无缝连成一个胶囊体。
   官方 View 树就是两段（上段 feed+shower 共用容器、下段 friend 独立）。 */
.fb-group{
  display:flex;flex-direction:column;
  background:rgba(255,255,255,.55);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
  border-radius:calc(var(--u) * 25);
  box-shadow:var(--sh-1);
  overflow:hidden;
}
.fab{
  width:calc(var(--u) * 50);height:calc(var(--u) * 70);
  border:0;border-radius:0;background:transparent;box-shadow:none;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:calc(var(--u) * 2);
  cursor:pointer;color:var(--button-fg,var(--strong));
  font-size:calc(var(--u) * 10);padding:0;
  transition:transform .12s;
}
.fab .fi{
  /* 图标（SVG）与文字；原来的字符图标是 font-size，改成图片后需要显式尺寸 */
  width:calc(var(--u) * 22);height:calc(var(--u) * 22);
  object-fit:contain;display:block;
}
.fab .ft{font-size:calc(var(--u) * 10);line-height:1;color:var(--strong);font-weight:600}
.fab:active{transform:scale(.94)}
.fab:disabled{opacity:.45;cursor:default}
.funcbar .fab:nth-child(3){margin-top:calc(var(--u) * 20)}
/* 停止/画面图标是 SVG，颜色已内嵌（暖橙系），不再用 color 改色 */

/* ---------- 5d. 底部入口（官方 y=728 居中，50×54） ---------- */
/* 底部弧形面板（官方底部是一块满宽上凸的浅米色圆台：
     顶点 y≈669dp 宽 48dp -> y=717dp 撑满全宽 359dp -> 延伸到画布底）。
     用 border-radius 画上凸弧：顶部两侧大圆角。 */
.deck{
  left:0;
  width:100%;
  bottom:0;
  top:auto;
  height:calc(var(--u) * 135);   /* 669 -> 804dp */
  background:linear-gradient(180deg,#EFDCC6 0%,#E4CFB9 22%,#E0C9B2 100%);
  border-radius:50% 50% 0 0 / calc(var(--u) * 52) calc(var(--u) * 52) 0 0;
  box-shadow:0 calc(var(--u) * -1) calc(var(--u) * 3) rgba(120,85,30,.08);
  z-index:1;
}
.drawer{
  /* 底部信息：坐在 .deck 弧形面板上，水平居中、贴近弧顶（官方图标在弧顶下方）。
     deck 高 135dp（669~804），图标中心约在 700dp -> 距 deck 顶 31dp。 */
  left:50%;
  transform:translateX(-50%);
  top:calc(var(--u) * 22);
  width:calc(var(--u) * 62);
  display:flex;flex-direction:column;align-items:center;
  gap:calc(var(--u) * 2);
  background:transparent;box-shadow:none;
  text-align:center;
}
.dico{
  width:calc(var(--u) * 44);height:calc(var(--u) * 46);
  object-fit:contain;flex:none;
}
.dcol{display:flex;flex-direction:column;align-items:center;gap:calc(var(--u) * 1)}
.dtitle{
  display:flex;align-items:center;justify-content:center;gap:calc(var(--u) * 3);
  font-weight:680;font-size:calc(var(--u) * 14);color:var(--strong);white-space:nowrap;
  /* 状态灯不参与排版（绝对定位到文字左侧）。
     否则 flex 居中会把「绿点+文字」整组居中，文字被灯宽推偏约半个灯宽
     （实测「打工中」偏右 5px，真机上看就是没对齐图标中心）。 */
  position:relative;
}
.dtitle .dot{
  position:absolute;left:calc(var(--u) * -12);
  top:50%;transform:translateY(-50%);
}
.drow{display:contents}



.dhint{display:none}
.dsub{
  font-size:calc(var(--u) * 9);color:var(--sub);
  font-variant-numeric:tabular-nums;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;max-width:100%;
}
.dmeta{display:none}
.dot{width:calc(var(--u) * 7);height:calc(var(--u) * 7);border-radius:50%;background:#9ca3af;flex:none}
.dot.on{background:var(--ok)}
.dot.off{background:#ef4444}
.saveMsg{font-size:calc(var(--u) * 10);text-align:center;margin-top:calc(var(--u) * 2);min-height:0;color:var(--ok)}
.saveMsg.err{color:#b45309}

/* ---------- 6. 通用卡片与文字 ---------- */
.card{
  background:var(--card);border:1px solid var(--line);
  border-radius:var(--r-xl);padding:12px 14px;
}
.card h2{
  font-size:12px;color:var(--sub);font-weight:600;
  margin:0 0 8px;letter-spacing:.03em;
}
.workline{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.workline .big{font-size:22px;font-weight:680;font-variant-numeric:tabular-nums;color:var(--strong)}
.workline .hint{font-size:12px;color:var(--sub)}
.subline{margin-top:6px;font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums}
.subh{font-size:11px;color:var(--sub);margin:14px 0 4px;letter-spacing:.03em}
.err{color:#b45309;font-size:12px}

/* 数值瓦片 */
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.tile{
  background:var(--card);border:1px solid var(--line);
  border-radius:var(--r-lg);padding:10px 8px;text-align:center;
}
.tile .v{font-size:19px;font-weight:680;font-variant-numeric:tabular-nums;color:var(--strong)}
.tile .k{font-size:11px;color:var(--sub);margin-top:2px}

/* 进度条（通用） */
.bar{height:4px;background:var(--track);border-radius:2px;overflow:hidden;margin-top:7px}
.bar>i{display:block;height:100%;background:var(--accent);width:0;border-radius:2px}
/* 学习/打工合并瓦片：两段叠加（学习橙在左、打工蓝紧随）。
   类名用 bar-split，不能用 sw（.sw 是设置页开关）。
   注意：这里是**双段**条，两段必须能区分（学习=橙 / 打工=蓝），
   所以不能直接用官方那张橙色条纹图（两段会同色）。
   **不要用 background-blend-mode:multiply 染色** —— 实测橙色条纹 × 蓝色
   = (14,93,0) 暗绿（multiply 是逐通道相乘，橙的 R=255 保留、G/B 被压掉）。
   正解：预先生成一张**蓝色条纹变体**素材（cap_bar_fg_blue.png，
   由 cap_bar_fg.png 做 HSV 色相旋转 +171° 得到，条纹形状/明暗完全保留），
   两段各用一张图。 */
.bar.bar-split{display:flex}
.bar.bar-split>i{flex:none;border-radius:0}
.bar.bar-split>i#swBarSchool{
  background-image:url('/qp-icons/official/cap_bar_fg.png');
}
.bar.bar-split>i#swBarWork{
  background-image:url('/qp-icons/official/cap_bar_fg_blue.png');
}
.bar.bar-split>i:last-child{border-top-right-radius:calc(var(--u) * 1.25);border-bottom-right-radius:calc(var(--u) * 1.25)}

/* ---------- 7. 任务队列 ---------- */
.qcard{border-radius:var(--r-xl)}
.qhead{
  display:flex;align-items:center;
  margin-bottom:calc(var(--u) * 4);
}
/* 面板标题（原状态行已移除，只留固定标题） */
.qtitle{
  font-size:calc(var(--u) * 11);font-weight:600;
  color:var(--sub);letter-spacing:.06em;
}
/* 标题右侧的收尾队列提示（原为单独一行，现并到标题行） */
.qpend{
  margin-left:auto;
  font-size:calc(var(--u) * 10.5);
  color:var(--sub);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  max-width:60%;
}
.qpend .run{color:var(--accent);font-weight:600}
.qgrp{font-size:10.5px;color:var(--sub);letter-spacing:.06em;margin:10px 0 2px}
.tasklist .mrow{
  display:flex;gap:10px;padding:9px 2px;
  border-top:1px solid var(--line);align-items:center;
}
.tasklist .mrow:first-child,.mrow:first-child{border-top:0}
.mcb{
  width:calc(var(--u) * 20);height:calc(var(--u) * 20);
  border-radius:calc(var(--u) * 6);background:rgba(0,0,0,.10);
  flex:none;cursor:pointer;position:relative;user-select:none;
  margin-left:auto;   /* 移到行尾（原"已启用/已禁用"的位置） */
}
.mcb.on{background:var(--accent)}
.mcb.on::after{
  content:"✓";position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;color:#fff;font-size:13px;font-weight:700;
}
.qico{width:18px;height:18px;flex:none;border-radius:5px;margin:0 -3px}
.mname{font-weight:650;font-size:14px;color:var(--strong)}
.mname.off{color:var(--sub);font-weight:500}
.mdet{
  margin-left:auto;font-size:12.5px;color:var(--sub);
  font-variant-numeric:tabular-nums;text-align:right;
}
.mdet .run{color:var(--accent);font-weight:650}
.mdet .off-t{color:#a89478}
.ttag{
  font-size:9.5px;padding:1px 5px;border-radius:4px;
  border:1px solid var(--line);color:var(--sub);flex:none;margin-left:2px;
}
.mrow.done{opacity:.6}
.mrow.done .mname{text-decoration:line-through;color:var(--sub);font-weight:500}
.mrow.run .mname{color:var(--accent)}
.tasklist .row{
  display:flex;justify-content:space-between;align-items:center;
  padding:8px 0;border-top:1px dashed var(--line);font-size:14px;
}
.tasklist .row:first-child{border-top:0}
.tasklist .t{display:flex;gap:8px;align-items:center}
.tasklist .nx{font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums}
.tasklist .qgrp+.row{border-top:0}
.tasklist .row.run .t>span:first-child{font-weight:650;color:var(--accent)}
.chip{
  font-size:11px;padding:2px 8px;border-radius:999px;
  background:rgba(255,255,255,.6);color:var(--sub);flex:none;
}
.chip.ready{background:rgba(100,201,0,.18);color:#3F7A00}
.chip.wait{background:rgba(255,153,15,.2);color:#9A5B00}
.chip.run{background:var(--accent);color:#fff;font-weight:600}
.chip.off{background:rgba(0,0,0,.06);color:#a89478}
.chip.done{background:rgba(0,0,0,.05);color:#8A7A62}

/* ---------- 8. 实时画面页 ---------- */
.shotpage{display:flex;flex-direction:column;gap:10px}
.scenecard{position:relative;display:block;border-radius:var(--r-xl);overflow:hidden}
.shotpage .scenecard{background:#0e0f12;box-shadow:var(--sh-2)}
.shotpage #shotLink{display:block;width:100%;overflow:hidden;background:transparent}
.shotpage #phoneShot{
  display:block;width:100%;height:auto;object-fit:contain;
  background:transparent;min-height:120px;
}
.shotpage-ctl{display:flex;gap:8px;flex:none;justify-content:center}
.shotpage-ctl .savebtn{text-decoration:none;display:inline-block;text-align:center}
.shotmeta{
  position:absolute;top:8px;right:10px;z-index:2;
  font-size:10.5px;color:#fff;background:rgba(0,0,0,.34);
  border-radius:999px;padding:2px 9px;backdrop-filter:blur(4px);
}
.scenecard.duoshot{flex:1;min-height:0;padding:0;background:transparent;border:0;box-shadow:none}
.scenecard #shotLink{flex:1;min-height:0;width:100%;display:block;line-height:0;overflow:hidden}
.scenecard #phoneShot{
  display:block;width:100%;height:100%;
  object-fit:cover;object-position:center center;background:transparent;
}
.scenecard .shoterr{padding:0 12px;font-size:11px;flex:none}
.shoterr{font-size:11px}
.shoterr:empty{display:none}

/* 画面加载/失败提示：浮层小胶囊，不铺底色（否则盖住房间背景） */
#shotLink.loading::before{
  content:"画面加载中…";position:absolute;left:50%;top:50%;
  transform:translate(-50%,-50%);font-size:11.5px;color:var(--sub);
  background:rgba(255,255,255,.8);border-radius:999px;
  padding:4px 12px;white-space:nowrap;
}
#shotLink.loading.failed::before{content:"获取失败，稍后自动重试…";color:#b45309}
#shotLink.loading #phoneShot{visibility:hidden}

/* ---------- 9. 表单与控件（照 QQ 宠物设置页：浅灰底 + 白卡分组） ---------- */
/* 分组容器 = 一张白色圆角卡片；组标题（.fsect）在卡片【外】上方 */
.form .fgrp{margin:calc(var(--u) * 14) 0 0}
.form .fgrp:first-child{margin-top:calc(var(--u) * 8)}
/* 卡片外的小标题（QQ 宠物设置页风格：灰色小字，在卡片上方） */
.form .fsect{
  font-size:calc(var(--u) * 11.5);
  color:#8A8A8E;
  padding:0 calc(var(--u) * 16) calc(var(--u) * 6);
  user-select:none;
}
.form .fsect::before{content:none}
/* 白色圆角卡片 */
.form .fsec{
  background:#fff;
  border-radius:calc(var(--u) * 12);
  margin:0 calc(var(--u) * 12);
  overflow:hidden;
  box-shadow:0 1px 2px rgba(0,0,0,.04);
}
.form .frow{
  display:flex;justify-content:space-between;align-items:center;
  gap:calc(var(--u) * 10);
  min-height:calc(var(--u) * 50);
  padding:calc(var(--u) * 10) calc(var(--u) * 16);
  border-top:1px solid #F0F0F2;
  font-size:calc(var(--u) * 14);
}
.form .frow:first-child{border-top:0}
.form .k{color:#1C1C1E;font-size:calc(var(--u) * 14);flex:none}
.form .u{color:#8A8A8E;font-size:calc(var(--u) * 12);margin-left:calc(var(--u) * 3)}
.form select,.form input[type=number],.form input[type=text]{
  border:0;background:transparent;color:#8A8A8E;
  font-size:calc(var(--u) * 14);text-align:right;
  padding:calc(var(--u) * 4) 0;max-width:58%;
}
.form input[type=number]{width:calc(var(--u) * 70)}
.form input[type=text]{width:calc(var(--u) * 140)}
.form .two{display:flex;gap:calc(var(--u) * 6)}
.form .two input{width:calc(var(--u) * 56)}
/* 行内右侧控件组：状态文字在左、按钮靠右（与开关行的右对齐一致）。
   不加 .ctrl 时按钮会被 frow 的 space-between 摊到中间（通知页"测试"行实测） */
.form .ctrl{display:flex;align-items:center;gap:calc(var(--u) * 8);min-width:0}
.form .ctrl #notifyTestMsg{
  color:var(--sub);font-size:calc(var(--u) * 12);
  text-align:right;overflow-wrap:anywhere;
}
/* 说明条目：标签左、正文右，两列对齐（原来是整段灰字墙，三段糊在一起） */
.form .noterow{
  display:flex;gap:calc(var(--u) * 12);
  padding:calc(var(--u) * 10) calc(var(--u) * 16);
  border-top:1px solid #F0F0F2;
}
.form .noterow:first-child{border-top:0}
.form .noterow .nt{
  color:#1C1C1E;font-size:calc(var(--u) * 13);font-weight:500;
  flex:none;width:calc(var(--u) * 62);
}
.form .noterow .nb{
  color:var(--sub);font-size:calc(var(--u) * 12.5);line-height:1.65;
  flex:1;min-width:0;
}
.form .noterow .nb b{color:#1C1C1E;font-weight:600}

/* 开关 */
.sw{
  position:relative;width:46px;height:26px;border-radius:13px;
  background:#d9dce3;border:none;transition:.2s;flex:none;cursor:pointer;
}
.sw.on{background:var(--accent)}
.sw::after{
  content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;
  border-radius:50%;background:#fff;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.25);
}
.sw.on::after{left:23px}

/* 按钮 */
.savebtn{
  width:100%;margin-top:12px;border:none;border-radius:var(--r-md);
  background:var(--accent);color:#fff;font-size:15px;font-weight:600;
  padding:11px;letter-spacing:.02em;
}
.savebtn:disabled{opacity:.55}
.savebtn.ghost{background:transparent;color:var(--accent);border:1.5px solid var(--accent)}
.btnrow2{display:flex;gap:8px;margin-top:10px}
.btnrow2 .savebtn{margin-top:0}
.minibtn{
  border:1px solid var(--line);background:#FFFDF8;border-radius:var(--r-sm);
  padding:2px 8px;font-size:11px;color:var(--sub);
}
.minibtn.on{background:var(--accent);border-color:var(--accent);color:#fff}
.saveMsg{font-size:12px;text-align:center;margin-top:6px;min-height:16px;color:var(--ok)}
.saveMsg.err{color:#b45309}

/* 运行信息（只读） */
.cfg .row{
  display:flex;justify-content:space-between;gap:calc(var(--u) * 10);
  /* 左右内距与 .form .frow 一致（16dp）：.form .fsec 卡片本身没有 padding，
     靠行自带内距撑开。原来写 padding:7px 0 时内容贴到卡片边缘（实测踩坑）。 */
  padding:calc(var(--u) * 9) calc(var(--u) * 16);
  border-top:1px solid #F0F0F2;font-size:calc(var(--u) * 14);
}
.cfg .row:first-child{border-top:0}
.cfg .k{color:#1C1C1E;flex:none}
.cfg .v{text-align:right;color:#8A8A8E;word-break:break-word}

/* ---------- 10. 日志 ---------- */
.logctl{display:flex;gap:calc(var(--u) * 8);align-items:center;flex-wrap:wrap}


/* 工具栏控件：浅灰底、圆角，聚焦描边（与设置页控件观感一致） */
.logctl input{
  flex:1;min-width:calc(var(--u) * 110);
  border:1px solid #E8E8EC;border-radius:calc(var(--u) * 9);
  padding:calc(var(--u) * 7) calc(var(--u) * 11);
  font-size:calc(var(--u) * 13);background:#F7F7F9;color:#1C1C1E;
  -webkit-appearance:none;
}
.logctl input:focus{outline:none;border-color:var(--accent);background:#fff}
.logctl button{
  border:1px solid #E8E8EC;background:#fff;border-radius:calc(var(--u) * 9);
  padding:calc(var(--u) * 7) calc(var(--u) * 13);
  font-size:calc(var(--u) * 12.5);color:#4A4A4F;cursor:pointer;
}
.logctl button:active{background:#F0F0F3}
.logctl button.on{background:var(--accent);border-color:var(--accent);color:#fff}
/* 日志元信息（当天日志文件名 + 当前行数）：放在「输出」白卡内、日志框下方。
   **不要挂回顶栏 .navmeta** —— .navtitle 是 position:absolute;left:50% 绝对居中，
   .navmeta 是 margin-left:auto 贴右，390px 下两者必然重叠（实测标题压住 meta 开头，
   糊成「实时日志2026-09-20.log · 250 行」）。日志框自带 --u*14 左右内距，
   这里用同样的内距让注脚与日志文字左右对齐；字号走 --u 随宽度缩放。
   注：截图验证须走 iframe 承载（headless Chrome 最小视口 500px，
   直接 --window-size=390 是按 500px 排版后裁右侧，见
   qqpet_assets/web/INTEGRATION.md 5.1）。 */
.logfoot{
  padding:0 calc(var(--u) * 14);
  text-align:right;
  font-size:calc(var(--u) * 11.5);color:#8A8A8E;
  font-variant-numeric:tabular-nums;
}
.logfoot:empty{display:none}
pre#logbox{
  /* 白底深字（用户要求）—— 对比度约 15:1；等宽字体便于对齐时间戳 */
  /* 高度按可视区减去页头等固定占用；同样要扣掉顶部安全区（body 的 padding-top
     已经吃掉一块），否则日志框底部会伸到折叠线以下。顺序 vh 在前、dvh 在后。 */
  height:calc(100vh - 320px - env(safe-area-inset-top, 0px));
  height:calc(100dvh - 320px - env(safe-area-inset-top, 0px));
  min-height:calc(var(--u) * 220);
  overflow:auto;background:transparent;color:#1C1C1E;
  font:calc(var(--u) * 12.5)/1.75 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  padding:calc(var(--u) * 12) calc(var(--u) * 14);
  white-space:pre-wrap;word-break:break-all;margin:0;
  /* 内层滚动区同样不回弹（原为 contain —— contain 只是不往父级传递，自身照弹） */
  overscroll-behavior:none;
}
/* 异常截图缩略图 */
.thumbs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.thumbs a{display:block;border:1px solid var(--line);border-radius:var(--r-sm);overflow:hidden;position:relative}
.thumbs img{width:100%;display:block}
.thumbs .cap{
  position:absolute;left:0;right:0;bottom:0;height:auto;
  background:rgba(33,29,24,.72);color:#fff;font-size:10px;
  padding:2px 6px;text-align:center;
}

/* ---------- 11. 冒险页 ---------- */
.advchart{
  width:100%;height:auto;display:block;margin:6px 0 0;
  border:1px solid var(--line);border-radius:var(--r-sm);
  background:#FFFDF8;cursor:crosshair;touch-action:pan-y;
}
.advcap{font-size:11px;color:var(--sub);margin:8px 0 0;letter-spacing:.03em}
.advtip{
  font-size:12.5px;color:var(--text);background:#FFFDF8;
  border:1px solid var(--line);border-radius:var(--r-sm);
  padding:7px 10px;margin-top:8px;font-variant-numeric:tabular-nums;min-height:18px;
}
.advchips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.advlist{
  max-height:44vh;overflow:auto;margin-top:6px;
  border:1px solid var(--line);border-radius:var(--r-sm);
  background:#FFFDF8;overscroll-behavior:none;
}
.advlist .arow{
  display:flex;justify-content:space-between;align-items:baseline;gap:8px;
  padding:7px 10px;border-top:1px dashed var(--line);font-size:13px;
  font-variant-numeric:tabular-nums;
}
.advlist .arow:first-child{border-top:0}
.advlist .ai{
  color:var(--sub);font-size:12px;flex:none;width:112px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.advlist .ag{
  color:var(--sub);font-size:11.5px;flex:1;min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right;
}
.advlist .av{font-weight:650;flex:none;min-width:44px;text-align:right}
.advlist .pos{color:var(--gold)}
.advlist .neg{color:#C0392B}
.advlist .zero{color:#a89478}

/* ---------- 12. 职业页 ---------- */
.planbars .pb{margin-top:10px}
.planbars .pb .t{
  display:flex;justify-content:space-between;font-size:12px;
  color:var(--sub);margin-bottom:3px;font-variant-numeric:tabular-nums;
}
.planbars .bar{margin-top:0}
.plansteps .st{
  display:flex;justify-content:space-between;gap:8px;padding:7px 0;
  border-top:1px dashed var(--line);font-size:13px;align-items:baseline;
}
.plansteps .st:first-child{border-top:0}
.plansteps .dot2{flex:none;font-size:12px}
.plansteps .tx{flex:1;min-width:0}
.plansteps .pr{color:var(--sub);font-size:11.5px;flex:none;font-variant-numeric:tabular-nums}
.plansteps .st.done .tx{color:var(--sub)}
.plansteps .st.done .pr{color:#a89478}
.plansteps .st.cur{background:rgba(255,153,15,.12);border-radius:var(--r-sm);padding-left:6px;padding-right:6px}
.plannote{font-size:11px;color:var(--sub);margin-top:6px;line-height:1.5}
.watchbox .wrow{
  display:flex;justify-content:space-between;gap:8px;font-size:12.5px;
  padding:6px 0;border-top:1px dashed var(--line);align-items:baseline;
}
.watchbox .wrow:first-child{border-top:0}
.watchbox .wstate{font-size:12px;color:var(--sub);padding:2px 0 4px}
.watchbox .wbadge{color:var(--ok);font-weight:600}
.planlines{display:grid;grid-template-columns:1fr 1fr;gap:6px 8px}
.planlines .ln{
  display:flex;justify-content:space-between;align-items:center;
  font-size:12.5px;padding:5px 8px;border:1px solid var(--line);border-radius:var(--r-sm);
}
.planlines .chipx{
  font-size:10.5px;padding:1px 6px;border-radius:999px;
  background:rgba(0,0,0,.06);color:#a89478;margin-left:4px;
}
.planlines .chipx.ok{background:rgba(100,201,0,.18);color:#3F7A00}
.plinedit{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.plinedit label{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--sub)}
.plinedit input{
  border:1px solid var(--line);border-radius:var(--r-sm);padding:6px;
  font-size:14px;text-align:center;background:#FFFDF8;color:var(--text);width:100%;
}
.planeditrow{
  grid-column:span 3;display:flex;gap:10px;align-items:center;
  flex-wrap:wrap;font-size:12.5px;margin-top:2px;
}

/* ---------- 13. 页脚 ---------- */

/* ---------- 14. 响应式 ---------- */
/* 窄屏（≤639px）：侧栏仍保留，收紧尺寸 */
@media(max-width:639px){
  .tasklist .mrow{gap:8px;padding:8px 0}
  .mcb{width:18px;height:18px;border-radius:5px}
  .mcb.on::after{font-size:11px}
  .mname{font-size:13px;white-space:nowrap}
  .mdet{font-size:10.5px;white-space:nowrap}
  .ttag{display:none}
}
/* 小屏（≤360px）：进一步收紧 */
@media(max-width:360px){
  .card{padding:10px 11px;border-radius:var(--r-lg)}
  .workline .big{font-size:20px}
  .tile .v{font-size:17px}
  .grid{gap:6px}
  /* 顶部三块间距一起收紧（仍保持三处等距）。
     不要再写成 .caps{gap:6px} —— 那只改胶囊两行的间距，
     资料卡→胶囊、胶囊→面板仍是 12，三处又不相等了。 */
  :root{--gap-top:10}
  .cap{padding:0 8px}
  .cap .cval{font-size:12.5px}
  .advlist .ai{width:96px}
  .form select,.form input[type=number],.form input[type=text]{max-width:52%}
  .form input[type=text]{width:126px}
}
/* 平板/中窗（≥640px）：多列 */
@media(min-width:640px){
  .grid{grid-template-columns:repeat(6,minmax(0,1fr))}
  .thumbs{grid-template-columns:repeat(4,minmax(0,1fr))}
}
/* 桌面（≥920px）：加宽版心 */
@media(min-width:920px){
  .grid{grid-template-columns:repeat(6,minmax(0,1fr))}
  .thumbs{grid-template-columns:repeat(4,minmax(0,1fr))}
  pre#logbox{height:58vh}
}

/* ---------- 15. 深色模式 ---------- */
/* 官方【没有独立深色主题】—— 深色下只是把房间背景换成偏暗的版本，
   组件（胶囊/圆钮/资料卡/文字）颜色**完全不变**。
   所以这里只切背景图，不改任何 token，保证与官方一致。 */
@media(prefers-color-scheme:dark){
  :root{
    /* 只替换背景图；其余 token 一律沿用浅色值 */
    --qp-room-main:url('/qp-icons/bg/room-main-dark.jpg');
    --qp-room-feed:url('/qp-icons/bg/room-feed-dark.jpg');
    --qp-room-shower:url('/qp-icons/bg/room-shower-dark.jpg');
    --qp-room-record:url('/qp-icons/bg/room-record-dark.jpg');
    /* 状态栏采样色是唯一例外：它要跟背景图走（深色房间顶部明显更暗），
       否则深色模式下状态栏还是浅色那条，比背景亮一截。取各 -dark 图顶部实测色。 */
    --qp-sb-main:#A9722D;
    --qp-sb-feed:#C39145;
    --qp-sb-shower:#A3651D;
    --qp-sb-record:#C18C3A;
  }
}

/* 房间背景按 `html[data-scene]` 切换。
   注意：必须写在 html 上 —— 背景图作用在 html 元素，而 CSS 变量只向下继承，
   写在 body 上的覆写对父级 html 无效（曾踩坑：data-scene 变了但背景不动）。 */
html[data-scene="feed"]{--qp-room:var(--qp-room-feed);--qp-statusbar:var(--qp-sb-feed)}
html[data-scene="shower"]{--qp-room:var(--qp-room-shower);--qp-statusbar:var(--qp-sb-shower)}
html[data-scene="record"]{--qp-room:var(--qp-room-record);--qp-statusbar:var(--qp-sb-record)}

/* 内页（非总览）把 html 背景换成白色。
   原因：body 的 padding-top 给状态栏留了安全区，露出的是 html 的底色 ——
   总览页露出房间暖色图是对的，但内页顶栏是白的，露出暖色就很突兀
   （真机 PWA 全屏实测：通知页顶部一条暖黄）。内页统一白底与顶栏衔接。 */
html[data-page]:not([data-page="main"]){background-image:none;background-color:#fff}

/* ---------- 设置页：照 QQ 宠物设置页（iOS 风格浅灰底 + 白卡分组） ---------- */
section[data-page="set"]{
  background:#F2F2F7;
  /* 必须减掉顶部安全区：body 已经为状态栏加了 padding-top，
     这里若用裸 100dvh，两者相加就比可视区高出一个安全区（要往下滑才见底）。
     与 .home 同一套写法，顺序同样是 vh 在前、dvh 在后。 */
  min-height:calc(100vh - env(safe-area-inset-top, 0px));
  min-height:calc(100dvh - env(safe-area-inset-top, 0px));
  margin:0;padding:0 0 calc(var(--u) * 20);
  border:0;border-radius:0;box-shadow:none;
}
section[data-page="set"] > h2{
  display:none;   /* 原「设置 保存后下一轮调度生效」标题移除（改用分组标题） */
}



/* ---------- 设置页（照 QQ 宠物：顶栏 + 分组卡片） ---------- */
/* 顶部导航栏：白底、返回箭头在左、标题居中 */
.navhead{
  position:relative;
  display:flex;align-items:center;
  height:calc(var(--u) * 48);
  background:#fff;
  border-bottom:1px solid #EDEDF0;
  padding:0 calc(var(--u) * 12);
}
.navhead .backbtn{
  width:calc(var(--u) * 34);height:calc(var(--u) * 34);
  border:0;background:transparent;padding:0;cursor:pointer;
  display:flex;align-items:center;justify-content:center;flex:none;
}
.navhead .backbtn img{width:62%;height:62%;object-fit:contain}
.navhead .backbtn:active{opacity:.55}
.navhead .navtitle{
  position:absolute;left:50%;transform:translateX(-50%);
  font-size:calc(var(--u) * 17);font-weight:600;color:#1C1C1E;
  white-space:nowrap;
}
/* 一级列表：小节（标题在卡外 + 白卡内多行） */
.msec{margin-top:calc(var(--u) * 16)}
.msec:first-child{margin-top:calc(var(--u) * 10)}
.menurow{cursor:pointer}
.menurow:active{background:#F5F5F7}
.menurow .chev{
  color:#C7C7CC;font-size:calc(var(--u) * 18);line-height:1;
  margin-left:auto;padding-left:calc(var(--u) * 6);
}

/* 顶栏右侧的附加信息（如日志的条数、冒险的场次） */
.navhead .navmeta{
  margin-left:auto;
  font-size:calc(var(--u) * 11);
  color:#8A8A8E;font-weight:400;
  white-space:nowrap;
  padding-right:calc(var(--u) * 4);
}

/* ---------- 内页统一排版（照设置页：浅灰底 + 白卡分节） ---------- */
main > section[data-page]:not([data-page="main"]){
  background:#F2F2F7;
  /* 同 section[data-page="set"]：减掉顶部安全区，避免比可视区高出一截 */
  min-height:calc(100vh - env(safe-area-inset-top, 0px));
  min-height:calc(100dvh - env(safe-area-inset-top, 0px));
  margin:0;padding:0 0 calc(var(--u) * 24);
  border:0;border-radius:0;box-shadow:none;
}
/* 分节：小标题（卡外） + 白卡（卡内） */
.pgsec{margin-top:calc(var(--u) * 14)}
.pgsec:first-of-type{margin-top:calc(var(--u) * 10)}
.pgsec > .pgsec-t{
  font-size:calc(var(--u) * 11.5);color:#8A8A8E;
  padding:0 calc(var(--u) * 16) calc(var(--u) * 6);
}
.pgsec > .pgsec-c{
  background:#fff;
  border-radius:calc(var(--u) * 12);
  margin:0 calc(var(--u) * 12);
  padding:calc(var(--u) * 12) calc(var(--u) * 14);
  box-shadow:0 1px 2px rgba(0,0,0,.04);
  overflow:hidden;
}
/* 卡内的标题/图表不再自带边框与底色 */
.pgsec-c .advchart,.pgsec-c .advlist,.pgsec-c .advtip{background:transparent;border:0}
.pgsec-c .advcap:first-child{margin-top:0}
.pgsec-t + .pgsec-c > .advcap:first-child{margin-top:0}
/* 内页里原橙色竖条小节标题 -> 灰色小字 */
main > section[data-page]:not([data-page="main"]) .subh{
  font-size:calc(var(--u) * 11.5);color:#8A8A8E;letter-spacing:0;
  margin:calc(var(--u) * 14) calc(var(--u) * 16) calc(var(--u) * 2);padding:0;
}
/* 内页按钮/提示与卡片对齐 */
main > section[data-page]:not([data-page="main"]) > .savebtn,
main > section[data-page]:not([data-page="main"]) > .btnrow2,
main > section[data-page]:not([data-page="main"]) > .saveMsg,
main > section[data-page]:not([data-page="main"]) > .plannote{
  margin-left:calc(var(--u) * 12);margin-right:calc(var(--u) * 12);
}

/* ---------- 好友名选择器（下拉 + 手输兜底） ---------- */
.fpick{display:flex;align-items:center;justify-content:flex-end;gap:calc(var(--u) * 6);
  flex:1;min-width:0;max-width:62%}
.fpsel{
  border:0;background:transparent;color:#8A8A8E;
  font-size:calc(var(--u) * 14);text-align:right;
  padding:calc(var(--u) * 4) 0;max-width:100%;min-width:0;
  -webkit-appearance:none;appearance:none;   /* 去掉 iOS 默认箭头，保持简洁 */
  text-align-last:right;
}
.fpinput{
  border:0;background:transparent;color:#8A8A8E;
  font-size:calc(var(--u) * 14);text-align:right;
  padding:calc(var(--u) * 4) 0;width:calc(var(--u) * 140);
}
.fpinput.hide{display:none}
</style>
</head>
<body>
<div class="app">
<!-- 左侧竖列圆钮导航（QQ 宠物主页的左侧圆钮位）：图标取自 qqpet_assets，title 做无障碍 -->

<main>

  <section class="card" id="advCard" data-page="adv">
    
      <div class="navhead"><button class="backbtn" data-back="main" title="返回总览"><img src="/qp-icons/official/off_l1_back.png" alt=""></button><span class="navtitle">冒险记录</span><span class="navmeta"><span id="advMeta" style="font-weight:400;font-size:10.5px"></span></span></div>
    <div class="advcap" id="advDateRow" style="margin:-2px 0 4px"></div>
    <div class="workline"><span class="big" id="advNet">--</span><span class="hint" id="advNetHint"></span></div>
    <div class="subline" id="advSub"></div>
    <div class="advchips" id="advChips"></div>
    <div class="advtip" id="advTip">点 / 拖动图表上的点或线，查看当次数据</div>
    <div class="advcap">累计收益曲线（金币）</div>
    <svg class="advchart" id="svgCum" viewBox="0 0 340 84"></svg>
    <div class="advcap">单次收益散点（橙虚线=平均）</div>
    <svg class="advchart" id="svgPts" viewBox="0 0 340 84"></svg>
    <div class="advcap" id="capStats"><span style="color:#16a34a">体力</span> / <span style="color:#0891b2">清洁</span> / <span style="color:#d97706">心情</span>（红虚线=阈值60，红竖线=护理）</div>
    <svg class="advchart" id="svgStats" viewBox="0 0 340 84"></svg>
    <div class="advcap">逐次明细（新→旧） <button class="minibtn on" id="btnAdvAll" style="float:right;margin-top:-2px">全部</button></div>
    <div class="advlist" id="advList"></div>
  </section>

  <section class="card" id="planCard" data-page="plan">
    
      <div class="navhead"><button class="backbtn" data-back="main" title="返回总览"><img src="/qp-icons/official/off_l1_back.png" alt=""></button><span class="navtitle">职业解锁计划</span><span class="navmeta"><span id="planMeta" style="font-weight:400;font-size:10.5px"></span></span></div>
    <div class="planbars" id="planBars"></div>
    <div class="subh">隐藏职业哨兵 <span id="watchMeta" style="font-weight:400;font-size:10.5px"></span></div>
    <div class="watchbox" id="watchBox"></div>
    <div class="subh">进度录入（新号的当前数值，改完点保存）</div>
    <div class="plinedit" id="planEdit"></div>
    <div class="btnrow2"><button class="savebtn" id="btnPlanSave">保存进度</button><button class="savebtn ghost" id="btnPlanSync">🔄 自动识别</button></div>
    <div class="saveMsg" id="planMsg"></div>
    <div class="subh">阶梯路线</div>
    <div class="plansteps" id="planSteps"></div>
    <div class="plannote">隐藏线解锁有概率性：数值达标只进入候选，实际以职业树实测为准（哨兵每节课后检测）。</div>
    <div class="subh">8 线解锁状态（见习 / 初级）<span id="planLinesMeta" style="font-weight:400;font-size:10.5px"></span></div>
    <div class="planlines" id="planLines"></div>
  </section>

  <!-- ===== 总览：照 QQ 宠物首页 1:1 还原 =====
       顶部资料卡（左返回钮 + 头像卡 + 右装扮钮）
       胶囊条（图标 + 数值 + 细分进度）
       中部场景（手机画面 = 我们的"宠物"）
       底部抽屉（当前任务 + 倒计时）
       右侧磨砂功能栏见 .funcbar（在 .app 内） -->
  <section class="home" data-page="main">
    <!-- ==================================================================
         照 QQ 宠物首页 1:1：480dp 画布上的绝对定位浮动层
         坐标逐项取自官方运行时 dump（web/UI_TREE_OFFICIAL.md）：

         左列圆钮 x=20  y=28/86/146/206       (4 个：返回 设置 消息 日记)
         右列圆钮 x=418 y=28/86/146           (3 个：装扮 会员 盲盒)
         资料卡   x=74  y=26  207x46
         胶囊     y=86 三颗：x=74/145/216 各 67x28
                  y=126 两颗：x=74/160
         右功能栏 x=414 y=335/405/495         (feed/shower/friend 各 50x70)
         底部入口 x=215 y=728  50x54          (打工)
         ================================================================== -->

    <!-- 左列圆钮（官方 4 个：返回/设置/消息/日记，x=20 y=28/86/146/206）
         第 1 个是官方首页的"返回"，但本仪表盘的总览页就是根页面、没有上一级，
         且 #tabbar 只在总览页可见 —— 即它永远只在"点了也没反应"的页面出现。
         故置灰禁用（disabled，样式见 .rbtn:disabled）。
         不加 class="on"：JS 仍会按 data-tab===curTab 给它加 .on，但 :disabled
         那条把白环压掉了，所以这里写不写都一样（留着反而误导）。 -->
    <nav class="flt col-l" id="tabbar">
      <button class="rbtn" data-tab="main" title="总览（当前页）" disabled><img src="/qp-icons/official/off_l1_back.png" alt=""></button>
      <button class="rbtn" data-tab="set" title="设置"><img src="/qp-icons/official/off_l2_gear.png" alt=""></button>
      <button class="rbtn" data-tab="log" title="日志"><img src="/qp-icons/official/off_l3_diary.png" alt=""></button>
      <button class="rbtn" data-tab="notify" title="通知"><img src="/qp-icons/official/off_l4_bell.png" alt=""></button>
    </nav>

    <!-- 右列圆钮（官方 3 个：装扮/会员/盲盒，x=418 y=28/86/146） -->
    <nav class="flt col-r" id="tabbar2">
      <button class="rbtn r" data-tab="adv" title="冒险"><img src="/qp-icons/official/cap_compass.png" alt=""></button>
      <button class="rbtn r" data-tab="plan" title="职业"><img src="/qp-icons/official/off_r2_briefcase.png" alt=""></button>
      <!-- `?v=2` 是**换图后的缓存击穿**：/qp-icons/* 走 Cache-Control: max-age=3600，
           不换 URL 的话浏览器会拿旧图渲染整整 1 小时（"改了没变化"就是这么来的）。
           路由用 urlparse 只取 path，查询串被忽略，加 ?v=N 不影响取文件。
           以后每次换这张图，把 N 加一即可。 -->
      <button class="rbtn r" data-tab="shot" title="实时画面"><img src="/qp-icons/ctrl/phone.svg?v=2" alt=""></button>
    </nav>

    <!-- 资料卡（官方 x=74 y=26 207×46） -->
    <div class="flt idcard">
      <span class="avatar" id="schedDot"><img src="/qp-icons/official/mood_smile.png" alt=""></span>
      <div class="idtxt">
        <div class="idname">QQ宠物托管</div>
        <div class="idsub" id="schedTxt">--</div>
      </div>
      <span class="idring" id="idRing"></span>
    </div>

    <!-- 胶囊（官方结构：图标在胶囊【外面】且比胶囊大 22.3 vs 19.7dp；文字在胶囊内居中）
         第1行 = 金币/踩踩/PK   第2行 = 冒险/学习打工/经验
         行内 4dp 间距，行间 40dp 步进（官方 y 94.3 -> 134.3）
         图标 = 官方 pet_home 内联图（static/qp-icons/official/cap_*.png，96×96、内容占比 88%）。
         官方首页胶囊只有「等级/踩踩/金币」三颗，PK 那颗是本仪表盘自加的，图标换过三版：
         ① cap_coin_hand.png（item_coin_hand，手拿爪印金币）—— 与左侧金币胶囊撞脸；
         ② cap_pk.png（pk_logo 拳击手套）—— 素材库里唯一的官方 PK 图标，但用户要的是「PK 字样」；
         ③ **当前 cap_pk_words.png**：游戏内好友宠物页右侧功能栏那颗
            「粉 P + 蓝 K」的 PK 字样图标（`src/locators.py` 里 content-desc="PK" 的那个按钮）。
         全库 OCR 扫描确认素材库里**没有**这个字样图标的文件（ResourceCache 的
         `1009999/9990002/39/9990002/` 里 pk_logo.png 只有拳击手套），它是引擎实时绘制的，
         故从实机截图（runs 抓取，1080×2412）按色相 210-220°/330-340° 抠图 + 连通域去噪生成，
         源图与抠图过程见 qqpet_assets/web/INTEGRATION.md。**换图必须换文件名**：
         /qp-icons/* 带 max-age 缓存，同名覆盖浏览器仍显示旧图。 -->
    <div class="flt caps">
      <div class="caps-row">
        <div class="cap" title="金币"><img class="cico" src="/qp-icons/official/cap_coin.png" alt=""><div class="capbody"><span class="cval" id="coins">--</span><span class="cunit" id="coinsAt"></span></div></div>
        <div class="cap" title="今日踩踩"><img class="cico" src="/qp-icons/official/cap_paw.png" alt=""><div class="capbody"><span class="cval" id="visitTxt">--</span><div class="bar"><i id="visitBar"></i></div></div></div>
        <div class="cap" title="今日PK"><img class="cico" src="/qp-icons/official/pk_words.png" alt=""><div class="capbody"><span class="cval" id="pkTxt">--</span><div class="bar"><i id="pkBar"></i></div></div></div>
      </div>
      <div class="caps-row">
        <div class="cap" title="今日冒险"><img class="cico" src="/qp-icons/official/cap_compass.png" alt=""><div class="capbody"><span class="cval" id="advTxt">--</span></div></div>
        <div class="cap" title="学习/打工" id="capSw"><img class="cico" src="/qp-icons/official/cap_cookie.png" alt=""><div class="capbody"><span class="cval" id="swTxt">--</span><span class="cunit" id="swLbl" hidden></span><div class="bar bar-split"><i id="swBarSchool"></i><i id="swBarWork"></i></div></div></div>
        <div class="cap" title="经验日常"><img class="cico" src="/qp-icons/official/cap_diamond.png" alt=""><div class="capbody"><span class="cval" id="expTxt">--</span></div></div>
      </div>
    </div>

    <!-- 中部场景层（官方是 3D 宠物；此处放任务队列） -->
    <div class="flt scene">
      <div class="qpanel">
        <div class="qhead"><span class="qtitle">任务列表</span><span class="qpend" id="qPend"></span></div>
        <div class="tasklist" id="taskList"></div>
      </div>
    </div>

    <!-- 右功能栏（官方 x=414 y=335/405/495，各 50×70） -->
    <div class="flt funcbar" aria-label="调度器控制">
      <div class="fb-group">
        <button class="fab" id="btnRunnerStart" title="启动调度器"><img class="fi" src="/qp-icons/ctrl/play.svg?v=3" alt=""><span class="ft">启动</span></button>
        <button class="fab stop" id="btnRunnerStop" title="停止调度器"><img class="fi" src="/qp-icons/ctrl/stop.svg?v=3" alt=""><span class="ft">停止</span></button>
      </div>
      <div class="fb-group">
        <button class="fab ghost" id="btnShot" title="刷新画面"><img class="fi" src="/qp-icons/ctrl/refresh.svg?v=3" alt=""><span class="ft">画面</span></button>
      </div>
    </div>

    <!-- 底部入口（官方 x=215 y=728 50×54） -->
    <div class="flt deck">
      <div class="flt drawer" id="runnerCard">
      <img class="dico" id="runnerIcon" src="/qp-icons/official/cap_coin.png" alt="">
      <div class="dcol">
        <div class="dtitle"><span class="dot" id="runnerDot"></span><span id="runnerState">--</span><span class="dhint" id="runnerHint"></span></div>
        <div class="dsub" id="workSub"></div>
        <div class="dsub" id="runnerSub"></div>
        <div class="saveMsg" id="runnerMsg"></div>
      </div>
        <span class="dmeta" id="runnerMeta"></span>
        </div>
      </div>
  </section><!-- /.home -->



  <!-- 实时画面（独立页）：手机画面单独一屏，便于放大看 / 点开原图 -->
  <section class="shotpage" data-page="shot">
    <div class="navhead">
      <button class="backbtn" data-back="main" title="返回总览"><img src="/qp-icons/official/off_l1_back.png" alt=""></button>
      <span class="navtitle">实时画面</span>
    </div>
    <div class="scenecard duoshot" id="shotCardWrap">
      <span class="shotmeta" id="shotMeta"></span>
      <a id="shotLink" class="loading" href="/api/screenshot" target="_blank" rel="noopener"><img id="phoneShot" alt="加载中…"></a>
      <div id="shotErr" class="err shoterr"></div>
    </div>
    <div class="shotpage-ctl">
      <a class="savebtn" id="shotOpen" href="/api/screenshot" target="_blank" rel="noopener">在新标签打开原图</a>
    </div>
  </section>




  <section class="card" data-page="notify">
    
      <div class="navhead"><button class="backbtn" data-back="main" title="返回总览"><img src="/qp-icons/official/off_l1_back.png" alt=""></button><span class="navtitle">通知</span></div>
    <div class="form" id="notifyForm"></div>
    <div class="saveMsg" id="notifySaveMsg"></div>
  </section>

  <section class="card" data-page="set">
    <!-- 第一级：分类列表（点进某项 -> 第二级该分类的设置项） -->
    <div id="setIndex">
      <div class="navhead">
        <button class="backbtn" data-back="main" title="返回总览"><img src="/qp-icons/official/off_l1_back.png" alt=""></button>
        <span class="navtitle">设置</span>
      </div>
      <div class="form" id="setMenu"></div>
      <!-- 小号工具人 + 运行信息：只在一级列表显示。
           包在 .form 容器内 —— 卡片样式（.form .fgrp/.fsect/.fsec/.frow）
           都要求 .form 祖先，否则样式全失效、内容贴到屏幕边缘（实测踩坑）。 -->
      <div class="form">
        <div class="fgrp">
          <div class="fsect">小号工具人</div>
          <div class="fsec">
            <div class="frow"><span class="k">大号名称</span><input type="text" id="txtAltMain" placeholder="大号的主人昵称或宠物名（服务端匹配）"></div>
            <div class="frow"><span class="k">一键配置小号</span><button class="minibtn" id="btnAltPreset">应用小号预设</button></div>
          </div>
        </div>
        <div class="saveMsg" id="presetMsg"></div>
        <div class="fgrp">
          <div class="fsect">运行信息（只读）</div>
          <div class="fsec"><div class="cfg" id="cfgList"></div></div>
        </div>
      </div>
    </div>
    <!-- 第二级：某个分类的设置项 -->
    <div id="setDetail" class="hide">
      <div class="navhead">
        <button class="backbtn" id="btnSetBack" title="返回设置列表"><img src="/qp-icons/official/off_l1_back.png" alt=""></button>
        <span class="navtitle" id="setDetailTitle">设置</span>
      </div>
      <div class="form" id="setForm"></div>
      <div class="saveMsg" id="saveMsg"></div>
    </div>
</section>

  <section class="card" data-page="log">
    <div class="navhead">
      <button class="backbtn" data-back="main" title="返回总览"><img src="/qp-icons/official/off_l1_back.png" alt=""></button>
      <span class="navtitle">实时日志</span>
    </div>
    <div class="pgsec">
      <div class="pgsec-t">工具栏</div>
      <div class="pgsec-c">
        <div class="logctl">
          <button id="btnAuto" class="on">自动滚动</button>
          <input id="logFilter" placeholder="过滤关键字…">
        </div>
      </div>
    </div>
    <div class="pgsec">
      <div class="pgsec-t">输出</div>
      <div class="pgsec-c">
        <pre id="logbox">加载中…</pre>
        <div class="logfoot" id="logMeta"></div>
      </div>
    </div>
    <div class="pgsec" id="shotCard">
      <div class="pgsec-t">异常截图（自动保存）</div>
      <div class="pgsec-c">
        <div class="thumbs" id="shots"></div>
      </div>
    </div>
  </section>


</main>

</div><!-- /.app -->


<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const TASKNAME={care:'护理',school:'学习',friend_care:'好友护理',gift_bag:'福袋',hire_friend:'雇佣好友',adventure:'冒险',visit:'踩踩',pk:'PK',work:'打工'};
// 任务 -> 图标文件名（static/qp-icons/ 下，必须用 colored/ 里存在的名字）
const TASKICON={care:'soap',school:'logo_study',friend_care:'emoji',gift_bag:'coin',
  hire_friend:'logo_work',adventure:'logo_adventure',visit:'logo_hangout',pk:'logo_pk',work:'logo_work'};

// 底部状态卡的图标：跟着「当前在干什么」换（冒险/学习/打工各一个图标）。
// 图标全部取自官方素材库，两种来源别混：
//   /qp-icons/<name>-48@2x.png     —— colored/ 那套（书本/香皂/金币/爪印）
//   /qp-icons/official/<name>.png  —— 首页胶囊/右栏那套（爪印金币、PK 字样、指南针）
// 键 = 状态名，两个来源互补：work_eta.kind（上课/打工/冒险/雇佣打工，主任务延时收尾期间
// 最准、带倒计时）优先；没有它时用 queue.current（护理/好友护理/踩踩/PK/福袋等场景
// 执行期间的中文任务名）。
// 图标选型依据（都拿实机截图/素材库核对过，别再凭文件名猜语义）：
//   冒险 = cap_compass（右侧竖栏最上那个「冒险页」按钮用的就是它）
//   学习 = study_book（书本+铅笔）—— 与指南针同批的 inline_icons 里那张
//          `pet_home_03951_47023.png`；**注意素材库里不少图标是哈希文件名**
//          （pet_home_01470_64134.png 才是 PK 字样、pet_home_01314_68535.png 才是
//          橙色 SOAP），按 `*pk*`/`*soap*` 搜文件名是搜不到的，得按图形找
//          （总览图做法见 qqpet_assets/web/INTEGRATION.md）
//   打工 = work_coin（爪印金币 **带绿色上升箭头**）—— 素材库 `inline_icons/
//          pet_home_03481_15399.png`（= 命名表里的 coin_paw_small.png），游戏底部
//          「打工中」状态条用的就是它；`store_money.png`（= coin-48@2x）是**不带箭头**
//          的另一版，别拿它顶替。**也不是** work_logo 公文包（那是出门地图页
//          「职业小镇」入口的图标），这三个曾被我弄混
//   护理 = soap（橙色 SOAP 香皂，素材库原件 pet_home_01314_68535.png）
//   等待中/已停止 = globe（地球，item_globe = pet_home_01997_36242.png）
const RUNICON={
  '上课':'/qp-icons/official/study_book.png',
  '学习':'/qp-icons/official/study_book.png',
  '打工':'/qp-icons/official/work_coin.png',
  '雇佣打工':'/qp-icons/official/work_coin.png',
  '雇佣好友':'/qp-icons/official/work_coin.png',
  '被雇佣检查':'/qp-icons/official/work_coin.png',
  '冒险':'/qp-icons/official/cap_compass.png',
  '护理':'/qp-icons/official/soap.png',
  '好友护理':'/qp-icons/official/soap.png',
  '踩踩':'/qp-icons/official/cap_paw.png',   // 同胶囊行「今日踩踩」那颗
  'PK':'/qp-icons/official/pk_words.png',
  '福袋':'/qp-icons/official/cap_coin.png',
};
const RUNICON_DEFAULT='/qp-icons/official/globe.png';   // 等待中/已停止：地球（item_globe）
function setRunIcon(key, stopped){
  const el=$('#runnerIcon'); if(!el) return;
  const src=RUNICON[key]||RUNICON_DEFAULT;
  if(el.getAttribute('src')!==src) el.setAttribute('src', src);
  // 已停止：灰度压暗，避免"没在跑却亮着"的误读
  el.style.filter=stopped?'grayscale(1) opacity(.55)':'';
}
// 把当前任务映射到房间场景，切 html[data-scene]（CSS 变量作用域要求写在 html 上）。
// 场景图只有 4 套（main/feed/shower/record）；store 那张是纯天空渐变、不是房间，不用。
// 映射依据：喂食->feed、洗澡/护理->shower、学习/记录类->record，其余回 main。
// 注意 etaKind 是"上课/打工/冒险"这类进行中活动名，优先级高于队列里的当前任务名
// （队列的 current 可能还是上一项，进行中活动才是"此刻在干什么"）。
const SCENE_OF={care:'feed',friend_care:'feed',school:'record',work:'record',
                hire_friend:'record',adventure:'main',visit:'main',pk:'main',gift_bag:'main'};
let lastScene='';
function applyScene(curKey, etaKind){
  let scene='main';
  if(etaKind.indexOf('洗澡')>=0||etaKind.indexOf('护理')>=0) scene='shower';
  else if(etaKind.indexOf('上课')>=0||etaKind.indexOf('学习')>=0) scene='record';
  else if(etaKind.indexOf('打工')>=0) scene='record';
  else if(curKey) scene=SCENE_OF[curKey]||'main';
  if(scene!==lastScene){
    lastScene=scene;
    document.documentElement.setAttribute('data-scene',scene);  // 必须写 html（见 CSS 注释）
    // 状态栏色跟着场景走（色值只定义在 CSS 的 --qp-statusbar，这里读出来即可，
    // 不在 JS 里重复维护一份色表）
    if(curTab==='main') syncThemeColor('main');
  }
}
let etaRemain=null, etaClock='', schedOn=false;
let logAuto=true, logFilter='';
try{ logAuto = localStorage.getItem('qpet_logAuto')!=='0'; }catch(e){}

function pad(n){return String(n).padStart(2,'0')}
function hms(sec){sec=Math.max(0,Math.floor(sec));const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;return (h?h+':':'')+pad(m)+':'+pad(s)}

async function j(u){const r=await fetch(u,{cache:'no-store'});if(!r.ok)throw new Error(r.status);return await r.json()}

function renderData(d){
  const todayStr=(d.now||'').slice(0,10);
  // 头部
  const dot=$('#schedDot');
  dot.className='avatar '+(d.scheduler.alive?'on':'off');
  const _ring=$('#idRing'); if(_ring) _ring.className='idring'+(d.scheduler.alive?'':' off');
  $('#schedTxt').textContent=d.scheduler.alive?('运行中 · 已跑 '+(d.scheduler.uptime||'')):'未运行';
  // 调度器卡片
  if($('#runnerState')){
    const sch=d.scheduler||{};
    $('#runnerDot').className='dot '+(sch.alive?'on':'off');
    $('#runnerMeta').textContent=sch.alive?('PID '+sch.pid+(sch.uptime?(' · 已跑 '+sch.uptime):'')):'未运行';
    $('#btnRunnerStart').disabled=!!sch.alive;
    $('#btnRunnerStop').disabled=!sch.alive;
  }
  // 金币
  const st=d.status||{};
  $('#coins').textContent=st.coins!=null?st.coins:'--';
  $('#coinsAt').textContent=(st.coins!=null&&st.updated)?'· '+st.updated.slice(11,16):'';
  // 打工卡片
  etaRemain=(d.work_eta&&d.work_eta.remaining!=null)?d.work_eta.remaining:null;
  etaClock=d.work_eta?d.work_eta.eta_clock:'';
  schedOn=(d.scheduler||{}).alive;
  const td=d.today_duration;
  let wdHtml='';
  if(td){
    const eff=(td.eff_pct!=null&&td.eff_pct<100)?('<span style="color:#d97706">效率 '+td.eff_pct+'%</span>'):'效率 100%';
    const nxt=(td.next_pct!=null&&td.next_in_min!=null)?('（再 '+td.next_in_min+' 分降到 '+td.next_pct+'%）'):'';
    wdHtml='今日：学习 '+td.learn_min+' 分 · 打工 '+td.work_min+' 分 · 合计 '+(td.total_min??(td.learn_min+td.work_min))+' 分 · '+eff+nxt;
  }
  const rs=$('#runnerState'), rh=$('#runnerHint'), rsub=$('#runnerSub');
  const curTask=(d.queue&&d.queue.current)?String(d.queue.current):'';
  const etaKind=(d.work_eta&&d.work_eta.kind)?String(d.work_eta.kind):'';
  // 状态 key：图标与文案共用同一个，避免出现「冒险中」配着福袋图标这种错位。
  //   ① remaining>0 的 work_eta = 主任务活动真在进行（带倒计时，最准）
  //   ② 否则队列 current = 调度器正在跑的场景（护理/PK/踩踩/福袋…）
  //   ③ 再否则 work_eta 残留 = 已到点、正在收尾
  // ① 必须判 >0：work_eta 在活动结束后 10 分钟内仍会返回（remaining 被 clamp 成 0），
  // 只看“非 null”会让图标和文案一直停在上一项活动上（实测：冒险已结束、队列在跑福袋）。
  const busyEta=(etaRemain>0&&etaKind)?etaKind:'';
  const finishing=(!busyEta&&!curTask&&etaRemain!=null&&etaKind)?etaKind:'';
  const stateKey=busyEta||curTask||finishing;
  if(!schedOn){
    // 调度器未运行：不引用日志里的旧“预计结算”行（会残留“进行中 剩余00:00”误导）
    rs.textContent='已停止';
    rh.textContent='';
    rsub.textContent='已停止：手机不会被自动操作；随时可再启动';
    $('#workSub').innerHTML=wdHtml;
    setRunIcon(null, true);
  }else if(busyEta){
    rs.textContent=busyEta+'中';
    rh.textContent='预计 '+etaClock+' 结束';
    $('#workSub').textContent='剩余 '+hms(etaRemain)+' · 结束后自动开启下一项';
    rsub.textContent='';
    setRunIcon(busyEta);
  }else if(finishing){
    rs.textContent=finishing+'中';
    rh.textContent='预计 '+etaClock+' 结束';
    $('#workSub').textContent='收尾中… · 结束后自动开启下一项';
    rsub.textContent='';
    setRunIcon(finishing);
  }else{
    // current 非空 = 调度器正在跑某个场景（护理/PK/踩踩/福袋…），此时不该说“等待中”
    rs.textContent=stateKey?(stateKey+'中'):'等待中';
    rh.textContent=d.last_line?d.last_line.replace(/^\[[\d:]+\]\s*/,'').slice(0,60):'';
    $('#workSub').innerHTML=wdHtml;
    rsub.textContent='';
    setRunIcon(stateKey);
  }
  if(rsub) rsub.style.display=rsub.textContent?'':'none';
  // 统计瓦片
  const pg=d.progress||{}, cfg=d.config||{};
  const vv=pg.visit&&pg.visit.learned!=null?pg.visit.learned:null;
  const vvMax=cfg.visit_per_day||10;
  $('#visitTxt').textContent=(vv!=null?vv:'--')+'/'+vvMax;
  $('#visitBar').style.width=(vv!=null?Math.min(100,vv/vvMax*100):0)+'%';
  const pp=pg.pk&&pg.pk.learned!=null?pg.pk.learned:null;
  const ppMax=cfg.pk_per_day||15;
  $('#pkTxt').textContent=(pp!=null?pp:'--')+'/'+ppMax;
  $('#pkBar').style.width=(pp!=null?Math.min(100,pp/ppMax*100):0)+'%';
  const av=pg.adventure&&pg.adventure.learned!=null?pg.adventure.learned:0;
  $('#advTxt').textContent=av+'/'+(cfg.adventure_times||1);
  // 今日学习 / 打工（合并一张卡：两者共享同一份合计预算，放一起才看得出分配）
  // 主数值 = 学习节数 + 打工次数（当前正在进行的那一项 +1）；进度条分两段叠加显示
  // 学习/打工占比。注意 kind 必须参与判断——work_eta 是"上课/打工/冒险"共用模板，
  // 只判有无会把"正在上课"错算成"正在打工"（曾显示 0+1 实际在上课）。
  // etaKind 已在上面状态卡那段声明（图标与文案共用同一个 key）
  const busy=(schedOn&&etaRemain!=null);
  const hrs=s=>((s||0)/3600).toFixed(1).replace(/\.0$/,'');
  const sc=pg.school&&pg.school.learned!=null?pg.school.learned:0;
  const scHrs=Number(hrs(pg.school&&pg.school.study_secs));
  const scQuota=cfg.study_quota_hours||0;
  const wk=pg.work&&pg.work.learned!=null?pg.work.learned:0;
  const wkHrs=Number(hrs(pg.work&&pg.work.work_secs));
  const wkQuota=cfg.work_quota_hours||0;
  const scBusy=busy&&etaKind.indexOf('上课')>=0;
  const wkBusy=busy&&etaKind.indexOf('打工')>=0;
  // 主数值：学习N节(+1) / 打工M次(+1)；两者都为0且无进行中时简显示 0
  const scTxt=sc+(scBusy?1:0), wkTxt=wk+(wkBusy?1:0);
  $('#swTxt').textContent=(scBusy||sc>0||wkBusy||wk>0)
    ? ('学 '+scTxt+' · 工 '+wkTxt) : '0';
  // 总预算 = 学习配额 + 打工配额（两者共享）；进度条按"已用/总预算"分两段
  const totalQuota=scQuota+wkQuota;
  const totalUsed=scHrs+wkHrs;
  const pct=v=>totalQuota?Math.min(100,Math.max(0,v/totalQuota*100)):0;
  $('#swBarSchool').style.width=pct(scHrs)+'%';
  $('#swBarWork').style.width=pct(wkHrs)+'%';
  const swLblTxt='学习 / 打工'
    +(totalQuota?(' · '+totalUsed.toFixed(1).replace(/\.0$/,'')+'/'+totalQuota+'h'):'');
  $('#swLbl').textContent=swLblTxt;
  // 胶囊太窄放不下，长文案转到 title 上（悬浮可见）
  const _capSw=$('#capSw'); if(_capSw) _capSw.title='今日 '+swLblTxt;
  $('#swTxt').title='学习 '+sc+' 节（'+scHrs+'h'
    +(scQuota?('/'+scQuota+'h'):'')+'）· 打工 '+wk+' 次（'+wkHrs+'h'
    +(wkQuota?('/'+wkQuota+'h'):'')+'）'
    +((scBusy||wkBusy)?(' · 正在进行：'+(d.work_eta.kind||'')):'');
  const ed=(pg.exp_daily&&pg.exp_daily.done)?'✓ 完成':'未完成';
  $('#expTxt').textContent=ed;
  // 队列：MAA 风格任务开关列表（勾选=启用该任务，写入 config 下轮生效）
  const q=d.queue||{}, qt=q.tasks||{};
  const qLive=(d.scheduler||{}).alive;
  const qOrder=(cfg.task_order||[]);
  const qRank=k=>{const i=qOrder.indexOf(k);return i<0?999:i;};
  let rows='';
  const cur=(q.current||'');
  const curMap={'上课':'school','学习':'school','打工':'work','冒险':'adventure',
                '护理':'care','踩踩':'visit','PK':'pk','好友护理':'friend_care',
                '福袋':'gift_bag','雇佣好友':'hire_friend'};
  const curKey=cur?(curMap[cur]||Object.keys(TASKNAME).find(k=>TASKNAME[k]===cur)||''):null;
  applyScene(curKey, (d.work_eta&&d.work_eta.kind)?String(d.work_eta.kind):'');
  // 任务类型标签：循环=按间隔反复巡检；每日=每天定时一轮；主线=主任务组
  const TTAG={care:'循环',friend_care:'循环',gift_bag:'循环',
              visit:'每日',pk:'每日',
              adventure:'主线',school:'主线',work:'主线',hire_friend:'主线'};
  const rowOf=(k,on,st,nx)=>{
    const isRun=curKey&&k===curKey;
    let det;
    if(!on) det='<span class="off-t">已禁用</span>';
    else if(st==='cfg') det=on==='cfg'?'':'已启用';
    else if(isRun) det='<span class="run">▶ 执行中</span>';
    else if(st==='ready') det='可执行';
    else if(st==='waiting') det=(qt[k]&&qt[k].next)?('等待 · '+qt[k].next.slice(11,16)):'等待';
    else if(st==='done') det='✓ 今日完成';
    else if(st==='dead') det='今日结束';
    else det='—';
    const done=(st==='done'||st==='dead');
    const tag=TTAG[k]?('<span class="ttag">'+TTAG[k]+'</span>'):'';
    // 按需求：右侧的"已启用/已禁用"状态文字已移除，
    // 勾选框从左侧移到原状态文字的位置（最右）。
    return '<div class="mrow'+(done?' done':'')+(isRun?' run':'')+'">'
      +'<img class="qico" src="/qp-icons/'+(TASKICON[k]||'coin')+'-24.png" alt="">'
      +'<span class="mname'+(on?'':' off')+'">'+(TASKNAME[k]||k)+'</span>'+tag
      +'<span class="mcb'+(on&&st!=='disabled'?' on':'')+'" data-k="'+k+'"></span></div>';
  };
  if(qLive){
    // 收尾队列：写在标题行右侧（原来单独占一行，视觉上像多了一个任务）
    const _qp=document.getElementById('qPend');
    if(_qp) _qp.innerHTML = q.pending
      ? ('<span class="run">'+q.pending+' 待结算</span>') : '';
    const ks=Object.keys(qt).slice().sort((a,b)=>qRank(a)-qRank(b));
    for(const k of ks){
      const st=qt[k].state||'';
      const nx=qt[k].next?('→ '+(qt[k].next.slice(0,10)===todayStr?'':'明 ')+qt[k].next.slice(11,16)):'';
      rows+=rowOf(k, st!=='disabled', st, nx);
    }
  }else{
    const _qp2=document.getElementById('qPend'); if(_qp2) _qp2.innerHTML='';
    const te=cfg.tasks_enabled||{};
    const keys=qOrder.length?qOrder:Object.keys(te);
    const allKeys=(keys.length?keys:Object.keys(TASKNAME)).slice().sort((a,b)=>qRank(a)-qRank(b));
    for(const k of allKeys){
      const on=te[k]!==false;
      rows+=rowOf(k, on, 'cfg', '');
    }
  }
  $('#taskList').innerHTML=rows||'';
  // 「未启用：xxx（不参与调度）」提示行已按需求移除
  // 截图
  const shots=d.shots||[];
  if(shots.length){
    $('#shotCard').classList.remove('hide');
    $('#shots').innerHTML=shots.map(s=>'<a href="/files/'+encodeURIComponent(s.name)+'" target="_blank"><img loading="lazy" src="/files/'+encodeURIComponent(s.name)+'"><span class="cap">'+s.mtime+'</span></a>').join('');
  }
  // 好友名单（供表单下拉）：变化时更新，并强制重渲染表单让下拉项生效
  if(Array.isArray(d.friends)){
    const changed = JSON.stringify(d.friends)!==JSON.stringify(FRIENDS);
    FRIENDS = d.friends;
    if(changed && window.__lastEditable && !setDirty) renderSettings(window.__lastEditable);
  }
  if(d.editable) window.__lastEditable=d.editable;
    // 重建表单会销毁正在操作的控件（下拉被自动关闭、输入焦点丢失）。
  // 三种情况都不重建：①有未保存改动 ②焦点在表单内 ③刚有过交互(2s 内)。
  // 见 markDirtyAndSave / __formBusy。
  if(d.editable && !setDirty && !formBusy()) renderSettings(d.editable);
  if(window.__wrapPages) window.__wrapPages();
  renderCfg((d.config||{}).rows);
  // 页面底部的策略行（footer）已按需求移除，这里不再拼文案
}

async function refreshData(){
  try{ renderData(await j('/api/data')); }
  catch(e){ $('#schedDot').className='avatar off'; $('#schedTxt').textContent='连接失败';
    const _r2=$('#idRing'); if(_r2) _r2.className='idring off'; }
}

function svgSet(id,inner){const el=document.getElementById(id);if(el)el.innerHTML=inner;}
function drawAdv(){
  const d=window.__adv;if(!d)return;
  const W=340,H=84,pad=8,tx=d.n||100;
  const sx=i=>pad+(Math.max(1,i)-1)/Math.max(1,(tx-1))*(W-2*pad);
  let inner='<line x1="0" y1="42" x2="'+W+'" y2="42" stroke="#e2e5ec" stroke-width="1" stroke-dasharray="4 4"/>';
  const ys0=(d.cum||[]).map(p=>p[1]);
  let mx=Math.max(10,...ys0.map(v=>Math.abs(v)))*1.15;
  const sy0=v=>42-v/mx*34;
  if((d.cum||[]).length>1){
    inner+='<polyline points="'+d.cum.map(p=>sx(p[0]).toFixed(1)+','+sy0(p[1]).toFixed(1)).join(' ')+'" fill="none" stroke="#ea580c" stroke-width="2" stroke-linejoin="round"/>';
    const lp=d.cum[d.cum.length-1];
    inner+='<circle cx="'+sx(lp[0]).toFixed(1)+'" cy="'+sy0(lp[1]).toFixed(1)+'" r="3" fill="#ea580c"/>';
  }
  if(window.__advSel&&window.__advSel.chart==='cum'){const cm={};(d.cum||[]).forEach(p=>cm[p[0]]=p[1]);const k=window.__advSel.k;if(k in cm){const xx=sx(k).toFixed(1);inner+='<line x1="'+xx+'" y1="4" x2="'+xx+'" y2="80" stroke="#94a3b8" stroke-width="1" stroke-dasharray="3 3"/><circle cx="'+xx+'" cy="'+sy0(cm[k]).toFixed(1)+'" r="4" fill="#ea580c" stroke="#fff" stroke-width="1.5"/>';}}
  svgSet('svgCum',inner);
  let inner2='<line x1="0" y1="42" x2="'+W+'" y2="42" stroke="#e2e5ec" stroke-width="1" stroke-dasharray="4 4"/>';
  const ys1=(d.pts||[]).map(p=>p[1]);
  let mx1=Math.max(10,...ys1.map(v=>Math.abs(v)))*1.2;
  const sy1=v=>42-v/mx1*34;
  if(d.avg!=null){const y=sy1(d.avg);inner2+='<line x1="0" y1="'+y.toFixed(1)+'" x2="'+W+'" y2="'+y.toFixed(1)+'" stroke="#f59e0b" stroke-width="1" stroke-dasharray="5 4"/>';}
  for(const p of (d.pts||[])){inner2+='<circle cx="'+sx(p[0]).toFixed(1)+'" cy="'+sy1(p[1]).toFixed(1)+'" r="2.2" fill="#0ea5e9" opacity=".85"/>';}
  if(window.__advSel&&window.__advSel.chart==='pts'){const dm={};(d.pts||[]).forEach(p=>dm[p[0]]=p[1]);const k=window.__advSel.k;if(k in dm){const xx=sx(k).toFixed(1);inner2+='<line x1="'+xx+'" y1="4" x2="'+xx+'" y2="80" stroke="#94a3b8" stroke-width="1" stroke-dasharray="3 3"/><circle cx="'+xx+'" cy="'+sy1(dm[k]).toFixed(1)+'" r="4" fill="#0ea5e9" stroke="#fff" stroke-width="1.5"/>';}}
  svgSet('svgPts',inner2);
  const st=d.stats||[];
  const sy2=v=>H-pad-(Math.max(0,Math.min(100,v))/100)*(H-2*pad);
  let inner3='<line x1="0" y1="'+sy2(60).toFixed(1)+'" x2="'+W+'" y2="'+sy2(60).toFixed(1)+'" stroke="#ef4444" stroke-width="1" stroke-dasharray="5 4" opacity=".7"/>';
  const defs=[['e',1,'#16a34a'],['c',2,'#0891b2'],['m',3,'#d97706']];
  for(const df of defs){
    const pts=st.filter(r=>r[df[1]]!=null);
    if(pts.length<2)continue;
    inner3+='<polyline points="'+pts.map(r=>sx(r[0]).toFixed(1)+','+sy2(r[df[1]]).toFixed(1)).join(' ')+'" fill="none" stroke="'+df[2]+'" stroke-width="1.8"/>';
    const lp=pts[pts.length-1];
    inner3+='<circle cx="'+sx(lp[0]).toFixed(1)+'" cy="'+sy2(lp[df[1]]).toFixed(1)+'" r="2.6" fill="'+df[2]+'"/>';
  }
  for(const r of st){if(r[4]===1){inner3+='<line x1="'+sx(r[0]).toFixed(1)+'" y1="'+pad+'" x2="'+sx(r[0]).toFixed(1)+'" y2="'+(H-pad)+'" stroke="#ef4444" stroke-width="1" stroke-dasharray="2 3" opacity=".6"/>';}}
  if(window.__advSel&&window.__advSel.chart==='stats'){const k=window.__advSel.k;const rw=st.filter(r=>r[0]===k);if(rw.length){const xx=sx(k).toFixed(1);inner3+='<line x1="'+xx+'" y1="4" x2="'+xx+'" y2="80" stroke="#94a3b8" stroke-width="1" stroke-dasharray="3 3"/>';for(const rr of rw){if(rr[1]!=null)inner3+='<circle cx="'+xx+'" cy="'+sy2(rr[1]).toFixed(1)+'" r="3" fill="#16a34a" stroke="#fff"/>';if(rr[2]!=null)inner3+='<circle cx="'+xx+'" cy="'+sy2(rr[2]).toFixed(1)+'" r="3" fill="#0891b2" stroke="#fff"/>';if(rr[3]!=null)inner3+='<circle cx="'+xx+'" cy="'+sy2(rr[3]).toFixed(1)+'" r="3" fill="#d97706" stroke="#fff"/>';}}}
  svgSet('svgStats',inner3);
}
function renderAdventure(d){
  window.__adv=d;
  if(!d||!d.ok){const m=$('#advMeta');if(m)m.textContent='暂无数据';return}
  const fmtNet=v=>(v>0?'+':'')+v;
  if(d.date==='all') $('#advMeta').textContent='共 '+d.n+' 把 · 今日 '+(d.today_n||0)+' 把（'+fmtNet(d.today_net||0)+'） · 更新 '+(d.updated||'');
  else if(d.date===d.today) $('#advMeta').textContent='共 '+d.n+' 把 · 更新 '+(d.updated||'');
  else $('#advMeta').textContent=(d.date||'').slice(5).replace('-','月')+'日 · 共 '+d.n+' 把 · 更新 '+(d.updated||'');
  renderAdvDates(d);
  const big=$('#advNet');big.textContent=(d.net>0?'+':'')+d.net;
  big.style.color=d.net>0?'var(--gold)':(d.net<0?'#dc2626':'');
  $('#advNetHint').textContent='金币收益合计（结算页口径） · 平均 '+(d.avg>0?'+':'')+d.avg+'/把';
  $('#advSub').textContent='有收益 '+d.win+' 把 · 零收益 '+d.zero+' 把';
  let ch='';
  for(const g of (d.gains||[])){ch+='<span class="chip">'+esc(g[0])+' +'+g[2]+' ×'+g[1]+'</span>';}
  $('#advChips').innerHTML=ch;
  const hasStats=!!((d.stats||[]).length);
  const sv=$('#svgStats'); if(sv) sv.style.display=hasStats?'':'none';
  const cp=$('#capStats'); if(cp) cp.style.display=hasStats?'':'none';
  drawAdv();
  renderAdvList(d);
}
function renderAdvDates(d){
  const row=$('#advDateRow'); if(!row)return;
  const sig=(d.date||'')+'|'+(d.dates||[]).join(',');
  if(row.__sig===sig)return; row.__sig=sig;
  const opts=[];
  for(const dt of (d.dates||[])){
    const lab=dt===d.today?'今天':(dt===d.yesterday?'昨天':dt.slice(5).replace('-','/'));
    opts.push('<option value="'+dt+'"'+(dt===d.date?' selected':'')+'>'+lab+'</option>');
  }
  opts.push('<option value="all"'+(d.date==='all'?' selected':'')+'>全部</option>');
  row.innerHTML='统计范围 <select id="advDate" style="font:inherit;padding:1px 4px">'+opts.join('')+'</select>';
}
const _advDateRow=document.getElementById('advDateRow');
if(_advDateRow) _advDateRow.addEventListener('change',e=>{
  if(e.target&&e.target.id==='advDate'){window.__advDate=e.target.value;refreshAdventure();}
});
let advShowAll=true;
function renderAdvList(d){
  const list=$('#advList'); if(!list)return;
  let arr=(d.recent||[]).slice().reverse().slice(0,400);
  if(!advShowAll) arr=arr.filter(r=>r[2]!==0||r[4]);
  list.innerHTML=arr.map(r=>{
    const v=r[2]; const cls=v>0?'pos':(v<0?'neg':'zero');
    const vt=(v>0?'+':'')+(v==null?'?':v);
    let g=esc(r[3]||'');
    if(r[4]) g+=(g?' ':'')+'<span style="color:#dc2626">扣费'+r[4]+'</span>';
    return '<div class="arow"><span class="ai">#'+r[0]+' '+(r[1]||'').slice(0,11)+'</span><span class="ag">'+g+'</span><span class="av '+cls+'">'+vt+'</span></div>';
  }).join('')||'<div class="arow"><span class="ag">暂无记录</span></div>';
  const b=$('#btnAdvAll'); if(b){b.className='minibtn'+(advShowAll?' on':'');b.textContent=advShowAll?'全部':'仅变化';}
}
const _advBtn=document.getElementById('btnAdvAll');
if(_advBtn) _advBtn.onclick=()=>{advShowAll=!advShowAll; if(window.__adv)renderAdvList(window.__adv);};
window.__advSel=null;
function advMaps(d){
  const m={cum:{},dl:{},gn:{},tm:{},st:{}};
  for(const p of (d.cum||[]))m.cum[p[0]]=p[1];
  for(const p of (d.pts||[]))m.dl[p[0]]=p[1];
  for(const r of (d.recent||[])){m.gn[r[0]]=r[3]||'';m.tm[r[0]]=r[1]||'';}
  for(const r of (d.stats||[])){const k=r[0];if(!(k in m.st)||r[4]===1)m.st[k]=r;}
  return m;
}
function advSelect(chart,k){
  if(!window.__adv)return;
  window.__advSel={chart:chart,k:k};
  drawAdv();
  const m=advMaps(window.__adv);
  const tip=$('#advTip');if(!tip)return;
  if(chart==='stats'){
    const r=m.st[k];
    if(r)tip.innerHTML='#'+k+(m.tm[k]?(' '+m.tm[k].slice(0,5)):'')+' · 体力 <b>'+(r[1]==null?'-':r[1])+'</b> · 清洁 <b>'+(r[2]==null?'-':r[2])+'</b> · 心情 <b>'+(r[3]==null?'-':r[3])+'</b>'+(r[4]===1?'（护理后）':'');
  }else{
    const v=m.dl[k];
    let s='#'+k+(m.tm[k]?(' '+m.tm[k].slice(0,5)):'')+' · 累计 '+((m.cum[k]||0)>0?'+':'')+(m.cum[k]||0)+' · 本趟 '+((v>0?'+':'')+(v==null?'?':v));
    if(m.gn[k])s+=' · '+esc(m.gn[k]);
    tip.innerHTML=s;
  }
}
function advNearest(chart,x){
  const d=window.__adv;if(!d)return;
  const pad=8,tx=d.n||100,W=340;
  const sx0=i=>pad+(Math.max(1,i)-1)/Math.max(1,(tx-1))*(W-2*pad);
  const arr=chart==='stats'?(d.stats||[]).map(r=>r[0]):(d.pts||[]).map(p=>p[0]);
  let best=null,bd=1e9;
  for(const i of arr){const dd=Math.abs(sx0(i)-x);if(dd<bd){bd=dd;best=i;}}
  if(best!=null)advSelect(chart,best);
}
function bindAdvChart(id,chart){
  const el=document.getElementById(id);if(!el||el.__b)return;el.__b=true;
  let down=false;
  const pos=e=>{const rect=el.getBoundingClientRect();return (e.clientX-rect.left)*(340/Math.max(1,rect.width));};
  el.addEventListener('pointerdown',e=>{down=true;advNearest(chart,pos(e));});
  el.addEventListener('pointermove',e=>{if(down)advNearest(chart,pos(e));});
  const up=()=>{down=false;};
  el.addEventListener('pointerup',up);el.addEventListener('pointercancel',up);el.addEventListener('pointerleave',up);
}
bindAdvChart('svgCum','cum');bindAdvChart('svgPts','pts');bindAdvChart('svgStats','stats');
let planDirty=false;
function planBar(t,c,tg,col){
  const w=tg?Math.max(0,Math.min(100,c/tg*100)):0;
  return '<div class="pb"><div class="t"><span>'+t+'</span><span>'+c+' / '+tg+'</span></div><div class="bar"><i style="width:'+w.toFixed(1)+'%;background:'+col+'"></i></div></div>';
}
function planBuildEdit(v){
  $('#planEdit').innerHTML=
    '<label>力量<input type="number" id="pn1" min="0" value="'+(v['力']??0)+'"></label>'+
    '<label>智力<input type="number" id="pn2" min="0" value="'+(v['智']??0)+'"></label>'+
    '<label>魅力<input type="number" id="pn3" min="0" value="'+(v['魅']??0)+'"></label>'+
    '<label>工分<input type="number" id="pn4" min="0" value="'+(v['工分']??0)+'"></label>'+
    '<label>金币<input type="number" id="pn5" min="0" value="'+(v['金币']??0)+'"></label>'+
    '<div class="planeditrow"><span>学园：</span><button class="sw'+(v['初级毕业']?' on':'')+'" id="swPrim"></button><span>初级毕业</span><button class="sw'+(v['中级毕业']?' on':'')+'" id="swMid"></button><span>中级毕业</span></div>';
  for(const id of ['#pn1','#pn2','#pn3','#pn4','#pn5']){ const el=$(id); if(el) el.oninput=()=>{planDirty=true;}; }
  const sp=$('#swPrim'), sm=$('#swMid');
  if(sp) sp.onclick=()=>{sp.classList.toggle('on');planDirty=true;};
  if(sm) sm.onclick=()=>{sm.classList.toggle('on');planDirty=true;};
}
function renderPlan(d){
  if(!d||!d.ok)return;
  window.__plan=d;
  $('#planMeta').textContent='总属性 '+d.total+'/'+d.total_target+' · 更新 '+(d.updated||'');
  $('#planBars').innerHTML=planBar('属性总进度',d.total,d.total_target,'var(--accent)')+planBar('见习解锁',d.jr_n,8,'#16a34a')+planBar('初级解锁',d.ch_n,8,'#ea580c');
  let firstOpen=false;
  $('#planSteps').innerHTML=(d.steps||[]).map(s=>{
    let cls='st',dot='○';
    if(s[2]){cls+=' done';dot='✅';}
    else if(!firstOpen){cls+=' cur';dot='▶';firstOpen=true;}
    return '<div class="'+cls+'"><span class="dot2">'+dot+'</span><span class="tx">'+esc(s[0]+' · '+s[1])+'</span><span class="pr">'+esc(s[3])+'</span></div>';
  }).join('');
  if($('#planLinesMeta')) $('#planLinesMeta').textContent=d.lines_meta||'';
  $('#planLines').innerHTML=(d.lines||[]).map(l=>'<div class="ln"><span>'+esc(l.name)+'</span><span><span class="chipx'+(l.jr?' ok':'')+'">见习</span><span class="chipx'+(l.ch?' ok':'')+'">初级</span></span></div>').join('');
  const w=d.watch||{};
  if($('#watchMeta')) $('#watchMeta').textContent=w.last_check?('上次检查 '+String(w.last_check).slice(11,16)):'';
  if($('#watchBox')){
    let st;
    if(!w.enabled) st='监控已关闭（设置页「职业」区可开）';
    else if(!w.alive) st='调度器未运行 — 启动后自动监控';
    else st='监控中 · '+(w.interval?('每节课后 + 每 '+w.interval+' 分钟兜底'):'每节课后')+(w.stop_study?' · 解锁后自动停学':' · 仅通知');
    let wh='<div class="wstate">'+st+'</div>';
    const evs=(w.events||[]).slice().reverse();
    if(evs.length){
      wh+=evs.map(e=>'<div class="wrow"><span class="wbadge">🎉 '+esc(e.career||'')+'（见习·'+esc(e.name||'?')+'）</span><span style="color:var(--sub);font-size:11.5px">'+esc(String(e.ts||'').slice(5,16))+'</span></div>').join('');
    } else {
      wh+='<div class="wrow" style="color:var(--sub)"><span>尚未解锁（武术家 / 梦境旅人 / 大明星）</span><span></span></div>';
    }
    $('#watchBox').innerHTML=wh;
  }
  if(!planDirty) planBuildEdit(d.values||{});
}
async function refreshPlan(){ try{ renderPlan(await j('/api/plan')); }catch(e){} }
const _planBtn=document.getElementById('btnPlanSave');
if(_planBtn) _planBtn.onclick=async()=>{
  const g=id=>{const el=$(id);return el?(parseInt(el.value||'0',10)||0):0;};
  const updates={'力':g('#pn1'),'智':g('#pn2'),'魅':g('#pn3'),'工分':g('#pn4'),'金币':g('#pn5'),
    '初级毕业':$('#swPrim')?$('#swPrim').classList.contains('on'):false,
    '中级毕业':$('#swMid')?$('#swMid').classList.contains('on'):false};
  try{
    const r=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({updates})});
    const d=await r.json();
    if(d.rejected&&d.rejected.length){$('#planMsg').className='saveMsg err';$('#planMsg').textContent='部分未保存：'+d.rejected.join('；');}
    else{planDirty=false;$('#planMsg').className='saveMsg';$('#planMsg').textContent='✅ 已保存';refreshPlan();}
  }catch(e){$('#planMsg').className='saveMsg err';$('#planMsg').textContent='保存失败：'+e.message;}
};
const _planSync=$('#btnPlanSync');
if(_planSync) _planSync.onclick=async()=>{
  if(_planSync.disabled) return;
  _planSync.disabled=true;
  const old=_planSync.textContent;
  _planSync.textContent='识别中…（约20秒）';
  $('#planMsg').className='saveMsg';
  $('#planMsg').textContent='正在识别游戏里的属性…';
  try{
    const r=await fetch('/api/plan/sync',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    const s=d.sync||{};
    if(s.ok){planDirty=false;$('#planMsg').className='saveMsg';$('#planMsg').textContent='✅ 已读取：力量'+s['力']+' · 智力'+s['智']+' · 魅力'+s['魅'];}
    else{$('#planMsg').className='saveMsg err';$('#planMsg').textContent='识别失败：'+(s.reason||'未知')+'（游戏画面忙，可稍后重试）';}
    refreshPlan();
  }catch(e){$('#planMsg').className='saveMsg err';$('#planMsg').textContent='识别失败：'+e.message;}
  _planSync.disabled=false;
  _planSync.textContent=old;
};
async function runnerAction(kind){
  const msg=$('#runnerMsg'); if(!msg)return;
  msg.className='saveMsg';
  msg.textContent=(kind==='start'?'正在启动调度器（连接设备约需几秒）…':'正在停止调度器（等当前任务收尾）…');
  try{
    const r=await fetch('/api/runner/'+kind,{method:'POST'});
    const d=await r.json();
    msg.textContent=d.msg||(d.ok?'完成':'失败');
    if(!d.ok) msg.className='saveMsg err';
  }catch(e){ msg.className='saveMsg err'; msg.textContent='请求失败：'+e.message; }
  refreshData();
}
const _rbStart=$('#btnRunnerStart'), _rbStop=$('#btnRunnerStop');
if(_rbStart) _rbStart.onclick=()=>runnerAction('start');
if(_rbStop) _rbStop.onclick=()=>{ if(confirm('停止调度器？正在进行的任务会先收尾再退出（约几秒到十几秒）。')) runnerAction('stop'); };
async function refreshAdventure(){
  try{ renderAdventure(await j('/api/adventure'+(window.__advDate?'?date='+encodeURIComponent(window.__advDate):''))); }catch(e){}
}

async function refreshLogs(){
  try{
    const d=await j('/api/logs?tail=250');
    const box=$('#logbox');
    const near=box.scrollHeight-box.scrollTop-box.clientHeight<48;
    let lines=d.lines||[];
    if(logFilter) lines=lines.filter(l=>l.indexOf(logFilter)>=0);
    box.textContent=lines.join('\n');
    // 元信息放日志框下方注脚（.logfoot），不再挤顶栏标题
    $('#logMeta').textContent=d.name?(d.name+' · '+lines.length+' 行'):'';
    if(logAuto&&near) box.scrollTop=box.scrollHeight;
  }catch(e){}
}

// 秒级：时钟 + 倒计时
setInterval(()=>{
  const n=new Date();
  if(etaRemain!=null && schedOn){
    etaRemain-=1;
    const sub= etaRemain>0? ('剩余 '+hms(etaRemain)+' · 结束后自动开启下一项') : '收尾中…';
    const el=$('#workSub'); if(el) el.textContent=sub;
  }
},1000);

$('#btnAuto').className=logAuto?'on':'';
$('#btnAuto').onclick=()=>{logAuto=!logAuto;$('#btnAuto').className=logAuto?'on':'';try{localStorage.setItem('qpet_logAuto',logAuto?'1':'0')}catch(e){}};
$('#logFilter').oninput=e=>{logFilter=e.target.value.trim();refreshLogs()};

function renderCfg(rows){
  const HIDE=['调度策略','打工','金币阈值','时长上限','踩踩','PK','冒险','护理'];
  rows=(rows||[]).filter(r=>HIDE.indexOf(r[0])<0);
  if(!rows.length){$('#cfgList').innerHTML='';return}
  $('#cfgList').innerHTML=rows.map(r=>'<div class="row"><span class="k">'+r[0]+'</span><span class="v">'+r[1]+'</span></div>').join('');
}

// ---- 设置页两级切换：一级分类列表 <-> 二级分类详情 ----
// **二级是独立的一层历史**（导航层级：总览 0 / 内页 1 / 设置二级 2）：
//   进二级 pushState（navDepth 1 -> 2），"返回设置列表"/侧滑 都是 **history.back()**（退栈）。
// 改前每进一次二级、每点一次"返回设置列表"都 pushState（按钮也压栈），
// 于是侧滑会反向往二级里钻（实测：进二级 -> 点"返回设置列表" -> 侧滑 又弹回二级）。
// skipHistory=true 只用于"定时刷新重建表单后恢复层级"，不碰历史。
function showSetIndex(skipHistory){
  const a=document.getElementById('setIndex'), b=document.getElementById('setDetail');
  if(a) a.classList.remove('hide');
  if(b) b.classList.add('hide');
  window.__setGrp=null;
  if(skipHistory) return;
  // 从二级返回一级：**退栈**（不是再压一条一级，否则侧滑会退回二级）
  if(navDepth>=2){ navDepth=1; try{ history.back(); }catch(e){} }
}
function openSetGroup(key, skipHistory){
  const a=document.getElementById('setIndex'), b=document.getElementById('setDetail');
  if(!a||!b) return;
  const grp=document.getElementById('grp_'+key);
  if(!grp) return;
  if(!skipHistory){
    try{ history.pushState({tab:'set',grp:key}, '', '?tab=set&grp='+key); }catch(e){}
    navDepth=2;
  }
  // 只显示这一组，其余隐藏
  document.querySelectorAll('#setForm .fgrp').forEach(g=>g.classList.toggle('hide', g.id!=='grp_'+key));
  const t=document.getElementById('setDetailTitle');
  if(t) t.textContent=grp.dataset.title||'设置';
  a.classList.add('hide'); b.classList.remove('hide');
  window.__setGrp=key;
}
document.addEventListener('click',function(e){
  const b=e.target.closest && e.target.closest('#btnSetBack');
  if(b){ showSetIndex(); }
},true);

// ---- 共用卡片生成器（设置页与通知页共用，保证两页排版完全一致） ----
// 结构：.fgrp（小节）> .fsect（卡外小标题） + .fsec（白卡，内含 .frow 行）
function card(title, rows, key){
  return '<div class="fgrp"'+(key?(' id="grp_'+key+'" data-title="'+title+'"'):'')
    +'><div class="fsect">'+title+'</div>'
    +'<div class="fsec">'+rows.join('')+'</div></div>';
}
// 行：左键值 + 右侧任意控件（开关/输入/下拉），设置页与通知页通用
function rowKv(label, ctrl, tip){
  return '<div class="frow"'+(tip?(' title="'+tip+'"'):'')+'>'
    +'<span class="k">'+label+'</span>'+ctrl+'</div>';
}

let setInit=null, setDirty=false;

// 表单"正忙"判定：避免定时刷新重建表单把用户正在操作的控件销毁。
// 现象：下拉/选择框点开后几秒被自动关闭、输入框失焦（每 6s 的 refreshData 重建表单）。
let _lastFormTouch = 0;
function formBusy(){
  const ae = document.activeElement;
  // 焦点在设置/通知表单内（含 select/input/button）
  if(ae && ae.closest && ae.closest('#setForm, #notifyForm')) return true;
  // 刚有过交互（2 秒保护期）—— 覆盖"点了下拉但焦点已转移"的瞬间
  return (Date.now() - _lastFormTouch) < 2000;
}
document.addEventListener('pointerdown', e=>{
  if(e.target.closest && e.target.closest('#setForm, #notifyForm')) _lastFormTouch = Date.now();
}, true);
document.addEventListener('focusin', e=>{
  if(e.target.closest && e.target.closest('#setForm, #notifyForm')) _lastFormTouch = Date.now();
}, true);
// 好友名单（由 /api/data 的 friends 字段带入），供各表单的下拉选择
let FRIENDS = [];
// 生成"可输入下拉"：既可从已有好友里选，也保留手输能力（用 datalist）
// 为什么用 datalist：原生、无依赖、iOS Safari 支持良好，且不破坏现有
// "读 input.value 保存"的逻辑（id/name 不变，保存代码零改动）。
function friendPicker(id, cur, placeholder){
  // 用原生 <select> 让用户直接选（iOS Safari 对 datalist 支持差：
  // 只在键盘上方出建议条，观感仍是"输入框"）。
  // 结构：select（选已有好友 / 手动输入）+ input（保留原 id，存实际值）。
  // input 保持原 id 不变 -> 保存逻辑（读取该 input 的 value）零改动。
  const val = cur||'';
  const opts = FRIENDS.map(n=>'<option value="'+esc(n)+'"'
      + (n===val?' selected':'')+'>'+esc(n)+'</option>').join('');
  const isCustom = val && FRIENDS.indexOf(val)<0;
  // 下拉里的是【好友列表的主人昵称】（content-desc 抓取，准确）；
  // 宠物名不在列表里 —— 用「手动输入…」填。
  const cnt = FRIENDS.length;
  return '<span class="fpick">'
    + '<select class="fpsel" data-for="'+id+'"'
      + (cnt?'':' title="还没有好友名单：跑一次踩踩/福袋后自动生成"')+'>'
      + '<option value="">（未设置）</option>'
      + (cnt?('<optgroup label="好友昵称（'+cnt+'）">'+opts+'</optgroup>')
            :'<option value="" disabled>（暂无好友名单）</option>')
      + '<option value="__custom__"'+(isCustom?' selected':'')+'>手动输入宠物名/昵称…</option>'
    + '</select>'
    + '<input type="text" id="'+id+'" class="fpinput'+(isCustom?'':' hide')+'"'
      + ' autocomplete="off" placeholder="'+esc(placeholder||'输入宠物名或主人昵称')+'"'
      + ' value="'+esc(val)+'">'
  + '</span>';
}
// 下拉选择后同步到 input（并触发自动保存）
document.addEventListener('change', function(e){
  const sel=e.target.closest && e.target.closest('.fpsel');
  if(!sel) return;
  const inp=document.getElementById(sel.dataset.for);
  if(!inp) return;
  const v=sel.value;
  if(v==='__custom__'){ inp.classList.remove('hide'); inp.focus(); return; }
  inp.classList.add('hide');
  inp.value=v;
  inp.dispatchEvent(new Event('input',{bubbles:true}));   // 触发改动即保存
}, true);

function renderSettings(ed){
  if(!ed) return;
  setInit=Object.assign({},ed);
  const sel=(id,opts,cur)=>'<select id="'+id+'">'+opts.map(v=>'<option value="'+v+'"'+(v===cur?' selected':'')+'>'+v+'</option>').join('')+'</select>';
  // 选项名只描述 work 与 adventure 的先后（school/hire_friend 两组里都固定在前，
  // 且各任务能否执行还取决于自身条件——金币/时长上限/疲劳/次数，见 _school_due 等）
  const moOpts=[['school>hire_friend>work>adventure','先打工，打满 8h 再冒险'],['school>hire_friend>adventure>work','先冒险，冒险没次数了再打工']];
  const moSel=(cur)=>'<select id="selMainOrder" title="主任务组（学习/雇佣/冒险/打工）互斥时的执行优先级：按 > 顺序逐个检查，第一个条件满足的执行。学习与雇佣好友在两组预设里都固定排在最前，此处切换的只是打工与冒险的先后；每个任务还要自身条件满足才会执行（金币达标/未超时长上限/未疲劳/次数未满），改完下一轮调度生效">'+moOpts.map(o=>'<option value="'+o[0]+'"'+(o[0]===cur?' selected':'')+'>'+o[1]+'</option>').join('')+(moOpts.some(o=>o[0]===cur)?'':'<option value="'+esc(cur||'')+'" selected>自定义：'+esc(cur||'')+'</option>')+'</select>';
  // 复用全局 card()：组标题在卡片【外】，行在白色卡片【内】
  const FG=(t,rows,key)=>card(t,rows,key);
  $('#setForm').innerHTML=
    FG('学习',[
    '<div class="frow"><span class="k">只打工不学习</span><button class="sw'+(ed.school_enabled?'':' on')+'" id="swSchool" title="开=只打工；关=学习+打工"></button></div>',
    '<div class="frow"><span class="k">学习科目</span>'+sel('selSchoolAttr', ['力量','智力','魅力','夏令营'], ed.school_attribute)+'</div>',
    '<div class="frow"><span class="k">每天学习次数</span><input type="number" id="numSchoolTimes" min="0" step="1" title="0=不限" value="'+(ed.school_times??0)+'"></div>',
    '<div class="frow"><span class="k">课时档位</span>'+sel('selSchoolDur', ['短课','长课'], ed.school_duration)+'</div>',
    '<div class="frow"><span class="k">当前选择</span><span id="schoolHint" style="color:var(--sub);font-size:12px"></span></div>',
    '<div class="frow"><span class="k">档位说明</span><span style="color:var(--sub);font-size:12px">课程轮播固定 7 张：卡1-3 短课（力量/智力/魅力）、卡4-6 长课（同序）、卡7 萌芽夏令营。各学院具体分钟数不同（初级10/30、高级30/90），实际时长选课后从面板自动读取，升级学院不用改配置</span></div>',
    '<div class="frow"><span class="k">课时说明</span><span style="color:var(--sub);font-size:12px">短课单位消耗收益更高（每30分钟 +6属性/+30学分 vs 长课 +5/+25）</span></div>',
    ],'school')+
    FG('打工',[
    '<div class="frow"><span class="k">打工地点</span>'+sel('selLoc', ed.work_locations||[], ed.work_location)+'</div>',
    '<div class="frow"><span class="k">打工时长</span>'+sel('selDur', ['10分钟','45分钟','2小时'], ed.work_duration)+'</div>',
    '<div class="frow"><span class="k">优先雇佣</span>'+friendPicker('txtHire', ed.hire_name, '宠物名/主人名，空=自动选收益最高')+'</div>',
    '<div class="frow"><span class="k">等TA空闲</span><button class="sw'+(ed.hire_wait?' on':'')+'" id="swHireWait" title="开=优先雇佣的好友正在打工/学习（面板显示 出门中/被雇佣中）时不换人，点头像进主页读剩余时间，等到他结束再雇（期间先跑冒险/护理等其他任务）；显示 对方今天很累了 时等待无意义，仍换收益最高的人。需先填「优先雇佣」"></button></div>',
    ],'work')+
    FG('学习 / 打工 总控（8h 共享预算）',[
    '<div class="frow"><span class="k">今日学习</span><input type="number" id="numStudyQuota" min="0" max="24" step="1" title="今天最多学几小时。0 = 今天不学习。学习与打工共享同一份合计预算" value="'+(ed.study_quota_hours??8)+'"><span class="u">小时</span></div>',
    '<div class="frow"><span class="k">今日打工</span><input type="number" id="numWorkQuota" min="0" max="24" step="1" title="今天最多打几小时。0 = 今天不打工。学习与打工共享同一份合计预算" value="'+(ed.work_quota_hours??8)+'"><span class="u">小时</span></div>',
    '<div class="frow"><span class="k">合计预算</span><input type="number" id="numHour" min="0" max="24" step="1" title="学习+打工合计达到该时长后，今天不再学习（但仍可打工，直到「打工停」）。0=不限" value="'+(ed.daily_hour_limit??'')+'"><span class="u">小时（学习停）</span></div>',
    '<div class="frow"><span class="k">打工停</span><input type="number" id="numWorkStop" min="0" max="24" step="1" title="学习+打工合计达到该时长后今天不再打工。设得比「合计预算」大 = 学满后继续吃 25% 档打工；两个都填 8 = 合计满 8h 全停转冒险" value="'+(ed.work_stop_hours??'')+'"><span class="u">小时（打工停）</span></div>',
    '<div class="frow"><span class="k">金币阈值</span><input type="number" id="numCoin" min="0" step="100" title="金币 ≥ 该值优先学习，低于该值先打工赚够再学。只学习时请填 0，否则金币不足会先去打工" value="'+(ed.coin_threshold??'')+'"></div>',
    '<div class="frow"><span class="k">当前设置</span><span id="quotaHint" style="color:var(--sub);font-size:12px"></span></div>',
    '<div class="frow"><span class="k">一键预设</span><span style="display:flex;gap:6px;flex-wrap:wrap">'
      +'<button class="minibtn" data-quota="study8" title="学习8 / 打工0 / 合计8 / 打工停8 / 金币0">只学习 8h</button>'
      +'<button class="minibtn" data-quota="study12" title="学习12 / 打工0 / 合计12 / 打工停12 / 金币0">只学习 12h</button>'
      +'<button class="minibtn" data-quota="work8" title="学习0 / 打工8 / 合计8 / 打工停8">只打工 8h</button>'
      +'<button class="minibtn" data-quota="half" title="学习4 / 打工4 / 合计8 / 打工停8 / 金币2000">各半 4+4</button>'
      +'<button class="minibtn" data-quota="both" title="学习8 / 打工8 / 合计8 / 打工停8 / 金币2000（默认：按金币自动选）">都行 8+8</button>'
      +'</span></div>',
    ],'quota')+
    FG('疲劳分两层（8h 降收益仍可跑 / 12h 完全停止）',[
    '<div class="frow"><span class="k">第一层门槛</span><input type="number" id="numEffT1" min="0" max="24" step="1" title="学习+打工合计达到该时长进入【第一层】：收益效率降到 25%，但仍可继续学习/打工。游戏在 8h/12h 的提示文案相同，分层以本工具的时长账本为准" value="'+(ed.efficiency_tier1_hours??8)+'"><span class="u">小时 → 25%，仍可跑</span></div>',
    '<div class="frow"><span class="k">第二层门槛</span><input type="number" id="numEffT2" min="0" max="24" step="1" title="学习+打工合计达到该时长进入【第二层】：收益效率降到 10%，且完全禁止学习/打工（转冒险）。0 = 不设第二层" value="'+(ed.efficiency_tier2_hours??12)+'"><span class="u">小时 → 10%，完全停</span></div>',
    '<div class="frow"><span class="k">说明</span><span style="color:var(--sub);font-size:12px">第一层只降收益、不拦任务；第二层才禁止学习/打工。游戏疲劳提示会记录，但是否停由上面的合计时长决定</span></div>',
    ],'fatigue')+
    FG('调度',[
    '<div class="frow"><span class="k">主任务优先级</span>'+moSel(ed.main_order)+'</div>',
    ],'schedule')+
    FG('踩踩',[
    '<div class="frow"><span class="k">踩踩次数/天</span><input type="number" id="numVisit" min="0" step="1" value="'+(ed.visit_times??'')+'"></div>',
    ],'visit')+
    FG('PK',[
    '<div class="frow"><span class="k">PK 次数/天</span><input type="number" id="numPk" min="0" step="1" value="'+(ed.pk_times??'')+'"></div>',
    '<div class="frow"><span class="k">PK 只打</span>'+friendPicker('txtPkOnly', ed.pk_only, '昵称或宠物名，逗号分隔，空=不限')+'</div>',
    '<div class="frow"><span class="k">PK 跳过</span>'+friendPicker('txtPkSkip', ed.pk_skip, '昵称或宠物名，逗号分隔，空=不跳过')+'</div>',
    '<div class="frow"><span class="k">PK 打手</span>'+friendPicker('txtPkHelper', ed.pk_helper, '只雇这些宠物代打（逗号分隔，按优先序）')+'</div>',
    '<div class="frow"><span class="k">打手兜底</span><button class="sw'+(ed.pk_helper_fallback?' on':'')+'" id="swPkHf" title="开=名单里的打手都不可雇（被雇佣中/不可雇佣/已达上限）时，自动雇战力最高的可雇宠物"></button></div>',
    '<div class="frow"><span class="k">PK 等级上限</span><input type="number" id="numPkLv" min="-2" step="1" title="-1=只打比我低；-2=只打比打手低" value="'+(ed.pk_max_level??0)+'"></div>',
    '<div class="frow"><span class="k">等级过滤说明</span><span style="color:var(--sub);font-size:12px">0=不限；-1=只打比我低的；-2=只打比打手低的</span></div>',
    ],'pk')+
    FG('冒险',[
    '<div class="frow"><span class="k">冒险次数/天</span><input type="number" id="numAdv" min="0" step="1" title="0=不冒险；主号策略设 999 ≈ 不限（疲劳后全冒险）" value="'+(ed.adventure_times??'')+'"></div>',
    ],'adventure')+
    FG('护理',[
    '<div class="frow"><span class="k">护理阈值（体力/清洁）</span><span class="two"><input type="number" id="numEnergy" min="0" max="100" value="'+(ed.care_energy??'')+'"><input type="number" id="numClean" min="0" max="100" value="'+(ed.care_clean??'')+'"></span></div>',
    '<div class="frow"><span class="k">护理方式</span>'+sel('selCare', ['一键护理','ocr检测'], ed.care_method)+'</div>',
    '<div class="frow"><span class="k">补货数量（个）</span><input type="number" id="numExchange" min="1" max="99" step="1" title="饼干/香皂不足时一次金币买多少个" value="'+(ed.care_exchange??'')+'"></div>',
    ],'care')+
    FG('好友护理',[
    '<div class="frow"><span class="k">好友护理</span><button class="sw'+(ed.friend_care_enabled?' on':'')+'" id="swFC" title="开=按间隔到指定好友家护理（体力/清洁<90自动补）"></button></div>',
    '<div class="frow"><span class="k">好友护理对象</span>'+friendPicker('txtFCName', ed.friend_care_name, '宠物名或主人名')+'</div>',
    '<div class="frow"><span class="k">好友护理间隔（秒）</span><input type="number" id="numFCInt" min="30" step="30" value="'+(ed.friend_care_interval??'')+'"></div>',
    '<div class="frow"><span class="k">好友护理方式</span>'+sel('selFCMethod', ['ocr检测','一键护理'], ed.friend_care_method)+'</div>',
    ],'friend_care')+
    FG('被雇佣（帮好友打工）',[
    '<div class="frow"><span class="k">被雇佣托管</span><button class="sw'+(ed.employed_enabled?' on':'')+'" id="swEmp" title="开=定时出门检查是否被好友雇去打工"></button></div>',
    '<div class="frow"><span class="k">被雇佣处理</span>'+sel('selEmpAction', ['等到25/75（小于45min）','等到25/75','立刻召回','让利雇主（不召回）'], ed.employed_action)+'</div>',
    '<div class="frow"><span class="k">检查间隔（秒）</span><input type="number" id="numEmpInt" min="30" step="30" value="'+(ed.employed_interval??'')+'"></div>',
    ],'employed')+
    FG('福袋',[
    '<div class="frow"><span class="k">福袋领取</span><button class="sw'+(ed.gift_bag_enabled?' on':'')+'" id="swGiftBag" title="开=定时遍历好友领取系绳福袋"></button></div>',
    '<div class="frow"><span class="k">福袋扫描间隔（秒）</span><input type="number" id="numGbInt" min="60" step="60" value="'+(ed.gift_bag_interval??'')+'"></div>',
    ],'gift_bag')+
    FG('职业',[
    '<div class="frow"><span class="k">隐藏职业解锁监控</span><button class="sw'+(ed.career_watch?' on':'')+'" id="swCareer" title="开=每节课结算后读职业树；武术家/梦境旅人/大明星解锁时记录并推送通知"></button></div>',
    '<div class="frow"><span class="k">解锁后自动停学</span><button class="sw'+(ed.career_stop_study?' on':'')+'" id="swCareerStop" title="开=解锁时自动关闭学习任务（等你安排下一阶段）"></button></div>',
    '<div class="frow"><span class="k">兜底检查间隔（分钟）</span><input type="number" id="numCareerInt" min="0" step="10" title="0 = 只每节课后检查" value="'+(ed.career_interval??60)+'"></div>',
    ],'career')+
    FG('连接手机（ADB）',[
    '<div class="frow"><span class="k">adb 路径</span><input type="text" id="txtAdbPath" style="width:100%" placeholder="留空自动探测（PATH / Homebrew / Android SDK）" value="'+esc(ed.adb_path||'')+'"></div>',
    '<div class="frow"><span class="k">设备序列号</span><input type="text" id="txtAdbSerial" style="width:100%" placeholder="留空 = 用第一台在线设备" value="'+esc(ed.adb_serial||'')+'"></div>',
    '<div class="frow" style="display:block"><span style="color:var(--sub);font-size:12px;line-height:1.6">'
      +'这两项属于<b>连接层</b>，改完要<b>重启调度器</b>才生效（连接在调度器启动时建立）。'
      +'设备序列号也可以从下面列表里直接选。</span></div>',
    '<div class="frow"><span class="k">在线设备</span><button class="minibtn" id="btnAdbRefresh">刷新</button></div>',
    '<div class="frow" style="display:block"><div id="adbDevices" style="font-size:12px;color:var(--sub);line-height:1.8">点「刷新」查看当前设备</div></div>',
    '<div class="frow"><span class="k">连接地址</span><input type="text" id="txtAdbAddr" style="width:100%" placeholder="192.168.1.5:5555 / 127.0.0.1:7555（省略端口按 :5555）"></div>',
    '<div class="frow"><span class="k">无线 / 模拟器</span><button class="minibtn" id="btnAdbConnect">连接</button></div>',
    '<div class="frow" style="display:block"><div id="adbMsg" style="font-size:12px;line-height:1.8"></div></div>',
    ],'adb');
  // 通知页单独渲染（不放设置页：渠道配置项多，独立成板更清楚）
  renderNotifyForm(ed);
  // ---- ADB 卡片：两个文本框跟着自动保存走，两个按钮各调一次接口 ----
  ['#txtAdbPath','#txtAdbSerial'].forEach(id=>{
    const el=$(id); if(el) el.addEventListener('change',()=>markDirtyAndSave());
  });
  { const b=$('#btnAdbRefresh'); if(b) b.onclick=refreshAdb; }
  { const b=$('#btnAdbConnect'); if(b) b.onclick=adbConnectNow; }
  $('#swSchool').onclick=()=>{ $('#swSchool').classList.toggle('on'); markDirtyAndSave(); };
  $('#swFC').onclick=()=>{ $('#swFC').classList.toggle('on'); markDirtyAndSave(); };
  $('#swEmp').onclick=()=>{ $('#swEmp').classList.toggle('on'); markDirtyAndSave(); };
  $('#swGiftBag').onclick=()=>{ $('#swGiftBag').classList.toggle('on'); markDirtyAndSave(); };
  $('#swCareer').onclick=()=>{ $('#swCareer').classList.toggle('on'); markDirtyAndSave(); };
  $('#swCareerStop').onclick=()=>{ $('#swCareerStop').classList.toggle('on'); markDirtyAndSave(); };
  $('#swPkHf').onclick=()=>{ $('#swPkHf').classList.toggle('on'); markDirtyAndSave(); };
  $('#swHireWait').onclick=()=>{ $('#swHireWait').classList.toggle('on'); markDirtyAndSave(); };
  // 「当前设置」实时提示：把四个数字翻译成一句人话，避免填错组合（如只学习却
  // 忘了把金币阈值调 0 → 金币不足时会先去打工，看着像"没在学习"）
  const qv=id=>{const el=$(id); return el?parseInt(el.value,10):NaN;};
  const updQuotaHint=()=>{
    const el=$('#quotaHint'); if(!el) return;
    const sq=qv('#numStudyQuota'), wq=qv('#numWorkQuota');
    const lim=qv('#numHour'), stop=qv('#numWorkStop'), coin=qv('#numCoin');
    const parts=[];
    if(sq===0&&wq===0) parts.push('学习和打工都关了（只剩冒险/支线）');
    else if(sq>0&&wq===0) parts.push('只学习 '+sq+' 小时');
    else if(sq===0&&wq>0) parts.push('只打工 '+wq+' 小时');
    else if(sq>0&&wq>0) parts.push('学习 '+sq+'h + 打工 '+wq+'h，先到先切');
    if(lim>0) parts.push('合计满 '+lim+'h 停学习');
    if(stop>0) parts.push('满 '+stop+'h 停打工');
    if(sq>0&&wq===0&&coin>0) parts.push('⚠ 金币阈值 '+coin+' > 0：金币不足时会先去打工，想纯学习请设 0');
    el.textContent=parts.join('；');
    el.style.color=(sq>0&&wq===0&&coin>0)?'var(--warn)':'var(--sub)';
  };
  ['#numStudyQuota','#numWorkQuota','#numHour','#numWorkStop','#numCoin'].forEach(id=>{
    const el=$(id); if(el) el.addEventListener('input',updQuotaHint);
  });
  updQuotaHint();
  // 「当前选择」实时提示：科目与档位是两个独立字段，选「夏令营」时档位会被忽略
  // （夏令营固定第 7 张卡、不按属性选框），这里说清，避免看着矛盾
  const updSchoolHint=()=>{
    const el=$('#schoolHint'); if(!el) return;
    const attrEl=$('#selSchoolAttr'), durEl=$('#selSchoolDur');
    const attr=attrEl?attrEl.value:'', dur=durEl?durEl.value:'';
    if(attr==='夏令营'){
      el.textContent='科目=萌芽夏令营（卡7）：随机属性+5，不走属性课卡；下面的课时档位对它无效';
      el.style.color='var(--warn)';
    } else {
      el.textContent=attr+' · '+(dur==='长课'?'长课（卡4-6）':'短课（卡1-3）')
        +' —— 具体分钟数选课后自动读取（各学院不同）';
      el.style.color='var(--sub)';
    }
  };
  ['#selSchoolAttr','#selSchoolDur'].forEach(id=>{
    const el=$(id); if(el) el.addEventListener('change',updSchoolHint);
  });
  updSchoolHint();
  // 一键预设：把「学习/打工怎么分」这类需求一次填好 5 个字段（只改表单，点保存才落盘）
  const QUOTA_PRESETS={
    study8:  {study:8,  work:0, lim:8,  stop:8,  coin:0},
    study12: {study:12, work:0, lim:12, stop:12, coin:0},
    work8:   {study:0,  work:8, lim:8,  stop:8,  coin:2000},
    half:    {study:4,  work:4, lim:8,  stop:8,  coin:2000},
    both:    {study:8,  work:8, lim:8,  stop:8,  coin:2000},
  };
  document.querySelectorAll('[data-quota]').forEach(b=>{
    b.onclick=()=>{
      const p=QUOTA_PRESETS[b.dataset.quota]; if(!p) return;
      const set=(id,v)=>{const el=$(id); if(el) el.value=v;};
      set('#numStudyQuota',p.study); set('#numWorkQuota',p.work);
      set('#numHour',p.lim); set('#numWorkStop',p.stop); set('#numCoin',p.coin);
      markDirtyAndSave(); updQuotaHint();
      const msg=$('#saveMsg');
      if(msg){ msg.className='saveMsg'; msg.textContent='已应用「'+b.textContent+'」，自动保存中…'; }
    };
  });

  // ---- 一级：分类列表（按 tasks.order 的常见顺序排列）----
  // 一级：分组卡片（照 QQ 宠物设置页 —— 小标题在卡外，卡内多行带 › 箭头）
  const MENU=[
    ['核心任务',[['school','学习'],['work','打工'],['quota','学习/打工总控'],['fatigue','疲劳与收益档']]],
    ['日常互动',[['care','护理'],['friend_care','好友护理'],['visit','踩踩'],['pk','PK'],['adventure','冒险']]],
    ['扩展',[['employed','被雇佣'],['gift_bag','福袋'],['career','职业']]],
    ['系统',[['schedule','调度'],['adb','连接手机（ADB）']]],
  ];
  const menu=$('#setMenu');
  if(menu){
    menu.innerHTML=MENU.map(([sec,items])=>{
      const rows=items.filter(([k])=>document.getElementById('grp_'+k))
        .map(([k,t])=>'<div class="frow menurow" data-grp="'+k+'">'
          +'<span class="k">'+t+'</span><span class="chev">›</span></div>').join('');
      if(!rows) return '';
      return '<div class="msec"><div class="fsect">'+sec+'</div><div class="fsec">'+rows+'</div></div>';
    }).join('');
    menu.querySelectorAll('.menurow').forEach(r=>r.onclick=()=>openSetGroup(r.dataset.grp));
  }
  // 重建后恢复原来的层级（定时刷新会重跑本函数，直接 showSetIndex 会把
  // 正在看二级详情的用户弹回一级 —— 曾实测每 6 秒被弹回一次）
  // 定时刷新重建：**只恢复视图层级，不碰历史**（skipHistory=true）
  // ?grp=<key> 直开某个二级分组（与 ?tab= 同理，便于分享链接/截图/调试）：
  // 每次渲染都读一次 URL，不能只看 window.__setGrp —— 首次渲染的时序不确定
  // （数据到达才渲染，实测依赖 __setGrp 会不生效）。点"返回一级"会清掉该参数。
  let want=window.__setGrp || _grpFromUrl;
  _grpFromUrl='';                        // 直开参数只用一次
  if(want && document.getElementById('grp_'+want)) openSetGroup(want, true);
  else showSetIndex(true);
}

// ---- 通知页（独立板块）：渠道配置 + 事件开关 + 测试 ----
// 与设置页共用 setInit（同一份 editable 快照），但有自己的保存按钮/提示，
// 只提交本页字段（差量），互不干扰。
function renderNotifyForm(ed){
  const form=$('#notifyForm'); if(!form) return;
  // 与设置页共用同一个 card() 生成器（原来是自己拼的，容易走样）
  const FGn=(t,rows)=>card(t,rows);
  form.innerHTML=
    FGn('飞书群机器人',[
    '<div class="frow"><span class="k">启用</span><button class="sw'+(ed.notify_feishu_enabled?' on':'')+'" id="swFeishu" title="开=用飞书自定义机器人推送"></button></div>',
    '<div class="frow"><span class="k">webhook</span><input type="text" id="txtFsHook" style="width:100%" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…" value="'+esc(ed.notify_feishu_webhook)+'"></div>',
    '<div class="frow"><span class="k">加签密钥</span><input type="text" id="txtFsSecret" placeholder="安全设置选「签名校验」时必填，否则留空" value="'+esc(ed.notify_feishu_secret)+'"></div>',
    ])+
    FGn('Telegram Bot',[
    '<div class="frow"><span class="k">启用</span><button class="sw'+(ed.notify_telegram_enabled?' on':'')+'" id="swTg" title="开=用 Telegram Bot 推送"></button></div>',
    '<div class="frow"><span class="k">Bot Token</span><input type="text" id="txtTgToken" style="width:100%" placeholder="123456789:AAE…（@BotFather 获取）" value="'+esc(ed.notify_telegram_token)+'"></div>',
    '<div class="frow"><span class="k">Chat ID</span><input type="text" id="txtTgChat" placeholder="私聊填数字 id；群/频道填 -100…" value="'+esc(ed.notify_telegram_chat_id)+'"></div>',
    ])+
    FGn('推送哪些事件',[
    '<div class="frow"><span class="k">今日配额达成</span><button class="sw'+(ed.notify_quota_done?' on':'')+'" id="swQuotaNotify" title="开=当天学习/打工打满你设的配额时推送（含当前截图）"></button></div>',
    '<div class="frow"><span class="k">隐藏职业解锁</span><button class="sw'+(ed.notify_career?' on':'')+'" id="swCareerNotify" title="开=武术家/梦境旅人/大明星解锁时推送（含职业树截图）"></button></div>',
    '<div class="frow"><span class="k">完成类通知总开关</span><button class="sw'+(ed.notify_event_notify?' on':'')+'" id="swEventNotify" title="关掉后所有「完成」类通知（如配额达成）都不发；任务失败告警不受影响"></button></div>',
    ])+
    FGn('异常提醒',[
    '<div class="frow"><span class="k">异常降级提醒</span><button class="sw'+(ed.notify_error_notify?' on':'')+'" id="swErrNotify" title="开=出现「没崩但静默降级」的错误时推送，如同类错误 30 分钟内最多一条。典型：配置读取失败后一直沿用旧配置，界面改什么都不生效"></button></div>',
    '<div class="frow" style="display:block"><span style="color:var(--sub);font-size:12px;line-height:1.6">'
    +'任务失败告警（学习/打工反复失败后退出调度器）<b>始终会发</b>，不受本页开关影响；'
    +'这里的开关只控制「完成通知」与「异常降级提醒」。'
    +'</span></div>',
    ])+
    FGn('测试与说明',[
    '<div class="frow"><span class="k">测试</span><span class="ctrl">'
    +'<span id="notifyTestMsg"></span>'
    +'<button class="minibtn" id="btnTestNotify">发送测试通知</button></span></div>',
    '<div class="noterow"><span class="nt">飞书</span><span class="nb">'
    +'群 → 右上角设置 → 群机器人 → 添加机器人 → 自定义机器人，复制 webhook 地址；'
    +'安全设置选「签名校验」就把密钥填到加签密钥（选「自定义关键词」可留空，关键词需含"QQ宠物"）。'
    +'</span></div>',
    '<div class="noterow"><span class="nt">Telegram</span><span class="nb">'
    +'跟 @BotFather 发 /newbot 建机器人拿 Token；<b>先给机器人发一条消息</b>，'
    +'再用 @userinfobot 查自己的 Chat ID（群/频道是 -100 开头的负数）。'
    +'</span></div>',
    '<div class="noterow"><span class="nt">告警</span><span class="nb">'
    +'任务失败告警始终会发（不受上面开关影响）；职业解锁与配额达成各有一个开关。'
    +'</span></div>',
    ]);
  ['#swFeishu','#swTg','#swQuotaNotify','#swCareerNotify','#swEventNotify','#swErrNotify'].forEach(id=>{
    const el=$(id); if(el) el.onclick=()=>{ el.classList.toggle('on'); };
  });
  const tn=$('#btnTestNotify');
  if(tn) tn.onclick=async()=>{
    const msg=$('#notifyTestMsg');
    msg.style.color='var(--sub)'; msg.textContent='先保存当前设置…';
    try{
      await saveNotifySettings(true);   // silent：不在保存区提示，只在测试行显示
      msg.textContent='正在发送…';
      const r=await fetch('/api/notify/test',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({target:'all'})});
      const d=await r.json();
      msg.style.color=d.ok?'#16a34a':'#b45309';
      msg.textContent=(d.ok?'✅ ':'✗ ')+(d.msg||'');
    }catch(e){
      msg.style.color='#b45309'; msg.textContent='测试失败：'+e.message;
    }
  };
}

async function saveNotifySettings(silent){
  if(!setInit) return;
  const msg=$('#notifySaveMsg');   // 保存按钮已移除（改动即自动保存）
  const updates={};
  const sw=(id,key)=>{const el=$(id); if(!el)return; const v=el.classList.contains('on');
                      if(!!v!==!!setInit[key]) updates[key]=v;};
  const tx=(id,key)=>{const el=$(id); if(!el)return; const v=el.value.trim();
                      if(v!==(setInit[key]||'')) updates[key]=v;};
  sw('#swFeishu','notify_feishu_enabled');   tx('#txtFsHook','notify_feishu_webhook');
  tx('#txtFsSecret','notify_feishu_secret');
  sw('#swTg','notify_telegram_enabled');     tx('#txtTgToken','notify_telegram_token');
  tx('#txtTgChat','notify_telegram_chat_id');
  sw('#swQuotaNotify','notify_quota_done');  sw('#swCareerNotify','notify_career');
  sw('#swEventNotify','notify_event_notify'); sw('#swErrNotify','notify_error_notify');
  if(!Object.keys(updates).length){
    if(!silent&&msg){ msg.className='saveMsg'; msg.textContent='没有改动'; }
    return {ok:true, changed:0};
  }
  try{
    const r=await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({updates})});
    const d=await r.json();
    if(d.rejected&&d.rejected.length){
      if(!silent&&msg){ msg.className='saveMsg err'; msg.textContent='部分未保存：'+d.rejected.join('；'); }
      return {ok:false, rejected:d.rejected};
    }
    // 保存成功：把快照同步成新值，避免下次又把它当"改动"重复提交
    Object.keys(updates).forEach(k=>setInit[k]=updates[k]);
    if(!silent&&msg){ msg.className='saveMsg'; msg.textContent='✅ 已保存，下一轮调度生效'; }
    refreshData();
    return {ok:true, changed:Object.keys(updates).length};
  }catch(e){
    if(!silent&&msg){ msg.className='saveMsg err'; msg.textContent='保存失败：'+e.message; }
    return {ok:false, err:String(e.message||e)};
  }finally{
    }
}
// ---- 内页自动分节：把散装内容按"小标题 + 白卡"归拢（照设置页排版） ----
// 各内页结构不一，这里用 DOM 包装统一：
//   .subh / .advcap 作为小节标题 -> 其后到下一个标题之前的内容包进一张白卡。
(function(){
  // 注意：notify/set 自带 .fgrp+.fsec 分节结构，不能再套 .pgsec（会双层白卡）
  // 注意：log 页已手写 .pgsec 分节（工具栏/输出/异常截图），不能再自动包
  const PAGES=['adv','plan'];
  function wrap(page){
    const sec=document.querySelector('main > [data-page="'+page+'"]');
    if(!sec || sec.dataset.wrapped==='1') return;
    const kids=[...sec.children].filter(el=>!el.classList.contains('navhead'));
    if(!kids.length) return;
    const groups=[]; let cur=null;
    kids.forEach(el=>{
      const isTitle = el.classList.contains('subh') || el.classList.contains('advcap');
      if(isTitle){ cur={title:el.textContent.trim(), els:[]}; groups.push(cur); el.remove(); }
      else if(cur){ cur.els.push(el); }
      else { cur={title:'', els:[el]}; groups.push(cur); }
    });
    // 清掉空组
    const use=groups.filter(g=>g.els.length);
    if(!use.length) return;
    const frag=document.createDocumentFragment();
    use.forEach(g=>{
      const secEl=document.createElement('div'); secEl.className='pgsec';
      if(g.title){ const t=document.createElement('div'); t.className='pgsec-t'; t.textContent=g.title; secEl.appendChild(t); }
      const c=document.createElement('div'); c.className='pgsec-c';
      g.els.forEach(e=>c.appendChild(e));
      secEl.appendChild(c);
      frag.appendChild(secEl);
    });
    // 插到 navhead 之后
    const nh=sec.querySelector('.navhead');
    if(nh) nh.after(frag); else sec.insertBefore(frag, sec.firstChild);
    sec.dataset.wrapped='1';
  }
  function run(){ PAGES.forEach(wrap); }
  run();
  window.__wrapPages=run;   // 数据刷新后重建元素时再跑
})();

// ---- 改动即自动保存（已移除所有"保存设置"按钮） ----
// 标记脏 + 600ms 防抖后自动提交（连续输入不会每次都发请求）；
// setDirty 同时用于阻止定时刷新重建表单覆盖用户输入。
let _autoT=null;
function markDirtyAndSave(){
  setDirty=true;
  _lastFormTouch=Date.now();
  if(_autoT) clearTimeout(_autoT);
  _autoT=setTimeout(async()=>{
    _autoT=null;
    try{
      if(document.querySelector('#setForm')  && $('#setForm').offsetParent!==null)  await saveSettings();
      if(document.querySelector('#notifyForm')&& $('#notifyForm').offsetParent!==null) await saveNotifySettings(true);
    }catch(e){ /* 保存失败已在各自函数内提示 */ }
  },600);
}
// ---- ADB 卡片：列设备 / 连接无线设备 ----
async function refreshAdb(){
  const box=$('#adbDevices'); if(!box) return;
  box.textContent='查询中…';
  try{
    const d=await j('/api/adb');
    const devs=d.devices||[];
    const head='当前 adb：'+esc(d.resolved||'（未找到）')
      +'<br>配置的序列号：'+(d.serial?esc(d.serial):'（空 = 用第一台在线设备）');
    if(!devs.length){
      box.innerHTML=head
        +(d.error?'<br><span style="color:var(--warn)">'+esc(d.error)+'</span>':'')
        +'<br>没有在线设备。真机请插 USB 并在手机上允许调试；模拟器 / 无线调试在下面填地址点「连接」。';
      return;
    }
    box.innerHTML=head+devs.map(x=>{
      const on=x.state==='device', cur=(d.serial===x.serial);
      return '<br><span style="color:'+(on?'var(--ok)':'var(--warn)')+'">●</span> '
        +'<b>'+esc(x.serial)+'</b>'+(x.model?'（'+esc(x.model)+'）':'')
        +' <span style="color:var(--sub)">'+esc(x.state)+'</span>'
        +(cur?' <span style="color:var(--accent)">← 当前配置</span>':'')
        +' <button class="minibtn" data-adbserial="'+esc(x.serial)+'">用这台</button>';
    }).join('');
    box.querySelectorAll('[data-adbserial]').forEach(b=>{
      b.onclick=()=>{
        const el=$('#txtAdbSerial'); if(!el) return;
        el.value=b.dataset.adbserial; markDirtyAndSave();
        const m=$('#adbMsg');
        if(m){ m.style.color='var(--ok)'; m.textContent='已填入序列号 —— 重启调度器后生效'; }
      };
    });
  }catch(e){
    box.innerHTML='<span style="color:var(--warn)">查询失败：'+esc(e.message)+'</span>';
  }
}
async function adbConnectNow(){
  const msg=$('#adbMsg'), el=$('#txtAdbAddr');
  const addr=el?el.value.trim():'';
  if(!addr){ if(msg){ msg.style.color='var(--warn)'; msg.textContent='请先填连接地址'; } return; }
  if(msg){ msg.style.color='var(--sub)'; msg.textContent='连接中…'; }
  try{
    const r=await fetch('/api/adb/connect',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({addr})});
    const d=await r.json();
    if(msg){ msg.style.color=d.ok?'var(--ok)':'var(--warn)';
             msg.textContent=(d.ok?'✅ ':'⚠ ')+(d.msg||''); }
    refreshAdb();
  }catch(e){
    if(msg){ msg.style.color='var(--warn)'; msg.textContent='连接失败：'+e.message; }
  }
}
document.addEventListener('input',e=>{
  if(!e.target||!e.target.closest) return;
  if(e.target.closest('#notifyForm')||e.target.closest('#setForm')) markDirtyAndSave();
});
document.addEventListener('change',e=>{
  if(!e.target||!e.target.closest) return;
  if(e.target.closest('#notifyForm')||e.target.closest('#setForm')) markDirtyAndSave();
});

async function saveSettings(){
  if(!setInit) return;
  const msg=$('#saveMsg');
  const updates={};
  const schoolEnabledNew = !$('#swSchool').classList.contains('on');
  if(!!schoolEnabledNew !== !!setInit.school_enabled) updates.school_enabled=schoolEnabledNew;
  const fcEnabledNew = $('#swFC').classList.contains('on');
  if(!!fcEnabledNew !== !!setInit.friend_care_enabled) updates.friend_care_enabled=fcEnabledNew;
  const empEnabledNew = $('#swEmp').classList.contains('on');
  if(!!empEnabledNew !== !!setInit.employed_enabled) updates.employed_enabled=empEnabledNew;
  const gbEnabledNew = $('#swGiftBag').classList.contains('on');
  if(!!gbEnabledNew !== !!setInit.gift_bag_enabled) updates.gift_bag_enabled=gbEnabledNew;
  const cwNew = $('#swCareer').classList.contains('on');
  if(!!cwNew !== !!setInit.career_watch) updates.career_watch=cwNew;
  const csNew = $('#swCareerStop').classList.contains('on');
  if(!!csNew !== !!setInit.career_stop_study) updates.career_stop_study=csNew;
  const hfNew = $('#swPkHf').classList.contains('on');
  if(!!hfNew !== !!setInit.pk_helper_fallback) updates.pk_helper_fallback=hfNew;
  const hwNew = $('#swHireWait').classList.contains('on');
  if(!!hwNew !== !!setInit.hire_wait) updates.hire_wait=hwNew;
  const getv=id=>($(id)?$(id).value.trim():'');
  const num=(id,key)=>{const v=getv(id); if(v==='')return; const n=parseInt(v,10); if(!isNaN(n)&&n!==setInit[key]) updates[key]=n;};
  const selc=(id,key)=>{const v=getv(id); if(v&&v!==setInit[key]) updates[key]=v;};
  const txtc=(id,key)=>{const v=getv(id); if(v!==(setInit[key]||'')) updates[key]=v;};
  selc('#selLoc','work_location'); selc('#selDur','work_duration'); selc('#selCare','care_method'); selc('#selFCMethod','friend_care_method'); selc('#selEmpAction','employed_action'); selc('#selMainOrder','main_order'); selc('#selSchoolAttr','school_attribute'); selc('#selSchoolDur','school_duration');
  txtc('#txtHire','hire_name');
  num('#numCoin','coin_threshold'); num('#numHour','daily_hour_limit'); num('#numWorkStop','work_stop_hours'); num('#numSchoolTimes','school_times');
  num('#numStudyQuota','study_quota_hours'); num('#numWorkQuota','work_quota_hours'); num('#numEffT1','efficiency_tier1_hours'); num('#numEffT2','efficiency_tier2_hours');
  num('#numVisit','visit_times'); num('#numPk','pk_times'); num('#numAdv','adventure_times');
  txtc('#txtPkOnly','pk_only'); txtc('#txtPkSkip','pk_skip'); num('#numPkLv','pk_max_level'); txtc('#txtPkHelper','pk_helper');
  num('#numEnergy','care_energy'); num('#numClean','care_clean'); num('#numExchange','care_exchange'); num('#numGbInt','gift_bag_interval'); num('#numCareerInt','career_interval');
  txtc('#txtFCName','friend_care_name'); num('#numFCInt','friend_care_interval'); num('#numEmpInt','employed_interval');
  // 连接层（ADB）：改完要重启调度器才生效，卡片里已提示
  txtc('#txtAdbPath','adb_path'); txtc('#txtAdbSerial','adb_serial');
  // 注意：通知渠道字段在**通知页**（#notifyForm），由 saveNotifySettings 单独提交，
  // 这里不要再取（那些 id 已不在本表单里，取了会是 null 而报错）。
  if(!Object.keys(updates).length){ msg.className='saveMsg'; msg.textContent='没有改动'; btn.disabled=false; return; }
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({updates})});
    const d=await r.json();
    if(d.rejected&&d.rejected.length){ msg.className='saveMsg err'; msg.textContent='部分未保存：'+d.rejected.join('；'); }
    else { setDirty=false; msg.className='saveMsg'; msg.textContent='✅ 已保存，下一轮调度生效'; refreshData(); }
  }catch(e){ msg.className='saveMsg err'; msg.textContent='保存失败：'+e.message; }
  if(btn) btn.disabled=false;
}
// 保存按钮已移除 -> 改为改动即自动保存（见下）

$('#btnAltPreset').onclick=async()=>{
  const name=($('#txtAltMain')?$('#txtAltMain').value:'').trim();
  const msg=$('#presetMsg');
  if(!name){ msg.className='saveMsg err'; msg.textContent='先填大号名称（主人昵称或宠物名都能匹配）'; return; }
  const btn=$('#btnAltPreset'); btn.disabled=true;
  msg.className='saveMsg'; msg.textContent='应用中…';
  try{
    const r=await fetch('/api/preset/alt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    const d=await r.json();
    if(d.ok){
      setDirty=false;
      msg.className='saveMsg';
      msg.textContent='✅ 小号工具人模式已应用（'+Object.keys(d.applied||{}).length+' 项），下一轮调度生效';
      refreshData();
    } else {
      msg.className='saveMsg err';
      msg.textContent='未完全应用：'+((d.rejected||[]).join('；')||'未知');
    }
  }catch(e){ msg.className='saveMsg err'; msg.textContent='应用失败：'+e.message; }
  btn.disabled=false;
};

let shotUrl=null,shotBusy=false;
async function refreshShot(force){
  if(shotBusy)return; shotBusy=true;
  try{
    const r=await fetch('/api/screenshot'+(force?('?t='+Date.now()):''),{cache:'no-store'});
    if(!r.ok) throw new Error((await r.text()).slice(0,80));
    const b=await r.blob(); const u=URL.createObjectURL(b);
    const img=$('#phoneShot'); if(shotUrl) URL.revokeObjectURL(shotUrl);
    shotUrl=u;
    img.onload=()=>$('#shotLink').classList.remove('loading','failed');
    img.src=u;
    $('#shotMeta').textContent='拍摄 '+((r.headers.get('X-Shot-At')||'').slice(0,5));
    $('#shotErr').textContent='';
  }catch(e){
    $('#shotLink').classList.add('failed');
    $('#shotErr').textContent='获取失败，点“刷新”重试';
  }
  shotBusy=false;
}
$('#btnShot').onclick=()=>refreshShot(true);


// 任务开关：点击勾选 → 写入 config（ruamel 保注释），调度器下一轮热加载生效
document.getElementById('taskList').addEventListener('click', async ev => {
  const cb = ev.target.closest('.mcb'); if(!cb) return;
  const k = cb.dataset.k; if(!k) return;
  const on = !cb.classList.contains('on');
  cb.classList.toggle('on', on);
  const nm = cb.parentElement.querySelector('.mname');
  if(nm) nm.classList.toggle('off', !on);
  try {
    await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({updates:{[k+'_enabled']:on}})});
    setTimeout(refreshData, 1500);
  } catch(e) { cb.classList.toggle('on'); setTimeout(refreshData, 800); }
});

// 状态栏配色跟随当前页（iOS 独立 Web App 下状态栏那条带取页面背景色，
// 见 <meta name="theme-color"> 上方注释）。
// 色值**只定义在 CSS**（--qp-statusbar，按场景/深浅色自动取值），这里读出来同步给
// meta —— 不在 JS 里再抄一份色表，否则以后改色必然漏一处。
// **注意**：必须在 showTab 的 `if(name===curTab) return;` 之前调用 ——
// 否则"首帧就是内页"（localStorage 记住的 tab / ?tab=set 直开）时不会被同步。
function syncThemeColor(name){
  const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  let want = '#FFFFFF';   // 内页顶栏是白的
  if(name==='main'){
    const v = getComputedStyle(document.documentElement)
                .getPropertyValue('--qp-statusbar').trim();
    if(v) want = v;
    else want = dark ? '#A9722D' : '#D5A758';   // CSS 变量读不到时的兜底
  }
  document.querySelectorAll('meta[name="theme-color"]').forEach(function(m){
    // 两组 meta 各带一个 media 查询，只改属于当前主题的那条，
    // 免得把另一主题的值也覆盖成当前主题的色（切系统主题时就会串色）
    const isDark = (m.getAttribute('media')||'').indexOf('dark') >= 0;
    if(isDark === !!dark) m.setAttribute('content', want);
  });
}
if(window.matchMedia){
  try{ window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', ()=>syncThemeColor(curTab)); }catch(e){}
}

// 当前页（供 history 手势判断）
let curTab = 'main';
// 导航层级：**0 = 总览，1 = 内页，2 = 设置二级**。
//
// **历史模型（第十二轮修）：栈与"导航层级"严格对应，最多三层**
//   往下一层        pushState      （总览 -> 内页、设置一级 -> 二级）
//   同级互切        replaceState   （内页 <-> 内页，栈不增长）
//   往上一层        history.back() （**退栈**，不是再压一条上层页）
//   跨层回总览      一次退够（go(-navDepth)）
//
// 改前是"访问轨迹"模型：每次切页都 push，连"返回"按钮也 push，于是
//   ① 侧滑要一路退回**访问过的每一页**（实测 总览→日志→设置→设置二级→冒险→职业
//      之后要滑 5 次才回总览，用户反馈"右滑了很多很多次才回到总览页"）
//   ② 点"返回"回总览后再侧滑，**又回到刚才那个内页**（栈里刚压了一条总览）
// 根因就一句话：**按钮在压栈、手势在退栈，两者方向相反**。
// 现在层级与栈一一对应：二级侧滑 -> 一级，再侧滑 -> 总览（用户要的两段式钻取）。
let navDepth = 0;
let pendingTab = null;   // 跳级退栈（二级 -> 别的内页）时，退到总览后再压目标页
function showTab(name, skipHistory){
  // 离开设置页时重置层级，下次进入从一级列表开始
  if(name!=='set' && window.__setGrp) window.__setGrp=null;
  document.querySelectorAll('main > [data-page]').forEach(el=>el.classList.toggle('hide', el.dataset.page!==name));
  // 内页（非总览）把 html 底色切白：body 的 padding-top 露出的就是 html 底色，
  // 总览露房间暖色图、内页露白色顶栏（见 CSS 里 html[data-page] 那条注释）
  document.documentElement.setAttribute('data-page', name);
  syncThemeColor(name);   // 状态栏同步（须在下面的早退之前，见函数注释）
  // 两列导航都要更新选中态（#tabbar2 是右列，早期漏了）
  document.querySelectorAll('#tabbar button, #tabbar2 button').forEach(b=>b.classList.toggle('on', b.dataset.tab===name));
  try{localStorage.setItem('qpet_tab',name);}catch(e){}
  // **层级同步必须放在早退之前**：侧滑回到"设置一级"时页面名没变（set -> set），
  // 若放在早退之后，navDepth 会停在 2，接着点"返回总览"就会多退一层（甚至退出应用）。
  if(skipHistory) navDepth = (name==='main') ? 0 : 1;
  if(name===curTab) return;
  curTab=name;
  // 侧滑/浏览器返回（popstate）驱动的切换：层级已同步，不再动历史
  if(skipHistory) return;
  // 跳级退栈（go(-2)）还在路上：本次只渲染页面、不动历史，
  // 否则连续两次切页会把栈算歪，最坏情况退过头直接退出应用。
  if(pendingTab){ navDepth = (name==='main') ? 0 : 1; return; }
  // showTab 只处理页面级（总览 0 / 内页 1）；设置二级由 openSetGroup 负责（层级 2）
  const target = (name==='main') ? 0 : 1;
  const url = (name==='main') ? (location.pathname + rootSearch) : ('?tab='+name);
  try{
    if(target > navDepth){                       // 往下一层：压栈
      navDepth = target;
      history.pushState({tab:name}, '', url);
    } else if(target === navDepth){              // 同级互切：替换，栈不增长
      if(target === 1) history.replaceState({tab:name}, '', url);
    } else if(target === 0){                     // 回总览：一次退到根
      const steps = navDepth;
      navDepth = 0;
      if(steps === 1) history.back(); else history.go(-steps);
    } else {                                     // 二级 -> 别的内页：先退到总览，落定后再压目标页
      pendingTab = name;
      navDepth = 0;
      history.go(-2);
    }
  }catch(e){}
}
// 侧滑/浏览器返回：退到栈里上一条。已在总览（根条目）则放行，让浏览器正常退栈。
window.addEventListener('popstate', function(e){
  const st=e.state||{};
  if(pendingTab){   // 跳级退栈的收尾：落回总览后再压目标页（见 showTab 最后一条分支）
    const t=pendingTab; pendingTab=null;
    try{ history.pushState({tab:t}, '', '?tab='+t); }catch(err){}
    navDepth=1;
    showTab(t, true);
    return;
  }
  const t=st.tab || 'main';
  // 设置页：按这条记录的 grp 决定停在一级还是二级（层级也要跟着落定：
  // showTab 只认页面级 0/1，二级的 2 必须在这里补上，否则"返回设置列表"会判不出该退栈）
  if(t==='set'){
    showTab('set', true);
    if(st.grp){ openSetGroup(st.grp, true); navDepth=2; }
    else { showSetIndex(true); navDepth=1; }
    return;
  }
  if(t===curTab) return;   // 栈里这条就是当前页（"回总览"的 back() 落到总览就是这种）
  showTab(t, true);
});
// tab 按钮：#tabbar（左列4个）+ #tabbar2（右列3个）都要绑
document.querySelectorAll('#tabbar button, #tabbar2 button').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
// 内页返回（.home 里的导航会随总览页一起隐藏，故内页需要独立返回入口）
// 内页顶栏返回按钮（统一 .backbtn[data-back]；二级设置页的 #btnSetBack 有自己的处理）
document.addEventListener('click',function(e){
  const b=e.target.closest && e.target.closest('.backbtn[data-back]');
  if(b){ showTab(b.dataset.back||'main'); }
},true);
let initTab='main';
try{initTab=localStorage.getItem('qpet_tab')||'main';}catch(e){}
// 支持 ?tab=set 直开某页（便于分享链接/截图/调试）
try{
  const qp=new URLSearchParams(location.search).get('tab');
  // 注意：#tabbar 只含左列 4 个按钮，set/log/shot 在 #tabbar2 —— 要全局找
  if(qp && document.querySelector('button[data-tab="'+qp+'"]')) initTab=qp;
}catch(e){}
// ?tab=set&grp=<key> 直开设置页的某个二级分组（同理，便于分享链接/截图/调试）。
// **必须在这里读、并且存进独立变量**，两个坑：
//   ① showTab('main') 会清 window.__setGrp（"离开设置页重置层级"），存那里会被抹掉；
//   ② showTab('set') 会把 URL 重写成 '?tab=set'，之后再读 location.search 就没有 grp 了。
let _grpFromUrl='';
try{ _grpFromUrl=new URLSearchParams(location.search).get('grp')||''; }catch(e){}
// 初始：先取好友名单（下拉选择用），再渲染 tab —— 否则首次渲染
// 的 datalist 是空的，用户以为"没有好友可选"
(async function initFriends(){
  try{
    const r=await fetch('/api/friends'); const d=await r.json();
    if(Array.isArray(d.friends) && d.friends.length){
      FRIENDS=d.friends;
      // 名单到手后重渲染一次设置表单，让下拉项立刻可用
      if(window.__lastEditable) renderSettings(window.__lastEditable);
    }
  }catch(e){}
})();
// 初始历史：**根条目永远是总览** —— 这样"内页 -> 总览"用 history.back() 一定落回
// 总览，而不会退到浏览器的上一页/直接退出应用（曾把 initTab 直接 replace 成根条目，
// 那样根条目可能是内页，退栈就退到应用外了）。
// 直开内页（?tab=set / localStorage 记住的页）时再压一层，栈 = [总览, 内页]。
// 查询串只清 `?tab=`（当前页由 history.state 决定），**其它参数要保留**
// ——曾整串丢掉，连 ?case= 这种无关参数一起没了。
let rootSearch='';
try{
  const sp=new URLSearchParams(location.search); sp.delete('tab');
  const qs=sp.toString(); rootSearch = qs ? ('?'+qs) : '';
}catch(e){}
try{ history.replaceState({tab:'main'}, '', location.pathname + rootSearch); }catch(e){}
curTab='main';
showTab('main', true);
if(initTab!=='main') showTab(initTab);

setInterval(()=>{if(!document.hidden)refreshLogs()},3000);
setInterval(()=>{if(!document.hidden)refreshData()},6000);
setInterval(()=>{if(!document.hidden)refreshAdventure()},10000);
setInterval(()=>{if(!document.hidden)refreshPlan()},15000);
setInterval(()=>{if(!document.hidden)refreshShot(false)},15000);
refreshData();refreshLogs();refreshAdventure();refreshPlan();refreshShot(false);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){refreshData();refreshLogs();refreshAdventure();refreshPlan();refreshShot(false)}});
try{
  const okSW=('serviceWorker' in navigator)&&(location.protocol==='https:'||location.hostname==='localhost'||location.hostname==='127.0.0.1');
  if(okSW) navigator.serviceWorker.register('/sw.js').catch(()=>{});
}catch(e){}

// ---- 舞台单位基准：1dp = 可用宽度 / 480 ----
// 不用 CSS 的 100vw：它含滚动条宽度（实测 clientWidth 489 vs 100vw 500），
// 会让所有绝对坐标偏大约 2%。这里按真实可用宽度精确计算并写入 --vu。
(function(){
  function setU(){
    // 用视口宽度算 1dp（不再依赖 .home 存在 —— 设置页等页面没有 .home，
    // 早期版本把 --u 定义在 .home 上，导致那些页面所有 calc(var(--u)*N) 失效）
    var w=document.documentElement.clientWidth || window.innerWidth;
    document.documentElement.style.setProperty('--vu',(w/360)+'px');
  }
  setU();
  window.addEventListener('resize',setU);
  window.addEventListener('orientationchange',setU);
})();

// ---- 功能栏与任务列表底部对齐 ----
// 列表高度随任务数变化（9 项时约 227dp），固定 top 不是压住 deck 就是留大空。
// 渲染后按列表实际底部反推功能栏 top（功能栏总高 230dp：20 + 70*2 + 70 + 20 间距）。
(function(){
  var home=document.querySelector('.home'),
      tl=document.getElementById('taskList'),
      fb=document.querySelector('.funcbar');
  if(!home||!tl||!fb) return;
  function place(){
    var u=parseFloat(getComputedStyle(home).getPropertyValue('--vu'))||(home.clientWidth/360);
    var hr=home.getBoundingClientRect(), tr=tl.getBoundingClientRect();
    var want=(tr.bottom-hr.top)/u;                 // 期望：功能栏底端 = 列表底端
    var h2=fb.getBoundingClientRect().height/u;    // 功能栏实际高
    var top=want-h2;
    if(top<100) top=319.3;                         // 列表过短时回官方位置
    fb.style.top='calc(var(--u) * '+top.toFixed(1)+')';
  }
  place();
  window.addEventListener('resize',place);
  var tlEl=document.getElementById('taskList');
  if(tlEl&&window.MutationObserver) new MutationObserver(place).observe(tlEl,{childList:true});
})();


</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, ctype: str, body: bytes, cache: str = 'no-store'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', cache)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        try:
            if path == '/':
                self._send(200, 'text/html; charset=utf-8', HTML.encode('utf-8'))
            elif path == '/manifest.json':
                self._send(200, 'application/manifest+json',
                           MANIFEST_JSON.encode('utf-8'))
            elif path == '/sw.js':
                self._send(200, 'application/javascript', SW_JS.encode('utf-8'))
            elif path.startswith('/qp-icons/'):
                # 路径归属校验：resolve 后必须仍在 static/qp-icons 内
                # （不要用 `'qp-icons' in fp.parts` 那种写法——`/qp-icons/../x.png`
                #   的 parts 里同样含 'qp-icons'，会放行穿越，实测返回 200）
                fp = (BASE / 'static' / path.lstrip('/')).resolve()
                root = (BASE / 'static' / 'qp-icons').resolve()
                # 白名单扩展名（背景是 .jpg；仍不接受任意后缀，避免误发其它文件）
                CTYPE = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                         '.webp': 'image/webp', '.svg': 'image/svg+xml'}
                if fp.exists() and fp.suffix.lower() in CTYPE and fp.is_relative_to(root):
                    self._send(200, CTYPE[fp.suffix.lower()], fp.read_bytes(), 'max-age=3600')
                else:
                    self._send(404, 'text/plain', b'not found')
            elif path in ('/icon-192.png', '/icon-512.png', '/apple-touch-icon.png'):
                # apple-touch-icon.png：iOS 主屏图标（HTML 里 <link rel="apple-touch-icon"> 指向它）。
                # 单独一个文件名是为了「换图标时能换 URL」—— iOS 按 URL 缓存该图标，
                # 复用 icon-192.png 的话内容更新了主屏也不会变（见 HTML 里的注释）。
                fp = BASE / 'static' / path.lstrip('/')
                if fp.exists():
                    data = fp.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/png')
                    self.send_header('Cache-Control', 'max-age=3600')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._send(404, 'text/plain', b'not found')
            elif path == '/api/data':
                body = json.dumps(build_data(), ensure_ascii=False).encode('utf-8')
                self._send(200, 'application/json; charset=utf-8', body)
            elif path == '/api/adb':
                # 单独一个接口（而不是并进 /api/data）：它要真跑一次 adb devices，
                # 几百毫秒起步，放 /api/data 会拖慢每 6 秒一次的整体刷新。
                body = json.dumps(adb_info(), ensure_ascii=False).encode('utf-8')
                self._send(200, 'application/json; charset=utf-8', body)
            elif path == '/api/adventure':
                q = parse_qs(u.query)
                body = json.dumps(adventure_data((q.get('date') or [''])[0]),
                                  ensure_ascii=False).encode('utf-8')
                self._send(200, 'application/json; charset=utf-8', body)
            elif path == '/api/friends':
                body = json.dumps({'friends': list_friends(), **friends_meta()},
                                  ensure_ascii=False).encode('utf-8')
                self._send(200, 'application/json; charset=utf-8', body)
            elif path == '/api/plan':
                body = json.dumps(plan_data(), ensure_ascii=False).encode('utf-8')
                self._send(200, 'application/json; charset=utf-8', body)
            elif path == '/api/logs':
                q = parse_qs(u.query)
                try:
                    n = min(int((q.get('tail') or ['250'])[0]), 1000)
                except ValueError:
                    n = 250
                p = today_log_path()
                lines = tail_lines(p, n) if p else []
                body = json.dumps({'name': p.name if p else None,
                                   'total': len(lines), 'lines': lines},
                                  ensure_ascii=False).encode('utf-8')
                self._send(200, 'application/json; charset=utf-8', body)
            elif path == '/api/screenshot':
                try:
                    data, at, cached = capture_phone()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Shot-At', at)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as e:
                    if _shot_cache['data'] and time.time() - _shot_cache['ts'] < 120:
                        data = _shot_cache['data']
                        self.send_response(200)
                        self.send_header('Content-Type', 'image/jpeg')
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-Shot-At', _shot_cache['at'] + '(缓存)')
                        self.send_header('Content-Length', str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    else:
                        self._send(503, 'text/plain; charset=utf-8',
                                   f'截图失败: {e}'.encode('utf-8'))
            elif path.startswith('/files/'):
                name = path[len('/files/'):]
                f = RUNS / name
                if re.fullmatch(r'[A-Za-z0-9_.\-]+\.png', name) and f.is_file():
                    self._send(200, 'image/png', f.read_bytes(), cache='public, max-age=86400')
                else:
                    self._send(404, 'text/plain', b'not found')
            else:
                self._send(404, 'text/plain', b'not found')
        except Exception as e:
            try:
                self._send(500, 'text/plain; charset=utf-8', str(e).encode('utf-8'))
            except Exception:
                pass

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == '/api/settings':
                length = int(self.headers.get('Content-Length') or 0)
                payload = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
                updates = payload.get('updates') or {}
                result = apply_settings(updates)
                audit(f'设置保存(来自 {self.client_address[0]}): 提交 {updates} '
                      f'→ 生效 {result["applied"]}'
                      + (f' 拒绝 {result["rejected"]}' if result['rejected'] else ''))
                # 唤醒调度器立即重读配置（否则它可能正在 30s 轮询睡眠里等下一轮）
                if result.get('ok') and result.get('applied'):
                    try:
                        (RUNS / 'reload.signal').write_text(
                            str(time.time()), encoding='utf-8')
                    except OSError:
                        pass
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self._send(200 if result['ok'] else 400,
                           'application/json; charset=utf-8', body)
            elif u.path == '/api/adb/connect':
                length = int(self.headers.get('Content-Length') or 0)
                payload = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
                result = adb_connect(str(payload.get('addr') or ''))
                audit(f'ADB 连接(来自 {self.client_address[0]}): {payload.get("addr")} → {result.get("msg")}')
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self._send(200 if result.get('ok') else 400,
                           'application/json; charset=utf-8', body)
            elif u.path == '/api/plan':
                length = int(self.headers.get('Content-Length') or 0)
                payload = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
                result = apply_plan(payload.get('updates') or {})
                audit(f'职业计划进度更新(来自 {self.client_address[0]}): {result["applied"]}')
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self._send(200 if result['ok'] else 400,
                           'application/json; charset=utf-8', body)
            elif u.path == '/api/plan/sync':
                result = career_sync()
                audit(f'职业进度自动识别(来自 {self.client_address[0]}): {result}')
                body = json.dumps({'sync': result, 'plan': plan_data()},
                                  ensure_ascii=False).encode('utf-8')
                self._send(200, 'application/json; charset=utf-8', body)
            elif u.path == '/api/preset/alt':
                length = int(self.headers.get('Content-Length') or 0)
                payload = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
                name = str(payload.get('name') or '').strip()
                if not name:
                    body = json.dumps({'ok': False, 'error': '缺少大号名称', 'applied': {}, 'rejected': []},
                                      ensure_ascii=False).encode('utf-8')
                    self._send(400, 'application/json; charset=utf-8', body)
                else:
                    from src.presets import apply_alt_preset
                    result = apply_alt_preset(name, dry_run=bool(payload.get('dry')))
                    result['ok'] = not result['rejected']
                    audit(f'应用小号预设(来自 {self.client_address[0]}): 大号={name} '
                          f'→ 生效 {len(result["applied"])} 项')
                    body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                    self._send(200 if result['ok'] else 400,
                               'application/json; charset=utf-8', body)
            elif u.path == '/api/notify/test':
                # 设置页「发送测试通知」：按当前 config.yaml 的 notify 配置发一条
                length = int(self.headers.get('Content-Length') or 0)
                try:
                    payload = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
                except Exception:
                    payload = {}
                from src.notify import test_notify
                result = test_notify(str(payload.get('target') or 'all'))
                audit(f'测试通知(来自 {self.client_address[0]}): {result.get("msg")}')
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self._send(200 if result.get('ok') else 400,
                           'application/json; charset=utf-8', body)
            elif u.path == '/api/runner/start':
                result = scheduler_start()
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self._send(200 if result.get('ok') else 400,
                           'application/json; charset=utf-8', body)
            elif u.path == '/api/runner/stop':
                result = scheduler_stop()
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self._send(200 if result.get('ok') else 400,
                           'application/json; charset=utf-8', body)
            else:
                self._send(404, 'text/plain', b'not found')
        except Exception as e:
            try:
                self._send(500, 'text/plain; charset=utf-8', str(e).encode('utf-8'))
            except Exception:
                pass

    def log_message(self, format, *args):  # noqa: A002 - 静音访问日志
        pass


def main():
    port = DEFAULT_PORT
    if '--port' in sys.argv:
        port = int(sys.argv[sys.argv.index('--port') + 1])

    # 端口占用时不要甩一堆 traceback 就死——给出可操作的提示（常见于重复启动/上次没退干净）
    try:
        srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, errno.EACCES):
            print(f'端口 {port} 已被占用，仪表盘未启动。'
                  f'先停掉占用进程：kill $(lsof -nP -iTCP:{port} -sTCP:LISTEN -t)',
                  file=sys.stderr, flush=True)
            audit(f'启动失败：端口 {port} 被占用（{e}）')
            return 1
        raise

    # 记录生命周期：进程是"被谁杀/何时死"的唯一线索（此前日志被启动脚本覆盖，无从排查）
    audit(f'仪表盘启动: pid={os.getpid()} port={port} '
          f'ppid={os.getppid()} argv={" ".join(sys.argv[1:]) or "-"}')
    print(f'QQ宠物托管仪表盘已启动: http://0.0.0.0:{port} (Ctrl+C 停止)', flush=True)

    # 客户端的半截连接（刷新/切页/手机休眠断连）不该打堆栈刷屏——静音，交给下面统一记账
    def _quiet_conn_error(request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        srv._real_handle_error(request, client_address)

    srv._real_handle_error = srv.handle_error
    srv.handle_error = _quiet_conn_error

    def _on_signal(signum, _frame):
        raise KeyboardInterrupt

    # 关终端/被 launchd 或 kill 收走时留下死因（SIGTERM/SIGHUP 默认直接死，静默无痕）
    for _sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(_sig, _on_signal)
        except (ValueError, OSError):
            pass

    code = 0
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        audit('仪表盘退出: 收到中断信号（Ctrl+C / SIGTERM / SIGHUP）')
    except SystemExit:
        audit('仪表盘退出: SystemExit')
        raise
    except BaseException as e:
        # 兜底：任何意外都不许静默死掉，记录后以非零码退出，便于外部守护脚本分辨
        audit(f'仪表盘异常退出: {type(e).__name__}: {e}')
        raise
    else:
        audit('仪表盘退出: serve_forever 正常返回')
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
    return code


if __name__ == '__main__':
    sys.exit(main())
