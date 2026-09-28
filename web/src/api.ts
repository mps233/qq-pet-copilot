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
  /** 今日学习/打工时长与效率档（分钟） */
  today_duration?: TodayDuration
  /** 进行中的活动（kind = 上课/打工/冒险，remaining = 剩余秒） */
  work_eta?: WorkEta
  /** 设置页可编辑项的快照（含 stop_total_hours 等） */
  editable?: Record<string, unknown>
  shots?: Shot[]
  friends?: string[]
  last_line?: string
}

export interface SchedulerInfo {
  alive: boolean
  pid: number
  uptime: string
}

export interface TodayDuration {
  learn_min: number
  work_min: number
  eff_pct: number
  total_min: number
  next_pct: number
  next_in_min: number
}

export interface WorkEta {
  eta_clock: string
  remaining: number
  /** 中文活动名：上课 / 打工 / 冒险 */
  kind: string
}

export interface Shot {
  name: string
  mtime: string
}

export interface QueueTaskState {
  state: string
  next?: string
  next_ts?: number
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
  energy: number | null
  clean: number | null
  mood: number | null
}

export interface TaskProgress {
  date?: string
  learned?: number
  done?: boolean
  study_secs?: number
  work_secs?: number
  duration?: string
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
}

/** 任务类型标签：「循环」= 按间隔巡检；「每日」= 每天定时；「主线」= 主任务组互斥 */
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

/** 日常轮巡组（顺序固定，不参与拖动排序） */
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

/** 调度器启停（功能栏的两个按钮） */
export const runnerStart = () => fetch('/api/runner/start', { method: 'POST' })
export const runnerStop = () => fetch('/api/runner/stop', { method: 'POST' })

/** GET /api/logs —— 当天日志（尾部 N 行） */
export interface LogsData {
  name: string
  total: number
  lines: string[]
}
export const fetchLogs = (): Promise<LogsData> => getJSON<LogsData>('/api/logs')

/** 收益汇总的一个桶（学习 / 打工各一份，另有 today_* 两份） */
export interface RewardBucket {
  sessions: number
  credits: number
  coins: number
  tired: number
  /** 解析出数值的条数 —— 前端据此区分"没收益"与"没解析出来"，不拿 +0 冒充 */
  credits_n: number
  coins_n: number
  workpoints: number
  workpoints_n: number
  /** 看视频加成（结算页「看视频获得 N 金币」），独立一笔、不计进 coins */
  ad_coins: number
  ad_coins_n: number
  attrs: Record<string, number>
}

/** 按次记录的一行：[时间, kind, 标题, 学分, 属性, 金币, 疲惫, 工分, 工资构成] */
export type RewardRow = [
  string,
  string,
  string,
  number | null,
  string,
  number | null,
  number,
  number | null,
  string,
]

export interface RewardsData {
  ok: boolean
  date: string
  dates: string[]
  date_n: Record<string, number>
  today: string
  yesterday: string
  all_n: number
  n: number
  school: RewardBucket
  work: RewardBucket
  today_school: RewardBucket
  today_work: RewardBucket
  recent: RewardRow[]
  /** 结算页头部解析出来的宠物名 / 主人名（换宠物时一眼看出这份数据是谁的） */
  pet: string
  owner: string
  updated: string
}

export const fetchRewards = (date?: string): Promise<RewardsData> =>
  getJSON<RewardsData>(`/api/rewards${date ? `?date=${encodeURIComponent(date)}` : ''}`)

/** 发送一条测试通知（通知页的"发送测试"按钮） */
export async function testNotify(): Promise<{ ok: boolean; msg?: string }> {
  const r = await fetch('/api/notify/test', { method: 'POST' })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as { ok: boolean; msg?: string }
}

/** 职业解锁哨兵的状态与历史事件 */
export interface WatchInfo {
  enabled: boolean
  stop_study: boolean
  interval: number
  last_check: string
  alive: boolean
  events: { career: string; name: string; ts: string; stopped: boolean }[]
}

