#!/usr/bin/env python3
"""QQ 宠物托管 · 手机仪表盘（纯标准库，零依赖）。

读取 runs/ 下的日志、进度、状态、队列文件，提供手机端页面：

    GET /             手机页面（自动刷新）
    GET /api/data     汇总数据 JSON
    GET /api/logs     实时日志尾部 JSON（?tail=250）
    GET /files/<png>  异常截图

启动:  python3 dashboard.py [--port 8787]
访问:  http://<本机内网IP>:8787
"""

import io
import json
import re
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
    """最后一条“今日时长: 已学习 X 分钟 + 已打工 Y 分钟”的 X/Y。"""
    m = None
    for ln in lines[-400:]:
        mm = re.search(r'今日时长: 已学习 (\d+) 分钟 \+ 已打工 (\d+) 分钟', ln)
        if mm:
            m = mm
    if not m:
        return None
    return {'learn_min': int(m.group(1)), 'work_min': int(m.group(2))}


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
    return {
        'school_enabled': bool((tasks.get('school') or {}).get('enabled', True)),
        'work_location': work.get('location'),
        'work_locations': locations,
        'work_duration': work.get('duration'),
        'hire_name': str(work.get('hire_name') or ''),
        'coin_threshold': sched.get('coin_threshold', 2000),
        'daily_hour_limit': sched.get('daily_hour_limit', 8),
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
:root{--bg:#f6f7f9;--card:#fff;--line:#e6e8ee;--text:#111827;--sub:#6b7280;--accent:#533afd;--ok:#16a34a;--warn:#b45309}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%;overflow-x:hidden}
header{position:sticky;top:0;z-index:10;background:rgba(246,247,249,.9);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:10px 14px;display:flex;justify-content:space-between;align-items:center;padding-top:calc(10px + env(safe-area-inset-top))}
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
.hide{display:none!important}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot" id="schedDot"></span>QQ宠物托管 <span style="font-weight:400;color:var(--sub);font-size:12px" id="schedTxt"></span></div>
  <div class="meta" id="clock">--:--:--</div>
</header>
<main>
  <section class="card" id="workCard">
    <h2>打工循环</h2>
    <div class="workline"><span class="big" id="workBig">--</span><span class="hint" id="workHint"></span></div>
    <div class="subline" id="workSub"></div>
  </section>

  <section class="grid">
    <div class="tile"><div class="v" id="coins">--</div><div class="k">金币 <span id="coinsAt" style="opacity:.75"></span></div></div>
    <div class="tile"><div class="v" id="visitTxt">--</div><div class="k">今日踩踩</div><div class="bar"><i id="visitBar"></i></div></div>
    <div class="tile"><div class="v" id="pkTxt">--</div><div class="k">今日PK</div><div class="bar"><i id="pkBar"></i></div></div>
    <div class="tile"><div class="v" id="advTxt">--</div><div class="k">今日冒险</div></div>
    <div class="tile"><div class="v" id="workCnt">--</div><div class="k">今日打工(次)</div></div>
    <div class="tile"><div class="v" id="expTxt">--</div><div class="k">经验日常</div></div>
  </section>

  <section class="duo">
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

  <section class="card">
    <h2>设置 <span style="font-weight:400;color:var(--sub)">保存后下一轮调度生效</span></h2>
    <div class="form" id="setForm"></div>
    <button class="savebtn" id="btnSave">保存设置</button>
    <div class="saveMsg" id="saveMsg"></div>
    <div class="subh">运行信息（只读）</div>
    <div class="cfg" id="cfgList"></div>
  </section>

  <section class="card">
    <h2>实时日志 <span id="logMeta" style="font-weight:400"></span></h2>
    <div class="logctl">
      <button id="btnAuto" class="on">自动滚动</button>
      <input id="logFilter" placeholder="过滤关键字…">
    </div>
    <pre id="logbox">加载中…</pre>
  </section>

  <section class="card hide" id="shotCard">
    <h2>异常截图（自动保存）</h2>
    <div class="thumbs" id="shots"></div>
  </section>
</main>
<footer>
  <div id="footStrategy"></div>
  <div>设置保存后下一轮生效 · 日志 3s / 数据 6s / 截图 15s</div>
</footer>

<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const TASKNAME={care:'护理',school:'学习',friend_care:'好友护理',hire_friend:'雇佣好友',adventure:'冒险',visit:'踩踩',pk:'PK',work:'打工'};
let etaRemain=null, etaClock='';
let logAuto=true, logFilter='';
try{ logAuto = localStorage.getItem('qpet_logAuto')!=='0'; }catch(e){}

function pad(n){return String(n).padStart(2,'0')}
function hms(sec){sec=Math.max(0,Math.floor(sec));const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;return (h?h+':':'')+pad(m)+':'+pad(s)}

async function j(u){const r=await fetch(u,{cache:'no-store'});if(!r.ok)throw new Error(r.status);return await r.json()}

function renderData(d){
  // 头部
  const dot=$('#schedDot');
  dot.className='dot '+(d.scheduler.alive?'on':'off');
  $('#schedTxt').textContent=d.scheduler.alive?('运行中 · 已跑 '+(d.scheduler.uptime||'')):'未运行';
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
    $('#workSub').textContent=wd?('今日：学习 '+wd.learn_min+' 分 · 打工 '+wd.work_min+' 分'):'';
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
  $('#qTop').textContent='待执行 '+(q.ready??'--')+' · 等待中 '+(q.waiting??'--')+(q.next?(' · 下一个：'+(TASKNAME[q.next]||q.next)+' '+(q.next_at||'')):'');
  $('#qUpd').textContent=q.updated?('更新 '+q.updated):'';
  let rows='';
  for(const [k,v] of Object.entries(qt)){
    const stt=v.state||'';
    const chip= stt==='ready'?'<span class="chip ready">可执行</span>'
              : stt==='waiting'?'<span class="chip wait">等待</span>'
              : stt==='disabled'?'<span class="chip off">已禁用</span>'
              : '<span class="chip">'+stt+'</span>';
    const nx=v.next?('→ '+v.next.slice(11)):'';
    rows+='<div class="row"><div class="t"><span>'+(TASKNAME[k]||k)+'</span>'+chip+'</div><div class="nx">'+nx+'</div></div>';
  }
  if(q.pending) rows+='<div class="row"><div class="t"><span>收尾队列</span><span class="chip ready">'+q.pending+' 待结算</span></div><div class="nx"></div></div>';
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
  $('#setForm').innerHTML=
    '<div class="frow"><span class="k">只打工不学习</span><button class="sw'+(ed.school_enabled?'':' on')+'" id="swSchool" title="开=只打工；关=学习+打工"></button></div>'+
    '<div class="frow"><span class="k">打工地点</span>'+sel('selLoc', ed.work_locations||[], ed.work_location)+'</div>'+
    '<div class="frow"><span class="k">打工时长</span>'+sel('selDur', ['10分钟','45分钟','2小时'], ed.work_duration)+'</div>'+
    '<div class="frow"><span class="k">优先雇佣</span><input type="text" id="txtHire" placeholder="宠物名/主人名，空=最上面" value="'+esc(ed.hire_name||'')+'"></div>'+
    '<div class="frow"><span class="k">金币阈值</span><input type="number" id="numCoin" min="0" step="100" value="'+(ed.coin_threshold??'')+'"></div>'+
    '<div class="frow"><span class="k">时长上限（小时）</span><input type="number" id="numHour" min="0" step="1" value="'+(ed.daily_hour_limit??'')+'"></div>'+
    '<div class="frow"><span class="k">踩踩次数/天</span><input type="number" id="numVisit" min="0" step="1" value="'+(ed.visit_times??'')+'"></div>'+
    '<div class="frow"><span class="k">PK 次数/天</span><input type="number" id="numPk" min="0" step="1" value="'+(ed.pk_times??'')+'"></div>'+
    '<div class="frow"><span class="k">PK 只打</span><input type="text" id="txtPkOnly" placeholder="昵称或宠物名，逗号分隔，空=不限" value="'+esc(ed.pk_only||'')+'"></div>'+
    '<div class="frow"><span class="k">PK 跳过</span><input type="text" id="txtPkSkip" placeholder="昵称或宠物名，逗号分隔，空=不跳过" value="'+esc(ed.pk_skip||'')+'"></div>'+
    '<div class="frow"><span class="k">PK 打手</span><input type="text" id="txtPkHelper" placeholder="只雇这些宠物代打（逗号分隔，按优先序）" value="'+esc(ed.pk_helper||'')+'"></div>'+
    '<div class="frow"><span class="k">PK 等级上限</span><input type="number" id="numPkLv" min="-1" step="1" title="-1 = 只打等级比我低的" value="'+(ed.pk_max_level??0)+'"></div>'+
    '<div class="frow"><span class="k">冒险次数/天</span><input type="number" id="numAdv" min="0" step="1" value="'+(ed.adventure_times??'')+'"></div>'+
    '<div class="frow"><span class="k">护理阈值（体力/清洁）</span><span class="two"><input type="number" id="numEnergy" min="0" max="100" value="'+(ed.care_energy??'')+'"><input type="number" id="numClean" min="0" max="100" value="'+(ed.care_clean??'')+'"></span></div>'+
    '<div class="frow"><span class="k">护理方式</span>'+sel('selCare', ['一键护理','ocr检测'], ed.care_method)+'</div>';
  $('#swSchool').onclick=()=>{ $('#swSchool').classList.toggle('on'); setDirty=true; };
}

async function saveSettings(){
  if(!setInit) return;
  const btn=$('#btnSave'); btn.disabled=true;
  const msg=$('#saveMsg');
  const updates={};
  const schoolEnabledNew = !$('#swSchool').classList.contains('on');
  if(!!schoolEnabledNew !== !!setInit.school_enabled) updates.school_enabled=schoolEnabledNew;
  const getv=id=>($(id)?$(id).value.trim():'');
  const num=(id,key)=>{const v=getv(id); if(v==='')return; const n=parseInt(v,10); if(!isNaN(n)&&n!==setInit[key]) updates[key]=n;};
  const selc=(id,key)=>{const v=getv(id); if(v&&v!==setInit[key]) updates[key]=v;};
  const txtc=(id,key)=>{const v=getv(id); if(v!==(setInit[key]||'')) updates[key]=v;};
  selc('#selLoc','work_location'); selc('#selDur','work_duration'); selc('#selCare','care_method');
  txtc('#txtHire','hire_name');
  num('#numCoin','coin_threshold'); num('#numHour','daily_hour_limit');
  num('#numVisit','visit_times'); num('#numPk','pk_times'); num('#numAdv','adventure_times');
  txtc('#txtPkOnly','pk_only'); txtc('#txtPkSkip','pk_skip'); num('#numPkLv','pk_max_level'); txtc('#txtPkHelper','pk_helper');
  num('#numEnergy','care_energy'); num('#numClean','care_clean');
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

setInterval(()=>{if(!document.hidden)refreshLogs()},3000);
setInterval(()=>{if(!document.hidden)refreshData()},6000);
setInterval(()=>{if(!document.hidden)refreshShot(false)},15000);
refreshData();refreshLogs();refreshShot(false);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){refreshData();refreshLogs();refreshShot(false)}});
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
