"""
mcp —— 把图谱的 22 条预置查询暴露成 MCP server，供 AWS DevOps Agent 使用。

模块划分：
  catalog_tools.py  QUERY_CATALOG → MCP 工具定义（含参数校验）
  provenance.py     响应出处富化 + 证据纪律（进 initialize.instructions）
  server.py         JSON-RPC 2.0 分派，与传输层解耦、可单测
  handler.py        Lambda 传输层（Function URL / API GW v2）+ api-key 鉴权
  local_server.py   本地 stdio / HTTP 调试入口
"""
