"""冒烟测试第 2 段：写链路（上传→Celery→ready）+ 权限过滤真对照。

会真实消耗 embedding 额度（一个极小文件，切片数 1~2）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[5]  # 归档后位于 项目阶段性总结/Day13.../001_冒烟测试脚本/，项目根在第 5 层
TAG = "smoke_secret"
DOC_NAME = "smoke_permission_probe.md"

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


def login(c, user, pwd):
    r = c.post("/api/auth/login", json={"username": user, "password": pwd})
    return (r.json()["access_token"], r.json()) if r.status_code == 200 else (None, r)


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=120.0)
    admin_t, admin_body = login(c, env("DEFAULT_ADMIN_USERNAME", "admin"), env("DEFAULT_ADMIN_PASSWORD"))
    if not admin_t:
        bad("管理员登录", str(admin_body)); return 1
    HA = {"Authorization": f"Bearer {admin_t}"}
    print(f"  管理员令牌就绪；permission_tags={admin_body.get('permission_tags')} is_admin={admin_body.get('is_admin')}")

    # ---------------------------------------------------- 1. alice 登录（普通用户）
    print("\n=== 1. 普通用户（alice）登录 ===")
    alice_t = None
    for pwd in ("AliceSmoke!2026", env("ALICE_PASSWORD"), "alice", "alice123"):
        if not pwd:
            continue
        t, body = login(c, "alice", pwd)
        if t:
            alice_t = t
            ok("alice 登录成功", f"is_admin={body.get('is_admin')} permission_tags={body.get('permission_tags')}")
            break
    if not alice_t:
        warn("alice 登录失败（未在 .env 找到口令）", "权限对照将跳过；可临时在库里给 alice 设一个口令后重跑")
    HL = {"Authorization": f"Bearer {alice_t}"} if alice_t else None

    # ---------------------------------------------------- 2. 上传一份带标签的文档
    print("\n=== 2. 上传链路：小文件 + permission_tags=['smoke_secret'] ===")
    content = (
        "# 冒烟测试文档\n\n"
        "这是一份用于验证权限过滤的临时文档，内容包含唯一标记 SMOKE-PROBE-2026。\n"
    ).encode("utf-8")
    files = {"file": (DOC_NAME, content, "text/markdown")}
    data = {"permission_tags": TAG}
    r = c.post("/api/documents", headers=HA, files=files, data=data)
    if r.status_code not in (200, 201):
        bad("上传文档", f"HTTP {r.status_code} {r.text[:250]}")
        return 1
    doc = r.json()
    did = doc["id"]
    ok("上传成功", f"id={did} status={doc.get('status')} tags={doc.get('permission_tags')}")

    # ---------------------------------------------------- 3. 轮询到 ready（Celery 全链路）
    print("\n=== 3. Celery 落库：轮询 get_document_status 直到 ready ===")
    deadline = time.time() + 180
    final = None
    last = ""
    while time.time() < deadline:
        r = c.get(f"/api/documents/{did}", headers=HA)
        if r.status_code == 200:
            d = r.json()
            last = f"status={d.get('status')} version={d.get('version')} latest_task={(d.get('latest_task') or {}).get('status')}"
            if d.get("status") in ("ready", "failed"):
                final = d
                break
        time.sleep(3)
    if final is None:
        bad("轮询超时（180s 未到终态）", last)
    elif final["status"] == "ready":
        lt = final.get("latest_task") or {}
        ok("文档进入 ready", f"{last} progress={lt.get('progress_done')}/{lt.get('progress_total')}")
    else:
        bad("文档 failed", f"{last} error={final.get('error_message')}")

    # ---------------------------------------------------- 4. 权限过滤真对照
    print("\n=== 4. ★ 权限过滤对照（admin 看得见 / 普通用户看不见）===")
    r = c.get("/api/documents", headers=HA, params={"page_size": 100})
    admin_sees = any(i["id"] == did for i in r.json().get("items", []))
    ok("admin 列表包含该文档", f"total={r.json().get('total')}") if admin_sees else bad("admin 列表竟不包含该文档")

    r = c.get(f"/api/documents/{did}", headers=HA)
    ok("admin 详情可见", f"HTTP {r.status_code}") if r.status_code == 200 else bad("admin 详情不可见", f"HTTP {r.status_code}")

    if HL:
        r = c.get("/api/documents", headers=HL, params={"page_size": 100})
        if r.status_code == 200:
            items = r.json().get("items", [])
            leak = [i["name"] for i in items if i["id"] == did]
            if leak:
                bad("★ 越权：普通用户列表看到了受限文档", f"total={r.json().get('total')}")
            else:
                ok("普通用户列表看不到受限文档", f"total={r.json().get('total')}（admin 视角 {21 + 1} 附近）")
        r = c.get(f"/api/documents/{did}", headers=HL)
        if r.status_code == 404:
            ok("普通用户取详情 -> 404（不区分不存在与无权）")
        elif r.status_code == 200:
            bad("★ 越权：普通用户能读受限文档详情")
        else:
            warn("普通用户取详情返回非 404", f"HTTP {r.status_code}")

        # 切片接口同样是越权面
        r = c.get(f"/api/documents/{did}/chunks", headers=HL)
        if r.status_code == 404:
            ok("普通用户取切片 -> 404")
        elif r.status_code == 200:
            bad("★ 越权：普通用户能读受限文档切片")
        else:
            warn("普通用户取切片返回非 404", f"HTTP {r.status_code}")

        # 管理面闸门
        for path in ("/api/users", "/api/roles"):
            r = c.get(path, headers=HL)
            ok(f"普通用户访问 {path} -> 403") if r.status_code == 403 else bad(
                f"普通用户访问 {path} 未 403", f"HTTP {r.status_code}"
            )

    # ---------------------------------------------------- 5. 恢复标签 + 清理
    print("\n=== 5. 恢复标签并清理临时文档 ===")
    r = c.patch(f"/api/documents/{did}/permission-tags", headers=HA, json={"permission_tags": []})
    if r.status_code == 200:
        ok("放宽为公开", f"tags={r.json().get('permission_tags')}")
    else:
        warn("放宽标签失败", f"HTTP {r.status_code} {r.text[:150]}")
    if HL:
        r = c.get(f"/api/documents/{did}", headers=HL)
        ok("普通用户现在可见（404->200 的反向验证）") if r.status_code == 200 else bad(
            "放宽后普通用户仍不可见", f"HTTP {r.status_code}"
        )

    r = c.delete(f"/api/documents/{did}", headers=HA)
    if r.status_code in (200, 204):
        ok("临时文档已删除", f"HTTP {r.status_code}")
    else:
        bad("临时文档删除失败", f"HTTP {r.status_code} {r.text[:150]}")

    print("\n" + "=" * 70)
    print(f"第 2 段结果：OK={len(PASS)}  WARN={len(WARN)}  FAIL={len(FAIL)}")
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
