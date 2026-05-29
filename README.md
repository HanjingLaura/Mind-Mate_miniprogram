# Mind-Mate

Mind-Mate 是一款微信小程序，后端使用 FastAPI，帮助用户通过 AI 陪伴、每日任务追踪与打卡来对抗拖延。支持本地开发的流式聊天，以及微信云托管的同步模式部署。

Mind-Mate is a WeChat mini program with a FastAPI backend that helps users fight procrastination through an AI companion, daily task tracking, and progress check-ins. It supports local development with streaming chat and a cloud-hosted mode for WeChat cloud container deployment.

## 功能概览 / Features

- AI 聊天：本地流式输出、云端同步返回 / AI chat with streaming responses (local) and sync mode (cloud)
- 今日任务看板：新增 / 打卡 / 删除 / Daily task board with add/check-in/delete
- 用户信息与作息设置接口 / User profile and settings endpoints
- 管理员升级与微信支付 V3 集成（可选） / Admin upgrade and WeChat Pay V3 integration (optional)

## 目录结构 / Project Structure

- backend/ - FastAPI 服务、数据库与三方集成 / FastAPI service, database, and integrations
- miniprogram/ - 微信小程序前端 / WeChat mini program UI
- agent/ - 提示词与代理配置 / Prompt and agent configs

## 本地启动 / Quick Start (Local)

### 1) 后端 / Backend

前置条件：Python 3.10+（推荐 3.11）
Prerequisites: Python 3.10+ (3.11 recommended)

```
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

验证 API / Verify the API:

```
http://localhost:8000/health
```

### 2) 小程序 / Mini Program

1. 打开微信开发者工具，导入 miniprogram/ 目录。
2. 确认本地 API 地址指向你的后端：

1. Open WeChat DevTools and import the miniprogram/ folder.
2. Ensure the local API base points to your backend:

- miniprogram/utils/api.js
  - LOCAL_API_BASE = http://localhost:8000

若在真机调试，请把 LOCAL_API_BASE 改为你的局域网 IP，确保手机可访问后端。
If you debug on a physical device, change LOCAL_API_BASE to your LAN IP so the device can reach the backend.

## 配置说明 / Configuration

- 复制 backend/.env.example 为 backend/.env 并补齐密钥。
- 数据库默认使用 SQLite，可通过 .env 的 DATABASE_URL 调整。
- 微信登录、订阅消息与支付需要对应的环境变量配置。

- Copy backend/.env.example to backend/.env and fill in keys as needed.
- Database defaults to SQLite via DATABASE_URL in .env.
- WeChat login, message subscription, and payment require their corresponding credentials in .env.

## 云托管模式（可选） / Cloud Mode (Optional)

当 wx.cloud 可用时，小程序会自动切换到云托管调用（见 miniprogram/app.js）。后端提供 Dockerfile 用于微信云托管部署。
When wx.cloud is available, the mini program switches to cloud calls automatically (see miniprogram/app.js). The backend includes a Dockerfile for WeChat cloud container deployment.
