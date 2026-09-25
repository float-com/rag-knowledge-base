# 07_评测API

> 期：**Day10 · 评测与 Bad Case 分析**
> 章：第 3 章 后端实现 · **第 7 步「评测 API」**（含 7.1 响应契约 / 7.2 路由层 / 7.3 装配）
> 记录日期：2026.09.25

---

## 一、本节在整条链路里的位置

> **前六步全在"后端自己人"之间传数据 —— Python 对象递来递去。
> 这一节是第一次"开门营业"：把能力翻译成 HTTP，暴露给前端。**

```
第 1 步  新建评测表      ← 账本（已归档）
第 2 步  构建评测集      ← 考卷 + 读卡机（已归档）
第 3 步  人工指标与归因   ← 判分标准（已归档）
第 4 步  RAGAS 自动指标  ← 请专业评委（已归档）
第 5 步  评测入口        ← 让评测与线上走同一条链路（已归档）
第 6 步  评测 Service    ← 执行器，把 1~5 串成一条流水线（已归档）
第 7 步  评测 API        ← 你在这里：8 个接口 + 后台任务挂载
```

**本节的三个文件，是三种完全不同的角色**：

| 文件 | 角色 | 行数 | 关键点 |
| --- | --- | --- | --- |
| `app/api/schemas/evaluations.py` | **契约层**（数据长什么样） | 269 | 13 类归因 Literal、Run 读模型拆两份、PATCH 三可选字段 |
| `app/api/routes/evaluations.py` | **传输层**（路怎么走） | 309 | 5 条路径 / 8 个端点、`BackgroundTasks` 挂载顺序 |
| `app/main.py` | **装配层**（门牌挂哪） | 130 → 141 | 一行 import + 一行 `include_router` |

> ⭐ **本节的难度不在代码量，在"边界"**：
> - **契约边界**：`schemas` 与前端 `types.gen.ts` 是两份拷贝，必须逐字对齐（**本期唯一的手工同步点**）
> - **事务边界**：路由层绝不 `commit`，也绝不碰 SQLAlchemy 实体以外的东西
> - **执行边界**：HTTP 请求**立刻返回**，评测在**响应之后**才跑（`BackgroundTasks` 的语义）
> - **错误边界**：框架 422（参数不合法）与业务 404/400（资源不存在/内容为空）是两条完全不同的路

---

## 二、本节与前后章节的**依赖关系**

```
   第 6 步（已归档）                          第 7 步（本节）
 ┌──────────────────────┐              ┌──────────────────────────────┐
 │ 6.1 EvaluationService│◄─────────────┤ routes: 每个端点 new 一次     │
 │   8 个方法            │   调用        │   service = EvaluationService │
 ├──────────────────────┤              │   (session)                   │
 │ 6.2 execute_evaluation_run          └──────────────────────────────┘
 │   （模块级后台执行器） │◄─────────────  background_tasks.add_task(
 └──────────────────────┘   登记为        execute_evaluation_run, run.id)
                          "响应后任务"
                                              │
                                              ▼
                                  第 8 步：前端控制台
                                  src/api/evaluation.ts（已就位）
                                  ← 用 8 个 operationId 拉 TanStack Query
```

| 关系 | 具体是什么 |
| --- | --- |
| **依赖第 6 步** | 6.1 的 **8 个方法 ↔ 8 个端点一一对应**；`list_datasets` 是**同步方法**（不加 `await`） |
| **依赖第 6 步** | `create_run` 内部会 `commit`，**之后**才允许 `add_task` —— 顺序颠倒后台任务会静默跳过 |
| **被第 8 步依赖** | `frontend/src/api/evaluation.ts` 用这 8 个 `operationId` 生成 8 个 SDK 函数与 `useEvaluationXxx` 钩子 |
| **被前端轮询依赖** | 进度条读 `progress_completed / progress_total`；`status` 判终态。**本步不推送进度，靠前端定频轮询** |

> 🎯 **一句话**：**第 6 步决定"能做什么"，本节决定"外面怎么叫得动它"。**

---

## 三、7.1 契约层：`app/api/schemas/evaluations.py`（269 行）

### 3.1 为什么 Schema 与 ORM 实体必须分家

```
EvaluationRun（ORM 实体）           EvaluationRunRead / Item（DTO）
  ├─ 指数据库表、列宽、外键           ├─ 指 HTTP 响应体、JSON 字段
  ├─ 含 __table_args__ / relationship  ├─ 只有前端要看的字段
  └─ 改动会影响迁移                    └─ 改动只影响接口文档（应当 = 前端类型的副本）
```

