<script setup>
/**
 * 人工确认弹框（阶段 5 · T5.3 / B7）。
 *
 * 什么时候出现：后端在执行「写 / 执行类」工具之前发一条 `permission_request`
 * （只读档直接拒绝、放开档不弹，都不会走到这里）。
 *
 * 三件事必须做对：
 *  1. **默认不勾**「本会话内对该工具总是允许」—— 不改变"每次都要确认"的安全默认；
 *  2. 勾选后**只对当前会话 + 当前权限模式**有效（后端按 `(模式, 工具名)` 记，切档自动失效）；
 *  3. **倒计时到点自己收起**：后端到点会按 B2 自动拒绝，留着框只会让用户点了没人接。
 *
 * 阶段 8 · P0（真机验收 F1）：**弹框必须也能停任务**。
 * 这个遮罩是 `position: fixed; inset: 0` —— 它把输入区的「⏹ 停止」按钮**整个盖住**，
 * 于是"卡在弹框上时点停止"这条路在界面上根本走不到（实测 `elementFromPoint` 命中的是 `div.overlay`）。
 * 后端那条路早就通了（`stop` → 收掉待确认的 future → `permissions.enforce` 先判停止再判允许/拒绝），
 * 所以这里补一个同样发 `stop` 的按钮：**点了就停整个任务**，不用先回答这个弹框。
 * ⚠️ 它**不同于「拒绝」**：拒绝只是否掉这一次工具调用，任务还会换个办法接着干。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { respondPermission, stopTask, store } from '../store'

const alwaysAllow = ref(false) // ← 默认不勾
const remaining = ref(store.confirmTimeoutSec || 0)
let timer = null

const request = computed(() => store.permissionRequest)
const queueLength = computed(() => store.permissionQueue.length)

onMounted(() => {
  if (!remaining.value) return
  timer = setInterval(() => {
    remaining.value -= 1
    if (remaining.value <= 0) {
      clearInterval(timer)
      // 后端已按"无人应答"自动拒绝，这里只负责把框收掉
      store.permissionRequest = null
    }
  }, 1000)
})

onUnmounted(() => clearInterval(timer))

function decide(allow) {
  const req = request.value
  if (!req) return
  respondPermission({
    requestId: req.requestId,
    allow,
    alwaysAllow: allow && alwaysAllow.value,
  })
}

/** 阶段 8 · P0（F1）：**停止整个任务**（不是"拒绝这一次调用"）。
 *
 *  后端收到 `stop` 会把待确认的弹框按"拒绝"收掉、并让任务在下一个检查点停下；
 *  所以这里只需把本地弹框与队列清掉（不然会挂着一个永远等不到回应的框）。
 *  ⚠️ **不发** `permission_response` —— 那个 Future 由后端自己结算，重复回答会变成"已失效的答复"。 */
function stopEverything() {
  stopTask()
  store.permissionRequest = null
  store.permissionQueue = []
}
</script>

<template>
  <!-- ⚠️ `:key` 用 requestId：队列里换到下一个请求时要**重新挂载**，倒计时才会重头开始 -->
  <div v-if="request" :key="request.requestId" class="overlay">
    <div class="dialog" :class="{ danger: request.highRisk }">
      <div class="head">
        <span class="badge" :class="request.highRisk ? 'badge-danger' : 'badge-normal'">
          {{ request.highRisk ? '🔴 高危操作' : '需要确认' }}
        </span>
        <code class="tool">{{ request.tool }}</code>
        <span class="queued" v-if="queueLength">还有 {{ queueLength }} 个待确认</span>
        <span class="countdown" v-if="remaining > 0">{{ remaining }}s 后自动拒绝</span>
      </div>

      <p class="note" v-if="request.note">{{ request.note }}</p>
      <p class="note muted" v-else>该工具属于「写 / 执行类」，在「需确认」权限模式下需要你放行。</p>

      <div class="args-label">参数</div>
      <pre class="args">{{ request.args }}</pre>

      <label class="always">
        <input type="checkbox" v-model="alwaysAllow" />
        <span>本会话内对 <code>{{ request.tool }}</code> 总是允许（切换权限模式或换会话后失效）</span>
      </label>

      <div class="actions">
        <!-- 阶段 8 · P0（F1）：整页遮罩会盖住输入区的「停止」，所以停止必须在这里也能点到 -->
        <button
          class="btn stop"
          title="停止整个任务（已产出的文件/数据一律保留）；它不同于「拒绝」——拒绝只是否掉这一次调用"
          @click="stopEverything()"
        >
          ⏹ 停止任务
        </button>
        <button class="btn deny" @click="decide(false)">拒绝</button>
        <button class="btn allow" @click="decide(true)">允许执行</button>
      </div>
      <p class="hint">
        「停止任务」= 让整个任务在下一步边界停下（已保留的产物不回滚）；「拒绝」只否掉这一次调用，任务会继续。
      </p>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed; inset: 0; background: rgba(2, 6, 23, 0.72);
  display: flex; align-items: center; justify-content: center; z-index: 50;
}
.dialog {
  width: min(620px, 92vw); background: #1e293b; border: 1px solid #3b82f6;
  border-radius: 12px; padding: 18px 20px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
}
.dialog.danger { border-color: #ef4444; }
.head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 999px; }
.badge-normal { background: #1d4ed8; color: #dbeafe; }
.badge-danger { background: #b91c1c; color: #fee2e2; }
.tool { font-size: 14px; color: #93c5fd; }
.countdown { margin-left: auto; font-size: 12px; color: #fbbf24; }
.queued { font-size: 12px; color: #93c5fd; }
.note { margin-top: 12px; font-size: 13px; line-height: 1.6; color: #fca5a5; }
.note.muted { color: #94a3b8; }
.args-label { margin-top: 14px; font-size: 11px; letter-spacing: 1px; color: #94a3b8; }
.args {
  margin-top: 6px; max-height: 180px; overflow: auto; white-space: pre-wrap; word-break: break-all;
  background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 10px;
  font-size: 12px; color: #cbd5e1;
}
.always { display: flex; gap: 8px; align-items: flex-start; margin-top: 14px; font-size: 12px; color: #cbd5e1; }
.always code { color: #93c5fd; }
.actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
.btn { padding: 8px 18px; border-radius: 8px; border: 1px solid transparent; font-size: 13px; cursor: pointer; }
.btn.deny { background: transparent; border-color: #64748b; color: #cbd5e1; }
.btn.deny:hover { background: #334155; }
.btn.allow { background: #2563eb; color: #fff; }
.btn.allow:hover { background: #1d4ed8; }
/* 阶段 8 · P0（F1）：停止按钮**靠左**放，与「拒绝 / 允许执行」拉开距离，避免误点 */
.btn.stop { margin-right: auto; background: #b91c1c; color: #fff; }
.btn.stop:hover { background: #dc2626; }
.hint { margin-top: 10px; font-size: 11.5px; line-height: 1.6; color: #64748b; }
</style>
