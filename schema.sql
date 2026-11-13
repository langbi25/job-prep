PRAGMA journal_mode = WAL;

-- ============ 知识库 ============
CREATE TABLE IF NOT EXISTS kb_cards (
  id            TEXT PRIMARY KEY,
  domain        TEXT NOT NULL,
  topic         TEXT NOT NULL,
  title         TEXT NOT NULL,
  question      TEXT,
  points_json   TEXT NOT NULL DEFAULT '[]',
  pitfalls_json TEXT NOT NULL DEFAULT '[]',
  related_json  TEXT NOT NULL DEFAULT '[]',
  tags_json     TEXT NOT NULL DEFAULT '[]',
  source_type   TEXT NOT NULL DEFAULT 'manual',
  source_ref    TEXT,
  file_path     TEXT NOT NULL,
  mastery       REAL NOT NULL DEFAULT 0,
  review_count  INTEGER NOT NULL DEFAULT 0,
  last_review_at TEXT,
  srs_due       TEXT,
  status        TEXT NOT NULL DEFAULT 'active',
  merged_into   TEXT REFERENCES kb_cards(id),  -- merge_card 动作：重复卡指向保留卡
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_dir_topic ON kb_cards(domain, topic);
CREATE INDEX IF NOT EXISTS idx_cards_srs ON kb_cards(srs_due) WHERE srs_due IS NOT NULL;

-- 外部内容 FTS：列名必须与 kb_cards 的真实列一一对应，否则 FTS5 回读列值时
-- 会拼出 SELECT ... T.<col> FROM kb_cards，列不存在则整条 MATCH 报错。
CREATE VIRTUAL TABLE IF NOT EXISTS kb_cards_fts USING fts5(
  title, question, points_json, content='kb_cards', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS cards_ai AFTER INSERT ON kb_cards BEGIN
  INSERT INTO kb_cards_fts(rowid,title,question,points_json)
  VALUES (new.rowid,new.title,new.question,new.points_json);
END;
CREATE TRIGGER IF NOT EXISTS cards_au AFTER UPDATE ON kb_cards BEGIN
  INSERT INTO kb_cards_fts(kb_cards_fts,rowid,title,question,points_json)
  VALUES ('delete',old.rowid,old.title,old.question,old.points_json);
  INSERT INTO kb_cards_fts(rowid,title,question,points_json)
  VALUES (new.rowid,new.title,new.question,new.points_json);
END;
CREATE TRIGGER IF NOT EXISTS cards_ad AFTER DELETE ON kb_cards BEGIN
  INSERT INTO kb_cards_fts(kb_cards_fts,rowid,title,question,points_json)
  VALUES ('delete',old.rowid,old.title,old.question,old.points_json);
END;

-- ============ 采集队列（面经投喂状态机） ============
CREATE TABLE IF NOT EXISTS ingests (
  id            TEXT PRIMARY KEY,
  source_type   TEXT NOT NULL,
  source_url    TEXT,
  source_site   TEXT,
  raw_path      TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',
  extracted_json TEXT,
  error         TEXT,
  created_at    TEXT NOT NULL,
  confirmed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingests_status ON ingests(status);

-- ============ 面经 ============
CREATE TABLE IF NOT EXISTS feed_interviews (
  id             TEXT PRIMARY KEY,
  ingest_id      TEXT REFERENCES ingests(id),
  company        TEXT,
  position       TEXT,
  direction      TEXT NOT NULL,
  round          TEXT,
  interview_date TEXT,
  result         TEXT DEFAULT 'unknown',
  source_type    TEXT NOT NULL,
  source_url     TEXT,
  raw_path       TEXT NOT NULL,
  question_count INTEGER DEFAULT 0,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_iv_dir ON feed_interviews(direction);
CREATE INDEX IF NOT EXISTS idx_iv_company ON feed_interviews(company);

-- ============ 题目池 ============
CREATE TABLE IF NOT EXISTS question_pool (
  id            TEXT PRIMARY KEY,
  text          TEXT NOT NULL,
  aliases_json  TEXT NOT NULL DEFAULT '[]',
  qtype         TEXT,
  topic         TEXT,
  difficulty    INTEGER,
  answer_points_json TEXT DEFAULT '[]',
  answer_completeness TEXT,
  answer_gaps_json TEXT DEFAULT '[]',
  freq_count    INTEGER NOT NULL DEFAULT 1,
  related_card_ids_json TEXT DEFAULT '[]',
  merged_into   TEXT REFERENCES question_pool(id),
  first_seen_at TEXT,
  last_seen_at  TEXT
);
-- 与 kb_cards_fts 同理：用外部内容表 + 触发器，避免"代码手工塞 FTS"造成的索引漂移
-- （合并同义题、改题面时最容易漏同步，表现为"明明有这道题却搜不到"）。
CREATE VIRTUAL TABLE IF NOT EXISTS question_fts USING fts5(
  text, aliases_json, content='question_pool', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS questions_ai AFTER INSERT ON question_pool BEGIN
  INSERT INTO question_fts(rowid,text,aliases_json) VALUES (new.rowid,new.text,new.aliases_json);
END;
CREATE TRIGGER IF NOT EXISTS questions_au AFTER UPDATE ON question_pool BEGIN
  INSERT INTO question_fts(question_fts,rowid,text,aliases_json)
  VALUES ('delete',old.rowid,old.text,old.aliases_json);
  INSERT INTO question_fts(rowid,text,aliases_json) VALUES (new.rowid,new.text,new.aliases_json);
END;
CREATE TRIGGER IF NOT EXISTS questions_ad AFTER DELETE ON question_pool BEGIN
  INSERT INTO question_fts(question_fts,rowid,text,aliases_json)
  VALUES ('delete',old.rowid,old.text,old.aliases_json);
END;

CREATE TABLE IF NOT EXISTS question_occurrences (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id  TEXT NOT NULL REFERENCES question_pool(id),
  interview_id TEXT NOT NULL REFERENCES feed_interviews(id),
  raw_text     TEXT,
  ask_order    INTEGER,
  UNIQUE(question_id, interview_id)
);

-- ============ 复习与闭环（阶段 0 建好，阶段 3~4 启用） ============
CREATE TABLE IF NOT EXISTS mistakes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id     TEXT REFERENCES question_pool(id),
  card_id         TEXT REFERENCES kb_cards(id),
  mock_session_id TEXT,
  raw_text        TEXT NOT NULL,
  cause_type      TEXT,                  -- 知识盲区 / 理解偏差 / 表达不清 / 临场紧张
  feedback        TEXT,
  resolved        INTEGER DEFAULT 0,
  created_at      TEXT NOT NULL,
  UNIQUE(mock_session_id, raw_text)      -- 幂等：同一场同一题只记一次
);

CREATE TABLE IF NOT EXISTS mastery_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id    TEXT NOT NULL REFERENCES kb_cards(id),
  delta      REAL NOT NULL,
  reason     TEXT NOT NULL,
  ref_type   TEXT,
  ref_id     TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS srs_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL REFERENCES kb_cards(id),
  reviewed_at TEXT NOT NULL,
  result TEXT NOT NULL,
  next_due TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mock_sessions (
  id TEXT PRIMARY KEY,
  direction TEXT NOT NULL,
  persona TEXT,
  jd_text TEXT,
  config_json TEXT,
  started_at TEXT,
  finished_at TEXT,
  overall_json TEXT,
  report_path TEXT
);

CREATE TABLE IF NOT EXISTS mock_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES mock_sessions(id),
  question_id TEXT,
  question_text TEXT NOT NULL,
  answer_text TEXT,
  scores_json TEXT,
  feedback TEXT,
  followups_json TEXT,
  created_at TEXT NOT NULL
);

-- ============ 阶段 5 深化：向量索引 + Agent 运行轨迹 ============
CREATE TABLE IF NOT EXISTS embeddings (
  entity_type TEXT NOT NULL,             -- card / question
  entity_id   TEXT NOT NULL,
  text_hash   TEXT NOT NULL,             -- 内容变更检测
  vector      TEXT NOT NULL,             -- JSON float list（数据量小，免向量库服务，2G 友好）
  model       TEXT NOT NULL DEFAULT '',  -- 生成向量用的模型 id；换模型时只重嵌不匹配的行，新旧向量不混算余弦
  PRIMARY KEY (entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS runs (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,             -- study_plan / remediation / ...
  status      TEXT NOT NULL,             -- done / max_iters / error
  steps_json  TEXT NOT NULL DEFAULT '[]',
  result_json TEXT,
  created_at  TEXT NOT NULL
);

-- Human-in-the-loop：Agent 提议的写操作，必须人工审批后才执行
CREATE TABLE IF NOT EXISTS agent_actions (
  id          TEXT PRIMARY KEY,
  run_id      TEXT,
  session_id  TEXT,
  kind        TEXT NOT NULL,             -- create_card / boost_srs
  payload_json TEXT NOT NULL,
  reason      TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected / failed
  result_json TEXT,
  created_at  TEXT NOT NULL,
  decided_at  TEXT
);

-- ============ 对话控制台（Agent 的唯一入口） ============
-- 为什么要单独一张表而不是塞进 runs：runs 记的是"一次 Agent 运行"，
-- 一个会话里有很多轮，且用户消息不属于任何一次 run。两者粒度和生命周期都不同。
CREATE TABLE IF NOT EXISTS console_messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role       TEXT NOT NULL,                 -- user / assistant
  content    TEXT NOT NULL,
  trace_json TEXT NOT NULL DEFAULT '[]',    -- 这一轮调了哪些工具（可回放）
  ctx_json   TEXT DEFAULT '{}',             -- 这一轮的上下文账本（刷新页面也能看见）
  mode       TEXT,                          -- auto / assist，记下当时给的写权限
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_console_session ON console_messages(session_id, id);

-- ============ 飞轮：作战地图 / 真实结果回录（阶段 8） ============
CREATE TABLE IF NOT EXISTS coverage_reports (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  interview_id  TEXT NOT NULL REFERENCES feed_interviews(id),
  generated_at  TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  UNIQUE(interview_id)              -- 重跑直接覆盖
);

CREATE TABLE IF NOT EXISTS interview_outcomes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  company       TEXT,
  occurred_on   TEXT NOT NULL,      -- 面试日期
  result        TEXT DEFAULT 'unknown',   -- pass / fail / unknown
  notes         TEXT,
  questions_json TEXT NOT NULL DEFAULT '[]',  -- [{text, self_band(0-10), matched_qid?}]
  created_at    TEXT NOT NULL
);

-- ============ 项目进化环（阶段 8）：题目 → 改造方案 → 实战复盘 ============
CREATE TABLE IF NOT EXISTS improvement_plans (
  id           TEXT PRIMARY KEY,
  question_id  TEXT REFERENCES question_pool(id),
  card_id      TEXT,                    -- 生成时命中的卡片（可为空）
  status       TEXT NOT NULL DEFAULT 'pending',   -- pending / adopted / archived
  payload_json TEXT NOT NULL,           -- 方案正文（goal/steps/acceptance/metrics/...）
  doc_path     TEXT,                    -- 落盘的方案文档（docs/plans/xxx.md）
  notes        TEXT,                    -- 实施笔记（回灌前的原始素材）
  created_at   TEXT NOT NULL,
  adopted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_plans_status ON improvement_plans(status);

-- ============ 上下文管理（阶段 9）：窗口外的旧对话压成有界摘要 ============
-- 为什么要落库而不是只放内存：摘要是"下一轮要注入的内容"，进程重启不能丢；
-- 而且 covered_to_id 记录"压到哪一条了"，避免重复压缩同一段对话。
CREATE TABLE IF NOT EXISTS conversation_summary (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id      TEXT NOT NULL,
  covered_from_id INTEGER NOT NULL,   -- 覆盖 console_messages.id 区间 [from, to]
  covered_to_id   INTEGER NOT NULL,
  n_messages      INTEGER NOT NULL,
  src_chars       INTEGER NOT NULL DEFAULT 0,
  summary         TEXT NOT NULL,      -- 三段式：事实 / 进度 / 约束
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_summary ON conversation_summary(session_id, covered_to_id);

-- 每轮的上下文账本：各分区 token、降级动作、锚定与丢弃数
-- 单独建表而不是塞进 console_messages.trace_json：账本是"每次模型请求"一条
-- （一轮里可能有 3~4 次请求），消息是"每轮"一条，粒度不同。
CREATE TABLE IF NOT EXISTS ctx_traces (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT,
  seq           INTEGER,               -- 本会话第几次组装（从 1 开始）
  budget        INTEGER,
  total_tokens  INTEGER,
  sections_json TEXT NOT NULL DEFAULT '{}',   -- {system, summary, retrieval, history, user, tools}
  dropped_msgs  INTEGER DEFAULT 0,
  anchored      INTEGER DEFAULT 0,
  summary_id    INTEGER,
  retrieved_n   INTEGER DEFAULT 0,
  degraded_json TEXT DEFAULT '[]',     -- ["drop_retrieval","shorten_summary","drop_oldest_turn"]
  fallback      TEXT,                  -- 非空表示降级/异常（CTX_OVER_BUDGET 等）
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ctx_traces ON ctx_traces(session_id, id);

-- ============ 回合事实（L0）：自进化的唯一数据源 ============
-- 为什么要有这张宽表：判断"这一轮好不好"需要的信号散在六张表里
-- （上下文在 ctx_traces、费用在 llm_usage、工具在 tool_calls、结果在 console_messages），
-- 每张都只回答一部分。分析时现 join 六张表，等于每个面板都要重写一遍口径——
-- 那正是"待复习 3 张 vs 25 张"那类事故的来源。
-- 所以这里做一件事：一轮问答结束，把四类信号快照成一行。写入只有一处，读取只认这里。
-- 四类信号的分界很重要：
--   输入（检索几条、记忆几条、预算多少）→ 解释"为什么这轮难"
--   过程（几轮 LLM、几次工具、几次报错）→ 解释"它是怎么跑的"
--   结果（回复多长、耗时、状态）        → 解释"跑完是个什么"
--   ★ 质量信号（点踩、追问间隔）        → 才是"好不好"的唯一直接证据
-- 前两类是解释变量，最后一类才是标签。混在一起就永远学不出东西。
CREATE TABLE IF NOT EXISTS turn_facts (
  id            TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,
  turn          INTEGER NOT NULL,
  ts            TEXT NOT NULL,
  trace_id      TEXT,                       -- 串 llm_usage / tool_calls / OTel 的键
  mode          TEXT NOT NULL DEFAULT 'auto',
  skill         TEXT,                       -- NULL = 没命中技能，走通用 agent
  skill_version INTEGER,                    -- 版本号未知时留 NULL，不编 0
  segs          INTEGER NOT NULL DEFAULT 1, -- 组合技能展开成几段
  need_tools    INTEGER NOT NULL DEFAULT 0, -- 该技能声明必须用几个工具（判断"盲答"用）
  -- 输入侧
  msg_chars     INTEGER NOT NULL DEFAULT 0,
  retrieval_n   INTEGER NOT NULL DEFAULT 0,
  memory_n      INTEGER NOT NULL DEFAULT 0,
  ctx_tokens    INTEGER NOT NULL DEFAULT 0,
  ctx_budget    INTEGER NOT NULL DEFAULT 0, -- 与 ctx_tokens 一比就是压缩比
  dropped_msgs  INTEGER NOT NULL DEFAULT 0, -- 为塞进预算丢掉了多少轮历史
  degraded_json TEXT NOT NULL DEFAULT '[]',
  hit_ids       TEXT NOT NULL DEFAULT '[]', -- 检索命中的条目 id+分数（最多 20 条）
  -- 过程侧
  llm_calls     INTEGER NOT NULL DEFAULT 0, -- 主循环（console:*）调用数，按台账聚合
  llm_aux       INTEGER NOT NULL DEFAULT 0, -- 同轮旁路：记忆抽取/标题生成等，不占主循环配额
  llm_errors    INTEGER NOT NULL DEFAULT 0, -- 失败/降级的模型调用数（稳定性 vs 内容问题）
  tool_calls    INTEGER NOT NULL DEFAULT 0,
  tool_errors   INTEGER NOT NULL DEFAULT 0,
  -- 结果侧
  reply_chars   INTEGER NOT NULL DEFAULT 0,
  duration_ms   INTEGER NOT NULL DEFAULT 0,
  tokens        INTEGER NOT NULL DEFAULT 0,
  cost_cny      REAL NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'ok', -- ok / partial / failed
  error         TEXT,
  -- 质量信号（自进化的标签）
  feedback      INTEGER,                    -- 1 有用 / -1 没用 / NULL 未表态
  feedback_note TEXT,
  followup_secs INTEGER,                    -- 下一轮距本轮的秒数，由后一轮回填
  -- 归因（写入时算一次，flags 留全部命中项，failure_rule 只留主因）
  flags_json    TEXT NOT NULL DEFAULT '[]',
  failure_rule  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_facts_seat ON turn_facts(session_id, turn);
CREATE INDEX IF NOT EXISTS idx_turn_facts_ts ON turn_facts(ts);
CREATE INDEX IF NOT EXISTS idx_turn_facts_rule ON turn_facts(failure_rule);

-- ============ 关系层（L2）：题↔卡一等关系，接替 json 列 ============
-- 为什么建表而不是继续用 related_card_ids_json：json 列装不下三样东西——
-- 这条关系是「建卡时记下的」还是「向量猜的」（match_method）、猜的有多确（confidence）、
-- 什么时候挂上的（created_at）。口径打架（作战地图 31% vs 看板 25%）的根源就在这。
-- json 列保留作镜像，写路径双写，读路径一律走本表。
CREATE TABLE IF NOT EXISTS card_question_links (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id  TEXT NOT NULL REFERENCES question_pool(id),
  card_id      TEXT NOT NULL REFERENCES kb_cards(id),
  match_method TEXT NOT NULL DEFAULT 'manual',   -- drafted/manual/vector/legacy/transfer
  confidence   REAL,
  created_at   TEXT NOT NULL,
  UNIQUE(question_id, card_id)
);
CREATE INDEX IF NOT EXISTS idx_cql_card ON card_question_links(card_id);

-- ============ MCP 可观测（2026-11-09）：每次工具调用一行 ============
-- 为什么落库而不是只打日志：MCP 的调试链路是「模型 → 宿主 → server → service」，
-- 四段里任何一段出问题，stdout 都被协议帧占满（stdio 传输下 stdout 就是通信管道），
-- 只能靠结构化记录查。args 存摘要不存全文，避免面经原文把库撑大。
CREATE TABLE IF NOT EXISTS mcp_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  tool          TEXT NOT NULL,
  client        TEXT NOT NULL DEFAULT 'stdio',   -- stdio / http / eval / ui_try
  args_json     TEXT NOT NULL DEFAULT '{}',
  args_hash     TEXT,
  status        TEXT NOT NULL,                   -- ok / error
  error         TEXT,
  latency_ms    INTEGER NOT NULL DEFAULT 0,
  result_chars  INTEGER NOT NULL DEFAULT 0,
  result_preview TEXT,
  read_only     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_ts ON mcp_calls(ts);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_tool ON mcp_calls(tool, status);

-- ============ Prompt 版本表（2026-11-11，P4 优化执行器） ============
-- prompt 是"源代码"：优化器改 prompt = 改逻辑，必须可追溯、可回滚。
-- 运行时唯一事实源是 prompts/*.yaml 文件；本表记录候选版本的生命周期与评测证据：
-- candidate（已起草未上机）→ active（gate+基线对比通过，文件已换成该版本）
--                          → rolled_back（评测不过或异常，文件已恢复快照）
-- retired（被更新的 active 版本顶掉）/ rejected（起草校验没过，未上机）。
CREATE TABLE IF NOT EXISTS prompt_versions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt_name    TEXT NOT NULL,
  version        INTEGER NOT NULL,               -- 候选版本号 = 父版本 + 1
  parent_version INTEGER,
  system_text    TEXT NOT NULL,
  user_text      TEXT NOT NULL,
  output_schema  TEXT,
  description    TEXT DEFAULT '',
  status         TEXT NOT NULL DEFAULT 'candidate',
  source         TEXT NOT NULL DEFAULT 'optimizer',  -- optimizer / manual / test
  reason         TEXT,                            -- 归因依据：哪些根因/失败触发了这次改写
  changes_json   TEXT,                            -- LLM 自述的修改点 + rationale
  snapshot_path  TEXT,                            -- 上机前的文件快照（回滚依据）
  eval_report    TEXT,                            -- 评测报告路径
  gate_passed    INTEGER,                         -- 1/0/NULL（未评测）
  baseline_metrics TEXT,                          -- 基线指标 json（对比证据）
  regressions    TEXT,                            -- 基线回归明细 json（回滚时非空）
  created_at     TEXT NOT NULL,
  evaluated_at   TEXT,
  decided_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_name ON prompt_versions(prompt_name, status);

-- ============ 复盘中心（2026-11-11，P6） ============
-- 复盘文档（对话/建设过程/项目实践总结）→ 考点提炼 → 建题入池 → 建卡走审批。
-- 不变量与全项目一致：题目可直入池（题池本来就是待办池，面经抽题同款），
-- 卡片绝不直写——create_card 一律进 agent_actions(pending) 等人审批。
CREATE TABLE IF NOT EXISTS retrospectives (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  source_path   TEXT,                              -- 若来自 docs/ 下文件，记录出处
  content       TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'ingested',  -- ingested/extracted/built/cards_done/failed
  points_json   TEXT,                              -- 考点 [{point, why, ask, topic}]
  questions_json TEXT,                             -- 生成的题目 [{text, qtype, topic, point}]
  question_ids_json TEXT DEFAULT '[]',             -- 已入池的题目 id（建卡用）
  cards_json    TEXT,                              -- 起草的卡草稿 [{action_id, title}]
  error         TEXT,                              -- failed 时的原因
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- ============ Agent 能力补强（2026-11-12）：记忆 / 工具结果 / 技能 / 编排 ============

-- 长期记忆：与「知识卡」正交。知识卡是"我学过什么"（可复习、可评测的内容实体），
-- 记忆是"关于用户/任务/过程的事实与偏好"（不该进复习系统，但每轮都影响决策）。
-- 版本策略：同 (scope,scope_id,kind,key) 只允许一条活跃行；被替代的行写 superseded_by
-- 软失效而不删除——"去年说想转后端、今年改口"这种演化必须可追溯，否则归因会错。
CREATE TABLE IF NOT EXISTS agent_memory (
  id             TEXT PRIMARY KEY,                 -- mem-20261112-153000-1234
  scope          TEXT NOT NULL,                    -- global / session
  scope_id       TEXT,                             -- scope=session 时为 session_id
  kind           TEXT NOT NULL,                    -- fact / preference / episode / task_state
  key            TEXT NOT NULL,                    -- 归一化主题（去重与覆盖的依据）
  value          TEXT NOT NULL,
  confidence     REAL NOT NULL DEFAULT 0.7,        -- 0~1，低置信不覆盖高置信
  source         TEXT NOT NULL,                    -- extract(自动抽取) / tool(显式) / import
  session_id     TEXT,                             -- 哪场会话产生的
  ref_json       TEXT DEFAULT '{}',                -- {turn, message_id, trace_id}
  version        INTEGER NOT NULL DEFAULT 1,
  superseded_by  TEXT,                             -- 被哪条记忆替代（非空=已失效）
  ttl_days       INTEGER,                          -- NULL=不自动过期
  expires_at     TEXT,
  access_count   INTEGER NOT NULL DEFAULT 0,
  last_access_at TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_lookup ON agent_memory(scope, scope_id, kind);
CREATE INDEX IF NOT EXISTS idx_memory_key ON agent_memory(scope, scope_id, key, superseded_by);
CREATE INDEX IF NOT EXISTS idx_memory_expire ON agent_memory(expires_at);

-- 记忆变更审计：写入/合并/替代/遗忘各一条，回答"这条记忆怎么来的、为什么没了"。
CREATE TABLE IF NOT EXISTS memory_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id  TEXT NOT NULL,
  action     TEXT NOT NULL,                        -- created / merged / superseded / rejected / forgotten
  reason     TEXT,
  old_value  TEXT,
  new_value  TEXT,
  session_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_events ON memory_events(memory_id, id);

-- 工具结果可寻址：严格截断会让模型"看过就再也回不去"，只能重复调用。
-- 大结果落库换 ref，模型自取片段（fetch_tool_result）——截断从"信息丢失"变成"分页"。
CREATE TABLE IF NOT EXISTS tool_results (
  ref         TEXT PRIMARY KEY,                    -- tr-xxxxxxxxxxxx（内容哈希，天然去重）
  tool        TEXT NOT NULL,
  args_json   TEXT,
  content     TEXT NOT NULL,
  chars       INTEGER NOT NULL,
  session_id  TEXT,
  turn        INTEGER,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_results_ts ON tool_results(created_at);

-- 技能执行记录：skill 是"一组工具 + 分步 SOP + 输出契约"，不是新工具。
-- 记下来是为了回答"这个技能实际被用了几次、成功率多少、哪一步最常失败"。
CREATE TABLE IF NOT EXISTS skill_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  skill       TEXT NOT NULL,
  session_id  TEXT,
  status      TEXT NOT NULL,                       -- ok / partial / failed
  steps_done  INTEGER NOT NULL DEFAULT 0,
  tools_used  TEXT,                                -- 逗号分隔
  tokens      INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  error       TEXT,
  skill_version INTEGER,                           -- 跑的是哪一版；NULL = 未进版本管理
  cost_cny    REAL NOT NULL DEFAULT 0,             -- 这次运行折算的钱
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_runs ON skill_runs(skill, created_at);

-- 多 Agent 编排台账：plan 是"证据"，不是缓存。
-- 一条记录 = 目标 → 步骤 DAG → 每步产出 → 汇总 → critic 判定，可回放整条编排轨迹。
CREATE TABLE IF NOT EXISTS agent_plans (
  id           TEXT PRIMARY KEY,                   -- plan-20261112-153000-ab12
  session_id   TEXT,
  goal         TEXT NOT NULL,
  plan_json    TEXT NOT NULL,                      -- [{id, role, goal, tools, depends_on}]
  status       TEXT NOT NULL DEFAULT 'running',    -- running / done / failed / partial
  steps_json   TEXT DEFAULT '[]',                  -- [{id, status, output, tokens, duration_ms, error}]
  result_json  TEXT,                               -- {answer, evidence, critique, retries}
  tokens_used  INTEGER NOT NULL DEFAULT 0,
  cost_cny     REAL NOT NULL DEFAULT 0,            -- 步骤 + 合成 + 自检的全部 LLM 费用（元）
  duration_ms  INTEGER NOT NULL DEFAULT 0,
  mode         TEXT NOT NULL DEFAULT 'assist',     -- 续跑要按原权限跑，不能默认放宽到 auto
  resume_count INTEGER NOT NULL DEFAULT 0,         -- 续跑过几次：多次续跑说明 plan 质量有问题
  trace_id     TEXT,                               -- 首跑那次的 trace_id；续跑沿用它，
                                                   -- 否则一次编排在调用树里会断成两棵
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_plans ON agent_plans(session_id, created_at);

-- LLM 用量台账：一行 = 一次 chat/completions 调用。写入点在 llm._chat_json
-- （唯一咽喉，漏不掉）。观测后端记的是"运维视角"的 token，这里记的是
-- "业务视角"的钱：按场景/会话/轮次归因，回答"这个技能一次花多少钱"。
CREATE TABLE IF NOT EXISTS llm_usage (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT NOT NULL,                 -- ISO 本地时间（毫秒精度）
  model             TEXT NOT NULL,
  scene             TEXT NOT NULL DEFAULT '',      -- tracing 的 llm.scene（console / orchestrator_step…）
  session_id        TEXT,
  turn              INTEGER NOT NULL DEFAULT 0,
  trace_id          TEXT,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cached_tokens     INTEGER NOT NULL DEFAULT 0,    -- 缓存命中部分（按 cached_input 价计）
  cost_cny          REAL NOT NULL DEFAULT 0,       -- 折算费用（元），单价见 app/core/pricing.py
  price_estimated   INTEGER NOT NULL DEFAULT 0,    -- 1 = 模型不在价目表，按默认价估的
  latency_ms        INTEGER,
  -- 失败也要落账（2026-11-12 补）。原来只在成功后写，于是"这轮调了 5 次、
  -- 其中 2 次 429 重试"这个事实在台账里完全不存在——只剩 3 行成功记录，
  -- 看起来像是"只调了 3 次、很顺"。稳定性问题因此永远归因不到。
  status            TEXT NOT NULL DEFAULT 'ok',    -- ok / error
  error             TEXT,                          -- 错误摘要（LLMError 原文前 300 字）
  finish_reason     TEXT,                          -- stop / length / tool_calls / content_filter
  retried           INTEGER NOT NULL DEFAULT 0     -- 1 = 流式失败后降级非流式成功
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(substr(ts,1,10));
CREATE INDEX IF NOT EXISTS idx_llm_usage_session ON llm_usage(session_id, turn);

-- 工具调用台账：dispatch 每执行一次工具落一行。
-- 和 mcp_calls 的区别：那张是 MCP 客户端视角（stdio/http，谁调了 server），
-- 这张是 agent 运行时视角——哪个技能、哪场会话、第几轮、是否重试/缓存/分了片。
-- 不合并：agent 调用可以不经过 MCP，MCP 也会被平台之外的东西直接调。
-- error_kind 单独一列，是因为"参数错"和"执行错"的处置完全不同：
-- 前者改 schema/描述，后者修实现；混成一个 error 字符串就分不出来。
CREATE TABLE IF NOT EXISTS tool_calls (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  tool         TEXT NOT NULL,
  source       TEXT NOT NULL DEFAULT 'console',    -- console / orchestrator / harness
  skill        TEXT NOT NULL DEFAULT '-',
  session_id   TEXT,
  turn         INTEGER NOT NULL DEFAULT 0,
  trace_id     TEXT,                               -- 和 llm_usage / turn_facts 同一个键
  read_only    INTEGER NOT NULL DEFAULT 1,
  status       TEXT NOT NULL,                      -- ok / error
  error_kind   TEXT,                               -- invalid_args / unknown_tool / timeout / bad_json / failed
  error        TEXT,
  latency_ms   INTEGER NOT NULL DEFAULT 0,
  result_chars INTEGER NOT NULL DEFAULT 0,
  retried      INTEGER NOT NULL DEFAULT 0,
  cached       INTEGER NOT NULL DEFAULT 0,
  ref          TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool, status, ts);

-- ============ 技能版本（2026-11-12）：技能的"提交历史" ============
-- 为什么不用 skills/.backup/ 目录当版本库：备份只有"文件 + 时间"，回答不了
-- "when 是哪一次改窄的、为什么改"。要 diff 还得自己按时间戳找两份文件，
-- 而时间戳在连续保存时还会撞（同一秒两次保存只剩一个备份）。
-- 版本表把正文本身存下来，diff / 回滚 / 审计都能直接做，也能与 skill_runs 对齐看
-- "这个版本跑过几次"。
CREATE TABLE IF NOT EXISTS skill_versions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  skill      TEXT NOT NULL,
  version    INTEGER NOT NULL,
  text       TEXT NOT NULL,
  sha        TEXT NOT NULL,                    -- 前 16 位，用来判断"内容没变，不记一版"
  message    TEXT,                             -- 这次改了什么，人写的
  source     TEXT NOT NULL DEFAULT 'save',      -- save / toggle / revert / import
  created_at TEXT NOT NULL,
  UNIQUE(skill, version)
);
CREATE INDEX IF NOT EXISTS idx_skill_versions ON skill_versions(skill, version DESC);

-- ============ Span 持久化（2026-11-12 夜）：本地调用树 ============
-- 为什么非要落库，而不是"起个 Phoenix 看"：
-- 观测的价值有一半在**事后**——出问题的是昨天那一轮、或者进程已经崩了。
-- 依赖外部后端 = 崩溃那几秒的调用树直接消失，而崩之前的调用树恰恰是唯一线索。
-- 所以本地存一份，OTel 后端变成"可选的更好看的视图"，不是数据源。
--
-- 为什么不用 OTel 的 SQLite exporter：它按 batch 导，且 schema 是 OTel 自己的
-- （属性全塞进一个 JSON 数组），要查"哪个工具最慢"得先在 SQL 里解 JSON。
-- 这里按自己要回答的问题建列：name/kind 进 GROUP BY，duration_ms 进 ORDER BY，
-- trace_id/parent_id 建树。属性仍然存 JSON，但它是**补充**，不是唯一入口。
--
-- 量级：一轮对话约 20~40 个 span，每行约 300~800 字节（属性已截断）。
-- 一天 100 轮 ≈ 3MB，保留 14 天可接受；超期由 prune_spans() 收拾。
CREATE TABLE IF NOT EXISTS spans (
  span_id     TEXT PRIMARY KEY,                -- 16 hex，同 OTel 格式
  trace_id    TEXT NOT NULL,                   -- 32 hex，与 llm_usage / tool_calls / turn_facts 同键
  parent_id   TEXT,                            -- 16 hex；根 span 为 NULL
  name        TEXT NOT NULL,                   -- agent.console / agent.segment.1 / llm.chat / tool.build_cards
  kind        TEXT NOT NULL DEFAULT 'CHAIN',   -- AGENT / CHAIN / LLM / TOOL / RETRIEVER
  session_id  TEXT,
  turn        INTEGER NOT NULL DEFAULT 0,
  seq         INTEGER NOT NULL DEFAULT 0,      -- 同 trace 内开始顺序，毫秒撞车时靠它稳定排序
  start_ts    TEXT NOT NULL,
  end_ts      TEXT,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'ok',      -- ok / error
  error       TEXT,
  attrs_json  TEXT NOT NULL DEFAULT '{}',
  events_json TEXT NOT NULL DEFAULT '[]',      -- [{t: 相对毫秒, n: 名称, a: {属性}}]
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id, seq);
CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_id, turn);
CREATE INDEX IF NOT EXISTS idx_spans_ts ON spans(start_ts);
CREATE INDEX IF NOT EXISTS idx_spans_name ON spans(name, duration_ms);

-- 大文本附件：prompt 原文、工具入参、工具返回全文。
--
-- 为什么不塞回 spans.attrs_json：
-- 1) 属性表有预算（要能整行读进内存做树），一段 8k 字的 prompt 会把同 span 的
--    维度键（tool.name / token 数）挤出预算——**丢证据的顺序刚好反了**。
-- 2) 详情页要一次渲染几十行，全文跟着树一起传会把页面拖死；单独按需取才好。
-- 3) 重复率高（同一 system prompt 每轮都带），单独一张表才好做去重与压缩。
--
-- 用 gzip：prompt / JSON 的压缩率通常 3~5 倍，SQLite 的 BLOB 不做压缩，
-- 这一层是把"全文都要留"的成本压到可接受的关键。读的时候在主进程解压。
--
-- 与 spans 不做外键：span 写入是"随手插一行就 commit"，父行还没写时子 span
-- 可能已经在写了（并行线程），加外键会让一批 span 因为顺序问题整行丢失。
-- 清理靠 prune 时按 span_id 反查，删 spans 顺带删 blob。
--
-- 2026-11-13 起正文按 hash 去重（见 blob_texts）：实测当前埋点下重复率很低
-- （43 条引用里 1 条重复），因为每次 llm.chat 记的 messages 都不一样。
-- 它现在的价值是"防"而不是"省"：一旦有父子 span 记同一段 prompt（最容易发生的
-- 是父 span 记入参、子 span 记完整 messages），重复就会成倍出现，而那时再改
-- 存储格式要搬几十 MB 的 blob。老数据没有 hash，gz 仍留在本表，读路径兼容两种形态。
CREATE TABLE IF NOT EXISTS span_blobs (
  span_id    TEXT NOT NULL,
  key        TEXT NOT NULL,                    -- input.value / output.value / tool.args / e0.input
  text_len   INTEGER NOT NULL DEFAULT 0,       -- 这条引用的原文**字符**数（可能被单条上限截过）
  gz_len     INTEGER NOT NULL DEFAULT 0,       -- 压缩后字节数（引用口径，实际占用看 blob_texts）
  gz         BLOB NOT NULL,                    -- 老行/未去重行：正文在这里；新行：空 blob
  hash       TEXT,                             -- 正文指纹，指向 blob_texts.hash
  created_at TEXT NOT NULL,
  PRIMARY KEY (span_id, key)
);
CREATE INDEX IF NOT EXISTS idx_span_blobs_ts ON span_blobs(created_at);
-- 注意：hash 上的索引不写在这里。老库的 span_blobs 还没有 hash 列，
-- 而 executescript 跑在建表之后、补列之前——在这里建索引会 "no such column"，
-- 整个应用起不来。索引统一在 db._ensure_columns 里补完列之后建。

-- 正文去重表：一段文本只存一份 gzip，多个 span(key) 共享。
--
-- 为什么值得单独一张表：只要有一层 span 把"父 span 的入参"再记一次，或者同一份
-- 工具返回被 CHAIN 与 TOOL 两层各记一次，字节就会成倍重复。当前埋点下这种重复
-- 很少（实测 43 条引用里 1 条），所以这是一份便宜的保险：写入端多一次
-- INSERT OR IGNORE，读取端多一次 join，换来"以后加了重复埋点也不会翻倍"。
--
-- text_len 记的是**实际存下来的**字符数（可能已被单条上限截断）；
-- "这份引用原本多长"记在 span_blobs.text_len 上——两者不一致正是"存过又被截"的证据。
CREATE TABLE IF NOT EXISTS blob_texts (
  hash       TEXT PRIMARY KEY,                 -- sha256(正文) 前 32 位
  text_len   INTEGER NOT NULL DEFAULT 0,
  gz_len     INTEGER NOT NULL DEFAULT 0,
  gz         BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blob_texts_ts ON blob_texts(created_at);

-- ---------------------------------------------------------------------------
-- 审计日志（差距清单"鉴权+审计"项）。
--
-- 只记两类请求，其余不记：
-- 1) 写操作（POST/PUT/DELETE/PATCH）——个人项目里读没有追责价值，记 GET 只是噪音；
-- 2) 被拒的请求（401）——这是唯一有"入侵尝试"信号的记录。
--
-- body 一律不落库：审计表不该成为第二个简历副本；排查业务问题去 spans 表。
-- actor 语义：单 token 设计，带有效 token = owner；401 = anonymous；
-- 鉴权关闭（APP_TOKEN 为空）时写操作记 local，明确表示"没有身份边界在生效"。
CREATE TABLE IF NOT EXISTS audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,                    -- ISO，与 spans.start_ts 同风格
  actor       TEXT NOT NULL DEFAULT 'anonymous',
  action      TEXT NOT NULL,                    -- HTTP 方法
  path        TEXT NOT NULL,
  query       TEXT,
  ip          TEXT,
  status      INTEGER NOT NULL,
  duration_ms REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);

-- ---------------------------------------------------------------------------
-- 告警历史（app/observability/alerts.py）。
--
-- 判定在本地 spans 表上做（滚动窗口的错误率 / 慢调用数），推送走 webhook。
-- 每次触发都落一行——**没送达也要落**：`delivered=0` + `error` 让"告警没发出去"
-- 本身可被看见，否则 webhook 断了以后告警变成静默失败，等于没有告警。
-- cooldown 靠查这张表实现，不放在内存里：重启不丢冷却状态。
CREATE TABLE IF NOT EXISTS alerts (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,                    -- ISO，触发时间
  rule      TEXT NOT NULL,                    -- error_rate | slow_calls
  severity  TEXT NOT NULL DEFAULT 'warning',
  message   TEXT NOT NULL,                    -- 中文可读正文（即推送内容）
  metrics   TEXT NOT NULL DEFAULT '{}',       -- 触发时的判定数据快照（JSON）
  delivered INTEGER NOT NULL DEFAULT 0,       -- 1=webhook 返回了 2xx
  error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);
