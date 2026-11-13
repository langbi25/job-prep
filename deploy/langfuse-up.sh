#!/usr/bin/env bash
# 一键起本地 Langfuse（Docker Desktop，wsl2 引擎）
#
# 用法：
#   bash deploy/langfuse-up.sh            # 起服务（幂等，已有数据不重建）
#   bash deploy/langfuse-up.sh --status   # 看容器状态与健康检查
#   bash deploy/langfuse-up.sh --down     # 停服务（保留数据卷）
#   bash deploy/langfuse-up.sh --reset    # 停服务并清空数据（危险，需二次确认）
#   bash deploy/langfuse-up.sh --dry-run  # 只生成密钥文件并打印接入配置，不启动容器
#
# 前置条件：Docker Desktop 引擎可达。若报引擎不可用，说明 wsl.exe 被拦：
#   安全中心 -> 命令安全 -> 程序黑名单 -> 移除 wsl.exe，然后重启 Docker Desktop。
#
# 内存要求：>= 6 GB 空闲（postgres + clickhouse + redis + minio + web + worker）。
set -uo pipefail

cd "$(dirname "$0")" || exit 1

ENV_FILE=.env.langfuse
COMPOSE=langfuse-compose.yml
PUBLIC_PORT=3000
# 只起核心链路，跳过 ai-gateway（多一个容器，本项目用不到）
SERVICES="postgres clickhouse redis minio langfuse-web langfuse-worker"

log() { printf '%s\n' "$*"; }
die() { printf '\n[失败] %s\n' "$*" >&2; exit 1; }

# ---------- 定位 docker ----------
find_docker() {
  if [ -n "${DOCKER_BIN:-}" ] && [ -x "$DOCKER_BIN" ]; then echo "$DOCKER_BIN"; return; fi
  if command -v docker >/dev/null 2>&1; then command -v docker; return; fi
  for p in "/e/Docker/resources/bin/docker.exe" \
           "/c/Program Files/Docker/Docker/resources/bin/docker.exe" \
           "/d/Docker/resources/bin/docker.exe"; do
    [ -x "$p" ] && { echo "$p"; return; }
  done
}

DOCKER="$(find_docker)"
[ -n "$DOCKER" ] || die "找不到 docker 命令。确认 Docker Desktop 已安装，或设置 DOCKER_BIN 指向 docker.exe"

# compose v2 是 docker 的插件，用 docker compose 调用
dc() { "$DOCKER" compose --env-file "$ENV_FILE" -f "$COMPOSE" "$@"; }

# ---------- 引擎可用性 ----------
engine_ok() { "$DOCKER" info >/dev/null 2>&1; }

check_engine() {
  if engine_ok; then return 0; fi
  cat >&2 <<'EOF'

[失败] Docker 引擎不可达（npipe dockerDesktopLinuxEngine 不存在）。

根因排查（按顺序）：
  1. Docker Desktop 没启动 —— 从开始菜单打开，等鲸鱼图标变绿
  2. wsl.exe 被安全策略拦截 —— 安全中心 → 命令安全 → 程序黑名单，移除 wsl.exe
     本机已装 Ubuntu20.04 发行版，Docker 用的是 wsl2 引擎，解禁后即可启动
  3. 虚拟化没开 —— 任务管理器 → 性能 → CPU，确认"虚拟化：已启用"

放行后重跑本脚本即可，无需改任何配置。
EOF
  exit 2
}

# ---------- 生成密钥 ----------
rand_hex() { openssl rand -hex "${1:-16}"; }

