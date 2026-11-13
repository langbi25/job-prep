<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>Agent 能力 · 记忆 / 技能 / 编排</h2>
        <p class="sub">
          这四项不是"概念演示"，是每轮对话真实走的路：技能路由决定给模型哪些工具，
          长期记忆决定它把用户当成谁，工具调度决定调用怎么执行，编排决定复杂目标怎么拆。
        </p>
      </div>
      <el-button :icon="Refresh" circle @click="loadAll" />
    </div>

    <el-row :gutter="12" style="margin-bottom: 12px">
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">{{ mem.stats?.active ?? '–' }}</div>
          <div class="stat-label">有效记忆</div>
          <div class="stat-sub">历史 {{ mem.stats?.total ?? 0 }} 条（含被替代）</div>
        </div></el-card>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">{{ skillRuns }}</div>
          <div class="stat-label">技能调用</div>
          <div class="stat-sub">成功 {{ skillOk }} 次</div>
        </div></el-card>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">{{ plans.length }}</div>
          <div class="stat-label">编排次数</div>
          <div class="stat-sub">完成 {{ donePlans }} 次</div>
        </div></el-card>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">{{ skillList.length }}</div>
          <div class="stat-label">已注册技能</div>
          <div class="stat-sub">skills/*.md 为事实源</div>
        </div></el-card>
      </el-col>
    </el-row>

    <el-tabs v-model="tab">
      <!-- ════ 记忆 ════ -->
      <el-tab-pane label="长期记忆" name="memory">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom: 10px">
          <template #title>
            与知识卡正交：知识卡是"我学过什么"（可复习），记忆是"关于用户的事实与偏好"。
            同 key 只允许一条有效，新值替代旧值但保留痕迹；低置信的新值不会顶掉高置信的旧值。
          </template>
        </el-alert>
        <div style="margin-bottom: 6px">
          <el-tag size="small" :type="mem.vector?.enabled ? 'success' : 'info'" effect="plain">
            {{ mem.vector?.enabled
              ? `语义召回已开启 · ${mem.vector.model} · cos≥${mem.vector.min_cos}`
              : `语义召回未启用：${mem.vector?.reason || '检测中'}` }}
          </el-tag>
        </div>
        <div class="hint" style="margin-bottom: 10px; line-height: 1.6">
          词面 + 向量双通道取较优者；向量不过绝对阈值一律不计分（cosine 没有零点，0.4 在语义上也等于无关）。
        </div>
        <div class="filter-row">
          <el-radio-group v-model="memFilter" size="small">
            <el-radio-button value="all">全部（{{ mem.memories?.length || 0 }}）</el-radio-button>
            <el-radio-button v-for="(n, k) in (mem.stats?.by_kind || {})" :key="k" :value="k">
              {{ kindZh[k] || k }} {{ n }}
            </el-radio-button>
          </el-radio-group>
          <span class="hint" v-if="mem.stats?.events">
            变更事件：{{ Object.entries(mem.stats.events).map(([k, v]) => `${eventZh[k] || k} ${v}`).join(' · ') }}
          </span>
        </div>
        <el-empty v-if="!filteredMem.length" :description="'还没有记忆——聊两轮，系统会自动抽取；也可以在对话里让它「记住」'" :image-size="60" />
        <el-table v-else :data="filteredMem" size="small" border>
          <el-table-column label="类型" width="86">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="kindTag(row.kind)">{{ kindZh[row.kind] || row.kind }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="key" label="主题" width="150" />
          <el-table-column prop="value" label="内容" min-width="240" show-overflow-tooltip />
          <el-table-column label="置信度" width="90" align="center">
            <template #default="{ row }">
              <span :class="{ weak: row.confidence < 0.7 }">{{ (row.confidence * 100).toFixed(0) }}%</span>
            </template>
          </el-table-column>
          <el-table-column label="范围" width="86" align="center">
            <template #default="{ row }">
              <span class="hint">{{ row.scope === 'global' ? '跨会话' : '本会话' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="版本/命中" width="92" align="center">
            <template #default="{ row }">v{{ row.version }} · {{ row.access_count }} 次</template>
          </el-table-column>
          <el-table-column label="更新时间" width="150">
            <template #default="{ row }"><span class="hint">{{ (row.updated_at || '').replace('T', ' ') }}</span></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ════ 技能 ════ -->
      <el-tab-pane label="技能" name="skills">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom: 10px">
          <template #title>
            技能 = 一组工具 + 分步 SOP + 验收契约。命中技能后工具白名单会被收窄（减少选错），
            执行完用 require_tools 校验"关键步骤有没有真做"。
          </template>
        </el-alert>
        <el-row :gutter="12">
          <el-col :span="12" v-for="s in skillList" :key="s.name" style="margin-bottom: 12px">
            <el-card shadow="never">
              <template #header>
                <div class="card-head">
                  <span class="skill-title">{{ s.title }}</span>
                  <el-tag size="small" :type="s.risk === 'write' ? 'warning' : 'success'" effect="plain">
                    {{ s.risk === 'write' ? '会写库' : '只读' }}
                  </el-tag>
                </div>
              </template>
              <div class="skill-when">{{ s.when }}</div>
              <div v-if="s.steps && s.steps.length" class="skill-tools">
                <span class="hint">组合技能：</span>
                <template v-for="(st, i) in s.steps" :key="i">
                  <el-tag size="small" type="warning" effect="plain" class="mtag">{{ st.skill }}</el-tag>
                  <span v-if="i < s.steps.length - 1" class="arrow">→</span>
                </template>
              </div>
              <div v-else class="skill-tools">
                <span class="hint">工具：</span>
                <el-tag v-for="t in s.tools" :key="t" size="small" effect="plain" class="mtag">{{ t }}</el-tag>
              </div>
              <div class="skill-stats">
                <span v-if="statOf(s.name)">调用 {{ statOf(s.name).runs }} 次 · 成功 {{ statOf(s.name).ok_runs }} · 均 {{ Math.round(statOf(s.name).avg_ms || 0) }}ms</span>
                <span v-else class="hint">还没跑过</span>
              </div>
            </el-card>
          </el-col>
        </el-row>
        <el-empty v-if="!skillList.length" description="skills/ 目录下没有 md 文件" :image-size="60" />
      </el-tab-pane>

      <!-- ════ 编排 ════ -->
      <el-tab-pane label="多 Agent 编排" name="plans">
        <el-card shadow="never" style="margin-bottom: 12px">
          <div class="run-row">
            <el-input v-model="goal" placeholder="给一个复合目标，例如：判断最该补的两个主题并给出本周复习顺序"
                      @keyup.enter="runOrch" />
            <el-select v-model="orchMode" style="width: 130px">
              <el-option label="只读（默认）" value="assist" />
              <el-option label="允许写库" value="auto" />
            </el-select>
            <el-button type="primary" :loading="running" @click="runOrch">跑一次</el-button>
          </div>
          <div class="hint" style="margin-top: 6px">
            拆解 → 无依赖步骤并行 → 汇总 → 自检（发现证据不足会重跑被点名的步骤，最多一轮）。
            默认只读：并行写库出问题无法定位是谁写的。
          </div>

          <div v-if="running && orchProgress.steps.length" class="orch-progress">
            <div v-for="s in orchProgress.steps" :key="s.id" class="orch-step">
              <el-tag size="small" effect="plain"
                      :type="s.status === 'done' ? 'success' : s.status === 'failed' ? 'danger' : s.status === 'running' ? 'primary' : 'info'">
                {{ stepZh[s.status] || s.status }}
              </el-tag>
              <span class="role">{{ s.role_label }}</span>
              <span class="goal">{{ s.goal }}</span>
              <span v-if="s.duration_ms" class="ms">{{ (s.duration_ms / 1000).toFixed(1) }}s</span>
            </div>
          </div>
          <div v-if="running && orchProgress.phase" class="orch-phase">
            <span class="pulse"></span><span class="phase-text">{{ orchProgress.phase }}</span>
          </div>
        </el-card>

        <el-row :gutter="12">
          <el-col :span="9">
            <el-card shadow="never">
              <template #header><div class="card-head"><span>编排记录（{{ plans.length }}）</span></div></template>
              <el-empty v-if="!plans.length" description="还没跑过编排" :image-size="60" />
              <div v-for="p in plans" :key="p.id" class="plan-row" :class="{ active: p.id === detail?.id }"
                   @click="openPlan(p.id)">
                <div class="plan-line1">
                  <el-tag size="small" :type="planTag(p.status)" effect="plain">{{ planZh[p.status] || p.status }}</el-tag>
                  <span class="plan-goal">{{ p.goal }}</span>
                </div>
                <div class="plan-line2">
                  {{ (p.created_at || '').replace('T', ' ') }} · {{ p.tokens_used }} tok · ¥{{ fmtCost(p.cost_cny) }} · {{ p.duration_ms }}ms
                  <span v-if="p.resume_count" class="resume-badge">· 续跑 {{ p.resume_count }} 次</span>
                </div>
              </div>
            </el-card>
          </el-col>

          <el-col :span="15">
            <el-card shadow="never" v-if="detail">
              <template #header>
                <div class="card-head">
                  <span class="card-title">{{ detail.goal }}</span>
                  <el-tag size="small" :type="planTag(detail.status)" effect="plain">{{ planZh[detail.status] || detail.status }}</el-tag>
                </div>
              </template>
              <div class="resume-bar" v-if="detail.status !== 'done'">
                <el-button size="small" type="warning" plain :loading="resuming" @click="resumePlan(detail.id)">
                  从断点续跑
                </el-button>
                <span class="hint">复用已完成步骤的结论，只重跑失败/跳过的步骤（不重新规划）</span>
              </div>
              <div class="resume-note" v-if="detail.resume_count">
                已续跑 {{ detail.resume_count }} 次
                <span v-if="detail.result?.skipped_done_steps?.length" class="hint">
                  · 上次直接复用 {{ detail.result.skipped_done_steps.join('、') }} 的结论
                </span>
              </div>
              <div class="steps">
                <div v-for="s in detail.steps" :key="s.id" class="step">
                  <div class="step-head">
                    <span class="step-id">{{ s.id }}</span>
                    <el-tag size="small" effect="plain">{{ roleZh[s.role] || s.role }}</el-tag>
                    <el-tag size="small" :type="stepTag(s.status)" effect="plain">{{ stepZh[s.status] || s.status }}</el-tag>
                    <span class="hint">{{ s.duration_ms }}ms</span>
                    <span v-if="s.tools && s.tools.length" class="hint">· {{ s.tools.length }} 次工具</span>
                    <span v-if="s.tokens" class="hint">· {{ s.tokens }} tok</span>
                    <span v-if="s.cost_cny" class="hint">· ¥{{ fmtCost(s.cost_cny) }}</span>
                    <span v-if="s.output" class="step-toggle" @click="toggle(s.id)">
                      {{ expanded[s.id] ? '收起' : '看结论' }}
                    </span>
                  </div>
                  <div class="step-goal">{{ s.goal }}</div>
                  <div class="step-err" v-if="s.error">{{ s.error }}</div>
                  <div class="step-out" v-if="expanded[s.id]">{{ s.output }}</div>
                </div>
              </div>
              <div class="answer" v-if="detail.result?.answer">
                <div class="answer-title">最终答案</div>
                <div class="answer-body">{{ detail.result.answer }}</div>
              </div>
              <div class="critique" v-if="detail.result?.critique">
                <span class="hint">自检：</span>
                <el-tag size="small" :type="detail.result.critique.ok ? 'success' : 'warning'" effect="plain">
                  {{ detail.result.critique.ok ? '通过' : '有问题' }}
                </el-tag>
                <span v-if="detail.result.retry_rounds" class="hint"> · 已重跑 {{ detail.result.retry_rounds }} 轮</span>
                <div v-for="(i, k) in (detail.result.critique.issues || [])" :key="k" class="issue">· {{ i }}</div>
              </div>
            </el-card>
            <el-card shadow="never" v-else>
              <el-empty description="左侧选一条编排记录看步骤 DAG，或直接在上面跑一次" :image-size="70" />
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>
      <!-- ════ 成本 ════ -->
      <el-tab-pane label="成本" name="cost">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom: 10px">
          <template #title>
            一行账 = 一次 LLM 调用（llm_usage 台账，写入点是所有调用的唯一咽喉，漏不掉）。
            单价取 DeepSeek 公布价（COST_PRICE_JSON 可覆盖），缓存命中按折扣价计；
            不在价目表里的模型按默认价估算并标记。
          </template>
        </el-alert>
        <el-row :gutter="12" style="margin-bottom: 12px">
          <el-col :xs="12" :sm="12" :md="6">
            <el-card shadow="never"><div class="stat">
              <div class="stat-num">¥{{ fmtCost(cost.summary?.cost_cny) }}</div>
              <div class="stat-label">近 {{ cost.window_days || 14 }} 天总费用</div>
              <div class="stat-sub" v-if="cost.summary?.estimated_calls">含 {{ cost.summary.estimated_calls }} 次估算价</div>
            </div></el-card>
          </el-col>
          <el-col :xs="12" :sm="12" :md="6">
            <el-card shadow="never"><div class="stat">
              <div class="stat-num">{{ cost.summary?.calls ?? 0 }}</div>
              <div class="stat-label">LLM 调用次数</div>
              <div class="stat-sub">编排中每个子 agent 都算</div>
            </div></el-card>
          </el-col>
          <el-col :xs="12" :sm="12" :md="6">
            <el-card shadow="never"><div class="stat">
              <div class="stat-num">{{ fmtTok(totalTok) }}</div>
              <div class="stat-label">总 token</div>
              <div class="stat-sub">入 {{ fmtTok(cost.summary?.prompt_tokens) }} / 出 {{ fmtTok(cost.summary?.completion_tokens) }}</div>
            </div></el-card>
          </el-col>
          <el-col :xs="12" :sm="12" :md="6">
            <el-card shadow="never"><div class="stat">
              <div class="stat-num">¥{{ fmtCost(avgCall) }}</div>
              <div class="stat-label">单次调用均价</div>
              <div class="stat-sub">缓存命中 {{ fmtTok(cost.summary?.cached_tokens) }} tok</div>
            </div></el-card>
          </el-col>
        </el-row>

        <!-- 今日预算：闸门是硬的，用户必须能提前看见还剩多少——
             否则只会遇到"突然不能用了"，而不知道是钱花完了 -->
        <div v-if="cost.budget?.enabled" class="budget-bar"
             :class="{ 'budget-blocked': cost.budget.blocked }">
          <div class="budget-row">
            <span>今日已花 <b>¥{{ fmtCost(cost.budget.spent_cny) }}</b>
              / 上限 ¥{{ fmtCost(cost.budget.limit_cny) }}</span>
            <span class="hint" v-if="cost.budget.blocked">
              已触发硬闸门：新的 LLM 调用会被拒绝，明天自动恢复
            </span>
            <span class="hint" v-else>剩余 ¥{{ fmtCost(cost.budget.remaining_cny) }}</span>
          </div>
          <el-progress :percentage="Math.min(100, Math.round((cost.budget.ratio || 0) * 100))"
                       :status="cost.budget.blocked ? 'exception' : undefined"
                       :stroke-width="6" :show-text="false" />
        </div>

        <el-row :gutter="12">
          <el-col :span="12">
            <el-card shadow="never">
              <template #header><div class="card-head"><span>按天</span></div></template>
              <el-empty v-if="!cost.by_day?.length" description="还没有调用记录" :image-size="60" />
              <el-table v-else :data="cost.by_day" size="small" border>
                <el-table-column prop="day" label="日期" width="100" />
                <el-table-column prop="calls" label="调用" width="60" align="center" />
                <el-table-column label="token" align="right">
                  <template #default="{ row }">{{ fmtTok(row.total_tokens) }}</template>
                </el-table-column>
                <el-table-column label="费用" width="100" align="right">
                  <template #default="{ row }">¥{{ fmtCost(row.cost_cny) }}</template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card shadow="never" style="margin-bottom: 12px">
              <template #header><div class="card-head"><span>按场景（费用占比）</span></div></template>
              <el-empty v-if="!cost.by_scene?.length" description="还没有调用记录" :image-size="60" />
              <div v-else>
                <div v-for="r in cost.by_scene" :key="r.scene" class="scene-row">
                  <div class="scene-top">
                    <span class="scene-name" :title="r.scene">{{ r.scene }}</span>
                    <span class="scene-cost">¥{{ fmtCost(r.cost_cny) }} · {{ r.calls }} 次</span>
                  </div>
                  <el-progress :percentage="scenePct(r)" :stroke-width="8" :show-text="false" />
                </div>
              </div>
            </el-card>
            <el-card shadow="never">
              <template #header><div class="card-head"><span>按模型</span></div></template>
              <el-empty v-if="!cost.by_model?.length" description="还没有调用记录" :image-size="60" />
              <el-table v-else :data="cost.by_model" size="small" border>
                <el-table-column prop="model" label="模型" />
                <el-table-column prop="calls" label="调用" width="60" align="center" />
                <el-table-column label="token" align="right">
                  <template #default="{ row }">{{ fmtTok(row.total_tokens) }}</template>
                </el-table-column>
                <el-table-column label="费用" width="100" align="right">
                  <template #default="{ row }">¥{{ fmtCost(row.cost_cny) }}</template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'

const tab = ref('memory')
const mem = reactive({ stats: {}, memories: [] })
const memFilter = ref('all')
const skillList = ref([])
const skillStats = ref([])
const plans = ref([])
const detail = ref(null)
const expanded = reactive({})
const goal = ref('')
const orchMode = ref('assist')
const running = ref(false)
const resuming = ref(false)
const orchProgress = reactive({ steps: [], phase: '' })

const kindZh = { fact: '事实', preference: '偏好', episode: '情景', task_state: '任务状态' }
const eventZh = { created: '新建', merged: '合并', superseded: '替代', rejected: '拒绝', forgotten: '遗忘' }
const planZh = { done: '完成', partial: '部分完成', failed: '失败', running: '进行中' }
const stepZh = { done: '完成', failed: '失败', skipped: '跳过', pending: '待执行', running: '执行中' }
const roleZh = { researcher: '调研', analyst: '分析', writer: '撰写', critic: '校验' }

const cost = reactive({ window_days: 14, summary: {}, by_day: [], by_scene: [],
                        by_model: [], by_session: [], budget: {} })
const avgCall = computed(() => {
  const s = cost.summary || {}
  return s.calls ? (s.cost_cny || 0) / s.calls : 0
})
const totalTok = computed(() => (cost.summary?.prompt_tokens || 0) + (cost.summary?.completion_tokens || 0))
const fmtCost = (v) => (Number(v) || 0).toFixed(4)
const fmtTok = (v) => {
  const n = Number(v) || 0
  return n >= 10000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}
const scenePct = (r) => {
  const total = (cost.summary || {}).cost_cny || 0
  return total > 0 ? Math.round(100 * (r.cost_cny || 0) / total) : 0
}

const filteredMem = computed(() => memFilter.value === 'all'
  ? (mem.memories || [])
  : (mem.memories || []).filter((m) => m.kind === memFilter.value))
const skillRuns = computed(() => skillStats.value.reduce((a, s) => a + (s.runs || 0), 0))
const skillOk = computed(() => skillStats.value.reduce((a, s) => a + (s.ok_runs || 0), 0))
const donePlans = computed(() => plans.value.filter((p) => p.status === 'done').length)

const statOf = (name) => skillStats.value.find((s) => s.skill === name)
const kindTag = (k) => ({ preference: 'warning', episode: 'info', task_state: 'success' }[k] || '')
const planTag = (s) => ({ done: 'success', partial: 'warning', failed: 'danger' }[s] || 'info')
const stepTag = (s) => ({ done: 'success', failed: 'danger', skipped: 'info' }[s] || 'info')

async function loadAll() {
  try {
    const [m, s, p, c] = await Promise.all([
      api.get('/api/agent/memory'),
      api.get('/api/console/skills'),
      api.get('/api/agent/plans?limit=30'),
      api.get('/api/agent/cost?days=14'),
    ])
    Object.assign(mem, { stats: m.stats || {}, memories: m.memories || [],
                         vector: m.vector || { enabled: false, reason: 'unknown' } })
    skillList.value = s.skills || []
    skillStats.value = s.stats || []
    plans.value = p.plans || []
    Object.assign(cost, { window_days: c.window_days || 14, summary: c.summary || {},
      by_day: c.by_day || [], by_scene: c.by_scene || [], by_model: c.by_model || [],
      by_session: c.by_session || [], budget: c.budget || {} })
    if (!detail.value && plans.value.length) openPlan(plans.value[0].id)
  } catch (e) { /* 拦截器已提示 */ }
}

