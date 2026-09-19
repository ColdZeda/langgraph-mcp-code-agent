<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { loadModels, loadSettings, saveSettings, testSettings } from '../store'

const emit = defineEmits(['close'])

const ROLE_LABELS = {
  planner: 'Planner（规划）',
  executor: 'Executor（执行）',
  verifier: 'Verifier（验收）',
  router: 'Router（复杂度路由）',
}

const baseUrl = ref('')
const apiKey = ref('')
const keyInfo = ref('')
const testing = ref(false)
const testResult = ref(null)
const saving = ref(false)

const models = ref([])           // 来自 GET /api/models
const roles = ref({ planner: '', executor: '', verifier: '', router: '' })
const hetero = ref(false)        // 验收使用异构模型（与 Executor 不同）

const roleKeys = computed(() => Object.keys(ROLE_LABELS))

onMounted(async () => {
  const [s, m] = await Promise.all([loadSettings(), loadModels()])
  baseUrl.value = s.base_url || ''
  keyInfo.value = s.api_key_set ? `已配置（尾号 ${s.api_key_tail}）` : '未配置（使用 .env 默认）'
  models.value = m.models || []
  roles.value = { ...roles.value, ...(m.roles || {}) }
  // 已有配置里如果 Verifier 与 Executor 不同，就默认勾上"异构验收"
  hetero.value = Boolean(roles.value.verifier && roles.value.verifier !== roles.value.executor)
})

// 不勾"异构验收"时，Verifier 跟随 Executor（默认行为：三角色同一个模型，可预期）
watch(
  () => roles.value.executor,
  (v) => {
    if (!hetero.value) roles.value.verifier = v
  }
)
watch(hetero, (v) => {
  if (!v) roles.value.verifier = roles.value.executor
})

const doTest = async () => {
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await testSettings({
      base_url: baseUrl.value,
      api_key: apiKey.value || undefined,
    })
  } catch (e) {
    testResult.value = { ok: false, error: String(e) }
  } finally {
    testing.value = false
  }
}

const doSave = async () => {
  saving.value = true
  try {
    const payload = { base_url: baseUrl.value, roles: { ...roles.value } }
    if (apiKey.value) payload.api_key = apiKey.value
    await saveSettings(payload)
    emit('close')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="mask" @click.self="emit('close')">
    <div class="panel">
      <div class="head">
        <span>模型配置</span>
        <button class="x" @click="emit('close')">✕</button>
      </div>

      <label v-for="role in roleKeys" :key="role" class="row">
        <span class="row-label">{{ ROLE_LABELS[role] }}</span>
        <select
          v-model="roles[role]"
          :disabled="role === 'verifier' && !hetero"
          class="select"
        >
          <option value="">使用配置默认</option>
          <option v-for="m in models" :key="m.key" :value="m.key">
            {{ m.key }}（{{ m.model }}）
          </option>
        </select>
      </label>

      <label class="check">
        <input v-model="hetero" type="checkbox" />
        <span>验收使用异构模型（与 Executor 不同）</span>
      </label>

      <label>API 地址（留空用 .env 默认）</label>
      <input v-model="baseUrl" placeholder="如 https://api.deepseek.com" />

      <label>API Key</label>
      <input v-model="apiKey" type="password" :placeholder="`留空保持现有：${keyInfo}`" />
      <p class="note">
        角色模型来自 <code>config/models.json</code>；本面板的改动存本机
        <code>runtime/web-settings.json</code>（已 gitignore，不进仓库）
      </p>

      <div v-if="testResult" class="test-result" :class="testResult.ok ? 'ok' : 'bad'">
        {{ testResult.ok ? `连接正常（${testResult.elapsedSec}s）` : `连接失败：${testResult.error}` }}
      </div>

      <div class="actions">
        <button class="ghost" :disabled="testing" @click="doTest">
          {{ testing ? '测试中...' : '测试连接' }}
        </button>
        <button class="primary" :disabled="saving" @click="doSave">
          {{ saving ? '保存中...' : '保存并生效' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mask { position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: flex; align-items: center; justify-content: center; z-index: 50; }
.panel { width: 480px; background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 20px; display: flex; flex-direction: column; gap: 8px; }
.head { display: flex; justify-content: space-between; align-items: center; font-size: 16px; font-weight: 700; margin-bottom: 6px; }
.x { background: none; border: none; color: #94a3b8; font-size: 14px; cursor: pointer; }
label { font-size: 12px; color: #94a3b8; margin-top: 6px; }
.row { display: flex; align-items: center; gap: 10px; }
.row-label { width: 160px; flex: none; }
.select { flex: 1; padding: 8px 10px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #e2e8f0; font-size: 12px; }
.select:disabled { opacity: 0.55; }
.check { display: flex; align-items: center; gap: 8px; margin-top: 10px; }
.check input { width: auto; }
input { padding: 9px 12px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #e2e8f0; font-size: 13px; }
input:focus { outline: none; border-color: #3b82f6; }
.note { font-size: 11px; color: #64748b; }
.note code { color: #93c5fd; }
.test-result { font-size: 12px; padding: 8px 10px; border-radius: 8px; }
.test-result.ok { background: #14532d; color: #86efac; }
.test-result.bad { background: #450a0a; color: #fca5a5; }
.actions { display: flex; gap: 10px; margin-top: 10px; }
.actions button { flex: 1; padding: 10px; border-radius: 8px; font-size: 13px; cursor: pointer; }
.ghost { background: transparent; border: 1px solid #334155; color: #cbd5e1; }
.primary { background: #2563eb; border: none; color: #fff; }
.actions button:disabled { opacity: 0.5; }
</style>
