# 评测集说明（evals）

这个目录是项目的"验收标准"。它回答四个问题：**判分准不准、召回好不好、抽取判定对不对、
Agent 有没有真完成任务**。没有这层，改 prompt / 调检索参数全靠感觉，越改越玄学。

## 文件

| 文件 | 作用 |
|---|---|
| `golden_answers.jsonl` | 评分校准集：题目 + 不同质量的回答 + 人工标注档位 |
| `retrieval_cases.jsonl` | 检索集：查询 + 应命中的卡片 id + 查询类型 |
| `tool_gap_cases.jsonl` | 缺口集：无卡的高频题，用于检验 `search_kb` 不会把"没卡"说成"有卡" |
| `agent_tasks.jsonl` | Agent 任务级：方向 + 必须提的字段 + 工具数/时长上限 |
| `extract_judge_cases.jsonl` | 提取判定集：原文 + 期望的材料类型 + 逐题期望的答案完整度 |
| `thresholds.json` | 门禁阈值，`run_evals.py --gate` 与 `ci_gate.py` 共用 |
| `results-*.json` | 每次评测的完整报告（留档，可对比版本） |

## 一、评分校准集怎么标

`expected_band` 只有三档，对应模拟面试里的"能不能过"：

- `high`：主干全对，讲出原理/权衡/量化细节，可直接过面试
- `mid`：概念正确但停留在"是什么"，缺原理与取舍，追问容易崩
- `low`：答非所问、有关键事实错误，或几乎没有有效信息

标注原则（踩过的坑写在这里）：

1. **按"覆盖主干要点"判档，不按措辞漂亮程度。** 口语化但要点齐 → 仍是 high。
2. **不按长度判档。** 短答案只要命中主干就是 high，长答案堆概念但没取舍就是 mid。
3. **"好像/大概/应该是"这类模糊表述要扣档。** 真实面试里这种回答等于不会。
4. 拿不准时以"面试官会不会继续追问"为准：会被追穿 → mid 及以下。

### 为什么要看 lift

标注天然容易偏斜（初版 54 例里 37 例是 high，占 68%）。只报 `band_accuracy` 时，
一个"永远猜 high"的判定器就能拿 0.68，看起来还行其实毫无判别力。
所以报告里同时给 `majority_baseline` 和 `lift = accuracy - baseline`，
门禁要求 `lift ≥ 0.2`。加新用例时请注意**补齐 mid / low**，别只加 high。

### 判分阈值是怎么定的

四维均分 → 档位的切点不是拍脑袋，是看实测分布定的：

| 档位 | 实际均分区间（62 例） |
|---|---|
| low | 0.5 ~ 4.8 |
| mid | 4.8 ~ 6.8 |
| high | 7.0 ~ 8.5 |

所以 `low/mid` 切在 **5.0**（直觉上的 4.0 是错的：判分器对"浅但不空白"的回答稳稳给 4.5），
`high` 切在 **7.0**（mid 最高 6.8，两者分离干净）。
⚠️ 这两个切点是在当前标注集上标定的，扩样本后必须重新确认，否则容易过拟合这 62 例。

### 判分器的已知抖动

同一个回答重复打分的均分波动约 ±0.5，落在 7.0 / 4.0 这两个档位边界上时会翻转。
这不是 bug，是 LLM as judge 的固有噪声。处理方式：

- 门禁用**总体准确率**，不用单例准确率；
- 边界案例（avg 6.6~7.4）出现翻转时，先怀疑标注，再怀疑 prompt；
- 需要更稳时可对边界案例打分 3 次取中位数（成本 ×3，暂未启用）。

## 二、检索集为什么要分 kind

`kind` 三类，缺一类评测就失去诊断价值：

- `keyword`：关键词型查询（"RDB AOF 区别"）。FTS 本该命中，**向量路不该更差**。
- `paraphrase`：口语改写（"进程挂了内存里的东西还能找回来吗"）。与卡片文本几乎没有
  字面重叠，**FTS 必然塌**，这一桶才能证明向量路的必要性。
- `distractor`：有共享词但容易召回到错卡（"消息既不能丢也不能重"）。
  这一桶看 rerank 能不能把 top1 掰正。

只看总体 hit@5 是不够的：全命中时三档都是 1.0，看不出任何差异。
所以报告按 kind 分桶输出，门禁额外卡 `paraphrase` 桶的 hit@5。

## 三、Agent 任务级评测

不只判断"有没有输出"，而是判断**任务有没有真完成**：

- `min_tools`：用到的不同工具数（证明真的去查了数据，而不是凭空编）
- `must_mention`：计划文本里必须出现的具体标识（卡片 id / 主题），证明引用了真实数据
- `max_minutes`：计划总时长不超过预算（证明它理解"今天做不完"这件事）

计划结构在 `harness._normalize_plan()` 里被强制归一成 `list[{priority, action, reason, minutes}]`，
评测只依赖归一后的结构——模型输出走样不会让评测崩掉。

## 三·五、为什么 search_kb 要单独评

底层 `rag_service.search` 的 hit@5 好，不代表 Agent 手里的工具好用——工具是
"检索 + 归档过滤 + 要点截断 + 置信度标注"的组合，问题只在工具层暴露：

