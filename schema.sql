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
