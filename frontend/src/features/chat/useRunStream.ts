import { onScopeDispose, ref, shallowRef, triggerRef } from 'vue'
import { api, base, readError, ApiError } from '../../api/client'
import { initialStream, reduceEvent, SSEParser } from './stream'
export function useRunStream(onSync: () => Promise<unknown>) {
  const state = shallowRef(initialStream(''))
  const warning = ref('')
  const connected = ref(false)
  let controller: AbortController | undefined
  function stop() {
    controller?.abort()
    controller = undefined
    connected.value = false
  }
  async function start(runId: string, replay = false) {
    stop()
    if (state.value.runId !== runId || replay) state.value = initialStream(runId)
    const own = (controller = new AbortController())
    warning.value = ''
    let failures = 0
    while (!own.signal.aborted && failures < 6) {
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
      try {
        const response = await fetch(`${base}/runs/${runId}/events?after=${state.value.seq}`, {
          signal: own.signal,
        })
        if (!response.ok) throw await readError(response)
        if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream'))
          throw new Error('任务事件连接格式无效')
        connected.value = true
        warning.value = ''
        const parser = new SSEParser((event) => {
          if (own.signal.aborted) return
          if (event.event === 'stream_error')
            throw new ApiError(
              event.data.http_status || 503,
              event.data.code || 'stream_error',
              event.data.message || '事件读取失败',
              undefined,
              event.data.retryable !== false,
            )
          reduceEvent(state.value, event)
          triggerRef(state)
        })
        reader = response.body.getReader()
        const decoder = new TextDecoder()
        while (!own.signal.aborted) {
          const chunk = await reader.read()
          if (chunk.done) {
            parser.push(decoder.decode(), true)
            break
          }
          parser.push(decoder.decode(chunk.value, { stream: true }))
          if (state.value.terminal) break
        }
        if (own.signal.aborted) return
        const snapshot = await api.run(runId)
        if (own.signal.aborted) return
        await onSync()
        if (!['queued', 'running'].includes(snapshot.status)) {
          connected.value = false
          return
        }
        throw new Error('事件连接已断开，正在重新连接')
      } catch (error) {
        if (own.signal.aborted) return
        connected.value = false
        warning.value = error instanceof Error ? error.message : '事件连接失败'
        if (
          error instanceof ApiError &&
          !error.retryable &&
          [400, 404, 409, 422].includes(error.status)
        ) {
          await onSync()
          return
        }
        failures++
        // Reconcile the task even when SSE is unavailable.
        try {
          const snapshot = await api.run(runId)
          if (own.signal.aborted) return
          await onSync()
          if (!['queued', 'running'].includes(snapshot.status)) return
        } catch {
          /* retry below */
        }
        if (own.signal.aborted) return
        await new Promise<void>((resolve) => {
          const timer = setTimeout(done, Math.min(1000 * 2 ** (failures - 1), 15000))
          function done() {
            clearTimeout(timer)
            own.signal.removeEventListener('abort', done)
            resolve()
          }
          own.signal.addEventListener('abort', done, { once: true })
        })
      } finally {
        await reader?.cancel().catch(() => {})
        reader?.releaseLock()
      }
    }
    if (!own.signal.aborted)
      warning.value = '实时连接暂不可用，任务可能仍在运行。点击重新连接获取最新状态。'
  }
  onScopeDispose(stop)
  return { state, warning, connected, start, stop }
}
