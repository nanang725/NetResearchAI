# NetResearch AI

面向计算机网络领域的科研辅助工具。本仓库用于第九届传智杯 AI 大模型创新应用挑战赛。

## 项目阶段

- **v0.1.0：工程基础与可靠论文检索（已完成）**：提供论文 PDF 管理、解析、混合检索及基础问答能力。
- **v0.2.x：文献管理、PDF 深度解析与大规模文献处理（已完成）**：包含上下文问答、可选按页 OCR、持久化批量导入任务、页级解析检查点与失败页恢复、语义相似候选、版本关系及索引一致性修复。当前仍会将整篇 PDF 内容读入内存，尚未具备端到端有界内存保证。
- **v0.3.0：证据增强科研工作台（开发预览）**：覆盖文献发现、本地引用图谱预览、多文献证据综述、学术大纲与 References、结构化阅读与翻译、CSV 数据分析。当前处于发布前验收阶段，不将独立版本比较页面或完整科研知识图谱列为已完成。
- **v0.4.0：自主科研 Agent（规划中）**：持久化 Planner、白名单工具、受控 Executor 和运行时证据校验。仓库已包含只读 Agent 的提前预览，但不属于 v0.3.0 正式完成目标。
- **v0.5.0：科研知识图谱（规划中）**：跨文献实体、关系、时间演化与完整知识图谱查询。
- **v1.0.0：完整科研 Copilot 与正式发布（规划中）**：完整写作、长期记忆、部署、权限和产品化交付。

现有上下文问答会将最近最多 12 条对话历史随请求传给后端，用于理解依赖上下文的追问，并在需要时轻量改写检索查询。服务端不保存持久化会话状态；这不是完整的长期记忆。基础意图识别分为五类：`paper_qa`（论文问答）、`simple_explanation`（通俗解释）、`comparative_analysis`（比较分析）、`research_assistance`（科研辅助）和 `academic_writing`（学术写作）。

回答可使用两种来源：`paper_evidence` 检索论文并依据筛选后的相关证据回答；`generic_knowledge` 不检索或引用论文证据。多轮对话默认继承上一轮回答的知识来源；用户明确要求根据论文原文或改用通用知识时，按本轮要求切换。论文证据不足时，回答规则要求明确说明，不把缺乏证据的推测包装成论文结论；没有有效相关证据时不会生成论文回答。问答接口使用现有检索与 LLM 服务，不是 LangGraph 工作流。

## 技术栈

- 前端：Vue 3 + TypeScript + Vite + Element Plus
- 后端：Python + FastAPI + SQLite + ChromaDB
- PDF 解析：PyMuPDF（保留页码）
- 检索：BM25、BGE-M3 向量检索与 RRF 融合
- 规划中：v0.4.0 自主科研 Agent 正式版本、v0.5.0 科研知识图谱、v1.0.0 完整科研 Copilot

## 目录结构

```text
NetResearchAI/
├── backend/                 FastAPI 服务
│   ├── app/
│   │   ├── api/             健康检查与论文接口
│   │   ├── core/            统一错误与响应
│   │   ├── models/          SQLAlchemy 模型
│   │   ├── schemas/         Pydantic 结构
│   │   └── services/        PDF、检索、问答业务
│   ├── data/                数据库、上传文件、模型与向量索引（运行时使用）
│   ├── tests/               pytest 自动化测试
│   └── requirements.txt
├── frontend/                Vue 工作台
├── start-backend.bat
├── start-frontend.bat
└── start.bat
```

## 当前开发预览已接入的真实能力

