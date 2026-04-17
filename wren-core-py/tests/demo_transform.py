"""
wren-core-py transform_sql 场景演示
运行方式: poetry run python tests/demo_transform.py
"""

import base64
import json

from wren_core import SessionContext


def make_ctx(manifest: dict) -> SessionContext:
    mdl_b64 = base64.b64encode(json.dumps(manifest).encode()).decode()
    return SessionContext(mdl_b64, None)


def show(title: str, sql: str, result: str):
    print(f"\n{'='*60}")
    print(f"【{title}】")
    print(f"  输入: {sql}")
    print(f"  输出: {result}")


# ─────────────────────────────────────────────
# 场景 1：单表简单查询
# 验证：表名被展开为物理表路径
# ─────────────────────────────────────────────
def case1_simple_query():
    manifest = {
        "catalog": "shop",
        "schema": "public",
        "dataSource": "datafusion",
        "models": [
            {
                "name": "orders",
                "tableReference": {"schema": "public", "table": "orders"},
                "columns": [
                    {"name": "order_id",    "type": "integer"},
                    {"name": "customer_id", "type": "integer"},
                    {"name": "amount",      "type": "double"},
                    {"name": "status",      "type": "varchar"},
                ],
                "primaryKey": "order_id",
            }
        ],
        "relationships": [],
        "metrics": [],
        "views": [],
    }
    ctx = make_ctx(manifest)

    show("单表查询 - SELECT *",
         "SELECT * FROM orders",
         ctx.transform_sql("SELECT * FROM orders"))

    show("单表查询 - 带 WHERE",
         "SELECT order_id, amount FROM orders WHERE amount > 100",
         ctx.transform_sql("SELECT order_id, amount FROM orders WHERE amount > 100"))

    show("单表查询 - 带 catalog.schema 前缀",
         "SELECT * FROM shop.public.orders",
         ctx.transform_sql("SELECT * FROM shop.public.orders"))


# ─────────────────────────────────────────────
# 场景 2：计算列（Calculated Column）
# 验证：计算列的表达式被展开内联
# ─────────────────────────────────────────────
def case2_calculated_column():
    manifest = {
        "catalog": "shop",
        "schema": "public",
        "dataSource": "datafusion",
        "models": [
            {
                "name": "orders",
                "tableReference": {"schema": "public", "table": "orders"},
                "columns": [
                    {"name": "order_id",    "type": "integer"},
                    {"name": "amount",      "type": "double"},
                    {"name": "tax_rate",    "type": "double"},
                    # 计算列：amount * (1 + tax_rate)
                    {
                        "name": "total_price",
                        "type": "double",
                        "isCalculated": True,
                        "expression": "amount * (1 + tax_rate)",
                    },
                ],
                "primaryKey": "order_id",
            }
        ],
        "relationships": [],
        "metrics": [],
        "views": [],
    }
    ctx = make_ctx(manifest)

    show("计算列 - 直接查询计算列",
         "SELECT order_id, total_price FROM orders",
         ctx.transform_sql("SELECT order_id, total_price FROM orders"))

    show("计算列 - 对计算列做 WHERE 过滤",
         "SELECT order_id FROM orders WHERE total_price > 500",
         ctx.transform_sql("SELECT order_id FROM orders WHERE total_price > 500"))


# ─────────────────────────────────────────────
# 场景 3：多表关系（Relationship）
# 验证：跨模型字段引用时自动推导 JOIN
# ─────────────────────────────────────────────
def case3_relationship():
    manifest = {
        "catalog": "shop",
        "schema": "public",
        "dataSource": "datafusion",
        "models": [
            {
                "name": "orders",
                "tableReference": {"schema": "public", "table": "orders"},
                "columns": [
                    {"name": "order_id",    "type": "integer"},
                    {"name": "customer_id", "type": "integer"},
                    {"name": "amount",      "type": "double"},
                    # 关系列：声明 orders → customers 的关联，类型填对端模型名
                    {"name": "customers", "type": "customers", "relationship": "orders_customers"},
                ],
                "primaryKey": "order_id",
            },
            {
                "name": "customers",
                "tableReference": {"schema": "public", "table": "customers"},
                "columns": [
                    {"name": "id",      "type": "integer"},
                    {"name": "name",    "type": "varchar"},
                    {"name": "email",   "type": "varchar"},
                    {"name": "country", "type": "varchar"},
                ],
                "primaryKey": "id",
            },
        ],
        "relationships": [
            {
                "name": "orders_customers",
                "models": ["orders", "customers"],
                "joinType": "MANY_TO_ONE",
                "condition": "orders.customer_id = customers.id",
            }
        ],
        "metrics": [],
        "views": [],
    }
    ctx = make_ctx(manifest)

    # 查询时用 模型名.字段名 访问关联表的字段
    show("关系 - 查询关联表字段（自动推导 JOIN）",
         "SELECT order_id, customers.name FROM orders",
         ctx.transform_sql("SELECT order_id, customers.name FROM orders"))

    show("关系 - 对关联表字段做 WHERE 过滤",
         "SELECT order_id, amount FROM orders WHERE customers.name = 'Alice'",
         ctx.transform_sql("SELECT order_id, amount FROM orders WHERE customers.name = 'Alice'"))


