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

# 实时流模块（设备端 scrcpy-server + 本机 ffmpeg 转 HLS）：不可用时 dashboard 仍要能跑
try:
    from src.screen_stream import STREAM_DIR, get_stream
except Exception as _stream_err:                            # noqa: BLE001
    STREAM_DIR, get_stream = None, None
    print(f'[dashboard] 实时流模块不可用（/stream 与 /api/stream 将 503）：{_stream_err}',
          flush=True)
RUNS = BASE / 'runs'
RUNNER_PID = RUNS / 'runner.pid'      # 调度器自己写的单实例登记（见 src/instance.py）

sys.path.insert(0, str(BASE))          # 让 `import src.instance` 在源码运行/打包都成立
from src import instance  # noqa: E402  （判活工具：pidfile + os.kill(pid,0)，不依赖 ps）

try:                                   # 队列状态的"已停止"标记（字段格式见 src/queue_status.py）
    from src.queue_status import mark_stopped as mark_queue_stopped  # noqa: E402
except Exception as _qs_err:           # 缺 ruamel/yaml 等极端环境：退化为本地写同一份格式
    print(f'[dashboard] src.queue_status 不可用（停止时改本地写）：{_qs_err}', flush=True)

    def mark_queue_stopped() -> None:
        try:
            (RUNS / 'queue_status.json').write_text(json.dumps({
                'current': '', 'pending': '', 'next': '', 'next_at': '', 'next_ts': 0,
                'ready': 0, 'waiting': 0, 'tasks': {}, 'stopped': True, 'pid': None,
                'updated': datetime.now().strftime('%H:%M:%S')}, ensure_ascii=False),
                encoding='utf-8')
        except OSError:
            pass

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

