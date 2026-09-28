// 与 dashboard.py 的 /api/* 对齐的类型定义。
// 以前这些字段靠脑记（`care_energy`、`work_eta`、`adventure_type`…），现在有编译器兜着。

/** GET /api/data —— 总览页的主数据源 */
export interface Data {
  now: string
  scheduler: SchedulerInfo
  queue: QueueInfo
  status: PetStatus
  progress: Record<string, TaskProgress>
  config: ConfigSnapshot
}

export interface SchedulerInfo {
  alive: boolean
  pid: number
  uptime: string
}

export interface QueueTaskState {
  state: string
  next?: string
  next_ts?: number
  /** 任务对象名（好友护理 → 喵帕斯～） */
  sub?: string
}

export interface QueueInfo {
  current: string
  pending: string
  pending_key: string
  next: string
  next_at: string
  next_ts: number
  ready: number
  waiting: number
  tasks: Record<string, QueueTaskState>
  pid: number
  started: number
  updated: string
}

export interface PetStatus {
  coins: number
  updated: string
  pet_name: string
  energy: number
  clean: number
  mood: number
}

export interface TaskProgress {
  today?: number
  total?: number
  history?: Record<string, number>
  [k: string]: unknown
}

export interface ConfigSnapshot {
  strategy: string
  school_enabled: boolean
  work_enabled: boolean
  work_location: string
  work_duration: string
  coin_threshold: number
  study_quota_hours: number
  work_quota_hours: number
  visit_per_day: number
  pk_per_day: number
  adventure_times: number
  adventure_start: string
  [k: string]: unknown
}

/** 任务键 → 中文名（与后端 key 一一对应） */
export const TASK_NAME: Record<string, string> = {
  care: '护理',
  school: '学习',
  friend_care: '好友护理',
  gift_bag: '福袋',
  hire_friend: '雇佣好友',
  adventure: '冒险',
  visit: '踩踩',
  pk: 'PK',
  work: '打工',
  employed: '被雇佣',
}

/** 任务分组：「循环」= 日常轮巡（顺序固定，不参与拖动）；其余可拖动排序 */
export const TASK_GROUP: Record<string, '循环' | '每日' | '主线'> = {
  care: '循环',
  friend_care: '循环',
  gift_bag: '循环',
  visit: '每日',
  pk: '每日',
  adventure: '主线',
  school: '主线',
  work: '主线',
  hire_friend: '主线',
}

export const isLoopTask = (k: string): boolean => TASK_GROUP[k] === '循环'

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url, { cache: 'no-store' })
  if (!r.ok) throw new Error(`${url} → HTTP ${r.status}`)
  return (await r.json()) as T
}

export const fetchData = (): Promise<Data> => getJSON<Data>('/api/data')

export interface SettingsResult {
  ok: boolean
  applied: Record<string, unknown>
  rejected: string[]
}

/** POST /api/settings —— 设置页保存（后端用 ruamel 往返写 config.yaml，保留注释） */
export async function saveSettings(
  updates: Record<string, unknown>,
): Promise<SettingsResult> {
  const r = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ updates }),
  })
  if (!r.ok) throw new Error(`保存失败 HTTP ${r.status}`)
  return (await r.json()) as SettingsResult
}
