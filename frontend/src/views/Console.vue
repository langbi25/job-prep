<template>
  <div class="page">
    <header class="bar">
      <div class="bar-l">
        <span class="title">Agent</span>
        <el-tag size="small" effect="plain" :type="mode === 'auto' ? 'success' : 'info'">
          {{ mode === 'auto' ? '可写' : '只读' }}
        </el-tag>
        <span class="hint hide-sm">
          它自己决定调哪些工具、连着做完一件事；写操作受右侧模式约束
        </span>
      </div>
      <div class="bar-r">
        <el-radio-group v-model="mode" size="small" @change="onModeChange">
          <el-radio-button value="auto">自动执行</el-radio-button>
          <el-radio-button value="assist">只查不改</el-radio-button>
        </el-radio-group>
        <el-button size="small" :icon="Plus" @click="newSession">新对话</el-button>
        <el-button size="small" :icon="Clock" circle title="历史会话" @click="openSessions" />
      </div>
    </header>

    <div ref="scroller" class="stream">
      <!-- 空态：给出可点的起点，而不是一个空白输入框 -->
      <div v-if="!messages.length && !busy" class="welcome">
        <h3>直接说你要干什么，我连着做完</h3>
        <p class="hint">
          我不只是问答：投喂面经时我会存档 → 抽题 → 过质量闸门 → 入池 → 起草待审卡，
          一整套跑完再汇报。知识卡的最后一步永远停在审批中心，等你批。
        </p>
        <div class="chips">
          <div v-for="c in starters" :key="c.label" class="chip" @click="useStarter(c)">
            <div class="chip-t">{{ c.label }}</div>
            <div class="chip-s">{{ c.desc }}</div>
          </div>
        </div>
        <p class="hint" v-if="mode === 'assist'">
          当前是「只查不改」：我会照常分析和给结论，但入池、起草卡、跑地图都会被拒绝执行。
        </p>
      </div>

      <div v-for="(m, i) in messages" :key="i" :class="['row', m.role]">
        <div class="bubble" :class="{ err: m.error }">
          <div v-if="m.role === 'assistant'" class="who">
            <span>Agent</span>
            <span v-if="m.mode" class="tag">{{ m.mode === 'auto' ? '自动' : '只读' }}</span>
            <span v-if="m.created_at" class="time">{{ (m.created_at || '').slice(11, 16) }}</span>
          </div>
          <div class="text">{{ m.content }}</div>

          <div v-if="m.trace && m.trace.length" class="steps">
            <div class="steps-head" @click="toggleSteps(i)">
              <span class="caret">{{ opened[i] ? '▾' : '▸' }}</span>
              {{ m.trace.length }} 步工具调用
              <span class="ok-n">{{ okCount(m.trace) }} 成功</span>
              <span v-if="failCount(m.trace)" class="bad-n">{{ failCount(m.trace) }} 失败</span>
              <span class="hint-inline">{{ opened[i] ? '收起' : '展开看轨迹' }}</span>
            </div>
            <div v-if="opened[i]" class="steps-body">
              <div v-for="(s, j) in m.trace" :key="j" class="step">
                <div class="step-row" @click="toggleStep(i + '-' + j)">
                  <span class="dot" :class="s.ok ? 'ok' : 'bad'"></span>
                  <span class="tname">{{ s.tool }}</span>
                  <span class="targs">{{ argsText(s.args) }}</span>
                  <span class="iter">第 {{ (s.iter ?? 0) + 1 }} 轮</span>
                </div>
                <pre v-if="stepOpen[i + '-' + j]" class="prev">{{ s.preview }}</pre>
              </div>
            </div>
          </div>

          <div v-if="m.ctx && m.ctx.total" class="ctxline">
            <el-tooltip placement="top" :show-after="200">
              <template #content>
                <div class="ctxtip">
                  system {{ m.ctx.sections.system }} · 摘要 {{ m.ctx.sections.summary }} ·
                  检索 {{ m.ctx.sections.retrieval }} · 历史 {{ m.ctx.sections.history }} ·
                  输入 {{ m.ctx.sections.user }}<br />
                  丢弃旧消息 {{ m.ctx.dropped }} 条 · 锚定 {{ m.ctx.anchored }} 轮 ·
                  检索命中 {{ m.ctx.retrieved_n }} 条
                </div>
              </template>
              <span>
                输入 {{ m.ctx.total }} / 预算 {{ m.ctx.budget }} tokens
                <span v-if="m.ctx.summary_used"> · 含历史摘要</span>
                <span v-if="m.ctx.degraded && m.ctx.degraded.length" class="deg">
                  · 降级 {{ m.ctx.degraded.join(' / ') }}
                </span>
              </span>
            </el-tooltip>
          </div>
          <div v-if="arts(m).length" class="arts">
            <el-button v-for="a in arts(m)" :key="a.url" size="small" plain type="primary"
                       @click="go(a.url)">{{ a.label }} →</el-button>
          </div>
        </div>
      </div>

      <div v-if="busy" class="row assistant">
        <div class="bubble thinking">
          <span class="pulse"></span>
          {{ busyText }}
          <div class="hint" style="margin-top: 6px">一轮可能要 20–60 秒（多次模型往返 + 抽取），别关页面</div>
        </div>
      </div>
    </div>

    <footer class="composer">
      <el-input v-model="draft" type="textarea" :autosize="{ minRows: 2, maxRows: 8 }"
                resize="none" :disabled="busy"
                placeholder="粘贴一份面经 / 问'我今天该复习什么' / 说'把缺卡的题起草成卡'（Enter 发送，Shift+Enter 换行）"
                @keydown.enter="onEnter" />
      <div class="composer-side">
        <div class="chars" :class="{ warn: draft.length > 15000 }">{{ draft.length }}</div>
        <el-button type="primary" :loading="busy" :disabled="!draft.trim()" @click="send">
          发送
        </el-button>
      </div>
    </footer>

    <el-drawer v-model="sessionsOpen" title="历史会话" size="380px">
      <div v-if="!sessions.length" class="hint">还没有对话记录</div>
      <div v-for="s in sessions" :key="s.session_id" class="sess" @click="loadSession(s.session_id)">
        <div class="sess-t">{{ s.title }}</div>
        <div class="hint">{{ s.n }} 条 · {{ (s.last_at || '').slice(5, 16).replace('T', ' ') }}</div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { nextTick, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Clock, Plus } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const router = useRouter()

