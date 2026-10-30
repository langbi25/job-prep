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
      <el-table-column label="抽取结果" width="220">
        <template #default="{ row }">
          <template v-if="row.extracted?.questions?.length">
            <div>
              {{ row.extracted.questions.length }} 题 ·
              {{ row.extracted.direction || '方向未知' }}
              <span v-if="row.extracted.company">· {{ row.extracted.company }}</span>
            </div>
            <div style="margin-top:2px">
              <el-tag v-for="t in judgeTags(row.extracted)" :key="t.text" size="small"
                      :type="t.type" style="margin-right:4px">{{ t.text }}</el-tag>
            </div>
          </template>
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

        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
          <span style="font-weight:600">抽取题目 {{ qlist.length }} 道，将入库 {{ keptCount }} 道</span>
          <el-tag size="small" type="info" effect="plain">{{ docTypeText(current.extracted?.doc_type) }}</el-tag>
          <el-tag v-if="buildCount" size="small" type="warning" effect="plain">
            自动起草 {{ buildCount }} 张卡（上限 3）
          </el-tag>
          <el-button size="small" text type="primary" @click="toggleAll(true)">全选</el-button>
          <el-button size="small" text @click="toggleAll(false)">全不选</el-button>
        </div>

        <el-table :data="qlist" max-height="380" size="small">
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="ans-box">
                <div v-if="row.answer">
                  <b>原文答案</b>
                  <pre class="ans-text">{{ row.answer }}</pre>
                </div>
                <div v-else class="muted">
                  原文里没有这道题的实质性回答（或回答被否定）——不会写入参考答案
                </div>
                <div v-if="row.answer_gaps?.length" style="margin-top:6px">
                  <b>缺什么</b>
                  <span>{{ row.answer_gaps.join('；') }}</span>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column width="50" align="center">
            <template #default="{ row }">
              <el-checkbox v-model="row.keep" />
            </template>
          </el-table-column>
          <el-table-column type="index" width="44" />
          <el-table-column prop="text" label="题目" min-width="300" show-overflow-tooltip />
          <el-table-column prop="qtype" label="题型" width="90" />
          <el-table-column prop="topic" label="主题" width="120" show-overflow-tooltip />
          <el-table-column label="答案" width="104">
            <template #default="{ row }">
              <el-tag size="small" :type="compType(row.answer_completeness)" effect="light">
                {{ compText(row.answer_completeness) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="AI 建卡" width="82" align="center">
            <template #default="{ row }">
              <el-checkbox v-model="row.build_card" :disabled="!row.keep" />
            </template>
          </el-table-column>
        </el-table>
        <el-alert type="info" :closable="false" show-icon style="margin-top: 12px"
                  title="取消勾选的题目不会入题目池。勾了「AI 建卡」的题，确认后会自动起草卡片进审批中心——仍要你逐条过一遍，因为卡片要点会成为模拟面试的评分基准。" />
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
const buildCount = computed(() => qlist.value.filter((q) => q.keep && q.build_card).length)

const COMP_TEXT = { none: '仅题目', partial: '答案不完整', complete: '答案完整' }
const COMP_TYPE = { none: 'info', partial: 'warning', complete: 'success' }
const compText = (c) => COMP_TEXT[c] || '仅题目'
const compType = (c) => COMP_TYPE[c] || 'info'

const docTypeText = (d) => ({
  interview: '面经（多题）', single_question: '单题摘录', answer_snippet: '答案笔记',
}[d] || '未判定')

// 列表页一眼看出这批抽取里有多少是"光有题、没答案"——它们才是真正需要建卡的
function judgeTags(ex) {
  const qs = ex.questions || []
  const n = (k) => qs.filter((q) => (q.answer_completeness || 'none') === k).length
  const tags = []
  if (n('complete')) tags.push({ text: `完整 ${n('complete')}`, type: 'success' })
  if (n('partial')) tags.push({ text: `不完整 ${n('partial')}`, type: 'warning' })
  if (n('none')) tags.push({ text: `仅题目 ${n('none')}`, type: 'info' })
  if (qs.some((q) => q.answer && !q.answer_gaps?.length)) tags.push({ text: '有参考答案', type: 'primary' })
  return tags
}

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
  qlist.value = (ex.questions || []).map((q) => {
    const comp = q.answer_completeness || 'none'
    return {
      text: q.text,
      qtype: q.qtype || '',
      topic: q.topic || q.topic_hint || '',
      keep: true,
      answer: q.answer || '',
      answer_completeness: comp,
      answer_gaps: q.answer_gaps || [],
      // 留用的题默认都建卡；有无答案只决定走"原文规范化"还是"模型起草"
      build_card: true,
    }
  })
  dialog.value = true
}

function toggleAll(v) { qlist.value.forEach((q) => { q.keep = v }) }

async function doConfirm() {
  confirming.value = true
  try {
    const res = await api.post(`/api/ingests/${current.value.id}/confirm`, {
      ...meta.value,
      questions: qlist.value.filter((q) => q.keep).map((q) => ({
        text: q.text, qtype: q.qtype, topic: q.topic, keep: true, build_card: q.build_card,
      })),
    })
    let msg = `已入库：面经 ${res.interview_id}，题目 ${res.questions_kept} 道`
    if (res.answers_saved) msg += `，写入参考答案 ${res.answers_saved} 道`
    ElMessage.success(msg)
    if (res.draft_queued) {
      ElMessage.info({
        message: `正在后台起草 ${res.draft_queued} 张卡片，约 1 分钟后到「审批中心」查看`
          + (res.draft_skipped ? `；另有 ${res.draft_skipped} 道未起草，可在题目池批量建卡` : ''),
        duration: 6000,
      })
    }
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
.ans-box { padding: 6px 12px 6px 48px; font-size: 12px; line-height: 1.7; }
.ans-text { margin: 4px 0 0; white-space: pre-wrap; font-family: inherit; color: #606266; }
.muted { color: #c0c4cc; }
</style>
