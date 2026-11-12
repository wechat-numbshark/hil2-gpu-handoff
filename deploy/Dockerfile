# 协调服务镜像。只出站访问飞书，无需公网入口。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY hil2 ./hil2
RUN pip install --no-cache-dir . && \
    useradd --system --uid 10001 --create-home hil2 && \
    mkdir -p /app/var && chown -R hil2:hil2 /app

USER hil2
EXPOSE 8848

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8848/api/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "hil2", "--host", "0.0.0.0", "--port", "8848"]
