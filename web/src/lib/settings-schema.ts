/**
 * 设置页的字段定义（数据驱动）。
 *
 * 字段名与 `dashboard.py` 的 `apply_settings` 映射表**逐字对应** —— 前端只发
 * `{字段名: 值}`，后端负责校验并写回 config.yaml（ruamel 往返，保留注释）。
 * 所以这里不需要知道配置键（`tasks.school.enabled` 那些），只需知道字段名与控件类型。
 *
 * 与 legacy 的差别：那边把 62 个字段的 HTML 全渲染进 `#setForm` 再用 `.hide`
 * 切换分组；这边**只渲染当前组**，省掉一半 DOM。
 */

export type FieldKind = 'bool' | 'int' | 'text' | 'select'

export interface FieldDef {
  key: string
  label: string
  kind: FieldKind
  /** select 的选项；也支持用 `'@work_locations'` 这种从 editable 里取选项的写法 */
  opts?: string[]
  tip?: string
  placeholder?: string
  /** 输入框宽度（u 单位，1u ≈ 视口宽/360）；不给则自适应 */
  w?: number
  unit?: string
}

export interface GroupDef {
  id: string
  title: string
  fields: FieldDef[]
}

/** 学习科目（后端 validate_field 的白名单） */
const SCHOOL_ATTRS = ['力量', '智力', '魅力', '夏令营']
/** 课时档位：短课/长课（具体分钟数按学园卡位定） */
const SCHOOL_DURATIONS = ['10分钟', '30分钟']
const WORK_DURATIONS = ['10分钟', '45分钟', '2小时']
const CARE_METHODS = ['ocr检测', '一键护理']
const EMPLOYED_ACTIONS = ['等到25/75（小于45min）', '等到25/75', '立刻召回', '让利雇主（不召回）']
const ADVENTURE_TYPES = ['附近走走', '诗和远方']

