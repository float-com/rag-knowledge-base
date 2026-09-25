# 06_评测Service

> 期：**Day10 · 评测与 Bad Case 分析**
> 章：第 3 章 后端实现 · **第 6 步「评测 Service」**（含 6.1 CRUD / 6.2 执行器主流程 / 6.3 单条 case 与最终聚合）
> 记录日期：2026.09.25

---

## 一、本节在整条链路里的位置

> **前五步都在"造零件"：账本、考卷、判分标准、专业评委、评测入口。
> 这一节是"总装线"—— 第一次把前面所有零件串成一条能跑完的流水线。**

```
第 1 步  新建评测表      ← 账本（已归档）
第 2 步  构建评测集      ← 考卷 + 读卡机（已归档）
第 3 步  人工指标与归因   ← 判分标准（已归档）
第 4 步  RAGAS 自动指标  ← 请专业评委（已归档）
第 5 步  评测入口        ← 让评测与线上走同一条链路（已归档）
第 6 步  评测 Service    ← 你在这里：执行器，把 1~5 串成一条流水线
第 7 步  评测 API        ← 暴露成 8 个接口
```

**本节的三个部分，对应三种完全不同的代码形态**：

| 小节 | 形态 | 谁在调 | 特征 |
| --- | --- | --- | --- |
| **6.1 `EvaluationService`** | **薄业务层**（同步、请求级） | 第 7 步的 HTTP 路由 | 8 个方法里 6 个是"一行转发"，**唯一不碰数据库的是 `list_datasets`** |
| **6.2 `execute_evaluation_run`** | **后台异步执行器** | FastAPI `BackgroundTasks` | 两阶段 + 3 个短会话 + 外层 try/except 兜底 |
| **6.3 `_run_single_case` / `_finalize_run`** | **6.2 内部的两个阶段** | 6.2 | 逐条落库 + 批量算分归因聚合 |

> ⭐ **本节的真正难度不在算法，在"边界"**：
> - 会话什么时候借连接、什么时候归还（**6.3 的会话流转**）
> - 一条 case 失败怎么不毁整轮（**6.2 的 Anti-Zombie Guard**）
> - 过滤后的样本怎么按原下标插回去（**6.3 的 Index-Preserving Filter**）
> - 8 个聚合分数各自除以几（**三种分母口径**）

---

## 二、本节与后续章节的**依赖关系**

```
        ┌────────────────────── 第 6 步（本节）──────────────────────┐
        │                                                            │
  第 7 步 API                                                       │
  POST /runs ──────► 6.1 EvaluationService.create_run               │
  GET  /runs ──────► 6.1 list_runs                                  │
  GET  /runs/{id} ─► 6.1 get_run                                    │
  DEL  /runs/{id} ─► 6.1 delete_run                                 │
  GET  /items ─────► 6.1 list_items                                 │
  GET  /items/{id}─► 6.1 get_item                                   │
  PATCH/items/{id}─► 6.1 update_item_bad_case                       │
  GET  /datasets ──► 6.1 list_datasets                              │
        │                                                            │
        │  create_run 之后，路由层再挂一个 BackgroundTasks：          │
        │      background_tasks.add_task(execute_evaluation_run, id)  │
        └────────────────────────┬───────────────────────────────────┘
                                 ▼
              6.2 execute_evaluation_run（后台异步跑起来）
                                 │
              ┌──────────────────┴──────────────────┐
              ▼                                     ▼
      6.3 _run_single_case                  6.3 _finalize_run
      （逐条调第 5 步的评测入口）            （批量调第 4 步 RAGAS + 第 3 步归因）
```

| 后续章节 | 依赖本节的什么 |
| --- | --- |
| 第 7 节 | **6.1 的 8 个方法一一对应 7 个 HTTP 端点**；`create_run` 之后挂 `BackgroundTasks.add_task(execute_evaluation_run, run_id)` 把 6.2 踢起来 |
| 第 7 节 | 前端进度条读的是 `run.progress_completed / progress_total`（**不是** `count(items)`）；8 个聚合字段原样返回 |

### ⭐ 本节是全期的"数据汇流点"

```
EvaluationCase（第 2 步）
   └─► _run_single_case ──► ChatService.answer_for_evaluation（第 5 步）
                              │
                              ├─► compute_citation_hit（第 3 步）
                              ├─► compute_refusal_correct（第 3 步）
                              └─► EvaluationAnswer ──► EvaluationItem 落库
                                                            │
   _finalize_run ◄──────────────────────────────────────────┘
        ├─► evaluate_batch（第 4 步 RAGAS）
        ├─► classify_bad_case（第 3 步 四级漏斗）
        └─► 8 个聚合字段 ──► EvaluationRun 落库
```

