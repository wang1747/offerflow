# OfferFlow

求职行动编排系统：解析 JD / 简历 / 投递记录，给出下一步行动建议；对「改业务数据」类操作强制人工确认后才执行。后端 FastAPI + LangGraph，前端 React + Vite，Docker Compose 编排（Postgres + Chroma）。

> 这是 OfferFlow 的**网页版**。三端总览与整体设计见仓库根 [README](../README.md)。

---

## 1. 功能特性

- **多模块求职工作流**：JD 分析、简历生成/优化、岗位匹配、面试准备、薪资谈判、投递追踪、交流中心、管理后台。
- **求职 Agent（v0.2-beta）**：
  - 只读切片读取 JD / 简历 / 匹配结果，产出结构化决策（`apply_now` / `optimize_first` / `collect_evidence` / `wait` / `skip`）与带证据引用的行动建议。
  - **确认写入**：Agent 提议修改投递状态时，先经 LangGraph `interrupt()` 暂停，用户在前端点「确认执行」才真正写库；拒绝/过期/不确认都不会改动业务数据。
  - **长期记忆**：Agent 从上下文推断的偏好先落 `proposed`，用户确认后才进 `confirmed` 并进入向量索引被召回；用户也可直接添加记忆。
  - **会话可恢复**：生产用 `PostgresSaver` 作 Checkpointer，确认断点与会话状态跨服务重启仍可恢复。
- **BYOK 架构**：LLM（DeepSeek）由用户自带 Key，服务端只做编排与存储，不绑定特定供应商。
- **可观测**：每次运行返回工具调用轨迹、耗时、证据 ref、Checkpointer 类型；前端可查看会话运行历史（trace）。
- **安全**：JWT / 字段级加密、默认密钥禁止上线、Postgres 仅容器内可达、日志轮转。

---

## 2. 技术栈

| 层 | 选型 |
|----|------|
| 前端 | React 19 + Vite + TypeScript，nginx 反代 `/api` |
| 后端 | FastAPI + SQLAlchemy 2.0（同步 ORM）+ Pydantic v2 |
| Agent | LangGraph 1.2 + langgraph-checkpoint-postgres（PostgresSaver）+ psycopg v3 |
| 数据库 | PostgreSQL 16（业务数据 + Checkpointer） |
| 向量库 | ChromaDB（语义检索 / 长期记忆索引） |
| 部署 | Docker Compose（nginx 反代 `/api`） |
| 测试 | pytest（后端 216 passed，含 Agent 评测） |

---

## 3. 架构图

```
                        ┌─────────────────────────────┐
   浏览器 / 用户  ─────► │   offerflow-frontend        │
   (React + Vite)       │   nginx :80 / :443         │
                        │   · 静态 SPA                │
                        │   · /api → 反代到 backend   │
                        └──────────────┬──────────────┘
                                       │  /api/v1/*
                                       ▼
                        ┌─────────────────────────────┐
                        │   offerflow-backend         │
                        │   FastAPI (uvicorn :8001)   │
                        │                             │
                        │  ┌─────────── 业务模块 ───────────┐
                        │  │ auth · jd · resume · match    │
                        │  │ application · interview ·      │
                        │  │ salary_negotiation · corpus ·  │
                        │  │ community · admin · model_cfg  │
                        │  └───────────────────────────────┘
                        │                             │
                        │  ┌────── 求职 Agent (v0.2-beta) ──┐
                        │  │ /api/v1/agent/*               │
                        │  │  · sessions / messages        │
                        │  │  · actions (确认写操作)        │
                        │  │  · memories (长期记忆)         │
                        │  │                              │
                        │  │  LangGraph Graph             │
                        │  │  load_context → recall_memory │
                        │  │  → select_read_tools →        │
                        │  │    execute_read_tools →       │
                        │  │  summarize → compose →        │
                        │  │  validate → (写?) → finalize  │
                        │  │       │                      │
                        │  │       └─ interrupt() 等确认   │
                        │  │            ↓ Command(resume)  │
                        │  │         execute_write_action  │
                        │  │                              │
                        │  │  Checkpointer: PostgresSaver  │
                        │  │  LLM: DeepSeek (BYOK)         │
                        │  │  Tools: jd.get / resume.get / │
                        │  │         match.get /           │
                        │  │         application.update_status│
                        │  └──────────────────────────────┘
                        └───────┬───────────────┬──────────┘
                                │               │
                  ┌─────────────▼──┐    ┌────────▼────────┐
                  │  PostgreSQL 16 │    │  ChromaDB       │
                  │  db: offferflow│    │  chroma_db      │
                  │  · 业务表      │    │  · 语义向量     │
                  │  · agent_*     │    │  · agent_memory │
                  │  · checkpoint  │    │    (长期记忆)   │
                  └────────────────┘    └─────────────────┘
```

