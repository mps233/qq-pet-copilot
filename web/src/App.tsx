import { useCallback, useEffect, useRef, useState } from 'react'
import { TASK_GROUP, TASK_NAME, isLoopTask, runnerStart, runnerStop, saveSettings } from './api'
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

  return (
    <div className="app">
      {/* 切页不做转场动画 —— 与旧版一致（legacy 的 showTab 只切 display）。
          曾经加过单页"滑入"，但用户明确否掉了（他要的是 iOS 那种前后两页同时动的
          转场，后来决定不做了），所以这里保持无动画。 */}
      <main>
        {page === 'main' ? (
          <Overview
            data={data}
            busy={busy}
            scene={scene}
            onNav={go}
            onStart={() => void start()}
            onStop={() => void stop()}
            onReorder={(o) => void onReorder(o)}
          />
        ) : page === 'log' ? (
          <section className="card" data-page="log">
            <NavHead title="日志" onBack={() => go('main')} />
            <LogPage shots={data.shots ?? []} />
          </section>
        ) : page === 'shot' ? (
          <section className="shotpage" data-page="shot">
            <NavHead title="实时画面" onBack={() => go('main')} />
            <LivePage />
          </section>
        ) : page === 'adv' ? (
          <section className="card" data-page="adv">
            <NavHead title="冒险记录" onBack={() => go('main')} />
            <AdvPage />
          </section>
        ) : page === 'plan' ? (
          <section className="card" data-page="plan">
            <NavHead title="职业解锁计划" onBack={() => go('main')} />
            <PlanPage />
          </section>
        ) : page === 'notify' ? (
          <section className="card" data-page="notify">
            <NavHead title="通知" onBack={() => go('main')} />
            <NotifyPage editable={data.editable ?? {}} onSave={saveOne} />
          </section>
        ) : page === 'set' ? (
          <section className="card" data-page="set">
            <SettingsPage
              editable={data.editable ?? {}}
              onSave={saveOne}
              onExit={() => setPage('main')}
            />
          </section>
        ) : (
          // 其余页面还没搬完：先给占位 + 回总览入口（旧界面 / 上功能是全的）
          <section className="card" data-page={page}>
            <div className="navhead">
              <button
                className="backbtn"
                data-back="main"
                title="返回总览"
                onClick={() => setPage('main')}
              >
                <img src="/qp-icons/official/off_l1_back.png" alt="" />
              </button>
              <span className="navtitle">
                {(
                  {
                    adv: '冒险记录',
                    plan: '职业解锁计划',
                    log: '日志',
                    notify: '通知',
                    set: '设置',
                    shot: '实时画面',
                  } as Record<string, string>
                )[page] ?? page}
              </span>
            </div>
            <div style={{ padding: 'calc(var(--u) * 20)', opacity: 0.72, lineHeight: 1.9 }}>
              这一页还在迁移中。功能完整的旧界面在{' '}
              <a href="/" style={{ color: '#c2410c' }}>
                这里
              </a>
              。
            </div>
          </section>
        )}
      </main>

      {/* 拖拽保存结果的轻提示（复用 #torderToast 的样式，与 legacy 一致） */}
      <div id="torderToast" className={orderMsg ? 'on' : ''}>
        {orderMsg}
      </div>
    </div>
  )
}
