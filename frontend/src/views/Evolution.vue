<template>
  <div class="page">
    <div class="page-header">
      <h2>进化报告 · 项目该往哪走</h2>
      <el-button :icon="Refresh" circle @click="load" />
    </div>

    <el-alert v-if="error" :title="error" type="warning" :closable="false" style="margin-bottom: 16px" />

    <el-row :gutter="16">
      <el-col :span="6" v-for="c in cards" :key="c.label">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-value" :style="{ color: c.color }">{{ c.value }}</div>
          <div class="stat-label">{{ c.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>高频无卡题（freq≥2 且无卡片 = 最优先建卡）</template>
          <el-empty v-if="!evo.high_freq_uncarded?.length" description="暂无 — 好信号，说明高频题都有卡" :image-size="60" />
          <div v-for="q in evo.high_freq_uncarded" :key="q.qid" class="q-row">
            <el-tag size="small" type="danger">×{{ q.freq }}</el-tag>
            <span class="q-text">{{ q.text }}</span>
            <el-tag size="small" type="info">{{ q.topic }}</el-tag>
          </div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>薄弱卡排行（有卡但掌握度低）</template>
          <el-empty v-if="!evo.weak_cards?.length" description="暂无薄弱卡" :image-size="60" />
          <div v-for="c in evo.weak_cards" :key="c.id" class="q-row">
            <el-tag size="small" type="warning">掌握 {{ c.mastery }}</el-tag>
            <span class="q-text">{{ c.title }}</span>
            <el-tag size="small" type="info">{{ c.domain }}/{{ c.topic }}</el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" style="margin-top: 16px">
      <template #header>建议动作队列（按收益排序）</template>
      <el-empty v-if="!evo.actions?.length" description="投喂面经并生成作战地图后出现" :image-size="60" />
      <el-table :data="evo.actions" size="small">
        <el-table-column prop="priority" label="#" width="50" />
        <el-table-column prop="type" label="类型" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.type === 'create_card' ? 'danger' : 'warning'">
              {{ row.type === 'create_card' ? '建卡' : '复习' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="target" label="目标" min-width="280" show-overflow-tooltip />
        <el-table-column prop="freq" label="频次" width="70" align="center" />
        <el-table-column prop="why" label="理由" min-width="200" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'

const evo = ref({})
const error = ref('')

const cards = computed(() => [
  { label: '题目总数', value: evo.value.totals?.questions ?? 0, color: '#409eff' },
  { label: '活跃卡片', value: evo.value.totals?.cards ?? 0, color: '#67c23a' },
  { label: '题目覆盖率', value: pct(evo.value.totals?.coverage_rate), color: '#e6a23c' },
  { label: '建议动作', value: evo.value.actions?.length ?? 0, color: '#f56c6c' },
])

const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`)

async function load() {
  error.value = ''
  try { evo.value = await api.get('/api/evolution/report') } catch { error.value = '后端未连接' }
}

onMounted(load)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.stat-card { text-align: center; }
.stat-value { font-size: 28px; font-weight: 700; }
.stat-label { color: #606266; margin-top: 4px; }
.q-row { display: flex; align-items: center; gap: 8px; padding: 7px 0; border-bottom: 1px dashed #ebeef5; }
.q-row:last-child { border-bottom: none; }
.q-text { flex: 1; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
