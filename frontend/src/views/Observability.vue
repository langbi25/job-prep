<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>可观测中心 · 每一次请求都看得见</h2>
        <p class="sub">
          数据来自 Phoenix（OpenTelemetry trace）。这里是中文翻译视图，深度排查可去
          <a :href="overview?.phoenix_url || 'http://127.0.0.1:6006'" target="_blank">Phoenix 原生界面</a>
        </p>
      </div>
      <div style="display:flex; align-items:center; gap:8px">
        <el-switch v-model="auto" active-text="自动刷新 5s" />
        <el-button :icon="Refresh" circle @click="loadAll" />
      </div>
    </div>

    <el-alert v-if="unavailable" type="warning" :closable="false" style="margin-bottom: 12px"
              :title="'Phoenix 未连接（面板降级）'" :description="unavailable">
      <template #default>
        <div>启动方式：在项目目录执行 <code>phoenix serve</code>（端口 6006）。原因：{{ unavailable }}</div>
      </template>
    </el-alert>

    <template v-if="!unavailable">
      <!-- 概览统计 -->
      <el-row :gutter="12" style="margin-bottom: 12px">
        <el-col :span="6">
          <el-card shadow="never"><div class="stat"><div class="stat-num">{{ overview?.trace_count ?? '–' }}</div>
            <div class="stat-label">累计请求 / 对话</div></div></el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never"><div class="stat"><div class="stat-num">{{ fmtTokens(overview?.tokens_total) }}</div>
            <div class="stat-label">Token 总用量（输入 {{ fmtTokens(overview?.tokens_prompt) }} / 输出 {{ fmtTokens(overview?.tokens_completion) }}）</div></div></el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never"><div class="stat"><div class="stat-num">{{ fmtMs(overview?.latency_p50_ms) }}</div>
            <div class="stat-label">P50 延迟（中位数）</div></div></el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never"><div class="stat"><div class="stat-num">{{ fmtMs(overview?.latency_p95_ms) }}</div>
            <div class="stat-label">P95 延迟（慢请求线）</div></div></el-card>
        </el-col>
      </el-row>

      <el-row :gutter="12">
        <!-- 左：trace 列表 -->
        <el-col :span="9">
          <el-card shadow="never">
            <template #header>
              <div class="card-head"><span>最近请求（{{ traces.length }}）</span>
                <span class="err-count" v-if="errCount">🔴 异常 {{ errCount }}</span></div>
            </template>
            <el-empty v-if="!traces.length" description="还没有 trace 数据——去 Agent 页聊一句就有了" :image-size="60" />
            <div v-for="t in traces" :key="t.trace_id" class="trace-row"
                 :class="{ active: t.trace_id === current?.trace_id }" @click="openTrace(t)">
              <div class="trace-line1">
                <el-tag size="small" :type="kindTag(t.kind)" effect="plain">{{ t.kind_zh }}</el-tag>
                <span class="trace-name">{{ t.name_zh }}</span>
                <el-tag v-if="t.error" size="small" type="danger">异常</el-tag>
                <span class="trace-lat" :class="{ slow: t.latency_ms > 3000 }">{{ fmtMs(t.latency_ms) }}</span>
              </div>
              <div class="trace-line2">
                {{ fmtTime(t.start) }} · {{ t.span_count }} 个步骤
                <span v-if="t.tokens"> · {{ fmtTokens(t.tokens) }} tokens</span>
              </div>
            </div>
          </el-card>
        </el-col>

        <!-- 右：span 瀑布 -->
        <el-col :span="15">
          <el-card shadow="never">
            <template #header>
              <div class="card-head">
                <span>调用链路详情</span>
                <span v-if="current" class="hint">共 {{ current.num_spans }} 步 · 总时长 {{ fmtMs(current.latency_ms) }}</span>
              </div>
            </template>
            <el-empty v-if="!current" description="点左侧任意一条请求，看它内部每一步耗时" :image-size="60" />
            <template v-else>
              <div class="legend">
                <span v-for="(v, k) in KIND_COLORS" :key="k" class="legend-item">
                  <i class="dot" :style="{ background: v }" />{{ k }}
                </span>
              </div>
              <div class="waterfall" :style="{ '--total': current.total_latency_ms || current.latency_ms }">
                <div v-for="n in flatTree" :key="n.span_id" class="wf-row" :class="{ err: n.error }"
                     @click="toggleExpand(n)">
                  <div class="wf-label" :style="{ paddingLeft: (n._depth * 16 + 4) + 'px' }">
                    <span class="wf-arrow" v-if="n.children.length">{{ n._open ? '▾' : '▸' }}</span>
                    <el-tag size="small" :type="kindTag(n.kind)" effect="plain">{{ n.kind_zh }}</el-tag>
                    <span class="wf-name" :title="n.name">{{ n.name_zh }}</span>
                    <span class="wf-tokens" v-if="n.tokens">{{ fmtTokens(n.tokens) }} tok</span>
                  </div>
                  <div class="wf-track">
                    <div class="wf-bar" :style="barStyle(n)" :title="`${n.name} · ${fmtMs(n.latency_ms)}`" />
                  </div>
                  <div class="wf-ms" :class="{ slow: n.latency_ms > 3000 }">{{ fmtMs(n.latency_ms) }}</div>
                </div>
              </div>

              <!-- 选中 span 的属性 -->
              <div v-if="expanded" class="attrs">
                <div class="attrs-title">🔍 {{ expanded.name_zh }}（{{ expanded.name }}）</div>
                <div v-if="expanded.error" class="attrs-err">❌ {{ expanded.error_message }}</div>
                <div v-for="(v, k) in expanded.attrs" :key="k" class="attr-line">
                  <span class="attr-key">{{ ATTR_LABEL[k] || k }}</span>
                  <span class="attr-val">{{ v }}</span>
                </div>
                <div v-if="!Object.keys(expanded.attrs).length && !expanded.error" class="hint">
                  该步骤没有额外信息（纯路由/序列化属正常）
                </div>
              </div>
              <div v-else class="hint" style="margin-top: 8px">点击任意一行展开该步骤的输入输出摘要</div>
            </template>
          </el-card>
        </el-col>
      </el-row>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'

