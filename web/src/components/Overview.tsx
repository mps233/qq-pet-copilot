import { useRef, useState } from 'react'
import type { Data } from '../api'
import type { useScene } from '../lib/useScene'
import { BgSheet } from './BgSheet'
import { Capsules } from './Capsules'
import { Deck } from './Deck'
import { FuncBar } from './FuncBar'
import { IdCard } from './IdCard'
import { TaskList } from './TaskList'

export type PageKey = 'main' | 'adv' | 'plan' | 'log' | 'notify' | 'set' | 'shot'

/** 总览页：照 QQ 宠物首页 1:1 还原的绝对定位浮动层。
 *  坐标全部来自官方运行时 dump（见 web/legacy/index.html 顶部注释），
 *  CSS 按 `calc(var(--u) * N)` 定位 —— 这里只负责放对 class/id。 */
export function Overview({
  data,
  busy,
  scene,
  onNav,
  onStart,
  onStop,
  onReorder,
}: {
  data: Data
  busy: string
  scene: ReturnType<typeof useScene>
  onNav: (p: PageKey) => void
  onStart: () => void
  onStop: () => void
  onReorder: (order: string[]) => void
}) {
  const [petOpen, setPetOpen] = useState(false)
  const [bgOpen, setBgOpen] = useState(false)
  const sch = data.scheduler

  // 左上角圆钮：**点按 = 开背景选择面板**、**长按 500ms = 直接切下一张**。
  // 官方那个位置是"返回"，但总览页就是根页面（点了永远早退，曾是置灰死键），
  // 所以改成换背景。长按用定时器实现：500ms 内抬手算点击。
  const holdTimer = useRef<number>()
  const longPressed = useRef(false)
  const sceneBtnHandlers = {
    onPointerDown: () => {
      longPressed.current = false
      holdTimer.current = window.setTimeout(() => {
        longPressed.current = true
        scene.cycle()
      }, 500)
    },
    onPointerUp: () => {
      window.clearTimeout(holdTimer.current)
      if (!longPressed.current) setBgOpen(true)
    },
    onPointerLeave: () => window.clearTimeout(holdTimer.current),
    onPointerCancel: () => window.clearTimeout(holdTimer.current),
  }

  return (
    <section className="home" data-page="main">
      {/* 左列圆钮（官方 4 个：返回/设置/消息/日记 x=20 y=36.3/94.3/154.3/214.3） */}
      <nav className="flt col-l" id="tabbar">
        <button className="rbtn" id="btnScene" data-tab="main" title="房间背景（点按选择 / 长按换下一张）" {...sceneBtnHandlers}>
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

      {/* 右列圆钮（官方 3 个：装扮/会员/盲盒 x=298 y=36.3/94.3/154.3，官方手机版） */}
      <nav className="flt col-r" id="tabbar2">
        <button className="rbtn r" data-tab="adv" title="冒险" onClick={() => onNav('adv')}>
          <img src="/qp-icons/official/cap_compass.png" alt="" />
        </button>
        <button className="rbtn r" data-tab="plan" title="职业" onClick={() => onNav('plan')}>
          <img src="/qp-icons/official/off_r2_briefcase.png" alt="" />
        </button>
        {/* `?v=2` 是换图后的缓存击穿（/qp-icons/* 走 max-age=3600，不换 URL 拿旧图 1 小时） */}
        <button className="rbtn r" data-tab="shot" title="实时画面" onClick={() => onNav('shot')}>
          <img src="/qp-icons/ctrl/phone.svg?v=2" alt="" />
        </button>
      </nav>

      <IdCard data={data} open={petOpen} onToggle={setPetOpen} />
      <Capsules data={data} />

      {/* 中部场景层（官方是 3D 宠物；这里放任务队列） */}
      <div className="flt scene">
        <TaskList data={data} onReorder={onReorder} />
      </div>

      <FuncBar
        alive={sch.alive}
        busy={busy}
        onStart={onStart}
        onStop={onStop}
        onShot={() => onNav('shot')}
      />
      <Deck data={data} />

      {/* 切换房间背景的轻提示 */}
      <div id="sceneToast" className={scene.toast ? 'on' : ''}>
        {scene.toast}
      </div>

      <BgSheet
        open={bgOpen}
        manual={scene.manual}
        dark={scene.dark}
        onSelect={(k) => scene.select(k)}
        onClose={() => setBgOpen(false)}
      />
    </section>
  )
}
