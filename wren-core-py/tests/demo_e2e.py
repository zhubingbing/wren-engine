"""
端到端演示：语义 SQL → ibis-server → ClickHouse 真实数据

流程:
  1. 定义 MDL manifest（语义层）
  2. 构造 HTTP 请求发到 ibis-server /v3/connector/clickhouse/query
  3. ibis-server 内部：wren-core 将语义 SQL 转换成物理 SQL，再由 Ibis 执行到 ClickHouse
  4. 返回结果

运行方式:
  cd wren-core-py
  poetry run python tests/demo_e2e.py
  # 或直接用 venv
  .venv/bin/python tests/demo_e2e.py

依赖: ibis-server 已在 http://127.0.0.1:8100 启动
  QUERY_CACHE_STORAGE_TYPE=local ../wren-core-py/.venv/bin/python -m fastapi dev --port 8100
"""

import base64
import json

import requests

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────
IBIS_SERVER = "http://127.0.0.1:8100"

CONNECTION_INFO = {
    "host": "localhost",
    "port": 8123,          # ClickHouse HTTP port（不是 native 9000）
    "database": "k8s_node_metrics",
    "user": "admin",
    "password": "admin",
}

# ─────────────────────────────────────────────────────────────────────────────
# MDL manifest（语义层定义）
# ─────────────────────────────────────────────────────────────────────────────
MANIFEST = {
    "catalog": "k8s_monitor",
    "schema": "k8s_node_metrics",
    "dataSource": "clickhouse",
    "models": [
        {
            "name": "host_metrics",
            "tableReference": {
                "schema": "k8s_node_metrics",
                "table": "ods_host_metrics_raw",
            },
            "columns": [
                {"name": "event_time", "type": "timestamp"},
                {"name": "ds", "type": "date"},
                {"name": "instance", "type": "varchar"},
                {"name": "hostname", "type": "varchar"},
                {"name": "host_ip", "type": "varchar"},
                {"name": "cluster", "type": "varchar"},
                {"name": "metric_name", "type": "varchar"},
                {"name": "metric_value", "type": "double"},
                # 计算列：EAV → 具名指标列
                {
                    "name": "cpu_usage",
                    "type": "double",
                    "isCalculated": True,
                    "expression": "CASE WHEN metric_name = 'cpu_usage' THEN metric_value END",
                },
                {
                    "name": "memory_usage_percent",
                    "type": "double",
                    "isCalculated": True,
                    "expression": "CASE WHEN metric_name = 'memory_usage_percent' THEN metric_value END",
                },
                {
                    "name": "memory_used_bytes",
                    "type": "double",
                    "isCalculated": True,
                    "expression": "CASE WHEN metric_name = 'memory_used_bytes' THEN metric_value END",
                },
            ],
            "primaryKey": "",
        },
        {
            "name": "cmdb_host",
            "tableReference": {
                "schema": "k8s_node_metrics",
                "table": "ods_cmdb_host_snapshot",
            },
            "columns": [
                {"name": "host_ip", "type": "varchar"},
                {"name": "idc", "type": "varchar"},
                {"name": "cabinet", "type": "varchar"},
                {"name": "supplier", "type": "varchar"},
                {"name": "is_deleted", "type": "integer"},
                {"name": "ds", "type": "date"},
                {
                    "name": "active_idc",
                    "type": "varchar",
                    "isCalculated": True,
                    "expression": "CASE WHEN is_deleted = 0 THEN idc END",
                },
                {
                    "name": "host_metrics",
                    "type": "host_metrics",
                    "relationship": "cmdb_to_metrics",
                },
            ],
            "primaryKey": "host_ip",
        },
    ],
    "relationships": [
        {
            "name": "cmdb_to_metrics",
            "models": ["cmdb_host", "host_metrics"],
            "joinType": "ONE_TO_MANY",
            "condition": "cmdb_host.host_ip = host_metrics.host_ip",
        }
    ],
    "metrics": [],
    "views": [
        {
            "name": "active_cmdb_host",
            "statement": (
                "SELECT * FROM k8s_monitor.k8s_node_metrics.cmdb_host WHERE is_deleted = 0"
            ),
        },
        {
            "name": "cpu_metrics",
            "statement": (
                "SELECT * FROM k8s_monitor.k8s_node_metrics.host_metrics "
                "WHERE metric_name = 'cpu_usage'"
            ),
        },
    ],
}

MANIFEST_B64 = base64.b64encode(json.dumps(MANIFEST).encode()).decode()


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def query(sql: str) -> dict:
    """向 ibis-server 发送语义 SQL，执行并返回结果 dict。"""
    resp = requests.post(
        f"{IBIS_SERVER}/v3/connector/clickhouse/query",
        json={
            "sql": sql,
            "manifestStr": MANIFEST_B64,
            "connectionInfo": CONNECTION_INFO,
        },
        timeout=30,
    )
    return resp.json()


