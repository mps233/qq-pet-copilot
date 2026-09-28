/** 右功能栏（官方 x=414 y=335/405/495 各 50×70）：启动 / 停止 / 画面 */
export function FuncBar({
  alive,
  busy,
  onStart,
  onStop,
  onShot,
}: {
  alive: boolean
  busy: string
  onStart: () => void
  onStop: () => void
  onShot: () => void
}) {
  return (
    <div className="flt funcbar" aria-label="调度器控制">
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
