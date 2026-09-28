import { useCallback, useEffect, useRef, useState } from 'react'
import { runnerPause, runnerResume, streamStatus, type StreamStatus } from '../api'

const isIOS = (): boolean => /iPhone|iPad|iPod/.test(navigator.userAgent)

/** MSE 的 codec 候选：**优先用服务端从 init.mp4 的 avcC 解析出的精确值**，
 *  再用常见 profile 兜底。写错 profile 时 addSourceBuffer 不报错但**静默不解码**（黑屏）。 */
function codecCandidates(codec?: string): string[] {
  const list: string[] = []
  if (codec) list.push(`video/mp4; codecs="${codec}"`)
  for (const c of ['avc1.64001f', 'avc1.42c01f', 'avc1.42e01f', 'avc1.4d401f', 'avc1.640028', 'avc1.640032', 'avc1.64001e']) {
    list.push(`video/mp4; codecs="${c}"`)
  }
  return list
}

const pickDims = (s: StreamStatus | null): { video: [number, number]; device: [number, number] } | null => {
  if (!s) return null
  const ok = (a: number[]) => a.length === 2 && (a[0] ?? 0) > 0 && (a[1] ?? 0) > 0
  const v = String(s.video || '').split('x').map(Number)
  const d = String(s.device || '').split('x').map(Number)
  if (ok(v) && ok(d)) return { video: [v[0]!, v[1]!], device: [d[0]!, d[1]!] }
  if (ok(v)) return { video: [v[0]!, v[1]!], device: [v[0]!, v[1]!] } // 设备尺寸缺失时按同比例
  return null
}

/** 实时画面页：设备端 scrcpy-server 采集 + 本机 ffmpeg 转 HLS。
 *  iOS 走原生 HLS，其余走 MSE 拉同一份 fMP4 分片；点画面注入操作（需先接管）。 */
