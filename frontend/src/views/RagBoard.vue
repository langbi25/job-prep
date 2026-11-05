<template>
  <div class="page">
    <div class="head">
      <div>
        <h2>RAG 看板</h2>
        <div class="sub">
          检索链路：FTS5(2-gram OR) + 向量(bge-small-zh) → RRF 融合 →（full 档）改写 + LLM 重排
          <el-tag v-if="data?.eval" :type="data.eval.stale ? 'danger' : 'success'" size="small" style="margin-left:8px">
            评测 {{ data.eval.age_hours }}h 前 · {{ data.eval.file }}
          </el-tag>
          <el-tag v-else type="warning" size="small" style="margin-left:8px">暂无评测结果</el-tag>
        </div>
      </div>
      <div class="ops">
        <el-button :loading="running" @click="rerun(false)">重新评测（零 LLM）</el-button>
        <el-button :loading="running" type="primary" @click="rerun(true)">含 LLM 改写+重排（慢）</el-button>
      </div>
    </div>

    <!-- 知识库构成：回答"库里到底是什么" -->
    <div class="stats row">
      <el-card shadow="never" class="stat" v-for="s in statCards" :key="s.label">
        <div class="num">{{ s.value }}</div>
        <div class="label">{{ s.label }}</div>
        <div class="hint">{{ s.hint }}</div>
      </el-card>
    </div>

    <!-- 指标 + 分桶 -->
    <div class="split row" v-if="data?.eval">
      <el-card shadow="never">
        <template #header>检索指标 vs 门禁阈值</template>
        <div class="metric" v-for="m in metrics" :key="m.name">
          <span class="mname">{{ m.name }}</span>
          <span class="mval">{{ m.value }}</span>
          <span class="mth">阈值 {{ m.th }}</span>
          <el-tag v-if="m.pass !== null" size="small" :type="m.pass ? 'success' : 'danger'">{{ m.pass ? '达标' : '未达' }}</el-tag>
        </div>
        <div class="note">hit@k：前 k 条里有没有标准卡；MRR：标准卡排名的倒数均值；gap_false_confident：库里没卡的题被标成高置信（Agent 会以为有卡）</div>
      </el-card>
      <el-card shadow="never">
        <template #header>分桶 hit@5：fts vs hybrid（按查询类型）</template>
        <div ref="barEl" class="chart chart-sm"></div>
      </el-card>
    </div>

    <!-- 知识图谱 -->
    <el-card shadow="never" class="row">
      <template #header>
        <div class="ghead">
          <span>知识库结构图（这不是知识图谱——边只有「域→主题→卡」「卡↔卡」「卡→题」三种真实关系；灰点是无卡题，即覆盖缺口）</span>
          <div>
            <el-checkbox v-model="showOrphan" size="small">显示无卡题（{{ orphanCount }} 个，覆盖缺口）</el-checkbox>
            <el-checkbox v-model="showArchived" size="small">显示归档卡</el-checkbox>
          </div>
        </div>
      </template>
      <div ref="graphEl" class="chart chart-lg"></div>
    </el-card>

    <!-- 用例明细 -->
    <el-card shadow="never" class="row" v-if="data?.eval?.search_tool">
      <template #header>
        <div class="ghead">
          <span>search_kb 用例明细（{{ data.eval.search_tool.cases }} 有卡 + {{ data.eval.search_tool.gap_cases }} 无卡）</span>
          <el-checkbox v-model="onlyMiss" size="small">只看未命中</el-checkbox>
        </div>
      </template>
      <el-table :data="caseRows" size="small" max-height="360">
        <el-table-column prop="q" label="查询" min-width="180" />
        <el-table-column prop="kind" label="类型" width="100" />
        <el-table-column label="命中" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.ok ? 'success' : 'danger'">{{ row.ok ? '✓' : '✗' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="confidence" label="置信度" width="90" />
        <el-table-column label="Top3" min-width="220">
          <template #default="{ row }">{{ (row.top3 || []).join(' · ') || row.note || row.confidence }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import api from '../api'

const data = ref(null)
const running = ref(false)
const showOrphan = ref(false)
const showArchived = ref(false)
const onlyMiss = ref(false)
const barEl = ref(null)
const graphEl = ref(null)
let barChart = null
let graphChart = null

