<template>
  <div class="page">
    <div class="page-header">
      <h2>知识库 · 分层视图</h2>
      <div>
        <el-radio-group v-model="mode" size="small" style="margin-right: 12px">
          <el-radio-button value="layered">分层</el-radio-button>
          <el-radio-button value="flat">平铺列表</el-radio-button>
        </el-radio-group>
        <el-button v-if="hollowCount" type="warning" plain :loading="filling" @click="fillHollow">
          AI 补空壳卡（{{ hollowCount }}）
        </el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate()">新建卡片</el-button>
        <el-button :icon="Refresh" circle @click="reloadAll" />
      </div>
    </div>

    <!-- ============ 分层模式 ============ -->
    <template v-if="mode === 'layered'">
      <!-- 层级总览条：L0 → L3 → 面经，点 L2/L3 可以切换右侧内容面板 -->
      <el-card shadow="never" class="strip-card" v-loading="loading">
        <div class="strip" v-if="layers">
          <div class="layer l0" @click="setContentTab('all')">
            <div class="ln">L0</div>
            <div class="lnum">{{ layers.L0_domains }}</div>
            <div class="lname">领域</div>
          </div>
          <div class="arrow">→</div>
          <div class="layer l1">
            <div class="ln">L1</div>
            <div class="lnum">{{ layers.L1_topics }}</div>
            <div class="lname">主题</div>
          </div>
          <div class="arrow">→</div>
          <div class="layer l2" :class="{ on: contentTab === 'cards' }" @click="setContentTab('cards')">
            <div class="ln">L2</div>
            <div class="lnum">{{ layers.L2_cards.active }}</div>
            <div class="lname">知识卡</div>
            <div class="lsub">
              <span v-if="layers.L2_cards.archived" class="dim">归档 {{ layers.L2_cards.archived }}</span>
              <span v-if="layers.L2_cards.hollow" class="warn">空壳 {{ layers.L2_cards.hollow }}</span>
            </div>
          </div>
          <div class="arrow">→</div>
          <div class="layer l3" :class="{ on: contentTab === 'questions' }" @click="setContentTab('questions')">
            <div class="ln">L3</div>
            <div class="lnum">{{ layers.L3_questions.total }}</div>
            <div class="lname">题目</div>
            <div class="lsub">
              <span class="ok">已关联 {{ layers.L3_questions.linked }}</span>
              <span class="bad">无卡 {{ layers.L3_questions.orphan }}</span>
            </div>
          </div>
          <div class="arrow">→</div>
          <div class="layer l4">
            <div class="ln">L4</div>
            <div class="lnum">{{ layers.interviews }}</div>
            <div class="lname">面经</div>
          </div>
        </div>
        <div v-if="layers" class="legend">
          卡↔题关联边 {{ layers.links.total }} 条：
          <el-tag v-for="(n, m) in layers.links.by_method" :key="m" size="small" effect="plain" class="ml6">
            {{ methodLabel(m) }} {{ n }}
          </el-tag>
          <span class="hint">无卡题不是垃圾，是覆盖缺口清单本身</span>
        </div>
      </el-card>

      <div class="cols">
        <!-- L0 领域 -->
        <div class="col">
          <div class="col-title">领域</div>
          <div class="pick" :class="{ sel: !selDomain }" @click="selDomain = null; selTopic = null">
            <span>全部</span><span class="cnt">{{ totalCards + totalQuestions }}</span>
          </div>
          <div v-for="d in tree" :key="d.domain" class="pick" :class="{ sel: selDomain === d.domain }"
               @click="selDomain = d.domain; selTopic = null">
            <span>{{ d.label }}</span>
            <span class="cnt">{{ d.card_count }}卡 / {{ d.question_count }}题</span>
          </div>
        </div>
        <!-- L1 主题 -->
        <div class="col">
          <div class="col-title">主题 <span v-if="curDomain" class="dim">· {{ curDomain.label }}</span></div>
          <template v-if="curDomain">
            <div class="pick" :class="{ sel: !selTopic }" @click="selTopic = null">
              <span>全部主题</span>
            </div>
            <div v-for="t in curDomain.topics" :key="t.raw_topic" class="pick"
                 :class="{ sel: selTopic === t.raw_topic }" @click="selTopic = t.raw_topic">
              <span>{{ t.topic }}</span>
              <span class="cnt">{{ t.card_count }}卡 / {{ t.question_count }}题
                <em v-if="t.orphans" class="bad">{{ t.orphans }}缺</em></span>
            </div>
          </template>
          <div v-else class="hint pad">← 先选领域</div>
        </div>
        <!-- 内容面板 -->
        <div class="col grow">
          <div class="content-toolbar">
            <el-input v-model="kw" placeholder="过滤卡标题 / 题面" clearable size="small" style="width: 220px" />
            <el-radio-group v-model="contentTab" size="small">
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="cards">知识卡</el-radio-button>
              <el-radio-button value="questions">题目</el-radio-button>
            </el-radio-group>
          </div>

          <template v-if="contentTab !== 'questions'">
            <div class="sec-title">知识卡 · {{ viewCards.length }}</div>
            <div v-for="c in viewCards" :key="c.id" class="item card-item" @click="openCardById(c.id)">
              <span class="it-title">{{ c.title || c.id }}</span>
              <span class="it-tags">
                <el-tag v-if="c.status !== 'active'" size="small" type="info" effect="plain">归档</el-tag>
                <el-tag v-else-if="c.hollow" size="small" type="danger" effect="plain">空壳</el-tag>
                <el-tag size="small" :type="c.mastery >= 3 ? 'success' : c.mastery >= 1.5 ? 'warning' : 'info'"
                        effect="plain">M{{ c.mastery }}</el-tag>
                <el-tag v-if="c.linked_questions" size="small" effect="plain" type="primary">
                  {{ c.linked_questions }}题</el-tag>
              </span>
            </div>
            <el-empty v-if="!viewCards.length" description="该范围没有卡" :image-size="50" />
          </template>

          <template v-if="contentTab !== 'cards'">
            <div class="sec-title">题目池 · {{ viewQuestions.length }}</div>
            <div v-for="q in viewQuestions" :key="q.id" class="item q-item" @click="openQuestionById(q.id)">
              <span class="it-title">{{ q.text }}</span>
              <span class="it-tags">
                <el-tag size="small" effect="plain" type="warning">×{{ q.freq_count }}</el-tag>
                <template v-if="q.orphan">
                  <el-tag size="small" type="danger" effect="plain">无卡</el-tag>
                </template>
                <el-tag v-for="lk in q.linked.slice(0, 2)" :key="lk.card_id" size="small" effect="plain">
                  {{ methodLabel(lk.match_method) }}</el-tag>
                <el-tag v-if="q.interview_count" size="small" effect="info" type="info">
                  {{ q.interview_count }}面经</el-tag>
              </span>
            </div>
            <el-empty v-if="!viewQuestions.length" description="该范围没有题" :image-size="50" />
          </template>
        </div>
      </div>
    </template>

    <!-- ============ 平铺列表模式（原视图，服务端搜索） ============ -->
    <template v-else>
      <el-card shadow="never" style="margin-bottom: 12px">
        <div class="toolbar">
          <el-input v-model="filters.q" placeholder="搜卡片（要点/标题）" clearable style="width: 300px"
                    @keyup.enter="load" @clear="load">
            <template #append><el-button :icon="Search" @click="load" /></template>
          </el-input>
          <el-select v-model="filters.domain" placeholder="领域" clearable style="width: 130px" @change="load">
            <el-option label="后端" value="backend" />
            <el-option label="大数据" value="bigdata" />
            <el-option label="Agent" value="agent" />
          </el-select>
          <span class="hint">卡片是 Markdown 唯一事实源，新建会同时落 knowledge/*.md 并进向量索引</span>
        </div>
      </el-card>

      <el-table :data="rows" v-loading="loading" size="small" style="width: 100%">
        <el-table-column prop="id" label="ID" width="180" />
        <el-table-column prop="title" label="题目/标题" min-width="300" show-overflow-tooltip />
        <el-table-column prop="domain" label="领域" width="90" />
        <el-table-column prop="topic" label="主题" width="130" show-overflow-tooltip />
        <el-table-column label="要点" width="70" align="center">
          <template #default="{ row }">
            <el-tag v-if="!row.points?.length" size="small" type="danger" effect="plain">空壳</el-tag>
            <span v-else>{{ row.points.length }}</span>
          </template>
        </el-table-column>
        <el-table-column label="掌握度" width="100" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.mastery >= 3 ? 'success' : row.mastery >= 1.5 ? 'warning' : 'info'">
              {{ row.mastery }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="review_count" label="复习" width="70" align="center" />
        <el-table-column prop="updated_at" label="更新时间" width="160" />
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openCardById(row.id)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <!-- 卡片详情抽屉 -->
    <el-drawer v-model="cardDrawer" :title="card?.title" size="46%">
      <template v-if="card">
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item label="ID">{{ card.id }}</el-descriptions-item>
          <el-descriptions-item label="L1 主题">{{ card.domain }} / {{ card.topic }}</el-descriptions-item>
          <el-descriptions-item label="掌握度">{{ card.mastery }}</el-descriptions-item>
          <el-descriptions-item label="复习次数">{{ card.review_count }}</el-descriptions-item>
        </el-descriptions>
        <h4>答题要点</h4>
        <el-empty v-if="!card.points?.length" description="空壳卡：覆盖率算它有卡，但谁都读不到内容" :image-size="50" />
        <ol v-else class="points">
          <li v-for="(p, i) in card.points" :key="i">{{ p }}</li>
        </ol>
        <template v-if="card.pitfalls?.length">
          <h4>常见坑</h4>
          <ul class="pitfalls"><li v-for="(p, i) in card.pitfalls" :key="i">{{ p }}</li></ul>
        </template>
        <template v-if="card.linked_questions?.length">
          <h4>覆盖的题目（L3）</h4>
          <div v-for="lq in card.linked_questions" :key="lq.question_id" class="link-line"
               @click="openQuestionById(lq.question_id)">
            <span>{{ lq.text }}</span>
            <el-tag size="small" effect="plain">{{ methodLabel(lq.match_method) }}</el-tag>
          </div>
        </template>
        <p v-if="card.file_path" class="path">源文件：{{ card.file_path }}</p>
      </template>
    </el-drawer>

    <!-- 题目详情抽屉 -->
    <el-drawer v-model="qDrawer" :title="'题目详情'" size="46%">
      <template v-if="qdetail">
        <div class="qtext">{{ qdetail.text }}</div>
        <el-descriptions :column="2" size="small" border style="margin-top: 12px">
          <el-descriptions-item label="频次">×{{ qdetail.freq_count }}</el-descriptions-item>
          <el-descriptions-item label="题型">{{ qdetail.qtype || '—' }}</el-descriptions-item>
          <el-descriptions-item label="主题">{{ qdetail.topic || '—' }}</el-descriptions-item>
          <el-descriptions-item label="最近出现">{{ qdetail.last_seen_at || '—' }}</el-descriptions-item>
        </el-descriptions>
        <template v-if="qdetail.aliases?.length">
          <h4>同义别名</h4>
          <el-tag v-for="a in qdetail.aliases" :key="a" size="small" effect="plain" class="ml6 mb6">{{ a }}</el-tag>
        </template>
        <h4>关联知识卡（L2）</h4>
        <el-empty v-if="!qdetail.linked_cards?.length"
                  description="无卡覆盖——这就是缺卡的地方，可以 AI 起草" :image-size="50" />
        <div v-for="lk in qdetail.linked_cards" :key="lk.card_id" class="link-line"
             @click="openCardById(lk.card_id)">
          <span>{{ lk.title }}</span>
          <span>
            <el-tag size="small" effect="plain">{{ methodLabel(lk.match_method) }}</el-tag>
            <el-tag v-if="lk.status !== 'active'" size="small" type="info" effect="plain">归档</el-tag>
          </span>
        </div>
        <h4>面经出处（L4）</h4>
        <el-empty v-if="!qdetail.occurrences?.length" description="没有记录到具体面经出处" :image-size="50" />
        <div v-for="(o, i) in qdetail.occurrences" :key="i" class="occ">
          <div class="occ-head">
            {{ o.company || '未知公司' }} · {{ o.position || o.direction || '' }}
            <el-tag v-if="o.round" size="small" effect="plain">{{ o.round }}</el-tag>
            <span class="dim">{{ o.interview_date }}</span>
          </div>
          <div v-if="o.raw_text" class="occ-raw">「{{ o.raw_text }}」</div>
        </div>
        <div style="margin-top: 20px">
          <el-button type="primary" size="small" :loading="drafting" @click="draftCard">
            AI 起草建卡
          </el-button>
        </div>
      </template>
    </el-drawer>

    <!-- 新建卡片（原样保留） -->
    <el-dialog v-model="dialog" title="新建知识卡" width="760px" top="7vh">
      <el-form label-width="70px" size="small">
        <el-form-item label="标题">
          <el-input v-model="form.title" type="textarea" :rows="2" placeholder="通常是题目本身" />
        </el-form-item>
        <el-form-item label="领域/主题">
          <el-select v-model="form.domain" style="width: 140px">
            <el-option label="后端" value="backend" />
            <el-option label="大数据" value="bigdata" />
            <el-option label="Agent" value="agent" />
          </el-select>
          <el-input v-model="form.topic" placeholder="主题" style="width: 200px; margin-left: 8px" />
        </el-form-item>
        <el-form-item label="答题要点">
          <el-input v-model="pointsText" type="textarea" :rows="8" placeholder="一行一条要点" />
        </el-form-item>
        <el-form-item label="常见坑">
          <el-input v-model="pitfallsText" type="textarea" :rows="3" placeholder="一行一条坑点（可选）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!form.title.trim() || !pointsText.trim()" @click="save">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const mode = ref('layered')
const tree = ref([])
const layers = ref(null)
const loading = ref(false)

const selDomain = ref(null)
const selTopic = ref(null)
const kw = ref('')
const contentTab = ref('all')

// 平铺模式数据
const rows = ref([])
const filters = ref({ q: '', domain: '' })

const cardDrawer = ref(false)
const card = ref(null)
const qDrawer = ref(false)
const qdetail = ref(null)
const drafting = ref(false)

const dialog = ref(false)
const saving = ref(false)
const form = ref({ title: '', domain: 'backend', topic: '' })
const pointsText = ref('')
const pitfallsText = ref('')

const METHOD_LABELS = {
  legacy: '建卡记录', manual: '手动', drafted: '起草时', vector: '向量猜', transfer: '合并转移',
}
const methodLabel = (m) => METHOD_LABELS[m] || m

const curDomain = computed(() => tree.value.find((d) => d.domain === selDomain.value) || null)
const totalCards = computed(() => layers.value?.L2_cards.total || 0)
const totalQuestions = computed(() => layers.value?.L3_questions.total || 0)

// 空壳卡 = 有卡无要点：覆盖率算它有卡，但复习/评分/Agent 都读不到内容
const hollowCount = computed(() => rows.value.filter((r) => !r.points?.length).length)

// 当前范围的卡与题（领域 → 主题 → 关键字 三级过滤，全部客户端做，量级 <200 没必要过后端）
const viewCards = computed(() => {
  const out = []
  for (const d of tree.value) {
    if (selDomain.value && d.domain !== selDomain.value) continue
    for (const t of d.topics) {
      if (selTopic.value && t.raw_topic !== selTopic.value) continue
      for (const c of t.cards) {
        if (kw.value && !(c.title || '').toLowerCase().includes(kw.value.toLowerCase())) continue
        out.push(c)
      }
    }
  }
  return out
})
const viewQuestions = computed(() => {
  const out = []
  for (const d of tree.value) {
    if (selDomain.value && d.domain !== selDomain.value) continue
    for (const t of d.topics) {
      if (selTopic.value && t.raw_topic !== selTopic.value) continue
      for (const q of t.questions) {
        if (kw.value && !q.text.toLowerCase().includes(kw.value.toLowerCase())) continue
        out.push(q)
      }
    }
  }
  return out
})

function setContentTab(tab) {
  contentTab.value = tab
}

async function loadTree() {
  loading.value = true
  try {
    const res = await api.get('/api/kb/tree')
    tree.value = res.domains
    layers.value = res.layers
  } finally { loading.value = false }
}

async function load() {
  loading.value = true
  try {
    rows.value = await api.get('/api/cards', {
      params: { q: filters.value.q || undefined, domain: filters.value.domain || undefined, limit: 200 },
    })
  } finally { loading.value = false }
}

async function reloadAll() {
  await Promise.all([loadTree(), load()])
}

// ---- 卡详情：从详情接口拿（含关联题），比树里的摘要全 ----
async function openCardById(id) {
  card.value = await api.get(`/api/cards/${id}`)
  cardDrawer.value = true
}

async function openQuestionById(id) {
  qdetail.value = await api.get(`/api/questions/${id}`)
  qDrawer.value = true
}

async function draftCard() {
  drafting.value = true
  try {
    const res = await api.post('/api/agent/cards/draft', { question_ids: [qdetail.value.id], limit: 1 })
    if (!res.drafted?.length) {
      ElMessage.warning(res.skipped || res.failed?.[0]?.error || '没有生成草稿')
      return
    }
    ElMessageBox.confirm(
      `已为该题起草知识卡 ${res.drafted.map((d) => d.card_id).join('、')}。去审批中心确认？`,
      'AI 起草建卡', { confirmButtonText: '去审批', cancelButtonText: '稍后' }
    ).then(() => window.location.hash = '#/approvals').catch(() => {})
  } finally { drafting.value = false }
}

function openCreate() {
  form.value = { title: '', domain: 'backend', topic: '' }
  pointsText.value = ''
  pitfallsText.value = ''
  dialog.value = true
}

async function save() {
  saving.value = true
  try {
    const points = pointsText.value.split('\n').map((s) => s.trim()).filter(Boolean)
    const pitfalls = pitfallsText.value.split('\n').map((s) => s.trim()).filter(Boolean)
    await api.post('/api/cards', { ...form.value, question: form.value.title, points, pitfalls, tags: [] })
    ElMessage.success('已入库')
    dialog.value = false
    reloadAll()
  } finally { saving.value = false }
}

async function fillHollow() {
  filling.value = true
  try {
    const res = await api.post('/api/agent/cards/fill', { limit: Math.min(hollowCount.value, 5) })
    if (!res.drafted?.length) {
      ElMessage.warning(res.skipped || res.failed?.[0]?.error || '没有生成草稿')
      return
    }
    ElMessageBox.confirm(
      `已为空壳卡起草 ${res.drafted.length} 份正文：${res.drafted.map((d) => d.card_id).join('、')}。` +
      '现在去审批中心逐条确认？',
      'AI 补卡正文', { confirmButtonText: '去审批', cancelButtonText: '稍后' }
    ).then(() => window.location.hash = '#/approvals').catch(() => {})
  } finally { filling.value = false }
}

// 切到平铺模式时才需要拉列表
watch(mode, (m) => { if (m === 'flat' && !rows.value.length) load() })

onMounted(reloadAll)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.hint { color: #909399; font-size: 12px; }
.hint.pad { padding: 12px; }
.points { padding-left: 20px; line-height: 1.9; }
.pitfalls { padding-left: 20px; line-height: 1.9; color: #e6a23c; }
.path { color: #909399; font-size: 12px; margin-top: 16px; }
.ml6 { margin-left: 6px; } .mb6 { margin-bottom: 6px; }
.bad { color: #f56c6c; font-style: normal; }

/* ---- 层级总览条 ---- */
.strip-card { margin-bottom: 12px; }
.strip { display: flex; align-items: stretch; gap: 8px; flex-wrap: wrap; }
.layer { border: 1px solid #dcdfe6; border-radius: 8px; padding: 8px 16px; min-width: 96px;
         cursor: pointer; background: #f5f7fa; transition: all .15s; }
.layer:hover { border-color: #409eff; }
.layer.on { border-color: #409eff; background: #ecf5ff; }
.ln { font-size: 11px; color: #909399; }
.lnum { font-size: 22px; font-weight: 600; line-height: 1.3; }
.lname { font-size: 12px; color: #606266; }
.lsub { font-size: 11px; display: flex; gap: 8px; margin-top: 2px; }
.lsub .ok { color: #67c23a; } .lsub .bad { color: #f56c6c; }
.lsub .dim { color: #909399; } .lsub .warn { color: #e6a23c; }
.arrow { align-self: center; color: #c0c4cc; font-size: 18px; }
.legend { margin-top: 10px; font-size: 12px; color: #606266; display: flex; align-items: center;
          gap: 4px; flex-wrap: wrap; }
.legend .hint { margin-left: auto; }

/* ---- 三栏 ---- */
.cols { display: grid; grid-template-columns: 170px 230px 1fr; gap: 12px; align-items: start; }
.col { background: #fff; border: 1px solid #ebeef5; border-radius: 8px; padding: 10px;
       max-height: calc(100vh - 260px); overflow-y: auto; }
.col.grow { min-height: 300px; }
.col-title { font-weight: 600; font-size: 13px; margin-bottom: 8px; }
.pick { display: flex; justify-content: space-between; align-items: center; gap: 6px;
        padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.pick:hover { background: #f5f7fa; }
.pick.sel { background: #ecf5ff; color: #409eff; }
.pick .cnt { color: #909399; font-size: 11px; white-space: nowrap; }
.content-toolbar { display: flex; justify-content: space-between; align-items: center;
                   gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.sec-title { font-weight: 600; font-size: 13px; margin: 10px 0 6px; color: #303133; }
.item { display: flex; justify-content: space-between; align-items: center; gap: 8px;
        padding: 7px 8px; border-radius: 6px; cursor: pointer; border-bottom: 1px solid #f5f7fa;
        font-size: 13px; }
.item:hover { background: #f5f7fa; }
.it-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.it-tags { display: flex; gap: 4px; flex-shrink: 0; }
.link-line { display: flex; justify-content: space-between; align-items: center; gap: 8px;
             padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 13px;
             border-bottom: 1px solid #f5f7fa; }
.link-line:hover { background: #f5f7fa; }
.qtext { font-size: 14px; font-weight: 600; line-height: 1.7; white-space: pre-wrap; }
.occ { padding: 8px; border: 1px solid #ebeef5; border-radius: 6px; margin-bottom: 8px; }
.occ-head { font-size: 13px; font-weight: 600; display: flex; gap: 8px; align-items: center; }
.occ-head .dim { color: #909399; font-weight: 400; font-size: 12px; }
.occ-raw { font-size: 12px; color: #606266; margin-top: 4px; line-height: 1.6; }
</style>