const messages = ref([])
const draft = ref('')
const busy = ref(false)
const busyText = ref('Agent 正在处理…')
const sessionId = ref('')
const sessions = ref([])
const sessionsOpen = ref(false)

const mode = ref(localStorage.getItem('console_mode') || 'auto')
const opened = reactive({})     // 折叠面板：每条消息的轨迹
const stepOpen = reactive({})   // 每条 step 的入参/结果预览
const scroller = ref(null)

const starters = [
  { label: '收一份面经', desc: '粘原文给它，我一路跑到待审卡', text: '' },
  { label: '我今天该复习什么', desc: '查到期卡 / 错题 / 薄弱主题后出计划', text: '我今天该复习什么？按优先级给我一份能落地的计划。' },
  { label: '给缺卡的题起草知识卡', desc: '挑高频无卡题，起草后进审批', text: '把题目池里还没有卡的高频题起草成知识卡，最多 3 张。' },
  { label: '这份面经我缺哪些', desc: '跑作战地图，逐题看覆盖与缺口', text: '用最近一份面经跑一次作战地图，告诉我最该补的 3 个缺口。' },
]

const ART_MAP = [
  { tools: ['ingest_text', 'confirm_ingest', 'list_recent_ingests'], label: '看采集详情', url: '#/ingests' },
  { tools: ['analyze_coverage'], label: '看作战地图', url: '#/coverage' },
  { tools: ['draft_cards', 'list_pending_actions'], label: '去审批中心', url: '#/approvals' },
  { tools: ['make_study_plan'], label: '去总览', url: '#/dashboard' },
]

function okCount(trace) { return trace.filter((s) => s.ok).length }
function failCount(trace) { return trace.filter((s) => !s.ok).length }

