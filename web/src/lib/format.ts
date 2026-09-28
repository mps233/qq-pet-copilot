/** 数值格式化 —— 与 legacy/app.js 的同名工具保持一致 */

export const pad2 = (n: number): string => String(n).padStart(2, '0')

/** 秒 → "H:MM:SS" / "MM:SS" */
export function hms(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return (h ? `${h}:` : '') + pad2(m) + ':' + pad2(s % 60)
}

/** 秒 → 小时字符串，保留 1 位且去掉多余的 .0（3600 → "1"，0 → "0"） */
export const hrs = (sec: number | undefined): string =>
  ((sec || 0) / 3600).toFixed(1).replace(/\.0$/, '')

/** 把值夹进 0~100 的百分比（进度条 / 状态环用） */
export const pct = (v: number, max: number): number =>
  max > 0 ? Math.min(100, Math.max(0, (v / max) * 100)) : 0

/** 状态值可能为 null / ''（未识别到），统一成 number | null */
export function petVal(st: Record<string, unknown>, k: string): number | null {
  const raw = st?.[k]
  if (raw === null || raw === undefined || raw === '') return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

/** HTML 转义（设置页里往 title 写文本时用） */
export const esc = (s: unknown): string =>
  String(s ?? '').replace(
    /[&<>"']/g,
    (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string,
  )
