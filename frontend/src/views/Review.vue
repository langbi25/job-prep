<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>复习中心</h2>
        <p class="sub">先自己口述一遍，再翻答案自评；记得 +0.5 掌握度，忘了 -0.5 并明天重来</p>
      </div>
      <div>
        <el-tag v-if="stats.done" type="success" style="margin-right: 8px">
          本轮 {{ stats.pass }} 过 / {{ stats.fail }} 未过
        </el-tag>
        <el-button :icon="Refresh" circle @click="load" />
      </div>
    </div>

    <el-row :gutter="12" style="margin-bottom: 12px">
      <el-col :span="6" v-for="m in metrics" :key="m.label">
        <el-card shadow="hover" class="stat">
          <div class="stat-value" :style="{ color: m.color }">{{ m.value }}</div>
          <div class="stat-label">{{ m.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="12">
      <el-col :span="15">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span>今日复习</span>
              <span class="hint" v-if="queue.length">
                {{ Math.min(pos + 1, queue.length) }} / {{ queue.length }}
              </span>
            </div>
          </template>

          <el-empty v-if="!queue.length" description="今天没有到期卡片，去投喂面经或直接建卡" :image-size="70" />
          <template v-else-if="pos < queue.length">
            <div class="q-top">
              <el-tag size="small" type="info">{{ cur.topic }}</el-tag>
              <el-tag size="small" :type="cur.mastery >= 3 ? 'success' : cur.mastery >= 1.5 ? 'warning' : 'danger'">
                掌握度 {{ cur.mastery }}
              </el-tag>
              <span class="hint">复习 {{ cur.review_count }} 次 · 到期 {{ cur.srs_due || '新卡' }}</span>
            </div>
            <div class="q-title">{{ cur.title }}</div>
            <div v-if="cur.question && cur.question !== cur.title" class="q-text">{{ cur.question }}</div>

            <div v-if="!revealed" class="ask">
              <p class="hint">先在心里（或出声）答一遍：结论 → 机制 → 工程细节 → 权衡。想好再翻答案。</p>
              <el-button type="primary" :loading="revealing" @click="reveal">显示答案</el-button>
            </div>

            <div v-else class="ans">
              <h4>要点</h4>
              <ol class="points"><li v-for="(p, i) in detail.points || []" :key="i">{{ p }}</li></ol>
              <template v-if="detail.pitfalls?.length">
                <h4>易错</h4>
                <ul class="pitfalls"><li v-for="(p, i) in detail.pitfalls" :key="i">{{ p }}</li></ul>
              </template>
              <el-alert v-if="lastResult" type="success" :closable="false" show-icon style="margin: 12px 0"
                        :title="`已记录：掌握度 ${lastResult.mastery} · 下次复习 ${lastResult.next_due}（${lastResult.interval_days} 天后）`" />
              <div class="grade">
                <span class="hint">对照要点自评：答齐主干了吗？</span>
                <div>
                  <el-button type="danger" plain :loading="grading" @click="grade('fail')">没记住</el-button>
                  <el-button type="success" :loading="grading" @click="grade('pass')">记得</el-button>
                </div>
              </div>
            </div>
          </template>

          <div v-else class="done">
            <h3>本轮结束</h3>
            <p>过了 {{ stats.pass }} 张，未过 {{ stats.fail }} 张。
              <template v-if="stats.fail">未过的卡明天会重新到期，别跳过。</template>
            </p>
            <el-button @click="load">再看看还有没有到期卡</el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :span="9">
        <el-card shadow="never" style="margin-bottom: 12px">
          <template #header>
            <div class="card-head">
              <span>未解决错题（{{ mistakes.length }}）</span>
              <el-button size="small" text @click="loadMistakes">刷新</el-button>
            </div>
          </template>
          <el-empty v-if="!mistakes.length" description="没有未解决错题，保持住" :image-size="50" />
          <div v-for="m in mistakes" :key="m.id" class="mistake">
            <div class="m-text">{{ m.raw_text }}</div>
            <div class="m-meta">
              <el-tag v-if="m.topic" size="small" type="info">{{ m.topic }}</el-tag>
              <el-tag v-if="m.cause_type" size="small" type="warning">{{ m.cause_type }}</el-tag>
              <span class="hint">{{ (m.created_at || '').slice(0, 10) }}</span>
              <el-button size="small" text type="success" @click="resolve(m)">已解决</el-button>
            </div>
            <div v-if="m.feedback" class="hint m-fb">{{ m.feedback }}</div>
          </div>
        </el-card>

        <el-card shadow="never">
          <template #header>薄弱卡片</template>
          <el-empty v-if="!weakCards.length" description="尚无数据（需要复习或错题记录）" :image-size="50" />
          <div v-for="w in weakCards" :key="w.id" class="weak-row">
            <span class="weak-title">{{ w.title }}</span>
            <el-tag size="small" type="info">{{ w.topic }}</el-tag>
            <el-tag size="small" type="danger" effect="plain">掌握 {{ w.mastery }}</el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const daily = ref({})
const queue = ref([])
const pos = ref(0)
const revealed = ref(false)
const revealing = ref(false)
const grading = ref(false)
const detail = ref({})
const lastResult = ref(null)
const mistakes = ref([])
const weakCards = ref([])
const stats = ref({ pass: 0, fail: 0, done: false })

const cur = computed(() => queue.value[pos.value] || {})
const metrics = computed(() => [
  { label: '今日到期', value: daily.value.due_cards ?? 0, color: '#409eff' },
  { label: '未解决错题', value: daily.value.open_mistakes ?? 0, color: '#f56c6c' },
  { label: '待处理采集', value: daily.value.pending_ingests ?? 0, color: '#e6a23c' },
  {
    label: '近期模拟均分',
    value: (daily.value.recent_mock_avgs || []).length
      ? (daily.value.recent_mock_avgs.reduce((a, b) => a + (b || 0), 0) / daily.value.recent_mock_avgs.length).toFixed(1)
      : '—',
    color: '#67c23a',
  },
])

async function load() {
  daily.value = await api.get('/api/daily')
  queue.value = (await api.get('/api/srs/due', { params: { limit: 30 } })) || []
  // daily.weak 是 {recent_mistakes, weak_cards} 结构，不是数组
  weakCards.value = daily.value.weak?.weak_cards || []
  pos.value = 0
  revealed.value = false
  detail.value = {}
  lastResult.value = null
  stats.value = { pass: 0, fail: 0, done: false }
  loadMistakes()
}

async function reveal() {
  revealing.value = true
  try {
    detail.value = await api.get(`/api/cards/${cur.value.id}`)
    revealed.value = true
  } finally { revealing.value = false }
}

async function grade(result) {
  grading.value = true
  try {
    lastResult.value = await api.post('/api/srs/review', { card_id: cur.value.id, result })
    stats.value[result === 'pass' ? 'pass' : 'fail'] += 1
    setTimeout(() => {
      revealed.value = false
      detail.value = {}
      lastResult.value = null
      if (pos.value < queue.value.length - 1) pos.value += 1
      else { pos.value = queue.value.length; stats.value.done = true }
    }, 900)
  } finally { grading.value = false }
}

async function loadMistakes() {
  mistakes.value = (await api.get('/api/mistakes', { params: { only_open: true, limit: 20 } })) || []
}

async function resolve(m) {
  await api.post(`/api/mistakes/${m.id}/resolve`)
  ElMessage.success('已标记解决')
  loadMistakes()
  daily.value = await api.get('/api/daily')
}

onMounted(load)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.page-header h2 { margin: 0 0 4px; }
.sub { margin: 0; color: #909399; font-size: 13px; }
.stat { text-align: center; }
.stat-value { font-size: 26px; font-weight: 700; }
.stat-label { color: #606266; font-size: 12px; margin-top: 4px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.hint { color: #909399; font-size: 12px; }
.q-top { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.q-title { font-size: 17px; font-weight: 500; line-height: 1.6; }
.q-text { margin-top: 6px; font-size: 13px; color: #606266; line-height: 1.7; }
.ask { margin-top: 18px; padding-top: 14px; border-top: 1px dashed #ebeef5; }
.ans { margin-top: 16px; padding-top: 14px; border-top: 1px dashed #ebeef5; }
.points { margin: 0; padding-left: 20px; line-height: 1.9; font-size: 13px; }
.pitfalls { margin: 0; padding-left: 20px; line-height: 1.9; font-size: 13px; color: #e6a23c; }
.grade { display: flex; justify-content: space-between; align-items: center; margin-top: 16px; }
.done { text-align: center; padding: 20px 0; }
.done h3 { margin: 0 0 8px; }
.mistake { padding: 8px 0; border-bottom: 1px dashed #ebeef5; }
.mistake:last-child { border-bottom: none; }
.m-text { font-size: 13px; line-height: 1.6; }
.m-meta { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
.m-fb { margin-top: 4px; }
.weak-row { display: flex; align-items: center; gap: 6px; padding: 6px 0; border-bottom: 1px dashed #ebeef5; font-size: 13px; }
.weak-row:last-child { border-bottom: none; }
.weak-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
h4 { margin: 12px 0 6px; font-size: 13px; }
</style>
