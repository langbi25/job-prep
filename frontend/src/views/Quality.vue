<template>
  <div class="page">
    <div class="page-header">
      <div class="head-text">
        <h2>回合质量</h2>
        <p class="sub">
          一轮问答一行事实。左边是"跑了什么"，右边是"跑得好不好"——
          自进化只认右边的标签，左边只用来解释原因。
          没有样本时这里会空着，不会显示 0%，因为"没人评价"和"没人不满"是两件事。
        </p>
      </div>
      <div class="head-actions">
        <el-select v-model="days" size="small" style="width: 108px" @change="load">
          <el-option :value="7" label="近 7 天" />
          <el-option :value="14" label="近 14 天" />
          <el-option :value="30" label="近 30 天" />
        </el-select>
        <el-button :icon="Refresh" circle :loading="loading" @click="load" />
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="warning" :closable="false" style="margin-bottom: 14px" />

    <!-- 分母的自检。上面四个比例都建立在"分母是对的"之上，
         所以它必须排在这些比例前面，而不是收在页面最底部当脚注。 -->
    <div v-if="cov.counted_since" class="cov" :class="{ warn: (cov.coverage ?? 1) < 0.95 || cov.sessions_after_wall > 0 }">
      <div class="cov-row">
        <span class="cov-title">落账覆盖率</span>
        <b class="cov-num">{{ pct(cov.coverage) }}</b>
        <span class="hint">{{ cov.turns_recorded }} / {{ cov.turns_seen }} 轮</span>
        <span class="cov-sep">·</span>
        <span class="hint" :title="cov.no_trace ? `${cov.no_trace} 行缺 trace_id（多为开始记账之前的行）` : ''">
          trace 完整 {{ pct(cov.trace_complete) }}<template v-if="cov.no_trace"> ({{ cov.no_trace }} 行缺)</template>
        </span>
        <span class="cov-sep">·</span>
        <span class="hint">费用完整 {{ pct(cov.cost_complete) }}</span>
        <span class="cov-sep">·</span>
        <span class="hint">自 {{ (cov.counted_since || '').slice(0, 16).replace('T', ' ') }} 起</span>
      </div>
      <div v-if="cov.missing?.length" class="cov-miss">
        <b>{{ cov.sessions_missing }}</b> 个会话有轮次没记全
        （最严重：<code>{{ cov.missing[0].session_id }}</code>
        缺 {{ cov.missing[0].lost }}/{{ cov.missing[0].turns }} 轮）。
        覆盖率不足 100% 时，上面所有比例只对"记下来的那部分"成立。
      </div>
      <div v-if="cov.sessions_after_wall" class="cov-miss bad">
        <b>{{ cov.sessions_after_wall }}</b> 个会话在开始记账之后仍然一条事实都没落——这是真漏，不是历史。
      </div>
      <div v-else-if="cov.sessions_before_wall" class="cov-miss">
        另有 {{ cov.sessions_before_wall }} 个会话发生在开始记账之前，不计入分母（没有回填历史）。
      </div>
    </div>

    <el-row :gutter="12" style="margin-bottom: 14px">
      <el-col :xs="12" :sm="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">{{ s.turns ?? '–' }}</div>
          <div class="stat-label">回合数</div>
          <div class="stat-sub">{{ s.first_ts ? `自 ${s.first_ts.slice(0, 10)} 起` : '还没有记录' }}</div>
        </div></el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">{{ s.flagged ?? 0 }}<span v-if="s.flag_rate != null" class="of">/{{ s.turns }}</span></div>
          <div class="stat-label">被标问题的回合</div>
          <div class="stat-sub">占 {{ pct(s.flag_rate) }}</div>
        </div></el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num" :class="{ bad: s.reject_rate >= 0.3 }">{{ pct(s.reject_rate) }}</div>
          <div class="stat-label">点踩率</div>
          <div class="stat-sub">{{ s.rated || 0 }} 人表态 · 差评 {{ s.disliked || 0 }}</div>
        </div></el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">{{ s.p95_ms ? (s.p95_ms / 1000).toFixed(1) + 's' : '–' }}</div>
          <div class="stat-label">P95 耗时</div>
          <div class="stat-sub">中位 {{ s.p50_ms ? (s.p50_ms / 1000).toFixed(1) + 's' : '–' }}
            · ¥{{ (s.cost_cny || 0).toFixed(2) }}</div>
        </div></el-card>
      </el-col>
    </el-row>

    <el-empty v-if="!loading && !s.turns" description="还没有回合记录。去 Agent 页问一轮，这里就会有数据。" :image-size="70" />

    <template v-else>
      <el-row :gutter="12">
        <el-col :md="12" :xs="24">
          <el-card shadow="never" class="box">
            <template #header>
              <div class="bh">
                <span>问题分布</span>
                <span class="hint">按主因归类，一条回合只算一次</span>
              </div>
            </template>
            <el-empty v-if="!s.by_rule?.length" description="没有回合被标问题" :image-size="50" />
            <div v-for="r in s.by_rule" :key="r.rule" class="rule-row">
              <div class="rule-top">
                <span class="rule-name">{{ r.label }}</span>
                <span class="rule-n">{{ r.n }}</span>
              </div>
              <div class="bar"><i :style="{ width: barW(r.n) }" /></div>
            </div>
          </el-card>
        </el-col>
        <el-col :md="12" :xs="24">
          <el-card shadow="never" class="box">
            <template #header>
              <div class="bh">
                <span>按技能</span>
                <span class="hint">同一口径，可横向比</span>
              </div>
            </template>
            <el-empty v-if="!s.by_skill?.length" description="还没有数据" :image-size="50" />
            <div v-for="k in s.by_skill" :key="k.skill" class="sk-row">
              <code class="sk-name">{{ k.skill }}</code>
              <span class="sk-n">{{ k.turns }} 轮</span>
              <span v-if="k.flagged" class="sk-bad">{{ k.flagged }} 标问题</span>
              <span v-if="k.disliked" class="sk-bad">差评 {{ k.disliked }}</span>
              <span class="sk-time">{{ (k.avg_ms / 1000).toFixed(1) }}s · ¥{{ Number(k.avg_cost).toFixed(3) }}</span>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <el-card shadow="never" class="box" style="margin-top: 12px">
        <template #header>
          <div class="bh">
            <span>该改哪里</span>
            <span class="hint">归因的出口：不是"失败 5 次"，是"改哪个文件、加哪句话"</span>
          </div>
        </template>
        <el-empty v-if="!imp.skills?.length" description="没有需要改的地方" :image-size="50" />
        <div v-for="sk in imp.skills" :key="sk.skill" class="imp-block">
          <div class="imp-head">
            <code class="sk-name">{{ sk.skill }}</code>
            <span class="hint">{{ sk.turns }} 轮中 {{ sk.flagged }} 轮有问题</span>
          </div>
          <div v-for="r in sk.rules" :key="r.rule" class="imp-rule">
            <el-tag size="small" type="warning" effect="plain">{{ r.label }} ×{{ r.n }}</el-tag>
            <span class="imp-target">{{ r.target }}</span>
            <span class="imp-advice">{{ r.advice }}</span>
          </div>
        </div>
      </el-card>

      <el-card shadow="never" class="box" style="margin-top: 12px">
        <template #header>
          <div class="bh">
            <span>回归用例候选</span>
            <span class="hint">真实出过问题的提问。must_mention 留空——该提到什么只能人定</span>
          </div>
        </template>
        <el-empty v-if="!cases.length" description="还没有候选。点踩或出现规则命中后会出现在这里" :image-size="50" />
        <div v-for="c in cases" :key="c.turn_id" class="case">
          <div class="case-top">
            <el-tag size="small" :type="c.feedback === -1 ? 'danger' : 'info'" effect="plain">{{ c.label }}</el-tag>
            <code>{{ c.skill || '(通用)' }}</code>
            <span class="hint">{{ (c.ts || '').slice(5, 16).replace('T', ' ') }}</span>
            <span class="hint">工具 {{ c.tool_calls }} 次 · {{ (c.duration_ms / 1000).toFixed(1) }}s</span>
          </div>
          <div class="case-q">{{ c.query || '（找不到原话，可能是采集前的老对话）' }}</div>
          <div v-if="c.feedback_note" class="case-note">你的备注：{{ c.feedback_note }}</div>
        </div>
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'

