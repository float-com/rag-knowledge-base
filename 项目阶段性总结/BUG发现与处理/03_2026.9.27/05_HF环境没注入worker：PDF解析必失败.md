# 05_HF 环境没注入 worker：PDF 解析必失败

> 期：**Day12 · 缓存、限流、异步任务与增量索引** → 第 4 节「Celery 异步任务」的连带影响
> 发现日期：2026.09.27（第 5 节收尾、用户实测上传 PDF 时暴露）
> 严重级别：**高** —— PDF 功能完全不可用，且**没有任何报错流向调用方**
> 潜伏时长：**从第 4 节改造完成到用户第一次上传 PDF 为止**
> 一句话：第 4 节把文档解析从 API 进程搬进了 worker 进程，
> 而 HF 环境注入只写在 `app/main.py` 里 —— **worker 是第三种进程入口，没人管它**。

---

## 症状（极具欺骗性）

用户上传一份 893KB 的 PDF，前端显示"解析中"，然后变成失败：

```
documents.status       = failed
documents.error_message = Docling 解析失败: Got: ConnectTimeout: [WinError 10060]
                          由于连接方在一段时间后没有正确答复或连接的主机没有反应，连接尝试失败。
                          An error happened while trying to locate the files on the Hub,
                          and we cannot find the appropriate snapshot folder for the
                          specified revision on the local disk.
                          Please check your internet connection and try again.
document_chunks        = 0
```

**同一时间上传的 Markdown / HTML 完全正常**（HTML 22 个切片、Markdown 正常入库）。

这个对比把排查方向指向了两个**都是错的**结论：

```
❌ 「本项目不支持 PDF 上传」
      → 白名单里明明有 .pdf；历史上（09-11、09-14）成功上传解析过 4 次，含一个 3.2MB 的

❌ 「网络问题 / 镜像站挂了」
      → .env 里 HF_ENDPOINT=https://hf-mirror.com 配了；而且模型早就缓存在本地 models/ 里（505MB）
```

## 时间线证据（这是定位的起点）

```
2026-09-11  黄策吾简历.pdf                  231KB  → 成功
2026-09-14  JavaEE编程技术课程设计-实验指导书.pdf  3.2MB → 成功
2026-09-27  第 4 节 Celery 改造完成（解析搬进 worker 进程）
2026-09-27  report_preview.pdf              893KB  → 【失败】ConnectTimeout
2026-09-27  report_preview.html              32KB  → 成功
```

**"改造之前能用、改造之后不能用"** —— 一句话就把嫌疑锁定在第 4 节的迁移上。

## 根因：三种进程入口只设了两处

`app/core/hf_env.py` 的 docstring 自己就写明了约束：

> huggingface_hub 是在 **import 阶段** 读取 HF_ENDPOINT 等环境变量并固化为模块级常量的，
> 之后再改就无效了。
> 【使用方式】：
> - 应用入口：`app/main.py` 顶部第一件事就是 `setup_hf_environment()`；
> - 独立脚本：脚本里同样要先 `setup_hf_environment()`，再去 import docling。

但**代码里只有 `app/main.py` 一处调用**（grep `setup_hf_environment` 全项目仅此一处）：

```
进程入口清单（改造后应当是 3 个）
  ① API 进程        app/main.py            ← setup_hf_environment() 在这里 ✅
  ② Celery worker   app/celery_app.py      ← 【没有任何人做】            ❌
  ③ 独立脚本        各脚本自己负责           ← 约定
```

于是 worker 进程里：

```
HF_ENDPOINT = None   →  huggingface_hub 回落官方源 https://huggingface.co（国内不可达）
HF_HOME     = None   →  缓存目录回落 ~/.cache/huggingface
                        → 既找不到项目里那 505MB 已缓存的模型，又要去连官方源
                        → ConnectTimeout
```

**两处缺失是同一个根因**：`HF_HOME` 没设导致它去默认目录找模型（找不到），`HF_ENDPOINT` 没设导致它去官方源下载（连不上）。缺一个都还能勉强活，缺两个就必死。

## 为什么它极难联想到

1. **错误信息把人引向"网络问题"。** `ConnectTimeout` + "Please check your internet connection"
   看起来就是网络不通。而真实原因是**配置压根没生效** —— 网络是好的，只是走错了地址。

2. **只有 PDF 会触发。** Markdown / HTML 不需要任何模型（splitter 纯文本切分），
   所以「别的格式都能传，只有 PDF 挂」很容易被当成"PDF 支持有问题"，
   而不是"worker 的运行环境不完整"。

3. **项目其实早就准备好了。** 500MB 模型就在 `models/` 里躺着，
   `.env` 里镜像站也配了 —— 这些"看起来已经做好的事"，恰恰让人不会去想
   "worker 知不知道这些配置"。

## 排查手法（可复用）

**拿两个进程对比同一份配置。** 这次就是这么一次定位的：

```python
# A. 模拟 worker：只 import 业务模块，不 import app.main
from app.ingestion import parser              # 连带 import docling -> huggingface_hub
from huggingface_hub import constants as c
print(os.environ.get('HF_ENDPOINT'), c.ENDPOINT)
#   → None   https://huggingface.co          ← 镜像配置没生效

# B. 模拟 API 进程：先 setup 再 import
from app.core.hf_env import setup_hf_environment
setup_hf_environment()
from huggingface_hub import constants as c
print(c.ENDPOINT)
#   → https://hf-mirror.com                  ← 生效
```

