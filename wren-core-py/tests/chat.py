"""
交互式 NL→SQL 对话终端

用户用自然语言提问 → LLM 生成 SQL → ClickHouse 执行 → 返回结果

运行:
  cd wren-core-py
  TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 .venv/bin/python tests/chat.py
"""

import os
import sys
import pathlib
import types
import re
import base64
import json

import requests
from openai import OpenAI

# ── 绕过 wren/__init__.py 的 engine 依赖 ─────────────────────────────────────
WREN_SRC = pathlib.Path(__file__).parents[2] / "wren" / "src"
sys.path.insert(0, str(WREN_SRC))
_wren_pkg = types.ModuleType("wren")
_wren_pkg.__path__ = [str(WREN_SRC / "wren")]
_wren_pkg.__package__ = "wren"
sys.modules.setdefault("wren", _wren_pkg)

from wren.memory.store import MemoryStore  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────
IBIS_SERVER   = "http://127.0.0.1:8100"
MEMORY_PATH   = "/mnt/data/wren/.wren/memory"
LLM_BASE_URL  = os.environ.get("LLM_BASE_URL", "https://claudecc.top/v1")
LLM_API_KEY   = os.environ.get("LLM_API_KEY", "")
LLM_MODEL     = os.environ.get("LLM_MODEL", "gpt-5.4")

CONNECTION_INFO = {
    "host": "localhost",
    "port": 8123,
    "database": "k8s_node_metrics",
    "user": "admin",
    "password": "admin",
}

