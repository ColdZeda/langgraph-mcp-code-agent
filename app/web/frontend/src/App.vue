<script setup>
import ChatView from './components/ChatView.vue'
import PermissionDialog from './components/PermissionDialog.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import KnowledgePanel from './components/KnowledgePanel.vue'
import { computed, onMounted, ref } from 'vue'
import {
  connectWs,
  hardDeleteSession,
  loadModels,
  loadSession,
  purgeSystemSessions,
  refreshSessions,
  renameSession,
  restoreSession,
  setPermissionMode,
  softDeleteSession,
  store,
  togglePinSession,
} from './store'

const chat = ref(null)

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

// ── 会话管理（阶段 7 · T7.5）─────────────────────────────────────────
// 交互一律用**行内二次确认**（不弹 `window.confirm`）：和设置面板里"确认删除？"的写法保持一致，
// 也不会被浏览器/WebView 拦掉。
const editing = ref(null) // { threadId, title } —— 正在行内重命名哪一条
const confirmingDelete = ref('') // 软删除的二次确认
const confirmingHard = ref('') // 回收站里"彻底删除"的二次确认
const confirmingPurge = ref(false) // 清空系统线程的二次确认
const sessionNotice = ref('') // 操作失败时的提示（例如"正在被另一个标签页使用"）

/** 侧栏时间：`今天 19:17` / `昨天 21:04` / `09-23 13:19`。
 *  原来是 `2026/9/27 19:17:46` 一长串 —— 一屏十几条时不好扫。 */
function shortTime(ts) {
  if (!ts) return '时间未知'
  const d = new Date(ts * 1000)
  const now = new Date()
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const sameDay = (a, b) => a.toDateString() === b.toDateString()
  if (sameDay(d, now)) return `今天 ${hm}`
  if (sameDay(d, new Date(now.getTime() - 86400000))) return `昨天 ${hm}`
  const md = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  return `${md} ${hm}`
}

/** 列表里显示什么：有标题用标题；还没有标题的（先建了会话没说话 / 老会话）回落成短 ID。 */
function sessionLabel(s) {
  return s.title || s.threadId
}

function startRename(s) {
  sessionNotice.value = ''
  editing.value = { threadId: s.threadId, title: s.title || '' }
}

async function commitRename() {
  const target = editing.value
  editing.value = null
  const title = (target?.title || '').trim()
  if (target && title) await renameSession(target.threadId, title)
}

async function toggleHidden() {
  store.showHidden = !store.showHidden
  await refreshSessions()
}

async function toggleEval() {
  store.showEval = !store.showEval
  await refreshSessions()
}

/** 软删除（进回收站）。⚠️ **当前正在用的会话**要先自动新开一条 —— 否则删完就没地方说话了。 */
async function doSoftDelete(s) {
  sessionNotice.value = ''
  if (s.threadId === store.threadId) {
    chat.value?.newSession()
    await new Promise((r) => setTimeout(r, 200)) // 等后端把 thread_id 切过去
  }
  const res = await softDeleteSession(s.threadId)
  confirmingDelete.value = ''
  if (res && !res.ok) {
    sessionNotice.value =
      res.status === 409 ? '这个会话正被另一个标签页使用，先在那里切走再删。' : '删除失败'
  }
}

async function doHardDelete(s) {
  sessionNotice.value = ''
  const res = await hardDeleteSession(s.threadId)
  confirmingHard.value = ''
  if (res && !res.ok) sessionNotice.value = '彻底删除失败（可能正被其它标签页使用）'
}

