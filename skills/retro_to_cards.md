---
name: retro_to_cards
title: 复盘文档转题卡
when: 用户给一份复盘/总结文档（对话复盘、项目实践总结），要"变成考点""生成题目""沉淀成卡片"
tools: [ingest_retrospective, confirm_ingest, draft_cards, search_kb, list_pending_actions]
require_tools: [ingest_retrospective]
risk: write
max_iters: 6
output: 提炼出的考点数、入池题数、待审卡数
---
## 这个技能解决什么

复盘文档和面经不同：面经是"别人被问了什么"，复盘是"我做过什么、踩了什么坑"。
后者抽出来的题往往更有价值（真实项目细节），但也更容易抽成流水账。

## 步骤

1. 确认来源：有文件路径用 `source_path`（相对项目根，如 `docs/复盘-自进化闭环.md`），
   否则用 `content` 传全文。两者都缺就直接问。
2. 调 `ingest_retrospective`，`build_cards` 默认 true，`card_limit` 2。
3. 读返回：`points`（考点）、`pooled`（入池题数）、`drafted`（待审卡）。
4. 如果考点里出现"某某模块实现了某某功能"这种**过程描述**，提醒用户：
   这类内容更适合留在文档里，不适合当面试题——但不要自作主张删掉。
5. 结尾给待审卡数量和"去审批中心"的指引。

## 输出契约

必须包含：考点数、入池题数、待审卡数。若 `ok=false`，必须说明停在哪一步（提炼/入池/建卡）。

## 禁止

- 不要修改原文档。
- 不要跳过审批直接建卡。