SW_JS = """// 迁到 React 版后不再需要离线缓存（界面本来就要求联网：数据来自 /api、画面来自 HLS 流）。
// 这个文件留着只为让老客户端上已注册的 Service Worker 能自我清理（清缓存 + 注销）。
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(
  caches.keys()
    .then(ks => Promise.all(ks.map(k => caches.delete(k))))
    .then(() => self.registration.unregister())
    .catch(() => {})
));
self.addEventListener('fetch', e => {
  if (e.request.mode === 'navigate') e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
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
    pids = _runner_pids()
    uptime = ''
    if pids:
        info = instance.read_pidfile(RUNNER_PID)
        if info and info['pid'] == pids[0] and info.get('started'):
            secs = max(0, int(time.time() - float(info['started'])))
            uptime = f'{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}'
        else:
            try:      # 没有 pidfile 的老进程：退回 ps（ps 不可用就算了，不显示运行时长）
                uptime = subprocess.run(['ps', '-o', 'etime=', '-p', str(pids[0])],
                                        capture_output=True, text=True, timeout=5).stdout.strip()
            except Exception:
                pass
    return {'alive': bool(pids), 'pid': pids[0] if pids else None, 'uptime': uptime}


def _runner_pids() -> list[int]:
    """调度器进程 PID 列表 —— **判活只信内核，不调 ps 读命令行**。

    踩过的坑（2026-09-28）：原来用 `pgrep -f` 拿 pid、再 `ps -p <pid> -o command=` 复核，
    可一旦 `ps` 不可用（受限沙箱/权限被拒），复核拿到的命令行是空串 → 所有 pid 被过滤掉 →
    `alive` 恒为 False：① 「已在运行不重复启动」的守卫失效，点一次「启动」就多起一个调度器
    （实测一口气 3 个，同时操作同一部手机）；② 「停止」也不认账（返回"本来就没有在运行"）。
    现在统一走 src/instance.find_running：pidfile（调度器自己写的）+ 内核判活，
    再补一遍 pgrep 兜底（覆盖旧版本起的进程，命令行做严格校验，防 shell 误配）。
    """
    return [r['pid'] for r in instance.find_running(RUNNER_PID)]


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


PAUSE_SIGNAL = RUNS / 'pause.signal'


def scheduler_pause() -> dict:
    """网页接管：写暂停信号，让调度器在安全点让路（不退出进程、pending 与进度全保留）。"""
    try:
        PAUSE_SIGNAL.write_text(str(time.time()), encoding='utf-8')
    except OSError as e:
        return {'ok': False, 'msg': f'写暂停信号失败：{e}'}
    audit('调度器让路（网页接管，来自仪表盘）')
    return {'ok': True, 'msg': '已请求让路（调度器在下一个安全点暂停）',
            'scheduler': scheduler_info()}


def scheduler_resume() -> dict:
    try:
        PAUSE_SIGNAL.unlink(missing_ok=True)
    except OSError as e:
        return {'ok': False, 'msg': f'清除暂停信号失败：{e}'}
    audit('调度器恢复（交还控制，来自仪表盘）')
    return {'ok': True, 'msg': '已交还控制权，调度器继续'}


def scheduler_paused() -> bool:
    """让路是否已生效：信号存在且已过一个响应周期（调度器睡眠切片 1 秒）。"""
    try:
        return time.time() - PAUSE_SIGNAL.stat().st_mtime > 2.0
    except OSError:
        return False


def scheduler_stop() -> dict:
    """停止调度器：先 SIGINT 优雅退出（任务收尾），12 秒不退再 SIGTERM/SIGKILL 兜底。"""
    try:
        PAUSE_SIGNAL.unlink(missing_ok=True)   # 停调度器时清掉暂停信号，避免残留挂住下次启动
    except OSError:
        pass
    pids = _runner_pids()
    if not pids:
        mark_queue_stopped()   # 顺手把可能残留的"进行中"状态标成已停止
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
    # 已停：队列状态标"已停止"，否则界面（本页/手机端）会继续引用上一轮的
    # "上课（进行中）"、日志里那条旧收尾时间也还在
    mark_queue_stopped()
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


#: 调度器"本次启动"的日志标记：`scenarios/runner.py` 的 run_scheduler() 每次启动都会
#: 先打一条 `调度引擎: task_queue|legacy`（在设备连接/启动检查之前），全项目只此一处。
#: **改动 runner 的这行日志时必须同步这里**（tools/test_state_freshness.py 有守卫）。
RUN_START_LOG_RE = re.compile(r'调度引擎:\s*(?:task_queue|legacy)')


def lines_since_runner_start(lines: list[str]) -> list[str]:
    """只保留"最近一次调度器启动之后"的日志行（找不到启动标记就原样返回）。

    重启会丢掉调度器内存里的 pending（"正在上课/打工"的登记），上一轮日志里那条
    "进行中，预计…收尾"就作废了——不截断的话界面会继续显示"上课中 / 剩余 xx 分钟"
    （用户实报："宠物去上课了，我手动召回，停掉调度器再启动，显示还是在上课"）。
    """
    for idx in range(len(lines) - 1, -1, -1):
        if RUN_START_LOG_RE.search(lines[idx]):
            return lines[idx:]
    return lines


def work_eta(lines: list[str]):
    """从日志里找最后一次"进行中"登记，算出剩余秒数 + 场景名（kind）。

    **两种日志格式都要认**（都代表"已登记 pending、先调度其他任务"，只是来源不同）：
      ① `冒险: 进行中，预计 44 秒后结束（…收尾）` —— scenario.defer_busy_end（场景主动延时收尾）
      ② `检测到正在打工，预计 2681 秒后结束（…收尾）` —— scenario.detect_busy_remaining
        （出门预检/被雇佣召回等路径）
    早期只匹配 ①，导致手动切到打工后界面仍停在更早那条"冒险: 进行中"上——
    而那条的结束时间早过了，于是显示"冒险中 / 收尾中"（用户实报的 bug）。

    **只看本次调度器启动之后的登记**（见 lines_since_runner_start）：重启后上一轮的
    登记已经作废，继续展示会让用户以为宠物还在上课/打工。
    """
    m = None
    for ln in lines_since_runner_start(lines)[-400:]:
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
    stop_h = stop_total_hours(sched)   # 合计停止点（学习+打工合计满则两项一起停）

    rows = [
        ['调度策略', strategy],
        ['调度引擎', str(runner.get('engine', 'task_queue'))],
        ['打工', f"{work.get('location', '')} · {work.get('duration', '')} · {n_per_day(work.get('times_per_day'))}"],
        ['金币阈值', f"{sched.get('coin_threshold', '-')}（低于优先打工）"],
        # 合计停止点：学习+打工合计满即两项一起停（不再分"停学习/停打工"两个数，
        # 见设置页「合计停止点与收益档」卡片与 stop_total_hours()）
        ['合计停止点', (f"{stop_h} 小时/天（学习+打工合计满则全停，转冒险）"
                    if stop_h else '不限')],
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
    # 状态曲线上的阈值参考线用配置里的真实值（以前把 60 写死在说明文案里，
    # 用户改了 care.energy_threshold 之后说明就跟实际不符）
    _care_thr = editable_snapshot()
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
        'care_energy': _care_thr.get('care_energy', 60),
        'care_clean': _care_thr.get('care_clean', 60),
    }


SESSION_REWARD_FILE = RUNS / 'session_rewards.jsonl'   # 学习/打工按次结算收益
_ATTR_KEYS = ('力量', '智力', '魅力')


def _reward_sum(items) -> dict:
    """把一批结算记录汇总成 次数/学分/属性/金币/疲惫场次。

    credits_n / coins_n = **解析出数值**的条数：打工结算页字段还没实测，
    金币可能一条都解析不出来，前端要靠它区分"没收益"和"没解析出来"。
    """
    out = {'sessions': 0, 'credits': 0, 'coins': 0, 'tired': 0,
           'credits_n': 0, 'coins_n': 0, 'workpoints': 0, 'workpoints_n': 0,
           'ad_coins': 0, 'ad_coins_n': 0,
           'attrs': {k: 0 for k in _ATTR_KEYS}}
    for r in items:
        out['sessions'] += 1
        if r.get('credits') is not None:
            out['credits'] += int(r['credits'])
            out['credits_n'] += 1
        if r.get('coins') is not None:
            out['coins'] += int(r['coins'])
            out['coins_n'] += 1
        if r.get('workpoints') is not None:
            out['workpoints'] += int(r['workpoints'])
            out['workpoints_n'] += 1
        # 广告加成金币（结算页「看视频获得 N 金币」）：独立一笔，不计进 coins
        if r.get('ad_coins') is not None:
            out['ad_coins'] += int(r['ad_coins'])
            out['ad_coins_n'] += 1
        if r.get('tired'):
            out['tired'] += 1
        for k, v in (r.get('attrs') or {}).items():
            if k in out['attrs']:
                out['attrs'][k] += int(v or 0)
    return out


def rewards_data(sel_date: str | None = None) -> dict:
    """学习/打工每次结算的收益（数据源 runs/session_rewards.jsonl，由
    src/scenario.record_session_reward 在结算页记录）。

    sel_date: 'YYYY-MM-DD' 看指定某天 / 'all' 全部历史 / 空或 'today' = 当天
    （当天还没有记录时回落到最近有记录的一天）。归属哪天优先取结算页自带的
    日期时间（= 这次活动的开始时间，跨零点结算归前一天），识别不到再用 ts。
    """
    rows = []
    try:
        for _line in SESSION_REWARD_FILE.read_text('utf-8').splitlines()[-3000:]:
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
        m = re.search(r'(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})', str(r.get('sig') or ''))
        if m:
            day = f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
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
    recent = []
    for r in view[-300:]:
        attrs = ' '.join(f'{k}+{v}' for k, v in (r.get('attrs') or {}).items() if v)
        # 标题：学习=课程 / 打工=岗位名（结算页"工资明细"里那行"武馆教学助理(10分钟)"），
        # 都没有才回落配置的打工地点
        recent.append([str(r.get('ts') or '')[5:16],
                       r.get('kind') or '',
                       r.get('course') or r.get('job') or r.get('location') or '',
                       r.get('credits'), attrs, r.get('coins'),
                       1 if r.get('tired') else 0,
                       r.get('workpoints'), r.get('pay_detail') or ''])
    school = _reward_sum([r for r in view if r.get('kind') == 'school'])
    work = _reward_sum([r for r in view if r.get('kind') == 'work'])
    today_school = _reward_sum([r for r in rows
                                if r.get('kind') == 'school' and r['_day'] == today])
    today_work = _reward_sum([r for r in rows
                              if r.get('kind') == 'work' and r['_day'] == today])
    # 宠物名 / 主人名（结算页头部，src/scenario.parse_session_reward 解析）：
    # 取当前统计范围内最近一条有值的，用来标明这份数据属于哪只宠物 / 哪个号。
    pet = next((str(r.get('pet')) for r in reversed(view) if r.get('pet')), '')
    owner = next((str(r.get('owner')) for r in reversed(view) if r.get('owner')), '')
    return {
        'ok': True, 'date': sel, 'dates': dates, 'date_n': date_n,
        'today': today, 'yesterday': yesterday, 'all_n': len(rows),
        'n': len(view), 'school': school, 'work': work,
        'today_school': today_school, 'today_work': today_work,
        'recent': recent, 'pet': pet, 'owner': owner,
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


def _int_or_0(v) -> int:
    """配置里的数字字段转 int（None / 空串 / 非数字 → 0）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def stop_total_hours(sched: dict) -> int:
    """设置页「合计满则停止」的当前值 = 学习与打工**一起**停下的那个合计小时数。

    底层仍是三个键（scenarios/runner.py 里各有一处判定，不区分引擎）：
    `daily_hour_limit`（合计满则停学习）/ `work_stop_hours`（满则停打工）/
    `efficiency_tier2_hours`（满则两项全停）。学习在 min(全停, 停学习) 停、
    打工在 min(全停, 停打工) 停，所以「都停」的时刻 = 两者里更晚的那个；
    任一项不限（对应键 0）就永远等不到"都停"，返回 0（不限）。

    设置页只暴露这一个数（保存时三键写同一个值，见 apply_settings），
    这个函数负责把老配置（三项不一致，例如"学满 8h 后继续打 25% 档到 12h"）
    折算成一个能看的数字；前端另有三项不一致的提示。
    """
    def _stop(*vals):
        pos = [v for v in (_int_or_0(x) for x in vals) if v > 0]
        return min(pos) if pos else None
    study = _stop(sched.get('efficiency_tier2_hours'), sched.get('daily_hour_limit'))
    work = _stop(sched.get('efficiency_tier2_hours'), sched.get('work_stop_hours'))
    if study is None or work is None:
        return 0
    return max(study, work)


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
    hf = cfg.get('hire_friend') or {}
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
        # 设置页「合计满则停止」= 唯一入口（stop_total_hours 由下面三个键折算，
        # 保存时三键一起写同一个值）。三个原键仍回填给前端：用于"三项不一致"
        # 提示与主页胶囊的分母，不再单独出现在表单里。
        'stop_total_hours': stop_total_hours(sched),
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
        # 冒险类型（2026-09 新增）：附近走走（约45秒，靠连跑刷次数）/ 诗和远方（约2小时）
        'adventure_type': adv.get('type', '附近走走'),
        'care_energy': care.get('energy_threshold', 60),
        'care_clean': care.get('clean_threshold', 60),
        'care_method': care.get('method', '一键护理'),
        'care_exchange': care.get('exchange_count', 20),
        'friend_care_enabled': bool(fc.get('enabled', False)),
        'friend_care_name': str(fc.get('friend_name') or ''),
        # 时间段（HH:MM-HH:MM；起止相同 = 跨零点 = 全天）
        'friend_care_range': str(fc.get('time_range') or ''),
        'friend_care_interval': fc.get('interval_seconds', 120),
        'friend_care_method': str(fc.get('method') or 'ocr检测'),
        'care_interval': care.get('interval_seconds', 60),
        'hire_friend_enabled': bool(hf.get('enabled', False)),
        # 雇佣好友的"备选目标"：实际优先用 work.hire_name（config 的 _hire_friend_raw 会把它
        # 排到 friend_name 列表最前），这里单独给一份供任务列表显示"雇的是谁"做兜底
        'hire_friend_name': str(hf.get('friend_name') or ''),
        'hire_friend_times': hf.get('times_per_day', 8),
        'employed_enabled': bool(emp.get('enabled', False)),
        'employed_action': str(emp.get('action') or '等到25/75（小于45min）'),
        'employed_interval': emp.get('interval_seconds', 60),
        'gift_bag_enabled': bool((cfg.get('gift_bag') or {}).get('enabled', True)),
        'gift_bag_range': str((cfg.get('gift_bag') or {}).get('time_range') or ''),
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
    # 合计停止点（设置页唯一入口）：学习+打工合计满 N 小时 → 两项一起停（转冒险）。
    # runner 里三个键各有一处判定——daily_hour_limit 停学习、work_stop_hours 停打工、
    # efficiency_tier2_hours 两项全停；设置页只给一个数，所以**三键写同一个值**
    #（只写其中一个会出现"设了 10 小时却 12 小时才停"的错觉）。三项的分开玩法
    # （例如"学满 8h 后继续吃 25% 档打工到 12h"）仍可手改 config.yaml，引擎照旧认。
    stop_total_keys = ('schedule.daily_hour_limit', 'schedule.work_stop_hours',
                       'schedule.efficiency_tier2_hours')
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
        # 合计停止点：映射到 work_stop_hours 只为过 validate_field（0..24 校验），
        # 实际写入见 stop_total_keys 的三键同值分支
        'stop_total_hours': ('schedule.work_stop_hours', 'int'),
        'daily_hour_limit': ('schedule.daily_hour_limit', 'int'),
        'work_stop_hours': ('schedule.work_stop_hours', 'int'),
        'study_quota_hours': ('schedule.study_quota_hours', 'int'),
        'work_quota_hours': ('schedule.work_quota_hours', 'int'),
        # 收益档门槛：**设置页已不给输入框**（游戏机制说明，卡片里只读展示）。
        # 保留映射只为向后兼容旧客户端/手写 API 调用；手改 config.yaml 也能改。
        'efficiency_tier1_hours': ('schedule.efficiency_tier1_hours', 'int'),
        'efficiency_tier2_hours': ('schedule.efficiency_tier2_hours', 'int'),
        'main_order': ('tasks.main_order', None),
        # 任务队列扫描顺序——任务列表拖拽排序写回的就是它。
        # `> `分隔的任务键，src/settings.validate_field 会校验键名合法性
        # （含未知键直接拒绝并回默认），不会把调度器的扫描表写坏。
        'task_order': ('tasks.order', None),
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
        'adventure_type': ('adventure.type', None),
        'care_energy': ('care.energy_threshold', 'int'),
        'care_clean': ('care.clean_threshold', 'int'),
        'care_method': ('care.method', None),
        'care_exchange': ('care.exchange_count', 'int'),
        'friend_care_name': ('friend_care.friend_name', None),
        'friend_care_range': ('friend_care.time_range', 'str'),
        'friend_care_interval': ('friend_care.interval_seconds', 'int'),
        'friend_care_method': ('friend_care.method', None),
        'care_interval': ('care.interval_seconds', 'int'),
        'hire_friend_times': ('hire_friend.times_per_day', 'int'),
        'employed_enabled': ('employed.enabled', 'bool'),
        'employed_action': ('employed.action', None),
        'employed_interval': ('employed.interval_seconds', 'int'),
        'gift_bag_range': ('gift_bag.time_range', 'str'),
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
        if field == 'stop_total_hours':
            # 合计停止点：校验一次（复用 work_stop_hours 的 0..24 规则），
            # 通过后三个键一起写，保证"填多少就多少小时全停"
            ok, fixed = S.validate_field('schedule.work_stop_hours', value)
            if not ok:
                rejected.append(f'{field}: 非法值 {value!r}')
                continue
            fixed = int(fixed)
            for k in stop_total_keys:
                S.set_value(data, k, fixed)
            applied[field] = fixed
            continue
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



