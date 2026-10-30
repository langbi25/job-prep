<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>模拟面试</h2>
        <p class="sub">组卷优先挑高频题 + 薄弱主题；答完当场四维评分，交卷后写回错题本与掌握度</p>
      </div>
      <div>
        <el-button v-if="session" @click="exitSession">退出本场</el-button>
        <el-button :icon="Refresh" circle @click="loadSessions" />
      </div>
    </div>

    <!-- 未开场：组卷 + 历史 -->
    <template v-if="!session">
      <el-row :gutter="16">
        <el-col :span="10">
          <el-card shadow="never">
            <template #header>新建一场</template>
            <el-form label-width="76px" size="small">
              <el-form-item label="方向">
                <el-radio-group v-model="form.direction">
                  <el-radio-button value="bigdata">大数据</el-radio-button>
                  <el-radio-button value="backend">后端</el-radio-button>
                  <el-radio-button value="agent">Agent</el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="面试官">
                <el-input v-model="form.persona" placeholder="可选，如：严厉的技术总监，专挑边界与权衡" />
              </el-form-item>
              <el-form-item label="JD">
                <el-input v-model="form.jd_text" type="textarea" :rows="3"
                          placeholder="可选，贴 JD 会按岗位要求补题" />
              </el-form-item>
              <el-form-item label="公司冲刺">
                <el-input v-model="form.company" placeholder="可选，如 美团：该公司面经出现过的题优先" />
              </el-form-item>
              <el-form-item label="题量">
                <el-input-number v-model="form.question_count" :min="3" :max="10" />
                <span class="hint" style="margin-left: 8px">题池不足会用 LLM 生成补足</span>
              </el-form-item>
            </el-form>
            <el-button type="primary" :loading="starting" style="width: 100%" @click="start">
              开始面试
            </el-button>
          </el-card>
        </el-col>
        <el-col :span="14">
          <el-card shadow="never">
            <template #header>历史场次</template>
            <el-empty v-if="!sessions.length" description="还没有面试记录" :image-size="60" />
            <el-table v-else :data="sessions" size="small" style="width: 100%">
              <el-table-column prop="id" label="场次" width="190" />
              <el-table-column prop="direction" label="方向" width="90" />
              <el-table-column prop="started_at" label="开始时间" width="160" />
              <el-table-column label="均分" width="90">
                <template #default="{ row }">
                  <el-tag v-if="row.overall" size="small"
                          :type="row.overall.overall_avg >= 7 ? 'success' : row.overall.overall_avg >= 5 ? 'warning' : 'danger'">
                    {{ row.overall.overall_avg }} / 10
                  </el-tag>
                  <span v-else class="hint">未交卷</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="100">
                <template #default="{ row }">
                  <el-button size="small" text type="primary" @click="openSession(row.id)">打开</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
      </el-row>
    </template>

    <!-- 进行中 / 已交卷 -->
    <template v-else>
      <el-card shadow="never" style="margin-bottom: 12px">
        <div class="bar">
          <span class="mono">{{ session.id }}</span>
          <el-tag size="small">{{ directionText(session.direction) }}</el-tag>
          <span class="hint">共 {{ session.questions.length }} 题 · 已答 {{ answeredCount }}</span>
          <el-progress :percentage="progress" :stroke-width="8" style="flex: 1; margin: 0 16px" />
          <el-button v-if="!session.finished_at" type="danger" plain size="small" :loading="finishing" @click="finish">
            交卷并生成报告
          </el-button>
          <el-tag v-else type="success" size="small">已交卷</el-tag>
        </div>
      </el-card>

      <!-- 报告 -->
      <el-card v-if="session.overall" shadow="never" style="margin-bottom: 12px">
        <template #header>
          <div class="card-head">
            <span>面试报告</span>
            <span class="mono small">{{ session.overall.report_path }}</span>
          </div>
        </template>
        <el-row :gutter="16">
          <el-col :span="7" class="center">
            <div class="big-score" :class="bandClass(session.overall.overall_avg)">
              {{ session.overall.overall_avg }}
            </div>
            <div class="hint">四维均分（满分 10）· 已答 {{ session.overall.answered }} / 跳过 {{ session.overall.skipped }}</div>
          </el-col>
          <el-col :span="17">
            <div v-for="(v, k) in session.overall.avg" :key="k" class="dim">
              <span class="dim-name">{{ dimText(k) }}</span>
              <el-progress :percentage="v * 10" :stroke-width="10"
                           :color="v >= 7 ? '#67c23a' : v >= 5 ? '#e6a23c' : '#f56c6c'" style="flex: 1" />
              <span class="dim-val">{{ v }}</span>
            </div>
            <div v-if="session.overall.report?.pass_estimate" class="pass">
              <b>通过评估：</b>{{ session.overall.report.pass_estimate }}
            </div>
          </el-col>
        </el-row>
        <el-divider />
        <p class="summary">{{ session.overall.report?.summary }}</p>
        <el-row :gutter="16">
          <el-col :span="8">
            <h4>亮点</h4>
            <ul><li v-for="(x, i) in session.overall.report?.strengths || []" :key="i">{{ x }}</li></ul>
          </el-col>
          <el-col :span="8">
            <h4>短板</h4>
            <ul class="weak"><li v-for="(x, i) in session.overall.report?.weaknesses || []" :key="i">{{ x }}</li></ul>
          </el-col>
          <el-col :span="8">
            <h4>改进建议</h4>
            <ul><li v-for="(x, i) in session.overall.report?.advice || []" :key="i">{{ x }}</li></ul>
          </el-col>
        </el-row>
        <el-alert v-if="session.overall.writeback" type="success" :closable="false" show-icon
                  style="margin-top: 8px"
                  :title="`已写回：错题 ${session.overall.writeback.mistakes ?? 0} 条 · 掌握度更新 ${session.overall.writeback.mastery_updates ?? 0} 张 · 补卡 ${session.overall.writeback.cards_created ?? 0} 张`" />
      </el-card>

      <!-- 逐题 -->
      <el-card shadow="never">
        <template #header>
          <div class="card-head">
            <span>第 {{ idx + 1 }} / {{ session.questions.length }} 题</span>
            <div>
              <el-button size="small" :disabled="idx === 0" @click="go(idx - 1)">上一题</el-button>
              <el-button size="small" :disabled="idx >= session.questions.length - 1" @click="go(idx + 1)">下一题</el-button>
            </div>
          </div>
        </template>

        <div class="q">
          <div class="q-text">{{ current.text }}</div>
          <div class="q-meta">
            <el-tag size="small" type="info">{{ current.qtype || '未标注' }}</el-tag>
            <el-tag size="small" type="info">{{ current.topic || '无主题' }}</el-tag>
            <el-tag size="small">题号 {{ current.qid }}</el-tag>
          </div>
        </div>

        <div v-if="!session.finished_at" class="answer">
          <el-input v-model="draft" type="textarea" :rows="8"
                    placeholder="像真面试一样说一遍再写下来：先给结论，再讲机制，最后讲权衡和你踩过的坑（Ctrl+Enter 提交）"
                    @keydown.ctrl.enter="submit" />
          <div class="answer-actions">
            <span class="hint">留空直接提交 = 跳过本题（不计分）</span>
            <el-button type="primary" :loading="evaluating" @click="submit">提交并评分</el-button>
          </div>
        </div>

        <div v-if="evaluation" class="eval">
          <el-row :gutter="10">
            <el-col :span="6" v-for="(v, k) in evaluation.scores" :key="k">
              <div class="score-box">
                <div class="score-val" :class="bandClass(v)">{{ v }}</div>
                <div class="hint">{{ dimText(k) }}</div>
              </div>
            </el-col>
          </el-row>
          <p class="feedback">{{ evaluation.feedback }}</p>
          <el-alert v-if="evaluation.followup" type="warning" :closable="false" show-icon
                    :title="`追问：${evaluation.followup}`" />
        </div>
        <el-alert v-else-if="session.finished_at" type="info" :closable="false" title="本题未作答（跳过）" />
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const sessions = ref([])
const session = ref(null)
const idx = ref(0)
const draft = ref('')
const evaluation = ref(null)
const starting = ref(false)
const evaluating = ref(false)
const finishing = ref(false)
const form = ref({ direction: 'bigdata', persona: '', jd_text: '', question_count: 5, company: '' })

