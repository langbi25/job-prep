<template>
  <div class="page">
    <div class="page-header">
      <h2>MCP 看板</h2>
      <div>
        <el-button size="small" :loading="running" @click="runEval(false)">契约评测</el-button>
        <el-button type="primary" size="small" :loading="running" @click="runEval(true)">
          含 LLM 评测
        </el-button>
        <el-button :icon="Refresh" circle size="small" @click="load" />
      </div>
    </div>

    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>
        接入方式：{{ status.transports?.stdio }}（stdio）·
        {{ status.transports?.http }}（HTTP，需 MCP_TOKEN）·
        资源 {{ status.resources?.length }} 个 · 提示词 {{ status.prompts?.length }} 个
      </template>
    </el-alert>

    <!-- 总览 -->
    <div class="stats">
      <div class="stat">
        <div class="k">工具</div>
        <div class="v">{{ status.tools }}<span class="unit">个</span></div>
        <div class="s">只读 {{ status.read_only }} · 写 {{ status.write }}</div>
      </div>
      <div class="stat">
        <div class="k">累计调用</div>
        <div class="v">{{ stats.total }}<span class="unit">次</span></div>
        <div class="s">今日 {{ stats.today }} 次</div>
      </div>
      <div class="stat">
        <div class="k">成功率</div>
        <div class="v" :class="rateClass(stats.ok_rate)">{{ pct(stats.ok_rate) }}</div>
        <div class="s">p50 {{ stats.p50_ms }}ms · p95 {{ stats.p95_ms }}ms</div>
      </div>
      <div class="stat">
        <div class="k">工具选择准确率</div>
        <div class="v" :class="rateClass(evalSel?.tool_accuracy)">{{ pct(evalSel?.tool_accuracy) }}</div>
        <div class="s">{{ evalSel ? `${evalSel.tool_ok}/${evalSel.total} 用例` : '未跑选择档' }}</div>
      </div>
    </div>

    <el-tabs v-model="tab">
      <!-- 工具目录 -->
      <el-tab-pane label="工具目录" name="tools">
        <el-table :data="toolRows" size="small" v-loading="loading">
          <el-table-column prop="name" label="工具" width="170">
            <template #default="{ row }"><code>{{ row.name }}</code></template>
          </el-table-column>
          <el-table-column label="类型" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.read_only ? 'success' : 'warning'" effect="plain">
                {{ row.read_only ? '只读' : '写' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="summary" label="说明" min-width="320" show-overflow-tooltip />
          <el-table-column prop="calls" label="调用" width="70" align="center" />
          <el-table-column label="成功率" width="80" align="center">
            <template #default="{ row }">{{ row.ok_rate == null ? '—' : pct(row.ok_rate) }}</template>
          </el-table-column>
          <el-table-column label="p95" width="80" align="center">
            <template #default="{ row }">{{ row.calls ? row.p95_ms + 'ms' : '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="80">
            <template #default="{ row }">
              <el-button size="small" text type="primary" :disabled="!row.read_only"
                         @click="openTry(row)">试跑</el-button>
            </template>
          </el-table-column>
        </el-table>
        <p class="hint">写工具不允许在页面试跑——会写审批队列，试写请走审批中心正常流程。</p>
      </el-tab-pane>

      <!-- 调用日志 -->
      <el-tab-pane label="调用日志" name="logs">
        <div class="toolbar">
          <el-select v-model="fTool" placeholder="全部工具" clearable size="small" style="width: 160px">
            <el-option v-for="t in toolRows" :key="t.name" :label="t.name" :value="t.name" />
          </el-select>
          <el-select v-model="fClient" placeholder="全部来源" clearable size="small" style="width: 150px">
            <el-option v-for="(n, c) in stats.clients" :key="c" :label="`${c}（${n}）`" :value="c" />
          </el-select>
          <el-select v-model="fStatus" placeholder="全部状态" clearable size="small" style="width: 120px">
            <el-option label="成功" value="ok" />
            <el-option label="失败" value="error" />
          </el-select>
          <span class="hint">共 {{ filteredCalls.length }} 条（最多显示最近 60 条）</span>
        </div>
        <el-table :data="filteredCalls" size="small" style="margin-top: 8px">
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="expand">
                <div><b>入参</b><pre>{{ row.args_json }}</pre></div>
                <div v-if="row.error"><b>错误</b><pre class="err">{{ row.error }}</pre></div>
                <div><b>返回预览</b><pre>{{ row.result_preview }}</pre></div>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="ts" label="时间" width="160" />
          <el-table-column prop="tool" label="工具" width="160" />
          <el-table-column prop="client" label="来源" width="110" />
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'ok' ? 'success' : 'danger'" effect="plain">
                {{ row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="latency_ms" label="耗时" width="90" align="right">
            <template #default="{ row }">{{ row.latency_ms }} ms</template>
          </el-table-column>
          <el-table-column prop="result_chars" label="返回字符" width="100" align="right" />
        </el-table>
      </el-tab-pane>

      <!-- 评测 -->
      <el-tab-pane label="评测明细" name="eval">
        <template v-if="evalReport">
          <div class="eval-head">
            <span class="hint">{{ evalReport.file }} · {{ evalReport.timestamp }}</span>
            <span v-if="evalReport.skipped" class="skip">{{ evalReport.skipped }}</span>
          </div>
          <div class="stats eval">
            <div class="stat">
              <div class="k">契约通过率</div>
              <div class="v" :class="rateClass(evalReport.contract?.ok_rate)">
                {{ pct(evalReport.contract?.ok_rate) }}
              </div>
              <div class="s">{{ evalReport.contract?.ok }}/{{ evalReport.contract?.total }} ·
                p95 {{ evalReport.contract?.p95_ms }}ms</div>
            </div>
            <div class="stat">
              <div class="k">工具选择准确率</div>
              <div class="v" :class="rateClass(evalSel?.tool_accuracy)">{{ pct(evalSel?.tool_accuracy) }}</div>
              <div class="s">{{ thr('tool_accuracy') }}</div>
            </div>
            <div class="stat">
              <div class="k">参数准确率</div>
              <div class="v" :class="rateClass(evalSel?.arg_accuracy)">{{ pct(evalSel?.arg_accuracy) }}</div>
              <div class="s">{{ thr('arg_accuracy') }}</div>
            </div>
            <div class="stat">
              <div class="k">越权调用</div>
              <div class="v" :class="(evalSel?.overreach || 0) ? 'bad' : 'good'">
                {{ evalSel ? evalSel.overreach : '—' }}
              </div>
              <div class="s">只读请求误用写工具，上限 0</div>
            </div>
          </div>

          <el-table v-if="evalSel" :data="evalSel.cases" size="small" style="margin-top: 12px">
            <el-table-column prop="case" label="用例" width="90" />
            <el-table-column prop="request" label="请求" min-width="260" show-overflow-tooltip />
            <el-table-column label="期望" width="130">
              <template #default="{ row }">{{ row.expect || '（不调工具）' }}</template>
            </el-table-column>
            <el-table-column label="实际" width="130">
              <template #default="{ row }">
                <span :class="{ bad: !row.tool_ok }">{{ row.got || '（未调）' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="参数" width="80" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.args_ok === true" size="small" type="success" effect="plain">对</el-tag>
                <el-tag v-else-if="row.args_ok === false" size="small" type="danger" effect="plain">错</el-tag>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column label="端到端" width="90" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.e2e_ok === true" size="small" type="success" effect="plain">通</el-tag>
                <el-tag v-else-if="row.e2e_ok === false" size="small" type="danger" effect="plain">断</el-tag>
                <span v-else>—</span>
              </template>
            </el-table-column>
            <el-table-column label="结果" width="70">
              <template #default="{ row }">
                <el-tag size="small" :type="row.tool_ok ? 'success' : 'danger'" effect="plain">
                  {{ row.tool_ok ? '对' : '错' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
          <p v-else class="hint">
            选择档没跑：契约档只验接口能不能跑通，「模型选得对不对」必须让模型在环里跑一遍才有数。
          </p>
        </template>
        <el-empty v-else description="还没有评测结果，点右上角「契约评测」跑一次" />
      </el-tab-pane>
    </el-tabs>

    <!-- 试跑 -->
    <el-dialog v-model="tryOpen" :title="`试跑 · ${tryTool?.name}`" width="680px">
      <p class="hint" style="margin-top: 0">{{ tryTool?.summary }}</p>
      <el-input v-model="tryArgs" type="textarea" :rows="6" spellcheck="false" />
      <div v-if="tryResult" class="try-result">
        <div class="try-meta">
          <el-tag size="small" :type="tryResult.ok ? 'success' : 'danger'" effect="plain">
            {{ tryResult.ok ? '成功' : '失败' }}
          </el-tag>
          <span class="hint">{{ tryResult.latency_ms }} ms · {{ tryResult.chars }} 字符</span>
        </div>
        <pre>{{ tryResult.payload }}</pre>
      </div>
      <template #footer>
        <el-button @click="tryOpen = false">关闭</el-button>
        <el-button type="primary" :loading="trying" @click="doTry">执行</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const loading = ref(false)
const running = ref(false)
const trying = ref(false)
const status = ref({})
const tools = ref([])
const stats = ref({ by_tool: [], calls: [], clients: {} })
const evalReport = ref(null)
const tab = ref('tools')

const fTool = ref('')
const fClient = ref('')
const fStatus = ref('')

const tryOpen = ref(false)
const tryTool = ref(null)
const tryArgs = ref('{}')
const tryResult = ref(null)

const evalSel = computed(() => evalReport.value?.selection || null)

// 工具目录 + 调用统计合并：目录是「有哪些能力」，统计是「实际用得怎么样」
const toolRows = computed(() => {
  const byTool = Object.fromEntries((stats.value.by_tool || []).map((t) => [t.tool, t]))
  return (tools.value || []).map((t) => ({
    ...t, ...(byTool[t.name] || { calls: 0, ok_rate: null, p95_ms: 0 }),
  }))
})

const filteredCalls = computed(() => (stats.value.calls || []).filter((c) =>
  (!fTool.value || c.tool === fTool.value)
  && (!fClient.value || c.client === fClient.value)
  && (!fStatus.value || c.status === fStatus.value)))

function pct(v) {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`
}

function rateClass(v) {
  if (v == null) return ''
  return v >= 0.9 ? 'good' : v >= 0.75 ? 'warn' : 'bad'
}

function thr(key) {
  const t = evalReport.value?.thresholds?.[key]
  return t == null ? '' : `阈值 ≥ ${(t * 100).toFixed(0)}%`
}

async function load() {
  loading.value = true
  try {
    const res = await api.get('/api/mcp/board')
    status.value = res.status
    tools.value = res.tools
    stats.value = res.stats
    evalReport.value = res.eval
  } finally { loading.value = false }
}

function openTry(row) {
  tryTool.value = row
  tryArgs.value = JSON.stringify(row.example || {}, null, 2)
  tryResult.value = null
  tryOpen.value = true
}

async function doTry() {
  let args
  try {
    args = JSON.parse(tryArgs.value || '{}')
  } catch {
    ElMessage.error('参数不是合法 JSON')
    return
  }
  trying.value = true
  try {
    tryResult.value = await api.post('/api/mcp/try', { tool: tryTool.value.name, args })
    load()  // 试跑会产一条调用记录，顺手刷新统计
  } finally { trying.value = false }
}

async function runEval(withLlm) {
  running.value = true
  try {
    const res = await api.post('/api/mcp/eval', { with_llm: withLlm })
    stats.value = res.stats
    evalReport.value = res.eval
    tab.value = 'eval'
    const c = res.eval.contract
    ElMessage.success(
      `契约 ${c.ok}/${c.total}${res.eval.selection ? `，选择准确率 ${pct(res.eval.selection.tool_accuracy)}` : ''}`)
  } finally { running.value = false }
}

onMounted(load)
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.page-header h2 { margin: 0; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 12px; }
.stat { border: 1px solid #ebeef5; border-radius: 8px; padding: 12px 14px; background: #fff; }
.stat .k { font-size: 12px; color: #909399; }
.stat .v { font-size: 24px; font-weight: 600; line-height: 1.4; }
.stat .v .unit { font-size: 12px; font-weight: 400; color: #909399; margin-left: 2px; }
.stat .s { font-size: 12px; color: #909399; }
.stat.eval .v { font-size: 20px; }
.good { color: #67c23a; } .warn { color: #e6a23c; } .bad { color: #f56c6c; } .skip { color: #e6a23c; }
.hint { color: #909399; font-size: 12px; }
.toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.expand { padding: 4px 16px 8px 48px; font-size: 12px; }
.expand pre { white-space: pre-wrap; word-break: break-all; background: #f5f7fa;
  padding: 8px; border-radius: 6px; margin: 4px 0 8px; max-height: 240px; overflow: auto; }
.expand .err { color: #f56c6c; }
.eval-head { display: flex; gap: 12px; align-items: center; margin-bottom: 8px; }
.try-result { margin-top: 12px; }
.try-meta { display: flex; gap: 10px; align-items: center; margin-bottom: 6px; }
.try-result pre { white-space: pre-wrap; word-break: break-all; background: #f5f7fa;
  padding: 10px; border-radius: 6px; max-height: 360px; overflow: auto; font-size: 12px; }
code { font-family: var(--font-mono, monospace); }
</style>
