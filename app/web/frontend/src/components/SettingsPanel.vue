<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  addCustomModel,
  deleteCustomModel,
  loadModels,
  loadSettings,
  saveSettings,
  store,
  testSettings,
} from '../store'

const emit = defineEmits(['close'])

const ROLE_LABELS = {
  planner: 'Planner（规划）',
  executor: 'Executor（执行）',
  verifier: 'Verifier（验收）',
  router: 'Router（复杂度路由）',
}

// ── ① 系统默认模型（= 直接走 .env 的那套：MODEL_NAME / MODEL_BASE_URL / MODEL_API_KEY）──
const baseUrl = ref('')
const apiKey = ref('')
const keyInfo = ref('')
const testing = ref(false)
const testResult = ref(null)
const saving = ref(false)
const saveNotice = ref('')

const models = ref([]) // 「我的模型」列表（内置预设刻意不再暴露给用户）
const roles = ref({ planner: '', executor: '', verifier: '', router: '' })
const hetero = ref(false) // 验收使用异构模型（与 Executor 不同）

// ── ② 我的模型（管理：每行 编辑/删除 + 一个折叠表单）──
const customModels = ref([])
const formOpen = ref(false)
const form = ref({ id: '', label: '', model: '', base_url: '', api_key: '' })
const formTesting = ref(false)
const formTestResult = ref(null)
const formBusy = ref(false)
const formError = ref('')
const pendingDelete = ref('') // 二次确认：第一次点"删除"只是记下来

const roleKeys = computed(() => Object.keys(ROLE_LABELS))
const formEditing = computed(() => Boolean(form.value.id))

/** 每个角色的下拉选项 = 我的模型；若**已保存的值不在列表里**（例如以前指向某个被删掉的模型），
 *  额外补一项把它显示出来 —— 否则 select 显示空白，用户以为"什么都没选"，
 *  下次保存就把这个值**静默丢掉**了。 */
const roleOptions = computed(() =>
  roleKeys.value.map((role) => {
    const keys = new Set(models.value.map((m) => m.key))
    const current = roles.value[role]
    const extra =
      current && !keys.has(current)
        ? [{ key: current, label: `${current}（已选，不在列表里）`, model: '' }]
        : []
    return { role, options: [...extra, ...models.value] }
  })
)

/** 下拉框显示名：`label` 给用户看；与**实际调用名**不同时才补括号。 */
function modelLabel(m) {
  return m.model && m.model !== m.label ? `${m.label}（实际调用 ${m.model}）` : m.label
}

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

