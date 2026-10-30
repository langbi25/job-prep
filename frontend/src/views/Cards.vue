<template>
  <div class="page">
    <div class="page-header">
      <h2>知识库 · {{ rows.length }} 张卡</h2>
      <div>
        <el-button v-if="hollowCount" type="warning" plain :loading="filling" @click="fillHollow">
          AI 补空壳卡（{{ hollowCount }}）
        </el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate()">新建卡片</el-button>
        <el-button :icon="Refresh" circle @click="load" />
      </div>
    </div>

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
          <el-button size="small" text type="primary" @click="openDetail(row)">查看</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer v-model="drawer" :title="current?.title" size="46%">
      <template v-if="current">
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item label="ID">{{ current.id }}</el-descriptions-item>
          <el-descriptions-item label="领域">{{ current.domain }} / {{ current.topic }}</el-descriptions-item>
          <el-descriptions-item label="掌握度">{{ current.mastery }}</el-descriptions-item>
          <el-descriptions-item label="复习次数">{{ current.review_count }}</el-descriptions-item>
        </el-descriptions>
        <h4>答题要点</h4>
        <el-empty v-if="!current.points?.length" description="暂无要点" :image-size="50" />
        <ol v-else class="points">
          <li v-for="(p, i) in current.points" :key="i">{{ p }}</li>
        </ol>
        <template v-if="current.pitfalls?.length">
          <h4>常见坑</h4>
          <ul class="pitfalls"><li v-for="(p, i) in current.pitfalls" :key="i">{{ p }}</li></ul>
        </template>
        <p v-if="current.file_path" class="path">源文件：{{ current.file_path }}</p>
      </template>
    </el-drawer>

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
import { computed, onMounted, ref } from 'vue'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const rows = ref([])
const loading = ref(false)
const filling = ref(false)
const filters = ref({ q: '', domain: '' })
const drawer = ref(false)
const current = ref(null)
const dialog = ref(false)
const saving = ref(false)
const form = ref({ title: '', domain: 'backend', topic: '' })
const pointsText = ref('')
const pitfallsText = ref('')

// 空壳卡 = 有卡无要点：覆盖率算它有卡，但复习/评分/Agent 都读不到内容
const hollowCount = computed(() => rows.value.filter((r) => !r.points?.length).length)

async function load() {
  loading.value = true
  try {
    rows.value = await api.get('/api/cards', {
      params: { q: filters.value.q || undefined, domain: filters.value.domain || undefined, limit: 200 },
    })
  } finally { loading.value = false }
}

function openDetail(row) {
  current.value = row
  drawer.value = true
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
    load()
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

onMounted(load)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.hint { color: #909399; font-size: 12px; }
.points { padding-left: 20px; line-height: 1.9; }
.pitfalls { padding-left: 20px; line-height: 1.9; color: #e6a23c; }
.path { color: #909399; font-size: 12px; margin-top: 16px; }
</style>
