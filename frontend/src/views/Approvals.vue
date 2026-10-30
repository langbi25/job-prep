<template>
  <div class="page">
    <div class="page-header">
      <h2>审批中心 · AI 动作需你点头才生效</h2>
      <div>
        <el-button :icon="Refresh" circle @click="load" />
      </div>
    </div>

    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px"
              title="AI 只会起草，不会直接改知识库。批准后才写进 Markdown 事实源并重建索引；驳回则只留记录。" />

    <el-tabs v-model="tab" @tab-change="load">
      <el-tab-pane label="待审批" name="pending" />
      <el-tab-pane label="已批准" name="approved" />
      <el-tab-pane label="已驳回" name="rejected" />
      <el-tab-pane label="失败" name="failed" />
      <el-tab-pane label="全部" name="" />
    </el-tabs>

    <div v-if="tab === 'pending' && rows.length" style="margin-bottom: 10px">
      <el-button type="success" size="small" :disabled="!selected.length" @click="batchApprove">
        批量批准（{{ selected.length }}）
      </el-button>
    </div>

    <el-table :data="rows" v-loading="loading" size="small" style="width: 100%"
              @selection-change="(v) => (selected = v)">
      <el-table-column v-if="tab === 'pending'" type="selection" width="44" />
      <el-table-column label="类型" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="kindType(row.kind)">{{ kindText(row.kind) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="目标" min-width="260" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.payload?.title || row.payload?.card_id || row.payload?.keeper_card_id || '—' }}
        </template>
      </el-table-column>
      <el-table-column label="理由" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.reason || '—' }}</template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="160" />
      <el-table-column prop="status" label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openDetail(row)">详情</el-button>
          <template v-if="row.status === 'pending'">
            <el-button size="small" type="success" plain @click="decide(row, true)">批准</el-button>
            <el-button size="small" type="danger" plain @click="decide(row, false)">驳回</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer v-model="drawer" title="动作详情" size="46%">
      <template v-if="current">
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item label="动作 ID">{{ current.id }}</el-descriptions-item>
          <el-descriptions-item label="类型">{{ kindText(current.kind) }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ statusText(current.status) }}</el-descriptions-item>
          <el-descriptions-item label="理由">{{ current.reason || '—' }}</el-descriptions-item>
        </el-descriptions>

        <template v-if="current.kind === 'create_card' && current.payload">
          <h4>{{ current.payload.title }}</h4>
          <p class="meta">
            领域 {{ current.payload.domain }} / 主题 {{ current.payload.topic }}
            <el-tag v-for="t in current.payload.tags || []" :key="t" size="small" style="margin-left: 4px">{{ t }}</el-tag>
          </p>
          <h4>答题要点（{{ current.payload.points?.length || 0 }} 条）</h4>
          <ol class="points"><li v-for="(p, i) in current.payload.points" :key="i">{{ p }}</li></ol>
          <template v-if="current.payload.pitfalls?.length">
            <h4>常见坑</h4>
            <ul class="pitfalls"><li v-for="(p, i) in current.payload.pitfalls" :key="i">{{ p }}</li></ul>
          </template>
          <p class="meta">prompt 版本 v{{ current.payload.prompt_version || '?' }}｜来源题目 {{ current.payload.question_id || '—' }}</p>
        </template>

        <pre v-else class="raw">{{ JSON.stringify(current.payload, null, 2) }}</pre>

        <div v-if="current.status === 'pending'" style="margin-top: 16px">
          <el-button type="success" @click="decide(current, true)">批准并执行</el-button>
          <el-button type="danger" plain @click="decide(current, false)">驳回</el-button>
        </div>
        <pre v-if="current.result" class="raw">{{ JSON.stringify(current.result, null, 2) }}</pre>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const rows = ref([])
const loading = ref(false)
const tab = ref('pending')
const selected = ref([])
const drawer = ref(false)
const current = ref(null)

const kindType = (k) => ({ create_card: 'primary', boost_srs: 'warning', merge_card: 'info', archive_card: 'danger' }[k] || 'info')
const kindText = (k) => ({ create_card: '建卡', boost_srs: '提前复习', merge_card: '合并卡片', archive_card: '归档卡片' }[k] || k)
const statusType = (s) => ({ pending: 'warning', approved: 'success', rejected: 'info', failed: 'danger' }[s] || 'info')
const statusText = (s) => ({ pending: '待审批', approved: '已批准', rejected: '已驳回', failed: '执行失败' }[s] || s)

async function load() {
  loading.value = true
  try {
    rows.value = await api.get('/api/agent/actions', { params: tab.value ? { status: tab.value } : {} })
  } finally { loading.value = false }
}

function openDetail(row) {
  current.value = row
  drawer.value = true
}

async function decide(row, approve) {
  if (!approve) {
    await ElMessageBox.confirm(`驳回「${row.payload?.title || row.kind}」？`, '确认')
  }
  const res = await api.post(`/api/agent/actions/${row.id}/${approve ? 'approve' : 'reject'}`)
  if (res.status === 'failed') ElMessage.error(`执行失败：${res.error}`)
  else ElMessage.success(approve ? '已批准并执行' : '已驳回')
  drawer.value = false
  load()
}

async function batchApprove() {
  await ElMessageBox.confirm(`批量批准 ${selected.value.length} 个动作？建卡动作会立刻写入知识库。`, '确认')
  let ok = 0
  for (const row of selected.value) {
    const res = await api.post(`/api/agent/actions/${row.id}/approve`)
    if (res.status === 'approved') ok += 1
  }
  ElMessage.success(`已批准 ${ok} 个`)
  load()
}

onMounted(load)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.meta { color: #909399; font-size: 12px; }
.points { padding-left: 20px; line-height: 1.9; }
.pitfalls { padding-left: 20px; line-height: 1.9; color: #e6a23c; }
.raw { background: #f5f7fa; padding: 12px; border-radius: 6px; font-size: 12px; overflow: auto; }
</style>
