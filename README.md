# job-prep：求职准备 Agent

一个面向后端 / Agent 开发 / 大数据三个面试方向的**个人求职准备 Agent**。
不是一个聊天机器人，而是一条完整的数据流水线：

```
面经采集 → LLM 抽题 → 人工确认 → 题目池/知识卡 → 模拟面试评分
    → 错题归因 → 掌握度更新 → SRS 复习调度 → 学习教练 Agent 生成计划
                              ↑ 全程评测门禁保护（CI）
```

## 为什么这个项目值得看

面试 Agent 岗位时，"做了个 Agent"没有区分度，有区分度的是工程细节。
这个项目刻意做深了以下几块：

**1. 评测驱动，不是感觉驱动**（`evals/`）

- 77 例分档标注的评分校准集，`band_accuracy=0.961`，同时报告多数类基线（0.481）与 lift——
  只报准确率不报基线，标注偏斜时高分是假的；
- 检索评测按 keyword / paraphrase / distractor 三类查询分桶，三档检索（FTS → 混合 → 精排）
  的每一段收益都能归因：口语改写桶 FTS 只有 0.333、向量补到 1.0、rerank 把 MRR 拉到 1.0；
- Agent 任务级评测不只看"有没有输出"，而是查工具调用数、数据引用、时间预算；
- GitHub Actions 双层门禁：零成本快检每次 push 必跑，全量评测（百次 LLM 调用）不达标直接红。

**2. 混合检索有真实的踩坑记录**（`app/services/rag_service.py`）

SQLite FTS5 对中文的坑被逐一踩掉：外部内容表列名对不上会静默 404、`unicode61`
把整句中文当一个 token、归档卡片的残留向量会让它从向量路"复活"。
修法与原则都写在代码注释和 `阶段6验收报告.md` 里。

**3. 写操作一律 Human-in-the-loop**（`app/services/multi_agent.py`）

教练 Agent 可以起草建卡 / 提前复习 / 合并重复卡 / 归档过时卡四类动作，
但全部要人工审批才落库。审批执行的每一步（数据库状态、向量清理、Markdown frontmatter 同步）
都有冒烟测试兜底。

**4. Markdown 是唯一事实源**

知识卡片是 Obsidian 可直接编辑的 md 文件（YAML frontmatter + wiki 链接），
SQLite 只是索引与状态库。换个空库、CI 环境、别的机器，启动即自动收编，不需要种子数据。

**5. 硬约束由代码兜底，不由 prompt 兜底**

prompt 里写"单日不超过 60 分钟"，模型实测仍会给 65——所以 harness 先让模型自我取舍一次
（trace 里有 `budget_violation` 记录），仍超预算再按优先级裁剪并显式标记 `trimmed`。

## 快速开始

```bash
git clone https://github.com/langbi25/job-prep.git && cd job-prep
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                             # 填 LLM_API_KEY（不填则非 LLM 功能可用）

uvicorn app.main:app --port 8765                 # 后端
streamlit run ui/streamlit_app.py                # 前端（另一终端）
```

Docker：`docker compose up -d --build`（2 核 2G 云服务器可跑，见 `deploy/README.md`）。

- API 文档：http://localhost:8765/docs
- 浏览器一键投喂面经：Streamlit 首页有 bookmarklet
- Wiki 用法：用 Obsidian 打开 `knowledge/` 目录即可，正常记笔记，Agent 侧自动收编

## 评测

```bash
python scripts/run_evals.py --gate    # 全量评测 + 门禁（约 3~4 分钟）
python scripts/ci_gate.py             # 零 LLM 调用，判定最近一次结果
```

当前成绩：band_accuracy 0.961（lift +0.481）· json_validity 1.0 · 检索 hit@5 1.0（MRR 1.0）·
Agent 任务级 success 1.0。

## 文档索引

| 文档 | 内容 |
|---|---|
| `evals/README.md` | 评测集设计：标注原则、为什么要看 lift、阈值标定依据 |
| `阶段6验收报告.md` / `阶段7验收报告.md` | 每轮实测数字 + 抓到并修掉的真实缺陷（面试可直接讲） |
| `deploy/README.md` | 2C2G 服务器部署（Docker Compose / systemd、swap、鉴权） |
| `prompts/*.yaml` | 全部 prompt 版本化注册，harness 渲染并记录版本号 |
