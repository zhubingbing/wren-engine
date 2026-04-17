# Wren-Core 技术内部文档

> 文档目的：帮助开发者理解 wren-core (Rust) 的代码架构、MDL 查询转换流程，以及通过 wren-core-py (PyO3) 从 Python 侧对接使用的方式。

---

## 一、项目概览

Wren Engine 的核心是 **wren-core**（Rust 实现），通过 **wren-core-py**（PyO3 绑定）暴露给 Python 层。它的作用是：接收用户 SQL + MDL（语义模型定义），将其转换为可直接在目标数据源执行的 SQL。

```
用户 SQL + MDL 定义
        ↓
  wren-core-py (PyO3 FFI)
        ↓
  wren-core (Rust)
    ├── MDL 解析与线性分析
    ├── DataFusion 逻辑计划
    └── SQL 生成
        ↓
  目标数据源 SQL (Postgres / BigQuery / Snowflake ...)
```

---

## 二、wren-core Rust 代码结构

### Cargo Workspace 布局

```
wren-core/
├── Cargo.toml              # Workspace 根，定义成员
├── core/                   # 核心语义引擎库 (library crate)
│   └── src/
│       ├── lib.rs          # 公开 API 入口
│       ├── mdl/            # MDL 解析、分析、线性追踪（核心）
│       └── logical_plan/   # DataFusion 分析规则 & 优化
├── sqllogictest/           # SQL 端到端测试框架
│   ├── bin/sqllogictests.rs
│   ├── src/test_context.rs
│   └── test_files/         # .slt 测试文件
│       ├── model.slt
│       ├── view.slt
│       ├── type.slt
│       └── tpch/tpch.slt
├── benchmarks/             # 性能基准测试
└── wren-example/           # 使用示例
```

关键依赖（`Cargo.toml` workspace 级别）：

| 依赖 | 说明 |
|------|------|
| **datafusion** | Canner fork（branch `canner/v49.0.1`），添加了 Wren 特定功能 |
| **wren-core-base** | Path 依赖（`../wren-core-base`），提供 Manifest / Model / Column 等类型定义 |
| **tokio** | 异步运行时 |
| **petgraph** | 图算法，用于 lineage（血缘）和关系链计算 |
| **serde/serde_json** | Manifest JSON 序列化 |

---

### 核心模块：`core/src/`

#### `lib.rs` — 公开 API 入口

```rust
pub mod logical_plan;
pub mod mdl;
pub use datafusion::arrow::*;
pub use logical_plan::error::WrenError;
pub use mdl::AnalyzedWrenMDL;
```

#### `mdl/` — MDL 处理（最核心模块，4063 行）

| 文件 | 职责 |
|------|------|
| `mod.rs` | `WrenMDL`、`AnalyzedWrenMDL`、`transform_sql()` 主流程 |
| `context.rs` | `apply_wren_on_ctx()` — 向 DataFusion SessionContext 注册 Wren 规则 |
| `lineage.rs` | 列级血缘追踪，构建计算列依赖 DAG |
| `dataset.rs` | `Dataset` 枚举（`Model | Metric`） |
| `type_planner.rs` | 类型推断与强制转换 |
| `utils.rs` | 辅助函数（`quoted()`、`to_field()` 等） |
| `function/` | 按方言分类的函数定义（scalar / aggregate / window / table） |
| `dialect/` | SQL 方言实现（wren_dialect、inner_dialect） |

**关键结构体**：

```rust
// WrenMDL：核心语义数据结构
pub struct WrenMDL {
    manifest: Arc<Manifest>,
    qualified_references: HashMap<Column, ColumnReference>, // Column → 物理位置映射
    register_tables: HashMap<String, Arc<dyn TableProvider>>,
    catalog_schema_prefix: Option<String>,
}

// AnalyzedWrenMDL：带血缘分析的包装
pub struct AnalyzedWrenMDL {
    wren_mdl: Arc<WrenMDL>,
    lineage: Arc<Lineage>, // 计算列依赖 DAG
}
```

**主要 API 函数**（`mdl/mod.rs`）：

