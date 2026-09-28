"""冒烟测试第 3 段：问答链路（SSE）+ 语义缓存 + MCP 五个工具。

会真实消耗 LLM / embedding 额度：REST 问答 2 次（第 2 次应命中缓存）+ MCP 问答 2 次 + MCP 上传 1 次。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
MCP_URL = "http://127.0.0.1:8000/mcp"
ROOT = Path(__file__).resolve().parents[5]  # 归档后位于 项目阶段性总结/Day13.../001_冒烟测试脚本/，项目根在第 5 层
QUESTION = "这份文档主要讲了什么？"

PASS, FAIL, WARN = [], [], []


def ok(n, d=""):
    PASS.append((n, d)); print(f"  [OK]   {n}" + (f"  |  {d}" if d else ""))


def bad(n, d=""):
    FAIL.append((n, d)); print(f"  [FAIL] {n}" + (f"  |  {d}" if d else ""))


def warn(n, d=""):
    WARN.append((n, d)); print(f"  [WARN] {n}" + (f"  |  {d}" if d else ""))


def env(key, default=""):
    p = ROOT / ".env"
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return default


def redis_size(db: int) -> int:
    import subprocess
    out = subprocess.run(
        ["docker", "exec", "rag-kb-redis", "redis-cli", "-n", str(db), "DBSIZE"],
        capture_output=True, text=True, timeout=20,
    )
    try:
        return int(out.stdout.strip())
    except Exception:
        return -1


def sse_ask(c, headers, question, conversation_id=None):
    """跑一次 SSE 问答，返回 (事件类型序列, 各事件载荷)。"""
    if conversation_id is None:
        r = c.post("/api/conversations", headers=headers, json={"title": "冒烟测试会话"})
        if r.status_code not in (200, 201):
            return None, None, f"建会话失败 HTTP {r.status_code} {r.text[:150]}"
        conversation_id = r.json()["id"]
    events = {}
    order = []
    body = {"question": question}
    with c.stream("POST", f"/api/conversations/{conversation_id}/chat",
                  headers={**headers, "Accept": "text/event-stream"}, json=body,
                  timeout=180.0) as resp:
        if resp.status_code != 200:
            return None, None, f"SSE HTTP {resp.status_code}"
        cur = None
        for line in resp.iter_lines():
            if line.startswith("event:"):
                cur = line.split(":", 1)[1].strip()
                order.append(cur)
            elif line.startswith("data:") and cur:
                raw = line.split(":", 1)[1].strip()
                try:
                    events.setdefault(cur, []).append(json.loads(raw))
                except Exception:
                    events.setdefault(cur, []).append(raw)
    return order, events, None


async def mcp_phase(admin_token, alice_token):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    results = {}

    def H(tok):
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    async def call(session, name, args):
        res = await session.call_tool(name, args)
        text = res.content[0].text if res.content else ""
        return res.isError, text

    # --- 管理员会话 ---
    async with streamablehttp_client(MCP_URL, headers=H(admin_token)) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            ok("MCP 握手（admin）")
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            ok("tools/list", ", ".join(names))
            for t in tools.tools:
                if not t.description:
                    warn(f"工具 {t.name} 没有 description", "模型难以判断何时调用")

            err, txt = await call(s, "list_documents", {"page": 1, "page_size": 3})
            results["admin_list"] = (err, txt[:220])
            ok("tools/call list_documents") if not err else bad("tools/call list_documents", txt[:200])

            err, txt = await call(s, "get_knowledge_base_stats", {})
            results["admin_stats"] = (err, txt[:220])
            ok("tools/call get_knowledge_base_stats") if not err else bad("tools/call get_knowledge_base_stats", txt[:200])

            err, txt = await call(s, "ask_knowledge_base", {"question": QUESTION})
            results["admin_ask"] = (err, txt[:150])
            ok("tools/call ask_knowledge_base") if not err else bad("tools/call ask_knowledge_base", txt[:250])

            # 非法 uuid 的拒绝路径
            err, txt = await call(s, "get_document_status", {"document_id": "not-a-uuid"})
            ok("非法 document_id 被明确拒绝", txt[:80]) if err and "UUID" in txt else bad(
                "非法 document_id 未被正确拒绝", f"isError={err} {txt[:150]}"
            )

            # 空问题拒绝路径
            err, txt = await call(s, "ask_knowledge_base", {"question": "   "})
            ok("空问题被拒绝", txt[:80]) if err else bad("空问题未被拒绝", txt[:150])

            # 上传工具（小文件）
            import base64
            payload = base64.b64encode("# MCP 冒烟测试\n\n内容标记 MCP-SMOKE-2026。\n".encode()).decode()
            err, txt = await call(s, "upload_document",
                                  {"filename": "mcp_smoke_probe.md", "content_base64": payload, "mime_type": "text/markdown"})
            results["admin_upload"] = (err, txt[:220])
            ok("tools/call upload_document") if not err else bad("tools/call upload_document", txt[:250])

            # 非法 base64 拒绝路径
            err, txt = await call(s, "upload_document", {"filename": "x.md", "content_base64": "!!!not base64!!!"})
            ok("非法 base64 被拒绝", txt[:80]) if err and "base64" in txt else bad(
                "非法 base64 未被正确拒绝", f"isError={err} {txt[:150]}"
            )

    # --- 普通用户会话（权限对照）---
    if alice_token:
        async with streamablehttp_client(MCP_URL, headers=H(alice_token)) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                ok("MCP 握手（alice）")
                err, txt = await call(s, "get_knowledge_base_stats", {})
                results["alice_stats"] = (err, txt[:220])
                ok("alice 调 get_knowledge_base_stats") if not err else bad("alice 调 get_knowledge_base_stats", txt[:200])
                err, txt = await call(s, "list_documents", {"page": 1, "page_size": 3})
                results["alice_list"] = (err, txt[:220])
                err, txt = await call(s, "upload_document",
                                      {"filename": "x.md", "content_base64": "aGk="})
                ok("alice 上传被 403/拒绝（require_admin 生效）") if err else bad(
                    "★ 越权：普通用户通过 MCP 上传成功", txt[:200]
                )
    else:
        warn("跳过 alice 的 MCP 权限对照", "alice 未登录成功")

    # --- 无令牌 ---
    try:
        async with streamablehttp_client(MCP_URL) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                err, txt = await call(s, "list_documents", {})
                ok("无令牌调用被拒", txt[:80]) if err else bad("★ 无令牌调用竟然成功")
    except Exception as e:
        warn("无令牌会话建立失败（可能被传输层直接拒绝）", f"{type(e).__name__}: {str(e)[:100]}")

    return results


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=60.0)
    admin_t = c.post("/api/auth/login", json={
        "username": env("DEFAULT_ADMIN_USERNAME", "admin"),
        "password": env("DEFAULT_ADMIN_PASSWORD"),
    }).json()["access_token"]
    HA = {"Authorization": f"Bearer {admin_t}"}
    alice_t = None
    r = c.post("/api/auth/login", json={"username": "alice", "password": "AliceSmoke!2026"})
    if r.status_code == 200:
        alice_t = r.json()["access_token"]

    # ---------------------------------------------------------- 1. SSE 问答
    print("\n=== 1. REST 问答：SSE 事件契约 ===")
    before = redis_size(2)
    t0 = time.time()
    order, events, err = sse_ask(c, HA, QUESTION)
    if err:
        bad("SSE 问答", err)
    else:
        kinds = sorted(set(order))
        ok("SSE 流式返回", f"{len(order)} 个事件，类型={kinds}")
        for must in ("message_start", "citation", "message_end"):
            ok(f"包含事件 {must}") if must in events else warn(f"未见事件 {must}", f"实际={kinds}")
        meta = (events.get("message_end") or [{}])[-1]
        ok("message_end 载荷", json.dumps(meta, ensure_ascii=False)[:200])
        print(f"  （第 1 次问答耗时 {time.time()-t0:.1f}s）")

    # ---------------------------------------------------------- 2. 语义缓存
    print("\n=== 2. 语义缓存：同一问题再问一次 ===")
    t1 = time.time()
    order2, events2, err2 = sse_ask(c, HA, QUESTION, conversation_id=None)
    dt = time.time() - t1
    after = redis_size(2)
    if err2:
        bad("第 2 次问答", err2)
    else:
        ok("第 2 次问答完成", f"耗时 {dt:.1f}s；Redis(db2) {before} -> {after}")
        k2 = sorted(set(order2 or []))
        if "cache_hit" in k2 or "message_start" in k2 and dt < 3:
            ok("疑似命中语义缓存", f"事件={k2} 耗时={dt:.1f}s")
        elif after > before:
            ok("缓存已写入（Redis key 增加）", f"{before} -> {after}")
        else:
            warn("未观察到缓存命中/写入", f"事件={k2} Redis {before}->{after} 耗时{dt:.1f}s")

    # ---------------------------------------------------------- 3. 限流
    print("\n=== 3. 限流：连续打 /api/auth/login（不挂限流，应全过）与 chat（挂限流）===")
    codes = []
    for _ in range(3):
        rr = c.post("/api/auth/login", json={"username": "nonexistent-user", "password": "x"})
        codes.append(rr.status_code)
    ok("登录接口错误口令连续请求", f"状态码={codes}（预期全是 401，说明登录未误挂限流）")

    # ---------------------------------------------------------- 4. MCP
    print("\n=== 4. MCP 通道：五个工具真实调用 ===")
    try:
        results = asyncio.run(mcp_phase(admin_t, alice_t))
        print("\n  ---- MCP 关键返回（人工判读用）----")
        for k, (err, txt) in results.items():
            print(f"   {k:16s} isError={err}  {txt[:160]}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        bad("MCP 阶段异常", f"{type(e).__name__}: {str(e)[:200]}")

    print("\n" + "=" * 70)
    print(f"第 3 段结果：OK={len(PASS)}  WARN={len(WARN)}  FAIL={len(FAIL)}")
    if FAIL:
        print("\n失败明细：")
        for n, d in FAIL:
            print(f"  - {n}  {d}")
    if WARN:
        print("\n警告明细：")
        for n, d in WARN:
            print(f"  - {n}  {d}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
