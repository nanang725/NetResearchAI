# NetResearch AI

面向计算机网络领域的全流程科研助手 Agent。本仓库用于第九届传智杯 AI 大模型创新应用挑战赛。

当前已完成**第一阶段：工程初始化、论文库基础能力与第一版检索问答**。系统支持论文 PDF 管理、BM25 与 BGE-M3 向量混合检索，并通过论文问答接口基于检索证据生成回答。LangGraph 工作流、引用图谱和论文版本比较仍属于后续规划；当前问答接口不保存或接收服务端多轮对话历史。

## 技术栈

- 前端：Vue 3 + TypeScript + Vite + Element Plus
- 后端：Python + FastAPI + SQLite + ChromaDB
- PDF 解析：PyMuPDF（保留页码）
- 检索：BM25、BGE-M3 向量检索与 RRF 融合
- 后续计划：LangGraph、NetworkX、ECharts

## 目录结构

```text
NetResearchAI/
├── backend/                 FastAPI 服务
│   ├── app/
│   │   ├── api/             健康检查与论文接口
│   │   ├── core/            统一错误与响应
│   │   ├── models/          SQLAlchemy 模型
│   │   ├── schemas/         Pydantic 结构
│   │   └── services/        PDF 解析与论文业务
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
- `POST /api/papers/agent/chat` 论文问答：检索论文证据、过滤无关证据后调用配置的 LLM 生成回答
- 前端中文科研工作台，统一 API 请求与错误处理
- 论文删除先提交 SQLite 记录，再清理上传 PDF 与向量；非事务性存储之间不承诺原子提交。失败的外部清理会写入上传目录下的待处理日志，并在服务启动时重试
- 智能问答支持粘贴导入 ChatGPT 对话，并将聊天记录保存在当前浏览器本地
- 前后端开发期通过 Vite `/api` 代理通信

## 尚未实现

- 基于 LangGraph 的 Agent 工作流编排（当前论文问答接口使用现有检索与 LLM 服务，不是 LangGraph 工作流）
- 服务端多轮对话上下文传递
- 引用抽取与图谱
- 论文版本比较
- 扫描件 OCR

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
- `POST /api/papers/agent/chat?query=...&paper_id=...`：论文问答；`query` 和可选的 `paper_id` 均为 URL 查询参数，省略 `paper_id` 时检索全库；需要配置 `LLM_API_KEY`
- `GET /api/papers/{paper_id}`：获取论文详情及分页文本
- `DELETE /api/papers/{paper_id}`：删除论文

## 安全说明

API Key 只允许放在后端环境变量中，禁止写入前端代码。
