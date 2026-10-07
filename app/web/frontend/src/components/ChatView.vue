<script setup>
import { computed, nextTick, ref } from 'vue'
import { newSession, sendChat, stopTask, store } from '../store'
import ResultCard from './ResultCard.vue'
import MarkdownText from './MarkdownText.vue'

const input = ref('')
const listEl = ref(null)

const scrollToBottom = () => nextTick(() => {
  if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
})

const send = () => {
  sendChat(input.value)
  input.value = ''
  scrollToBottom()
}

const newSessionAndClear = () => {
  store.messages = []
  newSession()
}

// ── 阶段 8 · P0：任务状态条 ────────────────────────────────────────
// 数值全部来自后端（`status` 事件），前端只负责补中间那 1 秒与排版。
const status = computed(() => store.status)

/** 秒 → `12:34` / `1:02:03`（比 `754.3s` 好扫）。 */
function fmtElapsed(sec) {
  const total = Math.max(0, Math.floor(Number(sec) || 0))
  const pad = (n) => String(n).padStart(2, '0')
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`
}

/** token 数 → `1.2 万`（量级够看；精确值在结果卡片的 meta 行）。 */
function fmtTokens(n) {
  const v = Number(n) || 0
  return v >= 10000 ? `${(v / 10000).toFixed(1)} 万` : String(v)
}

defineExpose({ newSession: newSessionAndClear })
</script>

<template>
  <div class="chat">
    <div class="header">
      <span>当前会话</span>
      <code class="thread">{{ store.threadId || '(连接后自动生成)' }}</code>
      <span class="status" :class="store.wsStatus">
        {{ store.wsStatus === 'open' ? '已连接' : store.wsStatus === 'connecting' ? '连接中...' : '已断开，重连中' }}
      </span>
    </div>

    <div class="list" ref="listEl">
      <div v-if="!store.messages.length" class="empty">
        <h2>Code Agent-novi</h2>
        <p>Planner → Executor → Verifier 三阶段协作</p>
        <p class="tip">试试：读取当前目录结构 / 查询 MySQL 数据库列表 / 在知识库中搜索 MCP 协议要点</p>
      </div>

      <template v-for="m in store.messages" :key="m.id">
        <div v-if="m.role === 'user'" class="row user">
          <div class="bubble user-bubble">{{ m.text }}</div>
        </div>
        <div v-else class="row assistant">
          <div v-if="m.phase === 'running'" class="bubble running-bubble">
            <div class="running-head"><span class="spinner"></span>{{ m.text }}</div>
            <!-- 阶段 8 · P0：任务状态条（时长 / 步数 / 用量）—— 跑长任务时一眼看得出"烧到哪了" -->
            <div v-if="status" class="statusbar">
              <span class="sb-item">
                ⏱ {{ fmtElapsed(status.elapsedSec) }}<span
                  v-if="status.wallClockSec"
                  class="sb-dim"
                > / {{ fmtElapsed(status.wallClockSec) }}</span>
              </span>
              <span class="sb-item">第 {{ status.steps }} 步</span>
              <span class="sb-item">
                {{ fmtTokens(status.tokens) }}<span
                  v-if="status.usageLimit"
                  class="sb-dim"
                > / {{ fmtTokens(status.usageLimit) }}</span> token
              </span>
              <span v-if="status.pausedSec >= 1" class="sb-dim">
                （人工确认等待 {{ fmtElapsed(status.pausedSec) }} 未计入）
              </span>
            </div>
            <!-- 阶段 5（T5.6）：节点级进度，任务进行中就看得见走到哪一步 -->
            <ol v-if="store.progress.length" class="progress">
              <li v-for="p in store.progress" :key="p.id">{{ p.text }}</li>
            </ol>
          </div>
          <div v-else-if="m.phase === 'error'" class="bubble error-bubble">{{ m.text }}</div>
          <ResultCard v-else-if="m.result" :result="m.result" />
          <!-- ⚠️ 阶段 5 修：**历史会话回放**走的就是这一支。
               loadSession 造出来的消息是 {role, phase:'done', text}，**没有 result 字段**，
               而上面三条分支分别要求 running / error / result →
               以前这里什么都不渲染，表现成"点历史会话只看到自己发的消息"。
               补一条纯文本回退分支即可。 -->
          <div v-else-if="m.text" class="bubble assistant-bubble">
            <span v-if="m.fromHistory" class="history-tag">历史</span>
            <!-- 阶段 7（T7.5）：历史回放的答案以前也是纯文本 ⇒ 这里同样走 Markdown 渲染 -->
            <MarkdownText :text="m.text" />
          </div>
        </div>
      </template>
    </div>

    <!-- 阶段 7 · T7.6：**还没有可用的模型**时的引导（新用户 clone 下来的默认状态）。
         以前这种情况服务在 import 期就崩了，用户根本看不到这个界面。 -->
    <div v-if="!store.modelReady" class="no-model">
      <b>还没有可用的模型</b> —— 点左下角「⚙ 模型设置」，在「我的模型」里填一个 API Key 就能开始。
    </div>

    <div class="input-area">
      <textarea
        v-model="input"
        :placeholder="
          !store.modelReady
            ? '先去「模型设置」里添加你的 API Key'
            : store.sending
              ? '任务执行中...'
              : '描述任务，Enter 发送，Shift+Enter 换行'
        "
        :disabled="store.wsStatus !== 'open' || !store.modelReady"
        @keydown.enter.exact.prevent="send"
      ></textarea>
      <!-- 阶段 8 · P0：停止按钮。⚠️ 协作式：点了之后在**下一个检查点**停下，
           所以按钮先变成「停止中…」（不是立刻消失）。 -->
      <button
        v-if="store.sending"
        class="btn-stop"
        :disabled="store.stopping || store.wsStatus !== 'open'"
        :title="
          store.stopping
            ? '已发出停止请求，会在下一步边界停下'
            : '停止当前任务（已产出的文件/数据一律保留）'
        "
        @click="stopTask()"
      >
        {{ store.stopping ? '停止中…' : '⏹ 停止' }}
      </button>
      <button
        v-else
        :disabled="!store.modelReady || !input.trim()"
        @click="send"
      >
        发送
      </button>
    </div>
  </div>
</template>

<style scoped>
.chat { display: flex; flex-direction: column; height: 100vh; }
.header { display: flex; align-items: center; gap: 10px; padding: 12px 20px; border-bottom: 1px solid #334155; font-size: 13px; color: #94a3b8; }
.thread { color: #93c5fd; font-size: 12px; }
.status { margin-left: auto; font-size: 12px; }
.status.open { color: #22c55e; }
.status.connecting { color: #eab308; }
.status.closed { color: #ef4444; }
.list { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 14px; }
.empty { text-align: center; margin-top: 18vh; color: #64748b; }
.empty h2 { color: #e2e8f0; margin-bottom: 8px; }
.empty .tip { font-size: 12px; margin-top: 6px; }
.row.user { display: flex; justify-content: flex-end; }
.row.assistant { display: flex; justify-content: flex-start; }
.bubble { max-width: 72%; padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.user-bubble { background: #2563eb; color: #fff; border-bottom-right-radius: 4px; }
.running-bubble { background: #1e293b; border: 1px solid #334155; color: #94a3b8; font-style: italic; }
.running-head { display: flex; align-items: center; }
/* 阶段 8 · P0：任务状态条（等宽数字，避免每秒抖动） */
.statusbar { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; font-style: normal;
  font-size: 12px; color: #cbd5e1; font-variant-numeric: tabular-nums; }
.sb-item { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 2px 8px; }
.sb-dim { color: #64748b; }
.progress { margin: 8px 0 0 20px; padding: 0; list-style: none; font-style: normal; font-size: 12px; line-height: 1.9; color: #93c5fd; max-height: 220px; overflow-y: auto; }
.progress li { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.assistant-bubble { background: #1e293b; border: 1px solid #334155; }
.history-tag { display: inline-block; margin-right: 6px; padding: 0 6px; border-radius: 6px; background: #334155; color: #94a3b8; font-size: 11px; vertical-align: 1px; }
.error-bubble { background: #450a0a; border: 1px solid #b91c1c; color: #fca5a5; }
.spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid #64748b; border-top-color: #93c5fd; border-radius: 50%; margin-right: 8px; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.input-area { display: flex; gap: 10px; padding: 14px 20px; border-top: 1px solid #334155; }
.no-model { margin: 0 20px 10px; padding: 10px 14px; border-radius: 10px; font-size: 13px;
  background: #422006; color: #fde68a; border: 1px solid #a16207; }
textarea { flex: 1; resize: none; height: 64px; padding: 10px 12px; border-radius: 10px; border: 1px solid #334155; background: #1e293b; color: #e2e8f0; font-size: 14px; font-family: inherit; }
textarea:focus { outline: none; border-color: #3b82f6; }
.input-area button { width: 90px; border: none; border-radius: 10px; background: #2563eb; color: #fff; font-size: 14px; cursor: pointer; }
.input-area button:disabled { opacity: 0.5; cursor: not-allowed; }
/* 阶段 8 · P0：停止按钮（红底，和"发送"位置一致，不会点错） */
.input-area button.btn-stop { background: #b91c1c; width: 110px; }
.input-area button.btn-stop:hover:not(:disabled) { background: #dc2626; }
.input-area button.btn-stop:disabled { background: #7f1d1d; color: #fecaca; }
</style>
