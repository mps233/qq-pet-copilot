import { useEffect, useState } from 'react'
import { fetchData, TASK_NAME, type Data } from './api'

/** 迁移期第一版：只验证「React 能拿到与旧界面相同的数据」。
 *  跑通后再按旧界面的视觉逐块搬（胶囊行 / 任务列表 / 状态卡）。 */
export default function App() {
  const [data, setData] = useState<Data | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const d = await fetchData()
        if (alive) {
          setData(d)
          setErr('')
        }
      } catch (e) {
        if (alive) setErr(String(e))
      }
    }
    void tick()
    const t = window.setInterval(tick, 6000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [])

  if (err) return <pre style={{ padding: 16 }}>连接失败：{err}</pre>
  if (!data) return <div style={{ padding: 16 }}>加载中…</div>

  const loopKeys = Object.keys(data.queue.tasks).filter((k) => TASK_NAME[k])

  return (
    <div style={{ padding: 16, fontFamily: 'system-ui, sans-serif', lineHeight: 1.9 }}>
      <h2 style={{ margin: '0 0 8px' }}>QQ 宠物托管 · React 版</h2>
      <div style={{ opacity: 0.7, fontSize: 13 }}>迁移中 —— 数据链路已通，界面逐块搬</div>

      <hr />

      <div>
        <b>调度器</b>：
        {data.scheduler.alive ? `运行中 · 已跑 ${data.scheduler.uptime}` : '已停止'}
        {data.scheduler.pid ? `（PID ${data.scheduler.pid}）` : ''}
      </div>
      <div>
        <b>宠物</b>：{data.status.pet_name} · 金币 {data.status.coins} · 体力{' '}
        {data.status.energy} · 清洁 {data.status.clean} · 心情 {data.status.mood}
      </div>
      <div>
        <b>队列</b>：当前 {data.queue.current || '—'} · 待结算 {data.queue.pending || '—'} · 下一项{' '}
        {data.queue.next || '—'} {data.queue.next_at}
      </div>
      <div>
        <b>节奏</b>：可执行 {data.queue.ready} · 等待中 {data.queue.waiting}
      </div>

      <hr />

      <div>
        <b>任务状态</b>（{loopKeys.length} 项）
      </div>
      <table style={{ borderCollapse: 'collapse', fontSize: 14 }}>
        <tbody>
          {loopKeys.map((k) => (
            <tr key={k}>
              <td style={{ padding: '2px 12px 2px 0' }}>{TASK_NAME[k]}</td>
              <td style={{ padding: '2px 12px 2px 0', opacity: 0.75 }}>
                {data.queue.tasks[k]?.state ?? '—'}
              </td>
              <td style={{ opacity: 0.6 }}>{data.queue.tasks[k]?.next ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
