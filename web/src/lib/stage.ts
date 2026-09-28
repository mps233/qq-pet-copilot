/**
 * 舞台单位基准：1dp = 可用宽度 / 360，写进 CSS 变量 `--vu`。
 *
 * CSS 里 `--u: var(--vu, calc(100vw / 360))` —— 优先用这里算出来的值。
 * **不用 100vw**：它把滚动条宽度也算进去（legacy 实测 clientWidth 489 vs 100vw 500），
 * 会让所有绝对坐标偏大约 2%。
 */
export function installStageUnit(): void {
  const setU = () => {
    const w = document.documentElement.clientWidth || window.innerWidth
    document.documentElement.style.setProperty('--vu', `${w / 360}px`)
  }
  setU()
  window.addEventListener('resize', setU)
  window.addEventListener('orientationchange', setU)
}