```rust
// 分析 MDL Manifest，构建符号表 + 血缘
AnalyzedWrenMDL::analyze(manifest, properties, mode) -> Result<Self>

// 应用 Wren 规则到 DataFusion SessionContext（异步）
apply_wren_on_ctx(ctx, analyzed_mdl, properties) -> Result<()>

// SQL 转换（同步包装）
transform_sql(ctx, sql) -> Result<String>

// SQL 转换（完整异步版）
transform_sql_with_ctx(ctx, analyzed_mdl, properties, sql) -> Result<String>
```

#### `logical_plan/` — 分析规则与优化

```
logical_plan/
├── analyze/
│   ├── model_anlayze.rs      # ModelAnalyzeRule（核心分析规则）
│   ├── plan.rs               # ModelPlanNode 定义
│   ├── relation_chain.rs     # 关系链解析（JOIN 推导）
│   ├── expand_view.rs        # View 展开
│   ├── access_control.rs     # RLAC / CLAC 访问控制验证
│   └── scope.rs              # 变量作用域追踪
└── optimize/
    ├── type_coercion.rs      # 类型强制转换
    └── simplify_timestamp.rs # Timestamp 表达式简化
```

**`ModelAnalyzeRule`** 是最核心的分析规则，三阶段处理：

1. **Scope 分析**：bottom-up 遍历查询树，收集每张表需要的列
2. **模型生成**：将 `TableScan` 节点转换为 `ModelPlanNode`，解析关系链（自动推导 JOIN）
3. **Schema 规范化**：移除 Wren catalog/schema 前缀

---

## 三、MDL 查询转换完整流程

```
1. 输入 SQL
        ↓
2. WrenMDL::new(manifest)
   → 构建 qualified_references 映射（Column → ColumnReference）
   → 设置 catalog/schema 上下文
        ↓
3. Lineage::new(&wren_mdl)
   → 追踪计算列依赖，构建有向无环图
        ↓
4. apply_wren_on_ctx() [异步]
   → 注册分析规则：ModelAnalyzeRule, ExpandWrenViewRule, ModelGenerationRule
   → 注册优化规则：TypeCoercion, TimestampSimplify
        ↓
5. DataFusion 解析 SQL → LogicalPlan
        ↓
6. ModelAnalyzeRule::analyze() [三阶段]
   → TableScan(model_name) → ModelPlanNode
   → 关系链 → JOIN 子查询自动生成
   → 计算列 → CASE / CTE 表达式
   → 访问控制规则 → WHERE 谓词注入
        ↓
7. 优化 Pass（类型强制、Timestamp 简化）
        ↓
8. Unparser → 目标 SQL 字符串
        ↓
9. 输出给数据源执行
```

---

## 四、wren-core-py Python 绑定

### 目录结构

```
wren-core-py/
├── Cargo.toml              # crate-type = ["cdylib"]，pyo3 = "0.26.0"，abi3-py311
├── pyproject.toml          # Maturin 配置，module-name = "wren_core"
├── build.rs                # pyo3_build_config 链接参数
├── justfile                # 开发命令
├── src/
│   ├── lib.rs              # PyO3 模块入口，注册所有类和函数
│   ├── context.rs          # PySessionContext（核心类）
│   ├── manifest.rs         # Manifest 序列化/反序列化
│   ├── extractor.rs        # PyManifestExtractor
│   ├── remote_functions.rs # PyRemoteFunction
│   ├── validation.rs       # 访问控制验证函数
│   └── errors.rs           # Rust 错误 → Python 异常转换
└── tests/
    ├── test_modeling_core.py  # Python 单元测试
    └── functions.csv          # 测试用远程函数定义
```

### 暴露给 Python 的完整 API

#### `PySessionContext` — 核心类

```python
from wren_core import PySessionContext

# 创建 Session（传入 base64 编码的 MDL JSON）
ctx = PySessionContext(
    mdl_base64="...",            # base64 编码的 Manifest JSON（必填）
    remote_functions_path=None,  # 远程函数 CSV 路径（可选）
    properties=None,             # Session 属性列表（可选）
    data_source=None             # 数据源类型字符串（可选）
)

# SQL 转换（核心功能）：语义 SQL → 目标 SQL
planned_sql = ctx.transform_sql("SELECT * FROM orders")

# 查询可用函数
all_functions = ctx.get_available_functions()
specific = ctx.get_available_function("ARRAY_AGG")

# LIMIT 下推（生成带 LIMIT 的 SQL）
sql_with_limit = ctx.pushdown_limit(sql, limit=100)
```