const current = computed(() => session.value?.questions?.[idx.value] || {})
const answeredCount = computed(() => (session.value?.answers || []).filter(Boolean).length)
const progress = computed(() =>
  session.value ? Math.round((answeredCount.value / session.value.questions.length) * 100) : 0)

const directionText = (d) => ({ bigdata: '大数据', backend: '后端', agent: 'Agent' }[d] || d)
const dimText = (k) => ({ correctness: '正确性', depth: '深度', structure: '结构', highlight: '亮点' }[k] || k)
const bandClass = (v) => (v >= 7 ? 'good' : v >= 5 ? 'mid' : 'bad')

async function loadSessions() {
  sessions.value = (await api.get('/api/mock/sessions')) || []
}

async function start() {
  starting.value = true
  try {
    const res = await api.post('/api/mock/sessions', form.value)
    ElMessage.success(`组卷完成：${res.question_count} 题`)
    await openSession(res.session_id)
    loadSessions()
  } finally { starting.value = false }
}

async function openSession(id) {
  session.value = await api.get(`/api/mock/sessions/${id}`)
  idx.value = 0
  syncAnswer()
}

function syncAnswer() {
  const a = session.value?.answers?.[idx.value]
  draft.value = a?.answer_text || ''
  evaluation.value = a?.scores ? { scores: a.scores, feedback: a.feedback, followup: a.followups } : null
}

