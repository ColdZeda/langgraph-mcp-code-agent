<script setup>
import ChatView from './components/ChatView.vue'
import PermissionDialog from './components/PermissionDialog.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import { computed, onMounted } from 'vue'
import {
  connectWs,
  loadModels,
  loadSession,
  refreshSessions,
  setPermissionMode,
  store,
} from './store'

onMounted(() => {
  connectWs()
  refreshSessions()
  loadModels() // 顶栏要显示"当前生效模型"
})

// 点历史会话：切换到它并回放历史（记忆由后端 checkpointer 按 thread_id 提供）
function switchSession(threadId) {
  if (threadId === store.threadId) return
  loadSession(threadId)
}

// 权限模式下拉框的说明文字（档位集合由后端给，措辞在前端 —— 见 store.permissionModes）
const PERMISSION_HINTS = {
  readonly: '只读（写操作直接拒绝）',
  confirm: '需确认（默认，写操作弹框）',
  open: '放开（不弹框，黑名单仍生效）',
}

function onPermissionChange(event) {
  setPermissionMode(event.target.value)
}

/** 当前生效模型（阶段 6）：显示 **Executor** 那个 —— 干活主要是它。
 *  ⚠️ 模型是**全局配置、不随会话保存**：这里显示的是"现在发消息会用谁"，
 *  不是"这个会话历史上用过谁"（后者看每条结果卡片上的"本轮实际使用模型"）。 */
const effective = computed(() => store.effectiveModels || {})
const executorModel = computed(() => effective.value.executor || null)
const rolesDiffer = computed(() => {
  const e = executorModel.value
  if (!e) return false
  return ['planner', 'verifier', 'router'].some(
    (r) => (effective.value[r]?.model || '') !== (e.model || '')
  )
})
const modelTitle = computed(() => {
  const rows = Object.entries(effective.value).map(
    ([role, m]) => `${role}: ${m.label}${m.model !== m.label ? `（实际调用 ${m.model}）` : ''}`
  )
  return rows.length ? rows.join('\n') : '读取中…'
})
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-dot" :class="store.wsStatus"></span>
        <span class="brand-name">Code Agent-novi</span>
      </div>
      <button class="btn-new" @click="$refs.chat?.newSession()">＋ 新会话</button>
      <label class="mode-row">
        <span class="session-title">执行模式</span>
        <select v-model="store.mode" class="mode-select" :disabled="store.sending">
          <option value="auto">auto（按复杂度自动）</option>
          <option value="single">single（单 Agent，快）</option>
          <option value="multi">multi（完整三阶段）</option>
        </select>
      </label>
      <!-- 阶段 5：与「执行模式」并排的第二条轴。两个名字不能都叫"模式"。 -->
      <label class="mode-row">
        <span class="session-title">权限模式</span>
        <select
          class="mode-select"
          :class="{ danger: store.permissionMode === 'open' }"
          :value="store.permissionMode"
          :disabled="store.sending"
          @change="onPermissionChange"
        >
          <option v-for="m in store.permissionModes" :key="m.value" :value="m.value">
            {{ PERMISSION_HINTS[m.value] || m.label }}
          </option>
        </select>
      </label>
      <!-- 阶段 6：常驻显示「当前生效模型」—— 模型是全局配置、不随会话保存，
           不显示的话用户不知道现在到底是谁在干活。点一下直接进模型设置。 -->
      <div class="model-row" :title="modelTitle" @click="store.showSettings = true">
        <span class="session-title">当前生效模型</span>
        <span class="model-name">{{ executorModel ? executorModel.model : '读取中…' }}</span>
        <span v-if="executorModel?.custom" class="model-tag">我的模型</span>
        <span v-if="rolesDiffer" class="model-warn">四个角色配置不同（悬停查看）</span>
      </div>
      <div class="session-title">历史会话（checkpoint）</div>
      <div class="session-list">
        <div
          v-for="s in store.sessions"
          :key="s.threadId"
          class="session-item"
          :class="{ active: s.threadId === store.threadId }"
          @click="switchSession(s.threadId)"
        >
          <span class="session-id">{{ s.threadId }}</span>
          <span class="session-meta">
            {{ s.updatedAt ? new Date(s.updatedAt * 1000).toLocaleString() : '时间未知' }}
          </span>
        </div>
        <div v-if="!store.sessions.length" class="session-empty">暂无历史会话</div>
      </div>
      <button class="btn-settings" @click="store.showSettings = true">⚙ 模型设置</button>
    </aside>
    <main class="main">
      <ChatView ref="chat" />
    </main>
    <SettingsPanel v-if="store.showSettings" @close="store.showSettings = false" />
    <PermissionDialog />
  </div>
</template>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body, #app { height: 100%; }
body { font-family: "Segoe UI", "Microsoft YaHei", sans-serif; background: #0f172a; color: #e2e8f0; }
.layout { display: flex; height: 100vh; }
.sidebar { width: 260px; background: #1e293b; padding: 16px 12px; display: flex; flex-direction: column; gap: 12px; border-right: 1px solid #334155; }
.brand { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 16px; }
.brand-dot { width: 10px; height: 10px; border-radius: 50%; background: #64748b; }
.brand-dot.open { background: #22c55e; }
.brand-dot.closed { background: #ef4444; }
.brand-dot.connecting { background: #eab308; }
.btn-new { padding: 9px; border: 1px solid #3b82f6; background: #2563eb; color: #fff; border-radius: 8px; cursor: pointer; font-size: 13px; }
.btn-new:hover { background: #1d4ed8; }
.session-title { font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }
.mode-row { display: flex; flex-direction: column; gap: 4px; }
.mode-select { padding: 6px 8px; background: #0f172a; color: #e2e8f0; border: 1px solid #334155; border-radius: 8px; font-size: 12px; }
.mode-select:disabled { opacity: 0.6; }
.mode-select.danger { border-color: #ef4444; color: #fca5a5; }
.model-row { display: flex; flex-direction: column; gap: 2px; padding: 8px 10px; background: #0f172a;
  border: 1px solid #334155; border-radius: 8px; cursor: pointer; }
.model-row:hover { border-color: #3b82f6; }
.model-name { font-family: Consolas, monospace; font-size: 12px; color: #93c5fd; word-break: break-all; }
.model-tag { align-self: flex-start; font-size: 10px; color: #86efac; background: #14532d;
  border-radius: 999px; padding: 1px 6px; }
.model-warn { font-size: 10px; color: #fbbf24; }
.session-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.session-item { padding: 8px 10px; background: #0f172a; border-radius: 8px; border: 1px solid #334155; display: flex; flex-direction: column; gap: 2px; cursor: pointer; }
.session-item:hover { border-color: #3b82f6; }
.session-item.active { border-color: #3b82f6; background: #172554; }
.session-id { font-family: Consolas, monospace; font-size: 12px; color: #93c5fd; }
.session-meta { font-size: 11px; color: #64748b; }
.session-empty { font-size: 12px; color: #64748b; padding: 8px; }
.btn-settings { padding: 9px; border: 1px solid #334155; background: transparent; color: #cbd5e1; border-radius: 8px; cursor: pointer; font-size: 13px; }
.btn-settings:hover { background: #334155; }
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
</style>