> 🎯 **一句话**：**前面五节各自是"可独立测试的零件"，本节是唯一一次把它们全部接上电。**

---

## 三、6.1 薄业务层：`EvaluationService`（L33-336）

### 3.1 类的定位

```
本服务只响应前端控制台的高频短请求（查看列表、创建元数据、人工修正分类）；
耗时较长、并发量大的"大模型实际跑测与打分流"由 FastAPI BackgroundTasks
在【同进程内】异步接管（见 6.2），严禁在此阻塞 HTTP 请求。
```

> ⚠️ 注意是 **`BackgroundTasks`（同进程）**，不是独立的 Worker 进程或消息队列。

### 3.2 八个方法 × 四条链路

| 前端动作 | Service 方法 | 仓储 | 落点 | 异常 |
| --- | --- | --- | --- | --- |
| 新建评测 | `create_run`（**L55**） | `run_repo.add` | `evaluation_runs` | **缺失 → 404 / 为空 → 400** |
| 评测列表 | `list_runs`（**L135**） | `run_repo.list_page` | `evaluation_runs` | 不会 404 |
| 批次详情 | `get_run`（**L117**） | `run_repo.get` | `evaluation_runs` | 查不到 → 404 |
| 删除批次 | `delete_run`（**L153**） | `run_repo.delete` | `evaluation_runs`（级联 items） | 删不到 → 404 |
| 用例列表 | `list_items`（**L183**） | 先 `get_run` 卫哨 + `item_repo.list_page` | `evaluation_items` | 404 |
| 用例详情 | `get_item`（**L229**） | `item_repo.get` | `evaluation_items` | 查不到 → 404 |
| 人工改归因 | `update_item_bad_case`（**L255**） | 复用 `get_item` + `session.commit` | `evaluation_items` | 404 |
| 评测集下拉 | `list_datasets`（**L330**） | **无仓储** | `datasets/*.jsonl`（纯文件） | — |

> ⭐ **`list_datasets` 是整个类里唯一不碰数据库的方法** —— 它只扫 `app/evaluation/datasets/` 目录。
> 这一点在配图里用一条独立支线单独标出。

### 3.3 `create_run`：先 load_dataset 再落库（防空跑）

```python
cases = load_dataset(dataset_name)          # ① 缺失 → NotFoundError(404)
if not cases:
    raise ValidationError(...)              # ② 内容为空 → ValidationError(400)
run = EvaluationRun(..., dataset_size=len(cases),
                    status=EvaluationRunStatus.RUNNING,
                    progress_total=len(cases))
```

**两种异常必须分清**（实测确认）：

| 情况 | 谁抛 | 状态码 |
| --- | --- | --- |
| 评测集**文件缺失** | `load_dataset` 内部（`dataset.py` L130-131） | **404** |
| 评测集**内容为空** | 本方法的 `if not cases` | **400** |

**`refresh` 回填的是什么**（L98-110 的注释）：

| 字段 | 来源 |
| --- | --- |
| `run.id` | **Python 端 `default=uuid4`**（flush 时生成，**不是自增、不是 server_default**） |
| `run.created_at` | **数据库 `server_default=func.now()`** |

两者在刚 `new` 出来的对象上都是 `None`，`refresh` 才能拿到真实值。

### 3.4 `update_item_bad_case`：全类唯一带业务分支的方法

**三个参数都是"传 `None` = 本次不改"，但清空 `category` 只能靠 `is_bad_case=False`。**

| 入参组合 | 出口状态 | 分支名 |
| --- | --- | --- |
| `is_bad_case is False` | `is_bad_case=False` + `bad_case_category=None` | **平反**（必须联动清空，防脏数据） |
| `bad_case_category` 非空 | 写入 category + `is_bad_case=True` | **隐式升级**（防前端漏传布尔值） |
| `is_bad_case is True` 且无 category | `is_bad_case=True`，category 不动 | **只标不合格** |
| 三者皆 `None` | 两个字段都不动 | **本次不改状态** |
| `bad_case_note` 非 `None` | 覆盖备注 | PATCH 语义（传 None 保留原值） |

> 🔴 **必须写 `is False`，不能写 `not is_bad_case`** —— 后者会把"传 `None`（本次不改）"也当成"要平反"。

---

## 四、6.2 执行器主流程：`execute_evaluation_run`（L345-451）

### 4.1 五个步骤