const overview = ref(null)
const traces = ref([])
const current = ref(null)
const unavailable = ref('')
const auto = ref(true)
const expanded = ref(null)
const collapsed = ref(new Set())

const KIND_COLORS = {
  智能体: '#409eff', 模型调用: '#e6a23c', 工具调用: '#67c23a',
  知识检索: '#9a6ae8', 链路编排: '#8d9dad', 服务: '#c8cdd4',
}
const ATTR_LABEL = {
  model: '模型', scene: '场景', temperature: '温度', tool_name: '工具名',
  retriever: '检索', chain_tokens: '上下文', llm_in: '输入（末条消息）', llm_out: '输出（末条消息）',
  input: '输入', output: '输出',
}
const kindTag = (k) => ({ AGENT: 'primary', LLM: 'warning', TOOL: 'success', RETRIEVER: 'danger', CHAIN: 'info' }[k] || 'info')

const errCount = computed(() => traces.value.filter((t) => t.error).length)

// 树 → 拍平（带深度；折叠的子树不展开）
const flatTree = computed(() => {
  const out = []
  const walk = (nodes, depth) => {
    for (const n of nodes) {
      n._depth = depth
      out.push(n)
      if (n.children.length && !collapsed.value.has(n.span_id)) walk(n.children, depth + 1)
    }
  }
  walk(current.value?.tree || [], 0)
  return out
})

function barStyle(n) {
  const total = Math.max(current.value?.total_latency_ms || 1, 0.01)
  const left = Math.min(100, (n.offset_ms / total) * 100)
  const width = Math.max(0.6, Math.min(100 - left, (n.latency_ms / total) * 100))
  return {
    left: left + '%',
    width: width + '%',
    background: n.error ? '#f56c6c' : (KIND_COLORS[n.kind_zh] || '#c8cdd4'),
  }
}

const fmtMs = (ms) => (ms == null ? '–' : ms >= 1000 ? (ms / 1000).toFixed(2) + ' s' : Math.round(ms) + ' ms')
const fmtTokens = (n) => (n == null ? '–' : n >= 10000 ? (n / 10000).toFixed(1) + ' 万' : String(n))
const fmtTime = (iso) => {
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch { return iso }
}

