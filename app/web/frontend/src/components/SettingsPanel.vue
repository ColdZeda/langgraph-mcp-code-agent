<script setup>
/**
 * 模型设置面板（阶段 7 · T7.6 **用户化改造**）。
 *
 * 这一版砍掉了所有"开发者视角"的东西，起因是用户走查时的一句：
 * **"（实际调用 xxx）不像面向用户的"** —— 顺着查下去发现不止一处（见
 * `docs/evidence/阶段7_WEB端走查与修复.md` §五）。改动清单：
 *
 * 1. **删掉整节「系统默认模型」**（两个"覆盖 `.env`"的输入框 + 「测试系统默认模型」按钮 +
 *    `MODEL_NAME / MODEL_BASE_URL / MODEL_API_KEY` 三个变量名）。
 *    依据：那一节只写进本机 `runtime/web-settings.json`，**只有这个 Web 进程自己读**，
 *    CLI 与 evals 完全不看它（`load_settings`/`apply_settings` 只存在于 `app/web/server.py`）
 *    ⇒ 删掉不影响任何评估口径。**后端接口原样保留**，只是界面不再露出来。
 * 2. **下拉框里不再有「（实际调用 xxx）」**，第一项也不再叫「使用配置默认」——
 *    显示名就是显示名，实际调用名进 hover tooltip（排查时鼠标一放就能看到）。
 * 3. **空状态**：一条自定义模型都没有时，四个角色下拉置灰 + 一句"还没有可用的模型 ——
 *    在下面填一个 API Key 就能开始" + 一个高亮的「＋ 添加你的模型」。
 *    配合后端 `session` 消息里的 `modelReady`，聊天区也会提示并禁用发送。
 * 4. **Verifier 不再是个灰掉的下拉框**：以前要先勾"验收使用异构模型"才能选它，
 *    用户第一反应是"为什么点不了"。现在四个角色都能直接选；Verifier 与 Executor 相同时
 *    给一句温和提示（换成另一个模型，验收更独立）。
 * 5. 底部那段"给仓库维护者看"的注释删掉，只留一句"这些设置只保存在你自己的电脑上"。
 * 6. 裸露的 Markdown 星号（`**…**`）去掉 —— 它们在普通文本节点里不会被渲染成加粗。
 */
import { computed, onMounted, ref } from 'vue'
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

// 角色措辞：前三个是产品主线（README 里解释过的三阶段），Router 只在 auto 模式用。
const ROLES = [
  { key: 'planner', label: 'Planner（规划）' },
  { key: 'executor', label: 'Executor（执行）' },
  { key: 'verifier', label: 'Verifier（验收）' },
  { key: 'router', label: 'Router（判断任务复杂度）', hint: '仅 auto 模式使用' },
]

const roles = ref({})
const customModels = ref([])
const notice = ref('')
const error = ref('')
const saving = ref(false)
const testingCurrent = ref(false)

// 「我的模型」编辑表单
const showForm = ref(false)
const form = ref({ id: '', label: '', model: '', base_url: '', api_key: '' })
const formError = ref('')
const formTesting = ref(false)
const formTestResult = ref(null)
const formSaving = ref(false)
const confirmDelete = ref('') // 二次确认：正在等"确认删除"的模型 id

const hasModels = computed(() => customModels.value.length > 0)
const modelOptions = computed(() =>
  customModels.value.map((m) => ({ value: m.id, label: m.label || m.model }))
)
const verifierSameAsExecutor = computed(
  () => (roles.value.verifier || '') === (roles.value.executor || '')
)

async function refresh() {
  const [settings] = await Promise.all([loadSettings(), loadModels()])
  roles.value = { ...(settings.roles || {}) }
  customModels.value = settings.customModels || []
}

onMounted(refresh)

/** 下拉项也要能"看真身"：实际调用名进 tooltip。
 *  （显示名与调用名是两个字段：官方把 `V4 Flash` 改成 `V4.1 Flash` 时只动后者。） */
function optionTitle(id) {
  const m = customModels.value.find((x) => x.id === id)
  return m ? `实际调用 ${m.model}` : ''
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    const res = await saveSettings({ roles: { ...roles.value } })
    if (res && res.ok === false) {
      error.value = res.error || '保存失败'
      return
    }
    notice.value = '已保存 —— 从下一条消息开始生效（正在跑的那条不受影响）'
    await refresh()
  } finally {
    saving.value = false
  }
}

/** 测"现在真正在用"的那个模型：Executor 选了哪个就测哪个。 */
async function testCurrent() {
  testingCurrent.value = true
  formTestResult.value = null
  notice.value = ''
  try {
    const id = roles.value.executor || ''
    const payload = id && customModels.value.some((m) => m.id === id) ? { model_id: id } : {}
    const res = await testSettings(payload)
    notice.value = res.ok
      ? `当前模型连接正常（${res.elapsedSec}s）｜实测：${res.testedModel}`
      : `当前模型连接失败：${res.error}`
  } finally {
    testingCurrent.value = false
  }
}

