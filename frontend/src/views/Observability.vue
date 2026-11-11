<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>可观测中心 · 每一次请求都看得见</h2>
        <p class="sub">
          数据来自 Phoenix（OpenTelemetry trace，OpenInference 语义约定）。中文翻译视图，深度排查可去
          <a :href="overview?.phoenix_url || 'http://127.0.0.1:6006'" target="_blank">Phoenix 原生界面</a>
        </p>
      </div>
      <div style="display:flex; align-items:center; gap:12px">
        <el-radio-group v-model="view" size="small">
          <el-radio-button value="traces">链路</el-radio-button>
          <el-radio-button value="sessions">会话 / 轮次</el-radio-button>
        </el-radio-group>
        <el-radio-group v-if="view === 'traces'" v-model="scope" size="small" @change="loadAll">
          <el-radio-button value="agent">Agent 链路</el-radio-button>
          <el-radio-button value="all">全部请求</el-radio-button>
        </el-radio-group>
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
            <div class="stat-label">{{ scope === 'agent' ? 'Agent 链路（对话/任务）' : '全部请求' }}
              <template v-if="overview">· Agent {{ overview.trace_count_agent }} / 全部 {{ overview.trace_count_all }}</template>
            </div></div></el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never"><div class="stat"><div class="stat-num">{{ overview?.session_count ?? '–' }}</div>
            <div class="stat-label">会话数（多轮对话按 session.id 聚合）</div></div></el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never"><div class="stat"><div class="stat-num">{{ fmtTokens(overview?.tokens_total) }}</div>
            <div class="stat-label">Token 总用量（输入 {{ fmtTokens(overview?.tokens_prompt) }} / 输出 {{ fmtTokens(overview?.tokens_completion) }}）</div></div></el-card>
        </el-col>
        <el-col :span="12">
          <el-card shadow="never"><div class="stat"><div class="stat-num">
            P50 {{ fmtMs(overview?.latency_p50_ms) }} · P95 {{ fmtMs(overview?.latency_p95_ms) }}</div>
            <div class="stat-label">{{ scope === 'agent' ? 'Agent 链路延迟分位' : '全部请求延迟分位' }}（P95 = 慢请求线）</div></div></el-card>
        </el-col>
      </el-row>

      <!-- ════ 视图一：链路 ════ -->
      <el-row v-if="view === 'traces'" :gutter="12">
        <el-col :span="9">
          <el-card shadow="never">
            <template #header>
              <div class="card-head"><span>最近{{ scope === 'agent' ? ' Agent 链路' : '请求' }}（{{ traces.length }}）</span>
                <span class="err-count" v-if="errCount">🔴 异常 {{ errCount }}</span></div>
            </template>
            <el-empty v-if="!traces.length"
                      :description="scope === 'agent' ? '还没有 Agent 链路——去 Agent 页聊一句就有了' : '还没有 trace 数据'"
                      :image-size="60" />
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

        <el-col :span="15">
          <el-card shadow="never">
            <template #header>
              <div class="card-head">
                <span>Span 调用关系（瀑布图，点行展开事件与输入输出）</span>
                <span v-if="current" class="hint">共 {{ current.num_spans }} 步 · 总时长 {{ fmtMs(current.latency_ms) }}</span>
              </div>
            </template>
            <el-empty v-if="!current" description="点左侧任意一条链路，看内部每个 span 的嵌套关系与耗时" :image-size="60" />
            <template v-else>
              <div class="legend">
                <span v-for="(v, k) in KIND_COLORS" :key="k" class="legend-item">
                  <i class="dot" :style="{ background: v }" />{{ k }}
                </span>
              </div>
              <div class="waterfall">
                <div v-for="n in flatTree" :key="n.span_id" class="wf-row" :class="{ err: n.error }"
                     @click="toggleExpand(n)">
                  <div class="wf-label" :style="{ paddingLeft: (n._depth * 16 + 4) + 'px' }">
                    <span class="wf-arrow" v-if="n.children.length">{{ n._open ? '▾' : '▸' }}</span>
                    <el-tag size="small" :type="kindTag(n.kind)" effect="plain">{{ n.kind_zh }}</el-tag>
                    <span class="wf-name" :title="n.name">{{ n.name_zh }}</span>
                    <span class="wf-tokens" v-if="n.tokens">
                      {{ n.prompt_tokens ? `${fmtTokens(n.prompt_tokens)}→${fmtTokens(n.completion_tokens)}` : fmtTokens(n.tokens) }} tok
                    </span>
                  </div>
                  <div class="wf-track">
                    <div class="wf-bar" :style="barStyle(n)" :title="`${n.name} · ${fmtMs(n.latency_ms)}`" />
                  </div>
                  <div class="wf-ms" :class="{ slow: n.latency_ms > 3000 }">{{ fmtMs(n.latency_ms) }}</div>
                </div>
              </div>

              <!-- 选中 span 的详情：事件时间线 + 属性 -->
              <div v-if="expanded" class="attrs">
                <div class="attrs-title">🔍 {{ expanded.name_zh }}（{{ expanded.name }}）</div>
                <div v-if="expanded.error" class="attrs-err">❌ {{ expanded.error_message }}</div>

                <template v-if="expanded.events.length">
                  <div class="events-title">⏱ 关键事件</div>
                  <div v-for="(ev, i) in expanded.events" :key="i" class="event-line">
                    <span class="event-name">{{ eventLabel(ev.name) }}</span>
                    <span class="event-offset" v-if="ev.offset_ms != null">+{{ fmtMs(ev.offset_ms) }}</span>
                    <span v-for="(v, k) in ev.attrs" :key="k" class="event-attr">{{ attrKey(k) }}: {{ v }}</span>
                  </div>
                </template>

                <div v-for="(v, k) in expanded.attrs" :key="k" class="attr-line">
                  <span class="attr-key">{{ ATTR_LABEL[k] || k }}</span>
                  <span class="attr-val">{{ v }}</span>
                </div>
                <div v-if="!Object.keys(expanded.attrs).length && !expanded.events.length && !expanded.error" class="hint">
                  该步骤没有额外信息（纯路由/序列化属正常）
                </div>
              </div>
              <div v-else class="hint" style="margin-top: 8px">
                点击任意一行：展开/折叠子 span，查看该步骤的关键事件（首 token、工具动作、token 用量…）与输入输出摘要
              </div>
            </template>
          </el-card>
        </el-col>
      </el-row>

      <!-- ════ 视图二：会话 / 轮次 ════ -->
      <el-row v-else :gutter="12">
        <el-col :span="9">
          <el-card shadow="never">
            <template #header><span>会话（{{ sessions.length }}）· 一场会话 = 连续多轮对话</span></template>
            <el-empty v-if="!sessions.length" description="还没有会话数据——去 Agent 页聊一句就有了（打点已带 session.id）" :image-size="60" />
            <div v-for="s in sessions" :key="s.session_id" class="session-row"
                 :class="{ active: s.session_id === currentSession?.session_id }" @click="openSession(s)">
              <div class="trace-line1">
                <span class="session-name">{{ s.session_id }}</span>
                <el-tag size="small" :type="s.errors ? 'danger' : 'success'" effect="plain">{{ s.turns }} 轮</el-tag>
              </div>
              <div class="trace-line2">{{ fmtTime(s.start) }}<span v-if="s.first_input"> · "{{ s.first_input }}"</span></div>
            </div>
          </el-card>
        </el-col>
        <el-col :span="15">
          <el-card shadow="never">
            <template #header>
              <div class="card-head">
                <span>轮次（客观侧：一轮 = 用户一句话触发的完整 Agent 循环）</span>
                <span v-if="currentSession" class="hint">{{ currentSession.session_id }}</span>
              </div>
            </template>
            <el-empty v-if="!currentSession" description="点左侧一场会话，看每轮的耗时与步骤" :image-size="60" />
            <el-table v-else :data="turns" size="small" @row-click="(row) => gotoTurn(row)"
                      style="cursor: pointer">
              <el-table-column prop="turn" label="轮次" width="70" />
              <el-table-column label="入口链路" min-width="160">
                <template #default="{ row }">{{ row.name_zh || row.name }}</template>
              </el-table-column>
              <el-table-column label="耗时" width="100">
                <template #default="{ row }">
                  <span :class="{ 'slow-text': row.latency_ms > 3000 }">{{ fmtMs(row.latency_ms) }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="num_spans" label="Span 数" width="80" />
              <el-table-column label="时间" width="150">
                <template #default="{ row }">{{ fmtTime(row.start) }}</template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag v-if="row.errors" size="small" type="danger">{{ row.errors }} 处异常</el-tag>
                  <el-tag v-else size="small" type="success" effect="plain">正常</el-tag>
                </template>
              </el-table-column>
            </el-table>
            <div v-if="currentSession" class="hint" style="margin-top: 8px">
              点击任意一轮 → 跳到「链路」视图看这轮的完整 span 调用关系
            </div>
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
const scope = ref('agent')
const view = ref('traces')
const expanded = ref(null)
const sessions = ref([])
const currentSession = ref(null)
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
const EVENT_LABEL = {
  first_token: '⚡ 首个 token',
  'gen_ai.usage': '🔢 token 用量',
  'tool.action': '✏️ 工具动作',
  stream_fallback: '⚠️ 流式降级',
  exception: '❌ 异常',
}
const EVENT_ATTR_LABEL = {
  ttft_ms: 'TTFT', prompt_tokens: '输入', completion_tokens: '输出', reason: '原因',
}
const kindTag = (k) => ({ AGENT: 'primary', LLM: 'warning', TOOL: 'success', RETRIEVER: 'danger', CHAIN: 'info' }[k] || 'info')
const eventLabel = (n) => EVENT_LABEL[n] || n
const attrKey = (k) => EVENT_ATTR_LABEL[k] || k

const errCount = computed(() => traces.value.filter((t) => t.error).length)
const turns = computed(() => currentSession.value?.turns || [])

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
  const r = await api.get('/api/obs/overview', { params: { scope: scope.value } })
  overview.value = r
  unavailable.value = r.available ? '' : (r.reason || 'Phoenix 不可用')
}

