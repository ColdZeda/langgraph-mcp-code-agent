// 轻量响应式全局状态（规模小，不引入 Pinia）
import { reactive } from 'vue'

export const store = reactive({
  wsStatus: 'connecting', // connecting | open | closed
  threadId: '',
  // 阶段 8 · 实测修复：**执行中不许切会话**（点了就提示，不再静默吞掉）
  notice: '',
  mode: 'auto', // 执行模式：auto | single | multi（由界面下拉框切换）
  // 权限模式（阶段 5）：readonly | confirm | open。
  // ⚠️ 与「执行模式」是**两条独立的轴**，名字不能都叫"模式"。
  permissionMode: 'confirm',
  permissionModes: [
    { value: 'readonly', label: '只读' },
    { value: 'confirm', label: '需确认' },
    { value: 'open', label: '放开' },
  ],
  confirmTimeoutSec: 120,
  // 阶段 7 · T7.6：**有没有可用的模型**（后端在 session 消息里给）。
  // 没有 ⇒ 界面显示"去添加你的 API Key"并禁用发送（新用户 clone 下来的默认状态）。
  modelReady: true,
  modelError: '',
  // 当前待人工确认的请求（有值 → 弹框）。结构见后端 permission_request 消息。
  permissionRequest: null,
  // ⚠️ 阶段 5 修：**待确认请求要排队**。以前只存一个槽位 ——
  //    两个 permission_request 同时到达时，后一个会把前一个**覆盖**掉，
  //    用户就永远答不上它（只能等后端超时自动拒绝）。
  permissionQueue: [],
  // 阶段 5（T5.6）：节点级进度（Planner/Executor 每步/Verifier），任务开始清空
  progress: [],
  // 阶段 8 · P0：任务状态条（时长 / 步数 / 用量）。
  // 由后端 `status` 事件刷新；两次事件之间的那 1 秒由本地计时器补 ——
  // ⚠️ 弹出权限弹框时**冻结**（与后端口径一致：人工确认的等待不算任务时间）。
  status: null,
  // 已发出「停止」请求、正等任务在下一个检查点停下（按钮据此变成"停止中…"）
  stopping: false,
  messages: [], // {id, role: 'user'|'assistant', text?, result?|error?}
  sending: false,
  sessions: [],
  // 阶段 7（会话管理）：`counts` = {visible, hidden, eval}，决定两行小字的显隐；
  // 两个开关是"要不要把回收站 / 系统线程也拉下来"（默认都不显示）。
  sessionCounts: { visible: 0, hidden: 0, eval: 0 },
  showHidden: false,
  showEval: false,
  showSettings: false,
  showKnowledge: false,
  // 「当前生效模型」：四个角色**解析后**各自会用哪个（后端算好给前端 —— 兜底链有三层，
  // 前端自己拼容易算错）。阶段 6 加的：模型是全局配置、不随会话保存，
  // 不显示的话用户不知道"现在到底是谁在干活"（用户实测后提的需求）。
  effectiveModels: {},
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

/** 把后端节点事件翻译成一行给人看的文字（文案在前端，后端只发结构化字段）。 */
function progressLine(e) {
  const tool = (e.tools || []).join('、')
  switch (e.node) {
    case 'route':
      return e.status === 'end'
        ? `路由：${e.route === 'simple' ? '简单任务（只跑 Executor）' : '复杂任务（走完整三阶段）'}`
        : '判断任务复杂度…'
    case 'planner':
      if (e.status === 'end' && e.cancelled) return '规划已取消（任务已停止）'
      return e.status === 'end' ? `计划已就绪（${e.steps} 步）` : 'Planner 规划中…'
    case 'executor':
      if (e.status === 'step') return `第 ${e.step} 步${tool ? `：调用 ${tool}` : ''}`
      if (e.status === 'end') {
        // 阶段 8 · P0：停止与"运行结束""预算终止"是三种不同的事，文案必须分开
        if (e.cancelled) return `Executor 已停止（第 ${e.steps} 步停下）`
        return e.budgetExceeded ? 'Executor 因 token 预算终止' : `Executor 完成（${e.steps} 步）`
      }
      return e.retry ? 'Executor 重跑中…' : 'Executor 开始执行…'
    case 'verifier':
      if (e.status === 'end') {
        if (e.cancelled) return '验收已停止'
        return e.passed ? '验收通过 ✅' : '验收未通过 ❌'
      }
      return 'Verifier 验收中…'
    default:
      return `${e.node} ${e.status}`
  }
}

// ── 阶段 8 · P0：任务状态条的本地计时 ────────────────────────────────
// 后端每走一步推一条 `status`（权威值），中间那 1 秒本地补 —— 不然计时看起来是"卡住的"。
// ⚠️ 有权限弹框时**不补**：后端在人工确认期间暂停计时，前端跟着暂停才对得上。
let tickTimer = null
function startTicker() {
  clearInterval(tickTimer)
  tickTimer = setInterval(() => {
    if (!store.sending || !store.status || store.permissionRequest) return
    store.status = { ...store.status, elapsedSec: (store.status.elapsedSec || 0) + 1 }
  }, 1000)
}
function stopTicker() {
  clearInterval(tickTimer)
  tickTimer = null
}

function handleWsMessage(msg) {
  if (msg.type === 'session') {
    store.threadId = msg.threadId
    // 权限模式由后端给（新会话取持久化值；「放开」按 D4 永不从这里回来）
    if (msg.permissionMode) store.permissionMode = msg.permissionMode
    if (Array.isArray(msg.permissionModes) && msg.permissionModes.length) {
      store.permissionModes = msg.permissionModes
    }
    if (msg.confirmTimeoutSec) store.confirmTimeoutSec = msg.confirmTimeoutSec
    // 阶段 7 · T7.6：后端告诉我们"现在到底有没有可用的模型"——
    // 没有的话界面要显示引导并禁用发送（全新用户 clone 下来就是这个状态）。
    if (typeof msg.modelReady === 'boolean') store.modelReady = msg.modelReady
    if (typeof msg.modelError === 'string') store.modelError = msg.modelError
    return
  }
  // 人工确认（阶段 5）：后端要求确认某个工具调用 → 弹框（多个请求排队，逐个问）
  if (msg.type === 'permission_request') {
    if (store.permissionRequest) store.permissionQueue.push(msg)
    else store.permissionRequest = msg
    return
  }
  if (msg.type === 'start') {
    store.sending = true
    store.stopping = false
    store.progress = []
    // 状态条先按 0 起（墙钟上限来自后端，不在这里写死）
    store.status = {
      elapsedSec: 0, pausedSec: 0, steps: 0, tokens: 0, usageLimit: 0,
      wallClockSec: msg.wallClockSec || 0, stage: '', cancelled: false,
    }
    startTicker()
    store.messages.push({
      id: `m_${Date.now()}`, role: 'assistant', phase: 'running',
      text: msg.message || '协作中...',
    })
    return
  }
  // 阶段 8：后端已收到停止请求（协作式：会在下一个检查点停，不是立刻掐断）
  if (msg.type === 'stopping') {
    store.stopping = true
    store.progress.push({ id: `p_stop_${Date.now()}`, text: '已请求停止：正在下一步边界停下…' })
    return
  }
  // 阶段 8 · P0：任务状态条（时长 / 步数 / 用量）—— 后端每步推一次，权威值
  if (msg.type === 'status') {
    if (store.sending) store.status = { ...(store.status || {}), ...msg }
    return
  }
  // 阶段 5（T5.6）：节点级进度 —— 任务进行中就能看到走到哪一步了
  if (msg.type === 'node') {
    store.progress.push({ id: `p_${Date.now()}_${store.progress.length}`, text: progressLine(msg) })
    if (store.progress.length > 40) store.progress.shift()
    return
  }
  if (msg.type === 'result') {
    store.sending = false
    store.stopping = false
    stopTicker()
    store.status = null // 结果卡片里有最终数字，状态条不重复显示
    // 任务结束 → 弹框与队列都该清掉（后端不会再等它们了）
    store.permissionRequest = null
    store.permissionQueue = []
    // ⚠️ 阶段 7（T7.5 走查发现）：**那条"协作中…"占位气泡要删掉**，不能只把 phase 改成 done。
    //    改 phase 的话它还留在列表里，而 ChatView 的最后一条分支要求 `m.text` ——
    //    于是任务跑完后永远挂着一句"Planner → Executor → Verifier 协作中…"（实测截图里就是）。
    store.messages = store.messages.filter((m) => m.phase !== 'running')
    store.messages.push({
      id: `r_${Date.now()}`, role: 'assistant', phase: 'done', result: msg,
    })
    refreshSessions()
    return
  }
  if (msg.type === 'error') {
    store.sending = false
    store.stopping = false
    stopTicker()
    store.status = null
    store.permissionRequest = null
    store.permissionQueue = []
    // 同上的理由：把**那一条**占位气泡改成错误气泡（以前是"改 phase + 再 push 一条"，
    // 结果是两条错误气泡叠在一起）。
    const running = [...store.messages].reverse().find((m) => m.phase === 'running')
    if (running) {
      running.phase = 'error'
      running.text = `出错：${msg.message}`
    } else {
      store.messages.push({
        id: `e_${Date.now()}`, role: 'assistant', phase: 'error',
        text: `出错：${msg.message}`,
      })
    }
  }
}

export function sendChat(message) {
  if (!message.trim() || store.wsStatus !== 'open' || store.sending) return
  store.messages.push({ id: `u_${Date.now()}`, role: 'user', text: message.trim() })
  ws.send(
    JSON.stringify({
      type: 'chat',
      message: message.trim(),
      threadId: store.threadId || undefined,
      mode: store.mode,
      permissionMode: store.permissionMode,
    })
  )
}

/** 回答一次人工确认（阶段 5）。`alwaysAllow` 对应「本会话内对该工具总是允许」勾选框。 */
export function respondPermission({ requestId, allow, alwaysAllow = false }) {
  if (store.wsStatus !== 'open') return
  ws.send(JSON.stringify({ type: 'permission_response', requestId, allow, alwaysAllow }))
  // 队列里还有就接着问下一个（没有则收起弹框）
  store.permissionRequest = store.permissionQueue.shift() || null
}

/**
 * 阶段 8 · P0：停止当前任务。
 *
 * ⚠️ **协作式**：后端在下一个检查点（节点入口 / ReAct 每一步 / 每次工具调用前）才停，
 * 正在飞的那一次模型调用不会被打断 —— 所以按钮会先变成「停止中…」。
 * 若此刻正卡在权限弹框上，后端会立刻把弹框收掉再停（不用等确认超时）。
 */
export function stopTask() {
  if (store.wsStatus !== 'open' || !store.sending || store.stopping) return
  store.stopping = true
  ws.send(JSON.stringify({ type: 'stop' }))
}

/** 切换权限模式：本连接立即生效，并按 D4 规则持久化（「放开」只生效不落盘）。 */
export function setPermissionMode(mode) {
  store.permissionMode = mode
  if (store.wsStatus === 'open') {
    ws.send(JSON.stringify({ type: 'set_permission_mode', mode }))
  }
}

export function newSession() {
  if (store.wsStatus !== 'open') return
  // ⚠️ 2026-10-07 实测：这里原来**没有** `sending` 守卫 ⇒ 执行中点它会清空本地消息并换 thread_id，
  //    旧任务的 node/status/result 帧随后串进新会话（用户看到的就是"点不回去"）。
  if (store.sending) {
    setNotice('当前任务正在执行，请先点「停止任务」或等它跑完，再新建会话')
    return
  }
  store.messages = []
  ws.send(JSON.stringify({ type: 'new_session' }))
}

export async function refreshSessions() {
  try {
    // 阶段 7：接口返回**对象**（items + counts）—— counts 决定"回收站 (N)"
    // 与"显示系统线程 (N)"这两行小字要不要出现（都是 0 时一个字都不显示）。
    const params = new URLSearchParams()
    if (store.showHidden) params.set('include_hidden', '1')
    if (store.showEval) params.set('include_eval', '1')
    const qs = params.toString()
    const res = await fetch(`/api/sessions${qs ? `?${qs}` : ''}`)
    const data = await res.json()
    store.sessions = data.items || []
    store.sessionCounts = data.counts || { visible: 0, hidden: 0, eval: 0 }
  } catch { /* 服务未就绪时静默 */ }
}

/** 改会话标题（后端会把 title_source 记为 user —— 之后自动标题不再覆盖它）。 */
export async function renameSession(threadId, title) {
  const res = await fetch(`/api/sessions/${encodeURIComponent(threadId)}/title`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
  await refreshSessions()
  return res.ok
}

/** 置顶 / 取消置顶。 */
export async function togglePinSession(threadId, pinned) {
  await fetch(`/api/sessions/${encodeURIComponent(threadId)}/pin`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pinned }),
  })
  await refreshSessions()
}