| 步骤 | 位置 | 动作 |
| --- | --- | --- |
| **1** | **L359** | 短会话：取 run → 判存在 → 写 `started_at` → `commit` → 取出 `dataset_name` |
| — | L377 | `cases = load_dataset(dataset_name)`（**此时会话已关，不占连接**） |
| **2** | **L397** | **阶段 1**：`for case in cases: await _run_single_case(run_id, case)` |
| **3** | **L409** | **阶段 2**：`await _finalize_run(run_id)` |
| **4** | **L418** | 短会话：`status=COMPLETED` + `finished_at` |
| **5** | **L432** | `except`：短会话：`status=FAILED` + `finished_at` + `error_message` |

### ⭐ 4.2 三个短会话，RAG 全程不占连接

```
步骤 1  ┐
        ├─ async with 短会话 → 用完即关
步骤 4  ┘
步骤 5  ┘
中间【步骤 2】约 1 分钟 +【步骤 3】约 1 分钟 —— 全程零数据库连接占用
```

**若把整个函数包在一个 `async with` 里**，评测跑两分钟就**占着一条连接两分钟**，并发几个 run 就能把连接池打满。

### ⭐ 4.3 特例：`run 不存在` 直接 `return`，**不标 FAILED**

```python
if run is None:
    logger.warning("evaluation run not found, skip: %s", run_id)
    return                       # ← 不是 raise
```

**它不是"失败"，是"任务已被删掉"** —— 标 FAILED 反而会在一条已删记录上留脏数据。所以这条分支是**中性**的，不走红色异常出口。

### 4.4 Anti-Zombie Guard：绝不留 RUNNING 僵尸任务

```python
except Exception as exc:
    logger.exception("evaluation run failed: run_id=%s", run_id)
    async with AsyncSessionLocal() as session:
        ...
        run.status = EvaluationRunStatus.FAILED
        run.finished_at = datetime.now(timezone.utc)
        run.error_message = (str(exc).strip() or exc.__class__.__name__)[:500]
```

- **`try` 覆盖步骤 1~4 全段**：任意一步抛异常都会落到这里；
- **`str(exc).strip() or exc.__class__.__name__`**：异常消息为空时回退成类名，保证非空；
- **`[:500]` 是主动截断**：该列在 `models.py` 里是 **`Text`**（PostgreSQL 无长度上限），截断是为了别把超长堆栈塞进接口响应。

---

## 五、6.3 单条 case 与最终聚合（L456-797）

### 5.1 `_run_single_case`：四个步骤（L456）

#### 🔴 会话流转（本节最容易误读的一处）

```python
async with AsyncSessionLocal() as session:          # ① 只创建会话对象
    chat_service = ChatService(session)
    answer = await chat_service.answer_for_evaluation(case.question)
# ② 退出块：session.close()  ← 没有连接 / 事务需要释放

...
await EvaluationItemRepository(session).add(item)   # ③ 首次发 SQL → autobegin
run = await EvaluationRunRepository(session).get(run_id)
await session.commit()                               # ④ 提交并归还连接
```

**实测证据**（探针输出）：

```
[进入 async with 后]   in_transaction=False  pool.checkedout=0   ← 没开事务、没借连接
[构造 ChatService 后]  in_transaction=False  pool.checkedout=0
[退出 async with 后]   in_transaction=False  pool.checkedout=0   ← 没什么可"释放"

对照·发了 SQL 后       in_transaction=True   pool.checkedout=1   ← 一执行 SQL 才借连接 + 开事务
对照·commit 后         in_transaction=False  pool.checkedout=0
```

**原因**：`answer_for_evaluation` 全程**不碰 `self.session`**（第 5 步已验证），所以那个块里**一条 SQL 都没发**。连接是**懒加载**的。

> ⚠️ **"关闭后的 session 还能继续用"不是 bug** —— SQLAlchemy 官方明确：`Session.close()` 之后**会话可复用**，下一次操作会自动 **autobegin** 一个全新事务。
> 而且 `run` 是在 `close()` **之后**重新 `get()` 出来的，属于新事务的 Identity Map，所以 `progress_completed += 1` 能被 `commit` 落库（实测 `progress_completed = 1` ✓）。

#### 四个步骤

| 步骤 | 位置 | 内容 |
| --- | --- | --- |
| **1** | **L473** | 短会话沙箱驱动 RAG（见上） |
| **2** | **L486** | 本地规则计算：`citation_hit`（三态）+ `refusal_correct` |
| **3** | **L521** | 装配 `EvaluationItem`（**21 个字段**） |
| **4** | **L559** | 落库 + 进度累加（复用 session，autobegin 新事务） |

