"""第 5 段：针对本轮 5 项修复的验证。

不重启被测服务；MCP 限流用"临时降低阈值"的方式跑真实实现（promote 到指定用户身份）。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
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


def check_config_gate():
    print("\n=== 修复 2：JWT 生产环境硬失败 ===")
    import importlib
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.config import Settings
    from app.core.exceptions import ConfigurationError

    cases = [
        ("development", "", False, "开发+空密钥"),
        ("development", "123456", False, "开发+弱密钥"),
        ("production", "", True, "生产+空密钥"),
        ("production", "123456", True, "生产+弱密钥"),
        ("production", "x" * 32, False, "生产+合规密钥"),
        ("PROD  ", "x" * 40, False, "生产别名(大写带空格)"),
    ]
    for e, s, should_fail, label in cases:
        try:
            Settings(environment=e, jwt_secret=s).warn_if_jwt_unconfigured()
            got = False
        except ConfigurationError:
            got = True
        if got == should_fail:
            ok(f"{label} -> {'拒绝' if got else '放行'}")
        else:
            bad(f"{label} 行为不符", f"期望 {'拒绝' if should_fail else '放行'}，实际 {'拒绝' if got else '放行'}")


def check_rate_limit_module():
    print("\n=== 修复 4：MCP 限流（真实实现 + 阈值打桩）===")
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.config import settings
    from app.core.exceptions import RateLimitError
    from app.core.redis import get_redis
    from app.mcp_server.limits import enforce_tool_limit
    from app.db.models import User

    class FakeUser:
        def __init__(self):
            self.id = uuid.uuid4()

    original_limit = settings.rate_limit_per_minute
    try:
        object.__setattr__(settings, "rate_limit_per_minute", 3)
        u = FakeUser()

        async def run_all():
            """★ 必须把全部异步步骤放进【同一个事件循环】。

            踩过的坑：限流器内部持有 `get_rate_limiter()` 这个 lru_cache 单例，
            而它的 Redis 客户端绑定在创建时的事件循环上。
            测试脚本若分多次 asyncio.run()，第二次会撞
            `RuntimeError: Event loop is closed` —— 这与被测代码无关，是脚本写法问题。
            """
            results = []
            for _ in range(4):
                try:
                    await enforce_tool_limit(u, scope="ask")
                    results.append("pass")
                except RateLimitError as e:
                    results.append(f"blocked:{e.message}")

            # 独立分桶验证：upload 不应继承 ask 的 3 次计数
            try:
                await enforce_tool_limit(u, scope="upload")
                upload_result = "pass"
            except RateLimitError:
                upload_result = "blocked"

            r = get_redis()
            keys = [k for k in await r.keys("rag:rate_limit:mcp-*")]
            # 清理测试 key
            for k in keys:
                await r.delete(k)
            return results, upload_result, keys

        results, upload_result, keys = asyncio.run(run_all())

        if results[:3] == ["pass"] * 3 and results[3].startswith("blocked"):
            ok("阈值=3 时前 3 次放行、第 4 次被限", results[3])
        else:
            bad("限流行为不符", str(results))

        ask_keys = [k for k in keys if "mcp-ask:" in k]
        if ask_keys:
            ok("限流 key 命名正确（mcp 前缀 + 分桶）", str(ask_keys[0]))
        else:
            bad("未找到 mcp-ask 分桶的限流 key")
        ok("与 REST 侧 key 空间隔离（mcp- 前缀）") if all("rag:rate_limit:mcp-" in k for k in keys) else bad(
            "key 前缀不对"
        )
        ok("upload 分桶独立计数（未被 ask 的 3 次影响）") if upload_result == "pass" else bad(
            "分桶未隔离", upload_result
        )
        ok("测试用限流 key 已清理")
    finally:
        object.__setattr__(settings, "rate_limit_per_minute", original_limit)


def check_live_service():
    print("\n=== 修复后服务回归（REST + MCP 真实调用）===")
    c = httpx.Client(base_url=BASE, timeout=60.0)
    for p in ("/api/health", "/api/health/db", "/api/health/cos"):
        r = c.get(p)
        ok(f"GET {p}") if r.status_code == 200 else bad(f"GET {p}", f"HTTP {r.status_code}")
    spec = c.get("/openapi.json").json()
    paths = len(spec["paths"])
    ops = sum(len([k for k in v if k in ("get", "post", "put", "patch", "delete")]) for v in spec["paths"].values())
    ok("契约未变 paths=29 / operations=40") if (paths, ops) == (29, 40) else bad("契约变化", f"{paths}/{ops}")

    tok = c.post("/api/auth/login", json={
        "username": env("DEFAULT_ADMIN_USERNAME", "admin"),
        "password": env("DEFAULT_ADMIN_PASSWORD"),
    }).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}

    async def mcp_round():
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(f"{BASE}/mcp", headers=H) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = (await s.list_tools()).tools
                res = await s.call_tool("list_documents", {"page": 1, "page_size": 2})
                stats = await s.call_tool("get_knowledge_base_stats", {})
                return len(tools), res.isError, stats.isError

    n, e1, e2 = asyncio.run(mcp_round())
    ok("MCP 工具数仍为 5") if n == 5 else bad("MCP 工具数异常", str(n))
    ok("只读工具不受限流影响（list_documents / stats 正常）") if not e1 and not e2 else bad(
        "只读工具调用异常", f"list.isError={e1} stats.isError={e2}"
    )


def check_prod_compose():
    print("\n=== 修复 1：生产编排的端口与关键项 ===")
    import subprocess
    probe = ROOT / ".env.prod.probe"
    probe.write_text(
        "POSTGRES_PASSWORD=probe-pwd\n"
        "JWT_SECRET=" + "a" * 40 + "\n"
        "DEFAULT_ADMIN_PASSWORD=probe-admin\n"
        "CORS_ORIGINS=https://kb.probe.local\n",
        encoding="ascii",
    )
    try:
        out = subprocess.run(
            ["docker", "compose", "--env-file", str(probe), "-f", str(ROOT / "docker-compose.prod.yml"), "config"],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        )
        text = out.stdout
        if out.returncode != 0:
            bad("prod compose 渲染失败", out.stderr[:200]); return
        ok("prod compose 渲染成功")
        if "published: \"5432\"" in text or "published: \"6379\"" in text:
            bad("★ 5432/6379 仍发布到宿主机")
        else:
            ok("5432/6379 未发布到宿主机（只 expose）")
        if "postgresql+asyncpg://rag:probe-pwd@postgres:5432/rag_kb" in text:
            ok("连接串走服务名 postgres")
        else:
            bad("连接串未走服务名")
        if "ENVIRONMENT: production" in text:
            ok("api/worker 注入了 ENVIRONMENT=production")
        else:
            bad("api/worker 未注入 production 标记")
        if "rag_kb_models" in text:
            ok("模型用命名卷持久化")
        else:
            bad("模型卷缺失")
    finally:
        probe.unlink(missing_ok=True)


def check_env_examples():
    print("\n=== 修复 3：默认口令不再可直接登录 ===")
    ex = (ROOT / ".env.example").read_text(encoding="utf-8", errors="ignore")
    line = [l for l in ex.splitlines() if l.startswith("DEFAULT_ADMIN_PASSWORD=")]
    if line and line[0].strip() != "DEFAULT_ADMIN_PASSWORD=admin":
        ok(".env.example 的默认口令已改成占位符", line[0].strip())
    else:
        bad("★ .env.example 仍是 admin", str(line))
    if (ROOT / ".env.prod.example").exists():
        ok(".env.prod.example 已提供（含占位说明）")
    else:
        bad(".env.prod.example 缺失")
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore")
    if "!.env.prod.example" in gi and ".env.*" in gi:
        ok(".gitignore：.env.prod 被忽略、模板被放行")
    else:
        bad(".gitignore 规则缺失", "需要 .env.* 忽略 + !.env.prod.example 放行")


def main():
    check_config_gate()
    check_rate_limit_module()
    check_live_service()
    check_prod_compose()
    check_env_examples()
    print("\n" + "=" * 70)
    print(f"第 5 段结果：OK={len(PASS)}  WARN={len(WARN)}  FAIL={len(FAIL)}")
    for n, d in FAIL:
        print(f"  [FAIL] {n}  {d}")
    for n, d in WARN:
        print(f"  [WARN] {n}  {d}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