#### `PyManifestExtractor` — Manifest 裁剪工具

```python
from wren_core import PyManifestExtractor

extractor = PyManifestExtractor(mdl_base64="...")

# 从 SQL 中自动提取用到的表名
tables = extractor.resolve_used_table_names(
    "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id"
)
# → ["orders", "customers"]

# 按实际用到的数据集裁剪 Manifest（减少传输体积）
sub_manifest = extractor.extract_by(["orders", "customers"])
```

#### 辅助函数

```python
from wren_core import to_json_base64, to_manifest, is_backward_compatible, validate_rlac_rule

# Manifest 反序列化（base64 JSON → Python 对象）
manifest = to_manifest(mdl_base64_str)

# Manifest 序列化（Python 对象 → base64 JSON）
base64_str = to_json_base64(manifest)

# 兼容性检查（是否使用了 v3-only 的访问控制特性）
compatible = is_backward_compatible(mdl_base64_str)

# 验证 RLAC 行级访问控制规则
validate_rlac_rule(rule=rlac_obj, model=model_obj)
```

### 数据类型对照

| Python 类 | Rust 对应 | 说明 |
|-----------|-----------|------|
| `Manifest` | `Manifest` (wren-core-base) | 顶层 MDL 定义 |
| `Model` | `Model` | 单个数据模型 |
| `RowLevelAccessControl` | `RowLevelAccessControl` | 行级访问控制规则 |
| `SessionProperty` | `SessionProperty` | Key-Value Session 属性 |
| `PyRemoteFunction` | `RemoteFunction` | 远程函数元数据 |

---

## 五、快速上手：如何跑测试

### 5.1 wren-core Rust 测试

```bash
cd wren-core

# 设置足够的栈大小（Rust 递归较深）
export RUST_MIN_STACK=8388608

# 单元测试（嵌入在 mdl/mod.rs 中的 #[test]）
cargo test --lib

# 所有测试（含 integration）
cargo test --lib --tests --bins

# SQL 逻辑测试（.slt 文件驱动的端到端测试）
cargo test --test sqllogictests

# 跑指定测试文件
cargo test --test sqllogictests -- model   # 测试 model.slt
cargo test --test sqllogictests -- tpch    # 测试 TPC-H 查询
```

### 5.2 wren-core-py Python 绑定测试

```bash
cd wren-core-py

# 第一步：安装 Python 依赖
just install        # 等价于 poetry install

# 第二步：编译 Rust 扩展（开发模式，不优化）
just develop        # 等价于 maturin develop

# Rust 单元测试（--no-default-features 禁用 extension-module 特性）
just test-rs        # 等价于 cargo test --no-default-features

# Python 测试（需先执行 just develop）
just test-py        # 等价于 poetry run pytest

# 全部测试
just test
```

> **注意**：`just develop` 必须在 `just test-py` 之前执行，否则 Python import 找不到编译好的 `.so` / `.dylib`。

### 5.3 从零快速验证

```bash
# 前置工具：Rust toolchain, Poetry, maturin
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
pip install maturin

cd wren-core-py
poetry install
maturin develop
poetry run pytest tests/
```

---

## 六、在自己代码中使用 wren-core-py

### 最小示例

```python
import base64
import json
from wren_core import PySessionContext

# 1. 构造 MDL Manifest（JSON 格式）
manifest = {
    "catalog": "my_catalog",
    "schema": "my_schema",
    "models": [
        {
            "name": "orders",
            "tableReference": {
                "catalog": "my_catalog",
                "schema": "public",
                "table": "orders"
            },
            "columns": [
                {"name": "order_id", "type": "integer", "isCalculated": False},
                {"name": "customer_id", "type": "integer", "isCalculated": False},
                {"name": "amount", "type": "double", "isCalculated": False},
            ],
            "primaryKey": "order_id"
        }
    ],
    "relationships": [],
    "metrics": [],
    "views": [],
    "dataSource": "DUCKDB"
}

# 2. 编码为 base64
mdl_base64 = base64.b64encode(json.dumps(manifest).encode()).decode()

# 3. 创建 Session
ctx = PySessionContext(mdl_base64=mdl_base64)

# 4. 转换 SQL
result = ctx.transform_sql("SELECT order_id, amount FROM orders WHERE amount > 100")
print(result)
# → SELECT orders.order_id, orders.amount
#   FROM my_catalog.public.orders AS orders
#   WHERE orders.amount > 100
```

