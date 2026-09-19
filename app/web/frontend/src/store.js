// 轻量响应式全局状态（规模小，不引入 Pinia）
import { reactive } from 'vue'

export const store = reactive({
  wsStatus: 'connecting', // connecting | open | closed
  threadId: '',
  messages: [], // {id, role: 'user'|'assistant', text?, result?|error?}
  sending: false,
  sessions: [],
  showSettings: false,
})

let ws = null
let reconnectTimer = null

export function connectWs() {
  clearTimeout(reconnectTimer)
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws/chat`)
  store.wsStatus = 'connecting'
  ws.onopen = () => { store.wsStatus = 'open' }
  ws.onclose = () => {
    store.wsStatus = 'closed'
    reconnectTimer = setTimeout(connectWs, 3000)
  }
  ws.onerror = () => ws.close()
  ws.onmessage = (event) => handleWsMessage(JSON.parse(event.data))
}

function handleWsMessage(msg) {
  if (msg.type === 'session') { store.threadId = msg.threadId; return }
  if (msg.type === 'start') {
    store.sending = true
    store.messages.push({
      id: `m_${Date.now()}`, role: 'assistant', phase: 'running',
      text: msg.message || '协作中...',
    })
    return
  }
  if (msg.type === 'result') {
    store.sending = false
    const running = [...store.messages].reverse().find((m) => m.phase === 'running')
    if (running) running.phase = 'done'
    store.messages.push({
      id: `r_${Date.now()}`, role: 'assistant', phase: 'done', result: msg,
    })
    refreshSessions()
    return
  }
  if (msg.type === 'error') {
    store.sending = false
    const running = [...store.messages].reverse().find((m) => m.phase === 'running')
    if (running) running.phase = 'error'
    store.messages.push({
      id: `e_${Date.now()}`, role: 'assistant', phase: 'error',
      text: `出错：${msg.message}`,
    })
  }
}

export function sendChat(message) {
  if (!message.trim() || store.wsStatus !== 'open' || store.sending) return
  store.messages.push({ id: `u_${Date.now()}`, role: 'user', text: message.trim() })
  ws.send(JSON.stringify({ type: 'chat', message: message.trim(), threadId: store.threadId || undefined }))
}

export function newSession() {
  if (store.wsStatus !== 'open') return
  store.messages = []
  ws.send(JSON.stringify({ type: 'new_session' }))
}

export async function refreshSessions() {
  try {
    const res = await fetch('/api/sessions')
    store.sessions = await res.json()
  } catch { /* 服务未就绪时静默 */ }
}

/** 切换到一个历史会话：告诉后端改用它，并把历史消息拉回来回放。 */
export async function loadSession(threadId) {
  if (store.wsStatus !== 'open' || store.sending) return
  ws.send(JSON.stringify({ type: 'load_session', threadId }))
  store.messages = []
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(threadId)}/messages`)
    const data = await res.json()
    store.messages = (data.messages || []).map((m, i) => ({
      id: `h_${threadId}_${i}`,
      role: m.role,
      phase: 'done',
      text: m.content,
      fromHistory: true,
    }))
  } catch { /* 服务未就绪时静默 */ }
}

export async function loadSettings() {
  const res = await fetch('/api/settings')
  return res.json()
}

export async function saveSettings(payload) {
  const res = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return res.json()
}

export async function testSettings(payload) {
  const res = await fetch('/api/settings/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return res.json()
}