- 后端健康检查 `GET /api/health`
- 论文 PDF 上传、文件校验（扩展名、PDF 文件头、大小、空文件、加密文档）
- PyMuPDF 按页提取文本，并保存章节、参考文献、图像元数据、表格行列和可识别的公式文本；表格作为独立检索块保留页码，公式使用启发式标记且不补造内容。扫描页继续复用 Tesseract OCR 并记录页面状态（上传和解析仍将整个 PDF 内容读入内存）
- PDF 页级持久化检查点：页面状态区分 pending、processing、completed 和 failed；失败后只重试未完成或失败页面，已完成页面跳过文本提取、OCR 和元素提取，避免重复插入页面元素与检索块
- 论文详情结构化阅读：按章节、段落、表格、公式、图像、图注和参考文献展示已解析元素及布局信息；支持单页或单元素中英文双向翻译。翻译结果仅用于辅助阅读，不写入论文数据库或 Chroma，也不参与 RAG 证据检索
- 本地引用图谱：从现有 `reference` 元素提取结构化参考文献，按 DOI、arXiv ID、规范化标题及辅助字段匹配本地论文；仅可靠匹配生成引用边，并保留未匹配和歧义记录。图谱使用 PageRank、入度和本地网络位置识别当前语料中的核心论文
- 多文献证据综述：选择 2～5 篇本地论文后逐篇执行 BM25/BGE-M3/RRF 检索，保留页码、Chunk 和原文证据生成综合综述；无效证据编号会被拒绝，Future Work 区分作者明确提出与基于局限的推断
- 文献发现：调用 arXiv 官方 API 搜索题录与摘要，支持收藏、PDF 入库任务、关键词手动追踪和本地/外部来源分离的推荐。收藏入库复用现有导入 Worker 与 SHA 去重
- 学术写作基础能力：基于真实本地 Chunk 生成带证据状态的大纲，生成不补写未知字段的可核验 References，并支持大纲中英翻译
- 数据分析：上传 CSV 后执行字段类型、缺失值、描述统计和确定性聚合，生成带数据来源标识与 SHA-256 的图表数据
- 自主科研 Agent 提前预览：实现持久化只读工具计划、依赖调度、运行时证据校验和受控重规划；该能力属于 v0.4.0，不计入 v0.3.0 正式完成范围
- 论文列表、详情（含页码文本）、删除
- 多 PDF 批量导入持久化任务，显示每个文件的解析/索引状态、进度与失败原因；服务重启后在 Worker 租约过期时重新排队未完成项，失败项可重试；Worker generation fencing 拒绝旧 Worker 在任务被接管后继续提交页面或任务状态
- 使用 DOI、规范化标题等元数据及 BGE-M3 生成相似论文候选及匹配依据；只作人工核对，不自动合并或删除
- 用户可显式登记论文版本关系、查看文本差异、标记保留版本及删除指定版本
- BM25 关键词检索、BGE-M3 向量检索及 RRF 混合检索；支持单篇论文范围检索和全库检索
- `POST /api/papers/agent/chat` 论文问答：接收最近最多 12 条对话历史，进行基础意图识别和上下文理解；论文证据模式会检索并过滤相关证据，通用知识模式不会检索论文证据
- 前端中文科研工作台，统一 API 请求与错误处理
- 论文删除先提交 SQLite 记录，再清理上传 PDF 与向量；非事务性存储之间不承诺原子提交。失败的外部清理会写入上传目录下的待处理日志，并在服务启动时重试
- 智能问答支持粘贴导入 ChatGPT 对话，并将聊天记录保存在当前浏览器本地
- 前后端开发期通过 Vite `/api` 代理通信
- 批量任务使用 SQLite 条件更新领取，并通过租约心跳、过期接管和 generation fencing 防止过期 Worker 更新任务结果；索引通过稳定 chunk ID 幂等写入，Chroma metadata 保存内容哈希、关键元数据指纹和 schema 版本，未变化的块可跳过重复向量化

## 尚未实现

