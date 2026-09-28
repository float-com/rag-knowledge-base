"""第 4 段：追查第 3 段暴露的三条线索 + 清理。

线索 1：REST SSE 下发的事件名是 citations（复数），前端 types.gen.ts 是否也是复数？
线索 2：语义缓存没有命中/写入（Redis db2 52->52）—— 是否因为走了"拒答"路径而不缓存？
线索 3：alice 与 admin 的统计口径（document_count 相同、chunk_count 不同）是否自洽？
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[5]  # 归档后位于 项目阶段性总结/Day13.../001_冒烟测试脚本/，项目根在第 5 层
PASS, FAIL, WARN = [], [], []


def ok(n, d=""):
    PASS.append((n, d)); print(f"  [OK]   {n}" + (f"  |  {d}" if d else ""))


def bad(n, d=""):
    FAIL.append((n, d)); print(f"  [FAIL] {n}" + (f"  |  {d}" if d else ""))


def warn(n, d=""):
    WARN.append((n, d)); print(f"  [WARN] {n}" + (f"  |  {d}" if d else ""))


def env(key, default=""):
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return default


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=180.0)
    admin_t = c.post("/api/auth/login", json={
        "username": env("DEFAULT_ADMIN_USERNAME", "admin"),
        "password": env("DEFAULT_ADMIN_PASSWORD"),
    }).json()["access_token"]
    HA = {"Authorization": f"Bearer {admin_t}"}
    alice_t = None
    r = c.post("/api/auth/login", json={"username": "alice", "password": "AliceSmoke!2026"})
    if r.status_code == 200:
        alice_t = r.json()["access_token"]
    HL = {"Authorization": f"Bearer {alice_t}"} if alice_t else None

    # ------------------------------------------------ 线索 1：citations 事件载荷形状
    print("\n=== 线索 1：SSE citations 事件的载荷形状（前端契约）===")
    r = c.post("/api/conversations", headers=HA, json={"title": "冒烟-载荷检查"})
    cid = r.json()["id"]
    events = {}
    cur = None
    with c.stream("POST", f"/api/conversations/{cid}/chat", headers={**HA, "Accept": "text/event-stream"},
                  json={"question": "实验要求是什么？"}, timeout=180.0) as resp:
        for line in resp.iter_lines():
            if line.startswith("event:"):
                cur = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and cur:
                try:
                    events.setdefault(cur, []).append(json.loads(line.split(":", 1)[1].strip()))
                except Exception:
                    pass
    cites = events.get("citations") or []
    ok("收到 citations 事件", f"{len(cites)} 条") if cites else warn("本次没有 citations（可能被拒答）")
    if cites:
        first = cites[0]
        print("   citations[0] 的键：", sorted(first.keys()))
        expected = {"ordinal", "chunk_id", "document_id", "document_name", "page_no", "section_path", "score", "quote"}
        missing = expected - set(first.keys())
        ok("citations 含前端必需字段") if not missing else bad("citations 缺字段", str(sorted(missing)))
        extra = set(first.keys()) - expected
        if extra:
            warn("citations 多出字段（前端 types 里可能未声明）", str(sorted(extra)))

    # ------------------------------------------------ 线索 2：语义缓存到底写不写
    print("\n=== 线索 2：语义缓存是否写入（换一个能答上来的问题，问两次）===")
    import subprocess
    def dbsize():
        o = subprocess.run(["docker", "exec", "rag-kb-redis", "redis-cli", "-n", "2", "DBSIZE"],
                           capture_output=True, text=True, timeout=20)
        return int(o.stdout.strip() or -1)

    q = "文档里提到的权限标签是怎么工作的？"
    sizes = []
    for i in (1, 2):
        r = c.post("/api/conversations", headers=HA, json={"title": f"冒烟-缓存{i}"})
        cid2 = r.json()["id"]
        n0 = dbsize()
        import time
        t0 = time.time()
        payload = {}
        cur = None
        with c.stream("POST", f"/api/conversations/{cid2}/chat", headers={**HA, "Accept": "text/event-stream"},
                      json={"question": q}, timeout=180.0) as resp:
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    cur = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and cur == "message_end":
                    try:
                        payload = json.loads(line.split(":", 1)[1].strip())
                    except Exception:
                        pass
        n1 = dbsize()
        sizes.append((n0, n1, round(time.time() - t0, 1), payload.get("refused")))
        print(f"   第 {i} 次：Redis {n0}->{n1}  耗时 {time.time()-t0:.1f}s  refused={payload.get('refused')}")

    if sizes[0][1] > sizes[0][0]:
        ok("缓存有写入（Redis key 增加）", f"{sizes[0][0]} -> {sizes[0][1]}")
    else:
        warn("缓存未写入", f"两次 Redis 均为 {sizes[0][0]}；若两次都拒答，属预期（拒答不入缓存）")

    # ------------------------------------------------ 线索 3：统计口径
    print("\n=== 线索 3：统计口径（admin vs alice，并对照原始 SQL）===")
    def sql(q):
        o = subprocess.run(["docker", "exec", "rag-kb-postgres", "psql", "-U", "rag", "-d", "rag_kb", "-t", "-A", "-c", q],
                           capture_output=True, text=True, timeout=30)
        return o.stdout.strip()

    db_docs_all = sql("select count(*) from documents;")
    db_docs_public = sql("select count(*) from documents where permission_tags = '{}';")
    db_chunks_ready = sql("select count(*) from document_chunks c join documents d on d.id=c.document_id where d.status='ready';")
    db_chunks_ready_public = sql("select count(*) from document_chunks c join documents d on d.id=c.document_id where d.status='ready' and d.permission_tags='{}';")
    print(f"   SQL 全量：documents={db_docs_all}  public={db_docs_public}")
    print(f"   SQL 切片：ready 全量={db_chunks_ready}  ready 且 public={db_chunks_ready_public}")

    st_admin = c.get("/api/documents", headers=HA, params={"page_size": 1}).json()["total"]
    st_alice = c.get("/api/documents", headers=HL, params={"page_size": 1}).json()["total"] if HL else None
    print(f"   REST 列表 total：admin={st_admin}  alice={st_alice}")
    ok("REST 列表口径与 SQL 一致") if (str(st_admin) == db_docs_all and (st_alice is None or str(st_alice) == db_docs_public)) else warn(
        "REST 列表口径与 SQL 不一致", f"admin={st_admin} vs SQL {db_docs_all}；alice={st_alice} vs SQL {db_docs_public}"
    )

    # ------------------------------------------------ 清理
    print("\n=== 清理：删除 MCP 冒烟上传的文档 ===")
    r = c.get("/api/documents", headers=HA, params={"page_size": 100})
    hits = [i for i in r.json()["items"] if "smoke" in i["name"].lower()]
    if not hits:
        ok("没有残留的冒烟文档")
    for h in hits:
        rr = c.delete(f"/api/documents/{h['id']}", headers=HA)
        ok(f"删除 {h['name']}", f"HTTP {rr.status_code}") if rr.status_code in (200, 204) else bad(
            f"删除 {h['name']} 失败", f"HTTP {rr.status_code}"
        )
    # 清理冒烟会话
    r = c.get("/api/conversations", headers=HA, params={"page_size": 100})
    for conv in r.json().get("items", []):
        if str(conv.get("title", "")).startswith("冒烟"):
            rr = c.delete(f"/api/conversations/{conv['id']}", headers=HA)
            print(f"   删除会话 {conv['title']} -> HTTP {rr.status_code}")

    print("\n" + "=" * 70)
    print(f"第 4 段结果：OK={len(PASS)}  WARN={len(WARN)}  FAIL={len(FAIL)}")
    for n, d in FAIL:
        print(f"  [FAIL] {n}  {d}")
    for n, d in WARN:
        print(f"  [WARN] {n}  {d}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