export function LivePage() {
  const videoRef = useRef<HTMLVideoElement>(null)
  const seenRef = useRef<Set<string>>(new Set())
  const msRef = useRef<MediaSource | null>(null)
  const sbRef = useRef<SourceBuffer | null>(null)
  const onRef = useRef(false)
  const timerRef = useRef<number | null>(null)
  const lastSeekRef = useRef(0)
  const restartingRef = useRef(false)
  const infoRef = useRef<StreamStatus | null>(null)
  const toastTimer = useRef<number>()

  const [on, setOn] = useState(false)
  const [takeover, setTakeover] = useState(false)
  // 心跳回调里要读"当前是否已接管"，但它是在 start() 里建立的长生命周期闭包 ——
  // 直接闭包捕获 takeover 会永远读到建立时的旧值，所以用 ref 同步。
  const takeoverRef = useRef(false)
  const [toast, setToast] = useState('')
  const [hint, setHint] = useState(
    '点「开始直播」看手机实时画面（首次启动约 3~7 秒；不看了点停止，服务端 30 秒无观众会自动回收）',
  )
  const [meta, setMeta] = useState('')

  const showToast = useCallback((msg: string, ms = 2000) => {
    setToast(msg)
    window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(''), ms)
  }, [])

  const dims = useCallback(async () => {
    const r = pickDims(infoRef.current)
    if (r) return r
    infoRef.current = await streamStatus()
    return pickDims(infoRef.current)
  }, [])

  const lag = useCallback((): number | null => {
    const v = videoRef.current
    if (!v) return null
    try {
      const sk = v.seekable
      if (!sk || !sk.length) return null
      const l = sk.end(sk.length - 1) - v.currentTime
      return Number.isFinite(l) && l > 0 ? l : 0
    } catch {
      return null
    }
  }, [])

  const stop = useCallback((why?: string) => {
    onRef.current = false
    if (timerRef.current) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
    const v = videoRef.current
    try {
      v?.pause()
      v?.removeAttribute('src')
      v?.load()
    } catch {
      /* 忽略 */
    }
    msRef.current = null
    sbRef.current = null
    seenRef.current = new Set()
    setOn(false)
    setMeta('')
    setHint(why || '已停止。服务端无人观看 30 秒后自动回收设备端编码。')
  }, [])

  /** MSE 播放：init 段 → 循环 pump 分片 → 有数据了再点火。
   *  顺序反了会死锁（play() 等数据、数据等 pump）。 */
  const playMse = useCallback(
    async (v: HTMLVideoElement): Promise<void> => {
      const mime = codecCandidates(infoRef.current?.codec).find((c) => window.MediaSource?.isTypeSupported(c))
      if (!mime) {
        showToast('浏览器不支持该视频编码，请改用 Safari 或手机端观看', 3600)
        throw new Error('no supported codec')
      }
      const ms = new MediaSource()
      msRef.current = ms
      seenRef.current = new Set()
      v.src = URL.createObjectURL(ms)
      await new Promise<void>((r) => ms.addEventListener('sourceopen', () => r(), { once: true }))
      const sb = ms.addSourceBuffer(mime)
      sbRef.current = sb

      const initBuf = await (await fetch(`/stream/init.mp4?t=${Date.now()}`, { cache: 'no-store' })).arrayBuffer()
      await new Promise<void>((r) => {
        sb.addEventListener('updateend', () => r(), { once: true })
        sb.appendBuffer(initBuf)
      })

      const pump = async (): Promise<void> => {
        if (!onRef.current) return
        try {
          const txt = await (
            await fetch(`/stream/index.m3u8?t=${Date.now()}`, { cache: 'no-store' })
          ).text()
          const segs = txt
            .split('\n')
            .map((s) => s.trim())
            .filter((s) => s && s.charAt(0) !== '#')
          // **全部未见分片都要 append**：HLS 分片按时间切（非关键帧起始），只补最新两片会
          // 缺参考帧 → MSE 解不出来（黑屏）。延迟靠追帧与缓冲裁剪控制，不靠丢分片。
          for (const s of segs) {
            if (!onRef.current) return
            if (seenRef.current.has(s)) continue
            seenRef.current.add(s)
            const buf = await (await fetch(`/stream/${s}`, { cache: 'no-store' })).arrayBuffer()
            try {
              await new Promise<void>((r) => {
                sb.addEventListener('updateend', () => r(), { once: true })
                sb.appendBuffer(buf)
              })
              if (!v.currentTime) v.play().catch(() => {}) // 有数据了再点火
            } catch {
              seenRef.current.delete(s) // 失败允许下轮重试
            }
          }
          // 缓冲只留最近约 3 秒：无上限增长会撞 QuotaExceeded，之后新分片全追加不进
          if (sb.buffered.length) {
            const st = sb.buffered.start(0)
            const en = sb.buffered.end(sb.buffered.length - 1)
            if (en - st > 10) {
              const cut = Math.max(st, v.currentTime - 1)
              if (cut > st) {
                try {
                  sb.remove(st, cut)
                } catch {
                  /* 忽略 */
                }
              }
            }
          }
        } catch {
          /* 网络抖动忽略，下轮重试 */
        }
        window.setTimeout(() => void pump(), 300)
      }
      void pump()
      v.play().catch(() => {})
    },
    [showToast],
  )

  const start = useCallback(async () => {
    if (timerRef.current) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
    const st = await streamStatus()
    infoRef.current = st
    const v = videoRef.current
    if (!st.running && st.error) {
      setHint(`启动失败：${st.error}`)
      return
    }
    setHint('缓冲中…（设备端采集启动约 3~7 秒）')
    onRef.current = true
    // 播放路径：**不能**只看 canPlayType 的真值 —— Chrome 对 HLS 返回 "maybe"（真值！）
    // 会误走原生分支而 Chrome 并不支持 HLS（黑屏）。只有 Safari/WebKit 返回 "probably"。
    const canNative = v?.canPlayType('application/vnd.apple.mpegurl') === 'probably'
    try {
      if (isIOS() || (!window.MediaSource && canNative)) {
        if (v) {
          v.src = `/stream/index.m3u8?t=${Date.now()}`
          void v.play().catch(() => {})
        }
      } else if (window.MediaSource && v) {
        await playMse(v)
      } else {
        onRef.current = false
        setHint('当前浏览器既不支持 MSE 也不支持原生 HLS，请用 Chrome / Safari')
        return
      }
    } catch (e) {
      onRef.current = false
      setHint(`直播启动失败：${e instanceof Error ? e.message : String(e)}`)
      return
    }
    setOn(true)
    setHint('')

    // 心跳：保活 + 刷新尺寸 + 延迟看门狗 + 接管失效检测
    timerRef.current = window.setInterval(async () => {
      if (!onRef.current) return
      try {
        await fetch('/api/stream/ping', { method: 'POST' })
      } catch {
        /* 忽略 */
      }
      const s = await streamStatus()
      infoRef.current = s
      const l = lag()
      setMeta(`${l !== null ? `延迟 ${l.toFixed(1)}s · ` : ''}${s.running ? `流运行中 ${s.uptime || 0}s` : '流已停止'}`)
      if (!s.running && s.error) setHint(`流异常：${s.error}`)
      // 追帧：落后 > 1.0s 就跳到直播边缘（3 秒内最多一次，避免频繁 seek 让播放器反复重缓冲）
      if (l !== null && l > 1.0 && Date.now() - lastSeekRef.current > 3000) {
        lastSeekRef.current = Date.now()
        const el = videoRef.current
        try {
          if (el) el.currentTime = Math.max(0, el.seekable.end(el.seekable.length - 1) - 0.2)
        } catch {
          /* 忽略 */
        }
        showToast(`已追到直播边缘（原落后 ${l.toFixed(1)}s）`, 1600)
      }
      // 别处（仪表盘/GUI）又把调度器拉起来时，注入会被服务端拒绝 —— 这里同步失效
      if (takeoverRef.current && s.scheduler_alive && !s.scheduler_paused) {
        takeoverRef.current = false
        setTakeover(false)
        showToast('调度器已恢复运行，接管失效（需要再点一次接管）', 2800)
      }
    }, 3000)
  }, [lag, playMse, showToast])

  // 离开页面/隐藏时停流，让服务端尽快回收（挂机不占设备编码器）
  useEffect(() => () => stop(), [stop])

  const toggleTakeover = useCallback(async () => {
    if (takeover) {
      takeoverRef.current = false
      setTakeover(false)
      try {
        await runnerResume()
      } catch {
        /* 忽略 */
      }
      showToast('已交还控制权，调度器继续跑', 2600)
      return
    }
    const st = await streamStatus()
    if (!st.scheduler_alive) {
      takeoverRef.current = true
      setTakeover(true)
      showToast('已接管（调度器未运行）', 2600)
      return
    }
    const r = await runnerPause()
    if (!r.ok) {
      showToast(`接管失败：${r.msg || '未知原因'}`)
      return
    }
    // 让路在"当前步骤完成后"生效：轮询等它真停下（最多 60 秒），期间不打断任务
    setHint('调度器正在完成当前步骤，随后让路…（不会中断任务、不丢进度）')
    for (let i = 0; i < 40; i++) {
      await new Promise((res) => window.setTimeout(res, 1500))
      const s2 = await streamStatus()
      if (!s2.scheduler_alive || s2.scheduler_paused) {
        takeoverRef.current = true
        setTakeover(true)
        setHint('')
        showToast('已接管：调度器已让路（任务未中断）', 3000)
        return
      }
    }
    showToast('调度器 60 秒内未让路；可回总览页点「停止」强制结束', 3800)
  }, [showToast, takeover])

  /** 点画面 → 注入操作。绑在**透明覆盖层**上（iOS 的 <video> 会吞掉 click），
   *  且每个分支都要给反馈 —— 否则"点了没反应"无从排查。 */
  const onTap = useCallback(
    async (ev: React.PointerEvent<HTMLDivElement>) => {
      if (!onRef.current) {
        showToast('请先点「开始直播」')
        return
      }
      if (!takeover) {
        showToast('请先点「接管操作」（会让调度器让路）')
        return
      }
      const d = await dims()
      if (!d) {
        showToast('还没拿到画面尺寸，等 1~2 秒再点')
        return
      }
      const v = videoRef.current
      if (!v) return
      const rect = v.getBoundingClientRect()
      const [vw, vh] = d.video
      const [dw, dh] = d.device
      // 视频在容器里是 object-fit: contain，两侧/上下可能有黑边，换算时要先扣掉
      const sc = Math.min(rect.width / vw, rect.height / vh) || 1
      const vidX = (ev.clientX - rect.left - (rect.width - vw * sc) / 2) / sc
      const vidY = (ev.clientY - rect.top - (rect.height - vh * sc) / 2) / sc
      const x = Math.round((vidX * dw) / vw)
      const y = Math.round((vidY * dh) / vh)
      try {
        const r = (await (
          await fetch('/api/stream/tap', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ x, y }),
          })
        ).json()) as { ok?: boolean; msg?: string }
        if (r?.ok) {
          showToast(`已点击 (${x}, ${y})`)
          return
        }
        const msg = r?.msg || '未知原因'
        showToast(`注入被拒：${msg}`, 2600)
        // 流被空闲回收/断开时自动重连，避免"操作一会儿突然就不能动"
        if (msg.includes('流未运行') && !restartingRef.current) {
          restartingRef.current = true
          setHint('流已断开，正在自动重连…')
          try {
            await start()
          } finally {
            restartingRef.current = false
          }
        }
      } catch (e) {
        showToast(`请求失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },
    [dims, showToast, start, takeover],
  )

  return (
    <>
      <div className="scenecard livecard">
        <video ref={videoRef} playsInline muted />
        {/* 透明覆盖层接收点击（iOS 的 <video> 会吞掉 click/touch） */}
        <div className="livetap" onPointerDown={(e) => void onTap(e)} />
        {hint ? <div className="livehint">{hint}</div> : null}
        <div className={'livetoast' + (toast ? ' on' : '')}>{toast}</div>
      </div>
      <div className="shotpage-ctl">
        <button
          className={'savebtn' + (on ? ' on' : '')}
          onClick={() => (on ? stop() : void start())}
        >
          {on ? '停止直播' : '开始直播'}
        </button>
        <button
          className={'savebtn' + (takeover ? ' on' : '')}
          onClick={() => void toggleTakeover()}
        >
          {takeover ? '已接管·点击生效' : '接管操作'}
        </button>
        <span>{meta}</span>
      </div>
    </>
  )
}
