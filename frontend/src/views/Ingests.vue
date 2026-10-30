<template>
  <div class="page">
    <div class="page-header">
      <h2>采集中心 · 面经投喂</h2>
      <el-button :icon="Refresh" circle @click="load" />
    </div>

    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>投喂新面经（粘贴文本）</template>
      <el-input v-model="text" type="textarea" :rows="6" placeholder="粘贴面经原文（公司/轮次/题目列表），越完整提取质量越高" />
      <div style="margin-top: 12px">
        <el-button type="primary" :loading="creating" :disabled="!text.trim()" @click="create">投喂</el-button>
        <span style="color:#909399;font-size:12px;margin-left:12px">投喂 → 提取（LLM 结构化）→ 质检确认（可取消勾选题目）→ 入池</span>
      </div>
    </el-card>

    <el-tabs v-model="tab" @tab-change="load">
      <el-tab-pane label="待处理" name="pending" />
      <el-tab-pane label="已提取" name="extracted" />
      <el-tab-pane label="已确认" name="confirmed" />
      <el-tab-pane label="已丢弃" name="discarded" />
      <el-tab-pane label="全部" name="all" />
    </el-tabs>

    <el-table :data="rows" v-loading="loading" style="width: 100%">
      <el-table-column prop="id" label="ID" width="170" />
      <el-table-column prop="source_type" label="来源" width="80" />
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="抽取结果" width="150">
        <template #default="{ row }">
          <span v-if="row.extracted?.questions?.length">
            {{ row.extracted.questions.length }} 题 ·
            {{ row.extracted.direction || '方向未知' }}
            <span v-if="row.extracted.company">· {{ row.extracted.company }}</span>
          </span>
          <span v-else style="color:#c0c4cc">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="时间" width="170" />
      <el-table-column prop="error" label="备注" show-overflow-tooltip />
      <el-table-column label="操作" width="240" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :disabled="row.status !== 'pending'" :loading="busy === row.id"
                     @click="extract(row.id)">提取</el-button>
          <el-button size="small" type="success" :disabled="row.status !== 'extracted'" @click="openConfirm(row)">质检确认</el-button>
          <el-button size="small" type="danger" plain :disabled="row.status === 'discarded' || row.status === 'confirmed'"
                     @click="discard(row.id)">丢弃</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 人工质检：确认入库前可逐条取消勾选、修正元信息 -->
    <el-dialog v-model="dialog" title="质检确认 · 入库前最后一道关" width="820px" top="6vh">
      <template v-if="current">
        <el-form :inline="true" label-width="70px" size="small" style="margin-bottom: 12px">
          <el-form-item label="公司">
            <el-input v-model="meta.company" placeholder="如 美团" style="width: 150px" />
          </el-form-item>
          <el-form-item label="岗位">
            <el-input v-model="meta.position" placeholder="如 大数据开发" style="width: 170px" />
          </el-form-item>
          <el-form-item label="方向">
            <el-select v-model="meta.direction" style="width: 120px">
              <el-option label="后端" value="backend" />
              <el-option label="大数据" value="bigdata" />
              <el-option label="Agent" value="agent" />
            </el-select>
          </el-form-item>
          <el-form-item label="轮次">
            <el-input v-model="meta.round" placeholder="一面/二面" style="width: 110px" />
          </el-form-item>
        </el-form>

        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-weight:600">抽取题目 {{ qlist.length }} 道，将入库 {{ keptCount }} 道</span>
          <el-button size="small" text type="primary" @click="toggleAll(true)">全选</el-button>
          <el-button size="small" text @click="toggleAll(false)">全不选</el-button>
        </div>

        <el-table :data="qlist" max-height="360" size="small">
          <el-table-column width="50" align="center">
            <template #default="{ row }">
              <el-checkbox v-model="row.keep" />
            </template>
          </el-table-column>
          <el-table-column type="index" width="44" />
          <el-table-column prop="text" label="题目" min-width="380" show-overflow-tooltip />
          <el-table-column prop="qtype" label="题型" width="100" />
          <el-table-column prop="topic" label="主题" width="130" show-overflow-tooltip />
        </el-table>
        <el-alert type="info" :closable="false" show-icon style="margin-top: 12px"
                  title="取消勾选的题目不会入题目池；入库后可在「作战地图」生成覆盖分析。" />
      </template>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="confirming" :disabled="!keptCount" @click="doConfirm">
          确认入库（{{ keptCount }} 题）
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const rows = ref([])
const text = ref('')
const tab = ref('all')
const loading = ref(false)
const creating = ref(false)
const busy = ref('')

const dialog = ref(false)
const confirming = ref(false)
const current = ref(null)
const qlist = ref([])
const meta = ref({ company: '', position: '', direction: '', round: '', interview_date: '' })

const keptCount = computed(() => qlist.value.filter((q) => q.keep).length)

const statusType = (s) => ({ pending: 'warning', extracted: 'primary', confirmed: 'success', discarded: 'info', error: 'danger' }[s] || 'info')
const statusText = (s) => ({ pending: '待提取', extracted: '待确认', confirmed: '已入库', discarded: '已丢弃', error: '出错' }[s] || s)

async function load() {
  loading.value = true
  try {
    rows.value = await api.get('/api/ingests', { params: tab.value === 'all' ? {} : { status: tab.value } })
  } finally { loading.value = false }
}

async function create() {
  creating.value = true
  try {
    await api.post('/api/ingests', { source_type: 'paste', text: text.value })
    ElMessage.success('投喂成功，接着点「提取」')
    text.value = ''
    tab.value = 'pending'
    load()
  } finally { creating.value = false }
}

async function extract(id) {
  busy.value = id
  try {
    await api.post(`/api/ingests/${id}/extract`)
    ElMessage.success('提取完成，请质检确认')
    await load()
    const row = rows.value.find((r) => r.id === id)
    if (row) openConfirm(row)
  } finally { busy.value = '' }
}

function openConfirm(row) {
  current.value = row
  const ex = row.extracted || {}
  meta.value = {
    company: ex.company || '',
    position: ex.position || '',
    direction: ex.direction || 'bigdata',
    round: ex.round || '',
    interview_date: ex.interview_date || '',
  }
  qlist.value = (ex.questions || []).map((q) => ({
    text: q.text,
    qtype: q.qtype || '',
    topic: q.topic || q.topic_hint || '',
    keep: true,
  }))
  dialog.value = true
}

function toggleAll(v) { qlist.value.forEach((q) => { q.keep = v }) }

async function doConfirm() {
  confirming.value = true
  try {
    const res = await api.post(`/api/ingests/${current.value.id}/confirm`, {
      ...meta.value,
      questions: qlist.value.filter((q) => q.keep).map((q) => ({
        text: q.text, qtype: q.qtype, topic: q.topic, keep: true,
      })),
    })
    ElMessage.success(`已入库：面经 ${res.interview_id}，题目 ${res.questions_kept} 道`)
    dialog.value = false
    tab.value = 'confirmed'
    load()
  } finally { confirming.value = false }
}

async function discard(id) {
  await ElMessageBox.confirm('丢弃后原文仍保留在 archived/，但不会入库', '确认丢弃')
  await api.post(`/api/ingests/${id}/discard`)
  ElMessage.info('已丢弃')
  load()
}

onMounted(load)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
</style>
