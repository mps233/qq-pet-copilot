import { useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchLogs,
  fetchRewards,
  type LogsData,
  type RewardBucket,
  type RewardRow,
  type RewardsData,
  type Shot,
} from '../api'

const RW_ICON: Record<string, string> = {
  school: '/qp-icons/official/study_book.png',
  work: '/qp-icons/official/work_coin.png',
}

/** 属性点摘要："力量+5 智力+2"（没数据的项不显示） */
const rwAttrs = (a: Record<string, number> | undefined): string =>
  ['力量', '智力', '魅力']
    .filter((k) => a && a[k])
    .map((k) => `${k}+${a?.[k] ?? 0}`)
    .join(' ')

/** 解析不出数值时**明说**，不拿 +0 冒充"这次没收益" */
const miss = (a: number | undefined, b: number | undefined): string =>
  (a || 0) < (b || 0) ? `（${(b || 0) - (a || 0)} 次未解析）` : ''

function sumLine(ico: string, label: string, value: string, detail: string, cls = '') {
  return (
    <div className={'rwline' + (cls ? ` ${cls}` : '')}>
      <img className="rwico" src={ico} alt="" />
      <span className="rwk">{label}</span>
      <span className={'rwv' + (cls ? ` ${cls}` : '')}>{value}</span>
      {detail ? <span className="rwd">{detail}</span> : null}
    </div>
  )
}

function RewardSummary({ d }: { d: RewardsData }) {
  const s: RewardBucket = d.school
  const w: RewardBucket = d.work
  const wc =
    (w.coins_n || 0) > 0
      ? `金币 +${w.coins || 0}${miss(w.coins_n, w.sessions)}`
      : w.sessions
        ? `金币 —${miss(0, w.sessions)}`
        : '—'
  const wpc = (w.workpoints_n || 0) > 0 ? ` · 工分 +${w.workpoints || 0}` : ''
  // 广告加成（结算页「看视频获得 N 金币」）：单独一笔，不计进上面的金币
  const wad = (w.ad_coins_n || 0) > 0 ? ` · 看视频 +${w.ad_coins || 0}` : ''
  const sc = (s.credits_n || 0) > 0 ? s.credits || 0 : null
  const tired = (s.tired || 0) + (w.tired || 0)

  return (
    <div className="rwsum" id="rwSum">
      {sumLine(
        RW_ICON.school ?? '',
        '学习',
        `${s.sessions || 0} 节`,
        (sc === null
          ? s.sessions
            ? `学分 —${miss(0, s.sessions)}`
            : '学分 +0'
          : `学分 +${sc}${miss(s.credits_n, s.sessions)}`) +
          (rwAttrs(s.attrs) ? ` · ${rwAttrs(s.attrs)}` : ''),
        s.tired ? 'tired' : '',
      )}
      {sumLine(RW_ICON.work ?? '', '打工', `${w.sessions || 0} 次`, wc + wpc + wad, w.tired ? 'tired' : '')}
      {tired ? (
        // 疲惫单独一行（"收益减少"是结算页原文）。占位 span 与图标同宽，
        // 否则这行没图标、标签会往左错一整列
        <div className="rwline" title="结算页显示「疲惫，收益减少」的场次">
          <span className="rwphs" />
          <span className="rwk">提示</span>
          <span className="rwv tired">{`疲惫 ${tired} 次·收益减少`}</span>
        </div>
      ) : null}
    </div>
  )
}

function RewardRowItem({ r, tsLab }: { r: RewardRow; tsLab: boolean }) {
  const [ts, kind, title, credits, attrs, coins, tired, wp, pay] = r
  const isS = kind === 'school'
  const val = isS ? (credits != null ? `学分+${credits}` : '—') : coins != null ? `+${coins}` : '—'
  const mid = [title || '', attrs || '', !isS && wp != null ? `工分+${wp}` : '']
    .filter(Boolean)
    .join(' · ')
  return (
    <div className="arow">
      <span className="ai">
        <img src={RW_ICON[kind] ?? ''} alt="" />
        <span className="rk">{tsLab ? ts : (ts || '').slice(6)}</span>
      </span>
      <span className="ag">
        {mid}
        {tired ? (
          <>
            {mid ? ' · ' : ''}
            <span className="tired">疲惫</span>
          </>
        ) : null}
      </span>
      <span
        className={'av' + (coins != null && !isS ? ' pos' : '') + (val === '—' ? ' zero' : '')}
        title={pay || undefined}
      >
        {val}
      </span>
    </div>
  )
}