def dry_plan(sql: str) -> dict:
    """只转换 SQL，不执行，返回物理 SQL（wren-core transform_sql 结果）。"""
    resp = requests.post(
        f"{IBIS_SERVER}/v3/connector/dry-plan",
        json={
            "sql": sql,
            "manifestStr": MANIFEST_B64,
        },
        timeout=30,
    )
    return resp.json()


def sep(title: str = ""):
    print("\n" + "=" * 70)
    if title:
        print(f"  {title}")
    print()


def print_result(result: dict):
    if "errorCode" in result:
        print(f"  ❌ 错误: [{result['errorCode']}] {result['message']}")
        return
    cols = result.get("columns", [])
    rows = result.get("data", [])
    # header
    widths = [max(len(str(c)), max((len(str(r[i])) for r in rows), default=0)) for i, c in enumerate(cols)]
    header = "  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths))
    divider = "  " + "  ".join("-" * w for w in widths)
    print(header)
    print(divider)
    for row in rows:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths)))
    print(f"\n  共 {len(rows)} 行")


# ─────────────────────────────────────────────────────────────────────────────
# 健康检查
# ─────────────────────────────────────────────────────────────────────────────
sep("健康检查")
health = requests.get(f"{IBIS_SERVER}/health").json()
print(f"  ibis-server status: {health['status']}")

# ─────────────────────────────────────────────────────────────────────────────
# Case 1: 单表 + 计算列（CASE WHEN 自动展开）
# ─────────────────────────────────────────────────────────────────────────────
sep("Case 1 — 各主机 CPU 平均使用率（降序）")
sql1 = """
SELECT hostname, AVG(cpu_usage) AS avg_cpu
FROM host_metrics
GROUP BY hostname
ORDER BY avg_cpu DESC
"""
print(f"  语义 SQL: {sql1.strip()}\n")
print_result(query(sql1))

# ─────────────────────────────────────────────────────────────────────────────
# Case 2: WHERE 过滤计算列
# ─────────────────────────────────────────────────────────────────────────────
sep("Case 2 — 内存使用率 > 50% 的主机")
sql2 = """
SELECT hostname, event_time, memory_usage_percent
FROM host_metrics
WHERE memory_usage_percent > 50
ORDER BY memory_usage_percent DESC
"""
print(f"  语义 SQL: {sql2.strip()}\n")
print_result(query(sql2))

# ─────────────────────────────────────────────────────────────────────────────
# Case 3: cmdb_host 计算列 active_idc
# ─────────────────────────────────────────────────────────────────────────────
sep("Case 3 — 有效 CMDB 主机（active_idc 计算列）")
sql3 = """
SELECT host_ip, active_idc, supplier
FROM cmdb_host
WHERE is_deleted = 0
"""
print(f"  语义 SQL: {sql3.strip()}\n")
print_result(query(sql3))

# ─────────────────────────────────────────────────────────────────────────────
# Case 4: JOIN（通过 relationship cmdb_to_metrics）
# ─────────────────────────────────────────────────────────────────────────────
sep("Case 4 — 关联查询：主机 IDC + CPU 使用率")
sql4 = """
SELECT c.idc, m.hostname, m.cpu_usage
FROM cmdb_host c
JOIN host_metrics m ON c.host_ip = m.host_ip
WHERE c.is_deleted = 0
  AND m.cpu_usage IS NOT NULL
ORDER BY m.cpu_usage DESC
"""
print(f"  语义 SQL: {sql4.strip()}\n")
print_result(query(sql4))

# ─────────────────────────────────────────────────────────────────────────────
# Case 5: 视图查询
# ─────────────────────────────────────────────────────────────────────────────
sep("Case 5 — 视图查询: cpu_metrics")
sql5 = """
SELECT hostname, metric_value AS cpu_usage
FROM k8s_monitor.k8s_node_metrics.cpu_metrics
ORDER BY cpu_usage DESC
"""
print(f"  语义 SQL: {sql5.strip()}\n")
print_result(query(sql5))

# ─────────────────────────────────────────────────────────────────────────────
# Case 6: dry-run（只验证 SQL，不执行，返回转换后的物理 SQL）
# ─────────────────────────────────────────────────────────────────────────────
sep("Case 6 — dry-plan: 查看 wren-core 转换后的物理 SQL（不执行）")
sql6 = "SELECT hostname, AVG(cpu_usage) AS avg_cpu FROM host_metrics GROUP BY hostname"
result6 = dry_plan(sql6)
# dry-plan 直接返回物理 SQL 字符串（或错误 dict）
if isinstance(result6, dict) and "errorCode" in result6:
    print(f"  ❌ {result6['message']}")
else:
    physical = result6 if isinstance(result6, str) else result6.get("sql", str(result6))
    print(f"  语义 SQL :\n    {sql6}")
    print(f"  物理 SQL :\n    {physical}")

sep()
print("  端到端演示完毕")
print("  语义 SQL → ibis-server(wren-core) → 物理 SQL → ClickHouse → 结果")