async function doPurgeSystem() {
  sessionNotice.value = ''
  const res = await purgeSystemSessions()
  confirmingPurge.value = false
  if (res && !res.ok) {
    sessionNotice.value = '清空系统线程失败'
    return
  }
  const skipped = (await res.json().catch(() => ({}))).skipped || []
  if (skipped.length) sessionNotice.value = `有 ${skipped.length} 条仍被连接占用，已跳过`
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-dot" :class="store.wsStatus"></span>
        <span class="brand-name">Code Agent-novi</span>
      </div>
      <button class="btn-new" @click="chat?.newSession()">＋ 新会话</button>
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
      <!-- 执行中点会话/新会话时的提示（用户定的口径：不许切，但必须说清为什么） -->
      <div v-if="store.notice" class="notice">⚠️ {{ store.notice }}</div>
      <div class="model-row" :title="modelTitle" @click="store.showSettings = true">
        <span class="session-title">当前生效模型</span>
        <!-- 阶段 7 · T7.6：没有可用模型时**别显示一个用不了的名字** ——
             以前这里会显示 `.env` 里的 fallback 名字（用户会以为能直接开聊）。 -->
        <span v-if="!store.modelReady" class="model-none">未配置 —— 点这里添加</span>
        <template v-else>
          <span class="model-name">{{ executorModel ? executorModel.model : '读取中…' }}</span>
          <span v-if="executorModel?.custom" class="model-tag">我的模型</span>
          <span v-if="rolesDiffer" class="model-warn">有角色使用了不同模型（悬停查看）</span>
        </template>
      </div>
      <div class="session-title">历史会话（checkpoint）</div>
      <div class="session-list">
        <div
          v-for="s in store.sessions"
          :key="s.threadId"
          class="session-item"
          :class="{
            active: s.threadId === store.threadId,
            gone: !!s.deletedAt,
            system: s.system,
          }"
          @click="switchSession(s.threadId)"
        >
          <div class="session-head">
            <!-- 行内重命名：点铅笔就地改，回车保存、Esc 取消（失焦也保存） -->
            <input
              v-if="editing && editing.threadId === s.threadId"
              v-model="editing.title"
              class="rename-input"
              maxlength="24"
              @click.stop
              @keydown.enter.prevent="commitRename"
              @keydown.esc="editing = null"
              @blur="commitRename"
            />
            <span v-else class="session-label" :title="`thread_id: ${s.threadId}`">
              {{ sessionLabel(s) }}
            </span>
            <span class="session-actions" @click.stop>
              <button
                class="mini"
                :title="s.pinned ? '取消置顶' : '置顶'"
                @click="togglePinSession(s.threadId, !s.pinned)"
              >
                📌
              </button>
              <button class="mini" title="重命名" @click="startRename(s)">✏️</button>
              <template v-if="s.deletedAt">
                <button class="mini" title="恢复（回到列表）" @click="restoreSession(s.threadId)">
                  ↩︎
                </button>
                <button
                  class="mini danger"
                  :title="'彻底删除（不可逆：会话记忆一起没）'"
                  @click="
                    confirmingHard === s.threadId
                      ? doHardDelete(s)
                      : (confirmingHard = s.threadId)
                  "
                >
                  {{ confirmingHard === s.threadId ? '确认彻底删除？' : '彻底删除' }}
                </button>
              </template>
              <button
                v-else
                class="mini danger"
                title="删除（进回收站，可恢复）"
                @click="
                  confirmingDelete === s.threadId ? doSoftDelete(s) : (confirmingDelete = s.threadId)
                "
              >
                {{ confirmingDelete === s.threadId ? '确认删除？' : '🗑' }}
              </button>
            </span>
          </div>
          <span class="session-meta">
            <span v-if="s.system" class="tag-system">系统</span>
            <span v-if="s.pinned" class="tag-pin">置顶</span>
            {{ shortTime(s.updatedAt) }} · {{ s.checkpointCount }} 步
          </span>
        </div>
        <div v-if="!store.sessions.length" class="session-empty">暂无历史会话</div>

        <!-- 下面两行小字**只在有东西时出现**（counts 为 0 时一个字都不显示） -->
        <button v-if="store.sessionCounts.hidden" class="link-row" @click="toggleHidden">
          回收站 ({{ store.sessionCounts.hidden }}){{ store.showHidden ? ' ▾' : ' ▸' }}
        </button>
        <button v-if="store.sessionCounts.eval" class="link-row" @click="toggleEval">
          {{ store.showEval ? '隐藏' : '显示' }}系统线程 ({{ store.sessionCounts.eval }})
        </button>
        <button
          v-if="store.showEval && store.sessionCounts.eval"
          class="link-row danger"
          @click="confirmingPurge ? doPurgeSystem() : (confirmingPurge = true)"
        >
          {{ confirmingPurge ? '确认清空系统线程？' : '清空系统线程' }}
        </button>
        <p v-if="sessionNotice" class="notice">{{ sessionNotice }}</p>
      </div>
      <!-- 阶段 8 · 实测新增：知识库面板（自动沉淀会自己写进去，用户得能看到/删掉） -->
      <button class="btn-settings" @click="store.showKnowledge = true">🧠 知识库</button>
      <button class="btn-settings" @click="store.showSettings = true">⚙ 模型设置</button>
    </aside>
    <main class="main">
      <ChatView ref="chat" />
    </main>
    <SettingsPanel v-if="store.showSettings" @close="store.showSettings = false" />
    <KnowledgePanel v-if="store.showKnowledge" @close="store.showKnowledge = false" />
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
.notice { margin: 6px 0; padding: 7px 10px; border-radius: 8px; background: #422006; border: 1px solid #854d0e;
  color: #fde68a; font-size: 12px; line-height: 1.5; }
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
.model-none { font-size: 12px; color: #fbbf24; }
.model-tag { align-self: flex-start; font-size: 10px; color: #86efac; background: #14532d;
  border-radius: 999px; padding: 1px 6px; }
.model-warn { font-size: 10px; color: #fbbf24; }
.session-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.session-item { padding: 8px 10px; background: #0f172a; border-radius: 8px; border: 1px solid #334155; display: flex; flex-direction: column; gap: 2px; cursor: pointer; }
.session-item:hover { border-color: #3b82f6; }
.session-item.active { border-color: #3b82f6; background: #172554; }
/* 回收站里的 / 系统线程：灰一点，一眼能看出"这不是我的日常会话" */
.session-item.gone { opacity: 0.6; border-style: dashed; }
.session-item.system .session-label { color: #94a3b8; }
.session-head { display: flex; align-items: center; gap: 6px; }
.session-label {
  flex: 1; min-width: 0; font-size: 12.5px; color: #e2e8f0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.session-actions { display: none; align-items: center; gap: 2px; flex-shrink: 0; }
.session-item:hover .session-actions { display: flex; }
.mini {
  border: none; background: transparent; cursor: pointer; font-size: 11px;
  padding: 1px 3px; border-radius: 5px; color: #94a3b8; line-height: 1.4;
}
.mini:hover { background: #334155; color: #e2e8f0; }
.mini.danger { color: #fca5a5; }
.mini.danger:hover { background: #450a0a; }
.rename-input {
  flex: 1; min-width: 0; font-size: 12.5px; padding: 2px 6px; border-radius: 6px;
  border: 1px solid #3b82f6; background: #0b1220; color: #e2e8f0; font-family: inherit;
}
.rename-input:focus { outline: none; }
.session-meta { font-size: 11px; color: #64748b; }
.tag-system { background: #334155; color: #cbd5e1; border-radius: 999px; padding: 0 6px; margin-right: 4px; }
.tag-pin { background: #422006; color: #fbbf24; border-radius: 999px; padding: 0 6px; margin-right: 4px; }
.link-row {
  border: none; background: transparent; color: #93c5fd; font-size: 11.5px;
  text-align: left; padding: 4px 2px; cursor: pointer;
}
.link-row:hover { text-decoration: underline; }
.link-row.danger { color: #fca5a5; }
.notice { font-size: 11px; color: #fca5a5; margin: 2px 0 0; }
.session-empty { font-size: 12px; color: #64748b; padding: 8px; }
.btn-settings { padding: 9px; border: 1px solid #334155; background: transparent; color: #cbd5e1; border-radius: 8px; cursor: pointer; font-size: 13px; }
.btn-settings:hover { background: #334155; }
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
</style>
