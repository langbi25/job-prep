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
        <span style="color:#909399;font-size:12px;margin-left:12px">投喂后点「提取」由 LLM 结构化，再「确认」入池</span>
      </div>
    </el-card>

    <el-tabs v-model="tab" @tab-change="load">
      <el-tab-pane label="待处理" name="pending" />
      <el-tab-pane label="已确认" name="confirmed" />
      <el-tab-pane label="已丢弃" name="discarded" />
      <el-tab-pane label="全部" name="all" />
    </el-tabs>

    <el-table :data="rows" v-loading="loading" style="width: 100%">
      <el-table-column prop="id" label="ID" width="150" />
      <el-table-column prop="source_type" label="来源" width="90" />
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="时间" width="170" />
      <el-table-column prop="error" label="备注" show-overflow-tooltip />
      <el-table-column label="操作" width="230" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :disabled="row.status !== 'pending'" :loading="busy === row.id"
                     @click="extract(row.id)">提取</el-button>
          <el-button size="small" type="success" :disabled="row.status !== 'extracted'" @click="confirm(row.id)">确认</el-button>
          <el-button size="small" type="danger" plain :disabled="row.status === 'discarded'" @click="discard(row.id)">丢弃</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const rows = ref([])
const text = ref('')
const tab = ref('all')
const loading = ref(false)
const creating = ref(false)
const busy = ref('')

const statusType = (s) => ({ pending: 'warning', extracted: 'primary', confirmed: 'success', discarded: 'info', error: 'danger' }[s] || 'info')

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
    ElMessage.success('投喂成功，可点击「提取」')
    text.value = ''
    load()
  } finally { creating.value = false }
}

async function extract(id) {
  busy.value = id
  try {
    await api.post(`/api/ingests/${id}/extract`)
    ElMessage.success('提取完成')
    load()
  } finally { busy.value = '' }
}

async function confirm(id) {
  await ElMessageBox.confirm('确认入池？将生成面经实体并把题目并入题目池', '确认')
  await api.post(`/api/ingests/${id}/confirm`, {})
  ElMessage.success('已确认入池')
  load()
}

async function discard(id) {
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