function startAdd() {
  form.value = { id: '', label: '', model: '', base_url: '', api_key: '' }
  formError.value = ''
  formTestResult.value = null
  showForm.value = true
}

function startEdit(m) {
  form.value = {
    id: m.id,
    label: m.label || '',
    model: m.model || '',
    base_url: m.base_url || '',
    api_key: '',
  }
  formError.value = ''
  formTestResult.value = null
  showForm.value = true
}

async function submitForm() {
  formError.value = ''
  if (!form.value.model.trim()) {
    formError.value = '「模型名」必填：必须是服务方要求的调用名（例如 glm-5.3）'
    return
  }
  formSaving.value = true
  try {
    const payload = {
      id: form.value.id || undefined,
      label: form.value.label,
      model: form.value.model,
      base_url: form.value.base_url,
      api_key: form.value.api_key,
    }
    const res = await addCustomModel(payload)
    if (!res.ok) {
      formError.value = res.error || '保存失败'
      return
    }
    const wasEdit = Boolean(form.value.id)
    showForm.value = false
    await refresh()
    notice.value = wasEdit
      ? '已更新'
      : '已添加 —— 四个角色默认都用它；需要的话可以在上面单独改某个角色'
  } finally {
    formSaving.value = false
  }
}

async function testForm() {
  formTesting.value = true
  formTestResult.value = null
  try {
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

async function remove(id) {
  const res = await deleteCustomModel(id)
  confirmDelete.value = ''
  if (!res.ok) {
    error.value = res.error || '删除失败'
    return
  }
  await refresh()
  notice.value = '已删除'
}
</script>

<template>
  <div class="mask" @click.self="emit('close')">
    <div class="panel">
      <div class="head">
        <span>模型设置</span>
        <button class="x" @click="emit('close')">✕</button>
      </div>

      <!-- ── 角色模型 ───────────────────────────────────────────── -->
      <div class="section">
        角色模型<span class="hint">（不给某个角色单独选的话，四个角色都用同一个）</span>
      </div>

      <div v-if="!hasModels" class="empty">
        <p class="empty-title">还没有可用的模型</p>
        <p class="empty-sub">在下面填一个 API Key 就能开始 —— 支持任何 OpenAI 兼容的服务。</p>
      </div>

      <label v-for="r in ROLES" :key="r.key" class="row">
        <span class="row-label">{{ r.label }}</span>
        <select
          v-model="roles[r.key]"
          class="select"
          :disabled="!hasModels || store.sending"
          :title="optionTitle(roles[r.key])"
        >
          <option value="">{{ hasModels ? '（跟随 Executor）' : '还没有模型' }}</option>
          <option
            v-for="m in modelOptions"
            :key="m.value"
            :value="m.value"
            :title="optionTitle(m.value)"
          >
            {{ m.label }}
          </option>
        </select>
        <span v-if="r.hint" class="row-hint">{{ r.hint }}</span>
        <span
          v-else-if="r.key === 'verifier' && verifierSameAsExecutor && hasModels"
          class="row-hint"
        >
          与执行相同（换成另一个模型，验收更独立）
        </span>
      </label>

      <!-- ── 我的模型 ───────────────────────────────────────────── -->
      <div class="section">我的模型<span class="hint">（自己的地址 + 自己的密钥）</span></div>

      <div v-if="hasModels" class="custom-list">
        <div v-for="m in customModels" :key="m.id" class="custom-item">
          <div class="custom-main">
            <b>{{ m.label || m.model }}</b>
            <span class="mono">{{ m.model }}</span>
          </div>
          <div class="custom-actions">
            <button class="mini" @click="startEdit(m)">编辑</button>
            <button
              class="mini danger"
              @click="confirmDelete === m.id ? remove(m.id) : (confirmDelete = m.id)"
            >
              {{ confirmDelete === m.id ? '确认删除？' : '删除' }}
            </button>
          </div>
        </div>
      </div>

      <button v-if="!showForm" class="ghost wide add" @click="startAdd">
        ＋ 添加你的模型
      </button>

      <div v-if="showForm" class="form">
        <div class="form-title">{{ form.id ? '编辑模型' : '添加模型' }}</div>
        <label>显示名（随便起，只给你看）</label>
        <input v-model="form.label" placeholder="如 GLM-5.3" />
        <label>模型名（<b>必须</b>是服务方要求的调用名）</label>
        <input v-model="form.model" placeholder="如 glm-5.3" />
        <label>API 地址</label>
        <input v-model="form.base_url" placeholder="如 https://open.bigmodel.cn/api/paas/v4" />
        <label>API Key{{ form.id ? '（留空则不改动已保存的）' : '' }}</label>
        <input v-model="form.api_key" type="password" placeholder="只保存在你自己的电脑上" />
        <div v-if="formTestResult" class="result" :class="formTestResult.ok ? 'ok' : 'bad'">
          {{
            formTestResult.ok
              ? `连接正常（${formTestResult.elapsedSec}s）｜实测模型：${formTestResult.testedModel}`
              : `连接失败：${formTestResult.error}`
          }}
        </div>
        <p v-if="formError" class="err">{{ formError }}</p>
        <div class="actions">
          <button class="ghost" :disabled="formTesting" @click="testForm">
            {{ formTesting ? '测试中...' : '测试连接' }}
          </button>
          <button class="primary" :disabled="formSaving" @click="submitForm">
            {{ formSaving ? '保存中...' : form.id ? '保存修改' : '添加' }}
          </button>
          <button class="ghost" @click="showForm = false">取消</button>
        </div>
      </div>

      <p class="note">这些设置只保存在你自己的电脑上，不会上传。</p>
      <div v-if="notice" class="result ok">{{ notice }}</div>
      <div v-if="error" class="result bad">{{ error }}</div>

      <div class="actions footer">
        <button class="ghost" :disabled="testingCurrent || !hasModels" @click="testCurrent">
          {{ testingCurrent ? '测试中...' : '测试当前模型' }}
        </button>
        <button class="primary" :disabled="saving" @click="save">
          {{ saving ? '保存中...' : '保存并生效' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mask { position: fixed; inset: 0; background: rgba(2, 6, 23, 0.66); display: flex;
  align-items: center; justify-content: center; z-index: 50; }
.panel { width: 620px; max-width: 92vw; max-height: 88vh; overflow-y: auto; background: #0f172a;
  border: 1px solid #334155; border-radius: 14px; padding: 18px 20px; display: flex;
  flex-direction: column; gap: 8px; }
.head { display: flex; align-items: center; font-size: 16px; font-weight: 700; color: #e2e8f0;
  margin-bottom: 4px; }
.x { margin-left: auto; background: none; border: none; color: #94a3b8; font-size: 16px;
  cursor: pointer; }
.section { font-size: 13px; font-weight: 600; color: #93c5fd; margin-top: 10px; }
.hint { color: #64748b; font-weight: 400; font-size: 12px; }
.row { display: flex; align-items: center; gap: 10px; }
.row-label { width: 190px; font-size: 13px; color: #cbd5e1; }
.row-hint { font-size: 11px; color: #64748b; margin-left: 8px; }
.select { flex: 1; padding: 6px 8px; background: #0b1220; color: #e2e8f0;
  border: 1px solid #334155; border-radius: 8px; font-size: 13px; }
.select:disabled { opacity: 0.6; }
.empty { padding: 12px 14px; border: 1px dashed #475569; border-radius: 10px; background: #0b1220; }
.empty-title { margin: 0; font-size: 14px; color: #fde68a; font-weight: 600; }
.empty-sub { margin: 4px 0 0; font-size: 12.5px; color: #94a3b8; }
.custom-list { display: flex; flex-direction: column; gap: 6px; }
.custom-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; background: #0b1220;
  border: 1px solid #334155; border-radius: 8px; }
.custom-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.custom-main b { font-size: 13px; color: #e2e8f0; }
.mono { font-family: Consolas, monospace; font-size: 11.5px; color: #93c5fd; }
.custom-actions { margin-left: auto; display: flex; gap: 6px; }
.mini { border: none; background: transparent; color: #94a3b8; cursor: pointer; font-size: 11.5px;
  padding: 2px 6px; border-radius: 6px; }
.mini:hover { background: #334155; color: #e2e8f0; }
.mini.danger { color: #fca5a5; }
.mini.danger:hover { background: #450a0a; }
.ghost { padding: 8px; border: 1px solid #334155; background: transparent; color: #cbd5e1;
  border-radius: 8px; cursor: pointer; font-size: 13px; }
.ghost:hover { background: #334155; }
.ghost:disabled { opacity: 0.5; cursor: not-allowed; }
.add { border-color: #3b82f6; color: #bfdbfe; margin-top: 4px; }
.wide { width: 100%; }
.form { display: flex; flex-direction: column; gap: 6px; padding: 12px; border: 1px solid #334155;
  border-radius: 10px; background: #0b1220; }
.form-title { font-size: 13px; font-weight: 600; color: #e2e8f0; margin-bottom: 2px; }
.form label { font-size: 12px; color: #94a3b8; }
.form input { padding: 7px 10px; border-radius: 8px; border: 1px solid #334155; background: #0f172a;
  color: #e2e8f0; font-size: 13px; font-family: inherit; }
.form input:focus { outline: none; border-color: #3b82f6; }
.actions { display: flex; gap: 8px; margin-top: 6px; }
.primary { flex: 1; padding: 9px; border: none; border-radius: 8px; background: #2563eb; color: #fff;
  font-size: 13px; cursor: pointer; }
.primary:disabled { opacity: 0.6; cursor: not-allowed; }
.footer { margin-top: 8px; }
.result { font-size: 12px; padding: 8px 10px; border-radius: 8px; }
.result.ok { background: #052e16; color: #86efac; }
.result.bad { background: #450a0a; color: #fca5a5; }
.err { font-size: 12px; color: #fca5a5; margin: 2px 0 0; }
.note { font-size: 11.5px; color: #64748b; margin: 8px 0 0; }
</style>