async function openPlan(id) {
  try {
    detail.value = await api.get(`/api/agent/plans/${id}`)
  } catch (e) { /* 拦截器已提示 */ }
}

function toggle(id) { expanded[id] = !expanded[id] }

async function runOrch() {
  const g = goal.value.trim()
  if (!g) { ElMessage.warning('先给一个目标'); return }
  running.value = true
  orchProgress.steps = []
  orchProgress.phase = '正在拆解…'

  // 编排一次 20~35 秒，必须流式看进度；断线要能续传（重跑等于再花一次钱）
  let streamId = ''
  let lastSeq = 0
  let finished = false
  let result = null

  const consume = async (url, init) => {
    const resp = await fetch(url, init)
    if (!resp.ok) {
      let detail = `请求失败（${resp.status}）`
      try { const j = await resp.json(); if (typeof j.detail === 'string') detail = j.detail } catch { /* 默认 */ }
      throw new Error(detail)
    }
    streamId = resp.headers.get('X-Stream-Id') || streamId
    const reader = resp.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      const parts = buf.split('\n\n')
      buf = parts.pop()
      for (const p of parts) {
        const dl = p.split('\n').find((l) => l.startsWith('data:'))
        if (!dl) continue
        try {
          const ev = JSON.parse(dl.slice(5))
          if (ev.seq) lastSeq = ev.seq
          if (ev.type === 'done') { finished = true; result = ev.result }
          else if (ev.type === 'error') { finished = true; throw new Error(ev.message || '编排失败') }
          applyOrchEvent(ev)
        } catch (err) {
          if (err instanceof SyntaxError) continue
          throw err
        }
      }
    }
  }

  try {
    await consume('/api/agent/orchestrate/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal: g, mode: orchMode.value, max_steps: 4 }),
    })
    if (result) {
      ElMessage.success(`编排${planZh[result.status] || result.status}：${result.steps.length} 步 / ${result.tokens} tokens / ¥${fmtCost(result.cost_cny)}`)
      await loadAll()
      await openPlan(result.plan_id)
      goal.value = ''
    }
  } catch (e) {
    if (streamId && !finished) {
      try {
        await consume(`/api/agent/orchestrate/stream/${streamId}?after=${lastSeq}`, { method: 'GET' })
        if (result) {
          ElMessage.success(`编排${planZh[result.status] || result.status}（已从断点续上）`)
          await loadAll()
          await openPlan(result.plan_id)
        }
      } catch (e2) {
        ElMessage.error(`编排中断且续传失败：${e2.message || e2}`)
      }
    } else {
      ElMessage.error(e.message || '编排失败')
    }
  } finally {
    running.value = false
    orchProgress.phase = ''
  }
}

