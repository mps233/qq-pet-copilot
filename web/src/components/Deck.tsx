import type { Data } from '../api'
import { hms, pad2 } from '../lib/format'

/** 状态图标：跟着"正在做的事"换图标。
 *  **文件名必须与 legacy 的 RUNICON 表逐字一致** —— 这些是官方素材、按名字放在
 *  `/qp-icons/official/` 下，名字写错就是 404、界面上显示成破图/问号
 *  （踩过：我编了 book_pencil.png / earth.png，实际叫 study_book.png / globe.png）。
 *  改这张表前先逐个验：`curl -o /dev/null -w "%{http_code}" http://127.0.0.1:8787/qp-icons/official/<name>.png` */
const ICON: Record<string, string> = {
  上课: '/qp-icons/official/study_book.png',
  学习: '/qp-icons/official/study_book.png',
  打工: '/qp-icons/official/work_coin.png',
  雇佣打工: '/qp-icons/official/work_coin.png',
  雇佣好友: '/qp-icons/official/work_coin.png',
  被雇佣检查: '/qp-icons/official/work_coin.png',
  冒险: '/qp-icons/official/cap_compass.png',
  护理: '/qp-icons/official/soap.png',
  好友护理: '/qp-icons/official/soap.png',
  踩踩: '/qp-icons/official/cap_paw.png',
  PK: '/qp-icons/official/pk_words.png',
  福袋: '/qp-icons/official/cap_coin.png',
}
/** 等待中/已停止：地球（legacy 的 RUNICON_DEFAULT —— 文件名是 globe 不是 earth） */
const ICON_IDLE = '/qp-icons/official/globe.png'

/** 底部抽屉（官方 x=215 y=728）：当前在做什么 + 倒计时 + 今日统计 */
export function Deck({ data }: { data: Data }) {
  const sch = data.scheduler
  const eta = data.work_eta
  const td = data.today_duration
  const kind = eta?.kind ?? ''
  const remain = eta?.remaining ?? null
  const running = sch.alive

  const icon = running && kind ? (ICON[kind] ?? ICON_IDLE) : ICON_IDLE
  const state = !running ? '未托管' : kind ? `${kind}中` : '空闲'
  const hint = running && remain != null ? `剩余 ${hms(remain)}` : ''
  const sub =
    running && kind && remain != null
      ? `剩余 ${hms(remain)} · 结束后自动开启下一项`
      : running
        ? '等待下一项任务'
        : '调度器未运行 · 点右侧「启动」'
  const stat = td
    ? `今日：学习 ${td.learn_min} 分 · 打工 ${td.work_min} 分 · 合计 ${td.total_min} 分 · 效率 ${td.eff_pct}%`
    : ''

  return (
    <div className="flt deck">
      <div className="flt drawer" id="runnerCard">
        <img
          className="dico"
          id="runnerIcon"
          src={icon}
          alt=""
          /* 已停止时灰度压暗，避免"没在跑却亮着"的误读（同 legacy 的 setRunIcon） */
          style={running ? undefined : { filter: 'grayscale(1) opacity(.55)' }}
        />
        <div className="dcol">
          <div className="dtitle">
            <span className={'dot' + (running ? ' on' : ' off')} id="runnerDot" />
            <span id="runnerState">{state}</span>
            <span className="dhint" id="runnerHint">
              {hint}
            </span>
          </div>
          <div className="dsub" id="workSub">
            {sub}
          </div>
          <div className="dsub" id="runnerSub">
            {stat}
          </div>
          <div className="saveMsg" id="runnerMsg" />
        </div>
        <span className="dmeta" id="runnerMeta">
          {running ? `PID ${sch.pid}${sch.uptime ? ` · 已跑 ${sch.uptime}` : ''}` : '未运行'}
        </span>
      </div>
    </div>
  )
}

/** 顶部时钟（legacy 把当前时间显示在资料卡右侧，这里保留为纯函数备用） */
export const clockNow = (d = new Date()): string => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
