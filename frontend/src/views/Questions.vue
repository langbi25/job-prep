<template>
  <div class="page">
    <div class="page-header">
      <h2>题目池 · 全部面试题</h2>
      <div>
        <el-tag type="info" style="margin-right: 8px">共 {{ total }} 道 · 无卡 {{ uncardedCount }} 道</el-tag>
        <el-button :icon="Refresh" circle @click="load" />
      </div>
    </div>

    <el-card shadow="never" style="margin-bottom: 12px">
      <div class="toolbar">
        <el-input v-model="filters.q" placeholder="搜题目（全文检索，如 数据倾斜 / Agent 记忆）"
                  clearable style="width: 320px" @keyup.enter="load" @clear="load">
          <template #append><el-button :icon="Search" @click="load" /></template>
        </el-input>
        <el-select v-model="filters.qtype" placeholder="题型" clearable style="width: 130px" @change="load">
          <el-option v-for="t in qtypes" :key="t" :label="t" :value="t" />
        </el-select>
        <el-select v-model="filters.topic" placeholder="主题" clearable filterable style="width: 180px" @change="load">
          <el-option v-for="t in topics" :key="t" :label="t" :value="t" />
        </el-select>
        <el-checkbox v-model="onlyUncarded">只看无卡</el-checkbox>
        <el-checkbox v-model="onlyFreq">只看高频(≥2)</el-checkbox>
        <el-divider direction="vertical" />
        <el-select v-model="aiCount" style="width: 110px" size="small">
          <el-option :value="1" label="AI 建 1 张" />
          <el-option :value="3" label="AI 建 3 张" />
          <el-option :value="5" label="AI 建 5 张" />
        </el-select>
        <el-button type="primary" size="small" :loading="aiBusy" @click="aiBatch">
          补齐高频无卡题
        </el-button>
        <span class="hint">AI 只出草稿，去审批中心点头后才入库</span>
      </div>
    </el-card>

    <el-table :data="shown" v-loading="loading" size="small" style="width: 100%" row-key="id">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="expand">
            <div><b>题号</b>：{{ row.id }}　<b>别名</b>：{{ row.aliases?.length ? row.aliases.join(' / ') : '无' }}</div>
            <div v-if="row.card"><b>命中卡片</b>：{{ row.card.title }}（掌握度 {{ row.card.mastery }}，相似度 {{ row.card.similarity }}）</div>
            <div v-else><b>覆盖状态</b>：无卡 —— 建卡后在作战地图重新分析即可看到覆盖变化</div>
            <div style="margin-top: 8px">
              <el-button size="small" @click="copy(row.text)">复制题目</el-button>
              <el-button size="small" type="primary" @click="openCreate(row)">建卡</el-button>
              <el-button size="small" type="warning" plain @click="toPlan(row)">生成改造方案</el-button>
              <el-button size="small" @click="router.push('/coverage')">去作战地图</el-button>
            </div>
            <div class="hint" style="margin-top: 6px">
              改造方案 = 针对你当前项目的一次性落地计划：做完后回灌成实战卡，这张卡就有了"我真做过"的深度。
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="id" label="题号" width="90" />
      <el-table-column prop="text" label="题目" min-width="330" show-overflow-tooltip />
      <el-table-column prop="qtype" label="题型" width="110">
        <template #default="{ row }"><el-tag size="small" type="info">{{ row.qtype || '未标注' }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="topic" label="主题" width="140" show-overflow-tooltip />
      <el-table-column label="出现" width="80" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="row.freq_count >= 2 ? 'danger' : 'info'">×{{ row.freq_count }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="面经数" width="80" align="center">
        <template #default="{ row }">{{ row.interview_count }}</template>
      </el-table-column>
      <el-table-column label="覆盖" width="150">
        <template #default="{ row }">
          <el-tag v-if="row.card" size="small" :type="row.card.mastery >= 3 ? 'success' : 'warning'">
            有卡·掌握{{ row.card.mastery }}
          </el-tag>
          <el-tag v-else size="small" type="danger" effect="plain">无卡</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="last_seen_at" label="最近出现" width="110" />
      <el-table-column label="操作" width="210" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click.stop="aiDraft(row)">AI 建卡</el-button>
          <el-button size="small" text @click.stop="openCreate(row)">手写</el-button>
          <el-button size="small" text type="warning" @click.stop="toPlan(row)">改造方案</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" title="建卡 · 把这道题变成可复习的知识卡" width="760px" top="7vh">
      <el-form label-width="70px" size="small">
        <el-form-item label="题目">
          <el-input v-model="form.title" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="领域/主题">
          <el-select v-model="form.domain" style="width: 140px">
            <el-option label="后端" value="backend" />
            <el-option label="大数据" value="bigdata" />
            <el-option label="Agent" value="agent" />
          </el-select>
          <el-input v-model="form.topic" placeholder="主题，如 数据倾斜" style="width: 200px; margin-left: 8px" />
        </el-form-item>
        <el-form-item label="答题要点">
          <el-input v-model="pointsText" type="textarea" :rows="8"
                    placeholder="一行一条要点（这是评分与复习时的参考答案骨架）" />
        </el-form-item>
        <el-form-item label="常见坑">
          <el-input v-model="pitfallsText" type="textarea" :rows="3" placeholder="一行一条坑点（可选）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!form.title.trim() || !pointsText.trim()"
                   @click="save">保存并入库</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const router = useRouter()
const rows = ref([])
const loading = ref(false)
const onlyUncarded = ref(false)
const onlyFreq = ref(false)
const filters = ref({ q: '', qtype: '', topic: '' })

const dialog = ref(false)
const saving = ref(false)
const aiBusy = ref(false)
const aiCount = ref(3)
const form = ref({ title: '', domain: 'backend', topic: '', question: '' })
const pointsText = ref('')
const pitfallsText = ref('')

const topics = computed(() => [...new Set(rows.value.map((r) => r.topic).filter(Boolean))])
const qtypes = computed(() => [...new Set(rows.value.map((r) => r.qtype).filter(Boolean))])
const uncardedCount = computed(() => rows.value.filter((r) => !r.card).length)
const total = computed(() => rows.value.length)
const shown = computed(() =>
  rows.value.filter((r) => (!onlyUncarded.value || !r.card) && (!onlyFreq.value || r.freq_count >= 2))
)

async function load() {
  loading.value = true
  try {
    rows.value = await api.get('/api/questions', {
      params: {
        q: filters.value.q || undefined,
        qtype: filters.value.qtype || undefined,
        topic: filters.value.topic || undefined,
        with_coverage: true,
        limit: 500,
      },
    })
  } finally { loading.value = false }
}

function toPlan(row) {
  router.push({ path: '/plans', query: { question_id: row.id } })
}

function openCreate(row) {
  form.value = {
    title: row.text, domain: 'backend', topic: row.topic || '', question: row.text,
    question_id: row.id,          // 带上题号，建完卡这道题立刻不再是"无卡"
  }
  pointsText.value = ''
  pitfallsText.value = ''
  dialog.value = true
}

async function save() {
  saving.value = true
  try {
    const points = pointsText.value.split('\n').map((s) => s.trim()).filter(Boolean)
    const pitfalls = pitfallsText.value.split('\n').map((s) => s.trim()).filter(Boolean)
    await api.post('/api/cards', { ...form.value, points, pitfalls, tags: [] })
    ElMessage.success('卡片已建，这道题的覆盖状态已刷新（去知识库能编辑要点）')
    dialog.value = false
    load()
  } finally { saving.value = false }
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch { ElMessage.warning('复制失败，请手动选择') }
}

async function aiDraft(row) {
  aiBusy.value = true
  try {
    const res = await api.post('/api/agent/cards/draft', { question_ids: [row.id], limit: 1 })
    if (!res.drafted?.length) {
      ElMessage.warning(res.skipped || res.failed?.[0]?.error || '没有生成草稿')
      return
    }
    ElMessageBox.confirm(
      `已生成草稿：${res.drafted[0].title}（${res.drafted[0].points} 条要点）。现在去审批中心确认？`,
      'AI 建卡', { confirmButtonText: '去审批', cancelButtonText: '稍后' }
    ).then(() => router.push('/approvals')).catch(() => {})
  } finally { aiBusy.value = false }
}

async function aiBatch() {
  aiBusy.value = true
  try {
    const res = await api.post('/api/agent/cards/draft', { limit: aiCount.value, only_uncarded: true })
    if (!res.drafted?.length) {
      ElMessage.warning(res.skipped || '没有可建卡的题')
      return
    }
    const names = res.drafted.map((d) => d.title.slice(0, 24)).join('；')
    ElMessageBox.confirm(`已生成 ${res.drafted.length} 张草稿：${names}。去审批中心逐条确认？`,
      'AI 批量建卡', { confirmButtonText: '去审批', cancelButtonText: '稍后' })
      .then(() => router.push('/approvals')).catch(() => {})
  } finally { aiBusy.value = false }
}

onMounted(load)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.hint { color: #909399; font-size: 12px; }
.expand { padding: 8px 20px; font-size: 13px; line-height: 1.8; color: #606266; }
</style>
