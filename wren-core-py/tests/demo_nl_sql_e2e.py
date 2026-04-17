"""
完整 NL→SQL 端到端演示

流程:
  用户自然语言问题
    → WrenMemory.recall_queries()  (few-shot 历史 SQL)
    → WrenMemory.get_context()     (schema 上下文)
    → LLM (gpt-5.4 via cctop)     (生成语义 SQL)
    → ibis-server /dry-plan        (验证 SQL 语法 + wren-core 转换)
    → ibis-server /query           (ClickHouse 执行)
    → WrenMemory.store_query()     (存回 memory 供下次 few-shot)

依赖:
  - ibis-server 运行在 http://127.0.0.1:8100
  - ClickHouse 运行在 localhost:8123
  - LLM: https://claudecc.top/v1 (gpt-5.4, Responses API)

运行:
  cd wren-core-py
  .venv/bin/python tests/demo_nl_sql_e2e.py
"""

import os
import sys
import pathlib
import re
import base64
import json
import types

import requests
from openai import OpenAI

# ── 绕过 wren/__init__.py 的 engine 依赖，直接加载 memory 子模块 ──────────────
WREN_SRC = pathlib.Path(__file__).parents[2] / "wren" / "src"
sys.path.insert(0, str(WREN_SRC))

_wren_pkg = types.ModuleType("wren")
_wren_pkg.__path__ = [str(WREN_SRC / "wren")]
_wren_pkg.__package__ = "wren"
sys.modules.setdefault("wren", _wren_pkg)

from wren.memory.store import MemoryStore  # noqa: E402
from wren.memory.seed_queries import generate_seed_queries  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────
IBIS_SERVER   = os.environ.get("IBIS_SERVER", "http://127.0.0.1:8100")
MEMORY_PATH   = os.environ.get("WREN_MEMORY_PATH", "/mnt/data/wren/.wren/memory")

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

# ─────────────────────────────────────────────────────────────────────────────
# MDL manifest
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

MANIFEST_B64 = base64.b64encode(json.dumps(MANIFEST).encode()).decode()


# ─────────────────────────────────────────────────────────────────────────────
# WrenMemory 包装
# ─────────────────────────────────────────────────────────────────────────────
class WrenMemory:
    def __init__(self, path: str):
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


# ─────────────────────────────────────────────────────────────────────────────
# LLM 客户端（gpt-5.4 Responses API）
# ─────────────────────────────────────────────────────────────────────────────
class LLMClient:
    """gpt-5.4 via OpenAI Responses API (stream mode).

    gpt-5.4 是 OpenAI 最新推理模型，只有 stream 模式才能拿到文字输出
    （非 stream 的 output 字段为空，是代理的已知问题）。
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key)

    def chat(self, messages: list[dict], max_tokens: int = 1024) -> str:
        """调用 Responses API (stream)，收集 TextDelta 事件拼成完整文本。"""
        # 合并 system + user 为单条 input（gpt-5.4 对 messages list 支持不稳定）
        combined = "\n\n".join(
            m["content"] for m in messages if m.get("content")
        )
        text_parts = []
        with self._client.responses.stream(
            model=self.model,
            input=combined,
            max_output_tokens=max_tokens,
        ) as stream:
            for event in stream:
                # ResponseTextDeltaEvent 携带增量文本
                delta = getattr(event, "delta", None)
                if delta and isinstance(delta, str):
                    text_parts.append(delta)
        result = "".join(text_parts)
        if not result:
            raise RuntimeError("LLM returned empty text (stream exhausted with no delta)")
        return result


# ─────────────────────────────────────────────────────────────────────────────
# ibis-server 客户端
# ─────────────────────────────────────────────────────────────────────────────
def ibis_query(sql: str) -> dict:
    resp = requests.post(
        f"{IBIS_SERVER}/v3/connector/clickhouse/query",
        json={"sql": sql, "manifestStr": MANIFEST_B64, "connectionInfo": CONNECTION_INFO},
        timeout=30,
    )
    return resp.json()


def ibis_dry_plan(sql: str) -> str | dict:
    """返回物理 SQL 字符串，或错误 dict。"""
    resp = requests.post(
        f"{IBIS_SERVER}/v3/connector/dry-plan",
        json={"sql": sql, "manifestStr": MANIFEST_B64},
        timeout=30,
    )
    data = resp.json()
    # 正常返回是纯字符串
    return data


def extract_sql(text: str) -> str:
    """从 LLM 输出中提取 SQL（去掉 markdown 代码块）。"""
    # 先找 ```sql ... ``` 块
    m = re.search(r"```(?:sql)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # 找第一个 SELECT/WITH 开头的行
    for line in text.split("\n"):
        stripped = line.strip()
        if re.match(r"^(SELECT|WITH|select|with)", stripped):
            # 取到结尾（简单处理）
            return text[text.index(stripped):].strip().rstrip(";") + ""
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# 核心：NL → SQL pipeline
# ─────────────────────────────────────────────────────────────────────────────
def nl_to_sql(nl_question: str, mem: WrenMemory, llm: LLMClient) -> dict:
    """
    完整 NL→SQL 流程，返回:
      {
        "nl": str,
        "sql": str,           # LLM 生成的语义 SQL
        "physical_sql": str,  # wren-core 转换后的物理 SQL
        "result": dict,       # ClickHouse 执行结果
        "few_shots": list,    # 使用的 few-shot 示例
        "error": str | None,
      }
    """
    result = {"nl": nl_question, "sql": None, "physical_sql": None,
              "result": None, "few_shots": [], "error": None}

    # ── Step 1: recall few-shot 历史 SQL ─────────────────────────────────────
    few_shots = mem.recall_queries(nl_question, limit=3)
    result["few_shots"] = few_shots

    # ── Step 2: 获取 schema 上下文 ────────────────────────────────────────────
    ctx = mem.get_context(MANIFEST, nl_question)
    schema_text = ctx.get("schema", "")
    if not schema_text and ctx.get("results"):
        schema_text = "\n".join(
            f"[{r['item_type']}] {r.get('model_name','')}.{r.get('name','')} — {r.get('description','')}"
            for r in ctx["results"]
        )

    # ── Step 3: 构造 prompt ───────────────────────────────────────────────────
    few_shot_text = ""
    if few_shots:
        few_shot_text = "\n\n## 历史 SQL 示例（few-shot）\n"
        for fs in few_shots:
            few_shot_text += f"Q: {fs['nl_query']}\nSQL: {fs['sql_query']}\n\n"

    system_prompt = """You are a SQL expert for Wren Engine semantic layer.
