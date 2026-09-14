# 文档上传链路 Mermaid 架构图

配套 ASCII 版见 `上传链路架构对比.md`。本文提供可直接渲染的 Mermaid 源码，适用于 GitHub / VS Code / Typora / Mermaid Live Editor。

---

## 1. 旧链路：multipart 代理上传（时序图）

```mermaid
sequenceDiagram
    autonumber
    participant FE as 浏览器 前端
    participant API as FastAPI 8000
    participant COS as 腾讯云 COS
    participant BG as BackgroundTasks
    participant DB as PostgreSQL

    FE->>API: POST /api/documents<br/>multipart 携带整个文件二进制
    Note over API: 文件字节流完整进入业务服务器

    API->>API: _resolve_mime_and_suffix<br/>后缀与 MIME 双重校验
    API->>API: content = await file.read<br/>全量读进服务端内存
    API->>API: 空文件与 UPLOAD_MAX_SIZE_MB<br/>上限校验
    API->>API: sha256 content 计算文件指纹

    API->>DB: get_by_hash file_hash
    DB-->>API: existing 或 None

    alt 哈希命中 秒传
        API-->>FE: 201 直接返回已有 Document
        Note over API,COS: 不传 COS 不建档<br/>不触发 ingest
    else 哈希未命中
        API->>COS: put_object key=documents/hash.suffix<br/>整个 bytes 一次性上传
        COS-->>API: 200
        Note over API,COS: 同一份文件在网络上走了两遍

        API->>DB: 建 Document status=UPLOADING 并 commit
        API->>BG: add_task ingest_document
        API-->>FE: 201 Created DocumentRead

        BG->>DB: 推进 PARSING 到 INDEXING 到 READY
    end
```

---

## 2. 新链路：预签名 URL 直传（时序图）

```mermaid
sequenceDiagram
    autonumber
    participant FE as 浏览器 前端
    participant API as FastAPI 8000
    participant COS as 腾讯云 COS
    participant BG as BackgroundTasks
    participant DB as PostgreSQL

    rect rgb(235, 245, 255)
    Note over FE,DB: 阶段一 INIT 创建会话并签发通行证
    FE->>API: POST /api/documents/uploads/init<br/>只有 file_name size mime_type permission_tags
    Note over FE,API: 请求体里没有文件内容
    API->>API: 大小上限校验
    API->>API: _resolve_upload_type 后缀白名单
    API->>DB: 建 UploadSession status=INITIATED<br/>object_key=document-uploads/uuid/文件名
    API->>API: create_presigned_upload<br/>Method=PUT Expired=300<br/>签名绑定 key 与 Content-Type
    API->>DB: commit
    API-->>FE: upload_id object_key presigned_url<br/>status=initiated
    end

    rect rgb(235, 255, 240)
    Note over FE,COS: 阶段二 直传 文件不经过 8000 端口
    FE->>COS: OPTIONS 预检 Origin 与<br/>Access-Control-Request-Method
    COS-->>FE: 200 Access-Control-Allow-Origin 等
    Note over FE,COS: 桶上没配 CORS 规则<br/>会被浏览器在这里拦下
    FE->>COS: PUT presigned_url<br/>Content-Type 与文件二进制
    COS-->>FE: 200 ETag
    end

    rect rgb(255, 250, 235)
    Note over FE,DB: 阶段三 COMPLETE 权威校验后转入后台
    FE->>API: POST /api/documents/uploads/id/complete<br/>空请求体
    API->>DB: get_by_id 并 _ensure_not_expired 过期校验
    API->>API: 状态机 FINALIZING 或 COMPLETED 则幂等返回<br/>其他状态返回 409

    API->>COS: head_object object_key<br/>只取元数据不下载正文
    COS-->>API: Content-Length 与 Content-Type
    Note over API: 以 COS 服务端为权威<br/>不信任前端传来的上传成功声明

    API->>DB: status=FINALIZING 并 commit
    API->>BG: add_task finalize_upload
    API-->>FE: status=finalizing
    Note over FE: 此刻 Document 尚未创建<br/>前端列表看不到
    end

    rect rgb(248, 240, 255)
    Note over BG,DB: 阶段四 后台 finalize 哈希 去重 建档 触发 ingest
    BG->>BG: 使用独立 AsyncSessionLocal<br/>不占用请求 session
    BG->>COS: hash_object 流式读取<br/>每 1MB 分块计算 SHA-256
    COS-->>BG: 字节流
    BG->>DB: get_by_hash file_hash
    alt 哈希命中 去重
        BG->>COS: delete_object 删除临时对象
        BG->>DB: UploadSession 置 COMPLETED
        Note over BG,DB: 不创建 Document<br/>也不触发 ingest
    else 哈希未命中
        BG->>DB: 建 Document status=UPLOADING<br/>cos_object_key 沿用临时路径
        BG->>DB: UploadSession 置 COMPLETED 并 commit
        BG->>DB: 调用 ingest_document 推进<br/>PARSING INDEXING READY
    end
    end
```

---

## 3. 两条链路的架构对比（流程图）

