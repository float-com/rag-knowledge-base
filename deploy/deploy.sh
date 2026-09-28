#!/usr/bin/env bash
# =============================================================================
# RAG KB 一键部署脚本（宝塔 / Debian / Ubuntu + systemd）
#
# 【它能省掉什么】教程里要你手工完成十几步（建软链接、改 .env、起容器、装依赖、
# 跑迁移、写两个 systemd unit、装 nginx 配置、构建前端）。本脚本把这些串成一条命令，
# 并在每一步【先校验再执行】—— 尤其是数据库端口有没有真的收回到回环地址。
#
# 【用法】
#   1) 先把仓库放到 /www/wwwroot/rag-kb（git clone 或宝塔面板上传解压）
#   2) 在项目根创建 .env（cp .env.example .env）并改好生产值
#   3) bash deploy/deploy.sh
#
# 【可重复执行】每次重新部署都可以直接再跑一遍（各步骤幂等）。
#
# 【它不做的三件事】
#   · 不替你改 .env 里的密钥（那是你的生产凭据，脚本不碰）
#   · 不替你申请 HTTPS 证书（宝塔面板里一键即可）
#   · 不替你预热 Docling 模型（单独一步，见脚本末尾提示；有 make/HTTP 两种方式）
# =============================================================================

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/www/wwwroot/rag-kb}"
LOG_DIR="/var/log/rag-kb"
UV_BIN="${UV_BIN:-}"

# ---------- 输出helpers ----------
c_ok()   { printf '\033[32m  [OK]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m  [WARN]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[31m  [FAIL]\033[0m %s\n' "$*"; }
c_step() { printf '\n\033[36m== %s ==\033[0m\n' "$*"; }

die() { c_err "$*"; exit 1; }

# ---------- 0. 前置检查 ----------
c_step "0/8 前置检查"

[[ -d "$PROJECT_DIR" ]] || die "找不到项目目录 $PROJECT_DIR（请先 clone 或上传到该路径）"
cd "$PROJECT_DIR"

command -v docker >/dev/null 2>&1 || die "未安装 docker（宝塔面板 → Docker 里安装）"
docker compose version >/dev/null 2>&1 || die "docker compose 不可用（需要 Docker Compose v2）"
c_ok "docker 与 docker compose 可用：$(docker compose version --short 2>/dev/null || echo '?')"

# !override 需要 v2.24+；这里只做提示，真正的验证在第 3 步看端口
compose_ver="$(docker compose version --short 2>/dev/null | tr -d 'v' || echo 0.0.0)"
c_ok "compose 版本 $compose_ver（!override 需要 >= 2.24）"

[[ -f .env ]] || die "缺少 .env（先执行：cp .env.example .env 并改好生产值）"
c_ok "找到 .env"

if [[ -z "$UV_BIN" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  else
    die "找不到 uv。宝塔面板装好 Python 3.12 后，用 'which uv' 查到路径，然后：
       UV_BIN=/www/server/pyporject_evn/versions/3.12.x/bin/uv bash deploy/deploy.sh"
  fi
fi
c_ok "uv 路径：$UV_BIN"

# ---------- 1. .env 关键项自检（只提示，不阻断：有的项缺失只是功能降级） ----------
c_step "1/8 .env 关键项自检"

getenv() { grep -E "^$1=" .env | head -1 | cut -d= -f2- ; }

warn_if() { # $1=变量名 $2=提示
  local v; v="$(getenv "$1" || true)"
  [[ -z "$v" ]] && c_warn "$1 为空：$2"
}

# ① 生产标记：没有它，config.py 的 JWT 硬校验不会触发
[[ "$(getenv ENVIRONMENT)" == "production" ]] \
  && c_ok "ENVIRONMENT=production（JWT 硬校验已启用）" \
  || c_warn "ENVIRONMENT 不是 production —— JWT 密钥缺失/过短将【不会】被拦下"

# ② JWT 密钥：长度 + 是否还是模板里的公开值
jwt="$(getenv JWT_SECRET || true)"
if [[ ${#jwt} -lt 32 ]]; then
  c_warn "JWT_SECRET 长度 ${#jwt} < 32（生产环境会被拒绝启动，需换成 openssl rand -hex 32）"
elif [[ "$jwt" == change-me* || "$jwt" == "change-me-please-use-a-strong-random-secret" ]]; then
  c_warn "JWT_SECRET 还是 .env.example 里的公开模板值 —— 必须换掉（它在 GitHub 上人人可见）"
else
  c_ok "JWT_SECRET 长度 ${#jwt}，且不是模板值"
fi

# ③ 管理员口令：占位符和 admin 都算没改
adm="$(getenv DEFAULT_ADMIN_PASSWORD || true)"
case "$adm" in
  ""|admin|password|123456|CHANGE_ME_BEFORE_FIRST_START)
    c_warn "DEFAULT_ADMIN_PASSWORD 仍是弱值/占位符（'$adm'）—— 库首次启动时会用它建号" ;;
  *) c_ok "DEFAULT_ADMIN_PASSWORD 已自定义" ;;
esac

# ④ 端口对齐：override 把端口改成了 55432/56379，.env 里的连接串必须跟着改
dburl="$(getenv DATABASE_URL || true)"
case "$dburl" in
  *localhost:55432*) c_ok "DATABASE_URL 指向 55432（与 override 对齐）" ;;
  *localhost:5432*)  c_warn "DATABASE_URL 还是 5432 —— override 会把 pg 映射到 55432，两者对不上会连不上库" ;;
  *) c_warn "DATABASE_URL 非预期：$dburl" ;;