const days = ref(14)
const loading = ref(false)
const error = ref('')
const s = ref({})
const cov = ref({})
const imp = ref({ skills: [] })
const cases = ref([])

const maxRule = computed(() => Math.max(1, ...(s.value.by_rule || []).map((r) => r.n)))

function pct(v) {
  if (v === null || v === undefined) return '—'
  return `${(Number(v) * 100).toFixed(Number(v) >= 0.995 ? 0 : 1)}%`
}
function barW(n) { return `${Math.max(4, (n / maxRule.value) * 100)}%` }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [q, i, c, cv] = await Promise.all([
      api.get(`/api/turns/quality?days=${days.value}`),
      api.get(`/api/turns/improvement?days=${days.value}`),
      api.get(`/api/turns/regression-candidates?days=${days.value}&limit=20`),
      api.get(`/api/turns/coverage?days=${days.value}`),
    ])
    s.value = q.summary || {}
    imp.value = i || { skills: [] }
    cases.value = c.candidates || []
    cov.value = cv || {}
  } catch (e) {
    error.value = e?.response?.data?.detail || '读不到回合数据'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.page { padding: 20px 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start;
  gap: 16px; margin-bottom: 16px; }
.head-text { flex: 1; min-width: 0; }
.head-text h2 { margin: 0 0 6px; font-size: 20px; white-space: nowrap; }
.sub { margin: 0; font-size: 12px; color: #909399; max-width: 660px; line-height: 1.7; }
.head-actions { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
.cov { border: 1px solid #e4e7ed; border-left: 3px solid #67c23a; border-radius: 4px;
  background: #fafafa; padding: 9px 12px; margin-bottom: 14px; }
.cov.warn { border-left-color: #e6a23c; background: #fdf6ec; }
.cov-row { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.cov-title { font-size: 12px; color: #606266; }
.cov-num { font-size: 15px; color: #303133; }
.cov.warn .cov-num { color: #e6a23c; }
.cov-sep { color: #dcdfe6; font-size: 11px; }
.cov-miss { margin-top: 5px; font-size: 11px; color: #909399; line-height: 1.6; }
.cov-miss.bad { color: #f56c6c; }
.cov-miss b { color: #606266; }
.cov-miss.bad b { color: #f56c6c; }
.cov-miss code { font-size: 11px; color: #606266; }
.stat { text-align: center; padding: 4px 0; }.stat-num { font-size: 24px; font-weight: 600; color: #303133; }
.stat-num.bad { color: #f56c6c; }
.stat-num .of { font-size: 14px; color: #c0c4cc; font-weight: 400; }
.stat-label { font-size: 12px; color: #606266; margin-top: 2px; }
.stat-sub { font-size: 11px; color: #b1b3b8; margin-top: 4px; }
.box { margin-bottom: 0; }
.bh { display: flex; justify-content: space-between; align-items: baseline;
  flex-wrap: wrap; gap: 2px 12px; }
.bh span:first-child { font-size: 13px; font-weight: 500; white-space: nowrap; }
.hint { font-size: 11px; color: #b1b3b8; }
.rule-row { margin-bottom: 10px; }
.rule-top { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; }
.rule-name { color: #303133; }
.rule-n { color: #909399; }
.bar { height: 6px; background: #f2f3f5; border-radius: 3px; overflow: hidden; }
.bar i { display: block; height: 100%; background: #e6a23c; border-radius: 3px; }
.sk-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 7px 0; border-bottom: 0.5px solid #f2f3f5; font-size: 12px; }
.sk-row:last-child { border-bottom: none; }
.sk-name { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; color: #303133; }
.sk-n { color: #909399; }
.sk-bad { color: #e6a23c; }
.sk-time { margin-left: auto; color: #b1b3b8; font-size: 11px; }
.imp-block { padding: 8px 0; border-bottom: 0.5px solid #f2f3f5; }
.imp-block:last-child { border-bottom: none; }
.imp-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
.imp-rule { display: flex; align-items: flex-start; gap: 8px; margin: 6px 0 0 2px; font-size: 12px; }
.imp-target { color: #409eff; white-space: nowrap; }
.imp-advice { color: #606266; line-height: 1.6; }
.case { padding: 8px 0; border-bottom: 0.5px solid #f2f3f5; }
.case:last-child { border-bottom: none; }
.case-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 11px; }
.case-top code { font-size: 11px; color: #606266; }
.case-q { margin-top: 5px; font-size: 13px; color: #303133; line-height: 1.6; }
.case-note { margin-top: 4px; font-size: 11px; color: #f56c6c; }
</style>