function go(i) {
  idx.value = i
  syncAnswer()
}

async function submit() {
  evaluating.value = true
  try {
    const res = await api.post(`/api/mock/sessions/${session.value.id}/answer`, {
      index: idx.value, answer_text: draft.value,
    })
    evaluation.value = res.evaluation
    session.value.answers[idx.value] = {
      answer_text: draft.value, scores: res.evaluation.scores,
      feedback: res.evaluation.feedback, followups: res.evaluation.followup,
    }
    const avg = Object.values(res.evaluation.scores).reduce((a, b) => a + b, 0) / 4
    ElMessage({ message: `本题均分 ${avg.toFixed(1)}`, type: avg >= 7 ? 'success' : avg >= 5 ? 'warning' : 'error' })
    if (idx.value < session.value.questions.length - 1) {
      setTimeout(() => go(idx.value + 1), 700)
    }
  } finally { evaluating.value = false }
}

async function finish() {
  const left = session.value.questions.length - answeredCount.value
  await ElMessageBox.confirm(
    left > 0 ? `还有 ${left} 题未作答（按跳过计 0 分），仍要交卷？` : '交卷后生成报告并写回错题本，继续？',
    '交卷', { confirmButtonText: '交卷', cancelButtonText: '再想想' })
  finishing.value = true
  try {
    session.value.overall = await api.post(`/api/mock/sessions/${session.value.id}/finish`)
    session.value.finished_at = new Date().toISOString()
    ElMessage.success('报告已生成')
    loadSessions()
  } finally { finishing.value = false }
}

function exitSession() {
  session.value = null
  draft.value = ''
  evaluation.value = null
  loadSessions()
}

onMounted(loadSessions)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.page-header h2 { margin: 0 0 4px; }
.sub { margin: 0; color: #909399; font-size: 13px; }
.hint { color: #909399; font-size: 12px; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
.small { color: #909399; }
.bar { display: flex; align-items: center; gap: 12px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.center { text-align: center; border-right: 1px dashed #ebeef5; }
.big-score { font-size: 44px; font-weight: 700; line-height: 1.1; }
.big-score.good { color: #67c23a; }
.big-score.mid { color: #e6a23c; }
.big-score.bad { color: #f56c6c; }
.dim { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.dim-name { width: 56px; font-size: 13px; color: #606266; }
.dim-val { width: 32px; text-align: right; font-size: 13px; }
.pass { margin-top: 8px; font-size: 13px; color: #606266; }
.summary { font-size: 13px; line-height: 1.8; background: #f5f7fa; padding: 10px 12px; border-radius: 6px; }
.weak li { color: #e6a23c; }
.q { margin-bottom: 12px; }
.q-text { font-size: 16px; line-height: 1.7; font-weight: 500; margin-bottom: 8px; }
.q-meta { display: flex; gap: 8px; }
.answer-actions { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; }
.eval { margin-top: 16px; border-top: 1px dashed #ebeef5; padding-top: 12px; }
.score-box { text-align: center; background: #f5f7fa; border-radius: 8px; padding: 10px 0; }
.score-val { font-size: 22px; font-weight: 700; }
.score-val.good { color: #67c23a; }
.score-val.mid { color: #e6a23c; }
.score-val.bad { color: #f56c6c; }
.feedback { font-size: 13px; line-height: 1.8; margin: 12px 0; color: #303133; }
h4 { margin: 10px 0 6px; font-size: 13px; }
ul { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.8; }
</style>