- 完整学术写作工作流：正文编辑、引用插入、修订历史以及 DOCX/PDF 导出尚未实现
- 服务端持久化会话状态或完整长期记忆
- LangGraph Agent 工作流编排
- v0.4.0 自主科研 Agent 正式版本：当前仅提供只读提前预览，尚未完成中断恢复、生产调度和完整真实依赖验收
- v0.5.0 完整科研知识图谱：当前仅提供基于本地引用关系的 v0.3.0 预览
- v1.0.0 完整科研 Copilot 与正式发布
- 独立版本比较工作台：论文库已有版本关系、差异查看和保留管理，但独立比较页面尚未实现；不将 v0.3.0 描述为已包含该页面
- 当前解析仍将整个 PDF 内容读入内存，尚不具备端到端有界内存保证
- 同一论文索引、删除和重建在多应用进程之间的互斥；单篇论文操作仍依赖进程内协调器，多进程部署暂不应启用
- OCR 的真实 Windows 环境部署与高质量扫描件验收（自动化测试使用模拟，不保证表格、公式或复杂版面 OCR）
- 当前本地真实论文和参考文献解析覆盖尚未达到省赛要求的 20 篇真实独立论文；引用图谱不会使用模拟节点或测试数据补足
- 引用匹配和 PageRank 只描述当前本地语料，不代表整个学术领域的权威基础论文排名
- 引用图谱复用已持久化的 `PaperElement(reference)`；旧论文如果没有元素级解析结果，需要先通过受控重新解析流程补齐，系统不会在浏览图谱时自动重读 PDF

## 环境要求

- Windows 10/11
- Python 3.11 或 3.12
- Node.js 20+（项目构建）
- Node.js 24+（当前直接执行 TypeScript 前端测试；不提高项目构建的最低 Node.js 版本）

## 启动方式

1. 复制环境变量模板（可选）：

```bat
copy .env.example .env
```

2. 启动后端（会自动创建 `.venv` 并安装依赖）：

```bat
start-backend.bat
```

后端默认地址：http://127.0.0.1:8000  
接口文档：http://127.0.0.1:8000/docs

3. 另开一个终端启动前端：

```bat
start-frontend.bat
```

前端地址：http://127.0.0.1:5173

也可双击 `start.bat` 同时打开两个窗口。

### OCR 配置（可选）

OCR 仅对缺少可提取文本的图像页尝试执行，不会无条件处理普通文本页。Windows 环境需单独安装 Tesseract OCR，并在 PATH 中提供 `tesseract.exe` 和 `eng`、`chi_sim` 语言数据；Python 依赖仍使用 PyMuPDF，不会自动下载 OCR 模型或语言数据。设置 `OCR_ENABLED=false` 可关闭 OCR。默认语言为 `eng+chi_sim`，可用 `OCR_LANGUAGE`、`OCR_DPI` 和 `OCR_MIN_TEXT_CHARS` 调整。若 Tesseract 或语言数据不可用，页面会保留明确状态和错误，不会伪造识别文本。

批量任务由应用内后台工作线程处理；SQLite 通过原子状态更新领取任务，Worker 每 20 秒续租、租约 90 秒到期后允许接管，过期 Worker 的任务状态提交会被拒绝。单篇成功后立即清理暂存 PDF，失败文件为支持重试最多保留 30 天，超期后清理暂存文件并保留失败记录。无效格式和超限文件各自记录为失败项，不阻断同批有效文件。索引审计分批检查 SQLite 文本块与 ChromaDB 向量的 ID、内容哈希、关键元数据和索引 schema 版本，区分缺失、过期、历史旧索引、无法验证及孤立向量；创建重建任务时只排队受影响论文，并在修复后重新验证。只清除 SQLite 中确认不存在的孤立向量。SQLite 和 ChromaDB 之间不提供分布式事务。同一论文的索引与删除仍通过进程内协调器互斥，因此当前仅安全支持单个应用进程，不应启用多进程部署。BGE-M3 相似候选需要本地模型及 ChromaDB 可用；候选阈值通过 `SEMANTIC_DUPLICATE_THRESHOLD` 设置。

## 运行测试

### 后端测试

```bat
cd backend
.venv\Scripts\python -m pytest -q
```

如果尚未创建虚拟环境，先运行 `start-backend.bat` 或：

```bat
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
```

### 前端测试

当前前端测试覆盖任务轮询、引用图谱转换、文献发现、比较结论和翻译阅读工具，需要 Node.js 24+ 的 TypeScript type stripping 支持。项目生产构建仍只要求 Node.js 20+。

```bat
cd frontend
npm test
```

