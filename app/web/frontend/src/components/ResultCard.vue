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

// ⚠️ 阶段 5 修：「验收未通过」以前是**误报** ——
//    判定写的是 `passed = verdict === 'PASS'`，于是**根本没有验收环节**的情况
//    （single 模式只跑 Executor；auto 模式走 simple 路由时也直接结束）
//    会因为 verdict 是空串而被显示成"✗ 验收未通过"。
//    后端是对的（run_multi_agent 只在 verdict 含 FAIL 时才当失败），错的是这个标签。
const hasVerdict = Boolean(verdictObj?.verdict)
const passed = verdictObj?.verdict?.toUpperCase() === 'PASS'
const showTrace = ref(false)

/** 阶段 6：**本轮实际使用的模型**（服务端在响应里回报的那个名字）。
 *  为什么要显示它：配置里写的是"我让谁答"，这里是"**真的谁答的**"——
 *  官方把旧模型名路由到新模型、或用中转别名时，两者会不一样。 */
const modelsUsed = (() => {
  const raw = props.result.modelsUsed || {}
  const names = new Set()
  for (const list of Object.values(raw)) {
    for (const n of list || []) names.add(n)
  }
  return {
    all: [...names].sort(),
    roles: Object.entries(raw)
      .map(([role, list]) => `${role}=${(list || []).join('/')}`)
      .filter((s) => !s.endsWith('='))
      .join('，'),
  }
})()
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
      <span v-if="hasVerdict" class="badge" :class="passed ? 'pass' : 'fail'">
        {{ passed ? '✓ 验收通过' : '✗ 验收未通过' }}
      </span>
      <span v-else class="badge none">— 本轮未验收（{{ result.mode === 'single' ? 'single' : '简单任务直通' }}）</span>
      <span v-if="verdictObj?.reason && !passed" class="reason">{{ verdictObj.reason }}</span>
      <span v-if="modelsUsed.all.length" class="models" :title="modelsUsed.roles">
        🧠 本轮实际使用：{{ modelsUsed.all.join('、') }}
      </span>
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
.badge.none { background: #1e293b; color: #94a3b8; border: 1px solid #334155; }
.reason { font-size: 12px; color: #fca5a5; }
.models { font-size: 12px; color: #93c5fd; cursor: help; }
.meta { margin-left: auto; font-size: 12px; color: #64748b; }
</style>