def stream_status() -> dict:
    """实时流状态 + 调度器状态（前端据此提示"先接管再操作"）。"""
    if not get_stream:
        return {'running': False, 'error': '流模块不可用'}
    st = get_stream().status()
    st['scheduler_alive'] = bool(scheduler_info().get('alive'))
    st['scheduler_paused'] = scheduler_paused()
    return st


def stream_inject(kind: str, payload: dict) -> dict:
    """网页端注入：接管模式要求调度器已停 —— 否则手动点击会打乱挂机状态机。

    坐标一律用**设备坐标**，换算到视频坐标系由 src/screen_stream.py 负责
    （scrcpy-server 的 PositionMapper 校验 screen_size，不匹配会静默丢弃事件）。
    """
    if not get_stream:
        return {'ok': False, 'msg': '流模块不可用'}
    if scheduler_info().get('alive') and not scheduler_paused():
        result = {'ok': False, 'msg': '调度器正在运行，请点「接管操作」让它让路（不会中断任务）'}
    else:
        try:
            if kind == 'tap':
                get_stream().inject_tap(int(payload.get('x', 0)), int(payload.get('y', 0)))
            else:
                get_stream().inject_swipe(int(payload.get('x1', 0)), int(payload.get('y1', 0)),
                                          int(payload.get('x2', 0)), int(payload.get('y2', 0)))
            result = {'ok': True}
        except Exception as e:                             # noqa: BLE001
            result = {'ok': False, 'msg': f'{type(e).__name__}: {e}'}
    # 记一行日志：网页端"点了没反应"时，据此判断请求到底有没有到服务端
    print(f'[stream {datetime.now():%H:%M:%S}] 网页注入 {kind} {payload} → {result}', flush=True)
    return result


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

    def _send_stream(self, name: str) -> None:
        """实时流分片：首次请求触发设备端采集启动；无人观看由后台看门狗回收。"""
        if not get_stream:
            self._send(503, 'text/plain; charset=utf-8', b'stream module unavailable')
            return
        if not re.fullmatch(r'(index\.m3u8|init\.mp4|seg\d{4}\.m4s)', name):
            self._send(404, 'text/plain', b'not found')      # 白名单，防目录穿越
            return
        if name == 'index.m3u8':
            get_stream().ensure_running()
            deadline = time.time() + 15                      # 等 ffmpeg 产出首个 m3u8
            while time.time() < deadline:
                if (STREAM_DIR / 'index.m3u8').is_file():
                    break
                st = get_stream().status()
                if not st.get('running') and st.get('error'):
                    self._send(503, 'text/plain; charset=utf-8',
                               f"启动失败: {st['error']}".encode('utf-8'))
                    return
                time.sleep(0.3)
        get_stream().touch_access()
        fp = STREAM_DIR / name
        if not fp.is_file():
            self._send(404, 'text/plain', b'not ready')
            return
        data = fp.read_bytes()
        if name == 'index.m3u8':
            # ffmpeg 在分片 <0.5s 时会写出非法的 #EXT-X-TARGETDURATION:0（规范要求正整数），
            # iOS 原生 HLS 播放器会因此拒绝播放（表现为黑屏）—— 这里兜底改成合法值
            text = re.sub(r'#EXT-X-TARGETDURATION:[\d.]+',
                          lambda m: '#EXT-X-TARGETDURATION:%d' % max(
                              1, int(float(m.group(0).split(':')[1]) + 0.999)),
                          data.decode('utf-8', 'replace'))
            data = text.encode('utf-8')
        ctype = 'application/vnd.apple.mpegurl' if name.endswith('.m3u8') else 'video/mp4'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        try:
            if path in ('/', '/index.html'):
                # 新版前端（React + TS）的构建产物。旧版那 4214 行 HTML 常量已随迁移完成删除。
                # `/next` 与 `/assets/` 都走同一个静态目录（见下面的分支），老书签/主屏图标还能用。
                fp = (BASE / 'web' / 'dist' / 'index.html').resolve()
                if fp.is_file():
                    self._send(200, 'text/html; charset=utf-8', fp.read_bytes())
                else:
                    self._send(503, 'text/plain; charset=utf-8',
                               b'web/dist not built: cd web && ./build.sh')
            elif path == '/manifest.json':
                self._send(200, 'application/manifest+json',
                           MANIFEST_JSON.encode('utf-8'))
            elif path == '/sw.js':
                self._send(200, 'application/javascript', SW_JS.encode('utf-8'))
            elif path.startswith('/assets/') or path.startswith('/next'):
                # 新版前端的静态资源（web/dist/）。
                # - `/assets/*` 是 Vite 的产物路径（base = '/'）
                # - `/next*` 是迁移期的兼容别名，老页面/书签仍指着它
                # 构建：cd web && ./build.sh（Vite；需要用户自己装的 node）。
                rel = (path[len('/next'):].lstrip('/') if path.startswith('/next')
                       else path.lstrip('/')) or 'index.html'
                fp = (BASE / 'web' / 'dist' / rel).resolve()
                root = (BASE / 'web' / 'dist').resolve()
                _NCT = {'.html': 'text/html; charset=utf-8',
                        '.js': 'application/javascript',
                        '.css': 'text/css', '.json': 'application/json',
                        '.map': 'application/json', '.png': 'image/png',
                        '.svg': 'image/svg+xml', '.woff2': 'font/woff2'}
                # 与 /qp-icons 同样的归属校验：resolve 后必须仍在 web/dist 内
                if fp.is_file() and fp.is_relative_to(root) and fp.suffix.lower() in _NCT:
                    self._send(200, _NCT[fp.suffix.lower()], fp.read_bytes())
                else:
                    self._send(404, 'text/plain; charset=utf-8',
                               b'not found (build first: cd web && ./build.sh)')
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
            elif path == '/api/rewards':
                q = parse_qs(u.query)
                body = json.dumps(rewards_data((q.get('date') or [''])[0]),
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
            elif path == '/api/stream/status':
                body = json.dumps(stream_status(), ensure_ascii=False).encode('utf-8')
                self._send(200, 'application/json; charset=utf-8', body)
            elif path.startswith('/stream/'):
                self._send_stream(path[len('/stream/'):])
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
            elif u.path in ('/api/runner/pause', '/api/runner/resume'):
                result = scheduler_pause() if u.path.endswith('pause') else scheduler_resume()
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self._send(200 if result.get('ok') else 400,
                           'application/json; charset=utf-8', body)
            elif u.path == '/api/runner/stop':
                result = scheduler_stop()
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self._send(200 if result.get('ok') else 400,
                           'application/json; charset=utf-8', body)
            elif u.path == '/api/stream/ping':
                # 前端心跳：原生 HLS 播放器缓冲够了会暂停拉分片，只靠 /stream/* 请求判断
                # "有没有人在看"会把正在操作的会话误回收（踩过：操作 1 分钟后流被回收）
                if get_stream:
                    get_stream().touch_access()
                if PAUSE_SIGNAL.exists():
                    try:
                        PAUSE_SIGNAL.touch()      # 网页接管期间续期，防止超时自动恢复
                    except OSError:
                        pass
                _dbg = parse_qs(u.query).get('dbg')
                if _dbg:
                    print(f'[stream diag] {_dbg[0]}', flush=True)   # 页面内 MSE 状态回传（远程排查用）
                self._send(200, 'application/json; charset=utf-8', b'{"ok": true}')
            elif u.path == '/api/stream/stop':
                if get_stream:
                    get_stream().stop()
                self._send(200, 'application/json; charset=utf-8', b'{"ok": true}')
            elif u.path in ('/api/stream/tap', '/api/stream/swipe'):
                length = int(self.headers.get('Content-Length') or 0)
                payload = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
                result = stream_inject(u.path.rsplit('/', 1)[-1], payload)
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                self._send(200 if result.get('ok') else 409,
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
