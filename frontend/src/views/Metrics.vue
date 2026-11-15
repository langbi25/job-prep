<template>
  <div class="page">
    <div class="page-header">
      <div class="head-text">
        <h2>指标 · 哪里慢、哪里贵、哪里在失败</h2>
        <p class="sub">
          数据来自本地三张台账（LLM 用量 / 工具调用 / 编排计划），不依赖 Phoenix 或 Langfuse。
          与「可观测中心」的分工：那边看单条链路的内部结构，这边看整体趋势——
          <b>哪个场景在涨、哪个工具在挂、编排是一次跑成还是要人接手</b>。
        </p>
      </div>
      <div class="head-actions">
        <el-select v-model="days" size="small" style="width: 108px" @change="load">
          <el-option :value="1" label="近 1 天" />
          <el-option :value="7" label="近 7 天" />
          <el-option :value="14" label="近 14 天" />
        </el-select>
        <el-button :icon="Refresh" circle :loading="loading" @click="load" />
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="warning" :closable="false" style="margin-bottom: 14px" />

    <template v-if="data">
      <!-- 四个数字：先给结论，再给明细 -->
      <el-row :gutter="12" style="margin-bottom: 12px">
        <el-col :span="6">
          <el-card shadow="never">
            <div class="stat">
              <div class="stat-num">¥{{ num(b.today_cost_cny) }}</div>
              <div class="stat-label">
                今日成本 · 日预算 ¥{{ num(b.budget_cny) }}（{{ pct(b.budget_ratio) }}）
              </div>
              <div class="bar"><i :style="{ width: barW(b.budget_ratio), background: ratioColor(b.budget_ratio) }" /></div>
            </div>
          </el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never">
            <div class="stat">
              <div class="stat-num">{{ llm.calls }}</div>
              <div class="stat-label">
                LLM 调用 · 失败 {{ llm.errors }}（{{ pct(llm.error_rate) }}）· 重试 {{ llm.retried }}
              </div>
            </div>
          </el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never">
            <div class="stat">
              <div class="stat-num">{{ ms(llm.p50_ms) }} / {{ ms(llm.p95_ms) }}</div>
              <div class="stat-label">LLM 延迟 P50 / P95（失败行不计入分位）</div>
            </div>
          </el-card>
        </el-col>
        <el-col :span="6">
          <el-card shadow="never">
            <div class="stat">
              <div class="stat-num">{{ pct(o.success_rate) }}</div>
              <div class="stat-label">
                编排一次跑成 · {{ o.plans }} 条中 {{ o.by_status.done || 0 }} 条（软停 {{ o.by_status.capped || 0 }} capped / {{ o.by_status.timeout || 0 }} timeout）
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 成本趋势：一根柱子一天，超预算的日子标红 -->
      <el-card shadow="never" style="margin-bottom: 12px">
        <template #header><div class="card-head"><span>按天成本</span>
          <span class="hint">近 {{ data.window_days }} 天 · 合计 ¥{{ num(totalCost) }}</span></div></template>
        <el-empty v-if="!llm.by_day.length" description="这个窗口还没有调用" :image-size="50" />
        <div v-else class="days">
          <div v-for="d in llm.by_day" :key="d.day" class="day-col">
            <div class="day-bar-wrap">
              <div class="day-bar" :class="{ over: d.cost_cny > b.budget_cny }"
                   :style="{ height: dayH(d.cost_cny) }" :title="`${d.day} ¥${num(d.cost_cny)} · ${d.calls} 次`" />
            </div>
            <div class="day-label">{{ d.day.slice(5) }}</div>
            <div class="day-cost">¥{{ num(d.cost_cny) }}</div>
          </div>
        </div>
      </el-card>

      <el-row :gutter="12">
        <el-col :span="12">
          <el-card shadow="never">
            <template #header><div class="card-head"><span>按场景</span>
              <span class="hint">成本降序 —— 第一眼就是花钱的地方</span></div></template>
            <el-table :data="llm.by_scene" size="small" :show-header="true">
              <el-table-column prop="scene" label="场景" min-width="130" show-overflow-tooltip />
              <el-table-column prop="calls" label="调用" width="64" align="right" />
              <el-table-column label="失败率" width="82" align="right">
                <template #default="{ row }">
                  <span :class="{ bad: row.error_rate >= 0.2 }">{{ pct(row.error_rate) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="P95" width="82" align="right">
                <template #default="{ row }">{{ ms(row.p95_ms) }}</template>
              </el-table-column>
              <el-table-column label="成本" width="92" align="right">
                <template #default="{ row }">¥{{ num(row.cost_cny) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>

        <el-col :span="12">
          <el-card shadow="never">
            <template #header><div class="card-head"><span>按工具</span>
              <span class="hint">调用次数降序 · 失败率高的排在上面一眼能看到</span></div></template>
            <el-table :data="tools.by_tool" size="small">
              <el-table-column prop="tool" label="工具" min-width="130" show-overflow-tooltip />
              <el-table-column prop="calls" label="调用" width="64" align="right" />
              <el-table-column label="失败率" width="82" align="right">
                <template #default="{ row }">
                  <span :class="{ bad: row.error_rate >= 0.2 }">{{ pct(row.error_rate) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="P95" width="82" align="right">
                <template #default="{ row }">{{ ms(row.p95_ms) }}</template>
              </el-table-column>
              <el-table-column label="缓存" width="64" align="right">
                <template #default="{ row }">{{ row.cached }}</template>
              </el-table-column>
            </el-table>
            <div v-if="tools.error_kinds.length" class="kinds">
              <span class="kinds-title">失败分类</span>
              <el-tag v-for="k in tools.error_kinds" :key="k.kind" size="small" type="warning" effect="plain">
                {{ kindZh(k.kind) }} × {{ k.n }}
              </el-tag>
              <span class="hint">参数错改 schema，执行错修实现</span>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <el-card shadow="never" style="margin-top: 12px">
        <template #header><div class="card-head"><span>编排</span>
          <span class="hint">
            {{ o.plans }} 条 · 平均 {{ o.avg_steps }} 步 / ¥{{ num(o.avg_cost_cny) }} / {{ ms(o.avg_duration_ms) }}
            · 硬失败率 {{ pct(o.hard_failure_rate) }}（capped/timeout 单列，不算失败）
          </span></div></template>
        <el-empty v-if="!o.plans" description="还没有编排记录" :image-size="50" />
        <el-row v-else :gutter="16">
          <el-col :span="8">
            <div class="mini-title">终态分布</div>
            <div v-for="(v, k) in o.by_status" :key="k" class="mini-row">
              <span class="mini-k">{{ statusZh(k) }}</span>
              <div class="mini-bar"><i :style="{ width: miniW(v, o.plans), background: statusColor(k) }" /></div>
              <span class="mini-n">{{ v }}</span>
            </div>
          </el-col>
          <el-col :span="8">
            <div class="mini-title">步骤状态（哪一步最容易挂）</div>
            <div v-for="s in o.step_status" :key="s.role + s.status" class="mini-row">
              <span class="mini-k">{{ roleZh(s.role) }} · {{ statusZh(s.status) }}</span>
              <span class="mini-n">{{ s.n }}</span>
            </div>
            <div class="hint" style="margin-top: 6px">重试过的步骤 {{ o.retried_steps }} 个</div>
          </el-col>
          <el-col :span="8">
            <div class="mini-title">步骤实际用的模型</div>
            <div v-for="m in o.step_models" :key="m.model" class="mini-row">
              <span class="mini-k mono">{{ m.model }}</span>
              <span class="mini-n">{{ m.n }} 步</span>
            </div>
            <div class="hint" style="margin-top: 6px">
              按角色路由：ORCH_ROLE_MODEL='{"analyst":"deepseek-reasoner"}'
            </div>
          </el-col>
        </el-row>
      </el-card>
      <!-- 意图分析：用户在拿 agent 干什么、哪类意图贵/容易失败 -->
      <el-card shadow="never" style="margin-top: 12px">
        <template #header><div class="card-head"><span>意图分析</span>
          <span class="hint">
            每条编排按目标意图聚合 · 早期未分类的归「未分类」单列，不混进「其他」
          </span></div></template>
        <el-empty v-if="!intents.items.length" description="还没有编排记录" :image-size="50" />
        <el-table v-else :data="intents.items" size="small">
          <el-table-column label="意图" width="110">
            <template #default="{ row }">
              <el-tag size="small" :effect="row.intent === 'unknown' ? 'info' : 'plain'">
                {{ row.label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="plans" label="条数" width="64" align="right" />
          <el-table-column label="成功率" width="80" align="right">
            <template #default="{ row }">{{ pct(row.success_rate) }}</template>
          </el-table-column>
          <el-table-column label="硬失败" width="80" align="right">
            <template #default="{ row }">
              <span :class="{ bad: row.hard_failure_rate >= 0.3 }">{{ pct(row.hard_failure_rate) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="软停" width="70" align="right">
            <template #default="{ row }">{{ pct(row.soft_stop_rate) }}</template>
          </el-table-column>
          <el-table-column label="平均费用" width="92" align="right">
            <template #default="{ row }">¥{{ num(row.avg_cost_cny) }}</template>
          </el-table-column>
          <el-table-column label="平均耗时" width="90" align="right">
            <template #default="{ row }">{{ ms(row.avg_duration_ms) }}</template>
          </el-table-column>
          <el-table-column label="平均步数" width="86" align="right">
            <template #default="{ row }">{{ row.avg_steps }}</template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- 子 agent 运行视图：每个角色单独一笔账 -->
      <el-card shadow="never" style="margin-top: 12px">
        <template #header><div class="card-head"><span>子 Agent 运行视图</span>
          <span class="hint">
            LLM/工具来自台账打标（scene=orchestrate:角色 · source=agent:角色），步骤来自 steps_json · main = 规划/汇总/校验（无角色权限）
          </span></div></template>
        <el-empty v-if="!subagents.roles.length" description="还没有编排记录" :image-size="50" />
        <el-table v-else :data="subagents.roles" size="small">
          <el-table-column label="角色" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.role === 'main' ? 'info' : 'primary'" effect="plain">
                {{ row.role === 'main' ? '主控' : roleZh(row.role) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="LLM 调用" width="82" align="right">
            <template #default="{ row }">
              {{ row.llm_calls }}<span v-if="row.llm_errors" class="bad"> ({{ row.llm_errors }}败)</span>
            </template>
          </el-table-column>
          <el-table-column label="费用" width="84" align="right">
            <template #default="{ row }">¥{{ num(row.cost_cny) }}</template>
          </el-table-column>
          <el-table-column label="LLM P95" width="82" align="right">
            <template #default="{ row }">{{ ms(row.llm_p95_ms) }}</template>
          </el-table-column>
          <el-table-column label="工具调用" width="90" align="right">
            <template #default="{ row }">
              <span v-if="row.role === 'main'">—</span>
              <template v-else>
                {{ row.tool_calls }}<span v-if="row.tool_denied" class="hint"> (拒{{ row.tool_denied }})</span>
              </template>
            </template>
          </el-table-column>
          <el-table-column label="步骤 完/挂/弃" width="110" align="right">
            <template #default="{ row }">
              <span v-if="row.role === 'main'">—</span>
              <template v-else>{{ row.steps_done }} / <span :class="{ bad: row.steps_failed }">{{ row.steps_failed }}</span> / {{ row.steps_skipped }}</template>
            </template>
          </el-table-column>
          <el-table-column label="重试" width="60" align="right" prop="retries" />
          <el-table-column label="主要模型" min-width="140" show-overflow-tooltip>
            <template #default="{ row }"><span class="mono">{{ row.main_model || '—' }}</span></template>
          </el-table-column>
          <el-table-column label="常用工具" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="!row.top_tools.length">—</span>
              <template v-else>{{ row.top_tools.map(t => `${t.tool}×${t.n}`).join('、') }}</template>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'

const days = ref(7)
const loading = ref(false)
const error = ref('')
const data = ref(null)

const llm = computed(() => data.value?.llm?.summary || {})
const b = computed(() => data.value?.budget || {})
const tools = computed(() => data.value?.tools || { by_tool: [], error_kinds: [] })
const o = computed(() => data.value?.orchestration || { by_status: {}, step_status: [], step_models: [] })
const intents = computed(() => data.value?.intents || { items: [], unclassified: 0 })
const subagents = computed(() => data.value?.subagents || { roles: [] })
const totalCost = computed(() => (data.value?.llm?.by_day || []).reduce((s, d) => s + (d.cost_cny || 0), 0))
const maxDay = computed(() => Math.max(0.01, ...(data.value?.llm?.by_day || []).map((d) => d.cost_cny || 0)))

function num(v) {
  const n = Number(v || 0)
  if (n === 0) return '0'
  if (n < 0.01) return n.toFixed(4)
  if (n < 1) return n.toFixed(3)
  return n.toFixed(2)
}
function pct(v) {
  if (v === null || v === undefined) return '—'
  return `${(Number(v) * 100).toFixed(Number(v) >= 0.995 ? 0 : 1)}%`
}
function ms(v) {
  if (v === null || v === undefined) return '—'
  return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`
}
function barW(v) { return `${Math.min(100, Math.max(2, (Number(v || 0) * 100)))}%` }
function dayH(v) { return `${Math.max(3, (Number(v || 0) / maxDay.value) * 100)}%` }
function miniW(v, total) { return `${Math.max(3, (v / Math.max(1, total)) * 100)}%` }
function ratioColor(v) {
  const n = Number(v || 0)
  return n >= 0.9 ? '#f56c6c' : n >= 0.7 ? '#e6a23c' : '#67c23a'
}
function statusColor(k) {
  if (k === 'done') return '#67c23a'
  if (k === 'partial') return '#e6a23c'
  if (k === 'failed') return '#f56c6c'
  return '#909399'      // capped / timeout：不是失败，用中性色
}
const STATUS_ZH = { done: '完成', partial: '部分完成', failed: '失败', capped: '成本封顶', timeout: '超时', running: '运行中' }
const ROLE_ZH = { researcher: '调研', analyst: '分析', writer: '撰写', critic: '校验' }
const KIND_ZH = {
  invalid_args: '参数错', unknown_tool: '工具不存在', timeout: '超时',
  bad_json: '返回非 JSON', failed: '执行错',
}
function statusZh(k) { return STATUS_ZH[k] || k }
function roleZh(k) { return ROLE_ZH[k] || k }
function kindZh(k) { return KIND_ZH[k] || k }

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.get(`/api/obs/metrics?days=${days.value}`)
  } catch (e) {
    error.value = e?.response?.data?.detail || '读不到指标数据'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.head-text h2 { margin: 0 0 6px; font-size: 18px; }
.sub { margin: 0; color: #909399; font-size: 12px; line-height: 1.7; max-width: 860px; }
.head-actions { display: flex; align-items: center; gap: 10px; }
.stat-num { font-size: 20px; font-weight: 600; color: #303133; }
.stat-label { margin-top: 4px; font-size: 11px; color: #909399; line-height: 1.6; }
.bar { margin-top: 8px; height: 4px; background: #f2f3f5; border-radius: 2px; overflow: hidden; }
.bar i { display: block; height: 100%; border-radius: 2px; }
.card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.hint { font-size: 11px; color: #b1b3b8; }
.days { display: flex; gap: 10px; align-items: flex-end; min-height: 96px; padding: 4px 2px 0; overflow-x: auto; }
.day-col { display: flex; flex-direction: column; align-items: center; width: 56px; flex: none; }
.day-bar-wrap { height: 64px; display: flex; align-items: flex-end; }
.day-bar { width: 22px; background: #409eff; border-radius: 2px 2px 0 0; transition: height .2s; }
.day-bar.over { background: #f56c6c; }
.day-label { margin-top: 4px; font-size: 10px; color: #909399; }
.day-cost { font-size: 10px; color: #b1b3b8; }
.bad { color: #f56c6c; }
.kinds { margin-top: 10px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kinds-title { font-size: 12px; color: #606266; }
.mini-title { font-size: 12px; color: #606266; margin-bottom: 8px; }
.mini-row { display: flex; align-items: center; gap: 8px; font-size: 12px; margin-bottom: 6px; }
.mini-k { min-width: 96px; color: #303133; }
.mini-bar { flex: 1; height: 5px; background: #f2f3f5; border-radius: 3px; overflow: hidden; }
.mini-bar i { display: block; height: 100%; border-radius: 3px; }
.mini-n { color: #909399; font-size: 11px; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px; }
</style>