esac
for k in REDIS_URL CELERY_BROKER_URL CELERY_RESULT_BACKEND; do
  v="$(getenv "$k" || true)"
  case "$v" in
    *:56379*) : ;;
    *:6379*)  c_warn "$k 还是 6379 —— override 会映射到 56379，请改成 56379" ;;
  esac
done

# ⑤ CORS：留 localhost 的话线上前端会被拦
cors="$(getenv CORS_ORIGINS || true)"
case "$cors" in
  *localhost*) c_warn "CORS_ORIGINS 仍含 localhost：$cors（线上前端会被 CORS 拦掉）" ;;
  *) c_ok "CORS_ORIGINS：$cors" ;;
esac

# ---------- 2. 端口覆盖软链接 ----------
c_step "2/8 建立 override 软链接"

if [[ -L docker-compose.override.yml ]]; then
  c_ok "docker-compose.override.yml 软链接已存在"
elif [[ -e docker-compose.override.yml ]]; then
  c_warn "docker-compose.override.yml 已存在但不是软链接，跳过（请确认内容是否为端口覆盖）"
else
  ln -s deploy/docker-compose.override.yml docker-compose.override.yml
  c_ok "已创建软链接 → deploy/docker-compose.override.yml"
fi

# ---------- 3. 起 postgres + redis 并【验证端口收回回环】 ----------
c_step "3/8 启动 PostgreSQL 与 Redis"

docker compose up -d postgres redis
c_ok "容器已启动，等待健康检查…"

for i in $(seq 1 30); do
  healthy=$(docker compose ps --format '{{.Name}} {{.Health}}' 2>/dev/null | grep -c healthy || true)
  [[ "$healthy" -ge 2 ]] && break
  sleep 2
done
docker compose ps

# ★ 关键安全校验：数据库/Redis 的端口绝不能绑到 0.0.0.0
ports_out="$(docker compose ps --format '{{.Name}} {{.Ports}}' 2>/dev/null || true)"
if echo "$ports_out" | grep -E '0\.0\.0\.0:(55432|5432|56379|6379)' >/dev/null 2>&1; then
  echo "$ports_out"
  die "数据库/Redis 端口绑到了 0.0.0.0 —— 公网可直接连！
      原因通常是 override 的 '!override' 未生效（Compose < 2.24）。
      请先 docker compose down，升级 Compose 或改用只 expose 不 ports 的写法，再重跑本脚本。"
fi
c_ok "端口已收回回环地址（未对公网暴露）"

# ---------- 4. 数据库迁移 ----------
c_step "4/8 执行数据库迁移（alembic upgrade head）"
cd "$PROJECT_DIR/backend"
"$UV_BIN" sync
"$UV_BIN" run alembic upgrade head
c_ok "迁移完成（pgvector / zhparser 扩展与业务表已就绪）"
cd "$PROJECT_DIR"

# ---------- 5. 安装 systemd 服务 ----------
c_step "5/8 安装并启动 systemd 服务（api + celery worker）"

