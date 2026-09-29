# Build context is the repository root (needs shared/ and infra/db/):
#   docker build -f services/ingestion/Dockerfile .
FROM python:3.12-slim AS builder
WORKDIR /build
COPY constraints.txt .
COPY shared ./shared
COPY services/ingestion ./services/ingestion
RUN pip install --no-cache-dir --prefix=/install -c constraints.txt ./shared ./services/ingestion

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
COPY --from=builder /install /usr/local
COPY infra/db /app/infra/db
ENV FSP_ALEMBIC_DIR=/app/infra/db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER app
EXPOSE 8101
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8101"]