### 使用关系（Relationship）的示例

```python
manifest = {
    "catalog": "wren",
    "schema": "public",
    "models": [
        {
            "name": "orders",
            "tableReference": {"catalog": "wren", "schema": "public", "table": "orders"},
            "columns": [
                {"name": "order_id", "type": "integer", "isCalculated": False},
                {"name": "customer_id", "type": "integer", "isCalculated": False},
                # 计算列：跨模型引用（需要 relationship）
                {
                    "name": "customer_name",
                    "type": "varchar",
                    "isCalculated": True,
                    "expression": "customers.name"
                }
            ],
            "primaryKey": "order_id"
        },
        {
            "name": "customers",
            "tableReference": {"catalog": "wren", "schema": "public", "table": "customers"},
            "columns": [
                {"name": "id", "type": "integer", "isCalculated": False},
                {"name": "name", "type": "varchar", "isCalculated": False},
            ],
            "primaryKey": "id"
        }
    ],
    "relationships": [
        {
            "name": "orders_customers",
            "models": ["orders", "customers"],
            "joinType": "MANY_TO_ONE",
            "condition": "orders.customer_id = customers.id"
        }
    ],
    "metrics": [],
    "views": [],
    "dataSource": "DUCKDB"
}

mdl_base64 = base64.b64encode(json.dumps(manifest).encode()).decode()
ctx = PySessionContext(mdl_base64=mdl_base64)

# wren-core 自动推导 JOIN
result = ctx.transform_sql("SELECT order_id, customer_name FROM orders")
print(result)
# → SELECT orders.order_id, customers.name AS customer_name
#   FROM wren.public.orders AS orders
#   LEFT JOIN wren.public.customers AS customers
#     ON orders.customer_id = customers.id
```

---

## 七、关键设计决策

| 设计点 | 说明 |
|--------|------|
| **Stable ABI** | `abi3-py311`，一个 wheel 文件支持 Python 3.11+ 所有版本 |
| **DataFusion fork** | Canner 维护，在上游基础上扩展了 Wren 特定逻辑 |
| **Arc 共享状态** | `WrenMDL`、`AnalyzedWrenMDL` 用 `Arc` 包装，线程安全 |
| **异步核心** | `transform_sql_with_ctx` 是 async，Python 层提供同步包装 |
| **Snapshot 测试** | 使用 `insta` 做 Rust 回归测试，`.snap` 文件记录期望输出 |
| **访问控制** | RLAC（行级）和 CLAC（列级）在分析阶段注入 WHERE/SELECT 谓词 |

---

## 八、已知限制

**`ModelAnalyzeRule` 不支持相关子查询的外层列引用**

在相关子查询（correlated subquery）中，分析规则只能看到子查询自身的表作用域，无法解析外层 SELECT 的列引用。

受影响的 TPC-H 查询：**Q2、Q4、Q15、Q17、Q20、Q21、Q22**

详见：`ibis-server/tests/routers/v3/connector/clickhouse/TPCH_ISSUES.md`

---

## 九、相关资源

- `wren-core/core/src/mdl/mod.rs` — MDL 核心实现（4063 行）
- `wren-core/core/src/logical_plan/analyze/model_anlayze.rs` — ModelAnalyzeRule
- `wren-core-py/src/context.rs` — PySessionContext 实现
- `wren-core-py/tests/test_modeling_core.py` — Python 绑定测试示例
- `wren-core/sqllogictest/test_files/` — SQL 端到端测试用例
- `wren-core-base/src/mdl/manifest.rs` — Manifest / Model / Column 类型定义
