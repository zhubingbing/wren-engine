# 开发环境搭建与 Demo 运行指南

本文档覆盖从零开始搭建开发环境、启动所有组件、验证各层功能的完整步骤。

---

## 一、前置工具安装

### 1.1 Rust 工具链

```bash
# 安装 rustup（如已安装跳过）
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"

# 验证
rustc --version    # 1.94.1+
cargo --version
```

### 1.2 Python 工具

```bash
# Python 3.11（必须，ibis-server 要求 >=3.11,<3.12）
python3 --version  # 3.11.x

# Poetry（wren-core-py、ibis-server 用）
pip install poetry
poetry --version   # 2.x

# just（任务运行器，替代 make）
brew install just   # macOS
# 或 cargo install just
just --version
```

### 1.3 可选：taplo（TOML 格式化）

```bash
cargo install taplo-cli
```

---

## 分支协作约定

本仓库建议固定使用下面这套分支职责：

- `upstream/main`：社区主线，来自 `Canner/wren-engine`
- `main`：本地社区镜像分支，只用于同步 `upstream/main`
- `origin/dev-bing`：你的个人远端开发分支
- `dev-bing`：你的本地长期开发分支

约定上：

- 不要把个人开发提交直接放到 `main`
- `main` 只做 `upstream/main` 的快进同步
- 日常开发、提交、推送都在 `dev-bing`
- 社区有更新时，先同步 `main`，再把 `main` 合到 `dev-bing`

### 固化命令

仓库根目录提供了脚本：`scripts/branch-flow.sh`

```bash
# 查看当前分支和跟踪关系
./scripts/branch-flow.sh status

# 同步本地 main 到 upstream/main
./scripts/branch-flow.sh sync-main

# 同步社区主线并合并到 dev-bing
./scripts/branch-flow.sh sync-dev

# 同步社区主线、合并到 dev-bing、并推送到 origin/dev-bing
./scripts/branch-flow.sh sync-dev --push
```

脚本默认使用下面的分支和远端：

```bash
UPSTREAM_REMOTE=upstream
ORIGIN_REMOTE=origin
MAIN_BRANCH=main
DEV_BRANCH=dev-bing
```

如果以后你改了分支名，也可以临时覆盖：

```bash
DEV_BRANCH=my-dev ./scripts/branch-flow.sh sync-dev --push
```

### 脚本行为说明

- 执行前会检查工作区是否干净，避免在半途切分支
- `sync-main` 会执行 `git fetch upstream` 和 `git merge --ff-only upstream/main`
- `sync-dev` 会先同步 `main`，再执行 `git merge main` 到 `dev-bing`
- `sync-dev --push` 会把合并后的 `dev-bing` 推到 `origin/dev-bing`

---

## 二、wren-core-py：Rust 语义引擎 Python 绑定

这是最核心的模块。后续所有 Demo 和 ibis-server 都依赖它编译出的 `.so` 文件。

### 2.1 安装 Python 依赖

```bash
cd wren-core-py
just install
# 等价于：poetry install --no-root
# 会创建 .venv/，安装 maturin、pytest 等
```

### 2.2 编译 Rust 扩展（开发模式）

```bash
just develop
# 等价于：poetry run maturin develop
# 编译 wren-core Rust 代码，产出 .venv/lib/python3.11/site-packages/wren_core/wren_core.abi3.so
```

**编译时间**：首次约 3~5 分钟（Rust 全量编译），后续增量编译约 10~30 秒。

**验证编译成功**：

```bash
.venv/bin/python -c "from wren_core import SessionContext; print('OK')"
# → OK
```

### 2.3 额外安装 memory 和 LLM 依赖

`demo_nl_sql.py`、`demo_nl_sql_e2e.py`、`chat.py` 需要 LanceDB、sentence-transformers、openai。
这些**不在** `pyproject.toml` 的 dev 依赖里，需手动安装到 `.venv`：

