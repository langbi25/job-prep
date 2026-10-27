# Job Prep 知识库（阶段 0~1 骨架）

求职准备 Agent 的初始知识库实现。配套设计文档：《初始知识库实现方案.md》。

## 当前能力（阶段 0 + 阶段 1 起步）

- SQLite 建库（8 张表 + FTS5 全文索引，一次建好后续阶段直接用）
- 知识卡片：创建（自动写 Markdown 文件 + SQLite 行）、列表、检索、详情
- Markdown ↔ SQLite 同步：启动时全量扫描 `knowledge/`，Obsidian 手改的文件会被收编
- 面经采集状态机：粘贴/URL 投喂 → LLM 抽题 → 人工确认 → 面经落库 + 题目池去重入池
- 统计：卡片/面经分布、高频题 TopN

向量检索（Chroma + bge-small-zh）留到阶段 1 后半段，当前为关键词 + FTS，骨架保持轻量。

## 快速开始

```bash
cd job-prep

# 1. 建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. 配置 LLM（面经抽取用；不配置也能用卡片功能）
copy .env.example .env          # 填入 LLM_API_KEY

# 3. 初始化并启动后端
uvicorn app.main:app --reload --port 8765

# 4. 启动前端（另开终端）
streamlit run ui/streamlit_app.py
```

- API 文档：http://localhost:8765/docs
- 采集台：Streamlit 首页第一个标签页
- Wiki 用法：用 Obsidian 打开 `knowledge/` 目录即可，正常记笔记，Agent 侧自动收编

## 浏览器一键投喂（bookmarklet）

收藏夹新建书签，URL 填（把 localhost 换成你的服务器地址）：

```javascript
javascript:(function(){var u='http://localhost:8765/ingest?url='+encodeURIComponent(location.href)+'&sel='+encodeURIComponent((window.getSelection()+'').slice(0,8000));window.open(u,'_blank')})()
```

## 目录

```
job-prep/
├── schema.sql            # 全部建表 DDL（真相）
├── templates/            # Markdown frontmatter 模板
├── knowledge/            # 知识卡片 md（Obsidian 打开这里）
├── interviews/           # 面经原文归档
├── inbox/                # 采集暂存（pending / archived）
├── app/                  # FastAPI 后端
└── ui/                   # Streamlit 前端
```

## 验证

```bash
python scripts/smoke_test.py   # 建库 + 建卡 + 投喂 + 确认入库 全链路自检
```
