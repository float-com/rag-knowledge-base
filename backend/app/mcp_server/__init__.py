"""MCP Server 集成层（第 13 期）。

本包是【对外协议层】：把已有的 app/services 与 app/workflows 包装成
MCP 工具，暴露给 Cursor / Claude Desktop 这类外部 Agent。

⚠️ 定位（别写歪了）：
    app/mcp_server/* 里**不放业务逻辑**，只做三件事 ——
        ① 协议声明（工具名 / 描述 / 参数 schema）
        ② 鉴权解析（auth.py：Bearer 令牌 → User）
        ③ 参数转译 + 调已有 service

    一旦这里出现了"第二套检索实现"或"第二套权限判断"，
    网页侧修好的 bug 就不会自动修到 MCP 侧 —— 这是明确要避免的。

目录（随第 13 期各章推进逐步补齐）：
    auth.py  第 2 章：Bearer 令牌 → 当前用户 → admin 闸门
"""