#### `citation_hit` 的三态

```python
citation_hit = None if case.should_refuse else compute_citation_hit(...)
```

| `should_refuse` | 结果 | 含义 |
| --- | --- | --- |
| `True` | **`None`** | 该题本就不该出引用 → **不进命中率分母** |
| `False` | `True` / `False` | 正常比对 |

#### 21 个字段的分组

| 组 | 字段 | 数量 |
| --- | --- | --- |
| 关联与标识 | `run_id` / `case_id` | 2 |
| 输入快照 | `question` / `expected_answer` / `expected_document_names` / `expected_keywords` / `should_refuse` / `tags` | 6 |
| 实际输出 | `actual_answer` / `actual_refused` / `citations` | 3 |
| 白盒轨迹 | `retrieved_chunks_meta` / `query_route` / `agent_steps` / `verify_result` | 4 |
| 性能与追踪 | `latency_ms` / `first_token_latency_ms` / `trace_id` | 3 |
| 本地指标 | `citation_hit` / `refusal_correct` | 2 |
| 异常记录 | `error_message` | 1 |
| | | **21** |

#### 进度计数器（口径已澄清）

```python
run.progress_completed += 1          # 无条件 +1（= 已跑完总数，含失败）
if answer.error_message:
    run.progress_failed += 1         # 额外 +1（它是 completed 的【子集】）
```

> **口径**：`progress_completed` 含失败条 → 进度 = `completed / total`；
> `progress_failed` 是它的**子集** → 异常率 = `failed / completed`。
> **前端实测读的正是 `progress_completed / progress_total`**（`EvaluationListPage.tsx` L62 / `EvaluationDetailPage.tsx` L137）。

### 5.2 `_finalize_run`：五个步骤（L579）

| 步骤 | 位置 | 内容 |
| --- | --- | --- |
| 前置 | L598 | `items = await item_repo.list_by_run(run_id)`；空则 `return`（防除零） |
| **1** | **L600** | **过滤 + 带原下标打包** → `samples_with_index` |
| **2** | **L631** | **批量 RAGAS** + 按下标回填 |
| **3** | **L643** | 逐条回填分数 + **无条件归因** |
| **4** | **L673** | run 级聚合（**8 个字段**） |
| **5** | **L713** | `await session.commit()` |

#### ⭐ Index-Preserving Filter（本节最核心的算法结构）

```
items（N 条）
  │  enumerate → (idx, item)；跳过 should_refuse / error_message / 无切片
  ▼
samples_with_index（M 条，M ≤ N）   ← 每条都带着它在 items 里的【原下标 idx】
  │  if samples_with_index: evaluate_batch([s for _, s in ...])
  ▼
indexed_metrics（M 条）
  │  zip(samples_with_index, indexed_metrics, strict=True) → metrics_list[idx] = m
  ▼
metrics_list（N 条，与 items 等长）   ← 按 idx 插回原位
  │  zip(items, metrics_list, strict=True)
  ▼
逐条回填 + 归因
```

**为什么必须带 idx**：过滤后长度不一致，直接 `for item, m in zip(items, metrics)` 会**从第一个被过滤的位置起全体错位**。

**`strict=True` 的作用**：长度对不上时**直接抛错**，而不是静默截断丢数据 —— 这是整套对齐设计的**最后一道保险**。

**过滤的三个判据，各有各的理由**：

| 判据 | 理由 |
| --- | --- |
| `should_refuse=True` | 该题的标准答案就是"不回答"，**RAGAS 四指标前提是"该有答案"**，对它不适用；而且第 3 节漏斗在 **Level 1 就因 `refusal_correct` 短路**，RAGAS 分数根本用不上。⚠️ **不是**因为"拒答题没有切片"—— 实测拒答题照样有 chunks（越界题 20 条） |
| `error_message` 非空 | RAG 链路已降级，`actual_answer` 是**空串** |
| `retrieved_chunks_meta` 为空 | **这一条才是**真的没有参考切片 |

#### ⭐ 步骤 3 的顺序（**归因是无条件执行**）

```python
for item, metrics in zip(items, metrics_list, strict=True):
    if metrics is not None:              # ← 条件执行
        item.faithfulness = ...（4 个字段）

    rule = classify_bad_case(...)        # ← 无条件执行（注意它不在 if 里面）
    item.is_bad_case = rule.is_bad_case
    item.bad_case_category = rule.category
```

**降级/拒答用例没有 RAGAS 分数，但照样会被归因** —— 这是设计意图，不是遗漏。