| 风险 | 不分家会怎样 |
| --- | --- |
| **字段泄露** | ORM 实体直接当 `response_model`，将来给表加一列 `internal_flag`，**前端立刻多收到一个字段** |
| **契约漂移** | 数据库列注释改了、字段名改了，接口会**静默**跟着变，前端不报错但取值变 `undefined` |
| **循环 / 懒加载** | `relationship` 序列化时可能触发额外 SQL 甚至死循环 |

> 分家后有一条硬规则：**路由层返回的必须是 `model_validate(实体)` 的 DTO，不是实体本身。**

### 3.2 枚举契约的"单一事实源"问题

`BadCaseCategoryValue`（L49-63）与 `scoring.py` 的 `BadCaseCategory` **值 + 顺序逐字对齐**：

```
document_parse_failed  chunk_split_bad  embedding_recall_miss  keyword_recall_miss
rrf_fusion_error  rerank_order_error  context_judge_too_loose  context_judge_too_strict
prompt_constraint_weak  generation_off_context  citation_parse_failed
permission_filter_error  other
```

> ⚠️ **这 13 类在项目里此刻存在 4 份拷贝**：
> `scoring.py`（事实源）· 本文件 · `frontend/src/client/types.gen.ts` · 数据库列注释。
> **改动任何一处都必须同步另外三处** —— 因为 `types.gen.ts` 是**手工维护、提前于后端**写的。
>
> 为什么用 `Literal` 而不是 `Enum`：让 OpenAPI 直接输出**候选值字符串数组**，
> 前端生成 TS 时得到 `"running" | "completed" | "failed"` 这样的联合类型，无需映射表。

### 3.3 Run 的两个读模型为什么要分开

| 字段组 | `EvaluationRunRead`（详情） | `EvaluationRunListItem`（列表） |
| --- | --- | --- |
| 标识与元数据 | ✅ | ✅ |
| 进度三元组 | ✅ | ✅ |
| 8 个聚合分 | ✅ | ✅ |
| `error_message` | ✅ | ❌ |
| `started_at` / `finished_at` | ✅ | ❌ |
| **合计** | **21 个字段** | **17 个字段** |

> 列表页只展示名称、状态、进度与聚合分，**不需要"失败原因"和起止时间戳**；
> 分开定义可以让列表响应更小、也更贴合前端表格的列。
> 这就是"脱敏 + 裁字段"发生在**路由层**的原因（见 4.4）。

### 3.4 三态与 PATCH 语义

| 字段 | 三态含义 | 坑在哪 |
| --- | --- | --- |
| `citation_hit: bool \| None` | `True` 命中 / `False` 未命中 / **`None` 该题本就该拒答、不考引用** | 前端若写 `citation_hit === false` 才标红，**`None` 会被漏掉** —— 这正是第 5 步归档里 `refusal_accuracy` 那个口径盲区的镜像 |
| `EvaluationItemUpdate` 三个字段全可选 | **传 `None` = 本次不改这一项** | 这是 **PATCH 部分更新**语义，与 PUT 全量覆盖完全不同 |
| 同上 | **`bad_case_category=None` 不会清空数据库里的分类** | 清空归因**只有一条路**：显式传 `is_bad_case=False`（Service 联动抹平） |

**两处 Pydantic 细节值得单记**：

| 写法 | 为什么 |
| --- | --- |
| `Field(default_factory=list)` | 直接写 `= []` 会让**所有实例共享同一个列表对象**（可变默认值陷阱） |
| `model_config = ConfigDict(from_attributes=True)` | 允许 `Model.model_validate(orm_entity)` 按**属性名**取值，免写转换函数 |

---

## 四、7.2 路由层：`app/api/routes/evaluations.py`（309 行）

### 4.1 8 个端点总表

```
router = APIRouter(prefix="/evaluations", tags=["evaluations"])
对外真实路径 = [/api] + [/evaluations] + 子路径
```

| # | 方法 | 子路径 | `operationId` | 入参 | 出参 | 状态码 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | GET | `/datasets` | `listEvaluationDatasets` | — | `DatasetListResponse` | 200 |
| 2 | POST | `/runs` | `createEvaluationRun` | `EvaluationRunCreate` | `EvaluationRunRead` | **201** |
| 3 | GET | `/runs` | `listEvaluationRuns` | `page` `page_size` | `EvaluationRunPage` | 200 |
| 4 | GET | `/runs/{run_id}` | `getEvaluationRun` | 路径 `UUID` | `EvaluationRunRead` | 200 |
| 5 | DELETE | `/runs/{run_id}` | `deleteEvaluationRun` | 路径 `UUID` | —（空 `Response`） | **204** |
| 6 | GET | `/runs/{run_id}/items` | `listEvaluationItems` | `page` `page_size` `bad_case_only` `category` | `EvaluationItemPage` | 200 |
| 7 | GET | `/items/{item_id}` | `getEvaluationItem` | 路径 `UUID` | `EvaluationItemRead` | 200 |
| 8 | PATCH | `/items/{item_id}` | `updateEvaluationItem` | `EvaluationItemUpdate` | `EvaluationItemRead` | 200 |