// ── 系统默认模型：测试 / 保存 ──
const doTest = async () => {
  testing.value = true
  testResult.value = null
  try {
    // ⚠️ 刻意**不带** model_id：这个按钮测的就是"系统默认那套"（模型名 = .env 的 MODEL_NAME，
    //    地址/密钥用下面两个框 —— 留空则用 .env 的值）。响应里的 testedModel 会显示测的是谁。
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
  saveNotice.value = ''
  try {
    const payload = { base_url: baseUrl.value, roles: { ...roles.value } }
    if (apiKey.value) payload.api_key = apiKey.value
    await saveSettings(payload)
    await refreshModels() // 顶栏的"当前生效模型"要跟着变
    saveNotice.value = '已保存并生效 —— 配置改动从**下一条消息**开始生效（正在跑的那条不受影响）'
  } finally {
    saving.value = false
  }
}

// ── 我的模型：增 / 改 / 删 / 测 ──
function openAdd() {
  form.value = { id: '', label: '', model: '', base_url: '', api_key: '' }
  formOpen.value = true
  formError.value = ''
  formTestResult.value = null
}

function openEdit(m) {
  form.value = { id: m.id, label: m.label, model: m.model, base_url: m.base_url, api_key: '' }
  formOpen.value = true
  formError.value = ''
  formTestResult.value = null
}

function closeForm() {
  formOpen.value = false
}

const doFormTest = async () => {
  formTesting.value = true
  formTestResult.value = null
  try {
    // 编辑已有模型且没重填 key：用 model_id 让后端取已保存的密钥来测
    const payload = form.value.api_key
      ? { model: form.value.model, base_url: form.value.base_url, api_key: form.value.api_key }
      : {
          model_id: form.value.id || undefined,
          model: form.value.model,
          base_url: form.value.base_url,
        }
    formTestResult.value = await testSettings(payload)
  } catch (e) {
    formTestResult.value = { ok: false, error: String(e) }
  } finally {
    formTesting.value = false
  }
}

const doSubmitForm = async () => {
  formError.value = ''
  if (!form.value.model.trim()) {
    formError.value = '「模型名」必填（必须是服务方要求的调用名，例如 glm-5.3）'
    return
  }
  formBusy.value = true
  try {
    const res = await addCustomModel({ ...form.value })
    if (!res.ok) {
      formError.value = res.error || '保存失败'
      return
    }
    formOpen.value = false
    await refreshSettings()
    await refreshModels()
  } finally {
    formBusy.value = false
  }
}

/** 删除要**点两次**（第一次只是把 id 记下来问一句）。
 *  它会连带清掉角色里指向它的引用，属于破坏性操作；而且若此刻有任务在跑，
 *  那条任务用的是"启动时的配置快照"——所以这里要提醒"从下一条消息生效"。 */
const doDelete = async (id) => {
  formError.value = ''
  if (pendingDelete.value !== id) {
    pendingDelete.value = id
    return
  }
  pendingDelete.value = ''
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
      <div class="section">
        角色模型<span class="hint">（默认 = 系统默认；也可以选「我的模型」里加的那些）</span>
      </div>
      <template v-for="entry in roleOptions" :key="entry.role">
        <label class="row">
          <span class="row-label">{{ ROLE_LABELS[entry.role] }}</span>
          <select
            v-model="roles[entry.role]"
            :disabled="entry.role === 'verifier' && !hetero"
            :title="entry.role === 'verifier' && !hetero ? '勾选下面那项后才能单独为 Verifier 选模型' : ''"
            class="select"
          >
            <option value="">使用配置默认</option>
            <option v-for="m in entry.options" :key="m.key" :value="m.key">
              {{ modelLabel(m) }}
            </option>
          </select>
        </label>
        <!-- 异构开关紧贴 Verifier 那一行：它是"能不能改 Verifier"的开关，
             放远了用户会以为 Verifier 是坏了 / 被禁用了 -->
        <label v-if="entry.role === 'verifier'" class="check">
          <input v-model="hetero" type="checkbox" />
          <span>验收使用异构模型（与 Executor 不同）—— <b>勾选后才能单独切换 Verifier</b></span>
        </label>
      </template>

      <!-- ── 系统默认模型（= .env 那套）── -->
      <div class="section">
        系统默认模型<span class="hint">（来自 .env：MODEL_NAME / MODEL_BASE_URL / MODEL_API_KEY）</span>
      </div>
      <p class="note">
        不动这里的话，四个角色默认就用 <code>.env</code> 里那套。
        下面两个框是**覆盖** <code>.env</code> 的值（留空即用 <code>.env</code>）。
      </p>
      <label>API 地址（留空用 .env 的 MODEL_BASE_URL）</label>
      <input v-model="baseUrl" placeholder="如 https://api.deepseek.com" />
      <label>API Key（留空保持现有）</label>
      <input v-model="apiKey" type="password" :placeholder="`留空保持现有：${keyInfo}`" />

      <div v-if="testResult" class="test-result" :class="testResult.ok ? 'ok' : 'bad'">
        {{ testResult.ok
          ? `连接正常（${testResult.elapsedSec}s）｜实测模型：${testResult.testedModel}`
          : `连接失败：${testResult.error}` }}
      </div>
      <button class="ghost wide" :disabled="testing" @click="doTest">
        {{ testing ? '测试中...' : '测试系统默认模型' }}
      </button>

      <!-- ── 我的模型（管理）── -->
      <div class="section">
        我的模型<span class="hint">（自己的地址 + 自己的密钥；加完会出现在上面的下拉框里）</span>
      </div>

      <div v-if="customModels.length" class="custom-list">
        <div v-for="m in customModels" :key="m.id" class="custom-item">
          <div class="custom-main">
            <b>{{ m.label }}</b>
            <span class="mono">{{ m.model }}</span>
            <span class="mono dim">{{ m.base_url || '（默认地址）' }}</span>
            <span class="dim">
              key {{ m.api_key_set ? `••••${m.api_key_tail}` : '未填（用系统默认凭据）' }}
            </span>
          </div>
          <div class="custom-actions">
            <button class="mini" @click="openEdit(m)">编辑</button>
            <button class="mini danger" @click="doDelete(m.id)">
              {{ pendingDelete === m.id ? '确认删除？' : '删除' }}
            </button>
          </div>
          <p v-if="pendingDelete === m.id" class="warn">
            删除会同时清掉「角色模型」里指向它的选择。
            <template v-if="store.sending">
              ⚠️ 当前有任务正在跑：删除**从下一条消息**生效，正在跑的那条不受影响。
            </template>
          </p>
        </div>
      </div>
      <p v-else class="note">还没有自定义模型。点下面的「＋ 添加模型」填自己的地址与密钥。</p>

      <button v-if="!formOpen" class="ghost wide" @click="openAdd">＋ 添加模型</button>

      <div v-if="formOpen" class="form">
        <div class="form-title">
          {{ formEditing ? `编辑：${form.label || form.model}` : '添加模型' }}
        </div>
        <label>显示名（随便起，只给你看）</label>
        <input v-model="form.label" placeholder="如 GLM-5.3" />
        <label>模型名（<b>必须</b>是服务方要求的调用名）</label>
        <input v-model="form.model" placeholder="如 glm-5.3" />
        <label>API 地址</label>
        <input v-model="form.base_url" placeholder="如 https://open.bigmodel.cn/api/paas/v4" />
        <label>API Key{{ formEditing ? '（留空则不改动原密钥）' : '（留空则用系统默认凭据）' }}</label>
        <input
          v-model="form.api_key"
          type="password"
          placeholder="只存在本机 runtime/web-settings.json"
        />

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
          <button class="primary" :disabled="formBusy" @click="doSubmitForm">
            {{ formBusy ? '保存中...' : formEditing ? '保存修改' : '添加' }}
          </button>
          <button class="ghost" @click="closeForm">取消</button>
        </div>
      </div>

      <p class="note">
        模型配置在 <code>config/models.json</code>（进仓库）；**我的模型与所有密钥**只存本机
        <code>runtime/web-settings.json</code> —— 已 gitignore，**且含明文密钥，不要分享**。
        界面设置**优先级最高**（盖住 <code>.env</code> 与 <code>models.json</code>）；
        改 <code>.env</code> 需**重启**才生效。
      </p>

      <div v-if="saveNotice" class="test-result ok">{{ saveNotice }}</div>
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
.panel { width: 600px; max-height: 88vh; overflow-y: auto; background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 20px; display: flex; flex-direction: column; gap: 6px; }
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
.warn { font-size: 11px; color: #fbbf24; line-height: 1.5; flex-basis: 100%; }
.test-result { font-size: 12px; padding: 8px 10px; border-radius: 8px; margin-top: 6px; }
.test-result.ok { background: #14532d; color: #86efac; }
.test-result.bad { background: #450a0a; color: #fca5a5; }
.actions { display: flex; gap: 10px; margin-top: 10px; }
.actions button { flex: 1; padding: 10px; border-radius: 8px; font-size: 13px; cursor: pointer; }
.ghost { background: transparent; border: 1px solid #334155; color: #cbd5e1; }
.ghost.wide { margin-top: 8px; width: 100%; padding: 9px; border-radius: 8px; font-size: 13px; cursor: pointer; }
.primary { background: #2563eb; border: none; color: #fff; }
.actions button:disabled { opacity: 0.5; }
.custom-list { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
.custom-item { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 8px 10px; }
.custom-main { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; font-size: 12px; color: #e2e8f0; flex: 1; min-width: 0; }
.custom-actions { display: flex; gap: 6px; }
.mini { font-size: 11px; padding: 4px 8px; border-radius: 6px; border: 1px solid #334155; background: transparent; color: #cbd5e1; cursor: pointer; }
.mini.danger { border-color: #7f1d1d; color: #fca5a5; }
.mono { font-family: ui-monospace, Consolas, monospace; color: #93c5fd; }
.dim { color: #64748b; }
.form { display: flex; flex-direction: column; gap: 4px; margin-top: 10px; padding: 12px; border: 1px solid #334155; border-radius: 10px; background: #16213a; }
.form-title { font-size: 12px; font-weight: 700; color: #93c5fd; margin-bottom: 2px; }
.form b { color: #fbbf24; }
</style>
