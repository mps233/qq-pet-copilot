import type { Data, PetStatus } from '../api'
import { petVal } from '../lib/format'

/** 三行状态：图标 + 名称 + 数值 + 进度条 + ›
 *  （› 在官方是"进详情页"，本工具没有那些页面，只作视觉还原，CSS 里 pointer-events:none） */
const PET_STATS: ReadonlyArray<readonly [string, string, string]> = [
  ['energy', '体力', 'e'],
  ['clean', '清洁', 'c'],
  ['mood', '心情', 'm'],
]

/** 三层同心弧：外=体力(蓝) / 中=清洁(绿) / 内=心情(橙)，每弧按 0~100 画。
 *  用 stroke-dashoffset 控制进度（与 legacy 的 setRingArc 一致）。 */
function ringStyle(v: number | null, r: number): React.CSSProperties {
  const C = 2 * Math.PI * r
  const p = v === null ? 0 : Math.max(0, Math.min(100, v))
  return {
    strokeDasharray: C.toFixed(2),
    strokeDashoffset: (C * (1 - p / 100)).toFixed(2),
  }
}

export function IdCard({
  data,
  open,
  onToggle,
}: {
  data: Data
  open: boolean
  onToggle: (next: boolean) => void
}) {
  const st: PetStatus = data.status
  const sch = data.scheduler
  const v = PET_STATS.map(([k]) => petVal(st as unknown as Record<string, unknown>, k))

  return (
    <div className={'flt idcard' + (open ? ' open' : '')} id="idCard">
      <div className="idtop">
        <span className={'avatar' + (sch.alive ? '' : ' off')} id="schedDot">
          <img src="/qp-icons/official/mood_smile.png" alt="" />
        </span>
        <div className="idtxt">
          <div className="idname">QQ宠物托管</div>
          <div className="idsub" id="schedTxt">
            {sch.alive ? `运行中 · 已跑 ${sch.uptime || ''}` : '未运行'}
          </div>
        </div>
        <button
          className="idring"
          id="idRing"
          type="button"
          aria-label="宠物状态（体力/清洁/心情）"
          title={`体力 ${v[0] ?? '--'} · 清洁 ${v[1] ?? '--'} · 心情 ${v[2] ?? '--'}（点击展开/收起）`}
          onClick={(e) => {
            e.preventDefault()
            e.stopPropagation()
            onToggle(!open)
          }}
        >
          <svg viewBox="0 0 40 40" aria-hidden="true">
            <circle className="trk" cx="20" cy="20" r="18.1" />
            <circle className="arc e" cx="20" cy="20" r="18.1" style={ringStyle(v[0] ?? null, 18.1)} />
            <circle className="trk" cx="20" cy="20" r="12.5" />
            <circle className="arc c" cx="20" cy="20" r="12.5" style={ringStyle(v[1] ?? null, 12.5)} />
            <circle className="trk" cx="20" cy="20" r="7.05" />
            <circle className="arc m" cx="20" cy="20" r="7.05" style={ringStyle(v[2] ?? null, 7.05)} />
          </svg>
        </button>
      </div>

      {/* 展开区：高度由 CSS 从 0 动画长出来（.idrows） */}
      <div className="idrows">
        <div id="petRows">
          {PET_STATS.map(([k, name, cls], i) => {
            const val = v[i] ?? null
            const p = val === null ? 0 : Math.max(0, Math.min(100, val))
            return (
              <div className="prow" key={k}>
                <img className="pico" src={`/qp-icons/official/status_${k}.png`} alt="" />
                <span className="pname">{name}</span>
                <span className="pval">{val === null ? '--' : val}</span>
                <span className="pbar">
                  <i className={cls} style={{ width: `${p}%` }} />
                </span>
                <span className="pchev">›</span>
              </div>
            )
          })}
        </div>
        <button
          className="idfold"
          id="idFold"
          type="button"
          aria-label="收起"
          onClick={(e) => {
            e.preventDefault()
            e.stopPropagation()
            onToggle(false)
          }}
        >
          <svg viewBox="0 0 24 12" aria-hidden="true">
            <path d="M3 9.5 L12 2.5 L21 9.5" />
          </svg>
        </button>
      </div>
    </div>
  )
}
