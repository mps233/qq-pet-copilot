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
    'background_color': '#f6f7f9', 'theme_color': '#ea580c',
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
    """从日志里找最后一次“……: 进行中，预计 N 秒后结束（HH:MM:SS 收尾）”——
    学习（上课）/打工/冒险都走这个模板，换算剩余秒数 + 场景名（kind）。"""
    m = None
    for ln in lines[-400:]:
        mm = re.search(r'([^\[\]:：]{1,8})[:：]\s*进行中，预计 (\d+) 秒后结束'
                       r'（(\d{2}):(\d{2}):(\d{2}) 收尾）', ln)
        if mm:
            m = mm
    if not m:
        return None
    now = datetime.now()
    target = now.replace(hour=int(m.group(3)), minute=int(m.group(4)),
                         second=int(m.group(5)), microsecond=0)
    rem = int((target - now).total_seconds())
    if rem < -600:
        return None
    return {'eta_clock': f'{m.group(3)}:{m.group(4)}:{m.group(5)}',
            'remaining': max(0, rem),
            'kind': m.group(1).strip()}


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
        'work_eta': work_eta(log_lines),
        'today_duration': today_duration(log_lines, efficiency_tiers_of(sched_cfg)),
        'last_line': log_lines[-1] if log_lines else '',
        'editable': editable_snapshot(),
    }


# ---------------------------------------------------------------- HTTP

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#f6f7f9" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#111318" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/png" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="QQ宠物">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<title>QQ宠物托管</title>
<style>
:root{--bg:#FCF7ED;--card:#fff;--line:#EFE3CF;--text:#111827;--sub:#6b7280;--accent:#ea580c;--ok:#16a34a;--warn:#b45309;--gold:#D4A017;--gold-d:#B8860B;
  /* —— QQ 宠物暖色系 —— */
  --qp-bg2:#EDD18F;--qp-card-warm:#F9EFDF;--qp-edge:#EFE3CF;
  --qp-energy:#07AAFC;--qp-clean:#66C904;--qp-mood:#E6B668;--qp-track:#DEB86E;
  --qp-icon-line:#374151;
  /* QQ 宠物房间背景（素材按场景分 4 套，各有暗色版）。
     由 html[data-scene] 选择，JS 按当前任务切换（见 applyScene）。
     store 那张是纯天空渐变、不是房间，故不使用。 */
  --qp-room-main:url('/qp-icons/bg/room-main.jpg');
  --qp-room-feed:url('/qp-icons/bg/room-feed.jpg');
  --qp-room-shower:url('/qp-icons/bg/room-shower.jpg');
  --qp-room-record:url('/qp-icons/bg/room-record.jpg');
  --qp-room:var(--qp-room-main)}
/* 按当前任务切房间背景。
   必须写在 html 上：背景图 background-image 作用在 html 元素，
   而 CSS 变量只向下继承——写在 body 上的覆写对父级 html 无效（曾踩坑，
   表现为 data-scene 变了但背景纹丝不动）。JS 用 documentElement 写属性。 */
html[data-scene="feed"]{--qp-room:var(--qp-room-feed)}
html[data-scene="shower"]{--qp-room:var(--qp-room-shower)}
html[data-scene="record"]{--qp-room:var(--qp-room-record)}
*{box-sizing:border-box}
html{margin:0;padding:0;min-height:100%;background-color:var(--qp-room-fallback,var(--bg));background-image:var(--qp-room);background-position:center top;background-size:cover;background-repeat:no-repeat;background-attachment:fixed}
body{margin:0;padding:0;min-height:100vh;background:transparent;color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%;overflow-x:hidden}html{overscroll-behavior-y:contain}
/* ============================================================
   布局骨架：照 QQ 宠物主页
     顶部 sticky 胶囊资料卡（头像 + 名称/状态 + 时间环）
     左侧竖列圆钮导航（原顶部横排 tab 迁移过来，DOM 顺序不变）
     中间内容列（各 data-page 卡片）
     右侧浮动功能栏（原卡片内启停按钮迁出）
   JS 契约不变：#tabbar button[data-tab] 与 main > [data-page]。
   ============================================================ */
.app{display:flex;align-items:flex-start;gap:0;max-width:1000px;margin:0 auto;padding:0 8px}
/* 三栏：左圆钮 72 + 中间内容自适应 + 右功能栏 72（窄屏用 media 收窄） */

/* 头像（资料卡内 + 顶栏）：橘猫 SVG */
.avatar{width:40px;height:40px;border-radius:50%;background:#ffedd5;flex:none;overflow:hidden;
  display:flex;align-items:center;justify-content:center;box-shadow:0 1px 3px rgba(140,100,40,.18)}