**要点**
- 写工具（`application.update_status`）**不**出现在模型的 function-calling 清单里，只能经「确认流程」执行（先 `prepare_write_action` 落 `ActionIntent`，`interrupt()` 暂停，用户确认后 `execute_write_action`）。
- 模型调用失败 / 输出校验失败都会如实返回结构化错误，不会把失败伪装成成功。
- 同一会话同一时刻只允许一条消息执行（进程内锁）。

---

## 4. 目录结构

```
web/
├── backend/                     # FastAPI 后端（app/ 源码 + tests/）
│   ├── app/
│   │   ├── main.py              # FastAPI 入口、路由注册、lifespan（建表/默认用户/Agent 运行时）
│   │   ├── config.py            # Settings（读 DATABASE_URL 等环境变量）
│   │   ├── core/                # llm / database / embeddings / vector_store / concurrency ...
│   │   ├── models/              # SQLAlchemy 模型（含 agent_session / agent_runs / agent_action / agent_memory）
│   │   ├── modules/
│   │   │   ├── career_agent/    # 求职 Agent：graph / routes / catalog / memory / checkpoint
│   │   │   ├── jd/ resume/ match/ application/ ...   # 业务模块
│   │   └── schemas/             # Pydantic 出入参
│   └── tests/                   # 后端测试（216 passed）
├── frontend/                    # React + Vite 前端
│   ├── Dockerfile               # 多阶段构建（node build → nginx 静态托管）
│   └── src/
│       ├── pages/AgentConsole.tsx   # 求职 Agent 页面（确认卡 + 记忆面板）
│       ├── agent/openCareerAgent.ts # 业务页进入 Agent 的唯一入口
│       └── modules.tsx              # 侧边栏/路由注册（含 /agent）
├── scripts/                     # 数据导入（题库 / 语料 / 薪资基准）
├── docker-compose.yml           # 编排（backend + frontend + n8n，可选 Postgres profile）
├── Dockerfile                   # 后端镜像
├── .env.example                 # 环境变量模板（cp 成 .env）
├── requirements.txt
└── run.py                       # 本地启动后端（uvicorn）
```

---

## 5. 快速开始（本地开发）

### 5.1 后端

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r ../requirements.txt

# 默认 SQLite；如需连 Postgres 设 DATABASE_URL
export DATABASE_URL="sqlite:///./offerflow.db"
export DEEPSEEK_API_KEY="你的Key"          # 或留空用本地/其他模型
python run.py                              # 监听 :8001
```

健康检查：`curl http://localhost:8001/api/health`

### 5.2 前端

```bash
cd frontend
npm install
npm run dev                               # Vite 开发服务器（默认 :5173）
```

前端通过 `VITE_API_BASE` 或默认 `/api` 访问后端；开发态可让 Vite 代理到 `:8001`。

> 首次使用前需在「设置 → 模型配置」里填写 LLM Key（BYOK），否则 Agent 与生成类功能会提示「模型不可用」。

---

## 6. 自建部署

```bash
cp .env.example .env
# 逐一替换 ENCRYPTION_KEY / JWT_SECRET_KEY / DEEPSEEK_API_KEY（如需服务端默认 Key）
# 生产环境另需改 APP_ENV=production 与 CORS_ORIGINS=你的域名

docker compose up --build -d
```

