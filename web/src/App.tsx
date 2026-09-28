import { useCallback, useState } from 'react'
import { TASK_GROUP, TASK_NAME, isLoopTask, runnerStart, runnerStop, saveSettings } from './api'
import { Overview, type PageKey } from './components/Overview'
import { useData } from './lib/useData'
import { useScene } from './lib/useScene'

/** 迁移中的外壳：目前只渲染总览页；其余页（冒险/职业/日志/通知/设置/实时画面）
 *  按计划逐块搬。旧界面仍在 `/` 上原样服务，互不影响。 */
export default function App() {
  const { data, error, reload } = useData(6000)
  const [busy, setBusy] = useState('')
  const [page, setPage] = useState<PageKey>('main')
  const [orderMsg, setOrderMsg] = useState('')

  // 自动换背景要跟着「当前任务 + 进行中的活动」走
  const curKey = data?.queue.pending ? data.queue.pending_key || '' : data?.queue.current || ''
  const etaKind = data?.work_eta?.kind ?? ''
  const scene = useScene(curKey, etaKind)

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
      <main>
        {page === 'main' ? (
          <Overview
            data={data}
            busy={busy}
            scene={scene}
            onNav={setPage}
            onStart={() => void start()}
            onStop={() => void stop()}
            onReorder={(o) => void onReorder(o)}
          />
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