```mermaid
flowchart TD
    classDef oldPath fill:#eceff1,stroke:#90a4ae,stroke-width:1.5px,color:#263238;
    classDef newPath fill:#e8f5e9,stroke:#43a047,stroke-width:2px,color:#1b5e20;
    classDef browserAct fill:#e3f2fd,stroke:#1e88e5,stroke-width:2px,color:#0d47a1;

    subgraph OLD["旧链路 已退役 multipart 代理上传（前端已停用 仅 API 可用）"]
        direction TB
        O1[" 浏览器读取整个文件 <br/> 文件要交给后端 "] --> O2[" POST /api/documents <br/> multipart 走 8000 端口 "]
        O2 --> O3[" 服务端把整个文件 <br/> 读进内存 "]
        O3 --> O4[" sha256 算文件指纹 "]
        O4 --> O5{"documents 表里<br/>是否已有同样的 hash"}
        O5 -->|"已有 命中"| O6[" 秒传 直接返回已有记录 <br/> 不传 COS 不建档 "]
        O5 -->|"没有 未命中"| O7[" 后端 put_object 转存 COS <br/> 同一份文件多走一趟服务器 "]
        O7 --> O8[" 建 Document 记录 <br/> key = documents/hash.suffix "]
        class O1,O2,O3,O4,O5,O6,O7,O8 oldPath
    end

    subgraph NEW["新链路 当前在用 预签名 URL 直传"]
        direction TB
        N1[" POST uploads/init <br/> 只发元数据 不发文件 "] --> N2[" 建 UploadSession <br/> status INITIATED "]
        N2 --> N3[" 签发 presigned_url <br/> 签名绑定 key 与 Content-Type <br/> 限时 300 秒 "]
        N3 --> N4[" 浏览器 OPTIONS 预检 <br/> 跨域请求 桶上必须配 CORS "]
        N4 --> N5[" 浏览器 PUT 直传 COS <br/> 文件完全不经过 8000 "]
        N5 --> N6[" POST uploads/id/complete <br/> 空请求体 "]
        N6 --> N7[" COS HEAD 权威校验 <br/> 以云端记录的 size 与 MIME 为准 <br/> 不信任前端声明 "]
        N7 --> N8[" UploadSession 置 FINALIZING <br/> 挂后台任务后立刻返回 "]
        N8 --> N9[" 后台 finalize_upload <br/> 独立 session 流式哈希与去重 "]
        N9 --> N10[" 建 Document 记录 <br/> key = document-uploads/uuid/文件名 "]
        class N1,N2,N3,N6,N7,N8,N9,N10 newPath
        class N4,N5 browserAct
    end
```

---


## 4.阶段对齐图（推荐用于逐步对照）

把两条链路的**同一个阶段放在同一行**，直接看出哪些步骤等价、哪些是新增的、从哪一步开始合流。没有跨子图连线。

```mermaid
flowchart TD
    classDef oldPath fill:#eceff1,stroke:#90a4ae,color:#263238;
    classDef newPath fill:#e8f5e9,stroke:#43a047,stroke-width:1.5px,color:#1b5e20;
    classDef shared fill:#f3e5f5,stroke:#8e24aa,stroke-width:2px,color:#4a148c;
    classDef noteBox fill:#ffffff,stroke:#bdbdbd,stroke-dasharray:4 3,color:#616161;

    subgraph P1["第一步 起点"]
        direction LR
        P1O["  旧 浏览器读取整个文件  <br/>  文件要交给后端  "]
        P1N["  新 只发文件元数据  <br/>  name size mime  "]
        class P1O oldPath
        class P1N newPath
    end

    subgraph P2["第二步 到达后端"]
        direction LR
        P2O["  旧 POST /api/documents  <br/>  multipart 携带文件  "]
        P2N["  新 建 UploadSession  <br/>  并签发 presigned_url  <br/>  后端全程不碰文件内容  "]
        class P2O oldPath
        class P2N newPath
    end

    subgraph P3["第三步 文件怎么上云"]
        direction LR
        P3O["  旧 服务端读入内存后  <br/>  put_object 转存  <br/>  文件进一次 出一次  "]
        P3N["  新 浏览器 PUT 直传 COS  <br/>  先过 OPTIONS 预检  "]
        class P3O oldPath
        class P3N newPath
    end

    subgraph P4["第四步 上传结果校验"]
        direction LR
        P4O["  旧 无独立校验  <br/>  服务端本来就有全量 bytes  "]
        P4N["  新 COS HEAD 复核  <br/>  比对外部记录的  <br/>  size 与 MIME  "]
        class P4O oldPath
        class P4N newPath
    end

    subgraph P5["第五步 算指纹"]
        direction LR
        P5O["  旧 上传前  <br/>  sha256 content  "]
        P5N["  新 上传后 后台流式读取  <br/>  每 1MB 分块算 SHA-256  "]
        class P5O oldPath
        class P5N newPath
    end

    subgraph P6["第六步 去重与建档"]
        direction LR
        P6O["  旧 documents 表  <br/>  key = documents/  <br/>  hash.suffix  "]
        P6N["  新 documents 表  <br/>  key = document-uploads/  <br/>  uuid/文件名  "]
        class P6O oldPath
        class P6N newPath
    end

    subgraph AFTER["合流点：从 documents 表往后 两条链路共用同一套代码"]
        direction TB
        A1["  ingest_document  <br/>  入库流水线  "] --> A2["  Docling 解析原文件  "]
        A2 --> A3["  splitter 切块  <br/>  生成 chunk_hash  "]
        A3 --> A4["  embedder 批量向量化  "]
        A4 --> A5["  写入 document_chunks 表  <br/>  含 pgvector  <br/>  向量数据  "]
        A5 --> A6["  documents 表  <br/>  status 置  <br/>  READY  "]
        class A1,A2,A3,A4,A5,A6 shared
    end

    subgraph NOTES["链路特性对比说明"]
        direction TB
        S1["  分歧只在 文件怎么进来  <br/>  从 documents 表往后  <br/>  是同一套代码  "]
        S2["  旧链路唯一优势  <br/>  上传前就能发现重复  <br/>  新链路代价  <br/>  文件先上了 COS 才知道重复  "]
        class S1,S2 noteBox
    end

    P1O --> P2O
    P2O --> P3O
    P3O --> P4O
    P4O --> P5O
    P5O --> P6O
    P1N --> P2N
    P2N --> P3N
    P3N --> P4N
    P4N --> P5N
    P5N --> P6N
    P6O --> A1
    P6N --> A1
```

