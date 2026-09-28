import { useEffect, useState } from 'react'
import { GROUPS, MENU, resolveOpts, type FieldDef } from '../lib/settings-schema'
import { FGroup, FRow, Switch } from './form'
import { NavHead } from './NavHead'

/** `?grp=<id>` 直开某个二级分组（便于分享链接/截图/调试，与 legacy 一致） */
const urlGrp = (): string | null => {
  try {
    const g = new URLSearchParams(window.location.search).get('grp')
    return g && GROUPS[g] ? g : null
  } catch {
    return null
  }
}

const strOf = (ed: Record<string, unknown>, k: string): string => {
  const v = ed[k]
  return v === null || v === undefined ? '' : String(v)
}
const boolOf = (ed: Record<string, unknown>, k: string): boolean => !!ed[k]

/** 单个字段的控件（按 kind 分派）。**改动即保存**，与 legacy 一致。 */
function Field({ f, editable, onSave }: {
  f: FieldDef
  editable: Record<string, unknown>
  onSave: (u: Record<string, unknown>) => Promise<void>
}) {
  const [local, setLocal] = useState<string>(() => strOf(editable, f.key))
  const [localBool, setLocalBool] = useState<boolean>(() => boolOf(editable, f.key))

  // 后端数据回来时同步（例如别处改了同一项）
  useEffect(() => {
    setLocal(strOf(editable, f.key))
    setLocalBool(boolOf(editable, f.key))
  }, [editable, f.key])

  if (f.kind === 'bool') {
    return (
      <FRow k={f.label} tip={f.tip}>
        <Switch
          on={localBool}
          title={f.tip}
          onToggle={() => {
            const next = !localBool
            setLocalBool(next)
            void onSave({ [f.key]: next })
          }}
        />
      </FRow>
    )
  }

  if (f.kind === 'select') {
    const opts = resolveOpts(f.opts, editable)
    return (
      <FRow k={f.label} tip={f.tip}>
        <select
          value={local}
          style={f.w ? { width: `calc(var(--u) * ${f.w + 40})` } : undefined}
          onChange={(e) => {
            setLocal(e.target.value)
            void onSave({ [f.key]: e.target.value })
          }}
        >
          {opts.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      </FRow>
    )
  }

  // int / text
  return (
    <FRow k={f.label} tip={f.tip}>
      <span className="ctrl">
        <input
          type={f.kind === 'int' ? 'number' : 'text'}
          value={local}
          placeholder={f.placeholder}
          style={f.w ? { width: `calc(var(--u) * ${f.w})` } : undefined}
          onChange={(e) => setLocal(e.target.value)}
          onBlur={() => {
            if (local !== strOf(editable, f.key)) void onSave({ [f.key]: local })
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
          }}
        />
        {/* 单位跟在**输入框后面**（legacy 的写法：`<input ...><span class="u">小时</span>`，
            CSS 的 .form .u 也是 margin-left —— 放前面会变成「秒 [60]」） */}
        {f.unit ? <span className="u">{f.unit}</span> : null}
      </span>
    </FRow>
  )
}

/** 设置页：一级菜单（4 个分区 / 17 个二级页）→ 二级表单。
 *  legacy 是把所有字段渲染进 `#setForm` 再用 `.hide` 切组，这边**只渲染当前组**。 */
export function SettingsPage({
  editable,
  onSave,
  onExit,
}: {
  editable: Record<string, unknown>
  onSave: (u: Record<string, unknown>) => Promise<void>
  onExit: () => void
}) {
  const [grp, setGrp] = useState<string | null>(() => urlGrp())
  const g = grp ? GROUPS[grp] : null

  // 进入二级页时把 ?grp 写进 URL（刷新/分享后还在这一页）；返回一级时清掉
  useEffect(() => {
    try {
      const u = new URL(window.location.href)
      if (grp) u.searchParams.set('grp', grp)
      else u.searchParams.delete('grp')
      window.history.replaceState(null, '', u.toString())
    } catch {
      /* 无所谓 */
    }
  }, [grp])

  if (!g) {
    return (
      <>
        <NavHead title="设置" onBack={onExit} />
        <div className="form" id="setMenu">
        {MENU.map(({ section, items }) => {
          const rows = items.filter((it) => GROUPS[it.id])
          if (!rows.length) return null
          return (
            <div className="msec" key={section}>
              <div className="fsect">{section}</div>
              <div className="fsec">
                {rows.map((it) => (
                  <div className="frow menurow" key={it.id} onClick={() => setGrp(it.id)}>
                    <span className="k">{it.title}</span>
                    <span className="chev">›</span>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
        </div>
      </>
    )
  }

  return (
    <>
      <div className="navhead">
        <button className="backbtn" title="返回设置列表" onClick={() => setGrp(null)}>
          <img src="/qp-icons/official/off_l1_back.png" alt="" />
        </button>
        <span className="navtitle">{g.title}</span>
      </div>
      <div className="form" id="setForm">
        <FGroup title={g.title} id={g.id}>
          {g.fields.map((f) => (
            <Field key={f.key} f={f} editable={editable} onSave={onSave} />
          ))}
        </FGroup>
      </div>
    </>
  )
}
