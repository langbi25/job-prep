# 部署指南（2核2G 服务器）

## 结论先说

LLM 走云端 API 的前提下，2核2G 够跑（内存预算 ~1.2-1.4G）。必须做三件事：**加 2G swap、embedding 用 small 模型、不引入数据库服务**。

## 方式一：Docker Compose（推荐）

```bash
# 服务器上
git clone <你的仓库> job-prep && cd job-prep
cp .env.example .env && vi .env        # 填 LLM_API_KEY
docker compose up -d --build           # 首次构建 + 下载 embedding 模型约 2 分钟

# 验证
curl http://localhost:8765/health
open http://<服务器IP>:8501
```

## 方式二：裸机 systemd（不用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
sudo tee /etc/systemd/system/jobprep-api.service > /dev/null <<'EOF'
[Unit]
Description=JobPrep API
After=network.target
[Service]
WorkingDirectory=/opt/job-prep
ExecStart=/opt/job-prep/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now jobprep-api
# Streamlit 同理，ExecStart 换成 streamlit run ui/streamlit_app.py --server.port 8501 --server.address 0.0.0.0
```

## 上线后自检（三件事）

```bash
curl -s http://localhost:8765/health            # 后端活着 + 数据库可读
curl -s "http://localhost:8765/api/search?q=消息丢失&mode=hybrid" | head -c 300
python scripts/run_evals.py --skip-llm          # 检索两档自检，秒级、不花钱
```

`docker-compose.yml` 里已经加了 healthcheck（首次启动 60s 宽限期，等建库 + 加载向量模型）
和 json-file 日志轮转（10MB × 3）。**2C2G 机器磁盘最常见的死法就是 uvicorn 日志写满 40G 盘**，
不要省这一行。

## 内存与稳定性（2C2G 必做）

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab   # 开机自动挂载
free -h   # 确认 swap 2G
```

实测占用：api 约 500~700MB（含 bge-small ONNX 模型），streamlit 约 250MB。
两个容器加起来约 1G，余量留给 OS 和构建缓存。

## 首次访问后要改的配置

1. **bookmarklet 指向服务器**：把 `http://localhost:8765` 换成 `http://<服务器IP>:8765`
2. **Streamlit 后端地址**：compose 已设 `JOBPREP_API=http://api:8765`；裸机部署在 `.env` 或启动命令里设 `JOBPREP_API=http://<服务器IP>:8765`
3. **公网暴露**：建议前置 Caddy/Nginx 加 Basic Auth，别裸奔（内含你的知识库和 API Key）

## 数据备份

```bash
cd /opt/job-prep && git init 2>/dev/null; git add -A && git commit -m "backup $(date +%F)"
# 或 crontab 每日 rsync 到另一台机器
```

## 已知限制（面试可讲）

- 单用户假设，无鉴权（多租户要加用户表 + JWT + 数据隔离）
- SQLite 单写者，多实例需换 Postgres
- 无监控告警，靠日志和 `/api/health`