function toggleExpand(n) {
  if (n.children.length) {
    const s = new Set(collapsed.value)
    s.has(n.span_id) ? s.delete(n.span_id) : s.add(n.span_id)
    collapsed.value = s
  }
  expanded.value = expanded.value?.span_id === n.span_id ? null : n
}

async function loadOverview() {
  const r = await api.get('/api/obs/overview')
  overview.value = r
  unavailable.value = r.available ? '' : (r.reason || 'Phoenix 不可用')
}

async function loadTraces() {
  const r = await api.get('/api/obs/traces', { params: { limit: 50 } })
  if (r.available === false) { unavailable.value = r.reason || 'Phoenix 不可用'; return }
  traces.value = r.items
}

async function openTrace(t) {
  try {
    const r = await api.get(`/api/obs/traces/${t.trace_id}`)
    if (r.available === false) return
    current.value = r
    expanded.value = null
    collapsed.value = new Set()
  } catch { /* 拦截器已提示 */ }
}

async function loadAll() {
  try { await loadOverview(); if (!unavailable.value) await loadTraces() } catch { /* 拦截器已提示 */ }
}

let timer = null
onMounted(() => {
  loadAll()
  timer = setInterval(() => { if (auto.value) { loadOverview(); loadTraces() } }, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.sub { color: #909399; font-size: 12px; margin: 4px 0 0; }
.sub a { color: #409eff; }
.hint { color: #909399; font-size: 12px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.stat { text-align: center; padding: 4px 0; }
.stat-num { font-size: 24px; font-weight: 700; color: #303133; }
.stat-label { color: #909399; font-size: 11px; margin-top: 2px; }
.err-count { font-size: 12px; color: #f56c6c; }

.trace-row { padding: 8px 6px; border-bottom: 1px dashed #ebeef5; cursor: pointer; border-radius: 4px; }
.trace-row:hover { background: #f5f7fa; }
.trace-row.active { background: #ecf5ff; }
.trace-line1 { display: flex; align-items: center; gap: 6px; }
.trace-name { font-size: 13px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.trace-lat { font-size: 12px; color: #606266; font-family: monospace; }
.trace-lat.slow { color: #e6a23c; font-weight: 600; }
.trace-line2 { color: #909399; font-size: 11px; margin-top: 2px; }

.legend { display: flex; gap: 14px; margin-bottom: 8px; }
.legend-item { font-size: 11px; color: #606266; display: inline-flex; align-items: center; gap: 4px; }
.legend-item .dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }

.waterfall { border-top: 1px solid #ebeef5; }
.wf-row { display: flex; align-items: center; height: 30px; border-bottom: 1px solid #f5f7fa; cursor: pointer; }
.wf-row:hover { background: #fafbfc; }
.wf-row.err { background: #fef0f0; }
.wf-label { width: 46%; display: flex; align-items: center; gap: 5px; overflow: hidden; }
.wf-arrow { color: #c0c4cc; font-size: 10px; width: 10px; }
.wf-name { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wf-tokens { font-size: 10px; color: #909399; }
.wf-track { flex: 1; height: 12px; position: relative; background: #f5f7fa; border-radius: 3px; overflow: hidden; }
.wf-bar { position: absolute; top: 2px; height: 8px; border-radius: 3px; min-width: 3px; }
.wf-ms { width: 64px; text-align: right; font-size: 11px; color: #606266; font-family: monospace; padding-right: 4px; }
.wf-ms.slow { color: #e6a23c; font-weight: 600; }

.attrs { margin-top: 12px; background: #fafafa; border: 1px solid #ebeef5; border-radius: 6px; padding: 10px 12px; }
.attrs-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.attrs-err { color: #f56c6c; font-size: 12px; margin-bottom: 6px; }
.attr-line { display: flex; gap: 8px; font-size: 12px; padding: 2px 0; }
.attr-key { color: #909399; min-width: 110px; }
.attr-val { color: #303133; word-break: break-all; }
</style>
