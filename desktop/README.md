# OfferFlow 桌面版

> OfferFlow 的 **PySide6 (Qt 6) 桌面客户端**。UI 与网页版对齐，但**五大 AI 能力（简历生成 / 简历优化 / 模拟面试 / 岗位匹配 / 薪资谈判）默认在本机跑**——自带 API Key（BYOK），不依赖服务端；本地模型不可用时才回落云端。

## 与网页版的关系

| | 网页版 | 桌面版（本项目） |
|---|---|---|
| 界面 | React 19 + Vite | PySide6 (Qt 6) |
| 后端 | FastAPI + LangGraph（`../web`） | 同一套后端，经 HTTP + Bearer Token 调用 |
| 数据库 | 服务端 PostgreSQL | 服务端为准 + 本机可写副本 `biz_local.db`（含离线 outbox） |
| AI 落点 | 服务端调用 | **默认本机**（快、离线可用、Key 不出本机） |
| 交付 | 浏览器 | 双击即用 + 自动增量更新 |

桌面版即使**不连后端**也能用：本地 AI 层 + 本地库构成完整闭环，接上后端只是为了多端同步。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt          # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux

.venv\Scripts\python main.py
```

首次启动 → 左侧 **模型设置** → 选预设（推荐 DeepSeek）→ 填 API Key → 测试连接 → 保存。

### 连接自建后端

默认连 `http://localhost:8001`（即 `../web` 里 `docker compose up` 起的后端）。指向别处：

```bash
set OFFERFLOW_CLOUD_URL=https://your-host        # Windows
export OFFERFLOW_CLOUD_URL=https://your-host     # macOS / Linux
```

也可以在应用内「设置 → 云端地址」改（存本机 SQLite 的 `cloud.base_url`）。

## 功能

- **简历生成**：结构化卡片输入（16 个求职方向模板 + 推荐技能点击添加）→ 一页内 800 字中文简历；字数硬约束；事实保真校验；逐段「换 3 个写法」多候选。
- **岗位分析**：JD 粘贴 / 文件导入（PDF/Word/文本，本地解析）/ **截图 OCR**（Windows 系统 OCR）；匹配度 = 本地技能词典 + JD 结构化需求混合评分。
- **简历优化**：差距分析驱动的整份重写；硬事实不丢失否则回退原文；一页约束裁剪；前后 diff；多版本；导出 Word / 纯文本。
- **面试准备**：面经库自动查重；出题（技术/项目/行为 + 参考答案 + 疑似编造数字标记）；多轮模拟面试。
- **薪资谈判**：规则表算市场价（学历 × 方向 × 城市 × 经验）+ **开口价 / 目标价 / 底线价三档**；4 场景谈判演练，AI HR 压价 + 教练点评。
- **投递追踪**：九态看板（支持拖拽改状态）、时间线、转化率统计、逾期/面试提醒。
- **离线能力**：文档解析、截图 OCR、本地向量检索（fastembed + bge-small-zh）、本地知识图谱与长期记忆。

## 本地 AI 层

```
offerflow_desktop/local_ai/
├── index.db        # 向量索引（fastembed, bge-small-zh 中文向量）
├── knowledge.db    # 知识图谱 + 长期记忆
└── cache/          # 各类数据的拉取快照
```

检索走统一入口 `local_source_of()` 分流「材料池（简历原文）」与「成果池（生成稿/优化稿）」，成果池有质量闸门（不合格降级回材料池）与回声防护（避免自己召回自己）。

> **首次运行注意**：中文向量模型 `BAAI/bge-small-zh-v1.5`（约 90MB）未随仓库分发。首次使用语义检索时会由 `fastembed` 自动下载到缓存目录；若网络受限，可手动把模型放到 `models/fast-bge-small-zh-v1.5/` 下免联网。**只跑单元测试时不影响**——需要模型的用例会跳过或较慢。

## 测试

```bash
.venv\Scripts\python tests/run_all.py
```

`tests/_mutate_*.py` 是**反向对照脚本**：逐个把关键判据改坏，确认对应用例真的变红。用来防「测试恒绿但其实没在验」——这是这个项目里最常踩的坑。

> 部分用例会跨目录读取 `../web/backend` 源码做「桌面实现 vs 服务端实现」的一致性对照；未 clone 网页版时这些用例会自动 skip。

## 打包发版

```bash
.venv\Scripts\python -m PyInstaller OfferFlow.spec --noconfirm --clean
```

再用 Inno Setup 编译 `installer/OfferFlow.iss` 得到安装包。增量更新由 `release/version.json` 驱动（版本号 / 更新说明 / 安装包地址 + sha256）。

## 目录

```
desktop/
├── main.py
├── offerflow_desktop/
│   ├── core/          # 生成守门层、事实保真、排版、技能词典、薪资基准…
│   ├── services/      # 简历生成/优化、面试、薪资谈判、语义检索
│   ├── local_ai/      # 向量索引 + 知识图谱 + 本地记忆
│   ├── ui/pages/      # 各功能页面
│   └── api_client.py  # 后端契约层（与本地实现同签名，UI 无感切换）
├── docs/模板预览/      # 导出效果示例
├── installer/         # Inno Setup 脚本
└── tests/
```
