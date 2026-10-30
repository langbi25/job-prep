<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>项目进化环 · 改造方案</h2>
        <p class="sub">
          题目 → 贴着本项目的一次性改造方案 → 用 WorkBuddy 真做 → 复盘文档回灌成实战卡 → 原子卡变厚
        </p>
      </div>
      <div>
        <el-tag type="info" style="margin-right: 8px">方案 {{ plans.length }} 份 · 待处理 {{ pendingCount }}</el-tag>
        <el-button :icon="Refresh" circle @click="loadAll" />
      </div>
    </div>

    <el-row :gutter="12">
      <el-col :span="9">
        <el-card shadow="never" class="left-card">
          <template #header>
            <div class="card-head">
              <span>项目档案</span>
              <el-button size="small" type="primary" :loading="savingProfile" @click="saveProfile">保存</el-button>
            </div>
          </template>
          <p class="hint">
            方案是"针对你的项目"的，所以必须知道你的项目长什么样：技术栈、模块、数据量、现在缺什么、
            部署约束。档案越具体，方案越不像通用模版。
          </p>
          <el-input v-model="profile" type="textarea" :rows="18" class="mono"
                    placeholder="例如：Python FastAPI + SQLite ... 现状 ... 痛点 ... 约束 ..." />
          <div class="hint">{{ profile.length }} 字 · 存于 data/project_profile.md</div>
        </el-card>
      </el-col>

      <el-col :span="15">
        <el-card shadow="never" style="margin-bottom: 12px">
          <template #header><span>生成新方案</span></template>
          <div class="gen">
            <el-select v-model="gen.question_id" filterable remote clearable
                       :remote-method="searchQuestions" :loading="searching"
                       placeholder="搜题目（如 工具链 / Skill / 记忆）" style="flex: 1">
              <el-option v-for="q in candidates" :key="q.id" :label="q.text" :value="q.id">
                <span>{{ q.text.slice(0, 46) }}</span>
                <span class="opt-meta">{{ q.topic || '未标注' }} · ×{{ q.freq_count }}</span>
              </el-option>
            </el-select>
            <el-input v-model="gen.focus" placeholder="侧重点（可选），如：优先低依赖改造" style="width: 220px" />
            <el-button type="primary" :loading="drafting" :disabled="!gen.question_id" @click="draft">
              生成改造方案
            </el-button>
          </div>
          <div class="hint">
            生成约 20–40 秒。方案会落到 <code>docs/plans/</code>，可直接拿去实施；
            方案本身不进知识库（它是本项目的一次性计划，不是通用答案）。
          </div>
        </el-card>

        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span>方案列表</span>
              <el-radio-group v-model="statusFilter" size="small" @change="loadPlans">
                <el-radio-button value="">全部</el-radio-button>
                <el-radio-button value="pending">待处理</el-radio-button>
                <el-radio-button value="adopted">已采纳</el-radio-button>
              </el-radio-group>
            </div>
          </template>
          <el-table :data="plans" v-loading="loadingPlans" size="small" style="width: 100%">
            <el-table-column prop="id" label="方案 ID" width="210" show-overflow-tooltip />
            <el-table-column label="对应题目" min-width="240" show-overflow-tooltip>
              <template #default="{ row }">{{ row.payload?.question }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.status === 'adopted' ? 'success' : 'warning'">
                  {{ row.status === 'adopted' ? '已采纳' : '待处理' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="原卡" width="120" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.card_id" class="mono small">{{ row.card_id }}</span>
                <el-tag v-else size="small" type="info" effect="plain">无卡</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="生成时间" width="150" />
            <el-table-column label="操作" width="190" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click="openDetail(row.id)">详情</el-button>
                <el-button v-if="row.status === 'pending'" size="small" text @click="adopt(row)">采纳</el-button>
                <el-button size="small" text type="success" @click="openFeedback(row)">回灌</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <!-- 方案详情 -->
    <el-drawer v-model="detailOpen" :title="detail?.id || '方案详情'" size="52%" top="4vh">
      <div v-if="detail" class="detail">
        <div class="detail-top">
          <el-tag size="small" :type="detail.status === 'adopted' ? 'success' : 'warning'">
            {{ detail.status === 'adopted' ? '已采纳' : '待处理' }}
          </el-tag>
          <span class="mono small">{{ detail.doc_path }}</span>
          <div style="margin-left: auto">
            <el-button size="small" @click="copyDoc">复制全文</el-button>
            <el-button size="small" @click="downloadDoc">下载 .md</el-button>
            <el-button size="small" type="success" @click="openFeedback(detail)">回灌实战经验</el-button>
          </div>
        </div>

        <h4>目标</h4>
        <p>{{ detail.payload?.goal }}</p>

        <h4>项目现状推断</h4>
        <ul><li v-for="(x, i) in detail.payload?.current_state || []" :key="i">{{ x }}</li></ul>

        <h4>实施步骤</h4>
        <el-steps direction="vertical" :active="99" space="96px">
          <el-step v-for="(s, i) in detail.payload?.steps || []" :key="i" :title="s.title">
            <template #description>
              <div class="step-body">
                <div><b>改什么</b>：{{ s.change }}</div>
                <div v-if="s.why"><b>为什么</b>：{{ s.why }}</div>
                <div class="hint">工作量 {{ s.effort || '—' }}｜风险 {{ s.risk || '—' }}</div>
              </div>
            </template>
          </el-step>
        </el-steps>

        <h4>验收标准</h4>
        <ul><li v-for="(x, i) in detail.payload?.acceptance || []" :key="i">{{ x }}</li></ul>

        <h4>可量化指标</h4>
        <ul><li v-for="(x, i) in detail.payload?.metrics || []" :key="i">{{ x }}</li></ul>

        <h4>简历 bullet</h4>
        <ul><li v-for="(x, i) in detail.payload?.resume_bullets || []" :key="i">{{ x }}</li></ul>

        <h4>面试叙述</h4>
        <p class="narrative">{{ detail.payload?.interview_narrative }}</p>

        <h4>取舍与风险</h4>
        <ul><li v-for="(x, i) in detail.payload?.tradeoffs || []" :key="i">{{ x }}</li></ul>

        <h4>复盘文档大纲</h4>
        <ol><li v-for="(o, i) in detail.payload?.doc_outline || []" :key="i">
          <b>{{ o.chapter }}</b> —— {{ o.what }}
        </li></ol>

        <el-divider />
        <h4>方案文档（可编辑后直接拿去实施）</h4>
        <pre class="doc">{{ detail.doc_text }}</pre>
      </div>
    </el-drawer>

    <!-- 回灌 -->
    <el-dialog v-model="fbOpen" title="回灌实战经验 · 生成实战卡草稿" width="720px" top="8vh">
      <p class="hint">
        做完之后，把真实发生的事写在这里：改了什么、踩了什么坑、指标从多少到多少。
        太笼统会被打回——AI 需要具体细节才能写出"我做过"的卡。
      </p>
      <div class="hint">对应题目：{{ fbPlan?.payload?.question }}</div>
      <el-input v-model="notes" type="textarea" :rows="10" style="margin-top: 8px"
                placeholder="例：把 6 个脚本封装成 tools/ 注册表，JSON Schema 校验参数；中间遇到 MCP stdio 偶发断连，加了重连；胶水代码从 200 行降到 0，新增工具只需 1 个 yaml..." />
      <div class="hint">{{ notes.length }} 字（至少 30 字）</div>
      <template #footer>
        <el-button @click="fbOpen = false">取消</el-button>
        <el-button type="primary" :loading="fbBusy" :disabled="notes.trim().length < 30"
                   @click="submitFeedback">生成实战卡草稿</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const route = useRoute()
const router = useRouter()

const profile = ref('')
const savingProfile = ref(false)
const plans = ref([])
const loadingPlans = ref(false)
const statusFilter = ref('')

const candidates = ref([])
const searching = ref(false)
const gen = ref({ question_id: '', focus: '' })
const drafting = ref(false)

const detailOpen = ref(false)
const detail = ref(null)

const fbOpen = ref(false)
const fbPlan = ref(null)
const notes = ref('')
const fbBusy = ref(false)

const pendingCount = computed(() => plans.value.filter((p) => p.status === 'pending').length)

async function loadProfile() {
  const res = await api.get('/api/profile')
  profile.value = res.profile || ''
}

async function saveProfile() {
  savingProfile.value = true
  try {
    const res = await api.put('/api/profile', { profile: profile.value })
    ElMessage.success(`项目档案已保存（${res.chars} 字）`)
  } finally { savingProfile.value = false }
}

async function loadPlans() {
  loadingPlans.value = true
  try {
    plans.value = await api.get('/api/plans', { params: { status: statusFilter.value || undefined } })
  } finally { loadingPlans.value = false }
}

async function searchQuestions(kw) {
  searching.value = true
  try {
    candidates.value = await api.get('/api/questions', { params: { q: kw || undefined, limit: 30 } })
  } finally { searching.value = false }
}

async function draft() {
  drafting.value = true
  try {
    const res = await api.post('/api/plans/draft', {
      question_id: gen.value.question_id, focus: gen.value.focus || '',
    })
    ElMessage.success(`方案已生成：${res.doc_path}`)
    await loadPlans()
    openDetail(res.id)
  } finally { drafting.value = false }
}

async function openDetail(id) {
  detail.value = await api.get(`/api/plans/${id}`)
  detailOpen.value = true
}

async function adopt(row) {
  await api.post(`/api/plans/${row.id}/adopt`)
  ElMessage.success('已采纳，去实施吧')
  loadPlans()
}

function openFeedback(row) {
  // 列表行与详情行都带 id + payload（question 在 payload 里），结构一致
  fbPlan.value = row
  notes.value = ''
  fbOpen.value = true
}

async function submitFeedback() {
  fbBusy.value = true
  try {
    const res = await api.post(`/api/plans/${fbPlan.value.id}/experience`, { notes: notes.value })
    fbOpen.value = false
    ElMessageBox.confirm(
      `已生成实战卡草稿《${res.title}》（${res.points} 条要点）` +
      (res.link_card_id ? `，将自动与原卡 ${res.link_card_id} 互链。` : '。') + '去审批中心确认入库？',
      '回灌成功', { confirmButtonText: '去审批', cancelButtonText: '稍后' }
    ).then(() => router.push('/approvals')).catch(() => {})
  } finally { fbBusy.value = false }
}

async function copyDoc() {
  try {
    await navigator.clipboard.writeText(detail.value.doc_text || '')
    ElMessage.success('方案全文已复制，可直接粘给 WorkBuddy')
  } catch { ElMessage.warning('复制失败，请用下载') }
}

function downloadDoc() {
  const blob = new Blob([detail.value.doc_text || ''], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${detail.value.id}.md`
  a.click()
  URL.revokeObjectURL(a.href)
}

async function loadAll() {
  await Promise.all([loadProfile(), loadPlans()])
}

onMounted(async () => {
  await loadAll()
  if (route.query.question_id) {
    gen.value.question_id = String(route.query.question_id)
    await searchQuestions('')
    const hit = candidates.value.find((c) => c.id === gen.value.question_id)
    if (hit) candidates.value = [hit, ...candidates.value.filter((c) => c.id !== hit.id)]
  }
})
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.page-header h2 { margin: 0 0 4px; }
.sub { margin: 0; color: #909399; font-size: 13px; }
.left-card { height: 100%; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.hint { color: #909399; font-size: 12px; line-height: 1.7; }
.mono :deep(textarea) { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
.mono.small { font-size: 12px; color: #606266; }
.gen { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.opt-meta { float: right; color: #909399; font-size: 12px; margin-left: 12px; }
.detail { font-size: 13px; line-height: 1.8; color: #303133; }
.detail h4 { margin: 18px 0 6px; font-size: 14px; color: #1d2530; }
.detail ul, .detail ol { margin: 0; padding-left: 20px; }
.detail-top { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.step-body { font-size: 12px; line-height: 1.7; color: #606266; }
.narrative { background: #f5f7fa; padding: 10px 12px; border-radius: 6px; white-space: pre-wrap; }
.doc { background: #1d2530; color: #d7dee8; padding: 14px; border-radius: 8px;
       font-size: 12px; line-height: 1.7; white-space: pre-wrap; word-break: break-word; max-height: 420px; overflow: auto; }
</style>
