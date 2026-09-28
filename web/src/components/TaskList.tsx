import { TASK_GROUP, TASK_NAME, isLoopTask, type Data, type QueueTaskState } from '../api'

/** 行右侧状态文字（与 legacy 的 rowOf 一致：只给"有信息量"的几种写字） */
function statusText(t: QueueTaskState | undefined, nowDate: string) {
  const st = t?.state ?? ''
  if (st === 'disabled') return { cls: 'off-t', text: '已禁用' }
  if (st === 'run' || st === 'running') return { cls: 'run', text: '执行中' }
  if (st === 'waiting') {
    const nx = t?.next
    if (!nx) return { cls: '', text: '等待' }
    const cross = nx.slice(0, 10) === nowDate ? '' : '明 '
    return { cls: '', text: `等待 ${cross}${nx.slice(11, 16)}` }
  }
  if (st === 'done') return { cls: 'done', text: '✓ 今日完成' }
  if (st === 'dead') return { cls: '', text: '今日结束' }
  return { cls: '', text: '' }
}

function TaskRow({
  k,
  t,
  pendingKey,
  nowDate,
}: {
  k: string
  t: QueueTaskState | undefined
  pendingKey: string
  nowDate: string
}) {
  const st = t?.state ?? ''
  const on = st !== 'disabled'
  const isPend = !!pendingKey && k === pendingKey
  const det = statusText(t, nowDate)
  const done = st === 'done' || st === 'dead'
  const tag = TASK_GROUP[k] ? <span className="ttag">{TASK_GROUP[k]}</span> : null

  return (
    <div
      className={'mrow' + (done ? ' done' : '') + (isPend ? ' run' : '') + (on ? '' : ' off')}
      data-k={k}
    >
      <span className={'mname' + (on ? '' : ' off')}>{TASK_NAME[k] ?? k}</span>
      {isPend ? (
        <span className="marrow" title="进行中（等收尾结算）">
          ▶
        </span>
      ) : null}
      {tag}
      <span className="mright">
        {det.text ? <span className={'mdet' + (det.cls ? ` ${det.cls}` : '')}>{det.text}</span> : null}
      </span>
      <span className={'mcb' + (on ? ' on' : '')} data-k={k} />
    </div>
  )
}

/** 任务列表：分两组（日常轮巡 = 循环类、顺序固定；任务顺序 = 其余，可拖动）。
 *  ⚠️ `main > [data-page]` 与 `#tabbar button[data-tab]` 是旧版 JS 契约，
 *  这里已经全部由 React 接管，但仍保持 class/id 一致，方便对照旧实现。 */
export function TaskList({ data }: { data: Data }) {
  const qt = data.queue.tasks || {}
  const nowDate = (data.now || '').slice(0, 10)
  const pendingKey = data.queue.pending ? (data.queue.pending_key || '') : ''
  const keys = Object.keys(qt)
  const loop = keys.filter(isLoopTask)
  const rest = keys.filter((k) => !isLoopTask(k))

  const group = (title: string, hint: string, ks: string[]) =>
    ks.length ? (
      <>
        <div className="tghd">
          <span>{title}</span>
          <i />
          <span className="tghint">{hint}</span>
        </div>
        {ks.map((k) => (
          <TaskRow key={k} k={k} t={qt[k]} pendingKey={pendingKey} nowDate={nowDate} />
        ))}
      </>
    ) : null

  return (
    <div className="qpanel">
      <div className="qhead">
        <span className="qtitle">任务列表</span>
        <span className="qpend" id="qPend">
          {data.queue.pending ? <span className="run">{data.queue.pending} 待结算</span> : ''}
        </span>
      </div>
      <div className="tasklist" id="taskList">
        {group('日常轮巡', '按间隔巡检 · 顺序固定', loop)}
        {group('任务顺序', '按住拖动排序', rest)}
      </div>
    </div>
  )
}
