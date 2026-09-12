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

import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
RUNS = BASE / 'runs'
LOGS = RUNS / 'logs'
ADV_LIVE_FILE = RUNS / 'adventure_live.jsonl'   # 冒险统一记录（实验 300 把 + 日常实时）
DEFAULT_PORT = 8787


# ---------------------------------------------------------------- 数据读取

def read_json(name: str) -> dict:
    try:
        return json.loads((RUNS / name).read_text('utf-8'))
    except Exception:
        return {}


def audit(msg: str) -> None:
    """关键操作审计日志（谁什么时候改了什么），写 runs/logs/dashboard.log。"""
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / 'dashboard.log', 'a', encoding='utf-8') as f:
            f.write(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n')
    except Exception:
        pass


def scheduler_info() -> dict:
    try:
        out = subprocess.run(['pgrep', '-f', 'scenarios/runner.py'],
                             capture_output=True, text=True, timeout=5).stdout
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        pids = []
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
        if 'runner.py' in cmd and 'python' in cmd.lower():
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
    """从日志里找最后一次“预计 N 秒后结束（HH:MM:SS 收尾）”，换算剩余秒数。"""
    m = None
    for ln in lines[-400:]:
        mm = re.search(r'预计 (\d+) 秒后结束（(\d{2}):(\d{2}):(\d{2}) 收尾）', ln)
        if mm:
            m = mm
    if not m:
        return None
    now = datetime.now()
    target = now.replace(hour=int(m.group(2)), minute=int(m.group(3)),
                         second=int(m.group(4)), microsecond=0)
    rem = int((target - now).total_seconds())
    if rem < -600:
        return None
    return {'eta_clock': f'{m.group(2)}:{m.group(3)}:{m.group(4)}',
            'remaining': max(0, rem)}


def today_duration(lines: list[str]):
    """最后一条“今日时长: 已学习 X 分钟 + 已打工 Y 分钟”的 X/Y + 效率档。

    效率档（游戏机制，与 scenarios/runner.py 的 EFFICIENCY_TIERS 一致）：
    学习+打工合计 >12 小时 10%、>8 小时 25%、否则 100%。"""
    m = None
    for ln in lines[-400:]:
        mm = re.search(r'今日时长: 已学习 (\d+) 分钟 \+ 已打工 (\d+) 分钟', ln)
        if mm:
            m = mm
    if not m:
        return None
    learn_min, work_min = int(m.group(1)), int(m.group(2))
    total_min = learn_min + work_min
    eff = 10 if total_min >= 12 * 60 else (25 if total_min >= 8 * 60 else 100)
    return {'learn_min': learn_min, 'work_min': work_min, 'eff_pct': eff}


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
    recover = cfg.get('recover') or {}
    adb = cfg.get('adb') or {}
    control = cfg.get('control') or {}
    notify = cfg.get('notify') or {}
    runner = cfg.get('runner') or {}
    school_enabled = bool((tasks.get('school') or {}).get('enabled', True))

    def n_per_day(n):
        return '不限次' if not n else f'{n} 次/天'

    rows = [
        ['调度策略', '只打工不学习' if not school_enabled else '学习 + 打工'],
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
        ['通知', 'macOS 桌面通知' + (' + OnePush' if str(notify.get('onepush_config', '')).strip() else '')],
    ]
    return {
        'strategy': '只打工不学习' if not school_enabled else '学习+打工',
        'school_enabled': school_enabled,
        'work_location': work.get('location'),
        'work_duration': work.get('duration'),
        'coin_threshold': sched.get('coin_threshold'),
        'visit_per_day': visit.get('times_per_day'),
        'pk_per_day': pk.get('times_per_day'),
        'adventure_times': adv.get('times_per_day'),
        'adventure_start': adv.get('start_time'),
        'task_order': [x.strip() for x in str(tasks.get('order') or '').split('>') if x.strip()],
        'rows': rows,
    }


def load_progress() -> dict:
    return {
        'visit': read_json('visit_progress.json'),
        'pk': read_json('pk_progress.json'),
        'work': read_json('work_progress.json'),
        'adventure': read_json('adventure_progress.json'),
        'exp_daily': read_json('exp_daily_progress.json'),
    }


def adventure_data() -> dict:
    """冒险记录（统一数据源：runs/adventure_live.jsonl——实验期 300 把 + 日常实时，
    由 src/scenario.record_adventure_live 在每把结算时记录）。"""
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
    coins = [int(r.get('coins') or 0) for r in rows]
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
    today = datetime.now().strftime('%Y-%m-%d')
    tn = tnet = 0
    gains = {}
    recent = []
    for i, r in enumerate(rows, 1):
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
        if ts.startswith(today):
            tn += 1
            tnet += c
        recent.append([i, ts[5:16] or ts[:5], c, ' '.join(gs), 0])
    return {
        'ok': True, 'n': n,
        'net': net, 'avg': round(net / n, 2) if n else 0,
        'dist': dist, 'win': win, 'zero': zero, 'loss': 0,
        'gains': [[k, v[0], v[1]] for k, v in sorted(gains.items())],
        'cum': cum, 'pts': pts, 'stats': [], 'recent': recent[-800:],
        'today_n': tn, 'today_net': tnet,
        'updated': str(rows[-1].get('ts') or '')[11:16],
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

    gates = {'工分': int(v['工分']) >= 360, '金币': int(v['金币']) >= 1000,
             '初级毕业': bool(v['初级毕业']), '中级毕业': bool(v['中级毕业'])}
    gates_ok = gates['工分'] and gates['金币'] and gates['初级毕业']
    lines = []
    for name, jr, ch in _PLAN_LINES:
        if jr is None and ch is None:
            lines.append({'name': name, 'jr': True, 'ch': False})
            continue
        jr_ok = ratio_ok(jr) if isinstance(jr, int) else _triple_ok(V, jr)
        ch_attr = _triple_ok(V, ch)
        lines.append({'name': name, 'jr': jr_ok, 'ch': ch_attr and gates_ok,
                      'ch_attr': ch_attr})
    r_force = max(24, int(2.5 * (V[1] + V[2])))
    r_int = max(100, int(2.5 * (V[0] + V[2])))
    steps = [
        ('S1', '魅力→24，解锁 偶像练习生', V[2] >= 24, f'{V[2]}/24'),
        ('S2', '补 力11·智11，解锁 侦探/法师/画家/大厨',
         V[0] >= 11 and V[1] >= 11 and V[2] >= 24, f'{V[0]}/11 · {V[1]}/11'),
        ('S3', '力量专修，解锁 习武小童（≥其余两和的2.5倍）',
         V[0] >= 24 and V[0] >= 2.5 * (V[1] + V[2]), f'{V[0]}（需≥{r_force}）'),
        ('S4', '智力专修，解锁 浅梦行者', V[1] >= 100 and V[1] >= 2.5 * (V[0] + V[2]),
         f'{V[1]}（需≥{r_int}）'),
        ('S5', '魅力补到 225（混合线初级前置）', V[2] >= 225, f'{V[2]}/225'),
        ('S6', '三维各 750 → 全 8 线初级', min(V) >= 750, f'最低 {min(V)}/750'),
    ]
    total = sum(V)
    return {
        'ok': True, 'values': v, 'total': total, 'total_target': 2250,
        'jr_n': sum(1 for l in lines if l['jr']),
        'ch_n': sum(1 for l in lines if l['ch']),
        'lines': lines,
        'steps': [[s[0], s[1], s[2], s[3]] for s in steps],
        'gates': gates,
        'updated': datetime.now().strftime('%H:%M:%S'),
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
    return {
        'school_enabled': bool((tasks.get('school') or {}).get('enabled', True)),
        'school_attribute': str(school.get('attribute') or '力量'),
        'school_duration': str(school.get('duration') or '10分钟'),
        'school_times': school.get('times_per_day', 0),
        'work_location': work.get('location'),
        'work_locations': locations,
        'work_duration': work.get('duration'),
        'hire_name': str(work.get('hire_name') or ''),
        'coin_threshold': sched.get('coin_threshold', 2000),
        'daily_hour_limit': sched.get('daily_hour_limit', 8),
        'work_stop_hours': sched.get('work_stop_hours', 12),
        'main_order': str(tasks.get('main_order') or ''),
        'visit_times': visit.get('times_per_day', 10),
        'pk_times': pk.get('times_per_day', 15),
        'pk_only': str(pk.get('only_names') or ''),
        'pk_skip': str(pk.get('skip_names') or ''),
        'pk_max_level': pk.get('max_level', 0) or 0,
        'pk_helper': str(pk.get('helper_names') or ''),
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
    }


def apply_settings(updates: dict) -> dict:
    """把设置卡改动写入 config.yaml（ruamel 往返保留注释，复用 src/settings 校验）。

    调度器每轮重读配置（含 tasks.* 任务级设置），保存后下一轮调度自动生效。
    """
    import src.settings as S
    mapping = {
        'school_enabled': ('tasks.school.enabled', 'bool'),
        'work_location': ('work.location', None),
        'work_duration': ('work.duration', None),
        'hire_name': ('work.hire_name', None),
        'coin_threshold': ('schedule.coin_threshold', 'int'),
        'daily_hour_limit': ('schedule.daily_hour_limit', 'int'),
        'work_stop_hours': ('schedule.work_stop_hours', 'int'),
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
        'adventure_times': ('adventure.times_per_day', 'int'),
        'care_energy': ('care.energy_threshold', 'int'),
        'care_clean': ('care.clean_threshold', 'int'),
        'care_method': ('care.method', None),
        'care_exchange': ('care.exchange_count', 'int'),
        'friend_care_enabled': ('friend_care.enabled', 'bool'),
        'friend_care_name': ('friend_care.friend_name', None),
        'friend_care_interval': ('friend_care.interval_seconds', 'int'),
        'friend_care_method': ('friend_care.method', None),
        'employed_enabled': ('employed.enabled', 'bool'),
        'employed_action': ('employed.action', None),
        'employed_interval': ('employed.interval_seconds', 'int'),
        'gift_bag_enabled': ('gift_bag.enabled', 'bool'),
        'gift_bag_interval': ('gift_bag.interval_seconds', 'int'),
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
            S.set_value(data, key, value)
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
    return {
        'now': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'scheduler': scheduler_info(),
        'queue': read_json('queue_status.json'),
        'status': accounts.get('default') or {},
        'progress': load_progress(),
        'config': config_summary(),
        'shots': list_shots(),
        'work_eta': work_eta(log_lines),
        'today_duration': today_duration(log_lines),
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
<meta name="theme-color" content="#f6f7f9">
<title>QQ宠物托管</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--line:#e6e8ee;--text:#111827;--sub:#6b7280;--accent:#533afd;--ok:#16a34a;--warn:#b45309;--gold:#D4A017;--gold-d:#B8860B}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%;overflow-x:hidden}
header{position:sticky;top:0;z-index:10;background:rgba(246,247,249,.9);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:10px 14px;display:flex;flex-direction:column;align-items:stretch;gap:0;padding-top:calc(10px + env(safe-area-inset-top))}
.hrow{display:flex;justify-content:space-between;align-items:center;width:100%}
.tabs{display:flex;gap:6px;margin-top:8px;width:100%}
.tabs button{flex:1;border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 0;font-size:12.5px;color:var(--sub)}
.tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.brand{font-weight:650;font-size:15px;display:flex;gap:8px;align-items:center}
.dot{width:8px;height:8px;border-radius:50%;background:#9ca3af;flex:none}
.dot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(22,163,74,.15)}
.dot.off{background:#ef4444;box-shadow:0 0 0 3px rgba(239,68,68,.12)}
.meta{font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums}
main{padding:12px;max-width:560px;margin:0 auto;display:flex;flex-direction:column;gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
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
.qhead{display:flex;justify-content:space-between;font-size:12px;color:var(--sub);margin-bottom:6px;font-variant-numeric:tabular-nums}
.tasklist .row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-top:1px dashed var(--line);font-size:14px}
.tasklist .row:first-child{border-top:0}
.tasklist .t{display:flex;gap:8px;align-items:center}
.tasklist .nx{font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums}
.chip{font-size:11px;padding:2px 8px;border-radius:999px;background:#f1f2f6;color:#6b7280;flex:none}
.chip.ready{background:#eaf7ee;color:#15803d}
.chip.wait{background:#f1efff;color:#533afd}
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
.duoshot{flex:0 0 46%;min-width:0}
.duoque{flex:1;min-width:0}
#phoneShot{display:block;width:100%;height:auto;border-radius:8px;border:1px solid var(--line);background:#eef0f4;min-height:48px}
.duoshot .shotctl{flex-wrap:wrap;gap:4px 8px}
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
.plansteps .st.cur{background:#f6f4ff;border-radius:8px;padding-left:6px;padding-right:6px}
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
</style>
</head>
<body>
<header>
  <div class="hrow">
    <div class="brand"><span class="dot" id="schedDot"></span>QQ宠物托管 <span style="font-weight:400;color:var(--sub);font-size:12px" id="schedTxt"></span></div>
    <div class="meta" id="clock">--:--:--</div>
  </div>
  <nav class="tabs" id="tabbar">
    <button data-tab="main" class="on">总览</button>
    <button data-tab="adv">冒险</button>
    <button data-tab="plan">职业</button>
    <button data-tab="set">设置</button>
    <button data-tab="log">日志</button>
  </nav>
</header>
<main>
  <section class="card" id="runnerCard" data-page="main">
    <h2>调度器 <span id="runnerMeta" style="font-weight:400;font-size:10.5px"></span></h2>
    <div class="workline"><span class="dot" id="runnerDot"></span><span class="big" id="runnerState" style="font-size:15px">--</span><span class="hint" id="runnerHint"></span></div>
    <div class="subline" id="runnerSub"></div>
    <div class="btnrow2"><button class="savebtn" id="btnRunnerStart">▶ 启动调度器</button><button class="savebtn ghost" id="btnRunnerStop">■ 停止调度器</button></div>
    <div class="saveMsg" id="runnerMsg"></div>
  </section>

  <section class="card" id="workCard" data-page="main">
    <h2>打工循环</h2>
    <div class="workline"><span class="big" id="workBig">--</span><span class="hint" id="workHint"></span></div>
    <div class="subline" id="workSub"></div>
  </section>

  <section class="card" id="advCard" data-page="adv">
    <h2>冒险记录 <span id="advMeta" style="font-weight:400;font-size:10.5px"></span></h2>
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
    <div class="subh">进度录入（新号的当前数值，改完点保存）</div>
    <div class="plinedit" id="planEdit"></div>
    <div class="btnrow2"><button class="savebtn" id="btnPlanSave">保存进度</button><button class="savebtn ghost" id="btnPlanSync">🔄 自动识别</button></div>
    <div class="saveMsg" id="planMsg"></div>
    <div class="subh">阶梯路线</div>
    <div class="plansteps" id="planSteps"></div>
    <div class="subh">8 线解锁状态（见习 / 初级）</div>
    <div class="planlines" id="planLines"></div>
  </section>

  <section class="grid" data-page="main">
    <div class="tile"><div class="v" id="coins">--</div><div class="k">金币 <span id="coinsAt" style="opacity:.75"></span></div></div>
    <div class="tile"><div class="v" id="visitTxt">--</div><div class="k">今日踩踩</div><div class="bar"><i id="visitBar"></i></div></div>
    <div class="tile"><div class="v" id="pkTxt">--</div><div class="k">今日PK</div><div class="bar"><i id="pkBar"></i></div></div>
    <div class="tile"><div class="v" id="advTxt">--</div><div class="k">今日冒险</div></div>
    <div class="tile"><div class="v" id="workCnt">--</div><div class="k">今日打工(次)</div></div>
    <div class="tile"><div class="v" id="expTxt">--</div><div class="k">经验日常</div></div>
  </section>

  <section class="duo" data-page="main">
    <div class="card duoshot">
      <h2>手机画面 <span id="shotMeta" style="font-weight:400;font-size:10.5px"></span></h2>
      <a id="shotLink" href="/api/screenshot" target="_blank" rel="noopener"><img id="phoneShot" alt="加载中…"></a>
      <div class="shotctl"><button id="btnShot">刷新</button><span id="shotErr" class="err"></span></div>
    </div>
    <div class="card duoque">
      <h2>任务队列</h2>
      <div class="qhead"><span id="qTop">--</span><span id="qUpd"></span></div>
      <div class="tasklist" id="taskList"></div>
    </div>
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
<footer>
  <div id="footStrategy"></div>
  <div>设置保存后下一轮生效 · 日志 3s / 数据 6s / 冒险 10s / 截图 15s</div>
</footer>

<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const TASKNAME={care:'护理',school:'学习',friend_care:'好友护理',gift_bag:'福袋',hire_friend:'雇佣好友',adventure:'冒险',visit:'踩踩',pk:'PK',work:'打工'};
let etaRemain=null, etaClock='';
let logAuto=true, logFilter='';
try{ logAuto = localStorage.getItem('qpet_logAuto')!=='0'; }catch(e){}

function pad(n){return String(n).padStart(2,'0')}
function hms(sec){sec=Math.max(0,Math.floor(sec));const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;return (h?h+':':'')+pad(m)+':'+pad(s)}

async function j(u){const r=await fetch(u,{cache:'no-store'});if(!r.ok)throw new Error(r.status);return await r.json()}

function renderData(d){
  const todayStr=(d.now||'').slice(0,10);
  // 头部
  const dot=$('#schedDot');
  dot.className='dot '+(d.scheduler.alive?'on':'off');
  $('#schedTxt').textContent=d.scheduler.alive?('运行中 · 已跑 '+(d.scheduler.uptime||'')):'未运行';
  // 调度器卡片
  if($('#runnerState')){
    const sch=d.scheduler||{};
    $('#runnerDot').className='dot '+(sch.alive?'on':'off');
    $('#runnerState').textContent=sch.alive?'运行中':'已停止';
    $('#runnerHint').textContent=sch.alive?('PID '+sch.pid+(sch.uptime?(' · 已跑 '+sch.uptime):'')):'';
    $('#runnerSub').textContent=sch.alive?'正在按任务队列自动跑（护理/踩踩/PK/打工/冒险…）':'已停止：手机不会被自动操作；随时可再启动';
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
  if(etaRemain!=null){
    $('#workBig').textContent='进行中';
    $('#workHint').textContent='预计 '+etaClock+' 结算';
    $('#workSub').textContent='剩余 '+hms(etaRemain)+' · 结算后自动开启下一轮';
  }else{
    const wd=d.today_duration;
    $('#workBig').textContent='等待中';
    $('#workHint').textContent=d.last_line?d.last_line.replace(/^\[[\d:]+\]\s*/,'').slice(0,60):'';
    $('#workSub').innerHTML=wd?('今日：学习 '+wd.learn_min+' 分 · 打工 '+wd.work_min+' 分 · '+((wd.eff_pct!=null&&wd.eff_pct<100)?('<span style="color:#d97706">效率 '+wd.eff_pct+'%</span>'):'效率 100%')):'';
  }
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
  const wk=pg.work&&pg.work.learned!=null?pg.work.learned:0;
  $('#workCnt').textContent=etaRemain!=null?wk+'+1':wk;
  const ed=(pg.exp_daily&&pg.exp_daily.done)?'✓ 完成':'未完成';
  $('#expTxt').textContent=ed;
  // 队列
  const q=d.queue||{}, qt=q.tasks||{};
  $('#qTop').textContent='待执行 '+(q.ready??'--')+' · 等待中 '+(q.waiting??'--')+(q.next?(' · 下个定时：'+(TASKNAME[q.next]||q.next)+' '+(q.next_at||'')):'');
  $('#qUpd').textContent=q.updated?('更新 '+q.updated):'';
  // 按执行顺序排：可执行在前（按任务执行顺序）、定时的居中（按时间升序）、已禁用/今日完成沉底
  const qOrder=(cfg.task_order||[]);
  const qRank=k=>{const i=qOrder.indexOf(k);return i<0?999:i;};
  const qStatRank=s=> s==='ready'?0 : s==='waiting'?1 : 2;
  const qItems=Object.entries(qt).map(([k,v])=>({k,v,st:v.state||'',sr:qStatRank(v.state||'')}));
  qItems.sort((a,b)=> (a.sr-b.sr)
      || (a.sr===1 ? String(a.v.next||'~').localeCompare(String(b.v.next||'~')) : (qRank(a.k)-qRank(b.k))));
  let rows='';
  if(q.pending) rows+='<div class="row"><div class="t"><span>收尾队列</span><span class="chip ready">'+q.pending+' 待结算</span></div><div class="nx"></div></div>';
  for(const o of qItems){
    const stt=o.st;
    const chip= stt==='ready'?'<span class="chip ready">可执行</span>'
              : stt==='waiting'?'<span class="chip wait">等待</span>'
              : stt==='done'?'<span class="chip done">✓ 今日完成</span>'
              : stt==='dead'?'<span class="chip done">✓ 今日完成</span>'
              : stt==='disabled'?'<span class="chip off">已禁用</span>'
              : '<span class="chip">'+stt+'</span>';
    const nx=o.v.next?('→ '+(o.v.next.slice(0,10)===todayStr?'':'明 ')+o.v.next.slice(11,16)):'';
    rows+='<div class="row"><div class="t"><span>'+(TASKNAME[o.k]||o.k)+'</span>'+chip+'</div><div class="nx">'+nx+'</div></div>';
  }
  $('#taskList').innerHTML=rows||'<div class="row">无数据（调度器未运行？）</div>';
  // 截图
  const shots=d.shots||[];
  if(shots.length){
    $('#shotCard').classList.remove('hide');
    $('#shots').innerHTML=shots.map(s=>'<a href="/files/'+encodeURIComponent(s.name)+'" target="_blank"><img loading="lazy" src="/files/'+encodeURIComponent(s.name)+'"><span class="cap">'+s.mtime+'</span></a>').join('');
  }
  if(d.editable && !setDirty) renderSettings(d.editable);
  renderCfg((d.config||{}).rows);
  // footer
  $('#footStrategy').textContent='策略：'+(cfg.strategy||'未知')+(cfg.work_duration?(' · 打工 '+cfg.work_duration+' @ '+cfg.work_location):'');
}

async function refreshData(){
  try{ renderData(await j('/api/data')); }
  catch(e){ $('#schedDot').className='dot off'; $('#schedTxt').textContent='连接失败'; }
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
    inner+='<polyline points="'+d.cum.map(p=>sx(p[0]).toFixed(1)+','+sy0(p[1]).toFixed(1)).join(' ')+'" fill="none" stroke="#533afd" stroke-width="2" stroke-linejoin="round"/>';
    const lp=d.cum[d.cum.length-1];
    inner+='<circle cx="'+sx(lp[0]).toFixed(1)+'" cy="'+sy0(lp[1]).toFixed(1)+'" r="3" fill="#533afd"/>';
  }
  if(window.__advSel&&window.__advSel.chart==='cum'){const cm={};(d.cum||[]).forEach(p=>cm[p[0]]=p[1]);const k=window.__advSel.k;if(k in cm){const xx=sx(k).toFixed(1);inner+='<line x1="'+xx+'" y1="4" x2="'+xx+'" y2="80" stroke="#94a3b8" stroke-width="1" stroke-dasharray="3 3"/><circle cx="'+xx+'" cy="'+sy0(cm[k]).toFixed(1)+'" r="4" fill="#533afd" stroke="#fff" stroke-width="1.5"/>';}}
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
  $('#advMeta').textContent='共 '+d.n+' 把 · 今日 '+(d.today_n||0)+' 把（'+((d.today_net||0)>0?'+':'')+(d.today_net||0)+'） · 更新 '+(d.updated||'');
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
  $('#planBars').innerHTML=planBar('属性总进度',d.total,d.total_target,'var(--accent)')+planBar('见习解锁',d.jr_n,8,'#16a34a')+planBar('初级解锁',d.ch_n,8,'#533afd');
  let firstOpen=false;
  $('#planSteps').innerHTML=(d.steps||[]).map(s=>{
    let cls='st',dot='○';
    if(s[2]){cls+=' done';dot='✅';}
    else if(!firstOpen){cls+=' cur';dot='▶';firstOpen=true;}
    return '<div class="'+cls+'"><span class="dot2">'+dot+'</span><span class="tx">'+esc(s[0]+' · '+s[1])+'</span><span class="pr">'+esc(s[3])+'</span></div>';
  }).join('');
  $('#planLines').innerHTML=(d.lines||[]).map(l=>'<div class="ln"><span>'+esc(l.name)+'</span><span><span class="chipx'+(l.jr?' ok':'')+'">见习</span><span class="chipx'+(l.ch?' ok':'')+'">初级</span></span></div>').join('');
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
  try{ renderAdventure(await j('/api/adventure')); }catch(e){}
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
  if(etaRemain!=null){
    etaRemain-=1;
    const sub= etaRemain>0? ('剩余 '+hms(etaRemain)+' · 结算后自动开启下一轮') : '结算中…';
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
  const moOpts=[['school>hire_friend>work>adventure','打工优先（打满8h疲劳后全冒险）'],['school>hire_friend>adventure>work','冒险优先（有次数就优先冒险）']];
  const moSel=(cur)=>'<select id="selMainOrder" title="主任务组（学习/雇佣/冒险/打工）互斥时的执行优先级，改完下一轮调度生效">'+moOpts.map(o=>'<option value="'+o[0]+'"'+(o[0]===cur?' selected':'')+'>'+o[1]+'</option>').join('')+(moOpts.some(o=>o[0]===cur)?'':'<option value="'+esc(cur||'')+'" selected>自定义：'+esc(cur||'')+'</option>')+'</select>';
  const FG=(t,rows)=>'<div class="fsec"><div class="fsect">'+t+'</div>'+rows.join('')+'</div>';
  $('#setForm').innerHTML=
    FG('学习',[
    '<div class="frow"><span class="k">只打工不学习</span><button class="sw'+(ed.school_enabled?'':' on')+'" id="swSchool" title="开=只打工；关=学习+打工"></button></div>',
    '<div class="frow"><span class="k">学习科目</span>'+sel('selSchoolAttr', ['力量','智力','魅力','夏令营'], ed.school_attribute)+'</div>',
    '<div class="frow"><span class="k">每天学习次数</span><input type="number" id="numSchoolTimes" min="0" step="1" title="0=不限" value="'+(ed.school_times??0)+'"></div>',
    '<div class="frow"><span class="k">课时时长</span>'+sel('selSchoolDur', ['10分钟','30分钟'], ed.school_duration)+'</div>',
    '<div class="frow"><span class="k">课时说明</span><span style="color:var(--sub);font-size:12px">10分钟课单位消耗收益更高；夏令营=随机属性+5（固定30分钟）</span></div>',
    '<div class="frow"><span class="k">金币阈值</span><input type="number" id="numCoin" min="0" step="100" title="金币 ≥ 该值优先学习，低于该值先打工" value="'+(ed.coin_threshold??'')+'"></div>',
    ])+
    FG('打工',[
    '<div class="frow"><span class="k">打工地点</span>'+sel('selLoc', ed.work_locations||[], ed.work_location)+'</div>',
    '<div class="frow"><span class="k">打工时长</span>'+sel('selDur', ['10分钟','45分钟','2小时'], ed.work_duration)+'</div>',
    '<div class="frow"><span class="k">优先雇佣</span><input type="text" id="txtHire" placeholder="宠物名/主人名，空=自动选收益最高" value="'+esc(ed.hire_name||'')+'"></div>',
    ])+
    FG('调度与效率',[
    '<div class="frow"><span class="k">主任务优先级</span>'+moSel(ed.main_order)+'</div>',
    '<div class="frow"><span class="k">时长上限（小时）</span><input type="number" id="numHour" min="0" step="1" title="学习+打工合计到该时长后今天不再学习（只打工）" value="'+(ed.daily_hour_limit??'')+'"></div>',
    '<div class="frow"><span class="k">打工停止（小时）</span><input type="number" id="numWorkStop" min="0" max="24" step="1" title="学习+打工合计到该时长后今天不再打工（主号=8：打满疲劳档转全冒险），0=不限" value="'+(ed.work_stop_hours??'')+'"></div>',
    ])+
    FG('踩踩',[
    '<div class="frow"><span class="k">踩踩次数/天</span><input type="number" id="numVisit" min="0" step="1" value="'+(ed.visit_times??'')+'"></div>',
    ])+
    FG('PK',[
    '<div class="frow"><span class="k">PK 次数/天</span><input type="number" id="numPk" min="0" step="1" value="'+(ed.pk_times??'')+'"></div>',
    '<div class="frow"><span class="k">PK 只打</span><input type="text" id="txtPkOnly" placeholder="昵称或宠物名，逗号分隔，空=不限" value="'+esc(ed.pk_only||'')+'"></div>',
    '<div class="frow"><span class="k">PK 跳过</span><input type="text" id="txtPkSkip" placeholder="昵称或宠物名，逗号分隔，空=不跳过" value="'+esc(ed.pk_skip||'')+'"></div>',
    '<div class="frow"><span class="k">PK 打手</span><input type="text" id="txtPkHelper" placeholder="只雇这些宠物代打（逗号分隔，按优先序）" value="'+esc(ed.pk_helper||'')+'"></div>',
    '<div class="frow"><span class="k">PK 等级上限</span><input type="number" id="numPkLv" min="-1" step="1" title="-1 = 只打等级比我低的" value="'+(ed.pk_max_level??0)+'"></div>',
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
    ]);
  $('#swSchool').onclick=()=>{ $('#swSchool').classList.toggle('on'); setDirty=true; };
  $('#swFC').onclick=()=>{ $('#swFC').classList.toggle('on'); setDirty=true; };
  $('#swEmp').onclick=()=>{ $('#swEmp').classList.toggle('on'); setDirty=true; };
  $('#swGiftBag').onclick=()=>{ $('#swGiftBag').classList.toggle('on'); setDirty=true; };
}

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
  const getv=id=>($(id)?$(id).value.trim():'');
  const num=(id,key)=>{const v=getv(id); if(v==='')return; const n=parseInt(v,10); if(!isNaN(n)&&n!==setInit[key]) updates[key]=n;};
  const selc=(id,key)=>{const v=getv(id); if(v&&v!==setInit[key]) updates[key]=v;};
  const txtc=(id,key)=>{const v=getv(id); if(v!==(setInit[key]||'')) updates[key]=v;};
  selc('#selLoc','work_location'); selc('#selDur','work_duration'); selc('#selCare','care_method'); selc('#selFCMethod','friend_care_method'); selc('#selEmpAction','employed_action'); selc('#selMainOrder','main_order'); selc('#selSchoolAttr','school_attribute'); selc('#selSchoolDur','school_duration');
  txtc('#txtHire','hire_name');
  num('#numCoin','coin_threshold'); num('#numHour','daily_hour_limit'); num('#numWorkStop','work_stop_hours'); num('#numSchoolTimes','school_times');
  num('#numVisit','visit_times'); num('#numPk','pk_times'); num('#numAdv','adventure_times');
  txtc('#txtPkOnly','pk_only'); txtc('#txtPkSkip','pk_skip'); num('#numPkLv','pk_max_level'); txtc('#txtPkHelper','pk_helper');
  num('#numEnergy','care_energy'); num('#numClean','care_clean'); num('#numExchange','care_exchange'); num('#numGbInt','gift_bag_interval');
  txtc('#txtFCName','friend_care_name'); num('#numFCInt','friend_care_interval'); num('#numEmpInt','employed_interval');
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
    shotUrl=u; img.src=u;
    $('#shotMeta').textContent='拍摄 '+((r.headers.get('X-Shot-At')||'').slice(0,5));
    $('#shotErr').textContent='';
  }catch(e){ $('#shotErr').textContent='获取失败，点“刷新”重试'; }
  shotBusy=false;
}
$('#btnShot').onclick=()=>refreshShot(true);

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
            elif path == '/api/data':
                body = json.dumps(build_data(), ensure_ascii=False).encode('utf-8')
                self._send(200, 'application/json; charset=utf-8', body)
            elif path == '/api/adventure':
                body = json.dumps(adventure_data(), ensure_ascii=False).encode('utf-8')
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
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'QQ宠物托管仪表盘已启动: http://0.0.0.0:{port} (Ctrl+C 停止)')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
