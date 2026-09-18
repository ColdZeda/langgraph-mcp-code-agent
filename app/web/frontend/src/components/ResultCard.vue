<script setup>
import { ref } from 'vue'

const props = defineProps({ result: { type: Object, required: true } })

const planObj = (() => {
  try {
    return JSON.parse(props.result.plan)
  } catch {
    return null
  }
})()

const verdictObj = (() => {
  try {
    return JSON.parse(props.result.verdict)
  } catch {
    return null
  }
})()

const passed = verdictObj?.verdict?.toUpperCase() === 'PASS'
const showTrace = ref(false)
</script>

<template>
  <div class="result">
    <div v-if="planObj?.steps?.length" class="card plan">
      <div class="card-head">📋 Planner 计划 · {{ planObj.goal }}</div>
      <ol class="steps">
        <li v-for="(s, i) in planObj.steps" :key="i">{{ s }}</li>
      </ol>
    </div>

    <div class="card answer">
      <div class="card-head">🤖 Executor 结果</div>
      <div class="answer-text">{{ result.finalResponse }}</div>
    </div>

    <div v-if="result.toolTrace?.length" class="card trace">
      <div class="card-head toggle" @click="showTrace = !showTrace">
        🔧 工具调用 {{ result.toolTrace.length }} 次 {{ showTrace ? '▾' : '▸' }}
      </div>
      <div v-if="showTrace" class="trace-list">
        <div v-for="(t, i) in result.toolTrace" :key="i" class="trace-item">
          <span class="trace-idx">{{ i + 1 }}</span>
          <code class="trace-name">{{ t.name }}</code>
          <code class="trace-args">{{ t.args }}</code>
        </div>
      </div>
    </div>

    <div class="card footer-row">
      <span class="badge" :class="passed ? 'pass' : 'fail'">
        {{ passed ? '✓ 验收通过' : '✗ 验收未通过' }}
      </span>
      <span v-if="verdictObj?.reason && !passed" class="reason">{{ verdictObj.reason }}</span>
      <span class="meta">打回 {{ result.retryCount }} 次 · 步数 {{ result.stepCount }} · token {{ result.tokenUsage }} · 耗时 {{ result.elapsedSec }}s</span>
    </div>
  </div>
</template>

<style scoped>
.result { display: flex; flex-direction: column; gap: 10px; max-width: 860px; width: 100%; }
.card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 12px 16px; }
.card-head { font-size: 13px; font-weight: 600; color: #93c5fd; margin-bottom: 8px; }
.card-head.toggle { cursor: pointer; user-select: none; }
.steps { padding-left: 20px; font-size: 13px; line-height: 1.8; color: #cbd5e1; }
.answer-text { font-size: 14px; line-height: 1.7; white-space: pre-wrap; word-break: break-word; color: #e2e8f0; }
.trace-list { display: flex; flex-direction: column; gap: 6px; }
.trace-item { display: flex; gap: 8px; align-items: baseline; font-size: 12px; }
.trace-idx { color: #64748b; min-width: 18px; }
.trace-name { color: #fbbf24; background: #0f172a; padding: 2px 8px; border-radius: 6px; }
.trace-args { color: #94a3b8; word-break: break-all; }
.footer-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.badge { padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.badge.pass { background: #14532d; color: #86efac; }
.badge.fail { background: #450a0a; color: #fca5a5; }
.reason { font-size: 12px; color: #fca5a5; }
.meta { margin-left: auto; font-size: 12px; color: #64748b; }
</style>
