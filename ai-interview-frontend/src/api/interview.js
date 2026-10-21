import api from './request'

export function startInterview(data) {
  return api.post('/interviews/start', data)
}

export function submitAnswer(interviewId, answer) {
  return api.post(`/interviews/${interviewId}/answer`, { answer })
}

export async function submitAnswerStream(interviewId, answer, onChunk, onDone) {
  const authStore = (await import('../stores/auth')).useAuthStore()
  const response = await fetch(`/api/v1/interviews/${interviewId}/answer/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${authStore.token}`
    },
    body: JSON.stringify({ answer })
  })

  if (!response.ok) throw new Error(`提交失败（HTTP ${response.status}），请核对服务器记录`)
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new Error('服务器没有返回有效事件流，请核对提交结果')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let receivedDone = false
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      if (done && buffer.trim()) lines.push(buffer)
      for (const line of lines) {
        if (!line.startsWith('data:')) continue
        const data = JSON.parse(line.slice(5).trim())
        if (data.type === 'chunk') onChunk(data.content)
        else if (data.type === 'done') {
          receivedDone = true
          onDone(data)
          await reader.cancel().catch(() => {})
          return
        } else if (data.type === 'error') {
          const error = new Error(data.content || '评估失败')
          error.retrySafe = data.retry_safe === true
          throw error
        }
      }
      if (done) break
    }
    if (!receivedDone) throw new Error('事件流提前结束，提交结果未确认')
  } finally {
    reader.releaseLock()
  }
}

export function getReport(interviewId) {
  return api.get(`/interviews/${interviewId}/report`)
}

export function getMessages(interviewId) {
  return api.get(`/interviews/${interviewId}/messages`)
}

export function getInterviews() {
  return api.get('/interviews')
}

export function deleteInterview(interviewId) {
  return api.delete(`/interviews/${interviewId}`)
}