### 5.3 ⭐ run 级 8 个聚合字段与【三种分母口径】

| 口径 | 分母 | 字段 | 表达式 |
| --- | --- | --- | --- |
| **A · 全部 items** | `len(items)` | `refusal_accuracy` | `sum(1 for i in items if i.refusal_correct) / len(items)` |
| | | `avg_latency_ms` | `sum(i.latency_ms for i in items) / len(items)` |
| **B · 该字段非 None 的条数** | `_avg()` 内部滤 None | `faithfulness` / `answer_relevancy` / `context_precision` / `context_recall` | `_avg([i.该字段 for i in items])` |
| | | `avg_first_token_latency_ms` | `_avg(非 None 的 TTFT)` |
| **C · `citation_hit` 非 None 的条数** | 排除拒答题 | `citation_hit_rate` | `sum(1 for h in 非 None 的 citation_hit if h) / len(非 None 的 citation_hit)` |

**为什么会有三种分母 → 根源在"列是否可空 + NULL 的语义"**：

| 口径 | 对应的列 | 依据 |
| --- | --- | --- |
| **A** | `refusal_correct`（`NOT NULL, default=False`）<br/>`latency_ms`（`NOT NULL, default=0`） | 这两列**每条必有值** |
| **B** | 4 项 RAGAS（`nullable=True`）<br/>`first_token_latency_ms`（`nullable=True`） | 这几列**可能算不出**（打分失败 / 没调 LLM）→ NULL = "算不出" |
| **C** | `citation_hit`（`nullable=True`） | 它的 NULL **不是"算不出"而是"不适用"**（拒答题本就不该有引用）→ 必须排除 |

### 5.4 三个辅助函数

| 函数 | 位置 | 职责 | 用在 |
| --- | --- | --- | --- |
| **`_chunk_meta(chunk)`** | **L719** | 把 `RetrievedChunk` 序列化成 9 键的轻量 dict（**保留 `content` 供 RAGAS 用**；UUID 显式 `str()` 以便 JSONB 序列化） | `_run_single_case` 步骤 3 |
| **`_verify_payload(result)`** | **L753** | `VerifyResult` → 可入库 dict；**空串 `reason` 归一化为 `None`**（空串在 JSON 里没有信息量） | `_run_single_case` 步骤 3 |
| **`_avg(values)`** | **L776** | 含 `None` 列表的有效均值：**滤掉 None 求平均**；**全 None 返回 `None`（不是 0、也不会除零）** | `_finalize_run` 步骤 4 |

> ⭐ **`_avg` 就是"口径 B"的化身**，它同时决定两件事：
> ① 分母 = 非 None 的条数；② **空集返回 `None` → 落库为 SQL NULL**。
> 第二批若全是拒答题 → `run.faithfulness = NULL`，前端看到 NULL 才知道"**这批没测**"，看到 0 会误以为"**这批全错**"。

---

## 六、运行验证（真实执行，非纸面推导）

### 6.1 端到端跑批（`smoke` 5 条，含 1 条拒答）

```
execute_evaluation_run 耗时 90.0s
run.status = completed | progress 5/5 failed 0 | error None

聚合指标（8 个字段全部写出）
  faithfulness               = 1.0
  answer_relevancy           = 0.9413
  context_precision          = 0.8958
  context_recall             = 1.0
  citation_hit_rate          = 1.0
  refusal_accuracy           = 0.8          ← 5 条里 1 条拒答判错
  avg_latency_ms             = 5745.6
  avg_first_token_latency_ms = 4039.2       ← 分母只算有 TTFT 的 4 条
```

**逐条明细**：

| case | should_refuse | actual_refused | citation_hit | refusal_correct | chunks | is_bad_case | category |
| --- | --- | --- | --- | --- | --- | --- | --- |
| smoke_001 | False | False | True | True | 5 | False | — |
| smoke_002 | False | False | True | True | 5 | False | — |
| smoke_003 | False | False | True | True | 5 | False | — |
| smoke_004 | False | False | True | True | 5 | False | — |
| **smoke_005** | **True** | **False** | **None** | **False** | 5 | **True** | **`context_judge_too_loose`** |

### ⭐ 6.2 全链路第一次真的抓到一个 Bad Case

`smoke_005` 是越界题（珠峰海拔）—— **该拒答，但系统没拒答**：

```
should_refuse=True  →  actual_refused=False
   →  compute_refusal_correct 判定 False
   →  classify_bad_case 在 Level 1 命中
   →  category = context_judge_too_loose（闸门过松）
   →  refusal_accuracy = 4/5 = 0.8
```

