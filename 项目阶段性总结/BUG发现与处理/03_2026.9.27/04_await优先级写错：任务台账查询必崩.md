# 04_await 优先级写错：任务台账查询必崩

> 期：**Day12 · 缓存、限流、异步任务与增量索引** → 第 5 节「增量索引」
> 发现日期：2026.09.27
> 严重级别：**高** —— 功能完全不可用（每次调用必抛 AttributeError → HTTP 500）
> 潜伏时长：**整整一个章节**（第 4 节写下，第 5 节才第一次被执行）
> 一句话：`await self.session.execute(stmt).scalar_one_or_none()` 这一行的运算符优先级是错的，
> 而它出自**教程原文**。

---

## 症状（延迟了整整一节才爆发）

第 5 节给 `DocumentRead` 加上 `latest_task` 字段之后，调用
`POST /api/documents/{id}/reindex` 得到：

```
500  {"code":"internal_error","message":"服务内部错误"}
```

而**几乎同时**，数据库里那份文档的 `version` 已经从 1 变成了 2 —— 也就是说
**业务副作用全都成功了，只是返回响应体的时候炸了**。

这个"半成功"现象是定位的关键线索：它立刻把排查范围从"整条 reindex 链路"
缩小到了"**服务方法返回之后的响应组装环节**"。

## 复现

```python
# 直接调服务层拿真实堆栈（比看 HTTP 500 快得多）
async with AsyncSessionLocal() as s:
    svc = DocumentService(s)
    doc = await svc.get(doc_id)
    latest = await svc.get_latest_task(doc.id)      # ← 就炸在这一行
```

```
  File ".../app/services/document_service.py", line 848, in get_latest_task
    return await self.task_repo.get_latest_by_document(document_id)
  File ".../app/db/repositories/ingestion_task_repo.py", line 87, in get_latest_by_document
    return await self.session.execute(stmt).scalar_one_or_none()
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AttributeError: 'coroutine' object has no attribute 'scalar_one_or_none'
sys:1: RuntimeWarning: coroutine 'AsyncSession.execute' was never awaited
```

## 根因：`.` 的优先级高于 `await`

Python 里 **属性访问 `.` 的优先级高于 `await`**。所以这一行并不是"先 await 再取结果"：

```python
await self.session.execute(stmt).scalar_one_or_none()
```

它的实际解析结果是：

```python
await ( self.session.execute(stmt).scalar_one_or_none() )
#      └────────────── 先算这一段 ──────────────┘
```

而 `AsyncSession.execute()` 是**协程函数**，调用它得到的只是一个**协程对象**，
协程对象上根本没有 `scalar_one_or_none` 这个方法 —— 于是在 `await` 生效之前，
属性访问就已经先抛 `AttributeError` 了。

日志末尾那句 `RuntimeWarning: coroutine 'AsyncSession.execute' was never awaited`
是这类错误的典型指纹：**只要看到它，第一反应就该去查 `await` 与 `.` 的优先级。**

## 为什么它能潜伏整整一节

这是本次最值得记住的一点：

```
第 4 节  write:  async def get_latest_by_document(...)   ← 写了，但从没人调
                 ↑ 全项目唯一的调用方是第 5 节的 _to_document_read
第 4 节  验证:   只验证了"任务能跑通、台账状态会变"——
                 全程没有一条断言碰到过这个方法
第 5 节  first call → 立刻必崩
```

也就是说：**这段代码在写下之后的整整一节时间里，从未被执行过一次。**

它没有被发现，不是因为它隐蔽，而是因为**没有调用方的代码等于未验证的代码**。
第 4 节的验证清单看起来很完整（任务注册、迁移、端到端上传、重试、进度推进……），
但恰恰漏掉了"台账的读取路径"—— 因为当时那一条路径根本还不存在。

> 顺带说明：这个 BUG 也解释了为什么第 4 节验收时"看起来一切正常"。
> 一个只写不读的仓储方法，是可以在测试里完全隐形的。

## 修法：拆成两步

```python
result = await self.session.execute(stmt)
return result.scalar_one_or_none()
```

