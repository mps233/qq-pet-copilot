import { useEffect, useRef } from 'react'

/** 右功能栏（官方 x=414 y=335/405/495 各 50×70）：启动 / 停止 / 画面
 *
 *  **top 是动态算出来的**，不能只靠 CSS 里那个默认值：功能栏的底端要对齐
 *  **任务列表的底端**（列表高度随任务数变化，9 项时约 227dp，固定 top 不是压住
 *  底栏就是留一大片空）。这段逻辑对应 legacy app.js 里的 `place()`。
 *  踩过的坑（都照搬注释）：
 *  - 列表还没内容时别摆位，否则量到"标题下面一点点"、命中兜底位置 → 先隐藏功能栏
 *  - **0.4u 迟滞**：6 秒一次的刷新会让列表底端有亚像素级变化，每次都跟着改 top
 *    就是用户看到的"偶尔上下抽动"
 *  - 量两次（下一帧 + 150ms 兜底）：行重建那一帧量到的可能是中间态，位置会先跳后回 */
export function FuncBar({
  alive,
  busy,
  /** 任务列表的签名（如各任务键拼起来）—— 变化时重新摆位 */
  placeKey,
  onStart,
  onStop,
  onShot,
}: {
  alive: boolean
  busy: string
  placeKey: string
  onStart: () => void
  onStop: () => void
  onShot: () => void
}) {
  const fbRef = useRef<HTMLDivElement>(null)
  const lastTop = useRef<number | null>(null)

  useEffect(() => {
    const fb = fbRef.current
    const tl = document.getElementById('taskList')
    const home = document.querySelector('.home') as HTMLElement | null
    if (!fb || !tl || !home) return

    const place = () => {
      if (!tl.children.length) {
        fb.style.visibility = 'hidden'
        return
      }
      fb.style.visibility = ''
      const vu = getComputedStyle(home).getPropertyValue('--vu').trim()
      const u = parseFloat(vu) || home.clientWidth / 360
      const hr = home.getBoundingClientRect()
      const tr = tl.getBoundingClientRect()
      const want = (tr.bottom - hr.top) / u // 期望：功能栏底端 = 列表底端
      const h2 = fb.getBoundingClientRect().height / u
      let top = want - h2
      if (top < 100) top = 319.3 // 列表过短时回官方位置
      if (lastTop.current !== null && Math.abs(top - lastTop.current) < 0.4) return // 迟滞
      lastTop.current = top
      fb.style.top = `calc(var(--u) * ${top.toFixed(1)})`
    }

    place()
    const raf = requestAnimationFrame(() => requestAnimationFrame(place))
    const timer = window.setTimeout(place, 150)
    return () => {
      cancelAnimationFrame(raf)
      window.clearTimeout(timer)
    }
  }, [placeKey])

  return (
    <div className="flt funcbar" ref={fbRef} aria-label="调度器控制">
      <div className="fb-group">
        <button
          className="fab"
          id="btnRunnerStart"
          title="启动调度器"
          disabled={alive || !!busy}
          onClick={onStart}
        >
          <img className="fi" src="/qp-icons/ctrl/play.svg?v=5" alt="" />
          <span className="ft">{busy === 'start' ? '启动中' : '启动'}</span>
        </button>
        <button
          className="fab stop"
          id="btnRunnerStop"
          title="停止调度器"
          disabled={!alive || !!busy}
          onClick={onStop}
        >
          <img className="fi" src="/qp-icons/ctrl/stop.svg?v=5" alt="" />
          <span className="ft">{busy === 'stop' ? '停止中' : '停止'}</span>
        </button>
      </div>
      <div className="fb-group">
        <button className="fab ghost" id="btnShot" title="实时画面" onClick={onShot}>
          <img className="fi" src="/qp-icons/ctrl/refresh.svg?v=5" alt="" />
          <span className="ft">画面</span>
        </button>
      </div>
    </div>
  )
}
