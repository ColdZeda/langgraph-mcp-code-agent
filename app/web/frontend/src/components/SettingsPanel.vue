<script setup>
import { onMounted, ref } from 'vue'
import { loadSettings, saveSettings, testSettings } from '../store'

const emit = defineEmits(['close'])

const model = ref('')
const baseUrl = ref('')
const apiKey = ref('')
const keyInfo = ref('')
const testing = ref(false)
const testResult = ref(null)
const saving = ref(false)

onMounted(async () => {
  const s = await loadSettings()
  model.value = s.model || ''
  baseUrl.value = s.base_url || ''
  keyInfo.value = s.api_key_set ? `已配置（尾号 ${s.api_key_tail}）` : '未配置（使用 .env 默认）'
})

const doTest = async () => {
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await testSettings({
      model: model.value, base_url: baseUrl.value,
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
    await saveSettings({
      model: model.value, base_url: baseUrl.value,
      ...(apiKey.value ? { api_key: apiKey.value } : {}),
    })
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
        <span>模型设置</span>
        <button class="x" @click="emit('close')">✕</button>
      </div>

      <label>模型名称</label>
      <input v-model="model" placeholder="如 deepseek-v4-flash（留空用 .env 默认）" />

      <label>API 地址</label>
      <input v-model="baseUrl" placeholder="如 https://api.deepseek.com（留空用 .env 默认）" />

      <label>API Key</label>
      <input v-model="apiKey" type="password" :placeholder="`留空保持现有：${keyInfo}`" />
      <p class="note">设置保存在本机 runtime/web-settings.json（已被 .gitignore 忽略，不会进仓库）</p>

      <div v-if="testResult" class="test-result" :class="testResult.ok ? 'ok' : 'bad'">
        {{ testResult.ok ? `连接正常（${testResult.elapsedSec}s）` : `连接失败：${testResult.error}` }}
      </div>

      <div class="actions">
        <button class="ghost" :disabled="testing" @click="doTest">{{ testing ? '测试中...' : '测试连接' }}</button>
        <button class="primary" :disabled="saving" @click="doSave">{{ saving ? '保存中...' : '保存并生效' }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mask { position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: flex; align-items: center; justify-content: center; z-index: 50; }
.panel { width: 460px; background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 20px; display: flex; flex-direction: column; gap: 8px; }
.head { display: flex; justify-content: space-between; align-items: center; font-size: 16px; font-weight: 700; margin-bottom: 6px; }
.x { background: none; border: none; color: #94a3b8; font-size: 14px; cursor: pointer; }
label { font-size: 12px; color: #94a3b8; margin-top: 6px; }
input { padding: 9px 12px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #e2e8f0; font-size: 13px; }
input:focus { outline: none; border-color: #3b82f6; }
.note { font-size: 11px; color: #64748b; }
.test-result { font-size: 12px; padding: 8px 10px; border-radius: 8px; }
.test-result.ok { background: #14532d; color: #86efac; }
.test-result.bad { background: #450a0a; color: #fca5a5; }
.actions { display: flex; gap: 10px; margin-top: 10px; }
.actions button { flex: 1; padding: 10px; border-radius: 8px; font-size: 13px; cursor: pointer; }
.ghost { background: transparent; border: 1px solid #334155; color: #cbd5e1; }
.primary { background: #2563eb; border: none; color: #fff; }
.actions button:disabled { opacity: 0.5; }
</style>