> 5 条路径 / 8 个端点；`operationId` 是给**前端 SDK 生成器**用的唯一函数标识
> （实测 27 个 `operationId` 全局唯一，8 个新增与原有 19 个**零冲突**）。

### 4.2 `BackgroundTasks`：本文件最关键的**两行顺序**

```python
run = await service.create_run(name=payload.name, dataset_name=payload.dataset_name)
background_tasks.add_task(execute_evaluation_run, run.id)   # ← 必须在 commit 之后
return EvaluationRunRead.model_validate(run)
```

```
❌ 顺序颠倒会发生什么：
   add_task 先登记 → create_run 后提交 → 后台任务按 run_id 查库 → 查不到
   → 打印 warning 并【静默 skip】→ 任务永远不跑，前端进度条永远 0/5、状态永远 running

✅ 正确顺序：
   create_run（内部 load_dataset 校验 + 落库 + commit）→ 拿到 run.id → 再登记后台任务
```

**为什么能"立即返回 201"** —— `BackgroundTasks` 的语义是**响应体发送给客户端之后**才在**同进程内**执行：

```
客户端 ──POST /api/evaluations/runs──► 路由层
                                        ├─ create_run（落库，状态 running）
                                        ├─ add_task(execute_evaluation_run, id)
                                        └─ 返回 201 + run 详情（progress 0/5）
客户端 ◄──── 201 ────────────────────────┘
                                        ↓ 响应发完之后，才开始跑
                             execute_evaluation_run（第 6 步 6.2，约 60~90 秒）
客户端 ──GET /runs/{id} 定频轮询────────► 读 progress_completed / progress_total
```

> ⚠️ 是 **`BackgroundTasks`（同进程）**，不是 Worker 进程、不是消息队列。
> 因此：**进程重启 = 正在跑的评测丢失**（DB 里会留下一条永远 `running` 的记录）。

### 4.3 路由层**只做三件事**

| 做 | 不做 |
| --- | --- |
| ① 参数校验（交给装饰器与签名声明） | ❌ 不写业务规则（归因联动、级联删除都在 Service） |
| ② 调用 Service（`service = EvaluationService(session)`） | ❌ 不 `commit`（事务边界由 Service 掌握） |
| ③ 把 ORM 实体转成 DTO（`model_validate`） | ❌ 不返回 ORM 实体 |

**一个易错点**：`list_datasets` 是**同步方法**（只扫目录数行数，不碰数据库）：

```python
items = service.list_datasets()          # ✅ 不要习惯性加 await
```

### 4.4 三条路径设计细节

| 细节 | 说明 |
| --- | --- |
| **`/runs/{run_id}/items` 与 `/items/{item_id}` 不撞车** | 两者**第一段就不同**（`runs` vs `items`），**不依赖路由注册顺序** |
| **`DELETE` 为什么返回 `Response(status_code=204)`** | 204 按 HTTP 规范**不允许携带响应体**，必须显式返回空的 `Response`；装饰器上的 `status_code=204` 是给 OpenAPI 文档用的声明 |
| **`bad_case_only` 与 `category` 相互独立** | 仓储层用 `filters` 列表动态拼装（AND）：`bad_case_only=True` 追加 `is_bad_case IS TRUE`；`category` 非空追加 `bad_case_category = :category`。**`category` 可以单独用**（不限 bad case） |

---

## 五、7.3 装配：`app/main.py`（130 → 141 行，只动两处）

```python
# L35：把评测业务接待员也请过来（在原有 chat, document_uploads, documents, health 后面加一个名字）
from app.api.routes import chat, document_uploads, documents, evaluations, health

# L122-130：新增注释块 + 一行挂载
app.include_router(evaluations.router, prefix="/api")
```

```
路径拼接公式：全局前缀 [/api] + 模块前缀 [/evaluations] + 接口子路径
                                         → /api/evaluations/datasets、/api/evaluations/runs ...
```

