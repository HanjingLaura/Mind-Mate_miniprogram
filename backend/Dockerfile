# Mind-Mate 后端 Dockerfile — 微信云托管部署
FROM python:3.11-slim

WORKDIR /app

# 安装依赖（利用 Docker 缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制应用代码
COPY . .

# 云托管注入 PORT 环境变量；默认 8000 用于本地测试
ENV PORT=8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:' + __import__('os').environ.get('PORT', '8000') + '/health')" || exit 1

# 启动命令，shell 形式以支持 $PORT 变量展开
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
