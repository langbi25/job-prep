/**
 * 树内搜索的断言（node scripts/test-tree-search.mjs）。
 *
 * 为什么用 node 而不是 pytest：这段是前端逻辑，把它翻译成 Python 再测，测的
 * 就是翻译而不是原码。而页面截图在这个环境里拿不到（分辨率过大），
 * 所以用真实数据形状直接验证匹配语义。
 */
import assert from 'node:assert/strict'
import { ancestorsOf, flatten, haystack, matchIds } from '../src/treeSearch.js'

const tree = {
  span_id: 'root',
  name: 'agent.console',
  kind: 'AGENT',
  status: 'ok',
  attrs: { 'agent.skill': 'daily_review', _blob_keys: 'input.value' },
  events: [],
  children: [
    {
      span_id: 'seg',
      name: 'agent.segment.1',
      kind: 'CHAIN',
      status: 'ok',
      attrs: {},
      events: [],
      children: [
        {
          span_id: 'llm',
          name: 'llm.chat',
          kind: 'LLM',
          status: 'ok',
          attrs: { 'input.value': 'PROMPT 全文预览…⟦全文 12000 字符⟧' },
          events: [{ t: 812, n: 'first_token', a: { ttft_ms: 812 } }],
          children: [],
        },
        {
          span_id: 'tool',
          name: 'tool.search_kb',
          kind: 'TOOL',
          status: 'error',
          error: 'TimeoutError: 向量检索超时',
          attrs: {},
          events: [{ t: 900, n: 'retry', a: { reason: 'degraded to fts' } }],
          children: [],
        },
      ],
    },
  ],
}

let n = 0
const ok = (label, fn) => { fn(); n += 1; console.log(`  OK  ${label}`) }

ok('flatten 覆盖整棵树（含被折叠的深层节点）', () => {
  assert.deepEqual(flatten(tree).map((s) => s.span_id), ['root', 'seg', 'llm', 'tool'])
})

ok('按 span 名命中', () => {
  assert.deepEqual(matchIds(tree, 'search_kb'), ['tool'])
})

ok('按属性值命中，且不区分大小写', () => {
  assert.deepEqual(matchIds(tree, 'prompt'), ['llm'])
})

ok('按错误信息命中', () => {
  assert.deepEqual(matchIds(tree, '向量检索超时'), ['tool'])
})

ok('按事件名与事件属性命中', () => {
  assert.deepEqual(matchIds(tree, 'first_token'), ['llm'])
  assert.deepEqual(matchIds(tree, 'degraded'), ['tool'])
})

ok('元数据键不参与匹配（_blob_keys 不是内容）', () => {
  assert.deepEqual(matchIds(tree, 'blob_keys'), [])
})

ok('搜索一个被折叠的子节点也能命中', () => {
  // 折叠是渲染层过滤：matchIds 只看树，所以折叠状态不影响"有没有"
  const ids = matchIds(tree, 'llm.chat')
  assert.deepEqual(ids, ['llm'])
  assert.ok(flatten(tree).some((s) => s.span_id === ids[0]))
})

ok('祖先集合能反推出要展开哪些节点', () => {
  const keep = ancestorsOf(tree, ['llm'])
  assert.deepEqual([...keep].sort(), ['root', 'seg'])
})

ok('空关键词与无命中都返回空', () => {
  assert.deepEqual(matchIds(tree, '   '), [])
  assert.deepEqual(matchIds(tree, '不存在的东西'), [])
})

ok('haystack 不把 JSON 对象变成 [object Object]', () => {
  assert.ok(haystack({ name: 'x', attrs: { args: { a: 1 } } }).includes('"a":1'))
})

console.log(`\n${n}/${n} passed`)