| 收益 | 说明 |
| --- | --- |
| 统一网关前缀 | 便于 Nginx 把所有 `/api/*` 反向代理给后端 |
| 自动进 Swagger | `/docs` 会把这 8 个端点**单独折叠成 `evaluations` 分组**（实测独立 tag，不与 documents/chat 混） |

> ⚠️ 注册顺序在**本项目里对评测模块无所谓**，但 `document_uploads.router` 必须排在
> `documents.router` **之前**（否则 `/documents/uploads/...` 会被 `/documents/{document_id}` 抢先匹配、
> 把 `uploads` 当 UUID 解析）—— 这条老规矩见 L108-111，**与本步无关但同文件同风险**。

---

## 六、实测验证（37/37 项全通过）

测试方式：`httpx.ASGITransport` 直连 ASGI app（不启 uvicorn），真连数据库 `rag_kb`。

| 验证项 | 结果 |
| --- | --- |
| **端点可达性** | 8/8 端点全部返回预期结构 |
| **OpenAPI 契约** | 5 条路径 / 8 个端点；**27 个 operationId 全局唯一**（19 旧 + 8 新，零冲突） |
| **枚举契约** | `bad_case_category` 在文档中展开为 **13 个候选值**，顺序与 `scoring.py` 一致 |
| **tag 分组** | `evaluations` 独立成组 |
| **POST /runs 真实耗时** | **67.7 秒**，返回 **201**（含后台任务，因 ASGI 调用会等任务跑完） |
| **创建后状态** | `status=completed`、`progress 5/5`、`failed 0`、8 个聚合分全部回填 |
| **422（框架级）** | 空 `name`、`page=0`、非法 `category`、PATCH 非法 category → 全部 422 |
| **404（业务级）** | run 不存在 / item 不存在 / run-items 的父 run 不存在 / PATCH 不存在的 item → 全部 404 |
| **DELETE /runs/{id}** | 首次 **204**；重复删除 **404**；级联后 `evaluation_items` 剩 **0** 条 |
| **数据库清理** | `evaluation_runs = 0`、`evaluation_items = 0`、临时文件 0 |

### ⭐ 两步错误处理的**两条路**（本节最该记住的对照）

| | 框架级 422 | 业务级 404 / 400 |
| --- | --- | --- |
| **谁抛** | FastAPI 自己（Pydantic 校验失败） | 领域异常 `NotFoundError` / `ValidationError` |
| **触发例** | 空 `name`、`page=0`、`category="xxx"`、路径 `UUID` 格式错 | run/item 不存在；`create_run` 时评测集文件缺失（404）或内容为空（400） |
| **在哪拦** | 请求**还没进**路由函数体 | 在 Service 层抛出，被**全局异常处理器**翻译 |
| **路由层要写 if 吗** | ❌ 一行都不用写 | ❌ 一行都不用写 |

> 🔴 **注意 `ValidationError` 撞名**：Service 抛的是**项目自定义**的
> `app.core.exceptions.ValidationError`（→ 400），**不是** Pydantic 的 `ValidationError`。
> 两者同名不同路，读代码时别看错。

---

## 七、7 个容易踩的坑（本节小结）

| # | 坑 | 正确做法 |
| --- | --- | --- |
| 1 | 把 ORM 实体直接当 `response_model` | 必须返回 `Model.model_validate(entity)` |
| 2 | 用 PUT 做归因覆盖 | 用 **PATCH**，三字段可选、"传 None = 本次不改" |
| 3 | `category` 用普通 `str` 声明 | 用 13 类 `Literal`，非法值自动 422，免写校验 |
| 4 | `add_task` 写在 `create_run` 之前 | **必须之后**（否则后台任务查不到 run，静默 skip） |
| 5 | 以为接口会推送进度 | 不推送，**前端定频轮询 `GET /runs/{id}`** |
| 6 | 204 端点返回 `None` 或字典 | 显式 `return Response(status_code=204)` |
| 7 | 给 `list_datasets` 加 `await` | 它是**同步**方法 |

---

## 八、遗留问题（本步**未改代码**，留到第 10 期收尾一起决定）

