export interface StreamEvent {
  id?: string
  event: string
  data: Record<string, any>
}
export interface StreamState {
  runId: string
  seq: number
  messageId: string
  drafts: Record<string, string>
  phase: string
  details: Record<string, any>
  terminal: boolean
}
export function initialStream(runId: string): StreamState {
  return {
    runId,
    seq: 0,
    messageId: '',
    drafts: {},
    phase: '连接任务',
    details: {},
    terminal: false,
  }
}
export function reduceEvent(state: StreamState, event: StreamEvent): boolean {
  const match = event.id?.match(/^(.*):(\d+)$/)
  if (match) {
    if (match[1] !== state.runId || Number(match[2]) <= state.seq) return false
  } else if (event.event !== 'stream_error') return false
  const d = event.data
  if (d.run_id && d.run_id !== state.runId) return false
  switch (event.event) {
    case 'start':
      state.phase = '等待执行'
      break
    case 'query_plan':
      state.phase = '查询规划完成'
      state.details.query_plan = d
      break
    case 'retrieval_start':
      state.phase = '正在检索资料'
      break
    case 'retrieval_end':
      state.phase = '资料检索完成'
      state.details.retrieval = d
      break
    case 'context_assembly':
      state.phase = '上下文已就绪'
      state.details.context_assembly = d
      break
    case 'message_start':
      state.messageId = d.message_id
      state.drafts[d.message_id] ??= ''
      state.phase = '正在生成回答'
      break
    case 'answer_start':
      state.phase = '正在生成可引用的答案'
      break
    case 'answer_repair':
      state.phase = '正在修正回答格式'
      state.details.answer_repair = d
      break
    case 'answer_validation':
      state.phase = d.valid ? '回答校验完成' : '正在校验来源关联'
      state.details.answer_validation = d
      break
    case 'text_delta':
      if (typeof d.text === 'string' && d.message_id) {
        state.messageId = d.message_id
        state.drafts[d.message_id] = (state.drafts[d.message_id] || '') + d.text
      }
      break
    case 'message_reset':
      delete state.drafts[d.message_id]
      if (state.messageId === d.message_id) state.messageId = ''
      break
    case 'tool_start':
      state.phase = `正在调用工具 · ${d.tool_name}`
      break
    case 'tool_end':
      state.phase = '工具调用完成'
      break
    case 'token_usage':
      state.details.token_usage = d
      break
    case 'resumed':
      state.phase = '继续执行'
      state.terminal = false
      break
    case 'paused':
      state.phase = '已暂停'
      state.terminal = true
      break
    case 'recovery_required':
      state.phase = '等待恢复'
      state.terminal = true
      break
    case 'done':
      state.phase = '已完成'
      state.terminal = true
      break
    case 'error':
      state.phase = '任务失败'
      state.terminal = true
      break
    case 'cancelled':
      state.phase = '已取消'
      state.terminal = true
      break
  }
  if (match) state.seq = Number(match[2])
  return true
}
export class SSEParser {
  private buffer = ''
  private event = 'message'
  private id: string | undefined
  private data: string[] = []
  constructor(private emit: (event: StreamEvent) => void) {}
  push(text: string, end = false) {
    this.buffer += text
    while (true) {
      const index = this.buffer.search(/[\r\n]/)
      if (index < 0 || (!end && this.buffer[index] === '\r' && index === this.buffer.length - 1))
        break
      const line = this.buffer.slice(0, index)
      const length = this.buffer[index] === '\r' && this.buffer[index + 1] === '\n' ? 2 : 1
      this.buffer = this.buffer.slice(index + length)
      this.line(line)
    }
  }
  private line(line: string) {
    if (line === '') {
      if (this.data.length) {
        const data: unknown = JSON.parse(this.data.join('\n'))
        if (!data || typeof data !== 'object' || Array.isArray(data))
          throw new Error('无效的任务事件')
        this.emit({ event: this.event, id: this.id, data: data as Record<string, any> })
      }
      this.data = []
      this.event = 'message'
      this.id = undefined
      return
    }
    if (line.startsWith(':')) return
    const split = line.indexOf(':')
    const name = split === -1 ? line : line.slice(0, split)
    const value = split === -1 ? '' : line.slice(split + 1).replace(/^ /, '')
    if (name === 'event') this.event = value
    if (name === 'id' && !value.includes('\0')) this.id = value
    if (name === 'data') this.data.push(value)
  }
}
