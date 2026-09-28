import type { ReactNode } from 'react'

/** 分组卡片：`.fgrp` > `.fsect`(标题) + `.fsec`(内容行)。
 *  与 legacy 的 `card(title, rows, key)` 生成同一份 DOM —— 设置页二级导航靠
 *  `id="grp_<key>"` 定位要显示哪一组，所以 id 命名必须保持一致。 */
export function FGroup({
  title,
  id,
  children,
}: {
  title: ReactNode
  id?: string
  children: ReactNode
}) {
  return (
    <div className="fgrp" id={id ? `grp_${id}` : undefined} data-title={typeof title === 'string' ? title : undefined}>
      <div className="fsect">{title}</div>
      <div className="fsec">{children}</div>
    </div>
  )
}

/** 一行：左边键名（可带 title 悬浮说明）+ 右侧任意控件 */
export function FRow({
  k,
  tip,
  children,
  block,
}: {
  k: ReactNode
  tip?: string
  children: ReactNode
  /** 整行块级（说明文字这种跨列内容用） */
  block?: boolean
}) {
  return (
    <div className="frow" style={block ? { display: 'block' } : undefined}>
      <span className="k" title={tip}>
        {k}
      </span>
      {children}
    </div>
  )
}

/** 开关按钮：`.sw` + 选中态 `.on`（CSS 里就是按钮样式，不是 checkbox） */
export function Switch({
  on,
  onToggle,
  title,
}: {
  on: boolean
  onToggle: () => void
  title?: string
}) {
  return (
    <button
      type="button"
      className={'sw' + (on ? ' on' : '')}
      title={title}
      aria-pressed={on}
      onClick={onToggle}
    />
  )
}

/** 说明行：`.noterow` > `.nt`(短标签) + `.nb`(正文) */
export function NoteRow({ t, children }: { t: string; children: ReactNode }) {
  return (
    <div className="noterow">
      <span className="nt">{t}</span>
      <span className="nb">{children}</span>
    </div>
  )
}