| # | 问题 | 位置 | 状态 |
| --- | --- | --- | --- |
| 1 | **`citation_hit` 口径盲区** | `evaluation_service.py` L492-504 | 降级失败的 case 会拿到 `citation_hit=False` 而非 `None`，混进 `citation_hit_rate` 分母（实测 `4/5=0.8` 应为 `4/4=1.0`）。修复只需一个条件 `or answer.error_message`，**已按用户要求"落 B"，本步不动** |
| 2 | `ragas` 依赖**无上界** | `backend/pyproject.toml` L22 | 建议加 `<1.0`。上次 `uv add` 已把 `openai 3.8.0→3.3.0` **降级**，收尾时统一处理 |
| 3 | `_run_single_case` 的 `async with` 写两次事务 | `evaluation_service.py` | docstring 说"独立事务"实为两段；建议改双 `async with`（**已实证不是 bug**，会话 `close()` 后可复用） |
| 4 | 本期全链路**非确定性** | `plan_retrieval` / `judge_context` 的 LLM 决策 | 同一道越域题三次跑出 `refused=False/True/False`，`route` 在 `original/multi_query/hyde` 间跳。**这正是 `evaluation_runs` 要快照每次运行的原因** |
| 5 | **禁止执行 `npm run gen:api`** | 前端 | 会让 `types.gen.ts` 从 2588 行缩到 1115 行、丢 142 个类型（`error TS2339`）。要跑必须先备份 + diff |

---

## 九、本节自查 5 题

1. `create_run` 与 `background_tasks.add_task` 的顺序为什么不能颠倒？颠倒后的**症状**是什么（不是"报错"）？
2. `EvaluationRunRead` 比 `EvaluationRunListItem` 多哪三个字段？为什么要拆成两个模型？
3. 传 `bad_case_category=None` 到 PATCH 接口，数据库里的分类会被清空吗？想清空该传什么？
4. `bad_case_category="hallucination"` 会得到什么状态码？是框架拦的还是业务代码拦的？
5. `DELETE /api/evaluations/runs/{id}` 删第二次为什么返回 404 而不是 204？

---

## 十、本节地图

```
第 7 步「评测 API」
├── 7.1 契约层  app/api/schemas/evaluations.py（269 行）
│   ├── 1. Literal 契约（§3.2）── EvaluationStatusValue(3) / BadCaseCategoryValue(13)
│   ├── 2. Run 模型     ── Create / Read(21) / ListItem(17) / Page
│   ├── 3. Item 模型    ── Read(31) / Page / Update（PATCH 三可选）
│   └── 4. 评测集元数据 ── DatasetInfo / DatasetListResponse
├── 7.2 传输层  app/api/routes/evaluations.py（309 行）
│   ├── §4.1 8 端点总表
│   ├── §4.2 BackgroundTasks 两行顺序 ★
│   ├── §4.3 路由层只做三件事
│   └── §4.4 路径设计（不撞车 / 204 / 双筛选）
└── 7.3 装配    app/main.py（+11 行：1 行 import + 1 行 include_router）

验证：37/37 通过 · OpenAPI 27 个 operationId 零冲突 · 13 类枚举一致
```

---

## 十一、最应该学习的图

**图一 `01_7.1+7.2-评测API端点与契约全景图.png`**
> 一眼看清"5 条路径 / 8 个端点 / 每个端点对应 6.1 的哪个方法 / 返回哪个 DTO"。
> **本节信息密度最高的一张图**，读它就够掌握 7.1 + 7.2 的对应关系。

**图二 `02_7.2-创建评测的BackgroundTasks时序图.png`**
> 一眼看清"**为什么能立即返回 201**"以及"后台任务在响应之后才跑"。
> `BackgroundTasks` 是全期唯一一处"同进程异步"，**时序图比文字准得多**。

---

## 十二、本节实际新增或修改文件

```
后端
├── backend/app/api/schemas/evaluations.py        【新增】269 行 · 契约层
├── backend/app/api/routes/evaluations.py         【新增】309 行 · 传输层
└── backend/app/main.py                           【修改】130 → 141 行（+11）

前端（已就位，等待第 8 步联调）
├── frontend/src/client/types.gen.ts              EvaluationXxx 7 个类型 + 13 类联合
├── frontend/src/client/sdk.gen.ts                8 个 SDK 函数
├── frontend/src/client/index.ts                  导出
└── frontend/src/api/evaluation.ts                TanStack Query 钩子

归档
└── 项目阶段性总结/Day10_评测与 Bad Case 分析/04_2026.9.25/
    └── 02_backend_app_api_routes_evaluations++backend_app_api_schemas_evaluations++backend_app_main/
        ├── 01_7.1+7.2-评测API端点与契约全景图.png
        ├── 02_7.2-创建评测的BackgroundTasks时序图.png
        └── 07_评测API.md                          【本文件】
```

> 依赖变更：`backend/pyproject.toml` 新增 `ragas>=0.4.3`（第 4 步引入）+ `langchain-community<0.4.2`，本节无新增依赖。
