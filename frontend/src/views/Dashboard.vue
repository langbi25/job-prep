<template>
  <div class="page">
    <div class="page-header">
      <h2>总览 · 飞轮状态</h2>
      <el-button :icon="Refresh" circle @click="load" />
    </div>

    <el-row :gutter="16">
      <el-col :span="6" v-for="m in statCards" :key="m.label">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-value" :style="{ color: m.color }">{{ m.value }}</div>
          <div class="stat-label">{{ m.label }}</div>
          <div class="stat-sub">{{ m.sub }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header>飞轮三环</template>
          <div class="loops">
            <div class="loop" :class="{ active: m.interviews > 0 }">
              <b>① 采集环</b>
              <span>{{ m.interviews }} 场面经 → {{ m.questions }} 道题</span>
              <el-tag size="small" :type="m.interviews > 0 ? 'success' : 'info'">{{ m.interviews > 0 ? '运转中' : '未启动' }}</el-tag>
            </div>
            <div class="loop" :class="{ active: m.cards > 0 }">
              <b>② 知识环</b>
              <span>{{ m.cards }} 张卡 · 覆盖率 {{ pct(m.coverage_rate) }}</span>
              <el-tag size="small" :type="m.cards > 0 ? 'success' : 'info'">{{ m.cards > 0 ? '运转中' : '未启动' }}</el-tag>
            </div>
            <div class="loop" :class="{ active: m.outcomes > 0 }">
              <b>③ 校准环</b>
              <span>{{ m.outcomes }} 次真实回录 · 预测命中 {{ m.prediction_hit_rate == null ? '—' : pct(m.prediction_hit_rate) }}（样本 {{ m.prediction_samples }}）</span>
              <el-tag size="small" :type="m.outcomes > 0 ? 'success' : 'info'">{{ m.outcomes > 0 ? '运转中' : '未启动' }}</el-tag>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>进化方向（最高优先级动作）</template>
          <el-empty v-if="!actions.length" description="投喂面经并生成作战地图后出现" :image-size="60" />
          <div v-for="a in actions" :key="a.priority" class="action-row">
            <el-tag size="small" :type="a.type === 'create_card' ? 'danger' : 'warning'">
              {{ a.type === 'create_card' ? '建卡' : '复习' }}
            </el-tag>
            <span class="action-target">{{ a.target }}</span>
            <span class="action-why">{{ a.why }}</span>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'

const m = ref({})
const actions = ref([])
const error = ref('')

const statCards = computed(() => [
  { label: '面经 / 题目', value: `${m.value.interviews ?? 0} / ${m.value.questions ?? 0}`, sub: '采集环吞吐', color: '#409eff' },
  { label: '知识卡片', value: m.value.cards ?? 0, sub: 'Markdown 唯一事实源', color: '#67c23a' },
  { label: '题目覆盖率', value: pct(m.value.coverage_rate), sub: '有卡题目占比', color: '#e6a23c' },
  { label: '高频无卡题', value: m.value.high_freq_uncarded ?? 0, sub: '最优先进化信号', color: '#f56c6c' },
])

function pct(x) { return x == null ? '—' : `${Math.round(x * 100)}%` }

async function load() {
  error.value = ''
  try {
    m.value = await api.get('/api/flywheel/metrics')
    const evo = await api.get('/api/evolution/report')
    actions.value = (evo.actions || []).slice(0, 6)
  } catch { error.value = '后端未连接，请先启动 API 服务 (端口 8765)' }
}

onMounted(load)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.stat-card { text-align: center; }
.stat-value { font-size: 30px; font-weight: 700; }
.stat-label { color: #606266; margin-top: 6px; }
.stat-sub { color: #909399; font-size: 12px; margin-top: 2px; }
.loops { display: flex; flex-direction: column; gap: 12px; }
.loop { display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-radius: 8px; background: #f5f7fa; color: #909399; }
.loop.active { background: #f0f9eb; color: #303133; }
.loop b { width: 70px; }
.loop span { flex: 1; font-size: 13px; }
.action-row { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-bottom: 1px dashed #ebeef5; }
.action-row:last-child { border-bottom: none; }
.action-target { font-size: 13px; max-width: 55%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.action-why { color: #909399; font-size: 12px; }
</style>
