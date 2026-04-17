"""
NL→SQL 完整工作流演示
- manifest 直接从 demo_ck_mdl.py 导入，无文件 IO
- LanceDB 存储路径: /mnt/data/wren/.wren/memory
- 依赖: lancedb, sentence-transformers, wren_core

运行方式:
  cd wren-core-py
  poetry run python tests/demo_nl_sql.py
"""

import sys
import pathlib

# 把 wren/src 加进来，但 wren/__init__.py 会触发 engine 导入链。
# 用 importlib 直接加载 memory 子模块，绕过顶层 __init__.py。
WREN_SRC = pathlib.Path(__file__).parents[2] / "wren" / "src"
sys.path.insert(0, str(WREN_SRC))

# 注入一个假的 wren 顶层包，让 `from wren.memory.xxx import ...` 正常找到文件
# 但不执行 wren/__init__.py（它依赖 sqlglot/ibis 等未安装的包）
import types
_wren_pkg = types.ModuleType("wren")
_wren_pkg.__path__ = [str(WREN_SRC / "wren")]
_wren_pkg.__package__ = "wren"
sys.modules.setdefault("wren", _wren_pkg)

import base64
import json

from wren_core import SessionContext
# 直接导入 memory 子模块，绕过 wren/__init__.py 中对 engine/connector 的导入
from wren.memory.store import MemoryStore
from wren.memory.seed_queries import generate_seed_queries


class WrenMemory:
    """轻量包装，等价于 wren.memory.WrenMemory，但不触发 engine 依赖。"""

    def __init__(self, path):
        self._store = MemoryStore(path=path)

    def index_manifest(self, manifest, *, replace=True, seed_queries=True):
        return self._store.index_schema(manifest, replace=replace, seed_queries=seed_queries)

    def get_context(self, manifest, query, **kwargs):
        return self._store.get_context(manifest, query, **kwargs)

    def store_query(self, nl_query, sql_query, *, datasource=None, tags=None):
        self._store.store_query(nl_query, sql_query, datasource=datasource, tags=tags)

    def recall_queries(self, query, *, limit=3, datasource=None):
        return self._store.recall_queries(query, limit=limit, datasource=datasource)

    def status(self):
        return self._store.status()