**这条 case 与第 5 步归档「遗留 §8.2」记的现象完全一致**：

> 模型自己回答"知识库中没有找到相关信息"，但 `refused` 标志仍是 `False`，
> 于是 5 条 `citations` 被照常下发。

**Day10 的整套评测体系，就是为了把这种"看起来正常、实际有问题"的 case 揪出来 —— 这一轮它真的揪出来了。**

### 6.3 检查项汇总（21 / 21）

| 检查组 | 结果 |
| --- | --- |
| `list_datasets` / `create_run` 两种异常 / `list_items` 卫哨 | ✅ 5/5 |
| 端到端：`status=completed` / `progress 5/5` / 5 条 item / 无整轮错误 | ✅ 4/4 |
| 每条都有 `trace_id` / `retrieved_chunks_meta` | ✅ 2/2 |
| `citation_hit` 三态（拒答 `None`、正向 `bool`） | ✅ 2/2 |
| 每条 `is_bad_case` 已归因 | ✅ 1/1 |
| **8 个聚合字段全部写出** | ✅ 1/1 |
| 至少一条拿到 RAGAS 分数 | ✅ 1/1 |
| **失败路径**：`status=failed` + `error_message='评测集不存在: __no_such__'` | ✅ 2/2 |
| `PATCH` 隐式升级 / 平反清空归因 | ✅ 2/2 |
| **级联删除**残留 0 | ✅ 1/1 |
| **合计** | ✅ **21 / 21** |

**数据清理**：运行前后 `evaluation_runs` / `evaluation_items` 均为 **0**，无测试残留。

---

## 七、配图（6 张）

| 图 | 文件 | 讲清什么 |
| --- | --- | --- |
| **图一** | `01_6.1-EvaluationService 方法全景与仓储映射图.png` | **8 个方法 × 前端动作 × 仓储 × 落点 × 错误码** 四列映射；⭐ `list_datasets` 单独一条不碰库的支线 |
| **图二** | `02_6.1-update_item_bad_case 归因状态机图.png` | **四步状态机**：平反 / 隐式升级 / 只标不合格 / 本次不改；含两条防脏数据注解 |
| **图三** | `03_6.2-execute_evaluation_run 两阶段主流程图.png` | **五步 + 两阶段 + 三短会话**；`run 不存在` 的中性出口与红色异常出口分开 |
| **图四** | `04_6.3-_run_single_case 单条 case 主流程图.png` | ⭐ **两种底色区分"无事务区 / 真事务区"**；`citation_hit` 三态分支 |
| **图五** | `05_6.3-_finalize_run 批量算分与归因流程图.png` | ⭐ **带下标过滤 → 批量打分 → 按下标回填**；`strict=True` 的保险作用 |
| **图六** | `06_6.3-run级聚合指标口径对照图.png` | ⭐ **8 个字段 × 三种分母口径**；A/B/C 三色对照 |

**最该先看的两张**：**图三**（总装线的骨架）→ **图四**（会话流转，全节最容易误读处）。

**排版建议**：

```
图三                    ← 先给主流程（它是 6.2/6.3 的父图）
├── 图四（6.2 步骤 2 的放大）
├── 图五（6.2 步骤 3 的放大）
│     └── 图六（图五 步骤 4 的再放大）
图一 + 图二              ← 6.1 的两张，独立成组
```

### 本节画图沉淀出的规则（补充前几章）

| # | 规则 | 出处 |
| --- | --- | --- |
| 1 | **有分支/菱形时，循环必须标出遍历范围**，否则会被读成"只执行一次" | 图五 |
| 2 | **"无条件执行"的节点不能被画成"并行二选一"** —— 分支汇合点必须落在正确的一侧 | 图五（步骤 3） |
| 3 | **颜色对比不能靠虚线边框**（drawio 导入常丢 `stroke-dasharray`），要靠**底色深浅** | 图四 |
| 4 | **label 里避开 `*` `=` `[:n]` 等字符** —— 极易被解析器截断（本节踩过 2 次） | 图五 |
| 5 | **旁注框用"单向虚线挂一侧"**，不要串进主链路，否则图会白白拉长 | 图四 |

---

## 八、遗留与待确认

### 🔴 8.1 ⚠️ `citation_hit` 的**口径取舍**（已在注释里标注，**代码未改**）

```python
citation_hit = None if case.should_refuse else compute_citation_hit(...)
```

只排除了"本就该拒答"的用例，**没有**排除 `answer.error_message` 非空的**降级用例**。后果链：