function applyOrchEvent(ev) {
  const find = (id) => orchProgress.steps.find((s) => s.id === id)
  if (ev.type === 'plan') {
    orchProgress.steps = (ev.steps || []).map((s) => ({ ...s, status: 'pending', duration_ms: 0 }))
    orchProgress.phase = `已拆成 ${orchProgress.steps.length} 步，开始执行`
  } else if (ev.type === 'step_start') {
    const s = find(ev.id)
    if (s) s.status = 'running'
    orchProgress.phase = `执行中：${s?.role_label || ev.role}`
  } else if (ev.type === 'step_end') {
    const s = find(ev.id)
    if (s) { s.status = ev.status; s.duration_ms = ev.duration_ms; s.error = ev.error }
  } else if (ev.type === 'synthesize') {
    orchProgress.phase = '正在汇总…'
  } else if (ev.type === 'critique') {
    orchProgress.phase = ev.ok ? '自检通过，准备收尾' : '自检发现问题，可能重跑个别步骤'
  }
}

async function resumePlan(id) {
  resuming.value = true
  try {
    const res = await api.post(`/api/agent/plans/${id}/resume`, null, { timeout: 300000 })
    const ok = res.steps.filter((s) => s.status === 'done').length
    ElMessage.success(
      `续跑${planZh[res.status] || res.status}：${ok}/${res.steps.length} 步完成 · 本次累计 ¥${fmtCost(res.cost_cny)}`)
    await loadAll()
    await openPlan(id)
  } catch (e) { /* 拦截器已提示 */ } finally { resuming.value = false }
}

