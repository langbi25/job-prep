<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2>复盘中心 · 把过程变成面试资产</h2>
        <p class="sub">
          复盘文档 → 提炼考点 → 生成题目入池 → 起草知识卡（卡进审批中心，不直接落库）
        </p>
      </div>
      <el-button :icon="Refresh" circle @click="loadAll" />
    </div>

    <el-row :gutter="16">
      <!-- 左：导入 + 列表 -->
      <el-col :span="7">
        <el-card shadow="never" style="margin-bottom: 12px">
          <template #header><span>导入复盘文档</span></template>
          <div class="hint" style="margin-bottom: 8px">
            从项目 <code>docs/</code> 选一份 MD（建设复盘、踩坑笔记、过程总结都行）：
          </div>
          <el-select v-model="pickDoc" filterable placeholder="选择文档…" style="width: 100%; margin-bottom: 8px">
            <el-option v-for="d in docs" :key="d.path" :label="`${d.title}（${d.chars} 字）`" :value="d.path" />
          </el-select>
          <el-button type="primary" :disabled="!pickDoc" :loading="ingesting" @click="ingestDoc" style="width: 100%">
            导入
          </el-button>
          <el-divider>或粘贴内容</el-divider>
          <el-input v-model="paste.title" placeholder="标题（可选，缺省取首行）" size="small" style="margin-bottom: 8px" />
          <el-input v-model="paste.content" type="textarea" :rows="6" placeholder="粘贴复盘内容（≥200 字）" />
          <el-button :disabled="paste.content.trim().length < 200" :loading="ingesting"
                     @click="ingestPaste" style="width: 100%; margin-top: 8px">
            导入粘贴内容
          </el-button>
        </el-card>

        <el-card shadow="never">
          <template #header><span>复盘列表（{{ rets.length }}）</span></template>
          <el-empty v-if="!rets.length" description="还没有复盘文档" :image-size="60" />
          <div v-for="r in rets" :key="r.id" class="retro-row" :class="{ active: r.id === current?.id }"
               @click="openRetro(r.id)">
            <div class="retro-title">
              <el-tag size="small" :type="statusType(r.status)">{{ statusText(r.status) }}</el-tag>
              {{ r.title }}
            </div>
            <div class="retro-meta">{{ r.chars }} 字 · {{ r.created_at }}</div>
          </div>
        </el-card>
      </el-col>

      <!-- 右：详情 + 三步流水线 -->
      <el-col :span="17">
        <el-empty v-if="!current" description="从左侧选择或导入一份复盘文档" />
        <template v-else>
          <el-card shadow="never" style="margin-bottom: 12px">
            <template #header>
              <div class="card-head">
                <span>{{ current.title }}</span>
                <div>
                  <el-button size="small" type="primary" :loading="busy === 'extract'"
                             :disabled="!current.content" @click="doExtract">
                    ① 提炼考点
                  </el-button>
                  <el-button size="small" type="primary" :loading="busy === 'questions'"
                             :disabled="!(current.points || []).length" @click="doQuestions">
                    ② 生成题目入池
                  </el-button>
                  <el-button size="small" type="warning" :loading="busy === 'cards'"
                             :disabled="!(current.question_ids || []).length" @click="doCards">
                    ③ 起草知识卡（进审批）
                  </el-button>
                </div>
              </div>
            </template>
            <div class="hint">
              来源：{{ current.source_path || '手动粘贴' }} · {{ (current.content || '').length }} 字
              <el-tag v-if="current.error" type="danger" size="small" style="margin-left: 8px">
                上次出错：{{ current.error }}
              </el-tag>
            </div>
            <div v-if="current.source_path" class="doc-preview">{{ contentPreview }}</div>
          </el-card>

          <el-card shadow="never" style="margin-bottom: 12px">
            <template #header>考点（{{ (current.points || []).length }}）</template>
            <el-empty v-if="!(current.points || []).length" description="先点「① 提炼考点」" :image-size="50" />
            <el-table v-else :data="current.points" size="small">
              <el-table-column prop="point" label="考点" min-width="220" show-overflow-tooltip />
              <el-table-column prop="why" label="为什么被问" min-width="180" show-overflow-tooltip />
              <el-table-column prop="ask" label="深挖方向" min-width="180" show-overflow-tooltip />
              <el-table-column prop="topic" label="主题" width="100">
                <template #default="{ row }"><el-tag size="small">{{ row.topic }}</el-tag></template>
              </el-table-column>
            </el-table>
          </el-card>

          <el-row :gutter="12">
            <el-col :span="12">
              <el-card shadow="never">
                <template #header>入池题目（{{ (current.questions || []).length }}）</template>
                <el-empty v-if="!(current.questions || []).length" description="点「② 生成题目入池」后出现" :image-size="50" />
                <div v-for="q in current.questions" :key="q.question_id" class="q-row">
                  <el-tag size="small" type="info">{{ q.qtype }}</el-tag>
                  <span class="q-text">{{ q.text }}</span>
                </div>
              </el-card>
            </el-col>
            <el-col :span="12">
              <el-card shadow="never">
                <template #header>
                  卡草稿（{{ (current.cards || []).length }}）· <el-tag size="small" type="warning">待审批</el-tag>
                </template>
                <el-empty v-if="!(current.cards || []).length" description="点「③ 起草知识卡」后出现" :image-size="50" />
                <div v-for="c in current.cards" :key="c.action_id" class="q-row">
                  <el-tag size="small" type="warning">pending</el-tag>
                  <span class="q-text">{{ c.title }}</span>
                </div>
                <div v-if="(current.cards || []).length" class="hint" style="margin-top: 8px">
                  到 <b>审批中心</b> 批准后才会成为知识卡——卡内容会进评分上下文，错一条污染一轮打分。
                </div>
              </el-card>
            </el-col>
          </el-row>
        </template>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'