mkdir -p "$LOG_DIR"

# uv 路径按实际探测结果替换（unit 文件里写的是宝塔默认路径，未必与你的机器一致）
for unit in rag-kb-api rag-kb-celery; do
  sed "s#^ExecStart=.*uv run#ExecStart=$UV_BIN run#" "deploy/$unit.service" > "/tmp/$unit.service"
  sudo cp "/tmp/$unit.service" "/etc/systemd/system/$unit.service"
done
c_ok "已写入 /etc/systemd/system/rag-kb-{api,celery}.service（ExecStart 用 $UV_BIN）"

sudo systemctl daemon-reload
sudo systemctl enable --now rag-kb-api
sudo systemctl enable --now rag-kb-celery
sleep 5

for unit in rag-kb-api rag-kb-celery; do
  if systemctl is-active --quiet "$unit"; then
    c_ok "$unit 运行中"
  else
    c_err "$unit 未运行 —— 最近日志："
    sudo journalctl -u "$unit" -n 20 --no-pager || true
    exit 1
  fi
done

# 后端健康检查
if curl -fsS http://127.0.0.1:8100/api/health >/dev/null 2>&1; then
  c_ok "后端健康检查通过：$(curl -fsS http://127.0.0.1:8100/api/health)"
else
  c_warn "后端健康检查未通过（可能还在启动），稍后手动验证：
       curl http://127.0.0.1:8100/api/health"
fi

# ---------- 6. Nginx ----------
c_step "6/8 配置 Nginx"

if [[ -f /etc/nginx/conf.d/rag-kb.conf ]]; then
  c_ok "已存在 /etc/nginx/conf.d/rag-kb.conf（跳过拷贝，如需更新请手动覆盖后 nginx -s reload）"
else
  sudo cp "$PROJECT_DIR/deploy/nginx.conf" /etc/nginx/conf.d/rag-kb.conf
  c_ok "已写入 /etc/nginx/conf.d/rag-kb.conf"
fi
c_warn "⚠️ 请确认 nginx.conf 里的 server_name 与 root 路径已改成你的实际值（宝塔面板里也能改）"

sudo nginx -t && sudo nginx -s reload && c_ok "Nginx 配置校验通过并已 reload" \
  || c_warn "nginx -t 未通过，请先修 /etc/nginx/conf.d/rag-kb.conf 再 reload"

# ---------- 7. 前端构建 ----------
c_step "7/8 构建前端"

if ! command -v npm >/dev/null 2>&1; then
  c_warn "未找到 npm —— 跳过前端构建（宝塔面板装 Node 后，手动执行：cd frontend && npm install && npm run build）"
else
  cd "$PROJECT_DIR/frontend"
  # 国内服务器建议走镜像（宝塔装的 npm 一般已配，这里不强制改全局配置）
  npm install
  npm run build
  c_ok "前端产物已生成：$PROJECT_DIR/frontend/dist"
  cd "$PROJECT_DIR"
fi

# ---------- 8. 收尾提示 ----------
c_step "8/8 部署完成，剩余手工项"
cat <<'EOF'
  ① 预热 Docling 模型（不做的话 PDF 解析会失败 —— 第 12 期踩过的坑）
       cd /www/wwwroot/rag-kb/backend && uv run python scripts/preheat_models.py
     完成后建议在 .env 里设 HF_HUB_OFFLINE=true，避免每次启动联网

  ② 重置管理员口令（若数据库【已存在】用户，改 .env 是无效的）
       cd /www/wwwroot/rag-kb/backend
       H=$(uv run python -c "from app.core.security import hash_password; print(hash_password('你的新口令'))")
       docker compose exec -T postgres psql -U rag -d rag_kb \
         -c "update users set password_hash='$(echo $H | tr -d '\r')' where username='admin';"

  ③ 验证 MCP 通道（对外 Agent 用）
       用 MCP Inspector 连 http://<你的域名或IP>/mcp ，应能列出 5 个工具
       ⚠️ 工具【调用】需要带 Authorization: Bearer <JWT>；不带会返回"请先登录"（这是设计如此）

  ④ HTTPS：宝塔面板 → 网站 → SSL → Let's Encrypt 一键申请
       证书装好后，记得取消 nginx.conf 顶部 http→https 跳转段的注释
EOF

c_ok "全部脚本步骤执行完毕"