```bash
# 进入 wren-core-py 目录
.venv/bin/pip install lancedb>=0.6 sentence-transformers>=2.2 openai>=2.0

# 当前已验证版本
# lancedb               0.30.2
# sentence-transformers 5.4.1
# transformers          5.5.4
# torch                 2.11.0（sentence-transformers 拉取的依赖）
# openai                2.32.0
```

> **注意**：`sentence-transformers` 会附带安装 `torch`，体积约 2~3 GB，首次安装较慢。

**验证 memory 可用**：

```bash
.venv/bin/python -c "from wren.memory.store import MemoryStore; print('memory OK')"
```

### 2.4 运行 Rust 单元测试

```bash
just test-rs
# 等价于：cargo test --no-default-features
# --no-default-features 禁用 pyo3/extension-module，让纯 Rust 测试可以链接
```

### 2.5 运行 Python 单元测试

```bash
just test-py
# 等价于：poetry run pytest
# 需要先执行 just develop，否则 import wren_core 会报错
```

---

## 三、ibis-server：FastAPI 查询服务

ibis-server 对接 wren-core-py 和各数据源（ClickHouse、Postgres 等）。

### 3.1 安装依赖

```bash
cd ibis-server
just install
# 会自动：
#   1. cd ../wren-core-py && just install && just build（编译 release wheel）
#   2. poetry install（安装 FastAPI、ibis-framework 等）
#   3. pip install wren-core-py wheel
```

### 3.2 启动开发服务器

**方式一（推荐）：使用 wren-core-py 的 venv，避免维护两套环境**

```bash
cd ibis-server
QUERY_CACHE_STORAGE_TYPE=local \
    ../wren-core-py/.venv/bin/python -m fastapi dev --port 8100
```

等价于 `just dev-unified`（justfile 中已定义）：

```bash
just dev-unified
# 绑定 justfile 中的 port=8000，改端口需传参：just dev-unified port=8100
```

**方式二：ibis-server 自己的 Poetry 环境**

```bash
QUERY_CACHE_STORAGE_TYPE=local just dev
# 等价于：poetry run python -m fastapi dev --port 8000
```

**环境变量说明**：

| 变量 | 值 | 说明 |
|------|-----|------|
| `QUERY_CACHE_STORAGE_TYPE` | `local` | 查询缓存存本地，开发必须设置，否则找不到缓存后端 |

### 3.3 验证 ibis-server 就绪

```bash
curl http://127.0.0.1:8100/health
# → {"status":"ok"}
```

---

## 四、ClickHouse：数据源

Demo 连接的 ClickHouse 实例配置如下（本地测试用）：

| 参数 | 值 |
|------|-----|
| host | localhost |
| HTTP port | 8123 |
| database | k8s_node_metrics |
| user | admin |
| password | admin |

**表结构**：

- `ods_host_metrics_raw` — 主机监控原始数据（EAV 格式，metric_name + metric_value）
- `ods_cmdb_host_snapshot` — CMDB 主机资产快照

---

## 五、Memory 存储（LanceDB）

所有 NL→SQL demo 的 few-shot memory 存储在本地 LanceDB：

```
路径: /mnt/data/wren/.wren/memory/
内容:
  query_history.lance   — NL-SQL 历史对（用于 few-shot 召回）
  schema_items.lance    — schema 向量索引（用于相关列检索）
```

如需清空重置：

```bash
rm -rf /mnt/data/wren/.wren/memory/
# 下次运行 demo 时会自动重建
```

---

## 六、Demo 脚本运行指南

所有 demo 脚本位于 `wren-core-py/tests/`，使用 wren-core-py 的 `.venv` 运行。

### Demo 1 — wren-core transform_sql 核心场景验证

**文件**：`tests/demo_transform.py`

**验证内容**：6 个核心场景——单表查询、计算列展开、关系/JOIN 自动推导、视图展开、LIMIT 下推、ManifestExtractor。

**前置条件**：仅需 wren-core-py 编译完成（`just develop`），无需网络/数据库。

