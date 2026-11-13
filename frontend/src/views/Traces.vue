<template>
  <div class="page">
    <div class="page-header">
      <div class="head-text">
        <h2>调用树</h2>
        <p class="sub">
          每一轮对话的完整 span 树，本地落库、不依赖外部后端。
          左边选一条 trace，右边看它内部怎么跑的：谁调了谁、慢在哪一层、哪一步报了错。
          点 <b>▸</b> 折叠子树，点整行看属性与事件；入参/返回的全文按需点开。
          带 <span class="orphan-mark">⚠</span> 的 span 是"父级没落库"的孤儿（多为崩在那一步之前）。
        </p>
      </div>
      <div class="head-actions">
        <el-select v-model="days" size="small" style="width: 100px" @change="loadList">
          <el-option :value="1" label="近 1 天" />
          <el-option :value="7" label="近 7 天" />
          <el-option :value="14" label="近 14 天" />
        </el-select>
        <el-button :icon="Refresh" circle :loading="loadingList" @click="reload" />
      </div>
    </div>

    <!-- 落库健康度：写失败非零时，下面所有东西都在悄悄丢样本，必须摆在最上面 -->
    <div class="health" :class="{ warn: writer.fails > 0 }">
      <span class="h-title">span 落库</span>
      <b class="h-num">{{ store.spans }}</b>
      <span class="h-hint">条 / {{ store.traces }} 条 trace · 均 {{ store.spans_per_trace }} 层/条</span>
      <span class="h-sep">·</span>
      <span class="h-hint">错误 span {{ store.error_spans }}</span>
      <span class="h-sep">·</span>
      <span class="h-hint">无根 trace {{ store.traces_without_root }}</span>
      <span class="h-sep">·</span>
      <span class="h-hint" :class="{ bad: writer.fails > 0 }">
        写失败 {{ writer.fails }}（{{ writer.writes }} 成功）
      </span>
      <span class="h-sep">·</span>
      <span class="h-hint" :class="{ bad: writer.blob_fails > 0 }">
        全文附件 {{ blobs.blobs }} 条 / {{ kb(blobs.gz_bytes) }}
        <template v-if="blobs.ratio">（压缩 {{ blobs.ratio }}×）</template>
      </span>
      <span v-if="!writer.enabled" class="h-off">已关闭（SPAN_PERSIST=0）</span>
    </div>

    <div class="body">
      <!-- 左：trace 列表 -->
      <div class="list">
        <div v-if="!traces.length" class="empty">
          这段时间还没有调用树。跑一轮对话就会出现在这里。
        </div>
        <div
          v-for="t in traces" :key="t.trace_id"
          class="item" :class="{ on: t.trace_id === curId, bad: t.error_spans > 0 }"
          @click="open(t.trace_id)">
          <div class="item-top">
            <span class="item-name">{{ t.name }}</span>
            <span class="item-ms">{{ fmtMs(t.duration_ms) }}</span>
          </div>
          <div class="item-meta">
            {{ (t.started || '').slice(5, 19).replace('T', ' ') }}
            · {{ t.span_count }} span
            <template v-if="t.error_spans"> · <span class="err">{{ t.error_spans }} 错误</span></template>
            <template v-if="t.session_id"> · {{ t.session_id.slice(0, 12) }}#{{ t.turn }}</template>
          </div>
        </div>
      </div>

      <!-- 右：瀑布图 -->
      <div class="detail">
        <div v-if="!tree" class="empty big">← 选一条 trace</div>
        <template v-else>
          <div class="d-head">
            <div class="d-title">
              {{ tree.root.name }}
              <span class="d-badge" :class="kindClass(tree.root.kind)">{{ tree.root.kind }}</span>
              <span v-if="tree.error_count" class="d-err">{{ tree.error_count }} 个 span 出错</span>
            </div>
            <div class="d-sub">
              总 {{ fmtMs(tree.total_ms) }} · {{ tree.span_count }} span
              <template v-if="tree.orphans"> · <span class="orphan-mark">{{ tree.orphans }} 个孤儿</span></template>
              <template v-if="tree.session_id"> · {{ tree.session_id }}#{{ tree.turn }}</template>
              · <code class="tid">{{ tree.trace_id.slice(0, 16) }}</code>
            </div>
          </div>

          <!-- 全文抽屉：按需取，不跟树一起传 -->
          <div v-if="blob" class="blob">
            <div class="blob-head">
              <span class="blob-title">
                <code>{{ blob.key }}</code>
                <span class="blob-meta">
                  {{ blob.len }} 字符
                  <template v-if="blob.truncated"> · 已截断（超过单条上限）</template>
                </span>
              </span>
              <button class="tb" @click="closeBlob()">收起</button>
            </div>
            <div v-if="blob.loading" class="muted">读取中…</div>
            <pre v-else class="blob-body">{{ blob.text }}</pre>
          </div>
          <div class="tree-bar">
            <button class="tb" @click="expandAll">全部展开</button>
            <button class="tb" @click="collapseAll">全部收起</button>
            <button class="tb" :class="{ on: onlyErr }" @click="focusErrors">
              只看错误分支
            </button>
            <span class="tb-hint">
              点 <b>▸</b> 折叠这棵子树 · 点整行看属性与事件
              <template v-if="collapsed.length"> · 已收起 {{ collapsed.length }} 个节点</template>
            </span>
          </div>

          <!-- 树内搜索：span 名/属性/事件都能搜，命中自动展开祖先。
               搜的是**整棵树**而不是当前可见行——折叠起来的部分不参与的话，
               "明明有却搜不到"会让人怀疑数据丢了。 -->
          <div class="tree-search">
            <input v-model="q" class="ts-in" type="search"
                   placeholder="在树里搜：span 名 / 属性值 / 事件（回车跳下一个）" />
            <template v-if="q">
              <span class="ts-n" :class="{ zero: !hitIds.length }">
                {{ hitIds.length ? hitIds.length + ' 个命中' : '没有命中' }}
              </span>
              <button v-if="hitIds.length" class="tb" @click="nextHit">下一个</button>
              <button class="tb" @click="q = ''">清除</button>
            </template>
            <span v-else class="ts-tip">搜 span 名、属性键与值、事件名与属性；不区分大小写</span>
          </div>

          <div class="wf-head">
            <span class="wf-c1">调用层级</span>
            <span class="wf-c2">时间轴（0 → {{ fmtMs(tree.total_ms) }}）</span>
            <span class="wf-c3">耗时</span>
          </div>

          <div class="wf">
            <template v-for="s in rows" :key="s.span_id">
              <div class="wf-row"
                   :class="{ open: openId === s.span_id, err: s.status === 'error',
                             hit: isHit(s), dim: !!q && !isHit(s), cur: s.span_id === curHitId }"
                   :data-sid="s.span_id"
                   @click="toggleDetail(s.span_id)">
                <div class="wf-name" :style="{ paddingLeft: (s.depth * 14 + 2) + 'px' }">
                  <!-- 层级引导线：树的形状靠竖线表达，光靠缩进看不出"跨了几层" -->
                  <i v-for="d in s.depth" :key="d" class="guide"
                     :style="{ left: (d - 1) * 14 + 2 + 'px' }"></i>
                  <span class="caret" :class="{ void: !s.children?.length }"
                        :title="s.children?.length ? (isCollapsed(s.span_id) ? '展开这棵子树' : '折叠这棵子树') : ''"
                        @click.stop="toggleBranch(s)">
                    {{ s.children?.length ? (isCollapsed(s.span_id) ? '▸' : '▾') : '·' }}
                  </span>
                  <span class="dot" :class="kindClass(s.kind)"></span>
                  <span class="nm">{{ s.name }}</span>
                  <span v-if="isCollapsed(s.span_id) && s.span_total > 1" class="fold-n"
                        title="已折叠的后代 span 数">
                    +{{ s.span_total - 1 }}
                    <b v-if="s.error_total" class="fold-err">{{ s.error_total }}!</b>
                  </span>
                  <span v-if="s.orphan" class="orphan-mark" title="父 span 未落库">⚠</span>
                </div>
                <div class="wf-track">
                  <div class="wf-bar" :class="kindClass(s.kind)"
                       :style="barStyle(s)" :title="`${s.offset_ms}ms 起，持续 ${s.duration_ms}ms`"></div>
                </div>
                <div class="wf-ms">{{ fmtMs(s.duration_ms) }}</div>
              </div>
              <div v-if="openId === s.span_id" class="wf-detail">
                <div v-if="s.error" class="d-error">{{ s.error }}</div>
                <div class="dgrid">
                  <div class="dk">属性</div>
                  <div class="dv">
                    <div v-if="!attrRows(s).length" class="muted">（无）</div>
                    <div v-for="a in attrRows(s)" :key="a.k" class="kv"
                         :class="{ clk: a.blob }"
                         @click="onAttrClick(s, a)">
                      <span class="k" :title="a.k">{{ a.k }}</span>
                      <!-- 全文入口固定在右侧一列，不跟着值往右挤：
                           长的 prompt 预览会换十几行，按钮要是跟在值末尾，
                           人得滚到底才找得到入口——等于没有入口。 -->
                      <button v-if="a.blob" class="link" :title="blobLen(a)"
                              @mousedown.prevent @click.stop="onAttrClick(s, a)">
                        全文
                      </button>
                      <span class="v" :class="{ long: String(a.v).length > 160 }">
                        {{ a.v }}
                      </span>
                    </div>
                    <div v-if="droppedOf(s)" class="muted drop-note">
                      预算裁剪：{{ droppedOf(s) }}（被裁的是预览，全文仍在附件里）
                    </div>
                  </div>
                </div>
                <div v-if="s.events?.length" class="dgrid">
                  <div class="dk">事件</div>
                  <div class="dv">
                    <!-- 事件属性逐条渲染：拼成一行的话，长的那条（工具入参、降级原因）
                         会把别的挤到看不见，也没法单独点开全文。 -->
                    <div v-for="(e, i) in s.events" :key="i" class="ev">
                      <div class="ev-h">+{{ e.t }}ms <b>{{ e.n }}</b></div>
                      <div v-for="(v, k) in (e.a || {})" :key="k" class="kv"
                           :class="{ clk: isBlobVal(v) }"
                           @click="openEventBlob(s, i, k)">
                        <span class="k" :title="String(k)">{{ k }}</span>
                        <button v-if="isBlobVal(v)" class="link" :title="blobLenOf(v)"
                                @mousedown.prevent @click.stop="openEventBlob(s, i, k)">
                          全文
                        </button>
                        <span class="v" :class="{ long: String(v).length > 160 }">{{ v }}</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </template>
          </div>

        </template>
      </div>
    </div>

    <!-- 慢调用聚合：单条慢是噪声，同一类反复慢才是问题 -->
    <div v-if="slow.length" class="slow">
      <div class="slow-head" @click="showSlow = !showSlow">
        <span class="caret">{{ showSlow ? '▾' : '▸' }}</span>
        最慢的调用（按 span 名聚合，{{ days }} 天）
        <span class="slow-hint">同一类调用反复出现在这里，才是要改的地方</span>
      </div>
      <table v-if="showSlow" class="slow-table">
        <thead>
          <tr><th>调用</th><th>类型</th><th>次数</th><th>平均</th><th>最长</th><th>错误</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in slow" :key="s.name">
            <td class="mono">{{ s.name }}</td>
            <td><span class="dot" :class="kindClass(s.kind)"></span> {{ s.kind }}</td>
            <td>{{ s.count }}</td>
            <td :class="{ hot: s.avg_ms > 2000 }">{{ fmtMs(s.avg_ms) }}</td>
            <td :class="{ hot: s.max_ms > 5000 }">{{ fmtMs(s.max_ms) }}</td>
            <td :class="{ err: s.errors }">{{ s.errors || '–' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'
import { ancestorsOf, matchIds } from '../treeSearch'

const route = useRoute()

const days = ref(7)
const loadingList = ref(false)
const traces = ref([])
const tree = ref(null)
const curId = ref('')
// 两套展开状态，刻意分开：
// - collapsed 控制**子树**折叠（树的结构）
// - openId 控制**当前行**的属性/事件详情（行的内容）
// 合成一个的话，想看一个 span 的属性就会顺手把它的子树也展开，
// 或者反过来——两者要独立，人才有"先看形状再看细节"的节奏。
const collapsed = ref([])
const openId = ref('')
const onlyErr = ref(false)
const blob = ref(null)
const store = ref({})
const writer = ref({})
const blobCache = ref({})
const blobs = ref({})
const slow = ref([])
const showSlow = ref(false)
// 树内搜索：q 是关键词，hitSeq 是"跳到第几个"的游标。
// 两者分开是因为同一个命中列表里要反复按回车走下一个，不能让 q 变。
const q = ref('')
const hitSeq = ref(0)

const KIND_CLASS = { AGENT: 'k-agent', CHAIN: 'k-chain', LLM: 'k-llm', TOOL: 'k-tool', RETRIEVER: 'k-ret' }

function kindClass(k) { return KIND_CLASS[k] || 'k-chain' }

function kb(n) {
  const v = Number(n) || 0
  return v >= 1048576 ? (v / 1048576).toFixed(1) + 'MB'
    : v >= 1024 ? (v / 1024).toFixed(0) + 'KB' : v + 'B'
}

function fmtMs(ms) {
  const v = Number(ms) || 0
  return v >= 1000 ? (v / 1000).toFixed(2) + 's' : v + 'ms'
}

function isCollapsed(id) { return collapsed.value.includes(id) }

// 深度优先展开成行，但遇到折叠的节点就不往下走。
// 这样"折叠"是渲染层的过滤，不是把树改掉——展开回来还是一样的结构。
const rows = computed(() => {
  const out = []
  const walk = (n) => {
    out.push(n)
    if (isCollapsed(n.span_id)) return
    ;(n.children || []).forEach(walk)
  }
  if (tree.value) walk(tree.value.root)
  return out
})

function toggleBranch(s) {
  if (!s.children?.length) return
  const i = collapsed.value.indexOf(s.span_id)
  if (i >= 0) collapsed.value.splice(i, 1)
  else collapsed.value.push(s.span_id)
}

function toggleDetail(id) {
  openId.value = openId.value === id ? '' : id
}

function allWithKids() {
  const ids = []
  const walk = (n) => {
    if (n.children?.length) ids.push(n.span_id)
    ;(n.children || []).forEach(walk)
  }
  if (tree.value) walk(tree.value.root)
  return ids
}

function collapseAll() { collapsed.value = allWithKids() }
function expandAll() { collapsed.value = [] }

// 只看错误分支：把"整棵子树都没错"的那些收起来，错误路径保持展开。
// 一轮 30 个 span 里通常只有 1~2 条路径出错，平铺着看要靠肉眼扫，收起来才看得见。
function focusErrors() {
  if (!tree.value) return
  if (onlyErr.value) { onlyErr.value = false; collapsed.value = []; return }
  if (!tree.value.error_count) {
    collapsed.value = []
    onlyErr.value = false
    return
  }
  const ids = []
  const walk = (n) => {
    if (n.children?.length && !n.error_total) ids.push(n.span_id)
    // 有错的子树要继续往下找，把里面没错的兄弟分支收掉
    ;(n.children || []).forEach(walk)
  }
  walk(tree.value.root)
  collapsed.value = ids
  onlyErr.value = true
}

// 0ms 的 span 也要看得见，否则"这步很快"会变成"这步不存在"
function barStyle(s) {
  const total = tree.value?.total_ms || 1
  const left = Math.min(99.5, (s.offset_ms / total) * 100)
  const w = Math.max(0.6, (s.duration_ms / total) * 100)
  return { left: left + '%', width: Math.min(100 - left, w) + '%' }
}

function short(v) {
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  return s && s.length > 300 ? s.slice(0, 300) + `…(+${s.length - 300})` : s
}

// ---------- 树内搜索 ----------
//
// 搜的是**整棵树**，不是当前渲染出来的行：折叠状态是渲染层过滤，
// 如果只在 rows 里搜，折叠起来的分支就等于"不存在"，人会以为数据丢了。
// 所以命中集合从 full tree 算，再把命中节点的祖先逐个展开——搜索结果要能被看见。
// 匹配规则本身在 treeSearch.js 里（有 node 侧断言，见 frontend/scripts）。

const hitIds = computed(() => matchIds(tree.value?.root, q.value))

const curHitId = computed(() => {
  const ids = hitIds.value
  if (!ids.length) return ''
  return ids[(Math.max(0, hitSeq.value - 1)) % ids.length]
})

function isHit(s) { return hitIds.value.includes(s.span_id) }

function revealHits() {
  const keep = ancestorsOf(tree.value?.root, hitIds.value)
  if (keep.size) collapsed.value = collapsed.value.filter((id) => !keep.has(id))
}

function nextHit() {
  const ids = hitIds.value
  if (!ids.length) return
  revealHits()
  const id = ids[hitSeq.value % ids.length]
  hitSeq.value += 1
  // 展开是下一帧才生效的（rows 是 computed），所以滚动要等一帧
  requestAnimationFrame(() => {
    const el = document.querySelector(`[data-sid="${id}"]`)
    if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  })
}

watch(q, () => {
  hitSeq.value = 0
  if (!q.value.trim()) return
  revealHits()
})

// 事件属性的全文：blob key 与后端约定为 e{事件下标}.{属性名}
const BLOB_MARK = /⟦全文 (\d+) 字符⟧/

function isBlobVal(v) { return typeof v === 'string' && BLOB_MARK.test(v) }
function blobLenOf(v) {
  const m = BLOB_MARK.exec(v || '')
  return m ? `${Number(m[1]).toLocaleString()} 字符` : ''
}
function openEventBlob(s, i, k) {
  if (!isBlobVal((s.events?.[i]?.a || {})[k])) return
  openBlob(s.span_id, `e${i}.${k}`)
}

// 属性列表：下划线开头的是 tracing 自己的元数据（哪几个键有全文、哪些被裁），
// 不是调用方打的点，混在属性里会让人以为业务真的记了这么个字段。
function attrRows(s) {
  const bk = String((s.attrs || {})._blob_keys || '').split(',').filter(Boolean)
  return Object.entries(s.attrs || {})
    .filter(([k]) => !k.startsWith('_'))
    .map(([k, v]) => {
      const full = typeof v === 'string' ? v : JSON.stringify(v)
      if (bk.includes(k)) {
        // 有全文附件的字段：预览只截前 240 字，**尾部那个「⟦全文 N 字符⟧」
        // 必须留着**——字符数是按钮上显示的"这段到底多大"，被 short() 切掉
        // 就只剩一个光秃秃的"看全文"，人不知道值不值得点。
        return { k, v: full.length > 240 ? full.slice(0, 240) + '…' : full, blob: true, full }
      }
      return { k, v: short(v), blob: false, full }
    })
}

function droppedOf(s) {
  return (s.attrs || {})._dropped_keys || ''
}

// 字符数就写在预览尾巴上（后端格式固定），不用为一个数字再发一次请求
function blobLen(a) {
  const m = /⟦全文 (\d+) 字符⟧/.exec(a.full || '')
  return m ? `${Number(m[1]).toLocaleString()} 字符` : ''
}

async function openBlob(spanId, key) {
  const ck = `${spanId}|${key}`
  if (blobCache.value[ck]) { blob.value = blobCache.value[ck]; return }
  blob.value = { key, loading: true, text: '', len: 0, truncated: false }
  try {
    const r = await api.get(`/api/traces/blob/${spanId}?key=${encodeURIComponent(key)}`)
    blobCache.value[ck] = { ...r, loading: false }
    // 请求回来时人可能已经点了别的，只有这条仍是"当前"才覆盖
    if (blob.value && blob.value.key === key) blob.value = blobCache.value[ck]
  } catch (e) {
    blob.value = { key, loading: false, text: `读取失败：${e.message || e}`, len: 0, truncated: false }
  }
}

function closeBlob() { blob.value = null }

// 点击属性行 / 全文按钮统一入口。放在函数里而不是模板里写 `a.blob && openBlob(...)`：
// 模板里的短路表达式出错时是静默的，函数里至少还能在这里拦住非法入参。
function onAttrClick(s, a) {
  if (!a || !a.blob) return
  openBlob(s.span_id, a.k)
}

async function loadList() {
  loadingList.value = true
  try {
    const [r, h, sl] = await Promise.all([
      api.get(`/api/traces/recent?days=${days.value}&limit=60`),
      api.get(`/api/traces/health?days=${days.value}`),
      api.get(`/api/traces/slow?days=${days.value}&limit=12`),
    ])
    traces.value = r.traces || []
    store.value = h.store || {}
    writer.value = h.writer || {}
    blobs.value = h.blobs || {}
    slow.value = sl.spans || []
    if (traces.value.length && !traces.value.some((t) => t.trace_id === curId.value)) {
      open(traces.value[0].trace_id)
    }
  } finally {
    loadingList.value = false
  }
}

async function open(id) {
  if (!id) return
  curId.value = id
  // 换一条 trace 就重置展开状态：上一棵树的 span_id 在新树里不存在，
  // 留着只会让"已收起 N 个节点"显示成一个对不上号的数字。
  collapsed.value = []
  openId.value = ''
  onlyErr.value = false
  blob.value = null
  // 搜索状态跟着换树清空：保留关键词只会让新树一进来就"全部变暗"，
  // 而人根本没在这条 trace 上搜过。
  q.value = ''
  hitSeq.value = 0
  tree.value = await api.get(`/api/traces/${id}`)
  // 拿到树之后再清一次：open() 可能被并发调用（列表自动选中 + ?trace= 定位），
  // 两个请求回来的先后不定，中间任何一次折叠都会被另一条 trace 的 span_id 污染。
  // 重置放在"树已就位"之后，才是确定的初始态。
  collapsed.value = []
}

function reload() { loadList() }

onMounted(async () => {
  await loadList()
  // 从回合质量页点"看调用树"会带 ?trace=xxx 过来，直接定位到那一轮
  const q = route.query.trace
  if (typeof q === 'string' && q) open(q)
})
</script>

<style scoped>
.head-text h2 { margin: 0 0 4px; }
.sub { margin: 0; color: #909399; font-size: 12px; line-height: 1.6; }
.head-actions { display: flex; align-items: center; gap: 8px; }

.health {
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px;
  font-size: 12px; color: #606266;
  padding: 7px 12px; margin-bottom: 12px;
  background: #f5f7fa; border: 1px solid #ebeef5; border-radius: 6px;
}
.health.warn { background: #fdf6ec; border-color: #f5dab1; }
.h-title { color: #909399; }
.h-num { font-size: 14px; color: #303133; }
.h-hint { color: #909399; }
.h-hint.bad { color: #e6a23c; font-weight: 600; }
.h-sep { color: #dcdfe6; }
.h-off { color: #e6a23c; }

.body { display: flex; gap: 14px; align-items: flex-start; }
@media (max-width: 900px) { .body { flex-direction: column; } }

.list {
  width: 268px; flex: none; max-height: 66vh; overflow-y: auto;
  border: 1px solid #ebeef5; border-radius: 6px; background: #fff;
}
@media (max-width: 900px) { .list { width: 100%; max-height: 240px; } }

.item { padding: 8px 10px; border-bottom: 1px solid #f5f7fa; cursor: pointer; }
.item:last-child { border-bottom: none; }
.item:hover { background: #fafcff; }
.item.on { background: #ecf5ff; box-shadow: inset 3px 0 0 #409eff; }
.item-top { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.item-name { font-size: 12px; color: #303133; font-weight: 500; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.item.bad .item-name { color: #e6a23c; }
.item-ms { font-size: 11px; color: #909399; flex: none; font-variant-numeric: tabular-nums; }
.item-meta { font-size: 11px; color: #b0b3b8; margin-top: 2px; }
.err { color: #f56c6c; }

.detail { flex: 1; min-width: 0; border: 1px solid #ebeef5; border-radius: 6px;
  background: #fff; padding: 12px 14px;
  /* 详情区自己滚：窗口不高时（笔记本 768px 常见）上面 header + 列表已经吃掉
     大半屏，树必须能在自己的框里滚到底，否则"底下那几个 span"永远看不见。 */
  max-height: calc(100vh - 240px); overflow-y: auto; }
.empty { padding: 26px 12px; text-align: center; color: #b0b3b8; font-size: 12px; }
.empty.big { padding: 90px 12px; }

.d-head { margin-bottom: 10px; }
.d-title { font-size: 14px; font-weight: 600; color: #303133; display: flex;
  align-items: center; gap: 8px; }
.d-badge { font-size: 10px; font-weight: 500; padding: 1px 6px; border-radius: 3px; color: #fff; }
.d-err { font-size: 11px; color: #f56c6c; font-weight: 400; }
.d-sub { font-size: 11px; color: #909399; margin-top: 4px; }
.tid { background: #f5f7fa; padding: 1px 4px; border-radius: 3px; font-size: 10px; }

.wf-head { display: flex; font-size: 11px; color: #b0b3b8; padding: 4px 0 6px;
  border-bottom: 1px solid #f0f2f5; }
.wf-c1 { width: 210px; flex: none; }
.wf-c2 { flex: 1; }
.wf-c3 { width: 62px; flex: none; text-align: right; }

.tree-bar { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.tb { font-size: 11px; color: #606266; background: #fff; border: 1px solid #dcdfe6;
  border-radius: 4px; padding: 3px 8px; cursor: pointer; line-height: 1.4; }
.tb:hover { color: #409eff; border-color: #b3d8ff; background: #ecf5ff; }
.tb.on { color: #409eff; border-color: #b3d8ff; background: #ecf5ff; }
.tb-hint { font-size: 11px; color: #b0b3b8; margin-left: 4px; }

/* 搜索条独立一行：和折叠按钮挤在一行时，输入框会被挤到换行，反而更难用 */
.tree-search { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
.ts-in { flex: 1; max-width: 380px; font-size: 12px; color: #303133;
  border: 1px solid #dcdfe6; border-radius: 4px; padding: 4px 8px; outline: none; }
.ts-in:focus { border-color: #409eff; }
.ts-n { font-size: 11px; color: #409eff; }
.ts-n.zero { color: #c0c4cc; }
.ts-tip { font-size: 11px; color: #c0c4cc; }

/* 做成有边框的小按钮而不是下划线文字：纯文字链接只有 13px 高，
   在会被换行的长属性值里既难点中，也分不清"这是值的一部分还是可点的"。 */
.link { flex: none; font-size: 11px; color: #409eff; background: #ecf5ff;
  border: 1px solid #b3d8ff; border-radius: 3px; padding: 1px 7px;
  cursor: pointer; line-height: 16px; }
.link:hover { color: #fff; background: #409eff; border-color: #409eff; }
.drop-note { margin-top: 4px; color: #e6a23c; }

.blob { margin-top: 10px; border: 1px solid #dcdfe6; border-radius: 6px;
  background: #fafbfc; }
.blob-head { display: flex; align-items: center; justify-content: space-between;
  padding: 6px 10px; border-bottom: 1px solid #ebeef5; }
.blob-title { font-size: 11px; color: #303133; display: flex; gap: 8px; align-items: baseline; }
.blob-title code { background: #fff; border: 1px solid #ebeef5; border-radius: 3px;
  padding: 1px 5px; font-size: 10px; }
.blob-meta { color: #909399; font-size: 11px; }
.blob-body { margin: 0; padding: 10px; max-height: 340px; overflow: auto;
  font-family: ui-monospace, Menlo, monospace; font-size: 11px; line-height: 1.6;
  color: #303133; white-space: pre-wrap; word-break: break-all; }

.wf-row { display: flex; align-items: center; height: 25px; cursor: pointer;
  border-radius: 3px; }
.wf-row:hover { background: #fafcff; }
/* 搜索结果：命中高亮、未命中压暗但**不隐藏**——树的结构本身是信息，
   把行删掉就看不出"这个慢 span 是在哪一层"了。当前那个命中再叠一层边框，
   否则连着滚过去根本分不清停在哪一个。 */
.wf-row.hit { background: #fdf6ec; box-shadow: inset 2px 0 0 #e6a23c; }
.wf-row.hit.cur { background: #fae6c4; box-shadow: inset 2px 0 0 #e6a23c, 0 0 0 1px #e6a23c inset; }
.wf-row.dim { opacity: .38; }
.wf-row.err .nm { color: #f56c6c; }
.wf-name { width: 210px; flex: none; display: flex; align-items: center; gap: 4px;
  position: relative; font-size: 11px; color: #303133; overflow: hidden; }
/* 层级竖线：有它才看得出"这一层嵌在第几层里"，纯缩进在多分支时看不出来 */
.guide { position: absolute; top: 0; bottom: 0; width: 1px; background: #eef0f4; }
/* 折叠箭头做成 16×20 的实体点击区：树里最常做的动作就是折叠，
   一个 9px 的字符太难点，点到旁边反而会展开属性面板（两件事，很容易误操作）。 */
.caret { color: #b0b3b8; font-size: 10px; width: 16px; height: 20px; flex: none;
  cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
  border-radius: 3px; }
.caret:hover { color: #409eff; background: #ecf5ff; }
.caret.void { color: #e8eaed; cursor: default; }
.caret.void:hover { color: #e8eaed; background: transparent; }
.dot { width: 6px; height: 6px; border-radius: 50%; flex: none; }
.nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.orphan-mark { color: #e6a23c; font-size: 10px; }
/* 折叠后要能看到"这里藏了多少"——否则折起来像这一步本来就没子步骤 */
.fold-n { color: #b0b3b8; font-size: 10px; flex: none; background: #f5f7fa;
  border-radius: 8px; padding: 0 5px; line-height: 14px; }
.fold-err { color: #f56c6c; margin-left: 3px; }

.wf-track { flex: 1; position: relative; height: 13px; background: #fafbfc;
  border-radius: 3px; overflow: hidden; }
.wf-bar { position: absolute; top: 2px; height: 9px; border-radius: 2px; min-width: 2px; }
.wf-ms { width: 62px; flex: none; text-align: right; font-size: 11px; color: #909399;
  font-variant-numeric: tabular-nums; }

.k-agent { background: #7c5cff; }
.k-chain { background: #409eff; }
.k-llm { background: #e6a23c; }
.k-tool { background: #67c23a; }
.k-ret { background: #14b8a6; }

.wf-detail { background: #fafbfc; border-left: 2px solid #e4e7ed; margin: 2px 0 6px 6px;
  padding: 8px 10px; border-radius: 0 4px 4px 0; }
.d-error { color: #f56c6c; font-size: 11px; margin-bottom: 6px;
  background: #fef0f0; padding: 4px 8px; border-radius: 3px; }
.dgrid { display: flex; gap: 10px; margin-bottom: 4px; }
.dk { width: 42px; flex: none; font-size: 11px; color: #b0b3b8; }
.dv { flex: 1; min-width: 0; }
.kv { display: flex; gap: 8px; font-size: 11px; line-height: 1.7; align-items: flex-start;
  border-radius: 3px; }
/* 整行可点：13px 高的"全文"小按钮在长属性值旁边太难命中，
   而这一行的语义就是"这条属性能展开"——目标做成整行，成本一样、好点得多。 */
.kv.clk { cursor: pointer; }
.kv.clk:hover { background: #f2f8ff; }
.kv .k { width: 168px; flex: none; color: #7a7f87; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.kv .v { flex: 1; min-width: 0; color: #303133; word-break: break-all;
  /* 单个属性值自己封顶：一个 300 字的 JSON 会把整页撑高十几行，
     把它自己的框限住，树的结构才不会被一条属性值冲散。 */
  max-height: 150px; overflow-y: auto; }
.kv .v.long { color: #606266; font-family: ui-monospace, Menlo, monospace; font-size: 10px; }
/* 事件块：一条事件可能带 4~5 个属性，彼此用左边框聚在一起，
   否则和下一个事件连成一片，看不出"这几行属于同一个时间点"。 */
.ev { margin-bottom: 6px; padding-left: 8px; border-left: 2px solid #f0f2f5; }
.ev-h { font-size: 11px; color: #909399; margin-bottom: 2px; }
.ev-h b { color: #606266; font-weight: 600; }
.ev .kv .k { width: 140px; }
.muted { color: #b0b3b8; font-size: 11px; }

.slow { margin-top: 14px; border: 1px solid #ebeef5; border-radius: 6px; background: #fff; }
.slow-head { display: flex; align-items: center; gap: 6px; padding: 8px 12px;
  font-size: 12px; color: #606266; cursor: pointer; }
.slow-head:hover { background: #fafcff; }
.slow-hint { color: #b0b3b8; font-size: 11px; margin-left: 6px; }
.slow-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.slow-table th { text-align: left; padding: 5px 10px; color: #909399;
  font-weight: 500; border-top: 1px solid #f0f2f5; }
.slow-table td { padding: 5px 10px; border-top: 1px solid #f8f9fb; color: #303133; }
.mono { font-family: ui-monospace, Menlo, monospace; font-size: 10px; }
.hot { color: #e6a23c; font-weight: 600; }
</style>
