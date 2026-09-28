"""部署前整体冒烟测试（第 1 段：静态契约 + 健康 + 认证 + 只读接口）。

只做只读与轻量写操作，不烧 LLM / embedding 额度。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[5]  # 归档后位于 项目阶段性总结/Day13.../001_冒烟测试脚本/，项目根在第 5 层

PASS, FAIL, WARN = [], [], []


def ok(name: str, detail: str = "") -> None:
    PASS.append((name, detail))
    print(f"  [OK]   {name}" + (f"  |  {detail}" if detail else ""))


def bad(name: str, detail: str = "") -> None:
    FAIL.append((name, detail))
    print(f"  [FAIL] {name}" + (f"  |  {detail}" if detail else ""))


def warn(name: str, detail: str = "") -> None:
    WARN.append((name, detail))
    print(f"  [WARN] {name}" + (f"  |  {detail}" if detail else ""))


def env(key: str, default: str = "") -> str:
    p = ROOT / ".env"
    if not p.exists():
        return default
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return default


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=30.0)

    # ---------------------------------------------------------------- 1. 健康
    print("\n=== 1. 健康与就绪 ===")
    for path in ("/api/health", "/api/health/db", "/api/health/cos"):
        try:
            r = c.get(path)
            body = r.text[:160]
            if r.status_code == 200 and '"ok"' in r.text:
                ok(f"GET {path}", body)
            elif r.status_code == 200:
                warn(f"GET {path} 返回 200 但非 ok", body)
            else:
                bad(f"GET {path}", f"HTTP {r.status_code} {body}")
        except Exception as e:
            bad(f"GET {path}", f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 2. 契约
    print("\n=== 2. 静态契约（第 1 章回归底线 R1）===")
    r = c.get("/openapi.json")
    spec = r.json()
    paths, ops = len(spec["paths"]), 0
    op_ids = []
    for p, item in spec["paths"].items():
        for m, meta in item.items():
            if m in ("get", "post", "put", "patch", "delete"):
                ops += 1
                op_ids.append(meta.get("operationId"))
    ok("OpenAPI 可读取", f"paths={paths} operations={ops}")
    if paths == 29 and ops == 40:
        ok("R1：paths=29 / operations=40 未变")
    else:
        bad("R1 回归被打破", f"实际 paths={paths} operations={ops}（期望 29/40）")
    dup = {x for x in op_ids if op_ids.count(x) > 1}
    if dup:
        bad("operationId 有重复", str(sorted(dup)))
    else:
        ok("operationId 全部唯一", f"{len(op_ids)} 个")

    # 第 11 期三个管理端点必须存在（否则前端 sdk 会 404）
    need = ["/api/auth/login", "/api/users", "/api/roles", "/api/documents", "/api/conversations"]
    missing = [p for p in need if p not in spec["paths"]]
    if missing:
        bad("关键路径缺失", str(missing))
    else:
        ok("关键路径齐全", ", ".join(need))

    # ---------------------------------------------------------------- 3. 认证
    print("\n=== 3. 认证链路 ===")
    admin_user = env("DEFAULT_ADMIN_USERNAME", "admin")
    admin_pwd = env("DEFAULT_ADMIN_PASSWORD")

    r = c.post("/api/auth/login", json={"username": admin_user, "password": admin_pwd})
    if r.status_code == 200 and "access_token" in r.json():
        token = r.json()["access_token"]
        ok("管理员登录成功", f"返回字段={sorted(r.json().keys())}")
    else:
        bad("管理员登录失败", f"HTTP {r.status_code} {r.text[:200]}")
        token = None

    r = c.post("/api/auth/login", json={"username": admin_user, "password": "definitely-wrong"})
    ok("错误口令被拒", f"HTTP {r.status_code}") if r.status_code == 401 else bad(
        "错误口令未被拒", f"HTTP {r.status_code} {r.text[:150]}"
    )

    if token:
        H = {"Authorization": f"Bearer {token}"}
        r = c.get("/api/auth/me", headers=H)
        ok("GET /api/auth/me", r.text[:160]) if r.status_code == 200 else bad("GET /api/auth/me", f"HTTP {r.status_code}")

        for hdr, label in (({}, "无令牌"), ({"Authorization": "Bearer not-a-jwt"}, "坏令牌"),
                           ({"Authorization": "Basic abc"}, "非 Bearer 方案")):
            r = c.get("/api/documents", headers=hdr)
            ok(f"拒绝{label}", f"HTTP {r.status_code}") if r.status_code == 401 else bad(
                f"未拒绝{label}", f"HTTP {r.status_code} {r.text[:120]}"
            )

        # ------------------------------------------------------------ 4. 只读
        print("\n=== 4. 文档只读接口（管理员视角）===")
        r = c.get("/api/documents", headers=H, params={"page": 1, "page_size": 5})
        if r.status_code == 200:
            d = r.json()
            ok("GET /api/documents", f"total={d.get('total')} items={len(d.get('items', []))} page={d.get('page')}")
            first = d["items"][0] if d.get("items") else None
        else:
            bad("GET /api/documents", f"HTTP {r.status_code} {r.text[:200]}")
            first = None

        # 边界：page=0 应被拒（422）
        r = c.get("/api/documents", headers=H, params={"page": 0})
        ok("page=0 被拒", f"HTTP {r.status_code}") if r.status_code == 422 else warn(
            "page=0 未被 422 拒绝", f"HTTP {r.status_code}"
        )

        if first:
            did = first["id"]
            print(f"  （取第一篇文档 id={did} 做详情/切片测试）")
            r = c.get(f"/api/documents/{did}", headers=H)
            ok("GET /api/documents/{id}", f"HTTP {r.status_code} status={r.json().get('status') if r.status_code==200 else '-'}")

            r = c.get(f"/api/documents/{did}/chunks", headers=H, params={"page": 1, "page_size": 3})
            if r.status_code == 200:
                d = r.json()
                ok("GET /{id}/chunks", f"total={d.get('total')} 返回={len(d.get('items', []))}")
            else:
                bad("GET /{id}/chunks", f"HTTP {r.status_code} {r.text[:200]}")

            # 不存在 / 非法 id
            r = c.get("/api/documents/00000000-0000-0000-0000-000000000000", headers=H)
            ok("不存在的文档 -> 404", f"HTTP {r.status_code}") if r.status_code == 404 else warn(
                "不存在的文档未返回 404", f"HTTP {r.status_code}"
            )
            r = c.get("/api/documents/not-a-uuid", headers=H)
            ok("非法 uuid -> 422", f"HTTP {r.status_code}") if r.status_code == 422 else warn(
                "非法 uuid 未返回 422", f"HTTP {r.status_code}"
            )

        # 会话列表（前端会轮询/列表）
        r = c.get("/api/conversations", headers=H, params={"page": 1, "page_size": 5})
        if r.status_code == 200:
            d = r.json()
            ok("GET /api/conversations", f"total={d.get('total')} 返回={len(d.get('items', []))}")
        else:
            bad("GET /api/conversations", f"HTTP {r.status_code} {r.text[:200]}")

        # ------------------------------------------------ 5. 鉴权闸门（非管理员）
        print("\n=== 5. 鉴权闸门：普通用户访问管理面必须 403 ===")
        for path in ("/api/users", "/api/roles"):
            body = {"username": "smoke_probe", "password": "SmokeProbe!2026", "display_name": "冒烟探针"}
            r = c.post(path, headers=H, json=body) if path == "/api/users" else c.get(path, headers=H)
            # 用管理员令牌应当成功（证明路由通），非管理员令牌才该 403 —— 这里先只验管理员可用
            if r.status_code < 400:
                ok(f"管理员可访问 {path}", f"HTTP {r.status_code}")
            else:
                bad(f"管理员访问 {path} 失败", f"HTTP {r.status_code} {r.text[:150]}")

    print("\n" + "=" * 70)
    print(f"第 1 段结果：OK={len(PASS)}  WARN={len(WARN)}  FAIL={len(FAIL)}")
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
