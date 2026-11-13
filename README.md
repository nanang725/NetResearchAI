# NetResearch AI

面向计算机网络领域的全流程科研助手 Agent。本仓库用于第九届传智杯 AI 大模型创新应用挑战赛。

当前完成的是**第一阶段：工程初始化与论文库基础能力**。智能问答、RAG、LangGraph Agent、引用图谱和版本比较尚未实现，对应页面会明确标注，不会用模拟数据冒充已完成功能。

## 技术栈

- 前端：Vue 3 + TypeScript + Vite + Element Plus
- 后端：Python + FastAPI + SQLite
- PDF 解析：PyMuPDF（保留页码）
- 后续计划：向量检索 + BM25、LangGraph、NetworkX、ECharts

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
│   ├── data/                数据库与上传文件（运行时生成）
│   ├── tests/               API 测试
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
- 前端中文科研工作台，统一 API 请求与错误处理
- 智能问答支持粘贴导入 ChatGPT 对话，并将聊天记录保存在当前浏览器本地
- 前后端开发期通过 Vite `/api` 代理通信

## 尚未实现

- RAG 混合检索
- LangGraph Agent 问答
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
{ "success": false, "data": null, "error": { "code": "PAPER_NOT_FOUND", "message": "论文不存在。" } }
```

主要接口：

- `GET /api/health`
- `GET /api/papers`
- `POST /api/papers/upload`
- `GET /api/papers/{id}`
- `DELETE /api/papers/{id}`

## 安全说明

API Key 只允许放在后端环境变量中，禁止写入前端代码。
