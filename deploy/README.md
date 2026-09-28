# deploy/ —— 服务器部署目录

> 期：**Day13 · MCP Server 集成（收尾）**
> 适用形态：**宝塔面板 + 宿主机跑后端（systemd）+ Docker 只跑 PG/Redis + Nginx 反代**
> 说明：本目录把教程里"要你在服务器上手工创建"的文件全部预先生成好并纳入版本控制，
> 服务器上 `git clone` 下来即可直接用，不必在 vi 里逐字敲。

---

## 一、文件清单

| 文件 | 作用 | 教程对应步骤 |
| --- | --- | --- |
| `deploy.sh` | ★ 一键部署脚本：前置校验 → 端口覆盖 → 起容器 → 验证端口收回回环 → 迁移 → 装 systemd → 配 Nginx → 构建前端 | 教程三~五节全部 |
| `docker-compose.override.yml` | 把 PG/Redis 端口收回到 `127.0.0.1:55432/56379` | 教程 3 节 |
| `rag-kb-api.service` | 后端 uvicorn 常驻（systemd） | 教程 4.1 节 |
| `rag-kb-celery.service` | Celery worker 常驻（systemd） | 教程 4.2 节 |
| `nginx.conf` | `/api/` 与 `/mcp` 反代 + 前端 SPA 回落 | 教程 5.2 节 |

**注意**：`deploy/docker-compose.override.yml` 是**源文件**（进仓库）；
根目录那个同名**软链接**是服务器上临时建立的（**已被 `.gitignore` 忽略**）。
原因：软链接若被提交，别人 clone 后本地 `docker compose up` 会莫名套用生产端口，
而他们本地 `.env` 写的是 5432/6379 → 连不上库且现象诡异。

---

## 二、最短部署路径（三条命令）

```bash
# ① 拿到代码（或宝塔面板上传压缩包到 /www/wwwroot/rag-kb）
cd /www/wwwroot
git clone https://github.com/float-com/rag-knowledge-base.git rag-kb

# ② 准备生产配置（照着注释改 4 项）
cd rag-kb
cp .env.example .env
vi .env
#   必改：POSTGRES_PASSWORD / JWT_SECRET(openssl rand -hex 32) / DEFAULT_ADMIN_PASSWORD / CORS_ORIGINS
#   对齐：POSTGRES_PORT=55432、DATABASE_URL 里的端口改 55432
#         REDIS_PORT=56379、REDIS_URL 与两个 CELERY_* 里的端口改 56379
#   加上：ENVIRONMENT=production   ← ★ 不加则 JWT 硬校验不触发（教程清单里没有这一项）

# ③ 一键部署
bash deploy/deploy.sh
#   若 uv 不在 PATH：UV_BIN=$(which uv) bash deploy/deploy.sh
```

脚本跑完还剩三件手工事（脚本末尾会再提示一遍）：

```
① 预热 Docling 模型（不做则 PDF 解析失败）
② 重置已存在库的 admin 口令（改 .env 对已建号无效）
③ 宝塔面板申请 HTTPS 证书
```

---

## 三、★ 脚本里的两道安全闸门（这是它比手工敲更值钱的地方）

### 闸门 1：验证数据库端口真的收回了回环

`deploy.sh` 在第 3 步起完容器后**强制检查**：

```
docker compose ps --format "{{.Name}} {{.Ports}}"
若出现 0.0.0.0:55432 / 0.0.0.0:5432 / 0.0.0.0:56379 / 0.0.0.0:6379
→ 直接报错退出，并提示原因（通常是 !override 未生效，Compose < 2.24）
```

**为什么必须查**：基础 `docker-compose.yml` 写的是 `"${POSTGRES_PORT:-5432}:5432"`，
**不写 IP = 绑定 0.0.0.0 = 公网可直连**。而 Compose 合并文件时 `ports` 默认是**追加**而非替换，
所以只写了 override 但没生效的话，结果是"0.0.0.0 和 127.0.0.1 各绑一次"——
**数据库照样裸奔，而且没有任何报错**。

### 闸门 2：`.env` 五项自检（含"模板值冒充合规值"）

脚本启动时会检查并告警（不阻断，因为部分项缺失只是功能降级）：

| 检查项 | 为什么 |
| --- | --- |
| `ENVIRONMENT=production` | 没有它，config.py 的 JWT 硬校验不会触发（教程清单里恰好没提这一项） |
| `JWT_SECRET` 长度 ≥ 32 **且不是模板值** | `.env.example` 里的 `change-me-please-use-a-strong-random-secret` 有 43 字符，**长度校验能过但它是公开值** —— 只查长度会漏掉这种"形似合规" |
| `DEFAULT_ADMIN_PASSWORD` | 占位符 `CHANGE_ME_BEFORE_FIRST_START` 与 `admin` 都算没改 |
| `DATABASE_URL` / `REDIS_URL` / `CELERY_*` 端口 | override 把端口改成 55432/56379，连接串不跟着改就连不上库 |
| `CORS_ORIGINS` | 留 `localhost` 会让线上前端被 CORS 拦掉 |

---

## 四、⚠️ 关于 `deploy.sh` 的诚实说明

```
· 语法已验：用 Git bash 跑过 `bash -n deploy/deploy.sh` → 退出码 0（无语法错误）
· ★ 但没有在真实服务器上跑过 —— 我只有 Windows 环境，没有 81.70.119.33 的凭据，
  也起不了 systemd / 装不了 Nginx，所以"逻辑正确"与"在你这台机器上一次跑通"是两回事。
· 因此脚本设计成【每步先校验再执行、且可重复执行】：
  某一节失败时直接重跑即可，不会因为"跑了一半"而留下半成品状态。
· 最可能需要现场调整的是 UV_BIN 路径（宝塔装的 Python 环境路径因机器而异），
  脚本已支持 UV_BIN=... 覆盖。
```

---

## 五、与教程的对照（本目录做了哪些"预生成"）

| 教程让你做的事 | 本目录的处理 |
| --- | --- |
| `mkdir deploy` 并新建 override | ✅ 已生成 `deploy/docker-compose.override.yml` |
| 新建两个 `*.service` 文件 | ✅ 已生成，且 `deploy.sh` 会用实测的 uv 路径替换 `ExecStart` |
| 新建 `deploy/nginx.conf` | ✅ 已生成，并补了教程 `/mcp` 段缺的 `chunked_transfer_encoding on` |
| `ln -s deploy/docker-compose.override.yml docker-compose.override.yml` | ✅ 脚本自动完成，且该软链接已被 `.gitignore` 忽略 |
| 逐条执行 docker compose / alembic / systemctl / npm build | ✅ `deploy.sh` 串成一条命令 |
| 指定 uv 绝对路径 | ✅ 脚本自动探测 `which uv`，也可用 `UV_BIN=` 覆盖 |

**没有替你做的**：改 `.env` 的密钥、申请 HTTPS 证书、预热模型（后两项宝塔面板一键即可）。
