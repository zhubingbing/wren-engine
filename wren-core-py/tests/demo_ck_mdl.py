"""
基于 k8s_node_metrics 两张 ClickHouse 表的 MDL 设计与 transform_sql 验证
运行方式: poetry run python tests/demo_ck_mdl.py
"""

import base64
import json

from wren_core import SessionContext


def make_ctx(manifest: dict) -> SessionContext:
    mdl_b64 = base64.b64encode(json.dumps(manifest).encode()).decode()
    return SessionContext(mdl_b64, None)


def show(title: str, input_sql: str, ctx: SessionContext):
    try:
        result = ctx.transform_sql(input_sql)
        print(f"\n{'='*70}")
        print(f"【{title}】")
        print(f"  输入 SQL:\n    {input_sql}")
        print(f"  转换后:\n    {result}")
    except Exception as e:
        print(f"\n{'='*70}")
        print(f"【{title}】")
        print(f"  输入 SQL:\n    {input_sql}")
        print(f"  ❌ 报错: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MDL 定义
# ─────────────────────────────────────────────────────────────────────────────
manifest = {
    "catalog": "k8s_monitor",       # 逻辑 catalog 名，随意起
    "schema":  "k8s_node_metrics",  # 对应 ClickHouse 的 database
    "dataSource": "clickhouse",
    "models": [

        # ── 表1：主机监控原始指标 ──────────────────────────────────────────
        {
            "name": "host_metrics",   # 语义层模型名（SQL 里用这个）
            "tableReference": {
                "schema": "k8s_node_metrics",         # ClickHouse database
                "table":  "ods_host_metrics_raw"      # 物理表名
            },
            "columns": [
                # 时间维度
                {"name": "event_time",    "type": "timestamp"},
                {"name": "ingest_time",   "type": "timestamp"},
                {"name": "ds",            "type": "date"},

                # 主机标识
                {"name": "instance",      "type": "varchar"},
                {"name": "hostname",      "type": "varchar"},
                {"name": "host_ip",       "type": "varchar"},

                # Prometheus 维度
                {"name": "job",           "type": "varchar"},
                {"name": "cluster",       "type": "varchar"},
                {"name": "metric_name",   "type": "varchar"},
                {"name": "metric_value",  "type": "double"},
                {"name": "labels_json",   "type": "varchar"},

                # CMDB 冗余字段
                {"name": "cmdb_switch_ip",     "type": "varchar"},
                {"name": "cmdb_switch_port",   "type": "varchar"},
                {"name": "cmdb_idc",           "type": "varchar"},
                {"name": "cmdb_cabinet",       "type": "varchar"},
                {"name": "cmdb_ipmi",          "type": "varchar"},
                {"name": "cmdb_supplier",      "type": "varchar"},
                {"name": "cmdb_purchase_date", "type": "date"},

                # 采集链路
                {"name": "data_source",      "type": "varchar"},
                {"name": "ingest_pipeline",  "type": "varchar"},
                {"name": "ingest_version",   "type": "varchar"},

                # ── 计算列：常用指标直接取（device='' 的标量指标）──────────
                {
                    "name": "cpu_usage",
                    "type": "double",
                    "isCalculated": True,
                    "expression": "CASE WHEN metric_name = 'cpu_usage' THEN metric_value END",
                },
                {
                    "name": "cpu_utilization",
                    "type": "double",
                    "isCalculated": True,
                    "expression": "CASE WHEN metric_name = 'cpu_utilization' THEN metric_value END",
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
                {
                    "name": "memory_total_bytes",
                    "type": "double",
                    "isCalculated": True,
                    "expression": "CASE WHEN metric_name = 'memory_total_bytes' THEN metric_value END",
                },
                {
                    "name": "tcp_established_count",
                    "type": "double",
                    "isCalculated": True,
                    "expression": "CASE WHEN metric_name = 'tcp_established_count' THEN metric_value END",
                },
                {
                    "name": "uptime_seconds",
                    "type": "double",
                    "isCalculated": True,
                    "expression": "CASE WHEN metric_name = 'uptime_seconds' THEN metric_value END",
                },
            ],
            "primaryKey": "",
        },

        # ── 表2：CMDB 主机快照 ────────────────────────────────────────────
        {
            "name": "cmdb_host",      # 语义层模型名
            "tableReference": {
                "schema": "k8s_node_metrics",
                "table":  "ods_cmdb_host_snapshot"
            },
            "columns": [
                {"name": "snapshot_time",  "type": "timestamp"},
                {"name": "source_system",  "type": "varchar"},
                {"name": "sync_batch_id",  "type": "varchar"},
                {"name": "host_ip",        "type": "varchar"},
                {"name": "switch_ip",      "type": "varchar"},
                {"name": "switch_port",    "type": "varchar"},
                {"name": "idc",            "type": "varchar"},
                {"name": "cabinet",        "type": "varchar"},
                {"name": "ipmi",           "type": "varchar"},
                {"name": "supplier",       "type": "varchar"},
                {"name": "purchase_date",  "type": "date"},
                {"name": "is_deleted",     "type": "integer"},
                {"name": "ingest_time",    "type": "timestamp"},
                {"name": "ds",             "type": "date"},

                # 计算列：过滤掉已删除记录的 idc（常用）
                {
                    "name": "active_idc",
                    "type": "varchar",
                    "isCalculated": True,
                    "expression": "CASE WHEN is_deleted = 0 THEN idc END",
                },

                # 关系列：关联到 host_metrics（通过 host_ip）
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
            "joinType": "ONE_TO_MANY",           # 1台主机 → 多条指标
            "condition": "cmdb_host.host_ip = host_metrics.host_ip",
        }
    ],

    "metrics": [],

    "views": [
        # 视图1：只看有效 CMDB 主机（过滤 is_deleted）
        {
            "name": "active_cmdb_host",
            "statement": (
                "SELECT * FROM k8s_monitor.k8s_node_metrics.cmdb_host "
                "WHERE is_deleted = 0"
            ),
        },
        # 视图2：cpu_usage 指标行（过滤掉其他 metric_name，减少扫描量）
        # 注意：视图里不放时间范围，时间过滤由查询侧传入，避免方言差异
        {
            "name": "cpu_metrics",
            "statement": (
                "SELECT * FROM k8s_monitor.k8s_node_metrics.host_metrics "
                "WHERE metric_name = 'cpu_usage'"
            ),
        },
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# 验证各种查询
# ─────────────────────────────────────────────────────────────────────────────
ctx = make_ctx(manifest)

print(">>> MDL 创建成功，开始验证 transform_sql\n")

# 1. 最简单：查 host_metrics 普通字段
show(
    "1. 单表查询 - 查指定主机的原始指标",
    "SELECT hostname, metric_name, metric_value, event_time "
    "FROM host_metrics "
    "WHERE hostname = 'c7h12-t2-163-109' "
    "  AND metric_name = 'cpu_usage'",
    ctx,
)

# 2. 使用计算列（CASE WHEN 被展开）
show(
    "2. 计算列 - 直接查 cpu_usage（自动展开 CASE WHEN）",
    "SELECT hostname, event_time, cpu_usage "
    "FROM host_metrics "
    "WHERE hostname = 'c7h12-t2-163-109' "
    "  AND cpu_usage IS NOT NULL",
    ctx,
)

# 3. 多个计算列同时查
show(
    "3. 计算列 - 同时查 CPU + Memory",
    "SELECT hostname, event_time, cpu_usage, memory_usage_percent, memory_used_bytes "
    "FROM host_metrics "
    "WHERE cluster = 'neodys'",
    ctx,
)

# 4. 查 cmdb_host 表
show(
    "4. CMDB 表 - 查有效主机信息",
    "SELECT host_ip, idc, cabinet, supplier "
    "FROM cmdb_host "
    "WHERE is_deleted = 0",
    ctx,
)

# 5. 计算列 active_idc
show(
    "5. CMDB 计算列 - 查 active_idc（只有未删除的主机才有值）",
    "SELECT host_ip, active_idc, supplier "
    "FROM cmdb_host",
    ctx,
)

# 6. 查视图（视图展开为子查询）
show(
    "6. 视图 - 查 active_cmdb_host",
    "SELECT host_ip, idc, cabinet "
    "FROM k8s_monitor.k8s_node_metrics.active_cmdb_host",
    ctx,
)

# 7. 查最近指标视图
show(
    "7. 视图 - 查 cpu_metrics（时间过滤由查询侧传入）",
    "SELECT hostname, metric_name, metric_value "
    "FROM k8s_monitor.k8s_node_metrics.cpu_metrics "
    "WHERE hostname = 'c7h12-t2-163-109'",
    ctx,
)

print(f"\n{'='*70}")
print("全部场景验证完毕")
print()
print("说明：")
print("  - 以上输出的 SQL 是 wren-core 翻译后的物理 SQL")
print("  - 拿这个 SQL 直接去 ClickHouse 执行即可")
print("  - 计算列的 CASE WHEN 表达式已被自动内联展开")