function argsText(args) {
  if (!args) return ''
  const parts = Object.entries(args).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
  const s = parts.join(' ')
  return s.length > 70 ? s.slice(0, 70) + '…' : s
}

// 与后端 _artifacts 同规则，但由前端按 trace 复算：历史消息也能点得通
function arts(m) {
  if (m.role !== 'assistant' || !m.trace || !m.trace.length) return []
  const names = new Set(m.trace.map((s) => s.tool))
  const out = []
  for (const a of ART_MAP) if (a.tools.some((t) => names.has(t))) out.push(a)
  if (names.has('ingest_text') && !out.find((x) => x.url === '#/approvals')) {
    out.push({ label: '去审批中心', url: '#/approvals' })
  }
  return out
}

function go(url) { router.push(url.replace(/^#/, '')) }

function toggleSteps(i) { opened[i] = !opened[i] }
function toggleStep(k) { stepOpen[k] = !stepOpen[k] }

function onModeChange(v) {
  localStorage.setItem('console_mode', v)
  ElMessage.success(v === 'auto' ? '已切到自动执行：写操作会真的落库' : '已切到只查不改：写工具会被拒绝')
}

function useStarter(c) {
  if (!c.text) {
    ElMessage.info('把面经原文粘进输入框，直接说「收进去」就行')
    return
  }
  draft.value = c.text
  send()
}

async function scrollBottom() {
  await nextTick()
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

async function send() {
  const text = draft.value.trim()
  if (!text || busy.value) return
  if (text.length > 20000) { ElMessage.error('单条消息最多 20000 字，请分段发'); return }

  messages.value.push({ role: 'user', content: text })
  draft.value = ''
  await scrollBottom()

  busy.value = true
  busyText.value = text.length > 2000 ? 'Agent 正在抽取并处理这份材料…' : 'Agent 正在处理…'
  try {
    const res = await api.post('/api/console/chat', {
      message: text, session_id: sessionId.value || undefined, mode: mode.value,
    })
    sessionId.value = res.session_id
    messages.value.push({
      role: 'assistant', content: res.reply, trace: res.trace || [],
      ctx: res.ctx || {}, mode: res.mode, created_at: new Date().toISOString(),
    })
  } catch (e) {
    const detail = e.response?.data?.detail
    messages.value.push({
      role: 'assistant', error: true,
      content: typeof detail === 'string' ? detail : '这一轮没跑通，看后端日志（消息已保留，可直接重发）',
    })
  } finally {
    busy.value = false
    await scrollBottom()
  }
}

function onEnter(e) {
  if (e.shiftKey) return            // Shift+Enter 换行
  e.preventDefault()
  send()
}

function newSession() {
  sessionId.value = ''
  messages.value = []
  Object.keys(opened).forEach((k) => delete opened[k])
  Object.keys(stepOpen).forEach((k) => delete stepOpen[k])
}

async function openSessions() {
  sessionsOpen.value = true
  sessions.value = await api.get('/api/console/sessions')
}

async function loadSession(sid) {
  const res = await api.get('/api/console/history', { params: { session_id: sid } })
  sessionId.value = sid
  messages.value = res.messages || []
  sessionsOpen.value = false
  await scrollBottom()
}

onMounted(async () => {
  // 进页面时接着上次的会话，避免"刷新就丢"
  try {
    const list = await api.get('/api/console/sessions', { params: { limit: 1 } })
    sessions.value = list
    if (list.length) await loadSession(list[0].session_id)
  } catch { /* 后端刚启动时忽略 */ }
})
</script>

<style scoped>
.page { display: flex; flex-direction: column; height: 100vh; background: #f7f8fa; }

.bar { display: flex; align-items: center; justify-content: space-between; gap: 12px;
       padding: 12px 20px; background: #fff; border-bottom: 1px solid #ebeef5; }
.bar-l { display: flex; align-items: center; gap: 10px; min-width: 0; }
.title { font-size: 16px; font-weight: 700; color: #1d2530; }
.bar-r { display: flex; align-items: center; gap: 8px; flex: none; }
.hint { color: #909399; font-size: 12px; line-height: 1.7; }
.hide-sm { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.stream { flex: 1; overflow-y: auto; padding: 20px 20px 8px; }

.welcome { max-width: 720px; margin: 3vh auto 0; }
.welcome h3 { margin: 0 0 8px; color: #1d2530; }
.chips { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }
.chip { background: #fff; border: 1px solid #ebeef5; border-radius: 10px; padding: 12px 14px;
        cursor: pointer; transition: all .15s; }
.chip:hover { border-color: #409eff; box-shadow: 0 2px 10px rgba(64, 158, 255, .12); }
.chip-t { font-size: 14px; font-weight: 600; color: #1d2530; margin-bottom: 4px; }
.chip-s { font-size: 12px; color: #909399; line-height: 1.6; }

.row { display: flex; margin-bottom: 14px; }
.row.user { justify-content: flex-end; }
.row.assistant { justify-content: flex-start; }
.bubble { max-width: min(760px, 86%); border-radius: 10px; padding: 12px 14px;
          background: #fff; border: 1px solid #ebeef5; }
.row.user .bubble { background: #409eff; border-color: #409eff; color: #fff; }
.bubble.err { border-color: #f56c6c; background: #fef0f0; color: #c45656; }
.who { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #909399; margin-bottom: 6px; }
.tag { background: #f0f2f5; border-radius: 4px; padding: 0 5px; }
.time { margin-left: auto; }
.text { font-size: 14px; line-height: 1.75; white-space: pre-wrap; word-break: break-word; }

.steps { margin-top: 10px; border-top: 1px dashed #ebeef5; padding-top: 8px; }
.steps-head { font-size: 12px; color: #606266; cursor: pointer; user-select: none;
              display: flex; align-items: center; gap: 8px; }
.caret { color: #909399; width: 10px; }
.ok-n { color: #67c23a; }
.bad-n { color: #f56c6c; }
.hint-inline { margin-left: auto; color: #c0c4cc; }
.steps-body { margin-top: 6px; }
.step { border-left: 2px solid #ebeef5; padding-left: 10px; margin-bottom: 6px; }
.step-row { display: flex; align-items: center; gap: 8px; font-size: 12px; cursor: pointer; }
.dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.dot.ok { background: #67c23a; }
.dot.bad { background: #f56c6c; }
.tname { font-family: ui-monospace, Menlo, Consolas, monospace; color: #1d2530; font-weight: 600; }
.targs { color: #909399; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.iter { margin-left: auto; color: #c0c4cc; flex: none; }
.prev { background: #1d2530; color: #d7dee8; border-radius: 6px; padding: 8px 10px; margin: 6px 0 0;
        font-size: 11px; line-height: 1.6; white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow: auto; }

.arts { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; }
.ctxline { margin-top: 8px; font-size: 11px; color: #b1b3b8; font-family: ui-monospace, Menlo, Consolas, monospace; }
.ctxline .deg { color: #e6a23c; }
.ctxtip { font-size: 12px; line-height: 1.6; }

.thinking { display: flex; flex-direction: column; color: #606266; font-size: 13px; }
.pulse { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #409eff;
         margin-right: 8px; animation: pulse 1.1s infinite ease-in-out; }
@keyframes pulse { 0%, 100% { opacity: .25; transform: scale(.8); } 50% { opacity: 1; transform: scale(1.15); } }

.composer { display: flex; gap: 10px; align-items: flex-end; padding: 12px 20px 16px;
            background: #fff; border-top: 1px solid #ebeef5; }
.composer-side { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; flex: none; }
.chars { font-size: 11px; color: #c0c4cc; }
.chars.warn { color: #e6a23c; }

.sess { padding: 10px 0; border-bottom: 1px solid #f2f3f5; cursor: pointer; }
.sess:hover .sess-t { color: #409eff; }
.sess-t { font-size: 13px; color: #303133; margin-bottom: 2px; }

@media (max-width: 1080px) {
  .chips { grid-template-columns: 1fr; }
  .hide-sm { display: none; }
}
</style>