const docs = ref([])
const pickDoc = ref('')
const paste = ref({ title: '', content: '' })
const rets = ref([])
const current = ref(null)
const ingesting = ref(false)
const busy = ref('')

const contentPreview = computed(() => {
  const c = current.value?.content || ''
  return c.length > 1200 ? c.slice(0, 1200) + ' …（完整内容见源文件）' : c
})

const statusText = (s) => ({ ingested: '已导入', extracted: '已提炼', built: '已入池', cards_done: '已起草', failed: '失败' }[s] || s)
const statusType = (s) => ({ ingested: 'info', extracted: '', built: 'primary', cards_done: 'warning', failed: 'danger' }[s] || 'info')

async function loadAll() {
  try {
    docs.value = (await api.get('/api/retro/docs')).items
    rets.value = (await api.get('/api/retro/list')).items
  } catch { /* 拦截器已提示 */ }
}

async function openRetro(id) {
  try { current.value = await api.get(`/api/retro/${id}`) } catch { /* 拦截器已提示 */ }
}

async function ingestDoc() {
  ingesting.value = true
  try {
    const r = await api.post('/api/retro/ingest-doc', { path: pickDoc.value })
    ElMessage.success(`已导入：${r.title}`)
    await loadAll(); await openRetro(r.retro_id)
  } catch { /* 拦截器已提示 */ } finally { ingesting.value = false }
}

async function ingestPaste() {
  ingesting.value = true
  try {
    const r = await api.post('/api/retro/ingest', { title: paste.value.title, content: paste.value.content })
    ElMessage.success(`已导入：${r.title}`)
    paste.value = { title: '', content: '' }
    await loadAll(); await openRetro(r.retro_id)
  } catch { /* 拦截器已提示 */ } finally { ingesting.value = false }
}

async function doExtract() {
  busy.value = 'extract'
  try {
    current.value = await api.post(`/api/retro/${current.value.id}/extract`, {})
    ElMessage.success(`提炼出 ${current.value.points.length} 个考点`); await loadAll()
  } catch { await openRetro(current.value.id) } finally { busy.value = '' }
}

async function doQuestions() {
  busy.value = 'questions'
  try {
    const r = await api.post(`/api/retro/${current.value.id}/questions`, { per_point: 1 })
    ElMessage.success(`新入池 ${r.inserted.length} 题，重复跳过 ${r.skipped}`)
    await openRetro(current.value.id); await loadAll()
  } catch { await openRetro(current.value.id) } finally { busy.value = '' }
}

async function doCards() {
  busy.value = 'cards'
  try {
    const r = await api.post(`/api/retro/${current.value.id}/cards`, { limit: 3 })
    ElMessage.success(`起草 ${r.drafted.length} 张卡，已进审批中心`)
    await openRetro(current.value.id); await loadAll()
  } catch { await openRetro(current.value.id) } finally { busy.value = '' }
}

onMounted(loadAll)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.sub { color: #909399; font-size: 12px; margin: 4px 0 0; }
.hint { color: #909399; font-size: 12px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.retro-row { padding: 8px 6px; border-bottom: 1px dashed #ebeef5; cursor: pointer; border-radius: 4px; }
.retro-row:hover { background: #f5f7fa; }
.retro-row.active { background: #ecf5ff; }
.retro-title { font-size: 13px; display: flex; align-items: center; gap: 6px; }
.retro-meta { color: #909399; font-size: 11px; margin-top: 2px; padding-left: 2px; }
.q-row { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px dashed #ebeef5; }
.q-row:last-child { border-bottom: none; }
.q-text { flex: 1; font-size: 13px; }
.doc-preview { white-space: pre-wrap; background: #fafafa; border: 1px solid #ebeef5; border-radius: 4px;
  padding: 10px; font-size: 12px; color: #606266; max-height: 180px; overflow: auto; margin-top: 8px; }
</style>
