import type { ReactNode } from 'react'

/** 内页统一顶栏：返回钮 + 标题（+ 右侧可选的 meta 区）。
 *  返回钮走 React 路由，但保留 `data-back`（旧版 JS 契约）。 */
export function NavHead({
  title,
  meta,
  onBack,
  titleId,
}: {
  title: ReactNode
  meta?: ReactNode
  onBack: () => void
  titleId?: string
}) {
  return (
    <div className="navhead">
      <button className="backbtn" data-back="main" title="返回总览" onClick={onBack}>
        <img src="/qp-icons/official/off_l1_back.png" alt="" />
      </button>
      <span className="navtitle" id={titleId}>
        {title}
      </span>
      {meta ? <span className="navmeta">{meta}</span> : null}
    </div>
  )
}
