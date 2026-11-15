# NetResearch AI

面向计算机网络领域的科研辅助工具。本仓库用于第九届传智杯 AI 大模型创新应用挑战赛。

## 项目阶段

- **v0.1.0：工程基础与论文检索可靠性（已完成）**：提供论文 PDF 管理、解析、混合检索及基础问答能力。
- **v0.2.0：上下文感知与意图驱动 Agent（核心功能已完成）**：支持有限对话上下文、轻量 Query Rewriting、基础意图识别，以及论文证据和通用知识两种回答来源。
- **v0.2.x：PDF 深度解析与海量文献处理（后续扩展）**。
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
- PyMuPDF 按页提取文本并入库
- 论文列表、详情（含页码文本）、删除
- BM25 关键词检索、BGE-M3 向量检索及 RRF 混合检索；支持单篇论文范围检索和全库检索
- `POST /api/papers/agent/chat` 论文问答：接收最近最多 12 条对话历史，进行基础意图识别和上下文理解；论文证据模式会检索并过滤相关证据，通用知识模式不会检索论文证据
- 前端中文科研工作台，统一 API 请求与错误处理
- 论文删除先提交 SQLite 记录，再清理上传 PDF 与向量；非事务性存储之间不承诺原子提交。失败的外部清理会写入上传目录下的待处理日志，并在服务启动时重试
- 智能问答支持粘贴导入 ChatGPT 对话，并将聊天记录保存在当前浏览器本地
- 前后端开发期通过 Vite `/api` 代理通信

## 尚未实现

- 科研辅助和学术写作的完整专属工作流（目前仅有基础意图识别入口）
- 服务端持久化会话状态或完整长期记忆
- LangGraph Agent 工作流编排
- v0.2.x 计划的 PDF 深度解析与海量文献处理扩展
- v0.3.0 大规模知识库智能检索
- v0.4.0 科研知识图谱、引用抽取与图谱展示
- 论文版本比较、扫描件 OCR

## 环境要求

- Windows 10/11
- Python 3.11 或 3.12
- Node.js 20+

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

## 运行测试

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
- `GET /api/papers/search?q=...`：混合检索
- `POST /api/papers/agent/chat?query=...&paper_id=...`：论文问答；`query` 和可选的 `paper_id` 均为 URL 查询参数，最近最多 12 条对话历史通过请求体传递；省略 `paper_id` 时检索全库；需要配置 `LLM_API_KEY`
- `GET /api/papers/{paper_id}`：获取论文详情及分页文本
- `DELETE /api/papers/{paper_id}`：删除论文

## 安全说明

API Key 只允许放在后端环境变量中，禁止写入前端代码。
