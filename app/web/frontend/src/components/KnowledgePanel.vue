<template>
  <div class="overlay" @click.self="$emit('close')">
    <div class="panel">
      <div class="head">
        <b>🧠 知识库</b>
        <span class="sub">宿主自动沉淀 + 手工保存的长期记忆（会作为「相关经验」注入到任务开头）</span>
        <button class="x" @click="$emit('close')">✕</button>
      </div>

      <p v-if="loading" class="hint">读取中…</p>
      <p v-else-if="!items.length" class="hint">知识库还是空的。任务成功后会**自动沉淀**经验到这里。</p>

      <ul v-else class="list">
        <li v-for="it in items" :key="it.name" class="row">
          <div class="row-head">
            <b class="name">{{ it.name }}</b>
            <span class="meta">{{ (it.size / 1024).toFixed(1) }} KB · {{ shortTime(it.mtime) }}</span>
            <button class="del" :disabled="busy === it.name" @click="remove(it)">
              {{ busy === it.name ? '删除中…' : '删除' }}
            </button>
          </div>
          <div class="preview">{{ it.preview }}</div>
        </li>
      </ul>

      <p v-if="error" class="error">{{ error }}</p>
      <p class="foot">
        ⚠️ 删除会**同时删掉向量**（否则检索还会命中"幽灵条目"）；删了就真的没了，没有回收站。
      </p>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { deleteKnowledge, loadKnowledge } from '../store'

defineEmits(['close'])

const items = ref([])
const loading = ref(true)
const busy = ref('')
const error = ref('')

function shortTime(ts) {
  const d = new Date(ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function refresh() {
  loading.value = true
  items.value = await loadKnowledge()
  loading.value = false
}

async function remove(item) {
  if (!window.confirm(`删除「${item.name}」？文件与向量都会删掉，不能撤销。`)) return
  busy.value = item.name
  error.value = ''
  const res = await deleteKnowledge(item.name)
  busy.value = ''
  if (!res.ok) {
    error.value = `删除失败：${res.error || '未知原因'}`
    return
  }
  items.value = res.items || []
}

onMounted(refresh)
</script>

<style scoped>
.overlay { position: fixed; inset: 0; background: rgba(2, 6, 23, 0.72); display: flex; align-items: center; justify-content: center; z-index: 60; }
.panel { width: min(760px, 92vw); max-height: 82vh; overflow: auto; background: #0f172a; border: 1px solid #334155; border-radius: 12px; padding: 18px 20px; color: #e2e8f0; }
.head { display: flex; align-items: baseline; gap: 10px; }
.head .sub { font-size: 12px; color: #94a3b8; }
.head .x { margin-left: auto; background: transparent; border: none; color: #94a3b8; font-size: 16px; cursor: pointer; }
.hint { color: #94a3b8; font-size: 13px; }
.list { list-style: none; padding: 0; margin: 12px 0; }
.row { border: 1px solid #1e293b; border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; background: #111c2e; }
.row-head { display: flex; align-items: center; gap: 10px; }
.row-head .name { font-size: 13px; color: #bfdbfe; }
.row-head .meta { font-size: 11.5px; color: #64748b; }
.row-head .del { margin-left: auto; font-size: 12px; padding: 3px 10px; border-radius: 6px; border: 1px solid #7f1d1d; background: #450a0a; color: #fecaca; cursor: pointer; }
.row-head .del:disabled { opacity: 0.6; cursor: default; }
.preview { margin-top: 6px; font-size: 12px; line-height: 1.6; color: #cbd5e1; }
.error { color: #fca5a5; font-size: 12.5px; }
.foot { font-size: 11.5px; color: #64748b; margin-top: 10px; }
</style>