| 环 | 内容 |
| --- | --- |
| ① | 降级用例的 `answer.citations` 是**空列表**（`EvaluationAnswer` 的默认值） |
| ② | `compute_citation_hit` 对空引用直接 `return False`（`scoring.py` 的前置边界防御） |
| ③ | 于是它拿到 `False` 而**不是 `None`** → `_finalize_run` 的 `if i.citation_hit is not None` 拦不住 → **进了分母、被算作"未命中"** |

**影响**（实测演示）：

```
一批 5 条、1 条因网络中断降级：
  现状：citation_hit_rate = 4 / 5 = 0.8   ❌ 把基础设施故障算成了"检索漏召回"
  期望：citation_hit_rate = 4 / 4 = 1.0
```

**而且同一行数据里两个字段口径不一致**：`bad_case_category` 会被正确归为 **`other`（系统故障）**，
`citation_hit` 却说"检索没命中"—— **报表读者会被带偏优化方向**。

**修法（一个条件）**：
```python
None if case.should_refuse or answer.error_message else ...
```

> **当前状态**：教程原文如此，本项目**暂不改行为**，已在 `evaluation_service.py` **L492-504** 用 14 行注释写全了后果链与修法。
> **待第 7 步 API 收口时决定**。

### 🔴 8.2 `_run_single_case` 在 `async with` 块**外**使用已关闭的 `session`

```python
async with AsyncSessionLocal() as session:      # ← 块在这里结束
    answer = await ChatService(session).answer_for_evaluation(...)

await EvaluationItemRepository(session).add(item)   # ← 却还在用 session
```

**实测能跑通**（SQLAlchemy 的会话 `close()` 后可复用，会 autobegin 新事务），但：

- docstring 写的"**独立 session + 独立事务**"与事实不符 —— 实际是**两个事务**；
- 依赖"关闭后可复用"这个**隐式特性**，读代码的人（以及别的 AI）会当成 bug 去追。

**建议改成两段显式 `async with`**（语义完全等价，零行为变化，但把"RAG 期间不占连接"这个意图写在脸上）：

```python
async with AsyncSessionLocal() as session:      # ① RAG 调用
    answer = await ChatService(session).answer_for_evaluation(case.question)

async with AsyncSessionLocal() as session:      # ② 落库
    ...
    await session.commit()
```

### ⚠️ 8.3 `_finalize_run` 是**长事务**（跨过整个 RAGAS 批量）

`list_by_run()` 一执行就开事务、占连接，而后面 `evaluate_batch` 要跑 **1~2 分钟**，直到最后 `commit` 才释放。

**建议**：先查出 items、**立刻关掉 session**，跑完 RAGAS 再开一个新会话回填。

### ⚠️ 8.4 评测结果**天然非确定**

同一道越界题在不同轮次可能给出不同结论（实测出现过 `refused` 为 `False / True / False` 三种）。
根因：`plan_retrieval`（Agentic 规划）与 `judge_context`（上下文裁定）**都是 LLM 决策**。

**这正是第 1 步要建 `evaluation_runs` 表的理由** —— 每轮结果必须完整快照（`run_id` + `trace_id`），才有"同一 case 跨批次对比"的依据。

### 🔸 8.5 `_chunk_meta` 丢掉了三个 rank 字段

`RetrievedChunk` 上的 `sources` / `vector_rank` / `keyword_rank` **没有落库**。
排查"**是不是 RRF 融合把某条好切片挤掉了**"时，**rank 比 score 更直接**。已在注释里标注，将来需要时再补。

### 🔸 8.6 `planner.refuse` 设计缺口（跨期遗留）

第 8 期就记过的三项选择（A/B/C）至今未落地。本节实测到的 **`context_judge_too_loose`**（`smoke_005`）与它同源。

---

## 九、关联文件索引

### 9.1 本节新增（1 个文件）

| 文件 | 位置 | 说明 |
| --- | --- | --- |
| **`backend/app/services/evaluation_service.py`** | **全文 797 行** | ⭐ 6.1 + 6.2 + 6.3 全部在这一支文件里 |

### 9.2 `evaluation_service.py` 精确锚点