| 指标 | 含义 | 门禁 |
|---|---|---|
| `hit3` | 有卡用例里，正确卡是否出现在前 3 | ≥ 0.8 |
| `top1` | 正确卡是否排第一（RRF 在 7 张卡的小语料上抖动明显，只报不卡） | — |
| `empty_point_hits` | 命中了但要点为空的次数 → 空壳卡数量 | 只报，靠人补 |
| `gap_false_confident` | 无卡题被标成 `high` 的例数（Agent 会误以为"我有卡"） | = 0 |

关键取舍：**没有用 cosine 硬阈值过滤"没卡"**。实测"换个问法"的合法命中只有
0.48~0.69，而无卡题也能到 0.50~0.60，两段重叠，卡阈值必然误杀好召回。
所以工具只透出 `similarity` + `confidence`（high/mid/low/none），由模型对着
要点自己判断"这是答案还是只是同主题"——这正是把内容而不只是 id 交给它的意义。

### 用例会随数据迁移，别让标签骗自己

`tool_gap_cases.jsonl` 原先那 5 条（Memory / Spark shuffle / 数仓分层 / 幻觉 / Agent 架构）
是 2026-10-27 标的"高频无卡题"。到 10-31 这 5 道题全建了卡，`search_kb` 也都能把它们召回到
第一位——继续当 gap 用例，等于把"建卡成功"记成"误标高置信"（`gap_false_confident` 一路报 5）。

所以 10-31 做了一次用例迁移：
- 那 5 条转入 `retrieval_cases.jsonl`，`kind=covered`，期望卡取建卡时记死的硬关联（1.0 把握）；
- gap 集换成 5 道**确实没卡**的题（手写 SQL 窗口函数、Claude Code 选型、Plan&Execute vs ReAct、
  Agent 死循环、Multi-Agent 通信），标注前逐条确认 `_match_card` 返回 None。

迁移后：检索集 14 → 19 例，`hit3` 0.737、`gap_false_confident` 归零。
**用例增长会让历史分数不可直接比**，比分数更该看的是剩下没命中的那几条是什么类型。

## 三·六、提取判定为什么也要评

提取阶段除了抽题面，还要判三件事：材料是面经还是单题摘录、这道题有没有带答案、
答案完不完整。**这三判不是"抽取质量"，是"数据流向"**：

- 判成 `complete` → 用原文答案规范化建卡（低风险分支，审批可批量过）
- 判成 `partial` / `none` → 模型从零起草（高风险分支，逐条审）

所以四列指标里有两列是专门盯"判错了会不会出事"的：

| 指标 | 含义 | 门禁 |
|---|---|---|
| `doc_type_accuracy` | 材料类型判对的比例 | ≥ 0.8 |
| `completeness_accuracy` | 逐题完整度判对的比例 | ≥ 0.7 |
| `complete_precision` | 判成 `complete` 的里面有多少真是完整 | ≥ 0.7 |
| `bare_recall` | 真实"光有题没答案"的题有没有被认出来 | ≥ 0.8 |

`complete_precision` 是这组里最重要的：误判会把一段残缺、甚至被面试官否定的回答
写进 `question_pool.answer_points_json`，而这个字段会被 `context_builder` 塞进模拟面试的
评分上下文——错一条，整轮打分都在跟错的基准对齐，且**没有任何报错**。
用例集里 `answer-negated`（"面试官说不对，我理解偏了"必须判 `none`）就是守这一条。

`doc_type` 的期望值允许写成数组（如 `["single_question","interview"]`）：
"阿里一面：Redis 持久化怎么做的"这种输入本身就介于两者之间，强行只认一个值
等于在考标注员的手感，而不是在评测模型。

## 四、怎么跑

```bash
python scripts/run_evals.py            # 全量（约 3~4 分钟，含 LLM 调用）
python scripts/run_evals.py --skip-llm # 只跑检索（fts/hybrid），秒级
python scripts/run_evals.py --gate     # 全量 + 门禁，不达标退出码 1
python scripts/ci_gate.py              # 只判定最近一次结果，零 LLM 调用（适合 pre-commit/CI）
python scripts/run_evals.py --gate --stability  # 边界用例三次取中位数，抑制判分抖动
```

改 prompt、改检索参数、换模型后都应该跑一次 `--gate`，并对比新旧 `results-*.json`。

## 五、CI 门禁（GitHub Actions）

`.github/workflows/evals.yml`，两层设计：

| 层 | 触发 | 内容 | 成本 |
|---|---|---|---|
| `fast-checks` | 每次 push / PR | 语法检查 + `--skip-llm` 检索快检 | 零 |
| `full-gate` | 同上，但**需要 secret** | 全量评测 + 门禁 | 每次 ~百次 LLM 调用 |

启用全量门禁：仓库 **Settings → Secrets and variables → Actions** 添加
`LLM_API_KEY`（可选变量 `LLM_BASE_URL` / `LLM_MODEL`，默认 DeepSeek）。
没配 secret 时 `fast-checks` 照常跑，`full-gate` 打 warning 跳过，不会假绿。

CI 环境数据库为空也能跑：知识卡片是 `knowledge/` 下的 md 文件，启动时由
`sync_all()` 自动收编入库——**Markdown 是唯一事实源**这一设计在这里直接兑现了。