Generate a single SQL query using the semantic model names (NOT physical table names).
Rules:
- Use model names like `host_metrics`, `cmdb_host` directly (not physical table names)
- Do NOT use table aliases (no AS c, no AS h) — use full model names in column references
- Calculated columns (cpu_usage, memory_usage_percent, memory_used_bytes, active_idc) can be used directly
- For JOIN use: JOIN cmdb_host ON cmdb_host.host_ip = host_metrics.host_ip
- Return ONLY the SQL query, no explanation, no markdown fences, no semicolons"""

    user_prompt = f"""## Schema
{schema_text}
{few_shot_text}
## Question
{nl_question}

SQL:"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # ── Step 4: LLM 生成 SQL ──────────────────────────────────────────────────
    try:
        raw_output = llm.chat(messages, max_tokens=512)
        sql = extract_sql(raw_output)
        result["sql"] = sql
    except Exception as e:
        result["error"] = f"LLM error: {e}"
        return result

    # ── Step 5: dry-plan 验证（wren-core 转换） ───────────────────────────────
    physical = ibis_dry_plan(sql)
    if isinstance(physical, dict) and "errorCode" in physical:
        result["error"] = f"dry-plan error: {physical['message']}"
        return result
    result["physical_sql"] = physical

    # ── Step 6: 执行查询 ──────────────────────────────────────────────────────
    query_result = ibis_query(sql)
    if "errorCode" in query_result:
        result["error"] = f"query error: {query_result['message']}"
        return result
    result["result"] = query_result

    # ── Step 7: 存回 memory ───────────────────────────────────────────────────
    mem.store_query(nl_question, sql, datasource="clickhouse", tags="source:llm")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────
def sep(title: str = ""):
    print("\n" + "=" * 70)
    if title:
        print(f"  {title}")
    print()


def print_result(result: dict):
    if "errorCode" in result:
        print(f"  ❌ {result['message']}")
        return
    cols = result.get("columns", [])
    rows = result.get("data", [])
    if not cols:
        print("  (无数据)")
        return
    widths = [
        max(len(str(c)), max((len(str(r[i])) for r in rows), default=0))
        for i, c in enumerate(cols)
    ]
    header  = "  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths))
    divider = "  " + "  ".join("-" * w for w in widths)
    print(header)
    print(divider)
    for row in rows:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths)))
    print(f"\n  共 {len(rows)} 行")


def print_pipeline(r: dict):
    """打印一次 NL→SQL 的完整过程。"""
    print(f"  问题   : {r['nl']}")

    if r["few_shots"]:
        print(f"  few-shot: {len(r['few_shots'])} 条历史 SQL 参考")
        for fs in r["few_shots"][:2]:
            print(f"    ↳ {fs['nl_query']}")

    if r["error"]:
        print(f"\n  ❌ 出错: {r['error']}")
        return

    print(f"\n  语义 SQL :\n    {r['sql']}")
    physical = r["physical_sql"] or ""
    print(f"\n  物理 SQL :\n    {physical[:200]}{'...' if len(physical) > 200 else ''}")
    print(f"\n  执行结果:")
    print_result(r["result"])


# ─────────────────────────────────────────────────────────────────────────────
# 初始化
# ─────────────────────────────────────────────────────────────────────────────
sep("初始化")

# 建立 memory 索引
mem = WrenMemory(path=MEMORY_PATH)
idx = mem.index_manifest(MANIFEST, replace=True, seed_queries=True)
print(f"  memory 索引: {idx['schema_items']} schema items, {idx['seed_queries']} seed queries")

# LLM 客户端
llm = LLMClient(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=LLM_MODEL)

# 健康检查
health = requests.get(f"{IBIS_SERVER}/health").json()
print(f"  ibis-server: {health['status']}")
print(f"  LLM model  : {LLM_MODEL} @ {LLM_BASE_URL}")


# ─────────────────────────────────────────────────────────────────────────────
# 测试用例
# ─────────────────────────────────────────────────────────────────────────────
test_questions = [
    "各主机的 CPU 平均使用率，按降序排列",
    "内存使用率超过 50% 的主机有哪些",
    "按 IDC 统计有效主机数量",
    "CPU 最高的主机来自哪个 IDC",
]

for i, question in enumerate(test_questions, 1):
    sep(f"问题 {i}")
    result = nl_to_sql(question, mem, llm)
    print_pipeline(result)

# ─────────────────────────────────────────────────────────────────────────────
# 查看 memory 状态
# ─────────────────────────────────────────────────────────────────────────────
sep("Memory 状态")
info = mem.status()
for table, count in info.get("tables", {}).items():
    print(f"  {table}: {count} 行")

sep()
print("  完整链路: NL → memory recall → schema ctx → LLM → dry-plan → query → memory store")
