import { useState } from 'react'
import { runnerStart, runnerStop } from './api'
import { Overview, type PageKey } from './components/Overview'
import { useData } from './lib/useData'

/** 迁移中的外壳：目前只渲染总览页；其余页（冒险/职业/日志/通知/设置/实时画面）
 *  与拖拽、背景切换等交互按计划逐块搬。旧界面仍在 `/` 上原样服务，互不影响。 */
export default function App() {
  const { data, error, reload } = useData(6000)
  const [busy, setBusy] = useState('')
  const [page, setPage] = useState<PageKey>('main')

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

  if (error && !data) {
    return (
      <div className="app">
        <main>
          <section className="home" data-page="main">
            <div style={{ padding: 'calc(var(--u) * 40)', color: '#b91c1c' }}>
              连接失败：{error}
            </div>
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
            onNav={setPage}
            onStart={() => void start()}
            onStop={() => void stop()}
          />
        ) : (
          // 其余页面还没搬完：先给个占位 + 回总览的入口（旧界面 / 上功能是全的）
          <section className="card" data-page={page}>
            <div className="navhead">
              <button className="backbtn" data-back="main" title="返回总览" onClick={() => setPage('main')}>
                <img src="/qp-icons/official/off_l1_back.png" alt="" />
              </button>
              <span className="navtitle">
                {({ adv: '冒险记录', plan: '职业解锁计划', log: '日志', notify: '通知', set: '设置', shot: '实时画面' } as Record<string, string>)[page] ?? page}
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
    </div>
  )
}
