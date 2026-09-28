/** 右功能栏（官方 x=414 y=335/405/495 各 50×70）：启动 / 停止 / 画面
 *
 *  top 由 CSS 静态给出 —— `top = 右列第三颗圆钮的底 + 两倍圆钮间距`（见 app.css 的
 *  `.funcbar`）。用户明确不要"与任务列表底端对齐"那套动态定位。
 *
 *  历史：legacy 用一段 `place()` 在运行时把功能栏底端对齐到任务列表底端（带 0.4u
 *  迟滞、量两次防中间态），我照搬过一版；后来用户改主意要"固定在圆钮下面两倍间距
 *  的位置"，于是又改回静态 top —— 那段动态逻辑已移除，别再顺手加回来。 */
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
