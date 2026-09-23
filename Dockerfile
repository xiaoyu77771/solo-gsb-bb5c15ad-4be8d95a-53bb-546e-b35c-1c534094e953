FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    IMD_DB_PATH=/data/intermod.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000

# 启动时由 FastAPI lifespan 自动初始化 SQLite 数据库（幂等建表）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
