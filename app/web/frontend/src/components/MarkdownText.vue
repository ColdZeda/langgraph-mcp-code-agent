<script setup>
/**
 * Markdown 渲染（阶段 7 · T7.5）。
 *
 * 为什么要有这个组件：答案**一直是纯文本**（父容器 `white-space: pre-wrap`），
 * 模型输出的 `## 标题` / `**加粗**` / `| 表格 |` 全部以**源码**显示 —— 用户实测反馈过。
 *
 * ⚠️ 两条硬约束：
 *  1. **必须消毒**：`finalResponse` 是**模型输出的不可信内容**（其中还可能夹着工具返回的网页内容），
 *     直接 `v-html` 等于自己开一个 XSS 入口 ⇒ 一律过 `DOMPurify.sanitize()`。
 *  2. **`breaks: true`**：Markdown 里单个换行默认被合并成空格，而模型的答复经常是"一行一个要点"，
 *     不开这个开关会把排版揉成一坨（旧行为是 `pre-wrap`，等于保留换行）。
 */
import { computed } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

const props = defineProps({ text: { type: String, default: '' } })

const html = computed(() => {
  const raw = props.text || ''
  if (!raw) return ''
  const parsed = marked.parse(raw, { gfm: true, breaks: true })
  return DOMPurify.sanitize(parsed, { USE_PROFILES: { html: true } })
})
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html —— 内容已过 DOMPurify，见上面的注释 -->
  <div class="md" v-html="html"></div>
</template>

<style scoped>
/* ⚠️ 必须是 normal：父容器（气泡 / 结果卡）历史上是 `pre-wrap`，
   那会让 marked 输出的**块级标签之间的换行**也变成真实换行 = 满屏空白。 */
.md {
  white-space: normal;
  font-size: 14px;
  line-height: 1.7;
  color: #e2e8f0;
  word-break: break-word;
}
.md :deep(> *:first-child) { margin-top: 0; }
.md :deep(> *:last-child) { margin-bottom: 0; }
.md :deep(h1),
.md :deep(h2),
.md :deep(h3),
.md :deep(h4) { color: #f1f5f9; line-height: 1.35; margin: 14px 0 8px; }
.md :deep(h1) { font-size: 19px; }
.md :deep(h2) { font-size: 17px; }
.md :deep(h3) { font-size: 15px; }
.md :deep(p) { margin: 8px 0; }
.md :deep(ul),
.md :deep(ol) { margin: 8px 0; padding-left: 22px; }
.md :deep(li) { margin: 3px 0; }
.md :deep(li > p) { margin: 2px 0; }
.md :deep(strong) { color: #f8fafc; font-weight: 600; }
.md :deep(em) { color: #cbd5e1; }
.md :deep(a) { color: #60a5fa; text-decoration: underline; }
.md :deep(code) {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 5px;
  padding: 1px 5px;
  font-size: 12.5px;
  color: #fbbf24;
}
.md :deep(pre) {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 8px;
  padding: 10px 12px;
  overflow-x: auto;
  margin: 10px 0;
}
.md :deep(pre code) { background: none; border: none; padding: 0; color: #e2e8f0; }
.md :deep(blockquote) {
  margin: 10px 0;
  padding: 2px 12px;
  border-left: 3px solid #3b82f6;
  color: #cbd5e1;
  background: rgba(59, 130, 246, 0.08);
}
.md :deep(table) { border-collapse: collapse; margin: 10px 0; font-size: 13px; display: block; overflow-x: auto; }
.md :deep(th),
.md :deep(td) { border: 1px solid #334155; padding: 4px 10px; text-align: left; }
.md :deep(th) { background: #0f172a; color: #93c5fd; font-weight: 600; }
.md :deep(hr) { border: none; border-top: 1px solid #334155; margin: 14px 0; }
.md :deep(img) { max-width: 100%; border-radius: 8px; }
</style>