前端生产构建：

```bat
cd frontend
npm run build
```

## API 约定

成功：

```json
{ "success": true, "data": {}, "error": null }
```

失败：

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "NOT_FOUND",
    "message": "论文不存在",
    "details": null
  }
}
```

失败响应的 HTTP 状态码与 `error.code` 共同表示错误类型。例如，论文不存在返回 HTTP 404 / `NOT_FOUND`；空文件、无效或损坏的 PDF、超限文件返回 HTTP 400；缺少必需请求参数返回 HTTP 422；重复上传返回 HTTP 409。论文删除后若 PDF 或向量清理未完成，会返回 HTTP 503 / `PAPER_CLEANUP_PENDING`，并保留待处理清理记录。

主要接口（论文接口均位于 `/api/papers` 下）：

- `GET /api/health`
- `GET /api/papers`
- `POST /api/papers/upload`：上传 PDF，成功返回 HTTP 201
- `POST /api/papers/import-jobs`：以 `files` 多文件表单创建持久化批量任务
- `GET /api/papers/import-jobs`、`GET /api/papers/import-jobs/{job_id}`、`POST /api/papers/import-jobs/{job_id}/retry`、`POST /api/papers/import-jobs/{job_id}/items/{item_id}/retry`
- `GET /api/papers/index/consistency`、`POST /api/papers/index/rebuild`：内容感知审计索引状态，并仅针对缺失/过期论文创建持久化修复任务
- `GET /api/papers/{paper_id}/similar`：查询 BGE-M3 相似候选
- `GET /api/papers/{paper_id}/versions`、`POST /api/papers/{paper_id}/versions`：查看/显式登记版本关系
- `POST /api/papers/{paper_id}/versions/{retained_paper_id}/retain`、`GET /api/papers/{paper_id}/versions/{other_paper_id}/diff`、`DELETE /api/papers/{paper_id}/versions/{version_id}`
- `GET /api/citations/graph`、`GET /api/citations/papers/{paper_id}`：查询本地引用图谱、PageRank 和论文引用详情
- `POST /api/citations/rebuild`、`POST /api/citations/papers/{paper_id}/rebuild`：全量或单篇幂等重建引用记录和可靠本地引用边
- `POST /api/reviews`：针对 2～5 篇本地论文逐篇检索证据并生成可回溯的多文献综述和 Future Work
- `GET /api/discovery/search`：arXiv 官方题录与摘要搜索
- `POST /api/discovery/favorites`、`GET /api/discovery/favorites`、`POST /api/discovery/favorites/{favorite_id}/import`：收藏、状态刷新和复用现有 ImportJob 的 PDF 入库
- `POST /api/discovery/tracking`、`GET /api/discovery/tracking`、`POST /api/discovery/tracking/{tracking_id}/refresh`：手动关键词追踪与历史新增计数
- `GET /api/discovery/recommendations`：区分 arXiv 外部与本地 RAG 的推荐结果
- `POST /api/writing/outline`、`POST /api/writing/references`、`POST /api/writing/translate`：证据大纲、可核验 References 与学术翻译
- `POST /api/datasets/analyze`：CSV 字段分析、统计摘要和确定性图表数据
- `/api/research-agent/runs`：自主科研 Agent 的 v0.4.0 提前预览 API，不属于 v0.3.0 正式验收范围
- `GET /api/papers/search?q=...`：混合检索
- `POST /api/papers/agent/chat?query=...&paper_id=...`：论文问答；可选的 `paper_id` 限定单篇，后端也支持 2～5 篇的 `paper_ids` 查询参数；最近最多 12 条对话历史通过请求体传递；省略论文范围时检索全库；需要配置 `LLM_API_KEY`
- `GET /api/papers/{paper_id}`：获取论文详情及分页文本
- `POST /api/papers/{paper_id}/translate`：从数据库读取指定页面或结构元素，执行中英双向学术翻译；结果不持久化
- `DELETE /api/papers/{paper_id}`：删除论文

## 安全说明

API Key 只允许放在后端环境变量中，禁止写入前端代码。
