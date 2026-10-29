<template>
  <div class="page">
    <div class="page-header">
      <h2>作战地图 · 面经覆盖分析</h2>
      <el-button :icon="Refresh" circle @click="load" />
    </div>

    <el-row :gutter="16">
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>面经列表</template>
          <el-empty v-if="!interviews.length" description="暂无面经，先去采集中心投喂" :image-size="60" />
          <div v-for="iv in interviews" :key="iv.id" class="iv-item" :class="{ selected: selected?.id === iv.id }"
               @click="select(iv)">
            <div class="iv-title">
              {{ iv.company || '(未识别公司)' }}
              <el-tag v-if="iv.has_coverage" size="small" type="success" style="margin-left: 6px">已分析</el-tag>
            </div>
            <div class="iv-meta">{{ iv.direction }} · {{ iv.n_questions }} 题 · {{ iv.created_at?.slice(0, 10) }}</div>
          </div>
        </el-card>
      </el-col>

      <el-col :span="16">
        <el-card shadow="never" v-loading="analyzing">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span>{{ selected ? `${selected.company || '面经'} 作战地图` : '作战地图' }}</span>
              <el-button v-if="selected" type="primary" size="small" @click="analyze">
                {{ selected.has_coverage ? '重新分析' : '生成作战地图' }}
              </el-button>
            </div>
          </template>

          <el-empty v-if="!selected" description="选择左侧面经" :image-size="60" />
          <template v-else-if="map">
            <el-row :gutter="12" style="margin-bottom: 16px">
              <el-col :span="6"><el-statistic title="题目数" :value="map.summary.questions" /></el-col>
              <el-col :span="6"><el-statistic title="预期得分(10分制)" :value="map.summary.expected_score ?? '—'" /></el-col>
              <el-col :span="6">
                <div class="dist">
                  <span>档位分布：</span>
                  <el-tag type="success" size="small">high {{ map.summary.band_dist.high }}</el-tag>
                  <el-tag type="warning" size="small">mid {{ map.summary.band_dist.mid }}</el-tag>
                  <el-tag type="danger" size="small">low {{ map.summary.band_dist.low }}</el-tag>
                </div>
              </el-col>
              <el-col :span="6" style="color:#909399;font-size:12px">
                分析时间：{{ map.analyzed_at }}
              </el-col>
            </el-row>

            <el-table :data="map.items" style="width:100%" size="small">
              <el-table-column type="index" width="42" />
              <el-table-column prop="text" label="题目" min-width="260" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ row.text }}
                  <el-tag v-if="row.freq > 1" size="small" type="danger" style="margin-left:4px">×{{ row.freq }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="predicted_band" label="预测" width="80" align="center">
                <template #default="{ row }">
                  <el-tag size="small" :type="bandType(row.predicted_band)">{{ row.predicted_band }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="gap" label="缺口" width="100" align="center">
                <template #default="{ row }">
                  <el-tag v-if="row.gap" size="small" :type="gapType(row.gap)">{{ row.gap }}</el-tag>
                  <span v-else style="color:#c0c4cc">—</span>
                </template>
              </el-table-column>
              <el-table-column label="命中卡片" min-width="150" show-overflow-tooltip>
                <template #default="{ row }">
                  <span v-if="row.card">{{ row.card.title }}（掌握 {{ row.card.mastery }}，相似 {{ row.card.similarity }}）</span>
                  <span v-else style="color:#f56c6c">无卡</span>
                </template>
              </el-table-column>
              <el-table-column prop="open_mistakes" label="未解决错题" width="96" align="center">
                <template #default="{ row }">
                  <el-tag v-if="row.open_mistakes" size="small" type="danger">{{ row.open_mistakes }}</el-tag>
                  <span v-else style="color:#c0c4cc">0</span>
                </template>
              </el-table-column>
              <el-table-column type="expand" label="要点" width="60">
                <template #default="{ row }">
                  <div style="padding: 8px 24px">
                    <template v-if="row.answer_points?.length">
                      <div v-for="(p, i) in row.answer_points" :key="i">• {{ typeof p === 'string' ? p : JSON.stringify(p) }}</div>
                    </template>
                    <span v-else style="color:#909399">无参考要点（可去知识库建卡）</span>
                  </div>
                </template>
              </el-table-column>
            </el-table>

            <template v-if="map.summary.gaps?.length">
              <h4 style="margin:16px 0 8px">缺口清单（建卡建议）</h4>
              <el-tag v-for="(g, i) in map.summary.gaps" :key="i" :type="gapType(g.gap)" style="margin: 0 8px 8px 0">
                {{ g.text.length > 30 ? g.text.slice(0, 30) + '…' : g.text }} · {{ g.gap }}{{ g.freq > 1 ? ` ×${g.freq}` : '' }}
              </el-tag>
            </template>
          </template>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const interviews = ref([])
const selected = ref(null)
const map = ref(null)
const analyzing = ref(false)

const bandType = (b) => ({ high: 'success', mid: 'warning', low: 'danger' }[b] || 'info')
const gapType = (g) => (g === '无卡·高频' ? 'danger' : g === '无卡' ? 'warning' : 'info')

async function load() {
  interviews.value = await api.get('/api/interviews')
  if (selected.value) {
    selected.value = interviews.value.find((i) => i.id === selected.value.id) || selected.value
  }
}

async function select(iv) {
  selected.value = iv
  map.value = null
  if (iv.has_coverage) {
    try { map.value = await api.get(`/api/coverage/${iv.id}`) } catch { /* analyzed_at 已刷新但缓存旧 */ }
  }
}

async function analyze() {
  analyzing.value = true
  try {
    map.value = await api.post(`/api/coverage/${selected.value.id}/analyze`)
    selected.value.has_coverage = true
    ElMessage.success('作战地图已生成')
  } finally { analyzing.value = false }
}

onMounted(load)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.iv-item { padding: 10px 12px; border-radius: 6px; cursor: pointer; border: 1px solid transparent; }
.iv-item:hover { background: #f5f7fa; }
.iv-item.selected { background: #ecf5ff; border-color: #b3d8ff; }
.iv-title { font-weight: 600; }
.iv-meta { color: #909399; font-size: 12px; margin-top: 4px; }
.dist { display: flex; gap: 4px; align-items: center; }
</style>
