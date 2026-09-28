import { useState } from 'react'
import { testNotify } from '../api'
import { FGroup, FRow, NoteRow, Switch } from './form'

/** `editable` 是个松散的 Record<string, unknown>，取值统一走这两个小工具 */
const boolOf = (ed: Record<string, unknown>, k: string): boolean => !!ed[k]
const strOf = (ed: Record<string, unknown>, k: string): string => {
  const v = ed[k]
  return v === null || v === undefined ? '' : String(v)
}

/** 通知页：飞书 / Telegram / 事件开关 / 异常提醒 + 配置说明。
 *  开关点一下**立即保存**（与设置页同一套「改动即自动保存」），文本框失焦时保存。 */
export function NotifyPage({
  editable,
  onSave,
}: {
  editable: Record<string, unknown>
  onSave: (updates: Record<string, unknown>) => Promise<void>
}) {
  // 本地即时态：保存是异步的，先乐观更新避免开关"点一下弹回去"
  const [optimistic, setOptimistic] = useState<Record<string, boolean>>({})
  const [testMsg, setTestMsg] = useState('')

  const on = (k: string) => optimistic[k] ?? boolOf(editable, k)
  const toggle = (k: string) => {
    const next = !on(k)
    setOptimistic((s) => ({ ...s, [k]: next }))
    void onSave({ [k]: next })
  }

  const sendTest = async () => {
    setTestMsg('发送中…')
    try {
      const r = await testNotify()
      setTestMsg(r.ok ? '已发送（看手机/群里的消息）' : `失败：${r.msg || '未知错误'}`)
    } catch (e) {
      setTestMsg(`失败：${String(e)}`)
    }
  }

  return (
    <div className="form" id="notifyForm">
      <FGroup title="飞书群机器人">
        <FRow k="启用">
          <Switch on={on('notify_feishu_enabled')} onToggle={() => toggle('notify_feishu_enabled')} title="开=用飞书自定义机器人推送" />
        </FRow>
        <FRow k="webhook">
          <input
            type="text"
            style={{ width: '100%' }}
            placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…"
            defaultValue={strOf(editable, 'notify_feishu_webhook')}
            onBlur={(e) => void onSave({ notify_feishu_webhook: e.target.value })}
          />
        </FRow>
        <FRow k="加签密钥">
          <input
            type="text"
            placeholder="安全设置选「签名校验」时必填，否则留空"
            defaultValue={strOf(editable, 'notify_feishu_secret')}
            onBlur={(e) => void onSave({ notify_feishu_secret: e.target.value })}
          />
        </FRow>
      </FGroup>

      <FGroup title="Telegram Bot">
        <FRow k="启用">
          <Switch on={on('notify_telegram_enabled')} onToggle={() => toggle('notify_telegram_enabled')} title="开=用 Telegram Bot 推送" />
        </FRow>
        <FRow k="Bot Token">
          <input
            type="text"
            style={{ width: '100%' }}
            placeholder="123456789:AAE…（@BotFather 获取）"
            defaultValue={strOf(editable, 'notify_telegram_token')}
            onBlur={(e) => void onSave({ notify_telegram_token: e.target.value })}
          />
        </FRow>
        <FRow k="Chat ID">
          <input
            type="text"
            placeholder="私聊填数字 id；群/频道填 -100…"
            defaultValue={strOf(editable, 'notify_telegram_chat_id')}
            onBlur={(e) => void onSave({ notify_telegram_chat_id: e.target.value })}
          />
        </FRow>
      </FGroup>

      <FGroup title="推送哪些事件">
        <FRow k="今日配额达成">
          <Switch
            on={on('notify_quota_done')}
            onToggle={() => toggle('notify_quota_done')}
            title="开=当天学习/打工打满你设的配额时推送（含当前截图）"
          />
        </FRow>
        <FRow k="隐藏职业解锁">
          <Switch
            on={on('notify_career')}
            onToggle={() => toggle('notify_career')}
            title="开=武术家/梦境旅人/大明星解锁时推送（含职业树截图）"
          />
        </FRow>
        <FRow k="完成类通知总开关">
          <Switch
            on={on('notify_event_notify')}
            onToggle={() => toggle('notify_event_notify')}
            title="关掉后所有「完成」类通知（如配额达成）都不发；任务失败告警不受影响"
          />
        </FRow>
      </FGroup>

      <FGroup title="异常提醒">
        <FRow k="异常降级提醒">
          <Switch
            on={on('notify_error_notify')}
            onToggle={() => toggle('notify_error_notify')}
            title="开=出现「没崩但静默降级」的错误时推送，同类错误 30 分钟内最多一条"
          />
        </FRow>
        <FRow k="" block>
          <span style={{ color: 'var(--sub)', fontSize: 12, lineHeight: 1.6 }}>
            任务失败告警（学习/打工反复失败后退出调度器）
            <b>始终会发</b>，不受本页开关影响；这里的开关只控制「完成通知」与「异常降级提醒」。
          </span>
        </FRow>
      </FGroup>

      <FGroup title="测试与说明">
        <FRow k="测试">
          <span className="ctrl">
            <span id="notifyTestMsg">{testMsg}</span>
            <button className="minibtn" type="button" onClick={() => void sendTest()}>
              发送测试通知
            </button>
          </span>
        </FRow>
        <NoteRow t="飞书">
          群 → 右上角设置 → 群机器人 → 添加机器人 → 自定义机器人，复制 webhook 地址；安全设置选「签名校验」就把密钥填到加签密钥（选「自定义关键词」可留空，关键词需含"QQ宠物"）。
        </NoteRow>
        <NoteRow t="Telegram">
         跟 @BotFather 发 /newbot 建机器人拿 Token；<b>先给机器人发一条消息</b>，再用 @userinfobot 查自己的 Chat ID（群/频道是 -100 开头的负数）。
        </NoteRow>
        <NoteRow t="告警">任务失败告警始终会发（不受上面开关影响）；职业解锁与配额达成各有一个开关。</NoteRow>
      </FGroup>
    </div>
  )
}
