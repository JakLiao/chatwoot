# ============================================================================
# Dockerfile · Chatwoot 客户声音采集网关
#
# 刻意做得很薄：一个 FastAPI 进程 + 一个 Chatwoot HTTP 客户端。
# 宿主内存只剩 ~2.5Gi，镜像里不带任何多余东西。
#
# 构建上下文是 ./gateway（见 docker-compose.yml 的 build.context）
# ============================================================================

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# ⚠️ 不装 tzdata：python:3.12-slim 基础镜像已经带了 /usr/share/zoneinfo，
#    实测 zoneinfo.ZoneInfo("Asia/Shanghai") 可用。apt-get 在本机网络上很慢，
#    多这一层纯属拖慢构建。

COPY requirements.txt ./

# pip 源：默认走阿里云镜像 —— 实测 files.pythonhosted.org 只有 ~23KB/s，
# 阿里云 ~530KB/s，差了 20 多倍。不需要镜像时覆盖即可：
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple/ intake
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

COPY app ./app
COPY config ./config

# 以非 root 运行；注册表只读挂载（里面只有 API Key 的 sha256，没有明文）
USER nobody

EXPOSE 8080

# --workers 1：网关是低并发内网服务，多 worker 只是白吃内存
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-server-header", "--log-level", "info"]
