<script setup>
import ChatView from './components/ChatView.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import { onMounted } from 'vue'
import { connectWs, loadSession, refreshSessions, store } from './store'

onMounted(() => {
  connectWs()
  refreshSessions()
})

// 点历史会话：切换到它并回放历史（记忆由后端 checkpointer 按 thread_id 提供）
function switchSession(threadId) {
  if (threadId === store.threadId) return
  loadSession(threadId)
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-dot" :class="store.wsStatus"></span>
        <span class="brand-name">Code Agent</span>
      </div>
      <button class="btn-new" @click="$refs.chat?.newSession()">＋ 新会话</button>
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