/** 二级分组：id 必须与 MENU 里的 key 一致（也是 `?grp=<id>` 直开用的值） */
export const GROUPS: Record<string, GroupDef> = {
  school: {
    id: 'school',
    title: '学习',
    fields: [
      { key: 'school_enabled', label: '启用学习', kind: 'bool' },
      { key: 'school_attribute', label: '学习科目', kind: 'select', opts: SCHOOL_ATTRS },
      { key: 'school_times', label: '每天学习次数', kind: 'int', tip: '0 = 不限', w: 60 },
      { key: 'school_duration', label: '课时档位', kind: 'select', opts: SCHOOL_DURATIONS, tip: '短课单位收益更高' },
    ],
  },
  work: {
    id: 'work',
    title: '打工',
    fields: [
      { key: 'work_enabled', label: '启用打工', kind: 'bool' },
      { key: 'work_location', label: '打工地点', kind: 'select', opts: ['@work_locations'] },
      { key: 'work_duration', label: '打工时长', kind: 'select', opts: WORK_DURATIONS },
      { key: 'hire_name', label: '优先雇佣', kind: 'text', placeholder: '好友的宠物名或主人名（留空 = 自动挑收益最高的）', tip: '部分匹配；不可雇/未配置时回落最上面一行' },
      { key: 'hire_wait', label: '等TA空闲', kind: 'bool', tip: '开 = 好友正忙时等它空下来再雇，而不是跳过' },
    ],
  },
  quota: {
    id: 'quota',
    title: '学习/打工 配额',
    fields: [
      { key: 'study_quota_hours', label: '今日学习', kind: 'int', unit: '小时', tip: '0 = 今天不学；24 ≈ 不限', w: 56 },
      { key: 'work_quota_hours', label: '今日打工', kind: 'int', unit: '小时', tip: '0 = 今天不打工；24 ≈ 不限', w: 56 },
      { key: 'coin_threshold', label: '金币阈值', kind: 'int', tip: '金币 >= 该值时优先学习，低于则先打工', w: 80 },
    ],
  },
  fatigue: {
    id: 'fatigue',
    title: '合计停止点与收益档',
    fields: [
      { key: 'stop_total_hours', label: '合计满则停止', kind: 'int', unit: '小时', tip: '学习+打工合计满这个小时数就两项都停（转冒险）。0 = 不限', w: 56 },
      { key: 'daily_hour_limit', label: '停学习点', kind: 'int', unit: '小时', tip: '合计满这么多小时后今天不再学习、只打工（手改 config.yaml 才生效于此项，界面默认跟合计同值）', w: 56 },
      { key: 'work_stop_hours', label: '停打工点', kind: 'int', unit: '小时', tip: '合计满这么多小时后今天连打工也停', w: 56 },
      { key: 'main_order', label: '主任务优先级', kind: 'text', placeholder: 'school>hire_friend>work>adventure', tip: '> 分隔，越靠前越优先。四个主任务：学习/雇佣好友/打工/冒险（任务列表也可以直接拖动排序）' },
    ],
  },
  care: {
    id: 'care',
    title: '护理',
    fields: [
      { key: 'care_energy', label: '体力阈值', kind: 'int', tip: '低于该值就喂食', w: 56 },
      { key: 'care_clean', label: '清洁阈值', kind: 'int', tip: '低于该值就洗澡', w: 56 },
      { key: 'care_method', label: '护理方式', kind: 'select', opts: CARE_METHODS, tip: '要拿护理勋章必须用「ocr检测」——一键护理不计入勋章进度' },
      { key: 'care_exchange', label: '补货数量', kind: 'int', unit: '个', tip: '金币补货时一次买多少个（兑换食物/购买洗澡道具共用）', w: 56 },
      { key: 'care_interval', label: '检查间隔', kind: 'int', unit: '秒', w: 56 },
    ],
  },
  friend_care: {
    id: 'friend_care',
    title: '好友护理',
    fields: [
      { key: 'friend_care_enabled', label: '启用', kind: 'bool' },
      { key: 'friend_care_name', label: '好友', kind: 'text', placeholder: '宠物名或主人名', tip: '留空则不会做，需要填一个好友' },
      { key: 'friend_care_range', label: '时间段', kind: 'text', placeholder: '00:00-00:00', tip: 'HH:MM-HH:MM；起止相同 = 全天' },
      { key: 'friend_care_interval', label: '间隔', kind: 'int', unit: '秒', w: 56 },
      { key: 'friend_care_method', label: '方式', kind: 'select', opts: CARE_METHODS },
    ],
  },
  hire_friend: {
    id: 'hire_friend',
    title: '雇佣好友',
    fields: [
      { key: 'hire_friend_enabled', label: '启用', kind: 'bool' },
      { key: 'hire_friend_times', label: '每天次数', kind: 'int', tip: '0 = 不限', w: 56 },
    ],
  },
  visit: {
    id: 'visit',
    title: '踩踩',
    fields: [
      { key: 'visit_enabled', label: '启用', kind: 'bool' },
      { key: 'visit_times', label: '每天次数', kind: 'int', w: 56 },
    ],
  },
  pk: {
    id: 'pk',
    title: 'PK',
    fields: [
      { key: 'pk_enabled', label: '启用', kind: 'bool' },
      { key: 'pk_times', label: '每天次数', kind: 'int', w: 56 },
      { key: 'pk_only', label: '只打', kind: 'text', placeholder: '宠物名/主人名，逗号分隔', tip: '留空 = 不限制；填了就只打名单里的' },
      { key: 'pk_skip', label: '跳过', kind: 'text', placeholder: '宠物名/主人名，逗号分隔' },
      { key: 'pk_helper', label: '打手', kind: 'text', placeholder: '宠物名/主人名，逗号分隔', tip: '不在名单里的会解雇、空位从宠友列表雇第一个可雇的' },
      { key: 'pk_helper_fallback', label: '打手兜底', kind: 'bool', tip: '开 = 名单都不可雇时自动雇战力最高的' },
      { key: 'pk_max_level', label: '等级上限', kind: 'int', tip: '-1 = 只打比自己低的；-2 = 只打比打手低的；0 = 不限', w: 56 },
    ],
  },
  adventure: {
    id: 'adventure',
    title: '冒险',
    fields: [
      { key: 'adventure_enabled', label: '启用', kind: 'bool' },
      { key: 'adventure_times', label: '次数/天', kind: 'int', tip: '0 = 不冒险；主号策略设 999 ≈ 不限', w: 64 },
      { key: 'adventure_type', label: '冒险类型', kind: 'select', opts: ADVENTURE_TYPES, tip: '附近走走约 45 秒（靠连跑刷次数）/ 诗和远方约 2 小时' },
    ],
  },
  employed: {
    id: 'employed',
    title: '被雇佣',
    fields: [
      { key: 'employed_enabled', label: '被雇佣托管', kind: 'bool' },
      { key: 'employed_action', label: '处理方式', kind: 'select', opts: EMPLOYED_ACTIONS },
      { key: 'employed_interval', label: '检查间隔', kind: 'int', unit: '秒', w: 56 },
    ],
  },
  gift_bag: {
    id: 'gift_bag',
    title: '福袋',
    fields: [
      { key: 'gift_bag_enabled', label: '启用', kind: 'bool' },
      { key: 'gift_bag_range', label: '时间段', kind: 'text', placeholder: '00:00-00:00' },
      { key: 'gift_bag_interval', label: '扫描间隔', kind: 'int', unit: '秒', w: 56 },
    ],
  },
  career: {
    id: 'career',
    title: '职业',
    fields: [
      { key: 'career_watch', label: '隐藏职业解锁监控', kind: 'bool', tip: '每节课结算后读职业树，检测到隐藏线解锁就记录并发通知' },
      { key: 'career_stop_study', label: '解锁后自动停学', kind: 'bool' },
      { key: 'career_interval', label: '兜底检查间隔', kind: 'int', unit: '分', w: 56 },
    ],
  },
  schedule: {
    id: 'schedule',
    title: '调度',
    fields: [
      { key: 'adventure_times', label: '冒险次数/天', kind: 'int', tip: '与「冒险」页同一项', w: 64 },
      { key: 'care_interval', label: '护理检查间隔', kind: 'int', unit: '秒', tip: '与「护理」页同一项', w: 56 },
    ],
  },
  adb: {
    id: 'adb',
    title: '连接手机（ADB）',
    fields: [
      { key: 'adb_path', label: 'adb 路径', kind: 'text', placeholder: '留空自动探测', tip: '改完需重启调度器才生效' },
      { key: 'adb_serial', label: '设备序列号', kind: 'text', placeholder: 'adb devices 查看' },
    ],
  },
}

