<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  addCustomModel,
  deleteCustomModel,
  loadModels,
  loadSettings,
  saveSettings,
  testSettings,
} from '../store'

const emit = defineEmits(['close'])

const ROLE_LABELS = {
  planner: 'Planner（规划）',
  executor: 'Executor（执行）',
  verifier: 'Verifier（验收）',
  router: 'Router（复杂度路由）',
}

// ── 内置模型凭据（对所有**内置**模型生效）──
const baseUrl = ref('')
const apiKey = ref('')
const keyInfo = ref('')
const testing = ref(false)
const testResult = ref(null)
const saving = ref(false)

const models = ref([]) // 内置注册表 + 我的模型
const roles = ref({ planner: '', executor: '', verifier: '', router: '' })
const hetero = ref(false) // 验收使用异构模型（与 Executor 不同）

// ── 我的模型（用户自定义：各自带模型名 / 地址 / 密钥）──
const customModels = ref([])
const form = ref({ label: '', model: '', base_url: '', api_key: '' })
const formTesting = ref(false)
const formTestResult = ref(null)
const formBusy = ref(false)
const formError = ref('')

const roleKeys = computed(() => Object.keys(ROLE_LABELS))

async function refreshModels() {
  const m = await loadModels()
  models.value = m.models || []
  roles.value = { ...roles.value, ...(m.roles || {}) }
}

async function refreshSettings() {
  const s = await loadSettings()
  baseUrl.value = s.base_url || ''
  keyInfo.value = s.api_key_set ? `已配置（尾号 ${s.api_key_tail}）` : '未配置（使用 .env 默认）'
  customModels.value = s.customModels || []
  roles.value = { ...roles.value, ...(s.roles || {}) }
  return s
}