MANIFEST = {
    "catalog": "k8s_monitor",
    "schema": "k8s_node_metrics",
    "dataSource": "clickhouse",
    "models": [
        {
            "name": "host_metrics",
            "tableReference": {"schema": "k8s_node_metrics", "table": "ods_host_metrics_raw"},
            "columns": [
                {"name": "event_time", "type": "timestamp"},
                {"name": "ds", "type": "date"},
                {"name": "hostname", "type": "varchar"},
                {"name": "host_ip", "type": "varchar"},
                {"name": "cluster", "type": "varchar"},
                {"name": "metric_name", "type": "varchar"},
                {"name": "metric_value", "type": "double"},
                {"name": "cpu_usage", "type": "double", "isCalculated": True,
                 "expression": "CASE WHEN metric_name = 'cpu_usage' THEN metric_value END"},
                {"name": "memory_usage_percent", "type": "double", "isCalculated": True,
                 "expression": "CASE WHEN metric_name = 'memory_usage_percent' THEN metric_value END"},
                {"name": "memory_used_bytes", "type": "double", "isCalculated": True,
                 "expression": "CASE WHEN metric_name = 'memory_used_bytes' THEN metric_value END"},
            ],
            "primaryKey": "",
        },
        {
            "name": "cmdb_host",
            "tableReference": {"schema": "k8s_node_metrics", "table": "ods_cmdb_host_snapshot"},
            "columns": [
                {"name": "host_ip", "type": "varchar"},
                {"name": "idc", "type": "varchar"},
                {"name": "cabinet", "type": "varchar"},
                {"name": "supplier", "type": "varchar"},
                {"name": "is_deleted", "type": "integer"},
                {"name": "ds", "type": "date"},
                {"name": "active_idc", "type": "varchar", "isCalculated": True,
                 "expression": "CASE WHEN is_deleted = 0 THEN idc END"},
                {"name": "host_metrics", "type": "host_metrics", "relationship": "cmdb_to_metrics"},
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
        {"name": "active_cmdb_host",
         "statement": "SELECT * FROM k8s_monitor.k8s_node_metrics.cmdb_host WHERE is_deleted = 0"},
        {"name": "cpu_metrics",
         "statement": "SELECT * FROM k8s_monitor.k8s_node_metrics.host_metrics WHERE metric_name = 'cpu_usage'"},
    ],
}

MANIFEST_B64 = base64.b64encode(json.dumps(MANIFEST).encode()).decode()

# Schema 描述（给 LLM 的 system prompt 用）
SCHEMA_DESC = """
数据模型:
- host_metrics: 主机指标表
  字段: event_time(时间), hostname(主机名), host_ip(IP), cluster(集群),
        cpu_usage(CPU使用率%), memory_usage_percent(内存使用率%),
        memory_used_bytes(已用内存字节), metric_name, metric_value
- cmdb_host: CMDB主机资产表
  字段: host_ip(IP), idc(机房), cabinet(机柜), supplier(供应商),
        is_deleted(是否删除,0=有效), active_idc(有效机房,计算列)
  关联: cmdb_host.host_ip = host_metrics.host_ip (一对多)

注意: cpu_usage, memory_usage_percent, memory_used_bytes, active_idc 是计算列，可直接使用
""".strip()

SYSTEM_PROMPT = f"""你是一个 ClickHouse 数据查询助手，帮助用户用自然语言查询 k8s 主机监控数据。

{SCHEMA_DESC}

生成 SQL 规则:
1. 直接使用模型名 host_metrics、cmdb_host（不用物理表名）
2. 不使用表别名（不写 AS h、AS c）
3. 列引用用全名: host_metrics.hostname、cmdb_host.idc
4. JOIN 写法: JOIN cmdb_host ON cmdb_host.host_ip = host_metrics.host_ip
5. 只返回 SQL，不加任何解释、不加分号、不用 markdown 代码块
"""


# ─────────────────────────────────────────────────────────────────────────────
# 组件
# ─────────────────────────────────────────────────────────────────────────────

class Memory:
    def __init__(self, path: str):
        self._store = MemoryStore(path=path)

    def index(self, manifest):
        return self._store.index_schema(manifest, replace=False, seed_queries=True)

    def recall(self, query: str, limit: int = 3) -> list:
        return self._store.recall_queries(query, limit=limit)

    def store(self, nl: str, sql: str):
        self._store.store_query(nl, sql, datasource="clickhouse", tags="source:user")


class LLM:
    def __init__(self):
        self._client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)

    def generate_sql(self, user_question: str, few_shots: list) -> str:
        few_shot_text = ""
        if few_shots:
            few_shot_text = "\n历史参考:\n"
            for fs in few_shots:
                few_shot_text += f"Q: {fs['nl_query']}\nSQL: {fs['sql_query']}\n"

        prompt = f"{SYSTEM_PROMPT}\n{few_shot_text}\n用户问题: {user_question}\nSQL:"

        parts = []
        with self._client.responses.stream(
            model=LLM_MODEL,
            input=prompt,
            max_output_tokens=512,
        ) as stream:
            for event in stream:
                delta = getattr(event, "delta", None)
                if delta and isinstance(delta, str):
                    parts.append(delta)

        sql = "".join(parts).strip().rstrip(";")
        # 去掉可能的 markdown 代码块
        m = re.search(r"```(?:sql)?\s*([\s\S]+?)```", sql, re.IGNORECASE)
        if m:
            sql = m.group(1).strip().rstrip(";")
        return sql


def ibis_dry_plan(sql: str) -> tuple[bool, str]:
    """验证 SQL 并获取物理 SQL。返回 (ok, physical_sql_or_error)"""
    resp = requests.post(
        f"{IBIS_SERVER}/v3/connector/dry-plan",
        json={"sql": sql, "manifestStr": MANIFEST_B64},
        timeout=30,
    )
    data = resp.json()
    if isinstance(data, dict) and "errorCode" in data:
        return False, data.get("message", "unknown error")
    return True, data  # data 是物理 SQL 字符串


def ibis_query(sql: str) -> tuple[bool, dict]:
    """执行 SQL。返回 (ok, result_or_error)"""
    resp = requests.post(
        f"{IBIS_SERVER}/v3/connector/clickhouse/query",
        json={"sql": sql, "manifestStr": MANIFEST_B64, "connectionInfo": CONNECTION_INFO},
        timeout=30,
    )
    data = resp.json()
    if "errorCode" in data:
        return False, data.get("message", "unknown error")
    return True, data


# ─────────────────────────────────────────────────────────────────────────────
# 结果渲染
# ─────────────────────────────────────────────────────────────────────────────

def render_table(result: dict, max_rows: int = 20) -> str:
    cols = result.get("columns", [])
    rows = result.get("data", [])
    if not cols:
        return "  (无数据)"

    display_rows = rows[:max_rows]
    widths = [
        max(len(str(c)), max((len(str(r[i])) for r in display_rows), default=0))
        for i, c in enumerate(cols)
    ]
    sep_line  = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    header    = "|" + "|".join(f" {str(c).ljust(w)} " for c, w in zip(cols, widths)) + "|"
    lines = [sep_line, header, sep_line]
    for row in display_rows:
        lines.append("|" + "|".join(f" {str(v).ljust(w)} " for v, w in zip(row, widths)) + "|")
    lines.append(sep_line)

    total = len(rows)
    suffix = f"  共 {total} 行" + (f"（显示前 {max_rows} 行）" if total > max_rows else "")
    lines.append(suffix)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 主循环
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("  k8s 主机监控 · 自然语言查询终端")
    print("  数据源: ClickHouse  |  LLM: gpt-5.4")
    print("  输入 /quit 退出，/sql 显示上次 SQL，/help 查看示例")
    print("=" * 60)

    # 初始化
    print("\n正在初始化...")
    mem = Memory(path=MEMORY_PATH)
    mem.index(MANIFEST)
    llm = LLM()
    print("就绪。\n")

    last_sql = None
    last_physical_sql = None

    while True:
        try:
            user_input = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue

        # 内置命令
        if user_input.lower() in ("/quit", "/exit", "quit", "exit"):
            print("再见！")
            break

        if user_input.lower() == "/sql":
            if last_sql:
                print(f"\n  语义 SQL:\n  {last_sql}")
                print(f"\n  物理 SQL:\n  {last_physical_sql[:300] if last_physical_sql else 'N/A'}...")
            else:
                print("  暂无 SQL 记录")
            continue

        if user_input.lower() == "/help":
            print("""
  示例问题:
    各主机的 CPU 平均使用率，按降序排列
    内存使用率超过 80% 的主机有哪些
    按 IDC 统计有效主机数量
    CPU 最高的主机来自哪个 IDC
    node-02 最近的内存和 CPU 指标
    北京机房有哪些主机
""")
            continue

        # NL → SQL → 执行
        print("  思考中...", end="", flush=True)

        # 1. 召回 few-shot
        few_shots = mem.recall(user_input, limit=3)

        # 2. LLM 生成 SQL
        try:
            sql = llm.generate_sql(user_input, few_shots)
            last_sql = sql
        except Exception as e:
            print(f"\r  ❌ LLM 错误: {e}")
            continue

        # 3. dry-plan 验证
        ok, physical = ibis_dry_plan(sql)
        if not ok:
            print(f"\r  ❌ SQL 无效: {physical}")
            print(f"  生成的 SQL: {sql}")
            continue
        last_physical_sql = physical

        # 4. 执行查询
        ok, result = ibis_query(sql)
        if not ok:
            print(f"\r  ❌ 查询失败: {result}")
            print(f"  SQL: {sql}")
            continue

        # 5. 渲染结果
        print("\r" + " " * 20 + "\r", end="")  # 清除"思考中"
        print(render_table(result))

        # 6. 存回 memory
        mem.store(user_input, sql)


if __name__ == "__main__":
    main()
