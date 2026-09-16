"""告警与事件通知：多渠道推送（参考 qq-farm-copilot）。

渠道（config.yaml 的 notify 段配置，可叠加，任一成功即算送达）：
- Windows/macOS 桌面通知：notify.win_toast: true（Windows 需 winotify）
- OnePush 推送：notify.onepush_config 填 YAML（设置页是多行输入框），
  支持 Bark / PushPlus / Server酱 / Telegram / SMTP / 自定义 webhook 等
  onepush 提供方，例如：
    onepush_config: "{provider: bark, key: 你的Key}"
  各提供方参数教程（ALAS wiki 中文文档）：
  https://github.com/LmeSzinc/AzurLaneAutoScript/wiki/Onepush-configuration-%5BCN%5D
- **飞书群机器人**：notify.feishu_enabled + feishu_webhook（+ 可选 feishu_secret 加签）
- **Telegram Bot**：notify.telegram_enabled + telegram_token + telegram_chat_id

飞书/Telegram 用标准库 urllib 直接发（不依赖 onepush），配置项少、设置页可直接填。
告警时可附当前手机屏幕截图（image_path）：Toast 用作图标、飞书/Telegram 直接传图
（Telegram 用 sendPhoto、飞书用图片上传接口；上传失败则降级为纯文本）。
发送失败只记日志不抛异常——通知本身不能再把调度器弄崩。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import NotifyConfig, load_config
from .progress import log

TITLE = '[QQ宠物助手告警]'
# 职业解锁通知的标题（与告警区分，便于在手机上看通知来源）
CAREER_TITLE = '[QQ宠物·职业解锁]'
# 错误/降级通知（"出错了但没崩"类，如配置读取失败后沿用旧配置）
ERROR_TITLE = '[QQ宠物·异常]'
HTTP_TIMEOUT = 20


def send_alert(reason: str, image_path: str | None = None) -> bool:
    """按 notify 配置发送告警，返回是否有渠道发送成功。"""
    return send(TITLE, reason, image_path)


def send_event(title: str, reason: str, image_path: str | None = None) -> bool:
    """发送"事件类"通知（今日完成/配额达成等），受 notify.event_notify 总开关控制。

    与告警区分：告警是出问题要人处理，事件是"按计划完成了"的告知——用户可能
    不想被完成类消息打扰，故单独给一个开关（默认开）。
    """
    try:
        cfg = load_config().notify
    except Exception as e:
        log(f'事件通知: 读取配置失败（{e}），跳过推送')
        return False
    if not getattr(cfg, 'event_notify', True):
        log('事件通知: notify.event_notify 已关闭，跳过推送')
        return False
    return send(title, reason, image_path, cfg=cfg)


# 错误通知限频：按 key（错误类别）记上次发送时间，避免同一错误刷屏。
# 调度器是长驻进程，模块级字典即可（进程重启后计数清零，可接受）。
_ERROR_SENT_AT: dict[str, float] = {}
# 同一 key 最短发送间隔（秒）——默认 30 分钟；同类错误在这期间只推一次
ERROR_NOTIFY_COOLDOWN = 1800


def notify_error(reason: str, key: str = 'default', cooldown: int = ERROR_NOTIFY_COOLDOWN,
                 image_path: str | None = None, log=print) -> bool:
    """发送错误/降级类通知（受 notify.error_notify 开关控制），同一 key 限频。

    用于"出错了但没崩、会静默降级"的场景——用户最需要知道却最容易漏掉，
    例如配置读取失败后一直沿用旧配置（界面改什么都不生效）。
    返回是否真的发出了（被限频/开关关闭/发送失败都算未发出）。
    """
    try:
        cfg = load_config().notify
    except Exception as e:
        log(f'错误通知: 读取配置失败（{e}），跳过推送')
        return False
    if not getattr(cfg, 'error_notify', True):
        return False
    now = time.time()
    last = _ERROR_SENT_AT.get(key)
    if last is not None and (now - last) < max(0, cooldown):
        left = int((max(0, cooldown) - (now - last)) / 60) + 1
        log(f'错误通知: 同类错误（{key}）{left} 分钟内已推送过，本次跳过')
        return False
    ok = send(ERROR_TITLE, reason, image_path, cfg=cfg)
    if ok:
        _ERROR_SENT_AT[key] = now
    return ok


def send_career_unlock(reason: str, image_path: str | None = None) -> bool:
    """职业解锁通知（受 notify.career_notify 开关控制）。"""
    try:
        cfg = load_config().notify
    except Exception as e:
        log(f'职业通知: 读取配置失败（{e}），跳过推送')
        return False
    if not getattr(cfg, 'career_notify', True):
        log('职业通知: notify.career_notify 已关闭，跳过推送')
        return False
    return send(CAREER_TITLE, reason, image_path, cfg=cfg)


def send(title: str, content: str, image_path: str | None = None,
         cfg: NotifyConfig | None = None) -> bool:
    """按 notify 配置把 title+content 发到所有已启用渠道；返回是否有渠道成功。

    cfg 传 None 时自行读配置（配置坏了按默认配置发，尽量把问题报出来）。
    """
    if cfg is None:
        try:
            cfg = load_config().notify
        except Exception as e:
            log(f'通知: 读取配置失败（{e}），按默认配置发送')
            cfg = NotifyConfig()
    try:
        sent = False
        if getattr(cfg, 'feishu_enabled', False) and str(cfg.feishu_webhook).strip():
            sent = _send_feishu(cfg, title, content, image_path) or sent
        if getattr(cfg, 'telegram_enabled', False) and str(cfg.telegram_token).strip():
            sent = _send_telegram(cfg, title, content, image_path) or sent
        if cfg.win_toast:
            # win_toast 兼作“桌面通知”开关：Windows 用 Toast，macOS 用 osascript 通知
            if sys.platform == 'darwin':
                sent = _send_mac_toast(f'{title} {content}') or sent
            else:
                sent = _send_windows_toast(f'{title} {content}', image_path) or sent
        if str(cfg.onepush_config).strip():
            sent = _send_onepush(str(cfg.onepush_config), f'{title} {content}',
                                 image_path) or sent
        if not sent:
            log('通知: 未发送成功（未启用渠道、配置不全或发送失败，详见上方日志）')
        return sent
    except Exception as e:
        # 通知本身绝不能再把调度器弄崩：任何异常都记日志后返回失败
        log(f'通知: 发送过程异常: {e}')
        return False


# ---------------------------------------------------------------- 飞书群机器人

def _feishu_sign(secret: str, timestamp: int) -> str:
    """飞书加签：以 "{timestamp}\\n{secret}" 为**密钥**、空串为消息做 HMAC-SHA256。

    注意是拿 timestamp+secret 当 key（不是拿 secret 当 key、timestamp 当消息）——
    飞书文档的写法容易看反，实测按文档示例算才对得上。
    """
    key = f'{timestamp}\n{secret}'.encode('utf-8')
    return base64.b64encode(hmac.new(key, b'', digestmod=hashlib.sha256).digest()).decode()


def _post_json(url: str, payload: dict) -> tuple[bool, str]:
    """POST JSON，返回 (是否成功, 说明)。飞书/Telegram 都用它，统一错误处理。"""
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = Request(url, data=body, method='POST',
                  headers={'Content-Type': 'application/json; charset=utf-8'})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        data = json.loads(raw) if raw.strip() else {}
    except HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', errors='replace')[:200]
        except Exception:
            pass
        return False, f'HTTP {e.code} {detail}'
    except URLError as e:
        return False, f'网络错误 {e.reason}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'
    # 飞书：code==0 成功；Telegram：ok==true 成功
    if isinstance(data, dict):
        if data.get('code') not in (None, 0):
            return False, f"code={data.get('code')} {data.get('msg') or ''}".strip()
        if data.get('ok') is False:
            return False, str(data.get('description') or 'ok=false')
    return True, 'ok'


def _feishu_upload_image(webhook: str, image_path: str) -> str | None:
    """把图片传到飞书拿 image_key（发图必须先上传）。失败返回 None。

    飞书没有公开的"上传图片"开放接口给自定义机器人，但群机器人收发图走
    im/v1/images。这里用 multipart 手工拼包（避免依赖 requests）。
    """
    url = 'https://open.feishu.cn/open-apis/im/v1/images'
    boundary = '----QQPetCopilot' + uuid.uuid4().hex
    try:
        with open(image_path, 'rb') as f:
            data = f.read()
    except OSError as e:
        log(f'飞书通知: 读取截图失败: {e}')
        return None
    ctype = mimetypes.guess_type(image_path)[0] or 'image/png'
    name = os.path.basename(image_path)
    parts = []
    parts.append(f'--{boundary}\r\n'.encode())
    parts.append(b'Content-Disposition: form-data; name="image_type"\r\n\r\n')
    parts.append(b'message\r\n')
    parts.append(f'--{boundary}\r\n'.encode())
    parts.append(
        f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'.encode())
    parts.append(f'Content-Type: {ctype}\r\n\r\n'.encode())
    parts.append(data)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    body = b''.join(parts)
    req = Request(url, data=body, method='POST', headers={
        'Content-Type': f'multipart/form-data; boundary={boundary}',
    })
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode('utf-8', errors='replace') or '{}')
        if payload.get('code') == 0:
            return ((payload.get('data') or {}).get('image_key')) or None
        log(f"飞书通知: 图片上传失败 code={payload.get('code')} "
            f"{payload.get('msg') or ''}".strip())
    except Exception as e:
        log(f'飞书通知: 图片上传失败: {type(e).__name__}: {e}')
    return None


def _send_feishu(cfg: NotifyConfig, title: str, content: str,
                 image_path: str | None = None) -> bool:
    """发送飞书群机器人消息。有截图先传图（image 消息），否则发文本。

    飞书自定义机器人支持的 msg_type：text / post / image / interactive / share_chat。
    """
    webhook = str(cfg.feishu_webhook).strip()
    if not webhook:
        log('飞书通知: 未填 webhook，跳过')
        return False
    payload: dict = {}
    secret = str(getattr(cfg, 'feishu_secret', '') or '').strip()
    if secret:
        ts = int(time.time())
        payload['timestamp'] = str(ts)
        payload['sign'] = _feishu_sign(secret, ts)

    text = f'{title}\n{content}'
    # 有截图：先传图，成功则发一条图片 + 一条文本（图单独发，文本带完整信息）
    sent_any = False
    if image_path and os.path.exists(image_path):
        key = _feishu_upload_image(webhook, image_path)
        if key:
            ok, why = _post_json(webhook, {**payload, 'msg_type': 'image',
                                           'content': {'image_key': key}})
            if ok:
                sent_any = True
            else:
                log(f'飞书通知: 图片消息发送失败: {why}')
        else:
            log('飞书通知: 图片上传未成功，仅发文本')
    ok, why = _post_json(webhook, {**payload, 'msg_type': 'text',
                                   'content': {'text': text}})
    if ok:
        log('飞书通知: 发送成功')
        return True
    log(f'飞书通知: 发送失败: {why}')
    return sent_any


# ---------------------------------------------------------------- Telegram Bot

def _send_telegram(cfg: NotifyConfig, title: str, content: str,
                   image_path: str | None = None) -> bool:
    """发送 Telegram 消息（有截图用 sendPhoto，否则 sendMessage）。"""
    token = str(cfg.telegram_token).strip()
    chat_id = str(cfg.telegram_chat_id).strip()
    if not token or not chat_id:
        log('Telegram 通知: token 或 chat_id 未填，跳过')
        return False
    text = f'{title}\n{content}'
    if image_path and os.path.exists(image_path):
        ok, why = _telegram_send_photo(token, chat_id, text, image_path)
        if ok:
            log('Telegram 通知: 发送成功（图片）')
            return True
        log(f'Telegram 通知: 图片发送失败（{why}），降级为文本')
    base = f'https://api.telegram.org/bot{token}'
    ok, why = _post_json(f'{base}/sendMessage',
                         {'chat_id': chat_id, 'text': text,
                          'disable_web_page_preview': True})
    if ok:
        log('Telegram 通知: 发送成功')
        return True
    log(f'Telegram 通知: 发送失败: {why}')
    return False


def _telegram_send_photo(token: str, chat_id: str, caption: str,
                         image_path: str) -> tuple[bool, str]:
    """multipart 上传图片 + caption（标准库手工拼包，不依赖 requests）。"""
    boundary = '----QQPetCopilot' + uuid.uuid4().hex
    try:
        with open(image_path, 'rb') as f:
            data = f.read()
    except OSError as e:
        return False, f'读取截图失败 {e}'
    ctype = mimetypes.guess_type(image_path)[0] or 'image/png'
    name = os.path.basename(image_path)
    # caption 上限 1024 字符，超了截断（避免整条发送失败）
    cap = caption if len(caption) <= 1000 else caption[:1000] + '…'
    parts = []
    for k, v in (('chat_id', chat_id), ('caption', cap)):
        parts.append(f'--{boundary}\r\n'.encode())
        parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        parts.append(v.encode('utf-8') + b'\r\n')
    parts.append(f'--{boundary}\r\n'.encode())
    parts.append(
        f'Content-Disposition: form-data; name="photo"; filename="{name}"\r\n'.encode())
    parts.append(f'Content-Type: {ctype}\r\n\r\n'.encode())
    parts.append(data)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    req = Request(f'https://api.telegram.org/bot{token}/sendPhoto',
                  data=b''.join(parts), method='POST',
                  headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode('utf-8', errors='replace') or '{}')
    except HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', errors='replace')[:200]
        except Exception:
            pass
        return False, f'HTTP {e.code} {detail}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'
    if isinstance(payload, dict) and payload.get('ok'):
        return True, 'ok'
    return False, str((payload or {}).get('description') or 'ok=false')


def test_notify(target: str = 'all') -> dict:
    """发送一条测试通知（设置页「测试通知」按钮用），返回每个渠道的结果。

    target: all / feishu / telegram —— 便于只测其中一个渠道。
    """
    try:
        cfg = load_config().notify
    except Exception as e:
        return {'ok': False, 'msg': f'读取配置失败: {e}', 'results': {}}
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    text = f'这是一条测试通知（{stamp}）。收到说明配置正确。'
    results: dict[str, str] = {}
    ok_any = False
    if target in ('all', 'feishu'):
        if not str(cfg.feishu_webhook).strip():
            results['飞书'] = '未填 webhook'
        else:
            ok = _send_feishu(cfg, '[测试] QQ宠物助手', text)
            results['飞书'] = '成功' if ok else '失败（详见日志）'
            ok_any = ok_any or ok
    if target in ('all', 'telegram'):
        if not (str(cfg.telegram_token).strip() and str(cfg.telegram_chat_id).strip()):
            results['Telegram'] = '未填 token 或 chat_id'
        else:
            ok = _send_telegram(cfg, '[测试] QQ宠物助手', text)
            results['Telegram'] = '成功' if ok else '失败（详见日志）'
            ok_any = ok_any or ok
    if target == 'all':
        summary = '、'.join(f'{k}:{v}' for k, v in results.items()) or '无可测渠道'
    else:
        summary = '、'.join(results.values())
    return {'ok': ok_any, 'msg': summary, 'results': results}



def _send_windows_toast(reason: str, image_path: str | None = None) -> bool:
    """发送 Windows Toast 通知。"""
    if not sys.platform.startswith('win'):
        log('告警通知: 非 Windows 平台，跳过 Toast')
        return False
    try:
        from winotify import Notification
    except ImportError:
        log('告警通知: 未安装 winotify，跳过 Windows Toast')
        return False
    except Exception as e:
        log(f'告警通知: winotify 导入失败: {e}，跳过 Windows Toast')
        return False
    icon = str(image_path or '')
    if not icon or not os.path.exists(icon):
        icon = ''
    try:
        toast = Notification(
            app_id='QQPetCopilot', title=TITLE, msg=str(reason), duration='long',
            **({'icon': icon} if icon else {}),
        )
        toast.show()
        log('告警通知: Windows Toast 推送成功')
        return True
    except Exception as e:
        log(f'告警通知: Windows Toast 发送失败: {e}')
        return False


def _send_mac_toast(reason: str) -> bool:
    """发送 macOS 桌面通知（osascript display notification）。"""
    if sys.platform != 'darwin':
        return False
    try:
        msg = str(reason).replace('"', "'").replace(chr(10), ' ').replace(chr(13), ' ')
        script = f'display notification "{msg}" with title "{TITLE}"'
        proc = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            log('告警通知: macOS 通知推送成功')
            return True
        log(f'告警通知: macOS 通知发送失败: {proc.stderr.strip()}')
        return False
    except Exception as e:
        log(f'告警通知: macOS 通知发送失败: {e}')
        return False


def _send_onepush(config_text: str, reason: str, image_path: str | None = None) -> bool:
    """发送 OnePush 通知。config_text 为单行 YAML（flow 格式），必须含 provider。"""
    import yaml

    try:
        cfg = yaml.safe_load(config_text.strip())
    except yaml.YAMLError as e:
        log(f'告警通知: OnePush 配置 YAML 解析失败: {e}')
        return False
    if not isinstance(cfg, dict):
        log('告警通知: OnePush 配置不是字典，跳过推送')
        return False
    cfg = dict(cfg)
    provider = str(cfg.pop('provider', '') or '').strip()
    if not provider:
        log('告警通知: OnePush 未配置 provider，跳过推送')
        return False
    try:
        from onepush import get_notifier
        from onepush.providers.custom import Custom
    except ImportError:
        log('告警通知: 未安装 onepush，跳过 OnePush 推送')
        return False
    except Exception as e:
        log(f'告警通知: onepush 导入失败: {e}，跳过 OnePush 推送')
        return False
    try:
        notifier = get_notifier(provider)
        payload: dict = dict(cfg)
        payload['title'] = TITLE
        payload['content'] = str(reason)
        if image_path and os.path.exists(image_path):
            payload['image_path'] = image_path
        if isinstance(notifier, Custom):
            # 自定义 webhook：默认 JSON POST，title/content 固定塞进 data
            if str(payload.get('method', 'post')).lower() == 'post':
                payload['datatype'] = 'json'
            data = payload.get('data')
            if not isinstance(data, dict):
                data = {}
            data['title'] = payload['title']
            data['content'] = payload['content']
            payload['data'] = data
        response = notifier.notify(**payload)
        status_code = int(getattr(response, 'status_code', 200) or 200)
        if status_code != 200:
            log(f'告警通知: OnePush 推送失败，状态码={status_code}')
            return False
        log(f'告警通知: OnePush 推送成功（{provider}）')
        return True
    except Exception as e:
        detail = str(e).strip() or repr(e)
        log(f'告警通知: OnePush 推送失败（{provider}）: {type(e).__name__}: {detail}')
        return False