| 位置 | 内容 |
| --- | --- |
| L1-26 | imports + `logger` |
| **L28-31** | **6.1 分隔注释** |
| **L33** | `class EvaluationService` |
| L46 | `__init__`（持有 session + 两个仓储） |
| **L55** | `create_run`（L69 步骤 1 防空跑 / L82 构造实体 / L98 落库与 refill） |
| **L117** | `get_run`（404 收敛） |
| **L135** | `list_runs`（返回 `(list, total)` 二元组） |
| **L153** | `delete_run`（ORM 级删除 + `passive_deletes` 级联） |
| **L183** | `list_items`（先 `get_run` 卫哨 → 语义隔离） |
| **L229** | `get_item` |
| **L255** | `update_item_bad_case`（L285 步骤 1 / L292 状态机 / L311 备注 / L318 提交刷新） |
| L330 | `list_datasets`（**唯一不碰数据库的方法**） |
| **L339-343** | **6.2 分隔注释** |
| **L345** | `execute_evaluation_run` |
| L359 | 步骤 1 · 短会话初始化 + `started_at` |
| L377 | `cases = load_dataset(dataset_name)` |
| L397 | 步骤 2 · 阶段 1（逐条跑 RAG + 进度） |
| L409 | 步骤 3 · 阶段 2（`_finalize_run`） |
| L418 | 步骤 4 · 正常收尾（`COMPLETED`） |
| L432 | 步骤 5 · 全局异常兜底（`FAILED` + 错误摘要截断） |
| **L454** | **6.3 分隔注释** |
| **L456** | `_run_single_case` |
| L473 | 步骤 1 · 短会话沙箱驱动 RAG |
| **L492-504** | ⭐ **【已知口径取舍】注释块**（`citation_hit` 的边界，见 §8.1） |
| L486 | 步骤 2 · 本地规则计算 |
| L521 | 步骤 3 · 装配 `EvaluationItem`（21 字段） |
| L559 | 步骤 4 · 落库 + 进度累加 |
| **L579** | `_finalize_run` |
| L600 | 步骤 1 · 过滤 + 带原下标打包 |
| L631 | 步骤 2 · 批量 RAGAS + 按下标回填 |
| L643 | 步骤 3 · 逐条回填 + 无条件归因 |
| L673 | 步骤 4 · run 级聚合（8 个字段） |
| L713 | 步骤 5 · `commit` |
| **L719** | `_chunk_meta` |
| **L753** | `_verify_payload` |
| **L776** | `_avg`（**口径 B 的实现**） |

### 9.3 依赖与相关

| 位置 | 说明 |
| --- | --- |
| `app/db/repositories/evaluation_repo.py` | `EvaluationRunRepository`（`add`/`get`/`list_page`/`delete`）+ `EvaluationItemRepository`（`bulk_add`/`add`/`get`/`list_by_run`/`list_page`）；**只 `flush`，从不 `commit`** |
| `app/db/session.py` | L25-33 `AsyncSessionLocal`：**`expire_on_commit=False`**、`autoflush=False` |
| `app/db/models.py` | L941-1033 `EvaluationRun`（含 8 个聚合列 + `progress_*`）；L1084-1272 `EvaluationItem`（21 个写入字段）；**L975-990 进度计数器口径注释** |
| `app/evaluation/dataset.py` | `load_dataset`（L130-131 缺文件抛 404）/ `list_datasets` |
| `app/evaluation/scoring.py` | `compute_citation_hit`（空引用 → `False`）/ `compute_refusal_correct` / `classify_bad_case`（四级漏斗） |
| `app/evaluation/ragas_runner.py` | `RagasSample` / `RagasMetrics` / `evaluate_batch` |
| `app/services/chat_service.py` | `ChatService.answer_for_evaluation` / `EvaluationAnswer`（降级时 `citations=[]`、`first_token_latency_ms=None`） |
| `app/core/exceptions.py` | `NotFoundError`（404）/ `ValidationError`（400） |
| `frontend/src/client/types.gen.ts` | L706-714 / L802-810：`progress_total/completed/failed` 与 8 个聚合字段 |
| `frontend/src/pages/EvaluationListPage.tsx` | **L62**：`progress_completed / progress_total` 驱动进度条 |
| `frontend/src/pages/EvaluationDetailPage.tsx` | **L137**：进度文案 `进度 {completed}/{total}` |

### 9.4 与前五节的对照

| | 第 1~5 步 | **第 6 步（本节）** |
| --- | --- | --- |
| 形态 | 各自独立、可单测的"零件" | **总装线**（唯一一次全部串起来） |
| IO | 表 / 文件 / 纯计算 / 网络 / 网络+图 | **全部都有** |
| 事务 | 第 1 步建表，其余 0 | **6 个短事务 + 1 个长事务**（见 §8.3） |
| 失败出口 | 3 个异常 / 0 / 0 / 0 | **0（绝不向上抛）** |
| 核心难点 | 各自领域的边界 | ⭐ **"不毁整轮" + "不从占连接" + "不错位" + "不串分母"** |