gen_env() {
  [ -f "$ENV_FILE" ] && { log "[跳过] $ENV_FILE 已存在，沿用原密钥"; return; }

  local pk="pk-lf-local-$(rand_hex 8)"
  local sk="sk-lf-local-$(rand_hex 8)"
  local pw; pw="$(rand_hex 6)"

  log "[生成] $ENV_FILE（随机密钥 + 自动建项目与 API Key）"
  cat > "$ENV_FILE" <<EOF
# 由 deploy/langfuse-up.sh 自动生成，勿提交
# 修改后需 bash deploy/langfuse-up.sh --down 再起才生效
LANGFUSE_IMAGE_TAG=4
NEXTAUTH_URL=http://localhost:${PUBLIC_PORT}
NEXTAUTH_SECRET=$(rand_hex 32)
SALT=$(rand_hex 16)
ENCRYPTION_KEY=$(openssl rand -hex 32)

POSTGRES_USER=postgres
POSTGRES_PASSWORD=$(rand_hex 16)
POSTGRES_DB=postgres
DATABASE_URL=postgresql://postgres:$(grep '^POSTGRES_PASSWORD=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)@postgres:5432/postgres

CLICKHOUSE_USER=clickhouse
CLICKHOUSE_PASSWORD=$(rand_hex 16)

REDIS_AUTH=$(rand_hex 16)

MINIO_ROOT_USER=minio
MINIO_ROOT_PASSWORD=$(rand_hex 16)

# S3（minio）凭证要保持三处一致
LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY=__MINIO_PW__
LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY=__MINIO_PW__
LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY=__MINIO_PW__

# 首次启动自动创建组织/项目/账号/API Key，省掉手点 UI
LANGFUSE_INIT_ORG_ID=jobprep-org
LANGFUSE_INIT_ORG_NAME=job-prep
LANGFUSE_INIT_PROJECT_ID=jobprep-local
LANGFUSE_INIT_PROJECT_NAME=job-prep-local
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=${pk}
LANGFUSE_INIT_PROJECT_SECRET_KEY=${sk}
LANGFUSE_INIT_USER_EMAIL=admin@local.dev
LANGFUSE_INIT_USER_NAME=admin
LANGFUSE_INIT_USER_PASSWORD=${pw}
EOF

  # 回填 minio 密码到三处 S3 配置
  local mp; mp="$(grep '^MINIO_ROOT_PASSWORD=' "$ENV_FILE" | cut -d= -f2)"
  sed -i "s/__MINIO_PW__/${mp}/g" "$ENV_FILE"
  # DATABASE_URL 里引用的密码在文件生成时还拿不到，这里统一修正
  local pp; pp="$(grep '^POSTGRES_PASSWORD=' "$ENV_FILE" | cut -d= -f2)"
  sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://postgres:${pp}@postgres:5432/postgres|" "$ENV_FILE"

  chmod 600 "$ENV_FILE"
  log "[完成] 登录账号 admin@local.dev / 密码 ${pw}"
}

env_val() { grep "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2-; }

print_conn() {
  local pk sk b64
  pk="$(env_val LANGFUSE_INIT_PROJECT_PUBLIC_KEY)"
  sk="$(env_val LANGFUSE_INIT_PROJECT_SECRET_KEY)"
  b64="$(printf '%s:%s' "$pk" "$sk" | base64 | tr -d '\n')"

  cat <<EOF

================ 接入配置（复制进 job-prep/.env 或启动环境变量）================
TRACING_ENDPOINT=http://127.0.0.1:${PUBLIC_PORT}/api/public/otel
TRACING_HEADERS=Authorization=Basic ${b64};x-langfuse-ingestion-version=4
OBS_BACKEND=langfuse
LANGFUSE_PUBLIC_KEY=${pk}
LANGFUSE_SECRET_KEY=${sk}
LANGFUSE_HOST=http://127.0.0.1:${PUBLIC_PORT}
============================================================================

界面：http://localhost:${PUBLIC_PORT}
账号：admin@local.dev（密码见上方首次生成日志，或 $ENV_FILE 里的 LANGFUSE_INIT_USER_PASSWORD）
EOF
}

wait_ready() {
  log "[等待] langfuse-web 就绪（最多 180s）..."
  local i code
  for i in $(seq 1 90); do
    code="$(curl -s -m 3 --noproxy '*' -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PUBLIC_PORT}/api/public/health" 2>/dev/null)"
    if [ "$code" = "200" ]; then log "[就绪] web 已响应（${i}0% 进度内）"; return 0; fi
    sleep 2
  done
  log "[警告] 180s 内未就绪，看日志：$DOCKER compose -f $COMPOSE logs --tail 50 langfuse-web"
  return 1
}

# ---------- 入口 ----------
case "${1:-up}" in
  --status)
    [ -f "$ENV_FILE" ] || die "$ENV_FILE 不存在，服务尚未初始化过"
    dc ps
    echo
    log "健康检查："
    curl -s -m 5 --noproxy '*' "http://127.0.0.1:${PUBLIC_PORT}/api/public/health" || log "web 未响应"
    exit 0
    ;;
  --down)
    [ -f "$ENV_FILE" ] || die "$ENV_FILE 不存在"
    dc stop
    log "[完成] 已停止（数据卷保留，重跑本脚本即可恢复）"
    exit 0
    ;;
  --reset)
    [ -f "$ENV_FILE" ] || die "$ENV_FILE 不存在"
    printf '将删除 Langfuse 全部数据卷（trace 全丢）。输入 yes 继续：'
    read -r ans
    [ "$ans" = "yes" ] || { log "已取消"; exit 1; }
    dc down -v
    log "[完成] 数据已清空"
    exit 0
    ;;
  --dry-run)
    # 只生成 env 并打印接入配置，不碰 docker
    gen_env
    print_conn
    exit 0
    ;;
esac

check_engine
gen_env

log "[启动] 拉取镜像并启动 ${SERVICES}（首次约 5-10 分钟）"
dc up -d $SERVICES || die "compose 启动失败，看：$DOCKER compose -f $COMPOSE logs --tail 80"

wait_ready || true

log ""
log "[状态]"
dc ps

print_conn