export interface PlanLine {
  name: string
  jr: boolean
  ch: boolean
}

/** 阶梯路线一行：[编号, 描述, 是否完成, 进度说明] */
export type PlanStep = [string, string, boolean, string]

export interface PlanData {
  ok: boolean
  /** 当前属性值（键是单字：力/智/魅/工分/金币 + 初级毕业/中级毕业） */
  values: Record<string, number | boolean>
  total: number
  total_target: number
  /** 8 条线里已解锁的数量 */
  jr_n: number
  ch_n: number
  lines: PlanLine[]
  steps: PlanStep[]
  lines_meta: string
  gates: Record<string, boolean>
  updated: string
  watch: WatchInfo
}

export const fetchPlan = (): Promise<PlanData> => getJSON<PlanData>('/api/plan')

/** 保存手工录入的进度 */
export async function savePlan(updates: Record<string, unknown>): Promise<{ ok: boolean; msg?: string }> {
  const r = await fetch('/api/plan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ updates }),
  })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as { ok: boolean; msg?: string }
}

/** 出门→职业小镇 OCR 自动识别（子进程隔离，防并发锁） */
export async function syncPlan(): Promise<{ ok: boolean; msg?: string }> {
  const r = await fetch('/api/plan/sync', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as { ok: boolean; msg?: string }
}

/** 冒险记录（统一数据源 runs/adventure_live.jsonl） */
export interface AdvData {
  ok: boolean
  /** 统计范围内的总把数 / 净收益 / 平均每把 */
  n: number
  net: number
  avg: number
  /** 收益档分布：{"20": 69, "0": 180, ...} */
  dist: Record<string, number>
  win: number
  zero: number
  loss: number
  /** 状态增益汇总：[属性名, 次数, 总量] */
  gains: [string, number, number][]
  /** 累计收益曲线 [索引, 值] */
  cum: [number, number][]
  /** 单次收益散点 [索引, 值] */
  pts: [number, number][]
  /** 状态轨迹 [索引, 体力, 清洁, 心情, 是否护理后] */
  stats: [number, number | null, number | null, number | null, number][]
  /** 逐次明细 [索引, 时间, 本趟收益, 增益文字, 扣费] */
  recent: [number, string, number | null, string, number][]
  today_n: number
  today_net: number
  date: string
  dates: string[]
  date_n: Record<string, number>
  today: string
  yesterday: string
  all_n: number
  updated: string
  /** 护理阈值（画那条红虚线用；两个值可能不同） */
  care_energy: number | null
  care_clean: number | null
}

export const fetchAdventure = (date?: string): Promise<AdvData> =>
  getJSON<AdvData>(`/api/adventure${date ? `?date=${encodeURIComponent(date)}` : ''}`)

/** 实时画面（设备端 scrcpy-server + 本机 ffmpeg 转 HLS）的状态 */
export interface StreamStatus {
  running: boolean
  error?: string
  /** 服务端从 init.mp4 的 avcC box 解析出的真实 codec（MSE 必须用它，写错会静默黑屏） */
  codec?: string
  /** "720x1280" 形式的视频尺寸 / 设备尺寸（点击换算要用） */
  video?: string
  device?: string
  uptime?: number
  scheduler_alive?: boolean
  /** 调度器已让路（接管生效） */
  scheduler_paused?: boolean
}

export const streamStatus = (): Promise<StreamStatus> => getJSON<StreamStatus>('/api/stream/status')

/** 接管：请调度器让路（原地等待，不退出进程、不丢进度） */
export async function runnerPause(): Promise<{ ok: boolean; msg?: string }> {
  const r = await fetch('/api/runner/pause', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as { ok: boolean; msg?: string }
}

/** 交还控制权，调度器从原处继续 */
export async function runnerResume(): Promise<{ ok: boolean; msg?: string }> {
  const r = await fetch('/api/runner/resume', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as { ok: boolean; msg?: string }
}
