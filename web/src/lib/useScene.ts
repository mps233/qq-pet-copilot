import { useCallback, useEffect, useState } from 'react'
import {
  applyScene,
  computeScene,
  isDarkTheme,
  readManualScene,
  SCENE_ORDER,
  sceneName,
  writeManualScene,
} from './scene'

/** 房间背景状态。
 *
 *  **手动选择必须暂停"按当前任务自动换背景"** —— 否则每 6 秒一次的数据刷新会立刻把
 *  背景改回任务场景，用户看到的就是"点了没用、自己弹回去"（legacy 踩过）。
 *  选择存 `localStorage('qpet_scene')`，选回「自动」才恢复跟随。 */
export function useScene(curKey: string, etaKind: string) {
  const [manual, setManual] = useState<string>(() => readManualScene())
  const [dark, setDark] = useState<boolean>(() => isDarkTheme())
  const [toast, setToast] = useState('')

  // 跟随系统深浅色（背景要换图，不是调暗）
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const on = () => setDark(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])

  const scene = manual === 'auto' ? computeScene(curKey, etaKind) : manual

  useEffect(() => {
    applyScene(scene, dark)
  }, [scene, dark])

  const select = useCallback((key: string, quiet = false) => {
    setManual(key)
    writeManualScene(key)
    if (!quiet) {
      setToast(sceneName(key))
      window.setTimeout(() => setToast(''), 1400)
    }
  }, [])

  /** 长按圆钮：直接切下一张（`auto` 时从第一张开始） */
  const cycle = useCallback(() => {
    const list = ['auto', ...SCENE_ORDER]
    const i = list.indexOf(manual)
    const next = list[(i + 1) % list.length] ?? 'auto'
    setManual(next)
    writeManualScene(next)
    setToast(sceneName(next))
    window.setTimeout(() => setToast(''), 1400)
  }, [manual])

  return { manual, scene, select, cycle, toast, dark }
}
