<script setup>
import { ref } from 'vue'
import MarkdownText from './MarkdownText.vue'

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

/** 没有裁定时的说明（阶段 7 · T7.5 修正）。
 *  以前写的是「`mode === 'single'` ? 'single' : **'简单任务直通'**」——
 *  于是**任何非 single 的情况都被说成"简单任务直通"**：multi 模式里 Verifier 没产出可解析裁定
 *  （上游抽风返回一整句错误文本是实测发生过的）也会被显示成"设计如此"，把"少验收了一次"粉饰掉。
 *  现在按**真实原因**分三种说：single 没有验收环节 / auto 路由判为简单任务 / 其余 = 未产出裁定。 */
const noVerdictLabel = (() => {
  if (props.result.mode === 'single') return 'single 模式，无验收环节'
  if (props.result.route === 'simple') return 'auto 判为简单任务，直通 Executor'
  return `${props.result.mode || '未知'} 模式未产出裁定`
})()
const showTrace = ref(false)

/** 阶段 8 · P0：本轮是不是被**停止**的（用户点停止 / 墙钟到点）。
 *  为什么要单独判：停止时根本没有验收结论，若走下面那条"未产出裁定"分支，
 *  会把"人让它停"显示成"这轮没验收" —— 用户看不出到底发生了什么。 */
const cancelLabel = (() => {
  if (!props.result.cancelled) return ''
  return props.result.cancelReason === 'wall_clock'
    ? '达到单任务墙钟上限（自动停止）'
    : '你点了「停止」'
})()
const cancelStageText = (() => {
  const stage = props.result.cancelStage
  if (!props.result.cancelled) return ''
  if (stage === 'planner') return '停在规划阶段（还没开始执行）'
  if (stage === 'verifier') return '停在验收环节（产物已产出）'
  return `停在第 ${props.result.stepCount} 步之后`
})()

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
      <!-- 阶段 7（T7.5）：以前这里是纯文本（`pre-wrap`），`##` / `**` / 表格全是源码。
           现在走 MarkdownText（marked + DOMPurify，见该组件注释）。 -->
      <MarkdownText :text="result.finalResponse" />
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
      <!-- 阶段 8 · P1.5：入口发现"模板没替换" ⇒ 只回问、**没进图**（不是"没验收"） -->
      <span v-if="result.needsClarification" class="badge ask">
        ❓ 需要你确认（我没开跑）
      </span>
      <!-- 阶段 8 · P0：停止优先于验收结论（停止时压根没有验收） -->
      <span v-else-if="result.cancelled" class="badge stop" :title="cancelStageText">
        ⏹ 已停止 · {{ cancelLabel }}
      </span>
      <span v-else-if="hasVerdict" class="badge" :class="passed ? 'pass' : 'fail'">
        {{ passed ? '✓ 验收通过' : '✗ 验收未通过' }}
      </span>
      <span v-else class="badge none">— 本轮未验收（{{ noVerdictLabel }}）</span>
      <span v-if="!result.cancelled && verdictObj?.reason && !passed" class="reason">{{ verdictObj.reason }}</span>
      <span v-if="modelsUsed.all.length" class="models" :title="modelsUsed.roles">
        🧠 本轮实际使用：{{ modelsUsed.all.join('、') }}
      </span>
      <span class="meta">
        打回 {{ result.retryCount }} 次 · 步数 {{ result.stepCount }} · token {{ result.tokenUsage }} ·
        耗时 {{ result.elapsedSec }}s<span
          v-if="result.pausedSec >= 1"
          class="meta-dim"
        >（含人工确认等待 {{ Math.round(result.pausedSec) }}s）</span>
      </span>
    </div>
  </div>
</template>

<style scoped>
.result { display: flex; flex-direction: column; gap: 10px; max-width: 860px; width: 100%; }
.card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 12px 16px; }
.card-head { font-size: 13px; font-weight: 600; color: #93c5fd; margin-bottom: 8px; }
.card-head.toggle { cursor: pointer; user-select: none; }
.steps { padding-left: 20px; font-size: 13px; line-height: 1.8; color: #cbd5e1; }
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
.badge.stop { background: #422006; color: #fbbf24; cursor: help; }
/* 阶段 8 · P1.5：入口回问（"模板没替换"）—— 与"没验收"要一眼可分 */
.badge.ask { background: #1e3a8a; color: #bfdbfe; }
.reason { font-size: 12px; color: #fca5a5; }
.models { font-size: 12px; color: #93c5fd; cursor: help; }
.meta { margin-left: auto; font-size: 12px; color: #64748b; }
.meta-dim { color: #475569; }
</style>