```bash
cd wren-core-py
.venv/bin/python tests/demo_transform.py
```

预期输出（节选）：

```
【单表查询 - SELECT *】
  输入: SELECT * FROM orders
  输出: SELECT orders.order_id, ... FROM shop.public.orders AS orders

【计算列 - 直接查询计算列】
  输入: SELECT order_id, total_price FROM orders
  输出: SELECT orders.order_id, orders.amount * (1 + orders.tax_rate) AS total_price ...
```

---

### Demo 2 — k8s 监控 MDL 设计验证

**文件**：`tests/demo_ck_mdl.py`

**验证内容**：基于 k8s_node_metrics 真实表结构的 MDL 定义，验证 7 个语义 SQL 场景（计算列、视图、关系查询）。

**前置条件**：同 Demo 1，无需网络/数据库。

```bash
cd wren-core-py
.venv/bin/python tests/demo_ck_mdl.py
```

预期输出（节选）：

```
【2. 计算列 - 直接查 cpu_usage（自动展开 CASE WHEN）】
  输入 SQL:    SELECT hostname, event_time, cpu_usage FROM host_metrics ...
  转换后:    SELECT host_metrics.hostname, ...,
               CASE WHEN host_metrics.metric_name = 'cpu_usage' THEN host_metrics.metric_value END AS cpu_usage
               FROM k8s_node_metrics.ods_host_metrics_raw AS host_metrics ...
```

---

### Demo 3 — 端到端查询（语义 SQL → ClickHouse 执行）

**文件**：`tests/demo_e2e.py`

**验证内容**：6 个 Case——AVG 聚合、WHERE 过滤计算列、JOIN 关联查询、视图查询、dry-plan 查看物理 SQL。

**前置条件**：
- ibis-server 已启动（`http://127.0.0.1:8100`）
- ClickHouse 可访问

```bash
cd wren-core-py
.venv/bin/python tests/demo_e2e.py
```

预期输出（节选）：

```
======================================================================
  Case 1 — 各主机 CPU 平均使用率（降序）

  语义 SQL: SELECT hostname, AVG(cpu_usage) AS avg_cpu ...

  hostname              avg_cpu
  --------------------  -------
  c7h12-t2-163-109      43.21
  ...
  共 12 行
```

---

### Demo 4 — Memory 工作流（建索引 → recall → store）

**文件**：`tests/demo_nl_sql.py`

**验证内容**：8 步完整 memory 工作流：建索引、seed NL-SQL 生成、召回、schema 上下文获取、transform_sql 验证、存储、再次召回验证、状态查看。

**前置条件**：
- wren-core-py 编译完成
- LanceDB、sentence-transformers 已安装（见第二节 2.3）

```bash
cd wren-core-py
TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
    .venv/bin/python tests/demo_nl_sql.py
```

> `TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1` — 禁止向 HuggingFace 发出网络请求，使用本地已缓存的 sentence-transformers 模型。如果模型未缓存会报错，需先联网下载一次（去掉这两个变量运行）。

预期输出（节选）：

```
步骤 1 — 建立 schema 索引 + seed NL-SQL 对
  索引完成: 12 schema items, 6 seed queries
  存储路径: /mnt/data/wren/.wren/memory

步骤 3 — 召回相似历史 SQL
  NL: '各主机的 CPU 使用率'
    ↳ [seed] 各主机平均 CPU 使用率 → SELECT hostname, AVG(cpu_usage) ...
```

---

### Demo 5 — 完整 NL→SQL 端到端（含 LLM）

**文件**：`tests/demo_nl_sql_e2e.py`

**验证内容**：4 个测试问题的完整链路——recall → schema ctx → LLM → dry-plan → ClickHouse 执行 → memory store。

**前置条件**：
- ibis-server 已启动
- ClickHouse 可访问
- LLM 代理可访问（`https://claudecc.top/v1`，model: `gpt-5.4`）
- LanceDB、sentence-transformers、openai 已安装