或者保留单表达式写法、但**主动加括号**：

```python
return (await self.session.execute(stmt)).scalar_one_or_none()
```

两种都可以。本项目选了**拆成两步**，并在注释里把这段优先级陷阱完整写了下来 ——
因为它不是"手误"，而是一个很容易再犯的语法盲区。

## 全局排查结果：32 处里只有这 1 处写错

修完之后对全后端做了一次模式扫描（正则匹配 `await ...execute(...).<访问器>`）：

```
chunk_repo.py         7 处  全部写成 (await ...).scalars().all() 之类，正确
document_repo.py      4 处  正确
conversation_repo.py  6 处  正确
evaluation_repo.py    5 处  正确
role_repo.py          3 处  正确
user_repo.py          4 处  正确
citation_repo.py      2 处  正确
ingestion_task_repo.py 1 处 【错误】← 就是它，已修
```

**31 处正确写法全都带了括号，只有这一处没带** —— 也就是说项目里既有的书写习惯是对的，
是第 4 节照抄教程时把教程的那一处错误一起抄了进来。

## 可迁移的教训

1. **照抄不等于验证过。** 教程的代码片段是"教学正确"，不是"运行正确"。
   本次两节里已经连撞两次同类问题（第 4 节的 `asyncio.run`、第 5 节的这一行）——
   它们的共同点是：**在教程那个"只跑一次/只写不读"的示例语境里都不会暴露**。

2. **没有调用方的代码 = 未验证的代码。**
   写下任何方法时，顺手问一句"现在有谁会调它？"。如果答案是"暂时没有"，
   那就要么当场写一个最小的调用（哪怕只是在验证脚本里调一下），
   要么在注释里显式标记"本方法尚无调用方，尚未被执行过"。

3. **`await` 与 `.` 的优先级是 Python 里最容易忘的一条。**
   安全写法是永远带括号：`(await xxx()).yyy()`，而不是 `await xxx().yyy()`。

4. **`RuntimeWarning: coroutine ... was never awaited` 是"优先级写错"的指纹。**
   它几乎总是意味着：你把某个协程对象当普通对象用了。

5. **"500 但数据其实已经变了"是最难排查的一类失败。**
   这次的线索不是日志，而是**数据库里的 version 已经从 1 变成了 2** ——
   用它反推出"失败发生在副作用之后"。
   以后遇到"报错但像是做了一半"的情况，先去核对数据状态，比读堆栈更快定位。

6. **做横切改动时，"读取路径"要单独过一遍。**
   第 5 节往 `DocumentRead` 上挂了 `latest_task`，等于给 5 个端点同时新增了一条读取路径
   （list / get / upload / retry / reindex）。这类"一处改动、多处生效"的路径，
   验证时必须逐个端点打钩。

## 修复后的验证结果

```
守卫与权限（14/14 全通过）
  · 内容与当前版本一致      → 400  「文件内容与现有版本一致，无需重新索引」
  · 内容与另一份文档一致    → 400  「新版本内容与库中已有文档《...》完全一致」
  · MIME 与原文档不一致     → 400
  · 状态为 uploading        → 400
  · 空文件                  → 400
  · 未登录                  → 401
  · 非管理员                → 403
  · document_id 不存在      → 404

契约
  · GET 详情带 version / latest_task（字段齐全）
  · 列表 15 条全部带 latest_task
  · 旧链路 upload 响应带 latest_task / version
  · OpenAPI：paths 29 / operations 40 / unique operationId 40
  · 前端 tsc -b 退出码 0（成品前端代码与新契约完全一致）
```

## 归档交接

- 相关代码：`backend/app/db/repositories/ingestion_task_repo.py` 的
  `get_latest_by_document`（含完整注释）
- 触发它的新代码：`backend/app/api/routes/documents.py` 的 `_to_document_read`
- 相关归档：`Day12_.../02_2026.9.27/05_增量索引/06_增量索引.md`
- 同期的另一处教程问题（第 4 节）：`03_Celery第二个任务必崩：事件循环与连接池生命周期错配.md`
