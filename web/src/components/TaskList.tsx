import {
  DndContext,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import { restrictToParentElement, restrictToVerticalAxis } from '@dnd-kit/modifiers'
import { SortableContext, arrayMove, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
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

function RowBody({
  k,
  t,
  pendingKey,
  nowDate,
  onToggle,
}: {
  k: string
  t: QueueTaskState | undefined
  pendingKey: string
  nowDate: string
  /** 点勾选框切换该任务启用状态（写回 `<k>_enabled`） */
  onToggle: (k: string, on: boolean) => void
}) {
  const st = t?.state ?? ''
  const on = st !== 'disabled'
  const isPend = !!pendingKey && k === pendingKey
  const det = statusText(t, nowDate)
  const tag = TASK_GROUP[k] ? <span className="ttag">{TASK_GROUP[k]}</span> : null

  return (
    <>
      <span className={'mname' + (on ? '' : ' off')}>{TASK_NAME[k] ?? k}</span>
      {isPend ? (
        <span className="marrow" title="进行中（等收尾结算）">
          ▶
        </span>
      ) : null}
      {tag}
      <span className="mright">
        {det.text ? (
          <span className={'mdet' + (det.cls ? ` ${det.cls}` : '')}>{det.text}</span>
        ) : null}
      </span>
      <span
        className={'mcb' + (on ? ' on' : '')}
        data-k={k}
        // 勾选框：阻止冒泡，免得 dnd-kit 挂在行上的 listeners 把这次按下当成拖拽起手
        // （legacy 是在 pointerdown 里 `if(e.target.closest('.mcb')) return` 做同样的事）
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation()
          onToggle(k, !on)
        }}
      />
    </>
  )
}

function rowClass(k: string, t: QueueTaskState | undefined, pendingKey: string) {
  const st = t?.state ?? ''
  const done = st === 'done' || st === 'dead'
  const isPend = !!pendingKey && k === pendingKey
  return 'mrow' + (done ? ' done' : '') + (isPend ? ' run' : '') + (st === 'disabled' ? ' off' : '')
}

/** 「任务顺序」组里的一行：用 dnd-kit 的 useSortable 包一层。
 *  listeners 绑在整行上，但传感器设了 220ms 长按门槛 —— 短按仍然是普通点击
 *  （勾选框/行内按钮照常工作），长按才进入拖拽。这替掉了 legacy 那套手写的
 *  pointerdown + 220ms 定时器实现。 */
function SortableRow(props: {
  k: string
  t: QueueTaskState | undefined
  pendingKey: string
  nowDate: string
  onToggle: (k: string, on: boolean) => void
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: props.k,
  })
  return (
    <div
      ref={setNodeRef}
      // 'sortable' 这个 class 是给 CSS 用的（.mrow.sortable{touch-action:none}）——
      // touch-action 必须在触摸开始前就声明好，不能等 onDragStart 再加。
      className={rowClass(props.k, props.t, props.pendingKey) + ' sortable' + (isDragging ? ' dragging' : '')}
      data-k={props.k}
      style={{
        transform: transform ? `translate3d(0, ${Math.round(transform.y)}px, 0)` : undefined,
        transition,
        zIndex: isDragging ? 9 : undefined,
      }}
      {...attributes}
      {...listeners}
    >
      <RowBody {...props} />
    </div>
  )
}

function StaticRow(props: {
  k: string
  t: QueueTaskState | undefined
  pendingKey: string
  nowDate: string
  onToggle: (k: string, on: boolean) => void
}) {
  return (
    <div className={rowClass(props.k, props.t, props.pendingKey)} data-k={props.k}>
      <RowBody {...props} />
    </div>
  )
}

/** 任务列表：分两组 —— 「日常轮巡」= 循环类，顺序固定不可拖；
 *  「任务顺序」= 其余，长按拖动排序，松手写回 tasks.order / tasks.main_order。 */
export function TaskList({
  data,
  onReorder,
  onToggle,
}: {
  data: Data
  onReorder: (order: string[]) => void
  /** 点勾选框切启用 —— 对应 legacy 里挂在 #taskList 上的那个 click 委托 */
  onToggle: (k: string, on: boolean) => void
}) {
  const qt = data.queue.tasks || {}
  const nowDate = (data.now || '').slice(0, 10)
  const pendingKey = data.queue.pending ? data.queue.pending_key || '' : ''
  const keys = Object.keys(qt)
  const loop = keys.filter(isLoopTask)
  const rest = keys.filter((k) => !isLoopTask(k))

  // 长按 220ms 才进入拖拽：既保住「点勾选框切启用」，也不跟页面滚动抢手势
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { delay: 220, tolerance: 8 } }),
  )

  const handleDragEnd = (e: DragEndEvent) => {
    const { active, over } = e
    if (!over || active.id === over.id) return
    const from = rest.indexOf(String(active.id))
    const to = rest.indexOf(String(over.id))
    if (from < 0 || to < 0) return
    onReorder(arrayMove(rest, from, to))
  }

  return (
    <div className="qpanel">
      <div className="qhead">
        <span className="qtitle">任务列表</span>
        <span className="qpend" id="qPend">
          {data.queue.pending ? <span className="run">{data.queue.pending} 待结算</span> : ''}
        </span>
      </div>
      <div className="tasklist" id="taskList">
        {loop.length ? (
          <>
            <div className="tghd">
              <span>日常轮巡</span>
              <i />
              <span className="tghint">按间隔巡检 · 顺序固定</span>
            </div>
            {loop.map((k) => (
              <StaticRow key={k} k={k} t={qt[k]} pendingKey={pendingKey} nowDate={nowDate} onToggle={onToggle} />
            ))}
          </>
        ) : null}

        {rest.length ? (
          <>
            <div className="tghd">
              <span>任务顺序</span>
              <i />
              <span className="tghint">按住拖动排序</span>
            </div>
            <DndContext
              sensors={sensors}
              collisionDetection={closestCenter}
              modifiers={[restrictToVerticalAxis, restrictToParentElement]}
              onDragEnd={handleDragEnd}
            >
              <SortableContext items={rest} strategy={verticalListSortingStrategy}>
                {rest.map((k) => (
                  <SortableRow key={k} k={k} t={qt[k]} pendingKey={pendingKey} nowDate={nowDate} onToggle={onToggle} />
                ))}
              </SortableContext>
            </DndContext>
          </>
        ) : null}
      </div>
    </div>
  )
}
