import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchData, type Data } from '../api'

/** 轮询 /api/data（默认 6 秒一次，与旧界面一致）。
 *  失败时保留上一份数据，只把错误抛出——避免网络抖一下界面就空掉。 */
export function useData(intervalMs = 6000) {
  const [data, setData] = useState<Data | null>(null)
  const [error, setError] = useState('')
  const [tick, setTick] = useState(0)

  const reload = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    let alive = true
    const run = async () => {
      try {
        const d = await fetchData()
        if (alive) {
          setData(d)
          setError('')
        }
      } catch (e) {
        if (alive) setError(String(e))
      }
    }
    void run()
    const t = window.setInterval(run, intervalMs)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [intervalMs, tick])

  return { data, error, reload }
}

/** 秒级 ticker（倒计时/时钟显示用，不触发数据请求） */
export function useSecondTick(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [])
  return now
}

/** 简易防抖 */
export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value)
  const timer = useRef<number>()
  useEffect(() => {
    timer.current = window.setTimeout(() => setV(value), ms)
    return () => window.clearTimeout(timer.current)
  }, [value, ms])
  return v
}
