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
      <el-table-column prop="id" label="ID" width="185">
        <template #default="{ row }">
          <el-link type="primary" :underline="false" title="点击查看这次采集的完整详情"
                   @click="openDetail(row.id)">{{ row.id }}</el-link>
        </template>
      </el-table-column>
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

    <!-- 单次采集详情：抽取判定 → 题目去向 → 答案 → 卡片，一条链路看到底 -->
    <el-drawer v-model="detailOpen" size="64%" :title="`采集详情 · ${detail?.id || ''}`">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <el-alert v-if="detail.status === 'failed'" type="error" :closable="false" show-icon
                    style="margin-bottom: 10px" :title="`抽取失败：${detail.error || '未知原因'}`" />

          <el-descriptions :column="3" size="small" border>
            <el-descriptions-item label="状态">
              <el-tag size="small" :type="statusType(detail.status)">{{ statusText(detail.status) }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="来源">
              {{ detail.source_type }}<span v-if="detail.source_site"> / {{ detail.source_site }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="投喂时间">{{ detail.created_at }}</el-descriptions-item>
            <el-descriptions-item label="面经 ID">{{ detail.interview?.id || '—' }}</el-descriptions-item>
            <el-descriptions-item label="公司 / 岗位">
              {{ detail.interview?.company || detail.extracted?.company || '—' }}
              <span v-if="detail.interview?.position || detail.extracted?.position">
                / {{ detail.interview?.position || detail.extracted?.position }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="原文">
              {{ detail.raw_chars }} 字
              <span :style="{ color: detail.raw_dir ? '#909399' : '#f56c6c' }">
                （{{ detail.raw_dir === 'archived' ? '已归档' : detail.raw_dir === 'pending' ? '待处理区' : '文件已丢失' }}）
              </span>
            </el-descriptions-item>
          </el-descriptions>

          <div v-if="detail.summary?.n_questions" class="sum-row">
            <el-tag size="small" effect="plain">{{ docTypeText(detail.extracted?.doc_type) }}</el-tag>
            <el-tag size="small" type="info" effect="plain">抽到 {{ detail.summary?.n_questions }} 题</el-tag>
            <el-tag size="small" type="success" effect="plain">新题 {{ detail.summary?.n_new }}</el-tag>
            <el-tag size="small" type="warning" effect="plain">并到老题 {{ detail.summary?.n_merged }}</el-tag>
            <el-tag size="small" type="primary" effect="plain">带答案 {{ detail.summary?.n_with_answer }}</el-tag>
            <el-tag size="small" effect="plain">已建卡 {{ detail.summary?.n_cards_created }}</el-tag>
            <el-tag v-if="detail.summary?.n_cards_pending" size="small" type="warning">
              待审批 {{ detail.summary?.n_cards_pending }}
            </el-tag>
          </div>

          <template v-if="detail.questions?.length">
            <h4>题目去向（{{ detail.questions.length }} 道）</h4>
            <el-table :data="detail.questions" size="small" max-height="330">
              <el-table-column type="expand">
                <template #default="{ row }">
                  <div class="ans-box">
                    <div v-if="row.answer_points?.length">
                      <b>参考答案要点（写入题目池，模拟面试评分时会用到）</b>
                      <ul class="pts"><li v-for="(p, i) in row.answer_points" :key="i">{{ p }}</li></ul>
                    </div>
                    <div v-else class="muted">本次没有为这道题写入参考答案</div>
                    <div v-if="row.cards?.length" style="margin-top:6px">
                      <b>建卡动作</b>
                      <div v-for="c in row.cards" :key="c.action_id" style="margin-top:2px">
                        <el-tag size="small" :type="c.low_risk ? 'success' : 'warning'" effect="plain">
                          {{ c.low_risk ? '原文抽取' : '模型起草' }}
                        </el-tag>
                        <el-tag size="small" :type="c.status === 'approved' ? 'success' : c.status === 'pending' ? 'info' : 'danger'"
                                style="margin-left:4px">{{ actionStatusText(c.status) }}</el-tag>
                        <span class="muted" style="margin-left:6px">
                          {{ c.n_points }} 条要点<template v-if="c.card_id"> → 卡片 {{ c.card_id }}</template>
                        </span>
                      </div>
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="ask_order" label="#" width="50" />
              <el-table-column prop="text" label="题目" min-width="290" show-overflow-tooltip />
              <el-table-column label="答案" width="100">
                <template #default="{ row }">
                  <el-tag size="small" effect="light" :type="compType(row.answer_completeness)">
                    {{ compText(row.answer_completeness) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="去向" width="150">
                <template #default="{ row }">
                  <el-tag v-if="row.is_new" size="small" type="success" effect="plain">新题 {{ row.question_id }}</el-tag>
                  <el-tag v-else-if="row.merged_into" size="small" type="info" effect="plain">已并入 {{ row.merged_into }}</el-tag>
                  <el-tag v-else size="small" type="info" effect="plain">并到 {{ row.question_id }}（第 {{ row.freq_count }} 次）</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="卡片" width="110">
                <template #default="{ row }">
                  <span v-if="!row.cards?.length" class="muted">—</span>
                  <template v-else>
                    <el-tag v-for="c in row.cards" :key="c.action_id" size="small"
                            :type="c.card_id ? 'success' : 'warning'" effect="plain"
                            style="margin-right:2px">
                      {{ c.card_id ? '已建' : actionStatusText(c.status) }}
                    </el-tag>
                  </template>
                </template>
              </el-table-column>
            </el-table>
          </template>
          <el-empty v-else-if="detail.status === 'confirmed'" :image-size="70"
                    description="这次采集确认入库时一道题都没留下（可能全部取消勾选了）" />
          <el-empty v-else-if="detail.status === 'pending'" :image-size="70"
                    description="还没抽取，点右上角「提取」再看" />
          <el-empty v-else-if="detail.status === 'discarded'" :image-size="70"
                    description="这次采集已丢弃，没有题目入库（原文仍在 archived/）" />
          <el-empty v-else :image-size="70" description="没有题目记录" />

          <el-collapse v-if="detail.raw_text" style="margin-top:14px">
            <el-collapse-item :title="`原始内容（${detail.raw_chars} 字）`" name="raw">
              <pre class="raw-body">{{ detail.raw_text }}</pre>
            </el-collapse-item>
          </el-collapse>
        </template>
      </div>
      <template #footer>
        <el-button @click="detailOpen = false">关闭</el-button>
        <el-button v-if="detail && ['pending', 'failed'].includes(detail.status)" type="primary"
                   :loading="busy === detail.id" @click="reExtract">重新提取</el-button>
      </template>
    </el-drawer>
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

const detailOpen = ref(false)
const detailLoading = ref(false)
const detail = ref(null)

const actionStatusText = (s) => (
  { pending: '待审批', approved: '已批准', rejected: '已驳回', failed: '失败' }[s] || s
)

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

async function openDetail(id) {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await api.get(`/api/ingests/${id}`)
  } finally { detailLoading.value = false }
}

async function reExtract() {
  const id = detail.value?.id
  if (!id) return
  busy.value = id
  try {
    await api.post(`/api/ingests/${id}/extract`)
    ElMessage.success('已重新提取')
    await openDetail(id)
    await load()
    const row = rows.value.find((r) => r.id === id)
    if (row) openConfirm(row)
  } finally { busy.value = '' }
}

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
.sum-row { display: flex; gap: 6px; flex-wrap: wrap; margin: 14px 0 4px; }
.pts { padding-left: 18px; line-height: 1.8; margin: 4px 0; }
h4 { margin: 16px 0 8px; font-size: 14px; }
.raw-body {
  background: #f5f7fa; padding: 10px 12px; border-radius: 6px;
  font-size: 12px; line-height: 1.7; white-space: pre-wrap; word-break: break-word;
  max-height: 360px; overflow: auto;
}
</style>
