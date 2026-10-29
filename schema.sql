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