# ─────────────────────────────────────────────
# 场景 4：View（视图）
# 验证：视图被展开为子查询
# ─────────────────────────────────────────────
def case4_view():
    manifest = {
        "catalog": "shop",
        "schema": "public",
        "dataSource": "datafusion",
        "models": [
            {
                "name": "orders",
                "tableReference": {"schema": "public", "table": "orders"},
                "columns": [
                    {"name": "order_id", "type": "integer"},
                    {"name": "amount",   "type": "double"},
                    {"name": "status",   "type": "varchar"},
                ],
                "primaryKey": "order_id",
            }
        ],
        "relationships": [],
        "metrics": [],
        "views": [
            {
                "name": "big_orders",
                # 视图定义：只看大额订单
                "statement": "SELECT * FROM shop.public.orders WHERE amount > 1000",
            }
        ],
    }
    ctx = make_ctx(manifest)

    show("视图 - 查询视图（视图被展开为子查询）",
         "SELECT * FROM shop.public.big_orders",
         ctx.transform_sql("SELECT * FROM shop.public.big_orders"))


# ─────────────────────────────────────────────
# 场景 5：LIMIT 下推
# 验证：pushdown_limit 在 SQL 外层加 LIMIT
# ─────────────────────────────────────────────
def case5_limit_pushdown():
    manifest = {
        "catalog": "shop",
        "schema": "public",
        "dataSource": "datafusion",
        "models": [
            {
                "name": "orders",
                "tableReference": {"schema": "public", "table": "orders"},
                "columns": [
                    {"name": "order_id", "type": "integer"},
                    {"name": "amount",   "type": "double"},
                ],
                "primaryKey": "order_id",
            }
        ],
        "relationships": [],
        "metrics": [],
        "views": [],
    }
    ctx = make_ctx(manifest)
    sql = ctx.transform_sql("SELECT order_id, amount FROM orders")

    show("LIMIT 下推 - pushdown_limit(sql, 10)",
         sql,
         ctx.pushdown_limit(sql, 10))


# ─────────────────────────────────────────────
# 场景 6：ManifestExtractor — 从 SQL 中提取用到的表
# ─────────────────────────────────────────────
def case6_extractor():
    from wren_core import ManifestExtractor

    manifest = {
        "catalog": "shop",
        "schema": "public",
        "dataSource": "datafusion",
        "models": [
            {
                "name": "orders",
                "tableReference": {"schema": "public", "table": "orders"},
                "columns": [{"name": "order_id", "type": "integer"}],
                "primaryKey": "order_id",
            },
            {
                "name": "customers",
                "tableReference": {"schema": "public", "table": "customers"},
                "columns": [{"name": "id", "type": "integer"}],
                "primaryKey": "id",
            },
            {
                "name": "products",
                "tableReference": {"schema": "public", "table": "products"},
                "columns": [{"name": "id", "type": "integer"}],
                "primaryKey": "id",
            },
        ],
        "relationships": [],
        "metrics": [],
        "views": [],
    }
    mdl_b64 = base64.b64encode(json.dumps(manifest).encode()).decode()
    extractor = ManifestExtractor(mdl_b64)

    sql = "SELECT * FROM shop.public.orders JOIN shop.public.customers ON orders.order_id = customers.id"
    used = extractor.resolve_used_table_names(sql)

    print(f"\n{'='*60}")
    print("【ManifestExtractor - 从 SQL 提取用到的表名】")
    print(f"  SQL: {sql}")
    print(f"  用到的表: {used}")

    # 只取 orders + customers 的 Manifest（裁剪掉 products）
    sub = extractor.extract_by(used)
    print(f"  裁剪后 Manifest 包含的模型: {[m.name for m in sub.models]}")


if __name__ == "__main__":
    print(">>> wren-core-py transform_sql 场景演示\n")
    case1_simple_query()
    case2_calculated_column()
    case3_relationship()
    case4_view()
    case5_limit_pushdown()
    case6_extractor()
    print(f"\n{'='*60}")
    print("全部场景执行完毕")
