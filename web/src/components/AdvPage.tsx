import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchAdventure, type AdvData } from '../api'

/** 图表几何：与 legacy 的 drawAdv 逐项一致（viewBox 340×84，内边距 8） */
const W = 340
const H = 84
const PAD = 8

type ChartKey = 'cum' | 'pts' | 'stats'

/** 横坐标：按索引 1..tx 均匀铺开 */
const sx = (i: number, tx: number): number =>
  PAD + ((Math.max(1, i) - 1) / Math.max(1, tx - 1)) * (W - 2 * PAD)
/** 收益类纵坐标（围绕中线 42 上下各 34） */
const syMoney = (v: number, mx: number): number => 42 - (v / mx) * 34
/** 状态类纵坐标（0~100 铺满） */
const syState = (v: number): number =>
  H - PAD - (Math.max(0, Math.min(100, v)) / 100) * (H - 2 * PAD)

const fmtNet = (v: number): string => (v > 0 ? `+${v}` : String(v))

export function AdvPage() {
  const [d, setD] = useState<AdvData | null>(null)
  const [err, setErr] = useState('')
  const [date, setDate] = useState('')
  const [sel, setSel] = useState<{ chart: ChartKey; k: number } | null>(null)
  const [showAll, setShowAll] = useState(true)
  const downRef = useRef<ChartKey | null>(null)

  useEffect(() => {
    let alive = true
    const run = async () => {
      try {
        const r = await fetchAdventure(date || undefined)
        if (alive) {
          setD(r)
          setErr('')
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
  }, [date])

  /** 点/拖动图表 → 找最近的索引并把读数写进提示行 */
  const pick = useCallback(
    (chart: ChartKey, e: React.PointerEvent<SVGSVGElement>, data: AdvData) => {
      const rect = e.currentTarget.getBoundingClientRect()
      const x = (e.clientX - rect.left) * (W / Math.max(1, rect.width))
      const arr = chart === 'stats' ? (data.stats || []).map((r) => r[0]) : (data.pts || []).map((p) => p[0])
      let best: number | null = null
      let bd = Infinity
      for (const i of arr) {
        const dd = Math.abs(sx(i, data.n || 100) - x)
        if (dd < bd) {
          bd = dd
          best = i
        }
      }
      if (best != null) setSel({ chart, k: best })
    },
    [],
  )

  const bind = (chart: ChartKey, data: AdvData) => ({
    onPointerDown: (e: React.PointerEvent<SVGSVGElement>) => {
      downRef.current = chart
      pick(chart, e, data)
    },
    onPointerMove: (e: React.PointerEvent<SVGSVGElement>) => {
      if (downRef.current === chart) pick(chart, e, data)
    },
    onPointerUp: () => {
      downRef.current = null
    },
    onPointerCancel: () => {
      downRef.current = null
    },
    onPointerLeave: () => {
      downRef.current = null
    },
  })

  if (err) return <div className="empty">读取冒险记录失败：{err}</div>
  if (!d) return <div className="empty">加载中…</div>
  if (!d.ok) return <div className="empty">暂无数据</div>

  const tx = d.n || 100
  const cum = d.cum || []
  const pts = d.pts || []
  const stats = d.stats || []
  const recent = d.recent || []
  const hasStats = stats.length > 0

  // 记录索引 → 时间/增益（提示行用）
  const tm: Record<number, string> = {}
  const gn: Record<number, string> = {}
  for (const r of recent) {
    tm[r[0]] = r[1] || ''
    gn[r[0]] = r[3] || ''
  }
  const cumMap: Record<number, number> = {}
  for (const p of cum) cumMap[p[0]] = p[1]
  const dlMap: Record<number, number> = {}
  for (const p of pts) dlMap[p[0]] = p[1]

  // ---- 提示行 ----
  let tip: React.ReactNode = '点 / 拖动图表上的点或线，查看当次数据'
  if (sel) {
    const k = sel.k
    if (sel.chart === 'stats') {
      const r = stats.filter((x) => x[0] === k).sort((a, b) => b[4] - a[4])[0]
      if (r) {
        tip = (
          <>
            #{k}
            {tm[k] ? ` ${tm[k].slice(0, 5)}` : ''} · 体力 <b>{r[1] ?? '-'}</b> · 清洁 <b>{r[2] ?? '-'}</b> ·
            心情 <b>{r[3] ?? '-'}</b>
            {r[4] === 1 ? '（护理后）' : ''}
          </>
        )
      }
    } else {
      const v = dlMap[k]
      tip = (
        <>
          #{k}
          {tm[k] ? ` ${tm[k].slice(0, 5)}` : ''} · 累计 {fmtNet(cumMap[k] ?? 0)} · 本趟{' '}
          {v == null ? '?' : fmtNet(v)}
          {gn[k] ? ` · ${gn[k]}` : ''}
        </>
      )
    }
  }

  // ---- 明细列表 ----
  let rows = [...recent].reverse().slice(0, 400)
  if (!showAll) rows = rows.filter((r) => r[2] !== 0 || r[4])

  const meta =
    d.date === 'all'
      ? `共 ${d.n} 把 · 今日 ${d.today_n || 0} 把（${fmtNet(d.today_net || 0)}） · 更新 ${d.updated || ''}`
      : d.date === d.today
        ? `共 ${d.n} 把 · 更新 ${d.updated || ''}`
        : `${(d.date || '').slice(5).replace('-', '月')}日 · 共 ${d.n} 把 · 更新 ${d.updated || ''}`

  return (
    <>
      <div className="navmeta" id="advMeta" style={{ fontWeight: 400, fontSize: 10.5, display: 'block', marginTop: 4 }}>
        {meta}
      </div>

      <div className="advcap" id="advDateRow" style={{ margin: '-2px 0 4px' }}>
        统计范围{' '}
        <select
          value={d.date}
          style={{ font: 'inherit', padding: '1px 4px' }}
          onChange={(e) => {
            setSel(null)
            setDate(e.target.value)
          }}
        >
          {(d.dates || []).map((dt) => (
            <option key={dt} value={dt}>
              {dt === d.today ? '今天' : dt === d.yesterday ? '昨天' : dt.slice(5).replace('-', '/')}
            </option>
          ))}
          <option value="all">全部</option>
        </select>
      </div>

      <div className="workline">
        <span className="big" id="advNet" style={{ color: d.net > 0 ? 'var(--gold)' : d.net < 0 ? '#dc2626' : undefined }}>
          {fmtNet(d.net)}
        </span>
        <span className="hint" id="advNetHint">
          金币收益合计（结算页口径） · 平均 {d.avg > 0 ? '+' : ''}
          {d.avg}/把
        </span>
      </div>
      <div className="subline" id="advSub">
        有收益 {d.win} 把 · 零收益 {d.zero} 把
      </div>

      <div className="advchips" id="advChips">
        {(d.gains || []).map((g) => (
          <span className="chip" key={g[0]}>
            {g[0]} +{g[2]} ×{g[1]}
          </span>
        ))}
      </div>

      <div className="advtip" id="advTip">
        {tip}
      </div>

      <div className="advcap">累计收益曲线（金币）</div>
      <svg className="advchart" viewBox="0 0 340 84" {...bind('cum', d)}>
        <line x1="0" y1="42" x2={W} y2="42" stroke="#e2e5ec" strokeWidth="1" strokeDasharray="4 4" />
        {(() => {
          const ys = cum.map((p) => p[1])
          const mx = Math.max(10, ...ys.map((v) => Math.abs(v))) * 1.15
          return (
            <>
              {cum.length > 1 ? (
                <polyline
                  points={cum.map((p) => `${sx(p[0], tx).toFixed(1)},${syMoney(p[1], mx).toFixed(1)}`).join(' ')}
                  fill="none"
                  stroke="#ea580c"
                  strokeWidth="2"
                  strokeLinejoin="round"
                />
              ) : null}
              {cum.length ? (
                <circle
                  cx={sx(cum[cum.length - 1]![0], tx).toFixed(1)}
                  cy={syMoney(cum[cum.length - 1]![1], mx).toFixed(1)}
                  r="3"
                  fill="#ea580c"
                />
              ) : null}
              {sel?.chart === 'cum' && sel.k in cumMap ? (
                <>
                  <line
                    x1={sx(sel.k, tx).toFixed(1)}
                    y1="4"
                    x2={sx(sel.k, tx).toFixed(1)}
                    y2="80"
                    stroke="#94a3b8"
                    strokeWidth="1"
                    strokeDasharray="3 3"
                  />
                  <circle
                    cx={sx(sel.k, tx).toFixed(1)}
                    cy={syMoney(cumMap[sel.k] ?? 0, mx).toFixed(1)}
                    r="4"
                    fill="#ea580c"
                    stroke="#fff"
                    strokeWidth="1.5"
                  />
                </>
              ) : null}
            </>
          )
        })()}
      </svg>

      <div className="advcap">单次收益散点（橙虚线=平均）</div>
      <svg className="advchart" viewBox="0 0 340 84" {...bind('pts', d)}>
        <line x1="0" y1="42" x2={W} y2="42" stroke="#e2e5ec" strokeWidth="1" strokeDasharray="4 4" />
        {(() => {
          const ys = pts.map((p) => p[1])
          const mx = Math.max(10, ...ys.map((v) => Math.abs(v))) * 1.2
          return (
            <>
              {d.avg != null ? (
                <line
                  x1="0"
                  y1={syMoney(d.avg, mx).toFixed(1)}
                  x2={W}
                  y2={syMoney(d.avg, mx).toFixed(1)}
                  stroke="#f59e0b"
                  strokeWidth="1"
                  strokeDasharray="5 4"
                />
              ) : null}
              {pts.map((p) => (
                <circle
                  key={p[0]}
                  cx={sx(p[0], tx).toFixed(1)}
                  cy={syMoney(p[1], mx).toFixed(1)}
                  r="2.2"
                  fill="#0ea5e9"
                  opacity=".85"
                />
              ))}
              {sel?.chart === 'pts' && sel.k in dlMap ? (
                <>
                  <line
                    x1={sx(sel.k, tx).toFixed(1)}
                    y1="4"
                    x2={sx(sel.k, tx).toFixed(1)}
                    y2="80"
                    stroke="#94a3b8"
                    strokeWidth="1"
                    strokeDasharray="3 3"
                  />
                  <circle
                    cx={sx(sel.k, tx).toFixed(1)}
                    cy={syMoney(dlMap[sel.k] ?? 0, mx).toFixed(1)}
                    r="4"
                    fill="#0ea5e9"
                    stroke="#fff"
                    strokeWidth="1.5"
                  />
                </>
              ) : null}
            </>
          )
        })()}
      </svg>

      {hasStats ? (
        <>
          <div className="advcap" id="capStats">
            <span style={{ color: '#16a34a' }}>体力</span> / <span style={{ color: '#0891b2' }}>清洁</span> /{' '}
            <span style={{ color: '#d97706' }}>心情</span>（红虚线=阈值
            <span id="capThr">
              {d.care_energy == null ? '--' : String(d.care_energy)}
              {d.care_clean != null && d.care_clean !== d.care_energy ? `/${d.care_clean}` : ''}
            </span>
            ，红竖线=护理）
          </div>
          <svg className="advchart" viewBox="0 0 340 84" {...bind('stats', d)}>
            <line
              x1="0"
              y1={syState(d.care_energy ?? 60).toFixed(1)}
              x2={W}
              y2={syState(d.care_energy ?? 60).toFixed(1)}
              stroke="#ef4444"
              strokeWidth="1"
              strokeDasharray="5 4"
              opacity=".7"
            />
            {(
              [
                [1, '#16a34a'],
                [2, '#0891b2'],
                [3, '#d97706'],
              ] as const
            ).map(([idx, col]) => {
              const rows2 = stats.filter((r) => r[idx] != null)
              if (rows2.length < 2) return null
              return (
                <g key={idx}>
                  <polyline
                    points={rows2
                      .map((r) => `${sx(r[0], tx).toFixed(1)},${syState(r[idx] ?? 0).toFixed(1)}`)
                      .join(' ')}
                    fill="none"
                    stroke={col}
                    strokeWidth="1.8"
                  />
                  <circle
                    cx={sx(rows2[rows2.length - 1]![0], tx).toFixed(1)}
                    cy={syState(rows2[rows2.length - 1]![idx] ?? 0).toFixed(1)}
                    r="2.6"
                    fill={col}
                  />
                </g>
              )
            })}
            {stats
              .filter((r) => r[4] === 1)
              .map((r) => (
                <line
                  key={`care-${r[0]}`}
                  x1={sx(r[0], tx).toFixed(1)}
                  y1={PAD}
                  x2={sx(r[0], tx).toFixed(1)}
                  y2={H - PAD}
                  stroke="#ef4444"
                  strokeWidth="1"
                  strokeDasharray="2 3"
                  opacity=".6"
                />
              ))}
          </svg>
        </>
      ) : null}

      <div className="advcap">
        逐次明细（新→旧）{' '}
        <button
          className={'minibtn' + (showAll ? ' on' : '')}
          style={{ float: 'right', marginTop: -2 }}
          onClick={() => setShowAll((v) => !v)}
        >
          {showAll ? '全部' : '仅变化'}
        </button>
      </div>
      <div className="advlist" id="advList">
        {rows.length ? (
          rows.map((r) => {
            const v = r[2]
            const cls = v != null && v > 0 ? 'pos' : v != null && v < 0 ? 'neg' : 'zero'
            const vt = v == null ? '?' : v > 0 ? `+${v}` : String(v)
            return (
              <div className="arow" key={`${r[0]}-${r[1]}`}>
                <span className="ai">
                  #{r[0]} {(r[1] || '').slice(0, 11)}
                </span>
                <span className="ag">
                  {r[3] || ''}
                  {r[4] ? (
                    <>
                      {r[3] ? ' ' : ''}
                      <span style={{ color: '#dc2626' }}>扣费{r[4]}</span>
                    </>
                  ) : null}
                </span>
                <span className={`av ${cls}`}>{vt}</span>
              </div>
            )
          })
        ) : (
          <div className="arow">
            <span className="ag">暂无记录</span>
          </div>
        )}
      </div>
    </>
  )
}