async function loadTraces() {
  const r = await api.get('/api/obs/traces', { params: { limit: 50, scope: scope.value } })
  if (r.available === false) { unavailable.value = r.reason || 'Phoenix 不可用'; return }
  traces.value = r.items
}

async function loadSessions() {
  const r = await api.get('/api/obs/sessions', { params: { limit: 30 } })
  if (r.available === false) { unavailable.value = r.reason || 'Phoenix 不可用'; return }
  sessions.value = r.items
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

async function openSession(s) {
  try {
    const r = await api.get(`/api/obs/sessions/${s.session_id}`)
    if (r.available === false) return
    currentSession.value = r
  } catch { /* 拦截器已提示 */ }
}

async function gotoTurn(row) {
  await openTrace({ trace_id: row.trace_id })
  view.value = 'traces'
}

async function loadAll() {
  try {
    await loadOverview()
    if (unavailable.value) return
    await loadTraces()
    if (view.value === 'sessions') await loadSessions()
  } catch { /* 拦截器已提示 */ }
}

let timer = null
onMounted(() => {
  loadAll()
  timer = setInterval(() => {
    if (!auto.value) return
    loadOverview(); loadTraces()
    if (view.value === 'sessions') loadSessions()
  }, 5000)
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

.trace-row, .session-row { padding: 8px 6px; border-bottom: 1px dashed #ebeef5; cursor: pointer; border-radius: 4px; }
.trace-row:hover, .session-row:hover { background: #f5f7fa; }
.trace-row.active, .session-row.active { background: #ecf5ff; }
.trace-line1 { display: flex; align-items: center; gap: 6px; }
.trace-name { font-size: 13px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-name { font-size: 12px; font-family: monospace; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.trace-lat { font-size: 12px; color: #606266; font-family: monospace; }
.trace-lat.slow, .slow-text { color: #e6a23c; font-weight: 600; }
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
.wf-tokens { font-size: 10px; color: #909399; white-space: nowrap; }
.wf-track { flex: 1; height: 12px; position: relative; background: #f5f7fa; border-radius: 3px; overflow: hidden; }
.wf-bar { position: absolute; top: 2px; height: 8px; border-radius: 3px; min-width: 3px; }
.wf-ms { width: 64px; text-align: right; font-size: 11px; color: #606266; font-family: monospace; padding-right: 4px; }
.wf-ms.slow { color: #e6a23c; font-weight: 600; }

.attrs { margin-top: 12px; background: #fafafa; border: 1px solid #ebeef5; border-radius: 6px; padding: 10px 12px; }
.attrs-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.attrs-err { color: #f56c6c; font-size: 12px; margin-bottom: 6px; }
.events-title { font-size: 12px; font-weight: 600; color: #606266; margin: 6px 0 4px; }
.event-line { display: flex; align-items: center; gap: 8px; font-size: 12px; padding: 3px 6px;
  background: #f0f6ff; border-radius: 4px; margin-bottom: 3px; flex-wrap: wrap; }
.event-name { font-weight: 600; color: #409eff; }
.event-offset { color: #909399; font-family: monospace; font-size: 11px; }
.event-attr { color: #606266; font-size: 11px; }
.attr-line { display: flex; gap: 8px; font-size: 12px; padding: 2px 0; }
.attr-key { color: #909399; min-width: 110px; }
.attr-val { color: #303133; word-break: break-all; }
</style>