/** 软删除：进回收站（checkpoint 一行都不少，可「恢复」）。 */
export async function softDeleteSession(threadId) {
  const res = await fetch(`/api/sessions/${encodeURIComponent(threadId)}`, { method: 'DELETE' })
  await refreshSessions()
  return res
}

/** 从回收站恢复。 */
export async function restoreSession(threadId) {
  await fetch(`/api/sessions/${encodeURIComponent(threadId)}/restore`, { method: 'POST' })
  await refreshSessions()
}

/** 彻底删除（不可逆：那个会话的跨轮记忆一起没了）。 */
export async function hardDeleteSession(threadId) {
  const res = await fetch(
    `/api/sessions/${encodeURIComponent(threadId)}?hard=1`,
    { method: 'DELETE' },
  )
  await refreshSessions()
  return res
}

/** 一键清空系统线程（评估 / 探针 / 冒烟）—— 用户会话一条都不动。 */
export async function purgeSystemSessions() {
  const res = await fetch('/api/sessions/purge-system', { method: 'POST' })
  await refreshSessions()
  return res
}

/** 切换到一个历史会话：告诉后端改用它，并把历史消息拉回来回放。 */
/** 提示一行小字（几秒后自动消失；不弹窗、不打断）。 */
let noticeTimer = null
export function setNotice(text, ms = 5000) {
  store.notice = text
  if (noticeTimer) clearTimeout(noticeTimer)
  if (text) noticeTimer = setTimeout(() => (store.notice = ''), ms)
}