onMounted(loadAll)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.sub { color: #909399; font-size: 12px; margin: 4px 0 0; max-width: 900px; line-height: 1.6; }
.hint { color: #909399; font-size: 12px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.stat { text-align: center; padding: 4px 0; }
.budget-bar { margin: 0 0 12px; padding: 10px 14px; border-radius: 6px;
  background: #f4f8ff; border: 1px solid #d9e8ff; }
.budget-blocked { background: #fef0f0; border-color: #fbc4c4; }
.budget-row { display: flex; justify-content: space-between; align-items: center;
  font-size: 12px; color: #303133; margin-bottom: 6px; gap: 12px; }
.stat-num { font-size: 24px; font-weight: 700; color: #303133; }
.stat-label { color: #606266; font-size: 12px; margin-top: 2px; }
.orch-progress { margin-top: 12px; border-top: 1px solid #ebeef5; padding-top: 10px; }
.orch-step { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 12px; }
.orch-step .role { color: #303133; font-weight: 500; flex: 0 0 auto; }
.orch-step .goal { color: #606266; flex: 1 1 auto; min-width: 0; overflow: hidden;
                   text-overflow: ellipsis; white-space: nowrap; }
.orch-step .ms { color: #909399; flex: 0 0 auto; font-variant-numeric: tabular-nums; }
.orch-phase { margin-top: 8px; font-size: 12px; color: #606266; display: flex; align-items: center; gap: 6px; }
.orch-phase .phase-text { flex: 1 1 auto; min-width: 0; }
.orch-phase .pulse { flex: 0 0 auto; width: 6px; height: 6px; border-radius: 50%;
                     background: #409eff; animation: orchpulse 1.2s ease-in-out infinite; }
@keyframes orchpulse { 0%, 100% { opacity: .25; transform: scale(.8); } 50% { opacity: 1; transform: scale(1.15); } }
.stat-sub { color: #c0c4cc; font-size: 11px; margin-top: 2px; }
.filter-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 12px; }
.weak { color: #e6a23c; font-weight: 600; }

.skill-title { font-weight: 600; font-size: 14px; }
.skill-when { font-size: 12px; color: #606266; line-height: 1.6; margin-bottom: 8px; }
.skill-tools { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.arrow { color: #c0c4cc; font-size: 11px; margin: 0 2px; }
.head-right { display: flex; align-items: center; gap: 8px; flex: none; }
.resume-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.card-title { flex: 1; min-width: 0; margin-right: 8px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.resume-badge { color: #e6a23c; }
.resume-note { font-size: 12px; color: #e6a23c; margin-bottom: 8px; }
.mtag { font-size: 10px; }
.skill-stats { margin-top: 8px; font-size: 12px; color: #409eff; }

.run-row { display: flex; gap: 8px; }
.plan-row { padding: 8px 6px; border-bottom: 1px dashed #ebeef5; cursor: pointer; border-radius: 4px; }
.plan-row:hover { background: #f5f7fa; }
.plan-row.active { background: #ecf5ff; }
.plan-line1 { display: flex; align-items: center; gap: 6px; }
.plan-goal { font-size: 13px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plan-line2 { color: #909399; font-size: 11px; margin-top: 2px; }

.steps { border-top: 1px solid #ebeef5; }
.step { padding: 8px 0; border-bottom: 1px dashed #ebeef5; }
.step-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.step-id { font-family: monospace; font-size: 12px; color: #909399; width: 26px; }
.step-toggle { margin-left: auto; font-size: 12px; color: #409eff; cursor: pointer; }
.step-goal { font-size: 12px; color: #606266; margin: 4px 0 0 32px; }
.step-err { font-size: 12px; color: #f56c6c; margin: 4px 0 0 32px; }
.step-out { font-size: 12px; color: #303133; background: #fafafa; border: 1px solid #ebeef5;
  border-radius: 4px; padding: 8px; margin: 6px 0 0 32px; white-space: pre-wrap; line-height: 1.7; }

.answer { margin-top: 12px; background: #f0f9eb; border: 1px solid #e1f3d8; border-radius: 6px; padding: 10px 12px; }
.answer-title { font-size: 12px; font-weight: 600; color: #67c23a; margin-bottom: 6px; }
.answer-body { font-size: 13px; color: #303133; line-height: 1.8; white-space: pre-wrap; }
.critique { margin-top: 10px; font-size: 12px; }
.issue { color: #e6a23c; margin-top: 4px; line-height: 1.6; }

.scene-row { padding: 6px 0; }
.scene-top { display: flex; justify-content: space-between; align-items: baseline;
  gap: 8px; margin-bottom: 3px; }
.scene-name { font-size: 12px; color: #606266; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.scene-cost { font-size: 12px; color: #303133; white-space: nowrap; flex-shrink: 0; }
</style>
