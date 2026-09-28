import { useEffect, useState } from 'react'
import { fetchPlan, savePlan, syncPlan, type PlanData } from '../api'

/** 一条进度条（`.pb > .t + .bar > i`），与 legacy 的 planBar 一致 */
function PlanBar({ t, c, tg, col }: { t: string; c: number; tg: number; col: string }) {
  const w = tg ? Math.max(0, Math.min(100, (c / tg) * 100)) : 0
  return (
    <div className="pb">
      <div className="t">
        <span>{t}</span>
        <span>
          {c} / {tg}
        </span>
      </div>
      <div className="bar">
        <i style={{ width: `${w.toFixed(1)}%`, background: col }} />
      </div>
    </div>
  )
}

const numOf = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
const boolOf = (v: unknown): boolean => !!v

/** 职业解锁计划页：进度条 / 阶梯路线 / 8 线状态 / 哨兵 / 进度录入 */
export function PlanPage() {
  const [d, setD] = useState<PlanData | null>(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  /** 手工录入框里的值（用户正在改时不让轮询覆盖 —— legacy 用 planDirty 做同一件事） */
  const [edit, setEdit] = useState<Record<string, string | boolean>>({})
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState('')

  useEffect(() => {
    let alive = true
    const run = async () => {
      try {
        const r = await fetchPlan()
        if (!alive) return
        setD(r)
        setErr('')
        // 只在没人在改的时候刷新录入框
        if (!dirty) {
          setEdit({
            '力': String(numOf(r.values['力'])),
            '智': String(numOf(r.values['智'])),
            '魅': String(numOf(r.values['魅'])),
            '工分': String(numOf(r.values['工分'])),
            '金币': String(numOf(r.values['金币'])),
            '初级毕业': boolOf(r.values['初级毕业']),
            '中级毕业': boolOf(r.values['中级毕业']),
          })
        }
      } catch (e) {
        if (alive) setErr(String(e))
      }
    }
    void run()
    const t = window.setInterval(run, 20000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [dirty])

  if (err) return <div className="empty">读取职业计划失败：{err}</div>
  if (!d) return <div className="empty">加载中…</div>

  const w = d.watch
  const evs = [...(w.events || [])].reverse()
  let firstOpen = false

  const doSave = async () => {
    setBusy('save')
    try {
      const r = await savePlan({
        ...edit,
        '力': numOf(Number(edit['力'])),
        '智': numOf(Number(edit['智'])),
        '魅': numOf(Number(edit['魅'])),
        '工分': numOf(Number(edit['工分'])),
        '金币': numOf(Number(edit['金币'])),
      })
      setMsg(r.ok ? '已保存' : `保存失败：${r.msg || ''}`)
      setDirty(false)
    } catch (e) {
      setMsg(`保存失败：${String(e)}`)
    } finally {
      setBusy('')
    }
  }

  const doSync = async () => {
    setBusy('sync')
    setMsg('识别中…（会操控手机出门，稍等）')
    try {
      const r = await syncPlan()
      setMsg(r.ok ? `识别完成：${r.msg || ''}` : `识别失败：${r.msg || ''}`)
    } catch (e) {
      setMsg(`识别失败：${String(e)}`)
    } finally {
      setBusy('')
    }
  }

  const setOne = (k: string, v: string | boolean) => {
    setEdit((s) => ({ ...s, [k]: v }))
    setDirty(true)
  }

  return (
    <>
      <div className="advcap" id="planMeta" style={{ marginTop: 'calc(var(--u) * 6)' }}>
        总属性 {d.total}/{d.total_target} · 更新 {d.updated || ''}
      </div>

      <div className="planbars" id="planBars">
        <PlanBar t="属性总进度" c={d.total} tg={d.total_target} col="var(--accent)" />
        <PlanBar t="见习解锁" c={d.jr_n} tg={8} col="#16a34a" />
        <PlanBar t="初级解锁" c={d.ch_n} tg={8} col="#ea580c" />
      </div>

      <div className="subh">
        隐藏职业哨兵{' '}
        <span id="watchMeta" style={{ fontWeight: 400, fontSize: 10.5 }}>
          {w.last_check ? `上次检查 ${String(w.last_check).slice(11, 16)}` : ''}
        </span>
      </div>
      <div className="watchbox" id="watchBox">
        <div className="wstate">
          {!w.enabled
            ? '监控已关闭（设置页「职业」区可开）'
            : !w.alive
              ? '调度器未运行 — 启动后自动监控'
              : `监控中 · ${w.interval ? `每节课后 + 每 ${w.interval} 分钟兜底` : '每节课后'}${w.stop_study ? ' · 解锁后自动停学' : ' · 仅通知'}`}
        </div>
        {evs.length ? (
          evs.map((e, i) => (
            <div className="wrow" key={`${e.ts}-${i}`}>
              <span className="wbadge">
                🎉 {e.career || ''}（见习·{e.name || '?'}）
              </span>
              <span style={{ color: 'var(--sub)', fontSize: 11.5 }}>{String(e.ts || '').slice(5, 16)}</span>
            </div>
          ))
        ) : (
          <div className="wrow" style={{ color: 'var(--sub)' }}>
            <span>尚未解锁（武术家 / 梦境旅人 / 大明星）</span>
            <span />
          </div>
        )}
      </div>

      <div className="subh">进度录入（新号的当前数值，改完点保存）</div>
      <div className="plinedit" id="planEdit">
        {(
          [
            ['力', '力量'],
            ['智', '智力'],
            ['魅', '魅力'],
            ['工分', '工分'],
            ['金币', '金币'],
          ] as const
        ).map(([k, label]) => (
          <label key={k}>
            {label}
            <input
              type="number"
              min={0}
              value={String(edit[k] ?? 0)}
              onChange={(e) => setOne(k, e.target.value)}
            />
          </label>
        ))}
        <div className="planeditrow">
          <span>学园：</span>
          <button
            type="button"
            className={'sw' + (edit['初级毕业'] ? ' on' : '')}
            onClick={() => setOne('初级毕业', !edit['初级毕业'])}
          />
          <span>初级毕业</span>
          <button
            type="button"
            className={'sw' + (edit['中级毕业'] ? ' on' : '')}
            onClick={() => setOne('中级毕业', !edit['中级毕业'])}
          />
          <span>中级毕业</span>
        </div>
      </div>

      <div className="btnrow2">
        <button className="savebtn" disabled={!!busy} onClick={() => void doSave()}>
          {busy === 'save' ? '保存中…' : '保存进度'}
        </button>
        <button className="savebtn ghost" disabled={!!busy} onClick={() => void doSync()}>
          {busy === 'sync' ? '识别中…' : '🔄 自动识别'}
        </button>
      </div>
      <div className="saveMsg" id="planMsg">
        {msg}
      </div>

      <div className="subh">阶梯路线</div>
      <div className="plansteps" id="planSteps">
        {(d.steps || []).map((s, i) => {
          let cls = 'st'
          let dot = '○'
          if (s[2]) {
            cls += ' done'
            dot = '✅'
          } else if (!firstOpen) {
            cls += ' cur'
            dot = '▶'
            firstOpen = true
          }
          return (
            <div className={cls} key={`${s[0]}-${i}`}>
              <span className="dot2">{dot}</span>
              <span className="tx">{`${s[0]} · ${s[1]}`}</span>
              <span className="pr">{s[3]}</span>
            </div>
          )
        })}
      </div>
      <div className="plannote">
        隐藏线解锁有概率性：数值达标只进入候选，实际以职业树实测为准（哨兵每节课后检测）。
      </div>

      <div className="subh">
        8 线解锁状态（见习 / 初级）{' '}
        <span id="planLinesMeta" style={{ fontWeight: 400, fontSize: 10.5 }}>
          {d.lines_meta || ''}
        </span>
      </div>
      <div className="planlines" id="planLines">
        {(d.lines || []).map((l) => (
          <div className="ln" key={l.name}>
            <span>{l.name}</span>
            <span>
              <span className={'chipx' + (l.jr ? ' ok' : '')}>见习</span>
              <span className={'chipx' + (l.ch ? ' ok' : '')}>初级</span>
            </span>
          </div>
        ))}
      </div>
    </>
  )
}