export async function loadSession(threadId) {
  if (store.wsStatus !== 'open') return
  if (threadId === store.threadId) return // 点自己：无动作、也不用提示（避免噪音）
  // ⚠️ 2026-10-07 实测：原来这里是一句静默 `return` ⇒ 用户点了会话"啥也没有"。
  //    现在的策略（用户定）：**执行中一律不许切走**，但必须**明确告诉他为什么**。
  if (store.sending) {
    setNotice('当前任务正在执行，请先点「停止任务」或等它跑完，再切换会话')
    return
  }
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

/** 取模型清单（「我的模型」+「系统默认」解析结果），给设置面板的下拉框当数据源。
 *  每项 `label` = 显示名、`model` = 实际调用名（不同时面板会补一句"实际调用 xxx"）。
 *  顺带把「当前生效模型」写进 store —— 顶栏常驻显示它。 */
export async function loadModels() {
  try {
    const res = await fetch('/api/models')
    const data = await res.json()
    store.effectiveModels = data.effectiveModels || {}
    return data
  } catch {
    return { models: [], roles: {}, effectiveModels: {} }
  }
}

/** 新增/更新一个自定义模型（自带模型名 / 地址 / 密钥）。 */
export async function addCustomModel(payload) {
  const res = await fetch('/api/settings/custom-model', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return res.json()
}

/** 删除一个自定义模型（后端会顺带清掉角色里指向它的引用）。 */
export async function deleteCustomModel(id) {
  const res = await fetch(`/api/settings/custom-model/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })
  return res.json()
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

/**
 * 阶段 7 · 界面第二轮反馈：一次测**当前配置里用到的每个模型**。
 * 后端按模型键去重 + 并发（"2 个模型分给 4 个角色" ⇒ 只发 2 次请求），
 * 返回 `{ok, groups: [{label, model, roles, ok, elapsedSec, error}]}`。
 */
export async function testRoleModels() {
  const res = await fetch('/api/settings/test-roles', { method: 'POST' })
  return res.json()
}

/** 列出知识库条目（自动沉淀会自己往里写，用户有权看到"它到底记住了什么"）。 */
export async function loadKnowledge() {
  try {
    const res = await fetch('/api/knowledge')
    const data = await res.json()
    return data.ok ? data.items || [] : []
  } catch {
    return []
  }
}

/** 删掉一条知识（文件 + 向量）；返回 {ok, error, items}。 */
export async function deleteKnowledge(name) {
  const res = await fetch(`/api/knowledge/${encodeURIComponent(name)}`, { method: 'DELETE' })
  return res.json()
}