**两个进程跑同一份 `.env` 却得到不同结果** —— 答案只可能是"某个进程少做了一步初始化"。

> 注意：`huggingface_hub` 在 import 阶段就把常量固化了，所以 A 和 B **必须分成两个独立进程**跑，
> 在同一个进程里先跑 A 再跑 B 是测不出来的。

## 修法

在 `app/celery_app.py` 的顶部补上第二处进程入口初始化：

```python
from app.core.hf_env import setup_hf_environment

# 必须在任何可能 import docling / huggingface_hub 的东西之前执行
setup_hf_environment()

celery_app = Celery(
    "rag_knowledge_base",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.ingestion.tasks"],
)
```

**为什么放这里就够**：worker 由 `celery -A app.celery_app worker` 启动，
Celery **先**加载 `app.celery_app`，**后**才按 `include` 去导入 `app.ingestion.tasks`。
而 `app/celery_app.py` 本身只 import `celery` 与 `app.core.config`，都不碰 huggingface_hub ——
所以这一行执行时，hf_hub 还没被导入，环境变量还来得及生效。

**为什么不是放进 `app/ingestion/tasks.py`**：那里已经 `from app.ingestion.pipeline import ...`，
而 pipeline 会连带 import docling。要生效就得把 import 拆开、在中间插一行调用 ——
那是"靠语句顺序救命"的脆弱写法。放在**进程入口**才是概念上正确的位置。

## 验证结果

worker 日志（修复后真实输出）：

```
20:38:13  Downloading object-detection model from HuggingFace: docling-project/docling-layout-heron@main
20:38:15  HTTP Request: GET https://hf-mirror.com/api/models/docling-project/docling-layout-heron/revision/main "HTTP/1.1 200 OK"
20:38:15  Downloaded model ... to D:\code\rag-knowledge-base\models\hub\... in 2.00 sec.   ← 走镜像站了
20:38:19  Model docling-project/docling-models already cached at D:\...\models\hub\...      ← 500MB 缓存命中
20:41:50  Finished converting report_preview.pdf in 225.00 sec.
20:41:54  ingest done: document_id=c2538b37-... chunks=23
```

```
documents.status = ready   document_chunks = 23   ✅
```

**顺带确认的两件事**：
- 修复后**不需要重新下载模型** —— `HF_HOME` 一设对，那 505MB 缓存立刻命中；
- 893KB 的 PDF 在 CPU 上解析要 **225 秒**（3 分 45 秒）。所以"解析中"停几分钟是正常的，
  不是卡死。想提速可以装 `onnxruntime` —— 日志里 Docling 的 OCR 回退到了
  `rapidocr + torch`（CPU 版 torch 很慢），因为 `onnxruntime` / `easyocr` 都没装。

## 可迁移的教训

1. **"进程入口"是一份容易过期的清单。**
   每引入一种新进程（worker / 定时任务 / CLI / 迁移脚本），都要回头问一遍：
   **入口处该做的初始化，在新进程里做了没有？**
   本项目 `hf_env.py` 的注释里只列了 2 处，第 4 节引入 worker 后**没有回头更新这份清单**。

2. **"必须在 import 之前执行"的初始化，天然容易漏。**
   它对**调用位置**敏感（早一行没用、晚一行失效），而代码审查通常只看"有没有调用"，
   不问"调用得够不够早"。这类初始化应该**贴着进程入口写**，而不是靠调用方自觉。

3. **迁移搬走的不只是代码，还有它依赖的运行环境。**
   第 4 节把 `ingest_document` 从 API 进程搬到 worker 时，一起被"搬走"的还有：
   - 请求级数据库会话 → 已由 `run_worker_coro` + 自建会话解决 ✅
   - HF 环境变量 → **本次漏掉的就是它** ❌
   做进程间迁移时，值得把"这段代码原先依赖哪些进程级状态"单列一张清单逐条核对。

4. **报错信息会指向错误的方向。**
   `ConnectTimeout` 让人以为是网络故障，实际是"配置没生效导致走错了地址"。
   遇到"配置明明配了却像是没生效"的情况，
   正确做法是**打印运行时真实生效的值**（本例是 `huggingface_hub.constants.ENDPOINT`），
   而不是反复检查配置文件本身。

5. **"只有某一种格式会失败"是一个强信号。**
   它说明问题在**该格式独有的处理路径**上。PDF 独有的是"需要下载 ML 模型"，
   顺着这条线就能走到 worker 的运行环境。

## 归档交接

- 修复代码：`backend/app/celery_app.py` 顶部的 `setup_hf_environment()`（含完整注释）
- 相关模块：`backend/app/core/hf_env.py`（约束写在它的 docstring 里）
- 同类问题（同一天、同一节）：
  - `03_Celery第二个任务必崩：事件循环与连接池生命周期错配.md` —— 也是"进程迁移的连带影响"
  - `04_await优先级写错：任务台账查询必崩.md`
- 三个共同点：**都是"照教程改完、看起来成功、实际没跑通"**，
  且都只在真实使用（而不是自测）时才暴露。
