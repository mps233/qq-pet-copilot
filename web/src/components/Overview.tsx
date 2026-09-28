import { useState } from 'react'
import type { Data } from '../api'
import { Capsules } from './Capsules'
import { Deck } from './Deck'
import { FuncBar } from './FuncBar'
import { IdCard } from './IdCard'
import { TaskList } from './TaskList'

export type PageKey = 'main' | 'adv' | 'plan' | 'log' | 'notify' | 'set' | 'shot'

/** 总览页：照 QQ 宠物首页 1:1 还原的绝对定位浮动层。
 *  坐标全部来自官方运行时 dump（见 web/legacy/index.html 顶部的注释），
 *  CSS 里按 `calc(var(--u) * N)` 定位 —— 这里只负责放对 class/id。 */
export function Overview({
  data,
  busy,
  onNav,
  onStart,
  onStop,
}: {
  data: Data
  busy: string
  onNav: (p: PageKey) => void
  onStart: () => void
  onStop: () => void
}) {
  const [petOpen, setPetOpen] = useState(false)
  const sch = data.scheduler

  return (
    <section className="home" data-page="main">
      {/* 左列圆钮（官方 4 个：返回/设置/消息/日记 x=20 y=28/86/146/206）。
          官方第 1 个是"返回"，总览页就是根页面，这里改成「房间背景」（下轮接选择面板）。 */}
      <nav className="flt col-l" id="tabbar">
        <button className="rbtn" id="btnScene" data-tab="main" title="房间背景">
          <img src="/qp-icons/official/off_l1_tshirt.png" alt="" />
        </button>
        <button className="rbtn" data-tab="set" title="设置" onClick={() => onNav('set')}>
          <img src="/qp-icons/official/off_l2_gear.png" alt="" />
        </button>
        <button className="rbtn" data-tab="log" title="日志" onClick={() => onNav('log')}>
          <img src="/qp-icons/official/off_l3_diary.png" alt="" />
        </button>
        <button className="rbtn" data-tab="notify" title="通知" onClick={() => onNav('notify')}>
          <img src="/qp-icons/official/off_l4_bell.png" alt="" />
        </button>
      </nav>

      {/* 右列圆钮（官方 3 个：装扮/会员/盲盒 x=418 y=28/86/146） */}
      <nav className="flt col-r" id="tabbar2">
        <button className="rbtn r" data-tab="adv" title="冒险" onClick={() => onNav('adv')}>
          <img src="/qp-icons/official/cap_compass.png" alt="" />
        </button>
        <button className="rbtn r" data-tab="plan" title="职业" onClick={() => onNav('plan')}>
          <img src="/qp-icons/official/off_r2_briefcase.png" alt="" />
        </button>
        {/* `?v=2` 是换图后的缓存击穿（/qp-icons/* 走 max-age=3600，不换 URL 会拿旧图 1 小时） */}
        <button className="rbtn r" data-tab="shot" title="实时画面" onClick={() => onNav('shot')}>
          <img src="/qp-icons/ctrl/phone.svg?v=2" alt="" />
        </button>
      </nav>

      <IdCard data={data} open={petOpen} onToggle={setPetOpen} />
      <Capsules data={data} />

      {/* 中部场景层（官方是 3D 宠物；这里放任务队列） */}
      <div className="flt scene">
        <TaskList data={data} />
      </div>

      <FuncBar
        alive={sch.alive}
        busy={busy}
        onStart={onStart}
        onStop={onStop}
        onShot={() => onNav('shot')}
      />
      <Deck data={data} />
    </section>
  )
}
