import { describe, expect, it } from 'vitest'
import { initialStream, reduceEvent, SSEParser } from '../src/features/chat/stream'

describe('SSE framing', () => {
  it('handles CRLF split across chunks, multiline JSON and comment heartbeats', () => {
    const events: unknown[] = []
    const parser = new SSEParser((e) => events.push(e))
    for (const char of ': heartbeat\r\n\r\nid: run:1\r\nevent: text_delta\r\ndata: {"text":\r\ndata: "你好", "message_id":"a"}\r\n\r\n')
      parser.push(char)
    expect(events).toEqual([
      { id: 'run:1', event: 'text_delta', data: { text: '你好', message_id: 'a' } },
    ])
  })
  it('does not publish incomplete frames on EOF', () => {
    const events: unknown[] = []
    const parser = new SSEParser((e) => events.push(e))
    parser.push('id: r:1\nevent: done\ndata: {"answer":"draft"}\n', true)
    expect(events).toEqual([])
  })
})
describe('durable event reducer', () => {
  it('keeps structured answer progress and repair events out of user drafts', () => {
    const s = initialStream('run')
    reduceEvent(s, {
      id: 'run:1',
      event: 'answer_start',
      data: { answer_protocol: 'rag-answer-v2' },
    })
    expect(s.phase).toBe('正在生成可引用的答案')
    reduceEvent(s, {
      id: 'run:2',
      event: 'answer_repair',
      data: { error_code: 'invalid_citations' },
    })
    expect(s.phase).toBe('正在修正回答格式')
    reduceEvent(s, { id: 'run:3', event: 'answer_validation', data: { valid: true } })
    expect(s.phase).toBe('回答校验完成')
    expect(s.drafts).toEqual({})
    expect(s.terminal).toBe(false)
  })
  it('retains assembly diagnostics when replaying durable events', () => {
    const s = initialStream('run')
    const diagnostics = { assembly_version: 'context-v1', source_count: 2, history_turns: 3 }
    reduceEvent(s, { id: 'run:1', event: 'context_assembly', data: diagnostics })
    expect(s.details.context_assembly).toEqual(diagnostics)
    expect(s.phase).toBe('上下文已就绪')
    expect(s.terminal).toBe(false)
  })
  it('deduplicates replay and ignores a different run', () => {
    const s = initialStream('run')
    const event = { id: 'run:1', event: 'text_delta', data: { message_id: 'a', text: '第一段' } }
    reduceEvent(s, event)
    reduceEvent(s, event)
    reduceEvent(s, { ...event, id: 'other:2' })
    expect(s.drafts.a).toBe('第一段')
    expect(s.seq).toBe(1)
  })
  it('keeps attempts separate and resets only the intended draft', () => {
    const s = initialStream('run')
    reduceEvent(s, { id: 'run:1', event: 'text_delta', data: { message_id: 'a', text: 'old' } })
    reduceEvent(s, { id: 'run:2', event: 'text_delta', data: { message_id: 'b', text: 'new' } })
    reduceEvent(s, { id: 'run:3', event: 'message_reset', data: { message_id: 'a' } })
    expect(s.drafts).toEqual({ b: 'new' })
    expect(s.messageId).toBe('b')
  })
  it('stream_error does not advance cursor or mark business failure', () => {
    const s = initialStream('run')
    reduceEvent(s, { event: 'stream_error', data: { message: 'database unavailable' } })
    expect(s.seq).toBe(0)
    expect(s.terminal).toBe(false)
    reduceEvent(s, { id: 'run:1', event: 'paused', data: {} })
    expect(s.terminal).toBe(true)
    reduceEvent(s, { id: 'run:2', event: 'resumed', data: {} })
    expect(s.terminal).toBe(false)
  })
})