- 后端首次启动自动 `Base.metadata.create_all()` 建表；Alembic 基线为 `b12887132010`。
- 默认可选 Postgres：`docker compose --profile postgres up -d`（否则走 SQLite，适合单机试跑）。
- **对外提供服务时请自行在反代层配 HTTPS**，并把 `APP_ENV` 设为 `production`——此时默认密钥会被拒绝启动。

---

## 7. 求职 Agent 使用演示

> 入口：登录后左侧「求职 Agent」（路由 `/agent`，需已完成模型配置）。

1. **进入 Agent 页面**
   - 在侧边栏点「求职 Agent」，或任意业务页（JD / 简历 / 投递）通过 `openCareerAgent()` 携带 `jd_id` / `resume_id` / `application_id` 进入（页面只传 ID，不传正文）。
2. **选择上下文并创建会话**
   - 在「选择上下文」里选目标 JD 与简历，点「开始会话」。后端返回 `session_id`（= `thread_id`）、`context_hash`；上下文缺失时不会猜测，会提示补齐。
3. **运行一条消息**
   - 输入如「这个岗位值得投吗？下一步该做什么？」，点「运行」。
   - 返回：决策标签、置信度、结论摘要、行动建议（每条带证据 ref）、工具调用轨迹、可追溯证据。
4. **确认一个写操作（核心）**
   - 若建议里含「改投递状态」，页面出现**待确认的写操作**卡，显示「把 X 的投递状态从 A 改为 B」（A/B 来自服务端 `pending_action.expected_status/new_status`）。
   - 点「确认执行」→ 后端 `POST /sessions/{id}/actions/{action_id}` resume `interrupt()`，真正写库；点「取消」则本轮不写任何数据。
   - 确认后页面显示「已按你的确认写入业务数据」，并落一条 `outcome` 记忆。
   - 若写操作因版本冲突（CAS）、过期或失败而未执行，页面按 `write_failure.code` 给出明确解释（不猜、不兜底）。
5. **长期记忆**
   - Agent 推断的偏好先出现在「待你确认」；点「记入记忆」后进入「已生效的记忆」并被召回。
   - 可直接在底部输入框添加一条记忆（你亲口说的偏好，直接生效）。
6. **查看运行轨迹**
   - 「运行轨迹」区可刷新查看该会话历次运行的 `run_id`、状态、工具调用、耗时；刷新/重启服务后，因 `PostgresSaver` 存在，会话与待确认断点仍可恢复。

### 关键 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/agent/sessions` | 创建/恢复会话（传 `jd_id`/`resume_id`/`application_id`） |
| POST | `/api/v1/agent/sessions/{id}/messages` | 运行一条用户消息 |
| POST | `/api/v1/agent/sessions/{id}/actions/{action_id}` | 确认/拒绝待执行写操作 |
| GET  | `/api/v1/agent/sessions/{id}/actions` | 列出待确认写操作 |
| GET  | `/api/v1/agent/memories` | 列出我的长期记忆 |
| POST | `/api/v1/agent/memories` | 显式添加一条记忆 |
| POST | `/api/v1/agent/memories/{id}/confirm` | 确认一条待确认记忆 |
| GET  | `/api/v1/agent/tools` | 列出 Agent 可用工具 |

---

## 8. 测试

```bash
cd backend
pytest tests/ -q
```

- 后端 **216 passed**（含 Agent 契约 / 图定义 / 真实 PG 跨重启恢复）。
- Agent 评测使用**假模型 + 构造数据**，零额度消耗；真模型只在用户 BYOK 账号上跑。

---

## 9. 已知项 / 注意事项

- **`/api/v1/match/rankings` 历史 500**：`RankingItem.suggestion` 为必填但 `match_results.suggestion` 可为空，存在脏数据时触发。修复方向：`suggestion: Optional[str] = None`。
- **HTTPS / 前端拆分 / 应用层凭据轮换**等为后续迭代项，自建部署时请自行在反代层处理。

---

## 10. 相关仓库

- 仓库根 [README](../README.md) —— 三端总览、架构图与设计取舍。
- [`../desktop`](../desktop) —— PySide6 桌面版（本地 AI 能力在此实现）。
- [`../android`](../android) —— Android WebView 外壳。
