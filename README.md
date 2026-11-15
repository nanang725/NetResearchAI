# NetResearch AI

面向计算机网络领域的科研辅助工具。本仓库用于第九届传智杯 AI 大模型创新应用挑战赛。

## 项目阶段

- **v0.1.0：工程基础与论文检索可靠性（已完成）**：提供论文 PDF 管理、解析、混合检索及基础问答能力。
- **v0.2.0：上下文感知与意图驱动 Agent（核心功能已完成）**：支持有限对话上下文、轻量 Query Rewriting、基础意图识别，以及论文证据和通用知识两种回答来源。
- **v0.2.x：PDF 深度解析与海量文献处理（开发中）**：已加入可选按页 OCR、持久化批量导入任务、页级解析检查点与失败页恢复、语义相似候选、人工版本关系及索引一致性检查/重建；当前仍会将整篇 PDF 内容读入内存，尚未具备端到端有界内存保证。
- **v0.3.0：大规模知识库智能检索（规划中）**。
- **v0.4.0：科研知识图谱（规划中）**。

v0.2.0 会将最近最多 12 条对话历史随请求传给后端，用于理解依赖上下文的追问，并在需要时轻量改写检索查询。服务端不保存持久化会话状态；这不是完整的长期记忆。基础意图识别分为五类：`paper_qa`（论文问答）、`simple_explanation`（通俗解释）、`comparative_analysis`（比较分析）、`research_assistance`（科研辅助）和 `academic_writing`（学术写作）。科研辅助和学术写作目前只完成基础意图识别，尚未实现完整的专属工作流。

回答可使用两种来源：`paper_evidence` 检索论文并依据筛选后的相关证据回答；`generic_knowledge` 不检索或引用论文证据。多轮对话默认继承上一轮回答的知识来源；用户明确要求根据论文原文或改用通用知识时，按本轮要求切换。论文证据不足时，回答规则要求明确说明，不把缺乏证据的推测包装成论文结论；没有有效相关证据时不会生成论文回答。问答接口使用现有检索与 LLM 服务，不是 LangGraph 工作流。

## 技术栈

- 前端：Vue 3 + TypeScript + Vite + Element Plus
- 后端：Python + FastAPI + SQLite + ChromaDB
- PDF 解析：PyMuPDF（保留页码）
- 检索：BM25、BGE-M3 向量检索与 RRF 融合
- 规划中：大规模知识库智能检索、科研知识图谱

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

## 已实现功能

- 后端健康检查 `GET /api/health`
- 论文 PDF 上传、文件校验（扩展名、PDF 文件头、大小、空文件、加密文档）
- PyMuPDF 按页提取文本，并保存章节、参考文献、图像元数据、表格行列和可识别的公式文本；表格作为独立检索块保留页码，公式使用启发式标记且不补造内容。扫描页继续复用 Tesseract OCR 并记录页面状态（上传和解析仍将整个 PDF 内容读入内存）
- PDF 页级持久化检查点：页面状态区分 pending、processing、completed 和 failed；失败后只重试未完成或失败页面，已完成页面跳过文本提取、OCR 和元素提取，避免重复插入页面元素与检索块
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

- 科研辅助和学术写作的完整专属工作流（目前仅有基础意图识别入口）
- 服务端持久化会话状态或完整长期记忆
- LangGraph Agent 工作流编排
- v0.3.0 大规模知识库智能检索
- v0.4.0 科研知识图谱、引用抽取与图谱展示
- 当前解析仍将整个 PDF 内容读入内存，尚不具备端到端有界内存保证
- 同一论文索引、删除和重建在多应用进程之间的互斥；单篇论文操作仍依赖进程内协调器，多进程部署暂不应启用
- OCR 的真实 Windows 环境部署与高质量扫描件验收（自动化测试使用模拟，不保证表格、公式或复杂版面 OCR）

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

当前前端测试直接导入 `taskPoller.ts`，需要 Node.js 24+ 的 TypeScript type stripping 支持。项目生产构建仍只要求 Node.js 20+。

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
- `GET /api/papers/search?q=...`：混合检索
- `POST /api/papers/agent/chat?query=...&paper_id=...`：论文问答；`query` 和可选的 `paper_id` 均为 URL 查询参数，最近最多 12 条对话历史通过请求体传递；省略 `paper_id` 时检索全库；需要配置 `LLM_API_KEY`
- `GET /api/papers/{paper_id}`：获取论文详情及分页文本
- `DELETE /api/papers/{paper_id}`：删除论文

## 安全说明

API Key 只允许放在后端环境变量中，禁止写入前端代码。