onMounted(async () => {
  await refreshSettings()
  await refreshModels()
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

/** 下拉框显示名：`label` 是给用户看的名字；与**实际调用名**不同时才补一句括号。
 *  （内置的 `deepseek-v4.1-flash → deepseek-flash` 就是这种情况：官方改名时显示名不动。） */
function modelLabel(m) {
  return m.model && m.model !== m.label ? `${m.label}（实际调用 ${m.model}）` : m.label
}

// ── 内置凭据：测试 / 保存 ──
const doTest = async () => {
  testing.value = true
  testResult.value = null
  try {
    // ⚠️ 带上当前 Executor 选的模型：不带的话后端会回落到 .env 的默认模型名，
    //    于是"测试通过"跟你在下拉框里选的那个模型其实没关系（假阳性）。
    testResult.value = await testSettings({
      base_url: baseUrl.value,
      api_key: apiKey.value || undefined,
      model_id: roles.value.executor || undefined,
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

// ── 我的模型：测试 / 新增 / 删除 ──
const doFormTest = async () => {
  formTesting.value = true
  formTestResult.value = null
  try {
    formTestResult.value = await testSettings({
      model: form.value.model,
      base_url: form.value.base_url,
      api_key: form.value.api_key || undefined,
    })
  } catch (e) {
    formTestResult.value = { ok: false, error: String(e) }
  } finally {
    formTesting.value = false
  }
}

const doAdd = async () => {
  formError.value = ''
  if (!form.value.model.trim()) {
    formError.value = '「模型名」必填（必须是服务方要求的调用名，例如 glm-5.3）'
    return
  }
  formBusy.value = true
  try {
    const res = await addCustomModel({ ...form.value })
    if (!res.ok) {
      formError.value = res.error || '添加失败'
      return
    }
    form.value = { label: '', model: '', base_url: '', api_key: '' }
    formTestResult.value = null
    // 自定义模型会出现在上面的下拉框里；roles 也可能被后端清理过 → 一起刷新
    await refreshSettings()
    await refreshModels()
  } finally {
    formBusy.value = false
  }
}

const doDelete = async (id) => {
  formError.value = ''
  const res = await deleteCustomModel(id)
  if (!res.ok) {
    formError.value = res.error || '删除失败'
    return
  }
  await refreshSettings()
  await refreshModels()
}
</script>

<template>
  <div class="mask" @click.self="emit('close')">
    <div class="panel">
      <div class="head">
        <span>模型配置</span>
        <button class="x" @click="emit('close')">✕</button>
      </div>

      <!-- ── 角色模型 ── -->
      <div class="section">角色模型<span class="hint">（内置 + 我的模型）</span></div>
      <template v-for="role in roleKeys" :key="role">
        <label class="row">
          <span class="row-label">{{ ROLE_LABELS[role] }}</span>
          <select
            v-model="roles[role]"
            :disabled="role === 'verifier' && !hetero"
            :title="role === 'verifier' && !hetero ? '勾选下面那项后才能单独为 Verifier 选模型' : ''"
            class="select"
          >
            <option value="">使用配置默认</option>
            <option v-for="m in models" :key="m.key" :value="m.key">
              {{ modelLabel(m) }}
            </option>
          </select>
        </label>
        <!-- 异构开关紧贴 Verifier 那一行：它是"能不能改 Verifier"的开关，
             放远了用户会以为 Verifier 是坏了 / 被禁用了 -->
        <label v-if="role === 'verifier'" class="check">
          <input v-model="hetero" type="checkbox" />
          <span>验收使用异构模型（与 Executor 不同）—— <b>勾选后才能单独切换 Verifier</b></span>
        </label>
      </template>

      <!-- ── 内置模型凭据 ── -->
      <div class="section">内置模型凭据<span class="hint">（对内置模型生效；我的模型用自己的）</span></div>
      <label>API 地址（留空用 .env 默认）</label>
      <input v-model="baseUrl" placeholder="如 https://api.deepseek.com" />

      <label>API Key</label>
      <input v-model="apiKey" type="password" :placeholder="`留空保持现有：${keyInfo}`" />

      <div v-if="testResult" class="test-result" :class="testResult.ok ? 'ok' : 'bad'">
        {{ testResult.ok
          ? `连接正常（${testResult.elapsedSec}s）｜实测模型：${testResult.testedModel}`
          : `连接失败：${testResult.error}` }}
      </div>
      <button class="ghost wide" :disabled="testing" @click="doTest">
        {{ testing ? '测试中...' : '测试内置凭据' }}
      </button>

      <!-- ── 我的模型 ── -->
      <div class="section">我的模型<span class="hint">（自己的地址 + 自己的密钥，加完出现在上面的下拉框里）</span></div>

      <div v-if="customModels.length" class="custom-list">
        <div v-for="m in customModels" :key="m.id" class="custom-item">
          <div class="custom-main">
            <b>{{ m.label }}</b>
            <span class="mono">{{ m.model }}</span>
            <span class="mono dim">{{ m.base_url || '（默认地址）' }}</span>
            <span class="dim">
              key {{ m.api_key_set ? `••••${m.api_key_tail}` : '未填（用内置凭据）' }}
            </span>
          </div>
          <button class="x" title="删除" @click="doDelete(m.id)">✕</button>
        </div>
      </div>
      <p v-else class="note">还没有自定义模型。</p>

      <div class="form">
        <label>显示名（随便起，只给你看）</label>
        <input v-model="form.label" placeholder="如 GLM-5.3" />
        <label>模型名（**必须**是服务方要求的调用名）</label>
        <input v-model="form.model" placeholder="如 glm-5.3" />
        <label>API 地址</label>
        <input v-model="form.base_url" placeholder="如 https://open.bigmodel.cn/api/paas/v4" />
        <label>API Key（留空则沿用内置凭据）</label>
        <input v-model="form.api_key" type="password" placeholder="只存在本机 runtime/web-settings.json" />

        <div v-if="formTestResult" class="test-result" :class="formTestResult.ok ? 'ok' : 'bad'">
          {{ formTestResult.ok
            ? `连接正常（${formTestResult.elapsedSec}s）｜实测模型：${formTestResult.testedModel}`
            : `连接失败：${formTestResult.error}` }}
        </div>
        <p v-if="formError" class="err">{{ formError }}</p>

        <div class="actions">
          <button class="ghost" :disabled="formTesting" @click="doFormTest">
            {{ formTesting ? '测试中...' : '测试连接' }}
          </button>
          <button class="ghost" :disabled="formBusy" @click="doAdd">
            {{ formBusy ? '添加中...' : '添加到我的模型' }}
          </button>
        </div>
      </div>

      <p class="note">
        内置模型来自 <code>config/models.json</code>（进仓库）；**我的模型与所有密钥**只存本机
        <code>runtime/web-settings.json</code> —— 该文件已 gitignore，**且含明文密钥，不要分享**。
        界面设置**优先级最高**（会盖住 .env 与 models.json）。
      </p>

      <div class="actions">
        <button class="primary" :disabled="saving" @click="doSave">
          {{ saving ? '保存中...' : '保存并生效' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mask { position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: flex; align-items: center; justify-content: center; z-index: 50; }
.panel { width: 560px; max-height: 88vh; overflow-y: auto; background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 20px; display: flex; flex-direction: column; gap: 6px; }
.head { display: flex; justify-content: space-between; align-items: center; font-size: 16px; font-weight: 700; margin-bottom: 4px; }
.x { background: none; border: none; color: #94a3b8; font-size: 14px; cursor: pointer; }
.section { margin-top: 14px; font-size: 13px; font-weight: 700; color: #e2e8f0; border-bottom: 1px solid #334155; padding-bottom: 4px; }
.hint { font-size: 11px; font-weight: 400; color: #64748b; margin-left: 6px; }
label { font-size: 12px; color: #94a3b8; margin-top: 6px; }
.row { display: flex; align-items: center; gap: 10px; }
.row-label { width: 160px; flex: none; }
.select { flex: 1; padding: 8px 10px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #e2e8f0; font-size: 12px; }
.select:disabled { opacity: 0.55; }
.check { display: flex; align-items: center; gap: 8px; margin: 2px 0 2px 170px; font-size: 12px; color: #cbd5e1; }
.check input { width: auto; }
.check b { color: #fbbf24; font-weight: 600; }
input { padding: 9px 12px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #e2e8f0; font-size: 13px; }
input:focus { outline: none; border-color: #3b82f6; }
.note { font-size: 11px; color: #64748b; line-height: 1.6; }
.note code { color: #93c5fd; }
.err { font-size: 12px; color: #fca5a5; }
.test-result { font-size: 12px; padding: 8px 10px; border-radius: 8px; margin-top: 6px; }
.test-result.ok { background: #14532d; color: #86efac; }
.test-result.bad { background: #450a0a; color: #fca5a5; }
.actions { display: flex; gap: 10px; margin-top: 10px; }
.actions button { flex: 1; padding: 10px; border-radius: 8px; font-size: 13px; cursor: pointer; }
.ghost { background: transparent; border: 1px solid #334155; color: #cbd5e1; }
.ghost.wide { margin-top: 8px; }
.primary { background: #2563eb; border: none; color: #fff; }
.actions button:disabled { opacity: 0.5; }
.custom-list { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
.custom-item { display: flex; align-items: center; gap: 8px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 8px 10px; }
.custom-main { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; font-size: 12px; color: #e2e8f0; flex: 1; }
.mono { font-family: ui-monospace, Consolas, monospace; color: #93c5fd; }
.dim { color: #64748b; }
.form { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; }
</style>