# ── manifest（从 demo_ck_mdl.py 直接复用） ────────────────────────────────
manifest = {
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

MEMORY_PATH = "/mnt/data/wren/.wren/memory"

# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def make_ctx() -> SessionContext:
    mdl_b64 = base64.b64encode(json.dumps(manifest).encode()).decode()
    return SessionContext(mdl_b64, None)


def transform(ctx: SessionContext, sql: str) -> str:
    """把语义 SQL 翻译成物理 SQL。"""
    try:
        return ctx.transform_sql(sql)
    except Exception as e:
        return f"[ERROR] {e}"


def sep(title: str = ""):
    print("\n" + "=" * 70)
    if title:
        print(f"  {title}")


# ─────────────────────────────────────────────────────────────────────────────
# 步骤 1：建索引
# ─────────────────────────────────────────────────────────────────────────────
sep("步骤 1 — 建立 schema 索引 + seed NL-SQL 对")

mem = WrenMemory(path=MEMORY_PATH)
result = mem.index_manifest(manifest, replace=True, seed_queries=True)
print(f"  索引完成: {result['schema_items']} schema items, {result['seed_queries']} seed queries")
print(f"  存储路径: {MEMORY_PATH}")

# ─────────────────────────────────────────────────────────────────────────────
# 步骤 2：查看自动生成的 seed NL-SQL 对
# ─────────────────────────────────────────────────────────────────────────────
sep("步骤 2 — 自动生成的 Seed NL-SQL 对")

seeds = generate_seed_queries(manifest)
for i, s in enumerate(seeds, 1):
    print(f"  [{i}] NL : {s['nl']}")
    print(f"       SQL: {s['sql']}")
    print()

# ─────────────────────────────────────────────────────────────────────────────
# 步骤 3：模拟 NL 提问，召回相似历史 SQL（few-shot）
# ─────────────────────────────────────────────────────────────────────────────
sep("步骤 3 — 召回相似历史 SQL（wren memory recall）")

test_queries = [
    "各主机的 CPU 使用率",
    "查机器内存用量",
    "按 IDC 统计主机数量",
]
for q in test_queries:
    pairs = mem.recall_queries(q, limit=2)
    print(f"  NL: {q!r}")
    if pairs:
        for p in pairs:
            print(f"    ↳ [{p.get('tags','')}] {p['nl_query']} → {p['sql_query']}")
    else:
        print("    ↳ (暂无相似历史记录)")
    print()

# ─────────────────────────────────────────────────────────────────────────────
# 步骤 4：获取 schema 上下文（给 LLM 拼 prompt 用）
# ─────────────────────────────────────────────────────────────────────────────
sep("步骤 4 — 获取 schema 上下文（wren memory fetch）")

ctx_result = mem.get_context(manifest, "CPU 使用率最高的机器")
print(f"  strategy: {ctx_result['strategy']}")
if ctx_result["strategy"] == "full":
    # 小 schema → 全文返回，截断显示
    text = ctx_result["schema"]
    print(f"  schema 文本长度: {len(text)} 字符")
    print("  前 500 字符:")
    print("  " + text[:500].replace("\n", "\n  "))
else:
    for r in ctx_result["results"]:
        print(f"  [{r['item_type']}] {r.get('model_name','')}.{r.get('name','')} — {r.get('description','')}")

# ─────────────────────────────────────────────────────────────────────────────
# 步骤 5：（模拟 LLM）手写 NL→SQL，用 wren_core 验证语义层
# ─────────────────────────────────────────────────────────────────────────────
sep("步骤 5 — 语义 SQL → 物理 SQL（wren_core.transform_sql）")

wren_ctx = make_ctx()

nl_sql_pairs = [
    (
        "各主机 CPU 平均使用率（降序）",
        "SELECT hostname, AVG(cpu_usage) AS avg_cpu FROM host_metrics GROUP BY hostname ORDER BY avg_cpu DESC",
    ),
    (
        "查内存用量 > 80% 的主机",
        "SELECT hostname, event_time, memory_usage_percent FROM host_metrics WHERE memory_usage_percent > 80",
    ),
    (
        "有效 CMDB 主机数量（按 IDC）",
        "SELECT active_idc, COUNT(*) AS cnt FROM cmdb_host WHERE is_deleted = 0 GROUP BY active_idc",
    ),
    (
        "关联查询：主机所属 IDC 和 CPU 使用率",
        "SELECT c.idc, m.hostname, m.cpu_usage FROM cmdb_host c JOIN host_metrics m ON c.host_ip = m.host_ip",
    ),
]

for nl, sql in nl_sql_pairs:
    physical = transform(wren_ctx, sql)
    print(f"  NL : {nl}")
    print(f"  语义SQL : {sql}")
    print(f"  物理SQL : {physical[:200]}{'...' if len(physical) > 200 else ''}")
    print()

# ─────────────────────────────────────────────────────────────────────────────
# 步骤 6：把成功的 NL-SQL 存回 memory（下次可以召回）
# ─────────────────────────────────────────────────────────────────────────────
sep("步骤 6 — 存储成功的 NL-SQL 对（wren memory store）")

mem.store_query(
    nl_query="各主机 CPU 平均使用率降序",
    sql_query="SELECT hostname, AVG(cpu_usage) AS avg_cpu FROM host_metrics GROUP BY hostname ORDER BY avg_cpu DESC",
    datasource="clickhouse",
    tags="source:user",
)
mem.store_query(
    nl_query="内存使用率超过 80% 的主机",
    sql_query="SELECT hostname, event_time, memory_usage_percent FROM host_metrics WHERE memory_usage_percent > 80",
    datasource="clickhouse",
    tags="source:user",
)
print("  已存储 2 条用户 NL-SQL 对")

# ─────────────────────────────────────────────────────────────────────────────
# 步骤 7：再次召回，验证新存的记录出现了
# ─────────────────────────────────────────────────────────────────────────────
sep("步骤 7 — 再次召回验证（存入的记录可被检索到）")

pairs = mem.recall_queries("CPU 使用率高的机器排名", limit=3)
print(f"  查询: 'CPU 使用率高的机器排名'  → 找到 {len(pairs)} 条")
for p in pairs:
    print(f"    NL : {p['nl_query']}")
    print(f"    SQL: {p['sql_query']}")
    print()

# ─────────────────────────────────────────────────────────────────────────────
# 步骤 8：查看 memory 状态
# ─────────────────────────────────────────────────────────────────────────────
sep("步骤 8 — Memory 状态（wren memory status）")

info = mem.status()
print(f"  路径: {info['path']}")
for table, count in info.get("tables", {}).items():
    print(f"  {table}: {count} 行")

sep()
print("  全流程演示完毕")
print("  NL→SQL 工作流: recall(few-shot) → fetch(schema ctx) → transform_sql(验证) → store(存档)")
