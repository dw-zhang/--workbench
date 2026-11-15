# ---------- 工作台 · 单镜像 ----------
# 全部代码只用 Python 标准库（无第三方依赖），所以不需要 pip install，
# 镜像 = 官方 slim + 源码 + 数据目录，构建一般 10~30 秒。
FROM python:3.11-slim

LABEL org.opencontainers.image.title="workbench" \
      org.opencontainers.image.description="钉钉/飞书/研发工具聚合工作台 + AI 审批助手" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    WB_PORT=8787

# 业务模块（纯标准库，零第三方依赖）
COPY *.py ./
COPY seed_schema.json ./
COPY seed_data.json* ./
COPY static ./static

# SQLite 与所有运行期数据都在这里；挂载卷后容器重建不丢
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/ping',timeout=3).status==200 else 1)"

CMD ["python3", "app.py"]