## 5. 新链路两个状态机

```mermaid
stateDiagram-v2
    direction LR

    state "upload_sessions 只管文件有没有传上来" as US {
        [*] --> INITIATED : init 建会话并签发 URL
        INITIATED --> FINALIZING : complete 通过 COS HEAD 校验
        FINALIZING --> COMPLETED : finalize 建档成功
        FINALIZING --> FAILED : finalize 抛异常
        INITIATED --> ABORTED : DELETE 主动取消
        INITIATED --> EXPIRED : 超过 expires_at
    }

    state "documents 只管内容有没有处理完" as DOC {
        [*] --> UPLOADING : finalize 建档
        UPLOADING --> PARSING : Docling 解析
        PARSING --> INDEXING : 切分与向量化
        INDEXING --> READY : 切片落库成功
        UPLOADING --> FAILED
        PARSING --> FAILED
        INDEXING --> FAILED
    }

    COMPLETED --> UPLOADING : 建档后立即触发 ingest_document
    READY --> [*]
    FAILED --> [*]
    ABORTED --> [*]
    EXPIRED --> [*]
```

---

## 6. 两条链路的对象键规则

```mermaid
flowchart LR
    subgraph OLDKEY["旧链路 内容寻址 CAS"]
        direction LR
        A1[" 文件内容 "] --> A2[" sha256 摘要 "] --> A3[" documents/hash.suffix "]
        A3 --> A4[" 同样内容必得同样 key <br/> 天然幂等 "]
    end

    subgraph NEWKEY["新链路 会话隔离"]
        direction LR
        B1[" upload_id UUID "] --> B2[" document-uploads/<br/>uuid/原始文件名 "]
        B2 --> B3[" 按会话隔离 <br/> 防并发同名覆盖 "]
        B3 --> B4[" 建档时直接沿用该路径 <br/> 不再搬到 documents 前缀下 "]
    end

    A4 -.->|" 仍然保留的统一约束 "| C["  documents.file_hash  <br/>  唯一索引  <br/>  去重语义不变  <br/>  物理路径换规则  "]
    B4 -.-> C
```

---

## 7. 端到端全景（上传到可检索）

```mermaid
flowchart TD
    U["用户选择文件"] --> INIT["POST uploads/init"]
    INIT --> SS["upload_sessions 表<br/>INITIATED"]
    SS --> SIGN["签发 presigned_url"]
    SIGN --> PUT["浏览器 PUT 直传 COS"]
    PUT --> CORS{"CORS 预检通过"}
    CORS -->|"否"| FAIL1["PUT 被浏览器拦截<br/>complete 永不触发<br/>界面静默失败"]
    CORS -->|"是"| CP["POST uploads/id/complete"]
    CP --> HEAD["COS HEAD 校验<br/>size 与 MIME"]
    HEAD --> ST2["upload_sessions 表<br/>FINALIZING"]
    ST2 --> FIN["后台 finalize_upload<br/>独立 session"]
    FIN --> HASH["流式 SHA-256"]
    HASH --> DUP{"documents 表<br/>哈希已存在"}
    DUP -->|"是"| DROP["删临时对象<br/>upload_sessions COMPLETED"]
    DUP -->|"否"| DOC["documents 表<br/>UPLOADING"]
    DOC --> ING["ingest_document"]
    ING --> DL["从 COS 下载原文"]
    DL --> PARSE["Docling 解析<br/>依赖本地模型权重"]
    PARSE --> SPLIT["切分 chunks"]
    SPLIT --> EMB["调用 Embedding 生成向量"]
    EMB --> CHUNKS["document_chunks 表<br/>含 pgvector 向量"]
    CHUNKS --> READY["documents 表 READY<br/>可被检索召回"]
```

---
