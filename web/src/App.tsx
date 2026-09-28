import { useCallback, useEffect, useRef, useState } from 'react'
import { TASK_GROUP, TASK_NAME, isLoopTask, runnerStart, runnerStop, saveSettings, type Data } from './api'
import { Overview, type PageKey } from './components/Overview'
import { AdvPage } from './components/AdvPage'
import { LivePage } from './components/LivePage'
import { LogPage } from './components/LogPage'
import { NavHead } from './components/NavHead'
import { NotifyPage } from './components/NotifyPage'
import { PlanPage } from './components/PlanPage'
import { SettingsPage } from './components/SettingsPage'
import { useData } from './lib/useData'
import { useScene } from './lib/useScene'

/** 迁移中的外壳：目前只渲染总览页；其余页（冒险/职业/日志/通知/设置/实时画面）
 *  按计划逐块搬。旧界面仍在 `/` 上原样服务，互不影响。 */
export default function App() {
  const { data, error, reload } = useData(6000)
  const [busy, setBusy] = useState('')
  const [page, setPage] = useState<PageKey>('main')
  /**
   * 历史栈模型（与 legacy 的 showTab / popstate 一一对应）：
   *   navDepth = 0（总览）/ 1（内页）/ 2（二级：设置 grp、日志子页）
   *     往下一层   → pushState
   *     同级互切   → replaceState（栈不增长）
   *     回总览     → 一次退够（back 或 go(-navDepth)）
   *     二级→别的内页 → 先退到总览再压目标（pendingTab 收尾）
   *   **"返回"类操作绝不能 pushState** —— 按钮在压栈、手势在退栈，方向相反会退不回去。
   *   这套是侧滑返回能用的前提；迁移时漏掉导致侧滑失效，这里补回来。
   */
  const navDepth = useRef(0)
  const pendingTab = useRef<PageKey | null>(null)

  const go = useCallback((name: PageKey) => {
    const target = name === 'main' ? 0 : 1
    const url = name === 'main' ? location.pathname : `?tab=${name}`
    try {
      if (target > navDepth.current) {
        navDepth.current = target
        history.pushState({ tab: name }, '', url)
      } else if (target === navDepth.current) {
        if (target === 1) history.replaceState({ tab: name }, '', url)
      } else if (target === 0) {
        const steps = navDepth.current
        navDepth.current = 0
        if (steps === 1) history.back()
        else history.go(-steps)
      } else {
        pendingTab.current = name
        navDepth.current = 0
        history.go(-2)
      }
    } catch {
      /* 某些环境禁 pushState，退化成纯状态切换 */
    }
    setPage(name)
  }, [])

  // 侧滑 / 浏览器返回
  useEffect(() => {
    const onPop = (e: PopStateEvent) => {
      const st = (e.state || {}) as { tab?: PageKey }
      if (pendingTab.current) {
        // 跳级退栈的收尾：落回总览后再压目标页
        const t = pendingTab.current
        pendingTab.current = null
        try {
          history.pushState({ tab: t }, '', `?tab=${t}`)
        } catch {
          /* 忽略 */
        }
        navDepth.current = 1
        setPage(t)
        return
      }
      // state 拿不到就回退到 URL —— pushState 时 URL 一定写了 ?tab=xxx。
      // 实测（headless）history.state 读出来是 null，不能只依赖它。
      const fromUrl = new URLSearchParams(location.search).get('tab') as PageKey | null
      const t: PageKey = (st.tab as PageKey) || fromUrl || 'main'
      navDepth.current = t === 'main' ? 0 : 1
      setPage(t)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  // 直开 ?tab=xxx（便于分享链接 / 截图 / 调试）
  useEffect(() => {
    try {
      const t = new URLSearchParams(location.search).get('tab') as PageKey | null
      if (t && ['adv', 'plan', 'log', 'notify', 'set', 'shot'].includes(t)) {
        setPage(t)
        navDepth.current = 1
      }
    } catch {
      /* 忽略 */
    }
  }, [])

  const [orderMsg, setOrderMsg] = useState('')

  // ---- iOS 式转场：前后两页同时在场（旧页左让 / 新页右入，返回时反过来）----
  // leaving = 正在退场的那一页（只活 320ms，动画结束就卸载）；dir 决定方向。
  const [leaving, setLeaving] = useState<PageKey | null>(null)
  const [dir, setDir] = useState<'fwd' | 'back'>('fwd')
  const [transiting, setTransiting] = useState(false)
  const prevPage = useRef<PageKey | null>(null)
  const leaveTimer = useRef<number>()

  /** page 一变就起转场。放在 effect 里而不是塞进 go()，是为了不受函数定义顺序约束
   *  （go 定义在前、会引用尚未初始化的 switchTo）。首次渲染不触发 —— 那时 prevPage
   *  还是 null。回总览算 back（新页从左进），进内页/内页互切算 fwd（新页从右进）。
   *  go()/popstate 两条路径都只 setPage，这里统一兜住。 */
  useEffect(() => {
    const from = prevPage.current
    prevPage.current = page
    if (!from || from === page) return
    setDir(page === 'main' ? 'back' : 'fwd')
    setLeaving(from)
    setTransiting(true)
    window.clearTimeout(leaveTimer.current)
    leaveTimer.current = window.setTimeout(() => {
      setLeaving(null)
      setTransiting(false)
    }, 340)
  }, [page])

  // 自动换背景要跟着「当前任务 + 进行中的活动」走
  const curKey = data?.queue.pending ? data.queue.pending_key || '' : data?.queue.current || ''
  const etaKind = data?.work_eta?.kind ?? ''
  const scene = useScene(curKey, etaKind)

  // 切页/换背景时同步两件「旧版有、迁移时漏掉」的东西：
  //
  // 1) `html[data-page]` —— CSS 里 `html[data-page]:not([data-page="main"])` 靠它把内页的
  //    底色与背景图切成白色顶栏。不设这个属性的话，进设置页后顶部的安全区**仍然显示总览页
  //    的房间暖色**（旧版是 showTab() 里 setAttribute 的，见 legacy app.js:2096）。
  // 2) `theme-color` meta —— iOS 18 及以前读它画状态栏那条带；iOS 26+ 改为采样 html 的
  //    background-color（那条路靠 CSS 的 --qp-statusbar，本身已经对）。
  useEffect(() => {
    const root = document.documentElement
    root.setAttribute('data-page', page)
    const dark = !!window.matchMedia?.('(prefers-color-scheme: dark)').matches
    let want = '#FFFFFF' // 内页顶栏是白的
    if (page === 'main') {
      const v = getComputedStyle(root).getPropertyValue('--qp-statusbar').trim()
      want = v || (dark ? '#A9722D' : '#D5A758') // CSS 变量读不到时的兜底
    }
    document.querySelectorAll('meta[name="theme-color"]').forEach((m) => {
      // 两组 meta 各带一个 media 查询，只改属于当前主题的那条 ——
      // 两条都改的话，切系统主题时会把另一主题的值也覆盖成当前主题的色（串色）
      const isDark = (m.getAttribute('media') || '').includes('dark')
      if (isDark === dark) m.setAttribute('content', want)
    })
  }, [page, scene.scene, scene.dark])

  const start = async () => {
    setBusy('start')
    try {
      await runnerStart()
    } finally {
      window.setTimeout(() => {
        setBusy('')
        reload()
      }, 3000)
    }
  }
  const stop = async () => {
    setBusy('stop')
    try {
      await runnerStop()
    } finally {
      window.setTimeout(() => {
        setBusy('')
        reload()
      }, 3000)
    }
  }

  /** 拖完写回配置：tasks.order（全量，轮巡组保持在最前）+ tasks.main_order
   *  （四个主任务按新相对顺序）——**两个都写拖动才真的影响调度**：主任务组互斥时
   *  看的是 main_order，只改 order 会"拖了但没生效"。（与 legacy 的 submitTaskOrder 一致） */
  const onReorder = useCallback(async (restOrder: string[]) => {
    const loopKeys = Object.keys(TASK_NAME).filter(isLoopTask)
    const full = [...loopKeys, ...restOrder.filter((k) => !loopKeys.includes(k))]
    const mains = restOrder.filter((k) => TASK_GROUP[k] === '主线')
    try {
      const r = await saveSettings({
        task_order: full.join('>'),
        main_order: mains.join('>'),
      })
      setOrderMsg(r.ok ? '任务顺序已保存 · 下一轮调度生效' : `保存失败：${r.rejected.join('、')}`)
    } catch (e) {
      setOrderMsg(`保存失败：${String(e)}`)
    } finally {
      window.setTimeout(() => setOrderMsg(''), 2200)
      reload()
    }
  }, [reload])

  /** 单字段保存（通知页的开关/文本框、设置页的表单都走这里）。
   *  与 legacy 的「改动即自动保存」一致：后端 ruamel 往返写 config.yaml，下一轮生效。 */
  const saveOne = useCallback(
    async (updates: Record<string, unknown>) => {
      try {
        const r = await saveSettings(updates)
        setOrderMsg(r.ok ? '已保存' : `保存失败：${r.rejected.join('、')}`)
      } catch (e) {
        setOrderMsg(`保存失败：${String(e)}`)
      } finally {
        window.setTimeout(() => setOrderMsg(''), 1800)
        reload()
      }
    },
    [reload],
  )

  if (error && !data) {
    return (
      <div className="app">
        <main>
          <section className="home" data-page="main">
            <div style={{ padding: 'calc(var(--u) * 40)', color: '#b91c1c' }}>连接失败：{error}</div>
          </section>
        </main>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="app">
        <main>
          <section className="home" data-page="main">
            <div style={{ padding: 'calc(var(--u) * 40)', opacity: 0.6 }}>加载中…</div>
          </section>
        </main>
      </div>
    )
  }

  /** 按页面键渲染。抽成函数是为了让"正在退场的那一页"也能被渲染 —— iOS 式转场
   *  需要前后两页同时在场（单页滑入那种是旧页瞬间消失，不像原生）。
   *  第二参数显式传数据，避免依赖闭包里的类型窄化。 */
  const renderPage = (p: PageKey, d: Data) => {
    if (p === 'main') {
      return (
        <Overview
          data={d}
          busy={busy}
          scene={scene}
          onNav={go}
          onStart={() => void start()}
          onStop={() => void stop()}
          onReorder={(o) => void onReorder(o)}
          onToggle={(k, on) => void saveOne({ [`${k}_enabled`]: on })}
        />
      )
    }
    if (p === 'log') {
      return (
        <section className="card" data-page="log">
          <NavHead title="日志" onBack={() => go('main')} />
          <LogPage shots={d.shots ?? []} />
        </section>
      )
    }
    if (p === 'shot') {
      return (
        <section className="shotpage" data-page="shot">
          <NavHead title="实时画面" onBack={() => go('main')} />
          <LivePage />
        </section>
      )
    }
    if (p === 'adv') {
      return (
        <section className="card" data-page="adv">
          <NavHead title="冒险记录" onBack={() => go('main')} />
          <AdvPage />
        </section>
      )
    }
    if (p === 'plan') {
      return (
        <section className="card" data-page="plan">
          <NavHead title="职业解锁计划" onBack={() => go('main')} />
          <PlanPage />
        </section>
      )
    }
    if (p === 'notify') {
      return (
        <section className="card" data-page="notify">
          <NavHead title="通知" onBack={() => go('main')} />
          <NotifyPage editable={d.editable ?? {}} onSave={saveOne} />
        </section>
      )
    }
    if (p === 'set') {
      return (
        <section className="card" data-page="set">
          <SettingsPage editable={d.editable ?? {}} onSave={saveOne} onExit={() => go('main')} />
        </section>
      )
    }
    return null
  }

  return (
    <div className="app">
      {/* className 带的方向 class 变化即触发滑入动画（不需要给 main 加 key，
          key 会重建整棵子树、把内页状态和滚动位置一起清掉）。 */}
      <main>
        {/* iOS 式转场：退场的那一页还在（往左/右让开），新页从另一侧推入 */}
        {leaving ? (
          <div className={'page-slot page-leave-' + dir} aria-hidden="true">
            {renderPage(leaving, data)}
          </div>
        ) : null}
        <div className={'page-slot page-enter' + (transiting ? ' page-enter-' + dir : '')}>
          {renderPage(page, data)}
        </div>
      </main>

      {/* 拖拽保存结果的轻提示（复用 #torderToast 的样式，与 legacy 一致） */}
      <div id="torderToast" className={orderMsg ? 'on' : ''}>
        {orderMsg}
      </div>
    </div>
  )
}