/** 一级菜单：4 个分区，共 17 个二级页（与 legacy 的 MENU 一致） */
export const MENU: { section: string; items: { id: string; title: string }[] }[] = [
  {
    section: '核心任务',
    items: [
      { id: 'school', title: '学习' },
      { id: 'work', title: '打工' },
      { id: 'quota', title: '学习/打工 配额' },
      { id: 'fatigue', title: '合计停止点与收益档' },
    ],
  },
  {
    section: '日常互动',
    items: [
      { id: 'care', title: '护理' },
      { id: 'friend_care', title: '好友护理' },
      { id: 'hire_friend', title: '雇佣好友' },
      { id: 'visit', title: '踩踩' },
      { id: 'pk', title: 'PK' },
      { id: 'adventure', title: '冒险' },
    ],
  },
  {
    section: '扩展',
    items: [
      { id: 'employed', title: '被雇佣' },
      { id: 'gift_bag', title: '福袋' },
      { id: 'career', title: '职业' },
    ],
  },
  {
    section: '系统',
    items: [
      { id: 'schedule', title: '调度' },
      { id: 'adb', title: '连接手机（ADB）' },
    ],
  },
]

/** 展开 `'@work_locations'` 这类"选项来自后端"的写法 */
export function resolveOpts(opts: string[] | undefined, editable: Record<string, unknown>): string[] {
  if (!opts) return []
  if (opts.length === 1 && opts[0]?.startsWith('@')) {
    const v = editable[opts[0].slice(1)]
    return Array.isArray(v) ? v.map(String) : []
  }
  return opts
}