const statCards = computed(() => {
  const kb = data.value?.kb || {}
  return [
    { label: '活跃知识卡', value: kb.active_cards ?? '–', hint: '域：' + Object.keys(kb.domains || {}).length },
    { label: '归档卡', value: kb.archived_cards ?? '–', hint: '去重/合并产物' },
    { label: '题目', value: kb.questions ?? '–', hint: '库里主体是"题"，卡是"学到的结论"' },
    { label: '题目→卡关联', value: kb.questions_linked ?? '–',
      hint: `关联率 ${pct(kb.coverage)} · ${Object.entries(kb.link_methods || {}).map(([k, v]) => `${k} ${v}`).join(' / ') || 'legacy'}` },
    { label: '卡↔卡边', value: kb.card_card_edges ?? '–', hint: 'related_json，尚稀疏' },
    { label: '图谱节点', value: data.value?.graph ? data.value.graph.nodes.length : '–', hint: '域/主题/卡/题四层' },
  ]
})

const orphanCount = computed(() =>
  (data.value?.graph?.nodes || []).filter(n => n.node_type === 'orphan_question').length)

const metrics = computed(() => {
  const ev = data.value?.eval || {}
  const th = data.value?.thresholds || {}
  const hyb = ev.retrieval?.hybrid || {}
  const fts = ev.retrieval?.fts || {}
  const st = ev.search_tool || {}
  const out = []
  if (hyb.hit5 !== undefined) out.push({ name: 'hybrid hit@5', value: hyb.hit5, th: th.retrieval_hit5 ?? '–', pass: th.retrieval_hit5 ? hyb.hit5 >= th.retrieval_hit5 : null })
  if (fts.hit5 !== undefined) out.push({ name: 'fts hit@5', value: fts.hit5, th: th.retrieval_hit5 ?? '–', pass: th.retrieval_hit5 ? fts.hit5 >= th.retrieval_hit5 : null })
  if (hyb.mrr !== undefined) out.push({ name: 'hybrid MRR', value: hyb.mrr, th: '–', pass: null })
  if (st.hit3 !== undefined) out.push({ name: 'search_kb hit@3', value: st.hit3, th: th.search_tool_hit3 ?? '–', pass: th.search_tool_hit3 ? st.hit3 >= th.search_tool_hit3 : null })
  if (st.top1 !== undefined) out.push({ name: 'search_kb top1', value: st.top1, th: '–', pass: null })
  if (st.gap_false_confident !== undefined) out.push({ name: 'gap 误标高置信', value: st.gap_false_confident, th: th.search_tool_gap_false_confident ?? '–', pass: st.gap_false_confident <= (th.search_tool_gap_false_confident ?? 0) })
  return out
})

const caseRows = computed(() => {  const st = data.value?.eval?.search_tool
  if (!st) return []
  const rows = [
    ...((st.details || []).map(d => ({ ...d, miss: !d.ok }))),
    ...((st.gap_details || []).map(d => ({ ...d, kind: 'gap', miss: !!d.false_confident, ok: !d.false_confident }))),
  ]
  return onlyMiss.value ? rows.filter(r => r.miss) : rows
})

function pct(v) { return v === null || v === undefined ? '–' : Math.round(v * 100) + '%' }

async function load() {
  data.value = await api.get('/api/rag/board')   // 拦截器已解包 resp.data
  await nextTick()
  renderBar()
  renderGraph()
}

async function rerun(withLlm) {
  running.value = true
  try {
    data.value = await api.post('/api/rag/eval', { with_llm: withLlm }, { timeout: 600000 })
    await nextTick()
    renderBar()
    renderGraph()
  } finally {
    running.value = false
  }
}

function renderBar() {
  if (!barEl.value || !data.value?.eval?.retrieval) return
  const r = data.value.eval.retrieval
  const kinds = [...new Set(Object.keys(r.fts?.by_kind || {}).concat(Object.keys(r.hybrid?.by_kind || {})))]
  barChart = barChart || echarts.init(barEl.value)
  barChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { left: 40, right: 10, top: 30, bottom: 24 },
    xAxis: { type: 'category', data: kinds,
      axisLabel: { interval: 0, fontSize: 10, rotate: kinds.length > 3 ? 15 : 0 } },
    yAxis: { type: 'value', max: 1 },
    series: ['fts', 'hybrid'].filter(m => r[m]).map((m, i) => ({
      name: m, type: 'bar', data: kinds.map(k => r[m].by_kind?.[k]?.hit5 ?? 0),
      itemStyle: { color: i ? '#409eff' : '#95d475' },
      label: { show: true, position: 'top', fontSize: 10, formatter: p => p.value || '' },
    })),
  }, true)
}

