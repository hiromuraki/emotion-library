# 1. 选择适合的基础镜像
FROM python:3.14-slim

# 2. 从官方的 uv alpine 镜像中直接复制 uv 和 uvx 二进制文件到当前环境中
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 3. 设置工作目录
WORKDIR /app

# 4. 配置 uv 的环境变量以优化 Docker 构建
# UV_COMPILE_BYTECODE: 自动将 .py 文件编译为 .pyc 以加快启动速度
# UV_LINK_MODE=copy: 强制使用复制而非硬链接，避免在 Docker 跨层/跨卷时出现权限或文件系统报错
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# 5. 利用 Docker 缓存：先复制依赖配置文件
# 如果你使用的是 requirements.txt，请将下面替换为 COPY requirements.txt .
COPY pyproject.toml uv.lock ./

# 6. 安装依赖 (但不包含项目源代码)
# --frozen 确保锁文件不会被修改，--no-dev 排除开发环境依赖
RUN uv sync --frozen --no-dev --no-install-project

# 7. 复制项目文件
COPY src/ ./src/
COPY static/ ./static/

# 8. 安装项目自身
RUN uv sync --frozen --no-dev

# 9. 数据库目录
RUN mkdir -p /data
VOLUME /data

# 10. 暴露端口
EXPOSE 8000

# 11. 启动
# HOST / PORT 环境变量控制监听地址和端口，默认 0.0.0.0:8000
CMD uv run uvicorn src.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"