/** 收益记录子页：统计范围下拉 + 汇总 + 按次记录 */
function RewardTab({ shots }: { shots: Shot[] }) {
  const [d, setD] = useState<RewardsData | null>(null)
  const [date, setDate] = useState<string>('')
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    const run = async () => {
      try {
        const r = await fetchRewards(date || undefined)
        if (alive) {
          setD(r)
          setErr('')
        }
      } catch (e) {
        if (alive) setErr(String(e))
      }
    }
    void run()
    const t = window.setInterval(run, 15000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [date])

  if (err) return <div className="empty">读取收益记录失败：{err}</div>
  if (!d) return <div className="empty">加载中…</div>

  const tsLab = d.date === 'all'
  const rows = [...(d.recent || [])].reverse()
  const scope = d.date === 'all' ? '全部历史' : d.date === d.today ? '今天' : d.date.slice(5).replace('-', '/')
  const tn = `今日 学习 ${d.today_school?.sessions || 0} 节 / 打工 ${d.today_work?.sessions || 0} 次`
  const who = d.pet ? `${d.pet}${d.owner ? ` · ${d.owner}` : ''}` : ''
  const meta = `${who ? `${who} · ` : ''}${scope} · 共 ${d.n || 0} 次 · ${d.date === d.today ? '' : `${tn} · `}更新 ${d.updated || ''}`

  return (
    <>
      {d.ok ? (
        <>
          <div className="pgsec" id="rwScopeSec">
            <div className="pgsec-t">统计范围</div>
            <div className="pgsec-c">
              <div className="logctl rwscope">
                <span id="rwDateRow">
                  <select id="rwDate" value={d.date} onChange={(e) => setDate(e.target.value)}>
                    {(d.dates || []).map((dt) => (
                      <option key={dt} value={dt}>
                        {dt === d.today ? '今天' : dt === d.yesterday ? '昨天' : dt.slice(5).replace('-', '/')}
                      </option>
                    ))}
                    <option value="all">全部</option>
                  </select>
                </span>
                <span className="logfoot" id="rwMeta">
                  {meta}
                </span>
              </div>
            </div>
          </div>
          <div className="pgsec" id="rwSumSec">
            <div className="pgsec-t">收益汇总</div>
            <div className="pgsec-c">
              <RewardSummary d={d} />
            </div>
          </div>
        </>
      ) : null}

      <div className="pgsec">
        <div className="pgsec-t">按次记录</div>
        <div className="pgsec-c">
          <div className="advlist" id="rwList">
            {rows.length ? (
              rows.map((r, i) => <RewardRowItem key={`${r[0]}-${i}`} r={r} tsLab={tsLab} />)
            ) : (
              <div className="empty">
                {d.ok
                  ? '这一天还没有收益记录'
                  : '还没有收益记录。调度器跑完一节学习/一次打工、检测到结算页后就会自动出现在这里（记录从本次更新之后开始）。'}
              </div>
            )}
          </div>
        </div>
      </div>

      {shots.length ? (
        <div className="pgsec" id="shotCard">
          <div className="pgsec-t">异常截图（自动保存）</div>
          <div className="pgsec-c">
            <div className="thumbs" id="shots">
              {shots.map((s) => (
                <a key={s.name} href={`/files/${encodeURIComponent(s.name)}`} target="_blank" rel="noreferrer">
                  <img loading="lazy" src={`/files/${encodeURIComponent(s.name)}`} alt="" />
                  <span className="cap">{s.mtime}</span>
                </a>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </>
  )
}

/** 实时日志子页：工具栏（自动滚动 + 过滤）+ 输出 + 异常截图 */
function LiveTab({ shots }: { shots: Shot[] }) {
  const [logs, setLogs] = useState<LogsData | null>(null)
  const [auto, setAuto] = useState(true)
  const [filter, setFilter] = useState('')
  const boxRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    let alive = true
    const run = async () => {
      try {
        const r = await fetchLogs()
        if (alive) setLogs(r)
      } catch {
        /* 调度器没在跑时日志可能读不到，静默保持上一份 */
      }
    }
    void run()
    const t = window.setInterval(run, 3000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [])

  const text = useMemo(() => {
    const lines = logs?.lines ?? []
    return (filter ? lines.filter((l) => l.includes(filter)) : lines).join('\n')
  }, [logs, filter])

  useEffect(() => {
    if (auto && boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight
  }, [text, auto])

  return (
    <>
      <div className="pgsec">
        <div className="pgsec-t">工具栏</div>
        <div className="pgsec-c">
          <div className="logctl">
            <button className={auto ? 'on' : ''} onClick={() => setAuto((v) => !v)}>
              自动滚动
            </button>
            <input
              placeholder="过滤关键字…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>
        </div>
      </div>
      <div className="pgsec">
        <div className="pgsec-t">输出</div>
        <div className="pgsec-c">
          <pre id="logbox" ref={boxRef}>
            {text || '加载中…'}
          </pre>
          <div className="logfoot" id="logMeta">
            {logs ? `${logs.name} · 共 ${logs.total} 行${filter ? `（已过滤）` : ''}` : ''}
          </div>
        </div>
      </div>
      {shots.length ? (
        <div className="pgsec" id="shotCard">
          <div className="pgsec-t">异常截图（自动保存）</div>
          <div className="pgsec-c">
            <div className="thumbs" id="shots">
              {shots.map((s) => (
                <a key={s.name} href={`/files/${encodeURIComponent(s.name)}`} target="_blank" rel="noreferrer">
                  <img loading="lazy" src={`/files/${encodeURIComponent(s.name)}`} alt="" />
                  <span className="cap">{s.mtime}</span>
                </a>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </>
  )
}

/** 日志页：两个子页（实时日志 / 收益记录），切换条常驻上方。
 *  层级同设置页二级 —— 切子页是一次"同级互切"，不涉及 history 压栈。 */
export function LogPage({ shots }: { shots: Shot[] }) {
  const [sub, setSub] = useState<'index' | 'reward'>('index')
  return (
    <>
      <div className="logctl" id="logSubtabs">
        <button
          data-lsub="index"
          className={sub === 'index' ? 'on' : ''}
          title="调度器全部输出"
          onClick={() => setSub('index')}
        >
          实时日志
        </button>
        <button
          data-lsub="reward"
          className={sub === 'reward' ? 'on' : ''}
          title="每次学习/打工结算拿到的学分·属性·金币"
          onClick={() => setSub('reward')}
        >
          收益记录
        </button>
      </div>
      {sub === 'index' ? <LiveTab shots={shots} /> : <RewardTab shots={shots} />}
    </>
  )
}