.avatar svg{display:block;width:100%;height:100%}
/* 调度器运行状态：头像外圈描边 */
.avatar.on{box-shadow:0 0 0 2.5px var(--ok),0 1px 4px rgba(140,100,40,.2)}
.avatar.off{box-shadow:0 0 0 2.5px #ef4444,0 1px 4px rgba(140,100,40,.2)}
.meta{font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums}
.dot{width:8px;height:8px;border-radius:50%;background:#9ca3af;flex:none}
.dot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(22,163,74,.15)}
.dot.off{background:#ef4444;box-shadow:0 0 0 3px rgba(239,68,68,.12)}

/* 左侧竖列圆钮导航（QQ 宠物主页左侧那一列）——移动端贴屏幕左缘，桌面随版心 */
/* 顶部横向圆钮行（照 QQ 首页：圆钮散布在顶部，不占整列宽度） */
.tabs{display:flex;gap:7px;padding:0;margin-top:10px;flex:none;justify-content:space-between}
.tabs button{flex:1;height:52px;border:0;background:rgba(255,255,255,.78);border-radius:14px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
  padding:0;cursor:pointer;box-shadow:0 1px 4px rgba(140,100,40,.14);
  color:#5B5B5B;font-size:9.5px;line-height:1;transition:transform .12s,box-shadow .12s}
.tabs button img{width:24px;height:24px;display:block}
.tabs button span{display:block}
.tabs button.on{background:var(--accent);color:#fff;font-weight:650;
  box-shadow:0 3px 10px rgba(234,88,12,.34);transform:scale(1.04)}
/* 选中态：圆钮底色变橙，图标保持原样。彩色图标不需反白——
   brightness(0) invert(1) 会把实心圆形图标（如 coin）糊成纯白块 */
.tabs button.on img{filter:none}

main{padding:12px 0;flex:1;min-width:0;display:flex;flex-direction:column;gap:10px;min-height:calc(100vh - 60px)}

/* ============================================================
   总览页：照 QQ 宠物首页 1:1 还原
   参考 01_ui_screenshots/02_pet_home.png（1080x2412），坐标换算成百分比：
     顶部资料卡 y 4.3~10.0%，左返回钮 x 5.6~17.2%，右装扮钮 x 82.8~94.4%
     胶囊条   y 11.7~15.2%，每颗宽 18.6%，间距 1.1%
     徽章条   y 16.7~20.2%
     右功能栏 x 81.7~95.6%，三段 y 39.7~48.4 / 48.4~57.1 / 59.6~68.3%
     底部抽屉 y >= 85%（拖拽把手 + 图标 + 名称 + 倒计时）
   ============================================================ */

/* --- 顶部资料卡行：左圆钮 + 头像卡 + 右圆钮 --- */
.homebar{display:flex;align-items:center;gap:10px}
.rbtn{width:44px;height:44px;flex:none;border:0;border-radius:50%;
  background:rgba(255,255,255,.82);display:flex;align-items:center;justify-content:center;
  padding:0;cursor:pointer;box-shadow:0 1px 4px rgba(140,100,40,.16)}
.rbtn img{width:22px;height:22px;display:block}
.rbtn:active{transform:scale(.95)}
.idcard{flex:1;min-width:0;display:flex;align-items:center;gap:9px;
  background:rgba(255,255,255,.82);border-radius:999px;padding:5px 12px 5px 5px;
  box-shadow:0 1px 4px rgba(140,100,40,.14)}
.idava{width:38px;height:38px;border-radius:50%;flex:none;background:#ffedd5}
.idtxt{flex:1;min-width:0}
/* 必须显式设色：资料卡是浅底，深色主题下继承的 --text 是浅色，
   会导致"浅字压浅底"、几乎看不见（曾踩坑） */
.idname{font-weight:680;font-size:14.5px;line-height:1.2;color:#1F1F1F;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.idsub{font-size:11px;color:#6B6B6B;line-height:1.25;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* 状态环：照 QQ 宠物资料卡右侧那个圆环（用 conic-gradient 表示运行状态） */
.idring{width:26px;height:26px;border-radius:50%;flex:none;
  background:conic-gradient(var(--ok) 0 75%, rgba(0,0,0,.10) 75% 100%);
  -webkit-mask:radial-gradient(circle, transparent 8px, #000 8px);
  mask:radial-gradient(circle, transparent 8px, #000 8px)}
.idring.off{background:conic-gradient(#ef4444 0 100%)}

/* --- 胶囊条：图标 + 数值 + 单位（照 QQ 宠物 等级/踩踩/金币 那三颗） --- */
.caps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin-top:10px}
.cap{display:flex;align-items:center;gap:5px;min-width:0;
  background:rgba(255,255,255,.78);border-radius:999px;
  padding:6px 10px;box-shadow:0 1px 3px rgba(140,100,40,.10);position:relative}
.cap .cico{width:20px;height:20px;flex:none;border-radius:50%}
/* 数值优先：不省略、不换行（窄屏下 6 颗胶囊三列排，单位文字会把数值挤成 "9…"） */
.cap .cval{font-weight:680;font-size:14.5px;font-variant-numeric:tabular-nums;
  color:#1F1F1F;white-space:nowrap;flex:1;min-width:0;overflow:visible}
.cap .cunit{font-size:10px;color:#6B6B6B;white-space:nowrap;flex:none}
/* 胶囊内的细进度条（贴胶囊底缘，照 QQ 宠物等级胶囊的橙色条） */
.cap .bar{position:absolute;left:12px;right:12px;bottom:3px;height:3px;margin:0;
  background:rgba(0,0,0,.09);border-radius:2px}
.cap .bar>i{background:var(--accent)}

/* --- 中部场景：手机画面当"宠物"，卡片化 --- */
/* 场景卡：固定为"一屏中部"的高度（照 QQ 首页场景占比 ~11%~85%）。
   不能让手机画面按原始 9:16 撑满——那会把底部抽屉推出首屏。 */
/* 场景区：照参考图占约 65% 屏高（y 20%~85%） */
/* 场景区：铺 QQ 宠物首页的房间背景（条纹墙 + 沙发 + 爪印地毯 + 木地板）。
   背景图比例 508:1368（≈0.371），屏幕通常更宽；用 cover 铺满并裁切，
   顶部对齐让条纹墙/踢脚线落在视觉合理位置。 */
.scenecard.card{position:relative;flex:1;display:flex;flex-direction:column;padding:0;
  background:transparent;border:0;box-shadow:none;min-height:0}
.scenecard #shotLink{flex:1;min-height:0;width:100%;display:block;
  line-height:0;overflow:hidden;background:transparent}
/* 画面铺满场景区（cover）。
   为什么不用 contain：手机画面本身就是 QQ 宠物首页（含宠物/沙发/地毯/功能栏），
   而页面背景又是同一间屋子——contain 会在两侧露出背景房间，
   导致沙发和地毯"出现两次"且错位。cover 让画面里的房间直接充当场景。 */
.scenecard #phoneShot{display:block;width:100%;height:100%;
  object-fit:cover;object-position:center center;background:transparent}
.scenecard .shoterr{padding:0 12px;font-size:11px;flex:none}
/* ===== 中部舞台（照 QQ 首页中央宠物位）===== */
.stage{flex:1;min-height:min(40vh,340px);display:flex;flex-direction:column;
  align-items:center;justify-content:flex-end;gap:8px;padding:12px 0 4px;position:relative}
.stagepet{max-height:100%;max-width:46%;object-fit:contain;display:block;
  filter:drop-shadow(0 10px 18px rgba(120,85,30,.28));
  animation:stagebob 3.2s ease-in-out infinite}
/* 轻微上下浮动，让待机中的宠物有"活着"的感觉（QQ 首页宠物也会呼吸/摇摆） */
@keyframes stagebob{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
@media(prefers-reduced-motion:reduce){.stagepet{animation:none}}
.stagecap{font-size:12px;color:#6B4E23;background:rgba(255,255,255,.66);
  border-radius:999px;padding:3px 12px;backdrop-filter:blur(4px);white-space:nowrap}
/* ===== 实时画面页（独立成页）：画面尽量大，纵向铺满 ===== */
/* ===== 画面页：整张截图完整可见 =====
   用"按宽度铺满、按原始比例给高度"的方式（不写死高度）：
   手机截图是 9:16 竖图，宽度铺满后高度自然确定，不会在下方留大片空白。
   object-fit:contain 只作兜底，正常比例下不会触发。 */
.shotpage{display:flex;flex-direction:column;gap:10px}
.shotpage .scenecard{display:block;border-radius:16px;overflow:hidden;
  box-shadow:0 2px 10px rgba(140,100,40,.18);background:#0e0f12}
.shotpage #shotLink{display:block;width:100%;overflow:hidden;background:transparent}
.shotpage #phoneShot{display:block;width:100%;height:auto;object-fit:contain;
  background:transparent;min-height:120px}
.shotpage-ctl{display:flex;gap:8px;flex:none;justify-content:center}
.shotpage-ctl .savebtn{text-decoration:none;display:inline-block;text-align:center}
.shotmeta{position:absolute;top:8px;right:10px;z-index:2;font-size:10.5px;color:#fff;
  background:rgba(0,0,0,.34);border-radius:999px;padding:2px 9px;backdrop-filter:blur(4px)}

/* --- 底部抽屉：拖拽把手 + 图标 + 名称 + 倒计时 --- */
.drawer{background:rgba(255,255,255,.86);border-radius:20px 20px 0 0;
  padding:8px 14px 12px;box-shadow:0 -2px 10px rgba(140,100,40,.12);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
.dhandle{width:52px;height:5px;border-radius:3px;background:rgba(0,0,0,.14);
  margin:0 auto 10px}
.drow{display:flex;align-items:flex-start;gap:10px}
.dico{width:38px;height:38px;flex:none;border-radius:50%;background:rgba(255,255,255,.9);
  padding:5px;box-shadow:0 1px 4px rgba(140,100,40,.16);margin-top:1px}
.dcol{flex:1;min-width:0}
/* 标题行允许换行（窄屏下"雇佣打工中 + 预计 19:27 结束"会挤成竖排，
   给 dhint 允许折行 + 限制不换行的只有主状态） */
.dtitle{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;font-weight:680;font-size:15.5px;min-width:0}
.dhint{font-size:11px;font-weight:400;color:var(--sub);white-space:nowrap}
.dsub{font-size:11px;color:var(--sub);font-variant-numeric:tabular-nums;margin-top:2px;
  word-break:break-word}
.dmeta{font-size:10px;color:var(--sub);flex:none;text-align:right;max-width:32%;
  word-break:break-word}
@media(max-width:480px){.dmeta{display:none}}

/* --- 任务队列卡（总览页保留，样式与抽屉统一） --- */
.qcard{border-radius:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:12px 14px}
/* 右侧浮动功能栏（QQ 宠物右侧 喂食/洗澡/好友 位） */
/* 右侧功能栏：照 QQ 首页那一体磨砂长胶囊（三段共用半透明容器） */
.funcbar{display:flex;flex-direction:column;gap:0;padding:8px 5px;flex:none;
  position:sticky;top:8px;z-index:15;
  background:rgba(255,255,255,.42);border-radius:999px;
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  box-shadow:0 1px 6px rgba(140,100,40,.12)}
.fab{width:46px;height:60px;border:0;background:transparent;border-radius:999px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
  cursor:pointer;color:#1F1F1F;font-size:10px;padding:0;
  transition:transform .12s}
.fab .fi{font-size:17px;line-height:1}
.fab .ft{font-size:10px;line-height:1;color:#5B5B5B}
.fab:active{transform:scale(.95)}
.fab:disabled{opacity:.45;cursor:default}
.fab.stop .fi{color:#dc2626}
.fab.ghost .fi{color:var(--accent)}
.card h2{font-size:12px;color:var(--sub);font-weight:600;margin:0 0 8px;letter-spacing:.03em}
.workline{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.workline .big{font-size:22px;font-weight:680;font-variant-numeric:tabular-nums}
.workline .hint{font-size:12px;color:var(--sub)}
.subline{margin-top:6px;font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 8px;text-align:center}
.tile .v{font-size:19px;font-weight:680;font-variant-numeric:tabular-nums}
.tile .k{font-size:11px;color:var(--sub);margin-top:2px}
.bar{height:4px;background:#eef0f4;border-radius:2px;overflow:hidden;margin-top:7px}
.bar>i{display:block;height:100%;background:var(--accent);width:0;border-radius:2px}
/* 学习/打工合并瓦片：普通 1 格（内容很少，双宽会破坏栅格节奏）；
   进度条分两段叠加——学习段（橙）在左、打工段（蓝）紧随其后，共同表示已用/总预算。
   用默认的 row 方向（不要 row-reverse：那会让起点翻到右侧，进度从右往左长）。
   类名用 bar-split（不能用 sw——.sw 是设置页开关的类名，会把进度条
   渲染成 46x26 圆角灰底带白点的开关形状）。 */
.bar.bar-split{display:flex}
.bar.bar-split>i{flex:none}
.bar.bar-split>i#swBarSchool{background:var(--accent)}
.bar.bar-split>i#swBarWork{background:#0ea5e9}
.qhead{display:flex;justify-content:space-between;font-size:12px;color:var(--sub);margin-bottom:6px;font-variant-numeric:tabular-nums}
.qgrp{font-size:10.5px;color:var(--sub);letter-spacing:.06em;margin:10px 0 2px}
.mrow{display:flex;gap:10px;padding:9px 2px;border-top:1px solid var(--line);align-items:center}
.mrow:first-child{border-top:0}
.mcb{width:20px;height:20px;border-radius:6px;background:var(--line);flex:none;cursor:pointer;position:relative;user-select:none}
.mcb.on{background:var(--accent)}
.mcb.on::after{content:"✓";position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;font-size:13px;font-weight:700}
/* 任务队列图标（放在 .mcb 与 .mname 之间，左右各收 3px 抵消 .mrow 的 10px gap） */
.qico{width:18px;height:18px;flex:none;border-radius:5px;margin:0 -3px}
.mname{font-weight:650;font-size:14px}
.mname.off{color:var(--sub);font-weight:500}
.mdet{margin-left:auto;font-size:12.5px;color:var(--sub);font-variant-numeric:tabular-nums;text-align:right}
.mdet .run{color:var(--accent);font-weight:650}
.mdet .off-t{color:#9ca3af}
.ttag{font-size:9.5px;padding:1px 5px;border-radius:4px;border:1px solid var(--line);color:var(--sub);flex:none;margin-left:2px}
.mrow.done{opacity:.6}
.mrow.done .mname{text-decoration:line-through;color:var(--sub);font-weight:500}
.mrow.run .mname{color:var(--accent)}
.tasklist .qgrp+.row{border-top:0}
.chip.run{background:var(--accent);color:#fff;font-weight:600}
.tasklist .row.run .t>span:first-child{font-weight:650;color:var(--accent)}
.tasklist .row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-top:1px dashed var(--line);font-size:14px}
.tasklist .row:first-child{border-top:0}
.tasklist .t{display:flex;gap:8px;align-items:center}
.tasklist .nx{font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums}
.chip{font-size:11px;padding:2px 8px;border-radius:999px;background:#f1f2f6;color:#6b7280;flex:none}
.chip.ready{background:#eaf7ee;color:#15803d}
.chip.wait{background:#fdeede;color:#ea580c}
.chip.off{background:#f4f4f5;color:#a1a1aa}
.chip.done{background:#f0f1f4;color:#52525b}
.logctl{display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap}
.logctl input{flex:1;min-width:110px;border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px;background:#fafafa;color:var(--text)}
.logctl button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 12px;font-size:12px;color:var(--sub)}
.logctl button.on{background:var(--accent);border-color:var(--accent);color:#fff}
pre#logbox{height:46vh;min-height:250px;overflow:auto;background:#0f1116;color:#d7dbe0;font:11.5px/1.65 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:10px 12px;border-radius:8px;white-space:pre-wrap;word-break:break-all;margin:0;overscroll-behavior:contain}
.thumbs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.thumbs a{display:block;border:1px solid var(--line);border-radius:8px;overflow:hidden;position:relative}
.thumbs img{width:100%;display:block}
.thumbs .cap{position:absolute;left:0;right:0;bottom:0;background:rgba(15,17,22,.72);color:#fff;font-size:10px;padding:2px 6px;text-align:center}
footer{color:#9ca3af;font-size:11px;text-align:center;padding:14px 16px 28px;line-height:1.7}
.duo{display:flex;gap:10px;align-items:stretch}
.duoshot-wrap{flex:0 0 46%;min-width:0}
.duoque{flex:1;min-width:0}
/* 手机画面卡：图片左右贴满卡片、下方不留占位。
   **窄卡片时「刷新」移到画面下方**（否则会把标题挤成"手机画面…"、"拍摄 01:xx" 丢失）。
   用 grid-template-areas 摆位（DOM 顺序固定为 标题/图片/按钮，不改结构）：
     宽卡片 → 标题行: [手机画面 拍摄 01:xx] [刷新]，图片占满下一行
     窄卡片 → 标题 / 图片 / [刷新]（按钮左对齐、在画面下方）

   判断依据用**容器查询**而非视口断点：卡片宽度 = 视口 × 46%（宽屏 44%/240/300px），
   按视口断点会在 400~460px 视口（卡片仅 ~184px）漏判。
   注意：元素不能被自身的容器查询命中——所以 container-type 放在外层包装
   .duoshot-wrap 上，@container 规则作用于内层 .duoshot.card（曾写在同一个元素上，
   规则永不匹配、按钮一直挤在标题行，实测踩坑）。
   临界值：实测量得完整标题 ~107px + 按钮 45px + 内边距/间距 34px ≈ 186px，
   取 230px 作断点留足余量（含 meta 未加载 / 字体差异）。 */
.duoshot-wrap{container-type:inline-size;min-width:0;display:flex}
.duoshot.card{
  flex:1;min-width:0;
  padding:0;overflow:hidden;position:relative;
  display:grid;grid-template-columns:1fr auto;
  grid-template-areas:"title ctl" "shot shot" "err err";
}
.duoshot>h2{grid-area:title;padding:12px 0 8px 14px;margin:0;min-width:0;display:flex;align-items:center}
.duoshot>h2 .shotttl{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#shotLink{grid-area:shot;display:block;position:relative;line-height:0}
/* 画面贴满：无圆角、无边框（截图比例与设备一致、无黑边） */
#phoneShot{display:block;width:100%;height:auto;border-radius:0;background:#eef0f4;min-height:48px;color:transparent}
/* 场景卡内：加载/失败提示做成浮层小胶囊，不铺底色（否则会盖住房间背景） */
#shotLink.loading::before{content:"画面加载中…";position:absolute;left:50%;top:50%;
  transform:translate(-50%,-50%);font-size:11.5px;color:#6B6B6B;
  background:rgba(255,255,255,.78);border-radius:999px;padding:4px 12px;white-space:nowrap}
#shotLink.loading.failed::before{content:"获取失败，稍后自动重试…";color:#b45309}
#shotLink.loading #phoneShot{visibility:hidden}
/* 刷新按钮（常规控件样式，跟随主题；不像浮层那样遮挡画面） */
.duoshot .shotctl{grid-area:ctl;align-self:center;margin:0;padding:12px 14px 8px 6px;display:flex;flex-wrap:nowrap;gap:6px;justify-content:flex-end}
.duoshot .shotctl button{padding:4px 10px;font-size:11.5px;white-space:nowrap;flex:0 0 auto}
/* 错误提示独立占一行（原先放在 .shotctl 里会与标题/按钮抢同一行宽度，
   把"手机画面 拍摄 01:xx"挤到截断）；空时不占高度（:empty） */
.duoshot .shoterr{grid-area:err;padding:0 14px;font-size:11px}
.duoshot .shoterr:empty{display:none}
.duoshot .shoterr:not(:empty){padding-bottom:10px}
/* 卡片 <240px 放不下"完整标题 + 刷新" → 按钮移到画面下方（居中）。
   240 而非 230：实测 230px 临界点上标题会被压到 23px（"手机画面"被截），
   留 10px 余量规避 CSS 圆整/字体差异导致的临界抖动。 */
@container (max-width:239px){
  .duoshot.card{grid-template-columns:1fr;grid-template-areas:"title" "shot" "ctl" "err"}
  .duoshot>h2{padding:12px 14px 8px}
  .duoshot .shotctl{padding:8px 0 10px;justify-content:center}
}
.duoque .qhead{flex-wrap:wrap;gap:2px 8px;font-size:11.5px}
.duoque .tasklist .row{font-size:13px;padding:7px 0;flex-wrap:wrap;gap:2px 6px}
.duoque .tasklist .t{gap:5px}
.duoque .chip{font-size:10.5px;padding:2px 7px}
.shotctl{display:flex;justify-content:center;gap:10px;align-items:center;margin-top:8px}
.shotctl button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 12px;font-size:12px;color:var(--sub)}
.err{color:#b45309;font-size:12px}
.cfg .row{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px dashed var(--line);font-size:13.5px}
.cfg .row:first-child{border-top:0}
.cfg .k{color:var(--sub);flex:none}
.cfg .v{text-align:right}
.form .frow{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 0;border-top:1px dashed var(--line)}
.form .frow:first-child{border-top:0}
.form .k{color:var(--sub);font-size:13.5px;flex:none}
.form select,.form input[type=number],.form input[type=text]{border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:13px;background:#fafafa;color:var(--text);max-width:58%}
.form input[type=number]{width:86px;text-align:right}
.form input[type=text]{width:150px;text-align:right}
.form .fsec{margin-top:16px}
.form .fsec:first-child{margin-top:0}
.form .fsect{font-size:11px;font-weight:600;color:var(--sub);letter-spacing:.06em;padding:0 0 6px;display:flex;align-items:center;gap:6px;user-select:none}
.form .fsect::before{content:"";width:3px;height:9px;border-radius:2px;background:var(--accent);opacity:.5;flex:none}
.form .two{display:flex;gap:6px}
.form .two input{width:64px}
.sw{position:relative;width:46px;height:26px;border-radius:13px;background:#d9dce3;border:none;transition:.2s;flex:none;cursor:pointer}
.sw.on{background:var(--accent)}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.25)}
.sw.on::after{left:23px}
.savebtn{width:100%;margin-top:12px;border:none;border-radius:10px;background:var(--accent);color:#fff;font-size:15px;font-weight:600;padding:11px;letter-spacing:.02em}
.savebtn:disabled{opacity:.55}
.saveMsg{font-size:12px;text-align:center;margin-top:6px;min-height:16px;color:var(--ok)}
.saveMsg.err{color:#b45309}
.subh{font-size:11px;color:var(--sub);margin:14px 0 4px;letter-spacing:.03em}
.advchart{width:100%;height:auto;display:block;margin:6px 0 0;border:1px solid var(--line);border-radius:8px;background:#fcfcfd;cursor:crosshair;touch-action:pan-y}
.advcap{font-size:11px;color:var(--sub);margin:8px 0 0;letter-spacing:.03em}
.advtip{font-size:12.5px;color:var(--text);background:#f8f9fb;border:1px solid var(--line);border-radius:8px;padding:7px 10px;margin-top:8px;font-variant-numeric:tabular-nums;min-height:18px}
.advchips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.advlist{max-height:44vh;overflow:auto;margin-top:6px;border:1px solid var(--line);border-radius:8px;background:#fff;overscroll-behavior:contain}
.advlist .arow{display:flex;justify-content:space-between;align-items:baseline;gap:8px;padding:7px 10px;border-top:1px dashed var(--line);font-size:13px;font-variant-numeric:tabular-nums}
.advlist .arow:first-child{border-top:0}
.advlist .ai{color:var(--sub);font-size:12px;flex:none;width:112px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.advlist .ag{color:var(--sub);font-size:11.5px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right}
.advlist .av{font-weight:650;flex:none;min-width:44px;text-align:right}
.advlist .pos{color:var(--gold-d)} .advlist .neg{color:#dc2626} .advlist .zero{color:#9ca3af}
#coins{color:var(--gold)}
.minibtn{border:1px solid var(--line);background:#fff;border-radius:6px;padding:2px 8px;font-size:11px;color:var(--sub)}
.minibtn.on{background:var(--accent);border-color:var(--accent);color:#fff}
.planbars .pb{margin-top:10px}
.planbars .pb .t{display:flex;justify-content:space-between;font-size:12px;color:var(--sub);margin-bottom:3px;font-variant-numeric:tabular-nums}
.planbars .bar{margin-top:0}
.plansteps .st{display:flex;justify-content:space-between;gap:8px;padding:7px 0;border-top:1px dashed var(--line);font-size:13px;align-items:baseline}
.plansteps .st:first-child{border-top:0}
.plansteps .dot2{flex:none;font-size:12px}
.plansteps .tx{flex:1;min-width:0}
.plansteps .pr{color:var(--sub);font-size:11.5px;flex:none;font-variant-numeric:tabular-nums}
.plansteps .st.done .tx{color:var(--sub)}
.plansteps .st.done .pr{color:#9ca3af}
.plansteps .st.cur{background:#fdf0e4;border-radius:8px;padding-left:6px;padding-right:6px}
.plannote{font-size:11px;color:var(--sub);margin-top:6px;line-height:1.5}
.watchbox .wrow{display:flex;justify-content:space-between;gap:8px;font-size:12.5px;padding:6px 0;border-top:1px dashed var(--line);align-items:baseline}
.watchbox .wrow:first-child{border-top:0}
.watchbox .wstate{font-size:12px;color:var(--sub);padding:2px 0 4px}
.watchbox .wbadge{color:var(--ok);font-weight:600}
.planlines{display:grid;grid-template-columns:1fr 1fr;gap:6px 8px}
.planlines .ln{display:flex;justify-content:space-between;align-items:center;font-size:12.5px;padding:5px 8px;border:1px solid var(--line);border-radius:8px}
.planlines .chipx{font-size:10.5px;padding:1px 6px;border-radius:999px;background:#f4f4f5;color:#a1a1aa;margin-left:4px}
.planlines .chipx.ok{background:#eaf7ee;color:#15803d}
.plinedit{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.plinedit label{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--sub)}
.plinedit input{border:1px solid var(--line);border-radius:8px;padding:6px;font-size:14px;text-align:center;background:#fafafa;color:var(--text);width:100%}
.planeditrow{grid-column:span 3;display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12.5px;margin-top:2px}
.btnrow2{display:flex;gap:8px;margin-top:10px}
.btnrow2 .savebtn{margin-top:0}
.savebtn.ghost{background:#fff;color:var(--accent);border:1.5px solid var(--accent)}
.hide{display:none!important}
/* 统一细滚动条（日志框/冒险列表/整页）：细圆角、透明轨道，悬停加深 */
::-webkit-scrollbar{width:7px;height:7px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#cfd3db;border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:#b4bac6}
::-webkit-scrollbar-corner{background:transparent}
html{scrollbar-width:thin;scrollbar-color:#cfd3db transparent}
/* 窄屏（≤639px）：三栏 -> 两栏。
   手机宽度放不下「左圆钮 + 内容 + 右功能栏」三栏（390px 视口实测右栏被挤出屏幕、
   内容被裁），所以右功能栏改为贴底横排工具条（QQ 宠物手机端也是右侧竖栏，
   但这里内容更宽，竖栏会挤掉正文，故横排贴底）。 */
@media(max-width:639px){
  .app{max-width:none}
  .tabs{gap:5px}
  .tabs button{height:46px;font-size:9px}
  .tabs button img{width:19px;height:19px}
  main{padding:10px 10px 84px 0}
  /* 窄屏保留右侧竖栏（QQ 宠物手机端也是右侧竖栏）：
     贴底横排会盖住内容且不符合参考图。内容区已收窄，竖栏放得下。 */
  .funcbar{padding:8px 4px;gap:8px}
  .fab{width:46px;height:46px}
  .fab .fi{font-size:15px}
  main{padding:10px 8px 24px 0}
  /* 窄屏：手机画面与任务队列改上下堆叠。
     原来并排时 .duoshot-wrap 固定 44%，剩下的宽度放不下任务队列
     （实测 390px 视口队列右侧被裁掉），故窄屏改单列。 */
  .duo{flex-direction:column}
  .duoshot-wrap{flex:none;width:100%}
  /* .grid 保持 base 的 3 列：改成 2 列会让瓦片过宽、撑出横向溢出，
     整页右移导致内容被裁（实测 390px 视口踩坑） */
  .mrow{gap:8px;padding:8px 0}
  .mcb{width:18px;height:18px;border-radius:5px}
  .mcb.on::after{font-size:11px}
  .mname{font-size:13px;white-space:nowrap}
  .ttag{display:none}
  .mdet{font-size:10.5px;white-space:nowrap}
}
/* 平板/中窗（640–919px）：比手机版用更宽的版心和更多列 */
@media(min-width:640px){
  main{max-width:none}
  .grid{grid-template-columns:repeat(6,minmax(0,1fr))}
  .thumbs{grid-template-columns:repeat(4,minmax(0,1fr))}
  .duoshot-wrap{flex:0 0 240px}
  #setForm{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 28px;align-items:start}
  #setForm .fsec:nth-child(1),#setForm .fsec:nth-child(2){margin-top:0}
}
/* 小屏手机（≤360px）：收紧字号与内边距，主页截图/队列改上下堆叠 */
@media(max-width:360px){
  .avatar{width:34px;height:34px}
  .tabs{gap:4px}
  .tabs button{height:42px;font-size:8.5px}
  .tabs button img{width:18px;height:18px}
  .funcbar{gap:7px;padding:8px 5px}
  .fab{width:46px;height:46px}
  .fab .fi{font-size:15px}
  .avatar{width:34px;height:34px}
  main{padding:8px}
  .card{padding:10px 11px;border-radius:10px}
  .workline .big{font-size:20px}
  .tile .v{font-size:17px}
  .grid{gap:6px}
  .duo{flex-direction:column}
  .duoshot-wrap{flex:none}
  .advlist .ai{width:96px}
  .form select,.form input[type=number],.form input[type=text]{max-width:52%}
  .form input[type=text]{width:126px}
}
/* 桌面/网页版（≥920px）：手机布局原样保留，宽屏下加宽 + 多列 */
@media(min-width:920px){
  .tabs{gap:10px}
  .tabs button{height:58px;font-size:10.5px}
  .tabs button img{width:24px;height:24px}
  .funcbar{gap:12px;padding:12px 10px}
  .fab{width:64px;height:64px}
  .fab .fi{font-size:19px}
  main{padding:16px 12px 30px}
  .grid{grid-template-columns:repeat(6,minmax(0,1fr))}
  .duoshot-wrap{flex:0 0 300px}
  .thumbs{grid-template-columns:repeat(4,minmax(0,1fr))}
  #setForm{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 34px;align-items:start}
  #setForm .fsec:nth-child(1),#setForm .fsec:nth-child(2){margin-top:0}
  pre#logbox{height:58vh}
}
/* 跟随系统深色模式：变量整体换肤 + 硬编码底色的控件逐个覆盖 */
@media(prefers-color-scheme:dark){
  :root{--bg:#111318;--card:#1b1e26;--line:#2a2e3a;--text:#e7eaf0;--sub:#98a0ae;--accent:#ff9440;--ok:#22c55e;--gold:#D4A017;--gold-d:#E6C35C}
  /* QQ 宠物暖色 · 深色适配 */
  :root{--bg:#171512;--card:#201D18;--line:#332C22;--text:#f0e9dd;--sub:#a89e8d;
    --qp-bg2:#241F18;--qp-card-warm:#262119;--qp-edge:#332C22;--qp-track:#544535;
    --qp-icon-line:#E7EAF0;
    --qp-room-main:url('/qp-icons/bg/room-main-dark.jpg');
    --qp-room-feed:url('/qp-icons/bg/room-feed-dark.jpg');
    --qp-room-shower:url('/qp-icons/bg/room-shower-dark.jpg');
    --qp-room-record:url('/qp-icons/bg/room-record-dark.jpg')}
  header{background:rgba(17,19,24,.88)}
  .logctl button,.shotctl button,.minibtn,.savebtn.ghost,.advlist{background:#1b1e26;color:var(--text)}
  /* QQ 宠物式布局元素（圆钮/胶囊卡/功能栏）在深色下的底色 */
  .funcbar{background:rgba(23,21,18,.92)}
  /* home 系列元素：浅底磨砂在深色下改成深色磨砂，否则"浅字压浅底"读不出来 */
  .tabs button{background:rgba(255,255,255,.08);color:var(--text)}
  .funcbar{background:rgba(255,255,255,.07)}
  .idcard,.cap,.drawer{background:rgba(32,29,24,.72)}
  .idname,.cap .cval{color:#F0E9DD}
  .idsub,.cap .cunit{color:#A89E8D}
  .tabs button,.fab{color:#C9C0B0}
  .fab .ft{color:#A89E8D}
  .fab{color:#E7EAF0}
  .rbtn{background:rgba(255,255,255,.10)}
  .dico{background:rgba(255,255,255,.10)}
  .dhandle{background:rgba(255,255,255,.20)}
  .idring{background:conic-gradient(var(--ok) 0 75%, rgba(255,255,255,.14) 75% 100%)}
  .tabs button.on{background:var(--accent);color:#fff}
  .avatar{background:#3a2f22}
  .logctl input,.form select,.form input[type=number],.form input[type=text],.plinedit input{background:#15171e;color:var(--text);border-color:var(--line)}
  #advDate{background:#1b1e26;color:var(--text);border:1px solid var(--line);border-radius:6px}
  .chip{background:#262a35;color:#a8b0bf}
  .chip.ready{background:#12291a;color:#4ade80}
  .chip.wait{background:#3a2812;color:#ffb877}
  .chip.run{background:#ff9440;color:#1b1e26}
  .chip.off{background:#20232c;color:#6b7280}
  .chip.done{background:#24262e;color:#a1a1aa}
  .planlines .chipx{background:#20232c;color:#6b7280}
  .planlines .chipx.ok{background:#12291a;color:#4ade80}
  .advtip,.advchart{background:#15171e}
  #phoneShot{background:transparent}
  #shotLink.loading::before{background:rgba(32,29,24,.8);color:#A89E8D}
  .bar{background:#262a35}
  .sw{background:#3a3f4d}
  .plansteps .st.cur{background:#382713}
  .savebtn.ghost{color:#ffb877;border-color:#ff9440}
  .err{color:#f59e0b}
  #shotLink.loading.failed::before{color:#f59e0b}
  ::-webkit-scrollbar-thumb{background:#333947}
  ::-webkit-scrollbar-thumb:hover{background:#454c5e}
  html{scrollbar-color:#333947 transparent}
}
</style>
</head>
<body>
<div class="app">
<!-- 左侧竖列圆钮导航（QQ 宠物主页的左侧圆钮位）：图标取自 qqpet_assets，title 做无障碍 -->

<main>

  <section class="card" id="advCard" data-page="adv">
    <h2>冒险记录 <span id="advMeta" style="font-weight:400;font-size:10.5px"></span></h2>
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
    <h2>职业解锁计划 <span id="planMeta" style="font-weight:400;font-size:10.5px"></span></h2>
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
    <!-- 顶部资料卡（照 QQ 首页：头像 + 双行文字 + 状态环 + 时钟） -->
    <div class="homebar">
      <div class="idcard">
        <span class="avatar" id="schedDot"><svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#ffedd5"/><path d="M7.5 12.5 L6 3.5 L14.5 8 Z" fill="#f59e0b"/><path d="M24.5 12.5 L26 3.5 L17.5 8 Z" fill="#f59e0b"/><path d="M8.3 10.6 L7.5 6 L12 8.3 Z" fill="#fbcfe8"/><path d="M23.7 10.6 L24.5 6 L20 8.3 Z" fill="#fbcfe8"/><circle cx="16" cy="18" r="10" fill="#f59e0b"/><ellipse cx="16" cy="21.6" rx="6.6" ry="5" fill="#fff7ed"/><circle cx="11.8" cy="16.4" r="1.7" fill="#1f2937"/><circle cx="20.2" cy="16.4" r="1.7" fill="#1f2937"/><path d="M14.7 19.4 h3.2 l-1.6 2 Z" fill="#f97316"/><path d="M16 21.4 v1.1 M16 22.5 q-1.2 1.3 -2.4 .3 M16 22.5 q1.2 1.3 2.4 .3" stroke="#92400e" stroke-width=".9" fill="none" stroke-linecap="round"/><path d="M6.5 18.5 h3 M6.8 21.5 h2.6 M25.5 18.5 h-3 M25.2 21.5 h-2.6" stroke="#d97706" stroke-width=".9" stroke-linecap="round"/></svg></span>
        <div class="idtxt">
          <div class="idname">QQ宠物托管</div>
          <div class="idsub" id="schedTxt">--</div>
        </div>
        <span class="idring" id="idRing"></span>
        <span class="meta" id="clock">--:--:--</span>
      </div>
    </div>
    <nav class="tabs" id="tabbar">
  <button data-tab="main" class="on" title="总览"><img src="/qp-icons/coin-24.png" alt=""><span>总览</span></button>
  <button data-tab="shot" title="实时画面"><img src="/qp-icons/emoji-24.png" alt=""><span>画面</span></button>
  <button data-tab="adv" title="冒险"><img src="/qp-icons/logo_adventure-24.png" alt=""><span>冒险</span></button>
  <button data-tab="plan" title="职业"><img src="/qp-icons/logo_work-24.png" alt=""><span>职业</span></button>
  <button data-tab="notify" title="通知"><img src="/qp-icons/bubble_button-24.png" alt=""><span>通知</span></button>
  <button data-tab="set" title="设置"><img src="/qp-icons/line/skills-24.png" alt=""><span>设置</span></button>
  <button data-tab="log" title="日志"><img src="/qp-icons/bubble_text-24.png" alt=""><span>日志</span></button>
</nav>
    <div class="caps">
    <div class="cap" title="金币" id="capCoins">
      <img class="cico" src="/qp-icons/coin-24.png" alt="">
      <span class="cval" id="coins">--</span>
      <span class="cunit" id="coinsAt"></span>
    </div>
    <div class="cap" title="今日踩踩" id="capVisit">
      <img class="cico" src="/qp-icons/logo_hangout-24.png" alt="">
      <span class="cval" id="visitTxt">--</span>
      <div class="bar"><i id="visitBar"></i></div>
    </div>
    <div class="cap" title="今日PK" id="capPk">
      <img class="cico" src="/qp-icons/logo_pk-24.png" alt="">
      <span class="cval" id="pkTxt">--</span>
      <div class="bar"><i id="pkBar"></i></div>
    </div>
    <div class="cap" title="今日冒险" id="capAdv">
      <img class="cico" src="/qp-icons/logo_adventure-24.png" alt="">
      <span class="cval" id="advTxt">--</span>
    </div>
    <div class="cap" title="今日学习 / 打工" id="capSw">
      <img class="cico" src="/qp-icons/logo_study-24.png" alt="">
      <span class="cval" id="swTxt">--</span>
      <!-- swLbl 原为可见标签；胶囊放不下长文案，改为隐藏元素供 JS 写入，
           其内容由 JS 同步到 capSw 的 title（悬浮可看） -->
      <span class="cunit" id="swLbl" hidden></span>
      <div class="bar bar-split"><i id="swBarSchool"></i><i id="swBarWork"></i></div>
    </div>
    <div class="cap" title="经验日常" id="capExp">
      <img class="cico" src="/qp-icons/emoji-24.png" alt="">
      <span class="cval" id="expTxt">--</span>
    </div>
    </div><!-- /.caps -->

    <!-- 中部舞台：照 QQ 首页中央的宠物位。
         宠物贴图由宠物视频合成（左半立绘 + 右半 alpha 遮罩），
         见 tools/make_pet_cutouts.py。 -->
    <div class="stage">
      <img class="stagepet" id="stagePet" src="/qp-icons/pets/pet00.png" alt="">
      <div class="stagecap" id="stageCap">待机中</div>
    </div>

    <!-- 底部抽屉（照 QQ 宠物首页底部那条）：当前任务 + 倒计时 -->
    <section class="drawer" id="runnerCard" data-page="main">
      <div class="dhandle"></div>
      <div class="drow">
        <img class="dico" id="runnerIcon" src="/qp-icons/logo_work-24.png" alt="">
        <div class="dcol">
          <div class="dtitle"><span class="dot" id="runnerDot"></span><span id="runnerState">--</span><span class="dhint" id="runnerHint"></span></div>
          <div class="dsub" id="workSub"></div>
          <div class="dsub" id="runnerSub"></div>
        </div>
        <span class="dmeta" id="runnerMeta"></span>
      </div>
      <div class="saveMsg" id="runnerMsg"></div>
    </section>
  </section><!-- /.home -->

  <!-- 实时画面（独立页）：手机画面单独一屏，便于放大看 / 点开原图 -->
  <section class="shotpage" data-page="shot">
    <div class="scenecard duoshot" id="shotCardWrap">
      <span class="shotmeta" id="shotMeta"></span>
      <a id="shotLink" class="loading" href="/api/screenshot" target="_blank" rel="noopener"><img id="phoneShot" alt="加载中…"></a>
      <div id="shotErr" class="err shoterr"></div>
    </div>
    <div class="shotpage-ctl">
      <a class="savebtn" id="shotOpen" href="/api/screenshot" target="_blank" rel="noopener">在新标签打开原图</a>
    </div>
  </section>


  <section class="card qcard" data-page="main">
    <h2>任务队列</h2>
    <div class="qhead"><span id="qTop">--</span><span id="qUpd"></span></div>
    <div class="tasklist" id="taskList"></div>
    <div id="qHidden" style="display:none;font-size:10.5px;color:var(--sub);margin-top:5px"></div>
    </section>

  <section class="card" data-page="notify">
    <h2>通知 <span style="font-weight:400;color:var(--sub)">推送渠道与事件开关</span></h2>
    <div class="form" id="notifyForm"></div>
    <button class="savebtn" id="btnNotifySave">保存通知设置</button>
    <div class="saveMsg" id="notifySaveMsg"></div>
  </section>

  <section class="card" data-page="set">
    <h2>设置 <span style="font-weight:400;color:var(--sub)">保存后下一轮调度生效</span></h2>
    <div class="form" id="setForm"></div>
    <button class="savebtn" id="btnSave">保存设置</button>
    <div class="saveMsg" id="saveMsg"></div>
    <div class="form" style="margin-top:16px">
      <div class="fsec"><div class="fsect">小号工具人</div>
        <div class="frow"><span class="k">大号名称</span><input type="text" id="txtAltMain" placeholder="大号的主人昵称或宠物名（服务端匹配）"></div>
        <div class="frow"><span class="k">一键配置小号</span><button class="minibtn" id="btnAltPreset" style="padding:7px 14px;font-size:12.5px">应用小号预设</button></div>
      </div>
      <div class="saveMsg" id="presetMsg"></div>
    </div>
    <div class="subh">运行信息（只读）</div>
    <div class="cfg" id="cfgList"></div>
  </section>

  <section class="card" data-page="log">
    <h2>实时日志 <span id="logMeta" style="font-weight:400"></span></h2>
    <div class="logctl">
      <button id="btnAuto" class="on">自动滚动</button>
      <input id="logFilter" placeholder="过滤关键字…">
    </div>
    <pre id="logbox">加载中…</pre>
  </section>

  <div data-page="log">
  <section class="card hide" id="shotCard">
    <h2>异常截图（自动保存）</h2>
    <div class="thumbs" id="shots"></div>
  </section>
  </div>

</main>
<!-- 右侧功能栏（QQ 宠物右侧 喂食/洗澡/好友 位）：启动/停止/刷新画面 -->
<!-- 右侧功能栏（照 QQ 首页右侧磨砂长胶囊）。
     注意：手机画面里游戏自带的 喂食/洗澡/好友 是操作宠物的；
     这里是控制调度器的，语义不同故并列保留。 -->
<aside class="funcbar" aria-label="调度器控制">
  <button class="fab" id="btnRunnerStart" title="启动调度器"><span class="fi">&#9654;</span><span class="ft">启动</span></button>
  <button class="fab stop" id="btnRunnerStop" title="停止调度器"><span class="fi">&#9632;</span><span class="ft">停止</span></button>
  <button class="fab ghost" id="btnShot" title="刷新手机画面"><span class="fi">&#8635;</span><span class="ft">画面</span></button>
</aside>
</div><!-- /.app -->
<footer>
  <div id="footStrategy"></div>
  <div>设置保存后下一轮生效 · 日志 3s / 数据 6s / 冒险 10s / 截图 15s</div>
</footer>

<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const TASKNAME={care:'护理',school:'学习',friend_care:'好友护理',gift_bag:'福袋',hire_friend:'雇佣好友',adventure:'冒险',visit:'踩踩',pk:'PK',work:'打工'};
// 任务 -> 图标文件名（static/qp-icons/ 下，必须用 colored/ 里存在的名字）
const TASKICON={care:'soap',school:'logo_study',friend_care:'emoji',gift_bag:'coin',
  hire_friend:'logo_work',adventure:'logo_adventure',visit:'logo_hangout',pk:'logo_pk',work:'logo_work'};
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
    // 按任务换一只宠物（12 张贴图循环取，同一任务固定同一只，避免每轮都跳）
    const idx=Math.abs([...scene].reduce((a,c)=>a+c.charCodeAt(0),0))%12;
    const pet=document.getElementById('stagePet');
    if(pet) pet.src='/qp-icons/pets/pet'+String(idx).padStart(2,'0')+'.png';
    const cap=document.getElementById('stageCap');
    if(cap){
      const NICE={main:'待机中',feed:'吃饭时间',shower:'洗澡时间',record:'学习中'};
      cap.textContent=NICE[scene]||'待机中';
    }
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
  if(!schedOn){
    // 调度器未运行：不引用日志里的旧“预计结算”行（会残留“进行中 剩余00:00”误导）
    rs.textContent='已停止';
    rh.textContent='';
    rsub.textContent='已停止：手机不会被自动操作；随时可再启动';
    $('#workSub').innerHTML=wdHtml;
  }else if(etaRemain!=null){
    const kind=(d.work_eta&&d.work_eta.kind)?d.work_eta.kind:'';
    rs.textContent=kind?(kind+'中'):'进行中';
    rh.textContent='预计 '+etaClock+' 结束';
    $('#workSub').textContent=(etaRemain>0?('剩余 '+hms(etaRemain)):'收尾中…')+' · 结束后自动开启下一项';
    rsub.textContent='';
  }else{
    rs.textContent='等待中';
    rh.textContent=d.last_line?d.last_line.replace(/^\[[\d:]+\]\s*/,'').slice(0,60):'';
    $('#workSub').innerHTML=wdHtml;
    rsub.textContent='';
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
  const etaKind=(d.work_eta&&d.work_eta.kind)?String(d.work_eta.kind):'';
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
  let hiddenQ=[];
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
    return '<div class="mrow'+(done?' done':'')+(isRun?' run':'')+'">'
      +'<span class="mcb'+(on&&st!=='disabled'?' on':'')+'" data-k="'+k+'"></span>'
      +'<img class="qico" src="/qp-icons/'+(TASKICON[k]||'coin')+'-24.png" alt="">'
      +'<span class="mname'+(on?'':' off')+'">'+(TASKNAME[k]||k)+'</span>'+tag
      +'<span class="mdet">'+(on?det:'<span class="off-t">已禁用</span>')+'</span></div>';
  };
  if(qLive){
    $('#qTop').innerHTML=(cur?'<span style="color:var(--accent);font-weight:650">正在执行:'+esc(cur)+'</span> · ':'')
      +'可执行 '+(q.ready??'--')+' · 等待 '+(q.waiting??'--')
      +(q.next?(' · 下个定时 '+(TASKNAME[q.next]||q.next)+' '+String(q.next_at||'').slice(0,5)):'');
    $('#qUpd').textContent=q.updated?('更新 '+q.updated):'';
    hiddenQ=Object.entries(qt).filter(([k,v])=>(v.state||'')==='disabled').map(([k])=>TASKNAME[k]||k);
    if(q.pending) rows+='<div class="mrow"><span class="mname">收尾队列</span>'
      +'<span class="mdet"><span class="run">'+q.pending+' 待结算</span></span></div>';
    const ks=Object.keys(qt).slice().sort((a,b)=>qRank(a)-qRank(b));
    for(const k of ks){
      const st=qt[k].state||'';
      const nx=qt[k].next?('→ '+(qt[k].next.slice(0,10)===todayStr?'':'明 ')+qt[k].next.slice(11,16)):'';
      rows+=rowOf(k, st!=='disabled', st, nx);
    }
  }else{
    const te=cfg.tasks_enabled||{};
    const keys=qOrder.length?qOrder:Object.keys(te);
    $('#qTop').textContent='调度器未运行 · 勾选即启用该任务（保存后下一轮生效）';
    $('#qUpd').innerHTML='<span style="color:#d97706">启动调度器后开始自动托管</span>';
    hiddenQ=keys.filter(k=>te[k]===false).map(k=>TASKNAME[k]||k);
    const allKeys=(keys.length?keys:Object.keys(TASKNAME)).slice().sort((a,b)=>qRank(a)-qRank(b));
    for(const k of allKeys){
      const on=te[k]!==false;
      rows+=rowOf(k, on, 'cfg', '');
    }
  }
  $('#taskList').innerHTML=rows||'';
  const qh=document.getElementById('qHidden');
  if(qh){
    if(hiddenQ.length){ qh.style.display=''; qh.textContent='未启用：'+hiddenQ.join('、')+'（不参与调度）'; }
    else { qh.style.display='none'; qh.textContent=''; }
  }
  // 截图
  const shots=d.shots||[];
  if(shots.length){
    $('#shotCard').classList.remove('hide');
    $('#shots').innerHTML=shots.map(s=>'<a href="/files/'+encodeURIComponent(s.name)+'" target="_blank"><img loading="lazy" src="/files/'+encodeURIComponent(s.name)+'"><span class="cap">'+s.mtime+'</span></a>').join('');
  }
  if(d.editable && !setDirty) renderSettings(d.editable);
  renderCfg((d.config||{}).rows);
  // footer：策略按配额（cfg.strategy 已算好）；打工地点/时长只在打工确实启用时附上，
  // 否则会出现"策略：只学习 · 打工 45分钟 @ 风铃旅社"这种自相矛盾的文案
  let foot='策略：'+(cfg.strategy||'未知');
  if(cfg.work_enabled&&(cfg.work_quota_hours||0)>0&&cfg.work_duration){
    foot+=' · 打工 '+cfg.work_duration+' @ '+(cfg.work_location||'');
  }
  if((cfg.study_quota_hours||0)>0){
    foot+=' · 学习配额 '+cfg.study_quota_hours+'h';
  }
  $('#footStrategy').textContent=foot;
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
    $('#logMeta').textContent=d.name?('('+d.name+' · '+lines.length+' 行)'):'';
    if(logAuto&&near) box.scrollTop=box.scrollHeight;
  }catch(e){}
}

// 秒级：时钟 + 倒计时
setInterval(()=>{
  const n=new Date();
  $('#clock').textContent=pad(n.getHours())+':'+pad(n.getMinutes())+':'+pad(n.getSeconds());
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

let setInit=null, setDirty=false;
function renderSettings(ed){
  if(!ed) return;
  setInit=Object.assign({},ed);
  const sel=(id,opts,cur)=>'<select id="'+id+'">'+opts.map(v=>'<option value="'+v+'"'+(v===cur?' selected':'')+'>'+v+'</option>').join('')+'</select>';
  // 选项名只描述 work 与 adventure 的先后（school/hire_friend 两组里都固定在前，
  // 且各任务能否执行还取决于自身条件——金币/时长上限/疲劳/次数，见 _school_due 等）
  const moOpts=[['school>hire_friend>work>adventure','先打工，打满 8h 再冒险'],['school>hire_friend>adventure>work','先冒险，冒险没次数了再打工']];
  const moSel=(cur)=>'<select id="selMainOrder" title="主任务组（学习/雇佣/冒险/打工）互斥时的执行优先级：按 > 顺序逐个检查，第一个条件满足的执行。学习与雇佣好友在两组预设里都固定排在最前，此处切换的只是打工与冒险的先后；每个任务还要自身条件满足才会执行（金币达标/未超时长上限/未疲劳/次数未满），改完下一轮调度生效">'+moOpts.map(o=>'<option value="'+o[0]+'"'+(o[0]===cur?' selected':'')+'>'+o[1]+'</option>').join('')+(moOpts.some(o=>o[0]===cur)?'':'<option value="'+esc(cur||'')+'" selected>自定义：'+esc(cur||'')+'</option>')+'</select>';
  const FG=(t,rows)=>'<div class="fsec"><div class="fsect">'+t+'</div>'+rows.join('')+'</div>';
  $('#setForm').innerHTML=
    FG('学习',[
    '<div class="frow"><span class="k">只打工不学习</span><button class="sw'+(ed.school_enabled?'':' on')+'" id="swSchool" title="开=只打工；关=学习+打工"></button></div>',
    '<div class="frow"><span class="k">学习科目</span>'+sel('selSchoolAttr', ['力量','智力','魅力','夏令营'], ed.school_attribute)+'</div>',
    '<div class="frow"><span class="k">每天学习次数</span><input type="number" id="numSchoolTimes" min="0" step="1" title="0=不限" value="'+(ed.school_times??0)+'"></div>',
    '<div class="frow"><span class="k">课时档位</span>'+sel('selSchoolDur', ['短课','长课'], ed.school_duration)+'</div>',
    '<div class="frow"><span class="k">当前选择</span><span id="schoolHint" style="color:var(--sub);font-size:12px"></span></div>',
    '<div class="frow"><span class="k">档位说明</span><span style="color:var(--sub);font-size:12px">课程轮播固定 7 张：卡1-3 短课（力量/智力/魅力）、卡4-6 长课（同序）、卡7 萌芽夏令营。各学院具体分钟数不同（初级10/30、高级30/90），实际时长选课后从面板自动读取，升级学院不用改配置</span></div>',
    '<div class="frow"><span class="k">课时说明</span><span style="color:var(--sub);font-size:12px">短课单位消耗收益更高（每30分钟 +6属性/+30学分 vs 长课 +5/+25）</span></div>',
    ])+
    FG('打工',[
    '<div class="frow"><span class="k">打工地点</span>'+sel('selLoc', ed.work_locations||[], ed.work_location)+'</div>',
    '<div class="frow"><span class="k">打工时长</span>'+sel('selDur', ['10分钟','45分钟','2小时'], ed.work_duration)+'</div>',
    '<div class="frow"><span class="k">优先雇佣</span><input type="text" id="txtHire" placeholder="宠物名/主人名，空=自动选收益最高" value="'+esc(ed.hire_name||'')+'"></div>',
    '<div class="frow"><span class="k">等TA空闲</span><button class="sw'+(ed.hire_wait?' on':'')+'" id="swHireWait" title="开=优先雇佣的好友正在打工/学习（面板显示 出门中/被雇佣中）时不换人，点头像进主页读剩余时间，等到他结束再雇（期间先跑冒险/护理等其他任务）；显示 对方今天很累了 时等待无意义，仍换收益最高的人。需先填「优先雇佣」"></button></div>',
    ])+
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
    ])+
    FG('疲劳分两层（8h 降收益仍可跑 / 12h 完全停止）',[
    '<div class="frow"><span class="k">第一层门槛</span><input type="number" id="numEffT1" min="0" max="24" step="1" title="学习+打工合计达到该时长进入【第一层】：收益效率降到 25%，但仍可继续学习/打工。游戏在 8h/12h 的提示文案相同，分层以本工具的时长账本为准" value="'+(ed.efficiency_tier1_hours??8)+'"><span class="u">小时 → 25%，仍可跑</span></div>',
    '<div class="frow"><span class="k">第二层门槛</span><input type="number" id="numEffT2" min="0" max="24" step="1" title="学习+打工合计达到该时长进入【第二层】：收益效率降到 10%，且完全禁止学习/打工（转冒险）。0 = 不设第二层" value="'+(ed.efficiency_tier2_hours??12)+'"><span class="u">小时 → 10%，完全停</span></div>',
    '<div class="frow"><span class="k">说明</span><span style="color:var(--sub);font-size:12px">第一层只降收益、不拦任务；第二层才禁止学习/打工。游戏疲劳提示会记录，但是否停由上面的合计时长决定</span></div>',
    ])+
    FG('调度',[
    '<div class="frow"><span class="k">主任务优先级</span>'+moSel(ed.main_order)+'</div>',
    ])+
    FG('踩踩',[
    '<div class="frow"><span class="k">踩踩次数/天</span><input type="number" id="numVisit" min="0" step="1" value="'+(ed.visit_times??'')+'"></div>',
    ])+
    FG('PK',[
    '<div class="frow"><span class="k">PK 次数/天</span><input type="number" id="numPk" min="0" step="1" value="'+(ed.pk_times??'')+'"></div>',
    '<div class="frow"><span class="k">PK 只打</span><input type="text" id="txtPkOnly" placeholder="昵称或宠物名，逗号分隔，空=不限" value="'+esc(ed.pk_only||'')+'"></div>',
    '<div class="frow"><span class="k">PK 跳过</span><input type="text" id="txtPkSkip" placeholder="昵称或宠物名，逗号分隔，空=不跳过" value="'+esc(ed.pk_skip||'')+'"></div>',
    '<div class="frow"><span class="k">PK 打手</span><input type="text" id="txtPkHelper" placeholder="只雇这些宠物代打（逗号分隔，按优先序）" value="'+esc(ed.pk_helper||'')+'"></div>',
    '<div class="frow"><span class="k">打手兜底</span><button class="sw'+(ed.pk_helper_fallback?' on':'')+'" id="swPkHf" title="开=名单里的打手都不可雇（被雇佣中/不可雇佣/已达上限）时，自动雇战力最高的可雇宠物"></button></div>',
    '<div class="frow"><span class="k">PK 等级上限</span><input type="number" id="numPkLv" min="-2" step="1" title="-1=只打比我低；-2=只打比打手低" value="'+(ed.pk_max_level??0)+'"></div>',
    '<div class="frow"><span class="k">等级过滤说明</span><span style="color:var(--sub);font-size:12px">0=不限；-1=只打比我低的；-2=只打比打手低的</span></div>',
    ])+
    FG('冒险',[
    '<div class="frow"><span class="k">冒险次数/天</span><input type="number" id="numAdv" min="0" step="1" title="0=不冒险；主号策略设 999 ≈ 不限（疲劳后全冒险）" value="'+(ed.adventure_times??'')+'"></div>',
    ])+
    FG('护理',[
    '<div class="frow"><span class="k">护理阈值（体力/清洁）</span><span class="two"><input type="number" id="numEnergy" min="0" max="100" value="'+(ed.care_energy??'')+'"><input type="number" id="numClean" min="0" max="100" value="'+(ed.care_clean??'')+'"></span></div>',
    '<div class="frow"><span class="k">护理方式</span>'+sel('selCare', ['一键护理','ocr检测'], ed.care_method)+'</div>',
    '<div class="frow"><span class="k">补货数量（个）</span><input type="number" id="numExchange" min="1" max="99" step="1" title="饼干/香皂不足时一次金币买多少个" value="'+(ed.care_exchange??'')+'"></div>',
    ])+
    FG('好友护理',[
    '<div class="frow"><span class="k">好友护理</span><button class="sw'+(ed.friend_care_enabled?' on':'')+'" id="swFC" title="开=按间隔到指定好友家护理（体力/清洁<90自动补）"></button></div>',
    '<div class="frow"><span class="k">好友护理对象</span><input type="text" id="txtFCName" placeholder="宠物名或主人名" value="'+esc(ed.friend_care_name||'')+'"></div>',
    '<div class="frow"><span class="k">好友护理间隔（秒）</span><input type="number" id="numFCInt" min="30" step="30" value="'+(ed.friend_care_interval??'')+'"></div>',
    '<div class="frow"><span class="k">好友护理方式</span>'+sel('selFCMethod', ['ocr检测','一键护理'], ed.friend_care_method)+'</div>',
    ])+
    FG('被雇佣（帮好友打工）',[
    '<div class="frow"><span class="k">被雇佣托管</span><button class="sw'+(ed.employed_enabled?' on':'')+'" id="swEmp" title="开=定时出门检查是否被好友雇去打工"></button></div>',
    '<div class="frow"><span class="k">被雇佣处理</span>'+sel('selEmpAction', ['等到25/75（小于45min）','等到25/75','立刻召回','让利雇主（不召回）'], ed.employed_action)+'</div>',
    '<div class="frow"><span class="k">检查间隔（秒）</span><input type="number" id="numEmpInt" min="30" step="30" value="'+(ed.employed_interval??'')+'"></div>',
    ])+
    FG('福袋',[
    '<div class="frow"><span class="k">福袋领取</span><button class="sw'+(ed.gift_bag_enabled?' on':'')+'" id="swGiftBag" title="开=定时遍历好友领取系绳福袋"></button></div>',
    '<div class="frow"><span class="k">福袋扫描间隔（秒）</span><input type="number" id="numGbInt" min="60" step="60" value="'+(ed.gift_bag_interval??'')+'"></div>',
    ])+
    FG('职业',[
    '<div class="frow"><span class="k">隐藏职业解锁监控</span><button class="sw'+(ed.career_watch?' on':'')+'" id="swCareer" title="开=每节课结算后读职业树；武术家/梦境旅人/大明星解锁时记录并推送通知"></button></div>',
    '<div class="frow"><span class="k">解锁后自动停学</span><button class="sw'+(ed.career_stop_study?' on':'')+'" id="swCareerStop" title="开=解锁时自动关闭学习任务（等你安排下一阶段）"></button></div>',
    '<div class="frow"><span class="k">兜底检查间隔（分钟）</span><input type="number" id="numCareerInt" min="0" step="10" title="0 = 只每节课后检查" value="'+(ed.career_interval??60)+'"></div>',
    ]);
  // 通知页单独渲染（不放设置页：渠道配置项多，独立成板更清楚）
  renderNotifyForm(ed);
  $('#swSchool').onclick=()=>{ $('#swSchool').classList.toggle('on'); setDirty=true; };
  $('#swFC').onclick=()=>{ $('#swFC').classList.toggle('on'); setDirty=true; };
  $('#swEmp').onclick=()=>{ $('#swEmp').classList.toggle('on'); setDirty=true; };
  $('#swGiftBag').onclick=()=>{ $('#swGiftBag').classList.toggle('on'); setDirty=true; };
  $('#swCareer').onclick=()=>{ $('#swCareer').classList.toggle('on'); setDirty=true; };
  $('#swCareerStop').onclick=()=>{ $('#swCareerStop').classList.toggle('on'); setDirty=true; };
  $('#swPkHf').onclick=()=>{ $('#swPkHf').classList.toggle('on'); setDirty=true; };
  $('#swHireWait').onclick=()=>{ $('#swHireWait').classList.toggle('on'); setDirty=true; };
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
      setDirty=true; updQuotaHint();
      const msg=$('#saveMsg');
      if(msg){ msg.className='saveMsg'; msg.textContent='已填入「'+b.textContent+'」，记得点下面的保存设置'; }
    };
  });
}

// ---- 通知页（独立板块）：渠道配置 + 事件开关 + 测试 ----
// 与设置页共用 setInit（同一份 editable 快照），但有自己的保存按钮/提示，
// 只提交本页字段（差量），互不干扰。
function renderNotifyForm(ed){
  const form=$('#notifyForm'); if(!form) return;
  const FGn=(t,rows)=>'<div class="fsec"><div class="fsect">'+t+'</div>'+rows.join('')+'</div>';
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
    '<div class="frow"><span class="k">测试</span><button class="minibtn" id="btnTestNotify">发送测试通知</button><span id="notifyTestMsg" style="color:var(--sub);font-size:12px"></span></div>',
    '<div class="frow" style="display:block"><span style="color:var(--sub);font-size:12px;line-height:1.7">'
    +'<b>飞书</b>：群 → 右上角设置 → 群机器人 → 添加机器人 → 自定义机器人，复制 webhook 地址；'
    +'安全设置选「签名校验」就把密钥填到加签密钥（选「自定义关键词」可留空，关键词需含"QQ宠物"）。<br>'
    +'<b>Telegram</b>：跟 @BotFather 发 /newbot 建机器人拿 Token；<b>先给机器人发一条消息</b>，'
    +'再用 @userinfobot 查自己的 Chat ID（群/频道是 -100 开头的负数）。<br>'
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
  const btn=$('#btnNotifySave'), msg=$('#notifySaveMsg');
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
  if(btn) btn.disabled=true;
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
    if(btn) btn.disabled=false;
  }
}
const _btnNS=document.getElementById('btnNotifySave');
if(_btnNS) _btnNS.onclick=()=>saveNotifySettings(false);
document.addEventListener('input',e=>{ if(e.target&&e.target.closest&&e.target.closest('#notifyForm')) setDirty=true; });

async function saveSettings(){
  if(!setInit) return;
  const btn=$('#btnSave'); btn.disabled=true;
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
  // 注意：通知渠道字段在**通知页**（#notifyForm），由 saveNotifySettings 单独提交，
  // 这里不要再取（那些 id 已不在本表单里，取了会是 null 而报错）。
  if(!Object.keys(updates).length){ msg.className='saveMsg'; msg.textContent='没有改动'; btn.disabled=false; return; }
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({updates})});
    const d=await r.json();
    if(d.rejected&&d.rejected.length){ msg.className='saveMsg err'; msg.textContent='部分未保存：'+d.rejected.join('；'); }
    else { setDirty=false; msg.className='saveMsg'; msg.textContent='✅ 已保存，下一轮调度生效'; refreshData(); }
  }catch(e){ msg.className='saveMsg err'; msg.textContent='保存失败：'+e.message; }
  btn.disabled=false;
}
$('#btnSave').onclick=saveSettings;
$('#setForm').addEventListener('input',()=>{setDirty=true});
$('#setForm').addEventListener('click',()=>{setDirty=true});

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

function showTab(name){
  document.querySelectorAll('main > [data-page]').forEach(el=>el.classList.toggle('hide', el.dataset.page!==name));
  document.querySelectorAll('#tabbar button').forEach(b=>b.classList.toggle('on', b.dataset.tab===name));
  try{localStorage.setItem('qpet_tab',name);}catch(e){}
}
document.querySelectorAll('#tabbar button').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
let initTab='main';
try{initTab=localStorage.getItem('qpet_tab')||'main';}catch(e){}
showTab(initTab);

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
                         '.webp': 'image/webp'}
                if fp.exists() and fp.suffix.lower() in CTYPE and fp.is_relative_to(root):
                    self._send(200, CTYPE[fp.suffix.lower()], fp.read_bytes(), 'max-age=604800')
                else:
                    self._send(404, 'text/plain', b'not found')
            elif path in ('/icon-192.png', '/icon-512.png'):
                fp = BASE / 'static' / path.lstrip('/')
                if fp.exists():
                    data = fp.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/png')
                    self.send_header('Cache-Control', 'max-age=604800')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._send(404, 'text/plain', b'not found')
            elif path == '/api/data':
                body = json.dumps(build_data(), ensure_ascii=False).encode('utf-8')
                self._send(200, 'application/json; charset=utf-8', body)
            elif path == '/api/adventure':
                q = parse_qs(u.query)
                body = json.dumps(adventure_data((q.get('date') or [''])[0]),
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
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self._send(200 if result['ok'] else 400,
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