function renderGraph() {
  const g = data.value?.graph
  if (!g || !graphEl.value) return
  const catIdx = { domain: 0, topic: 1, card: 2, archived_card: 3, question: 4, orphan_question: 5 }
  const keep = n =>
    (n.node_type !== 'orphan_question' || showOrphan.value) &&
    (n.node_type !== 'archived_card' || showArchived.value)
  const nodes = g.nodes.filter(keep)
  const ids = new Set(nodes.map(n => n.id))
  const links = g.edges.filter(e => ids.has(e.source) && ids.has(e.target))
  const size = { domain: 42, topic: 22, card: 13, archived_card: 9, question: 8, orphan_question: 7 }
  graphChart = graphChart || echarts.init(graphEl.value)
  graphChart.setOption({
    tooltip: { formatter: p => p.dataType === 'node' ? `${p.data.name}<br/><span style="opacity:.6">${p.data.id}</span>` : '' },
    legend: { data: g.categories, top: 0 },
    series: [{
      type: 'graph', layout: 'force', roam: true, draggable: true,
      top: 38, bottom: 8,   // 给上面的图例留位置，否则节点会钻到图例底下
      categories: g.categories.map((name, i) => ({ name })),
      color: ['#5470c6', '#91cc75', '#9254de', '#c0c4cc', '#e6a23c', '#f56c6c'],
      data: nodes.map(n => ({
        ...n, category: catIdx[n.node_type], symbolSize: size[n.node_type] || 10,
        // 域是大圆，标签放外面会被旁边节点盖住；主题/卡放右侧
        label: n.node_type === 'domain'
          ? { position: 'inside', color: '#fff', fontWeight: 'bold', fontSize: 10 }
          : undefined,
      })),
      links,
      // layoutAnimation:false —— 一次性算完布局。开动画时 128 个节点会被甩出画布，
      // 截图/首屏看到的点数是随机的一部分（我第一版就踩了，看起来像"节点丢了"）。
      force: { repulsion: 120, edgeLength: [30, 80], gravity: 0.12, layoutAnimation: false },
      label: { show: true, position: 'right', fontSize: 9,
        formatter: p => ['domain', 'topic'].includes(p.data.node_type) ? p.data.name : '' },
      labelLayout: { hideOverlap: true },   // 21 个主题名会重叠，交给 echarts 抽稀
      emphasis: { focus: 'adjacency', label: { show: true, fontSize: 11 } },
      lineStyle: { color: 'source', opacity: 0.35, curveness: 0.1 },
    }],
  }, true)
}

function onResize() { barChart?.resize(); graphChart?.resize() }

watch([showOrphan, showArchived], renderGraph)   // 勾选后必须重画，否则开关是摆设

onMounted(async () => {
  await load()
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  barChart?.dispose(); graphChart?.dispose()
})
</script>

<style scoped>
.page { padding: 4px 2px; }
.head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; gap: 12px; flex-wrap: wrap; }
.head h2 { margin: 0 0 4px; font-size: 18px; }
.sub { font-size: 12px; color: var(--el-text-color-secondary); }
.row { margin-bottom: 10px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(112px, 1fr)); gap: 10px; }
.split { display: grid; grid-template-columns: 1fr 1.35fr; gap: 10px; align-items: stretch; }
.stat { text-align: center; }
.stat .num { font-size: 24px; font-weight: 600; }
.stat .label { font-size: 13px; margin-top: 2px; }
.stat .hint { font-size: 11px; color: var(--el-text-color-secondary); margin-top: 2px; }
.metric { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px dashed var(--el-border-color-lighter); font-size: 13px; }
.metric:last-of-type { border-bottom: none; }
.mname { width: 150px; }
.mval { font-size: 18px; font-weight: 600; width: 70px; }
.mth { color: var(--el-text-color-secondary); flex: 1; }
.note { font-size: 11px; color: var(--el-text-color-secondary); margin-top: 8px; line-height: 1.6; }
.chart { width: 100%; }
.chart-sm { height: 240px; }
.chart-lg { height: 520px; }
.ghead { display: flex; justify-content: space-between; align-items: center; gap: 10px; font-size: 13px; flex-wrap: wrap; }
@media (max-width: 980px) {
  .split { grid-template-columns: 1fr; }
  .stat .num { font-size: 20px; }
  .chart-lg { height: 440px; }
  .mname { width: 120px; }
}
</style>