```bash
cd wren-core-py
TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
    .venv/bin/python tests/demo_nl_sql_e2e.py
```

测试问题：
1. 各主机的 CPU 平均使用率，按降序排列
2. 内存使用率超过 50% 的主机有哪些
3. 按 IDC 统计有效主机数量
4. CPU 最高的主机来自哪个 IDC

预期输出（节选）：

```
  问题   : 各主机的 CPU 平均使用率，按降序排列
  few-shot: 1 条历史 SQL 参考

  语义 SQL :
    SELECT hostname, AVG(cpu_usage) AS avg_cpu FROM host_metrics GROUP BY hostname ORDER BY avg_cpu DESC

  物理 SQL :
    SELECT host_metrics.hostname, avg(CASE WHEN ...) AS avg_cpu FROM ...

  执行结果:
  hostname              avg_cpu
  --------------------  -------
  c7h12-t2-163-109      43.21
```

---

### Demo 6 — 交互式对话终端

**文件**：`tests/chat.py`

**验证内容**：交互式 NL→SQL 终端，支持持续提问，每次成功查询自动存入 memory 作为下次 few-shot。

**前置条件**：同 Demo 5。

```bash
cd wren-core-py
TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
    .venv/bin/python tests/chat.py
```

内置命令：

| 命令 | 说明 |
|------|------|
| `/help` | 显示示例问题 |
| `/sql` | 显示上一次的语义 SQL 和物理 SQL |
| `/quit` 或 `/exit` | 退出 |

示例交互：

```
你: 各主机的 CPU 平均使用率，按降序排列
  +--------------------+---------+
  | hostname           | avg_cpu |
  +--------------------+---------+
  | c7h12-t2-163-109   | 43.21   |
  ...
  共 12 行（显示前 20 行）

你: /sql
  语义 SQL:
    SELECT hostname, AVG(cpu_usage) AS avg_cpu FROM host_metrics GROUP BY hostname ORDER BY avg_cpu DESC
  物理 SQL:
    SELECT host_metrics.hostname, avg(CASE WHEN ...) AS avg_cpu FROM k8s_node_metrics.ods_host_metrics_raw AS host_metrics ...
```

---

## 七、组件启动顺序总结

依赖关系：`ClickHouse` ← `ibis-server` ← `Demo 3/5/6`

```
1. ClickHouse（localhost:8123）       # 独立运行，通常已常驻
2. ibis-server（:8100）               # 依赖 ClickHouse
3. Demo 脚本（按需运行）
```

**快速启动 ibis-server**：

```bash
cd ibis-server
QUERY_CACHE_STORAGE_TYPE=local \
    ../wren-core-py/.venv/bin/python -m fastapi dev --port 8100
```

---

## 八、常见问题

### Q: `just develop` 失败，提示链接器找不到 Python 库

```bash
# macOS 上补全 OpenSSL 路径（有时 psycopg2 需要）
LDFLAGS="-L$(brew --prefix openssl)/lib" \
CPPFLAGS="-I$(brew --prefix openssl)/include" \
    just install
```

### Q: `from wren_core import SessionContext` 报 ImportError

先确认已执行 `just develop`：

```bash
ls .venv/lib/python3.11/site-packages/wren_core/wren_core.abi3.so
# 文件存在则编译成功，否则重新执行 just develop
```

### Q: sentence-transformers 运行时尝试联网下载模型

加上 `TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1` 前缀。如果模型本地不存在，先去掉这两个变量运行一次让它下载，之后加上即可离线使用。

### Q: ibis-server 启动报 `wren_core` 找不到

用 `../wren-core-py/.venv/bin/python` 启动（而不是系统 python 或 ibis-server 自己的 poetry env），确保使用的是装了 `wren_core.abi3.so` 的那个 venv。

### Q: `QUERY_CACHE_STORAGE_TYPE` 未设置导致 ibis-server 启动失败

开发环境必须设置 `QUERY_CACHE_STORAGE_TYPE=local`，生产环境可配置 Redis 或 S3。
