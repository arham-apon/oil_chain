# Build context is the repository root:  docker build -f services/cognitive/Dockerfile .
FROM python:3.12-slim AS builder
WORKDIR /build
COPY constraints.txt .
COPY shared ./shared
COPY services/cognitive ./services/cognitive
RUN pip install --no-cache-dir --prefix=/install -c constraints.txt ./shared ./services/cognitive

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
COPY --from=builder /install /usr/local
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER app
EXPOSE 8104
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8104"]
