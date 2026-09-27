# 03_Windows原生Redis抢占6379导致语义缓存不可用

> 发现日期：2026.09.26
> 发现阶段：**Day12 · 第 2 章「引入 Redis 与依赖」**（配置写完、跑连通性验证时暴露）
> 状态：**✅ 已修复**（停用并禁用 Windows 原生 Redis 服务）
> 性质：**环境冲突**（非代码 bug，但会让语义缓存完全不可用，且报错信息误导性极强）

---

## 一、问题一句话

**本机（Windows）上有一个 2015 年的原生 Redis 3.0.504 服务，一直占着 6379 端口；
而 Day12 要用的 Redis Stack（含 RediSearch 模块）跑在 Docker 里，端口映射登记成功却绑不上。
结果是：Python 连 6379 连的是那个老 Redis，SemanticCache 会因为"没有 RediSearch 模块"而彻底不可用。**

---

## 二、症状与误导点

配置与容器都正常，但 `MODULE` 命令报错：

```
redis.exceptions.ResponseError: unknown command 'MODULE'
```

**这个报错的误导性在于**：

| 直觉判断 | 实际情况 |
| --- | --- |
| "redis-py 版本太新，不认识这个命令？" | ❌ redis-py 没问题 |
| "容器镜像不对，没装 RediSearch？" | ❌ 镜像是对的（`redis/redis-stack-server:7.4.0-v0`） |
| "Docker 端口映射没生效？" | ❌ `docker port` 显示映射正常 |
| —— | ✅ **连的根本不是容器里的 Redis** |

**决定性线索**：`INFO server` 返回的 `redis_version = 3.0.504`。
而容器镜像明明是 7.4.0 —— 版本对不上，说明连接被"截胡"了。

---

## 三、根因链

```
Windows 上装过一个原生 Redis（C:\Program Files\Redis\redis-server.exe）
        ↓
它注册成 Windows 服务：服务名 Redis，StartMode = Auto（开机自启）
        ↓
它以 PID 5540 监听 0.0.0.0:6379
        ↓
Docker 启动 rag-kb-redis 时做端口映射 6379->6379：
    Docker 会登记这条映射（docker port 显示正常）
    但底层 bind 6379 失败（已被占）
        ↓
Windows 的端口分配不报错，只是【谁先占谁赢】
        ↓
于是 localhost:6379 实际连到的是那个 Redis 3.0.504
        ↓
Redis 3.x 没有 MODULE 命令 → 也读不到任何模块
        ↓
SemanticCache 依赖的 RediSearch（FT.CREATE / KNN 查询）全部不可用
```

**为什么 `docker port` 会"撒谎"**：它展示的是**配置意图**（compose 里写了 6379:6379），
不是**当前内核里真实的监听者**。判断端口真正归谁，要看 `Get-NetTCPConnection -LocalPort 6379`。

---

## 四、诊断过程（可复用的排查顺序）

```
① 看症状：MODULE 命令不认 / 版本号与预期不符
        ↓
② 怀疑"连错对象"，先确认端口真实占用者：
      Get-NetTCPConnection -LocalPort 6379 -State Listen
      → LocalAddress=0.0.0.0  PID=5540
      Get-Process -Id 5540 → redis-server.exe（路径在 C:\Program Files\Redis\）
        ↓
③ 与 Docker 的映射对照：
      docker port rag-kb-redis → 6379/tcp -> 0.0.0.0:6379
      结论：Docker 想用，但 Windows 进程先占了
        ↓
④ 查这个占用者是什么来头：
      Get-CimInstance Win32_Service -Filter "Name='Redis'"
      → PathName = "C:\Program Files\Redis\redis-server.exe" --service-run ...
      → StartMode = Auto
        ↓
⑤ 判断能不能安全停用：查它的库里有没有数据
      （实测：db0 只有 1 个键，还是本次验证脚本刚写进去的探针键 → 库是空的）
```

---

## 五、修复动作

```powershell
# 需要管理员权限（普通会话 Stop-Service 会报 "Cannot open Redis service"）
Stop-Service -Name Redis -Force
Set-Service  -Name Redis -StartupType Disabled
```

**实测执行结果**：

```
is_admin: True
stop_service: OK
disable_autostart: OK
service_now: Status=Stopped StartType=Disabled
port_6379: STILL_IN_USE pid=28444      ← 但这个 PID 已是 com.docker.backend（Docker 端口代理）
```

修复后验证：

```
redis_version = 7.4.0                  ✅ 不再是 3.0.504
已加载模块    = ReJSON RedisCompat bf redisgears_2 search timeseries
RediSearch    = True                   ✅ 语义缓存的前提具备
```

> 📌 **为什么必须设成 `Disabled` 而不只是 `Stop`**：
> 该服务是 **Auto 启动**。只 `Stop` 的话，**下次开机它会再次抢走 6379**，
> 而症状完全一样（版本号 3.0.504、MODULE 不认），排查一次要花很久。
> **一次禁用，永久解决。**

---

## 六、影响范围与风险评估

| 项 | 结论 |
| --- | --- |
| Windows Redis 里有没有真实数据 | ❌ 没有。db0 仅 1 个键，是本次验证脚本的探针键 |
| 停用会不会影响本项目 | ❌ 不会。Day12 之前所有数据都在 PostgreSQL，项目从未用过 Redis |
| 会不会影响你其它项目 | ⚠️ **需要你确认**。若别的本地项目依赖这个 Redis 服务，需要另行安排（见下） |
| 是否可回退 | ✅ 随时可回退：`Set-Service Redis -StartupType Automatic; Start-Service Redis` |
| 教训 | **引入一个"需要特定端口的新中间件"时，先确认端口真实占用者，不要只看 docker 映射** |

**若将来确实需要让别的项目继续用那个老 Redis**，可以两条路并存：

```
方案 A（推荐）：让那个项目改用别的端口，本机 6379 固定给 Redis Stack
方案 B：把 Redis Stack 映射到 6380，本项目的三个 URL 一起改成 6380
        （代价：与教程不一致，后续对照教程时都要记得偏移一位）
```

---

## 七、一条可迁移的教训

```
引入任何"独占端口"的中间件时，第一件事不是看 docker ps，
而是问：这个端口现在真正归谁？【谁在 bind 它？】

    看配置意图 → docker port / docker-compose.yml      （可能撒谎）
    看真实占用 → Get-NetTCPConnection -LocalPort N     （不会撒谎）
```

**为什么这条特别值得记**：
这类冲突**不会在安装时报错**，也不会让容器变成 unhealthy
（本例中容器一直是 `Up (healthy)`，因为容器内部自己的 6379 是好的）。
它只在**第一个真正用到该中间件高级特性的功能**上突然爆炸，
而那时你已经写了几百行代码，很容易误以为是代码写错了。

---

## 八、归档时的交接说明

- **本文档已修复**，无需后续动作；保留它是为了：
  1. 若将来该服务被谁改回 `Automatic`，症状会重现 —— 先来看本文档
  2. 记住"`docker port` 不等于真实占用者"这个诊断习惯
- **关联归档**：
  - `Day12/01_需求分析与方案设计/01_需求分析与方案设计.md` §5.2 → 提到"必须用 redis/redis-stack 镜像"，
    本文档补充了"即使镜像对了，也可能被本机旧 Redis 截胡"
  - `BUG发现与处理/02_2026.9.26/01_密码超72字节直接500.md`、`02_直传链路漏掉鉴权与权限标签.md` → 同批次归档
