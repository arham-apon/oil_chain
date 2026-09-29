# Build context is the repository root (needs shared/ and constraints.txt):
#   docker build -f services/forecast/Dockerfile .
FROM python:3.12-slim AS builder
WORKDIR /build
COPY constraints.txt .
COPY shared ./shared
COPY services/forecast ./services/forecast
RUN pip install --no-cache-dir --prefix=/install -c constraints.txt ./shared ./services/forecast

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app && mkdir /models && chown app:app /models
COPY --from=builder /install /usr/local
ENV MODEL_DIR=/models \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER app
VOLUME /models
EXPOSE 8102
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8102"]
