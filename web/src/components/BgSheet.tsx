import { BG_VER, SCENE_INFO, SCENE_ORDER, sceneBgFile } from '../lib/scene'

/** 房间背景选择面板（点总览页左上角圆钮打开；点一张即生效）。
 *  缩略图跟着深浅色取对应文件（房间场景有 -dark 版），与实际应用的那张保持一致。 */
export function BgSheet({
  open,
  manual,
  dark,
  onSelect,
  onClose,
}: {
  open: boolean
  manual: string
  dark: boolean
  onSelect: (key: string) => void
  onClose: () => void
}) {
  if (!open) return null
  return (
    <div id="bgSheet" className="on" aria-hidden="false">
      <div className="bgmask" data-bgclose onClick={onClose} />
      <div className="bgpanel">
        <div className="bghead">
          <span>房间背景</span>
          <button type="button" data-bgclose onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="bggrid" id="bgSheetGrid">
          <button
            type="button"
            className={'bgtile' + (manual === 'auto' ? ' on' : '')}
            data-bg="auto"
            onClick={() => onSelect('auto')}
          >
            <span className="bgthumb autothumb">自动</span>
            <span className="bgname">跟随任务</span>
          </button>
          {SCENE_ORDER.map((k) => (
            <button
              type="button"
              key={k}
              className={'bgtile' + (manual === k ? ' on' : '')}
              data-bg={k}
              onClick={() => onSelect(k)}
            >
              <img
                className="bgthumb"
                loading="lazy"
                src={`/qp-icons/bg/${sceneBgFile(k, dark)}.jpg?v=${BG_VER}`}
                alt=""
              />
              <span className="bgname">{SCENE_INFO[k]?.name}</span>
            </button>
          ))}
        </div>
        <div className="bgtip">
          「自动」= 跟着当前任务换（喂食/洗澡/学习打工各一套）。长按左上角圆钮可直接切下一张。
        </div>
      </div>
    </div>
  )
}
