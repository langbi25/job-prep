/**
 * 树搜索的纯逻辑：把"能不能搜到"从渲染里拆出来。
 *
 * 为什么单独一个文件：这段判断以前藏在 .vue 的 computed 里，只能靠人点页面确认。
 * 但它有三处容易错的地方——搜整棵树而不是可见行、命中要能反推祖先把它们展开、
 * 属性里的元数据键不能被当成内容——全都可以用数据断言，没必要靠肉眼。
 *
 * 约定：span 的形状与 /api/traces/{id} 返回的一致（含 children）。
 */

/** 属性里下划线开头的是 tracing 自己的元数据（_blob_keys / _dropped_keys），不是内容。 */
const META_PREFIX = '_'

export function haystack(span) {
  const parts = [span.name, span.kind, span.status]
  if (span.error) parts.push(span.error)
  for (const [k, v] of Object.entries(span.attrs || {})) {
    if (k.startsWith(META_PREFIX)) continue
    parts.push(k, typeof v === 'string' ? v : JSON.stringify(v))
  }
  for (const e of span.events || []) {
    parts.push(e.n)
    for (const [k, v] of Object.entries(e.a || {})) parts.push(k, String(v))
  }
  return parts.join(' ').toLowerCase()
}

/** 深度优先展平整棵树（含被折叠的部分——折叠是渲染层的事，不影响"有没有"）。 */
export function flatten(root, out = []) {
  if (!root) return out
  out.push(root)
  for (const c of root.children || []) flatten(c, out)
  return out
}

/** 命中的 span_id 列表，按树里的出现顺序。空关键词返回空数组。 */
export function matchIds(root, query) {
  const needle = String(query || '').trim().toLowerCase()
  if (!needle || !root) return []
  return flatten(root).filter((s) => haystack(s).includes(needle)).map((s) => s.span_id)
}

/**
 * 命中节点到根的祖先集合：要展开这些节点，命中才可见。
 * 只返回"有子节点"的 id——叶子当折叠节点处理没有意义。
 */
export function ancestorsOf(root, ids) {
  const want = new Set(ids)
  const keep = new Set()
  const walk = (node, chain) => {
    if (want.has(node.span_id)) chain.forEach((id) => keep.add(id))
    for (const c of node.children || []) walk(c, [...chain, node.span_id])
  }
  if (root) walk(root, [])
  return keep
}
