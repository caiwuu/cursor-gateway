FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
WORKDIR /app/cursor-gateway

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cursor_gateway ./cursor_gateway
COPY run.py .
COPY vendor/sand /app/
COPY --from=web /web/dist ./web/dist

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CURSOR_GATEWAY_HOST=0.0.0.0 \
    CURSOR_GATEWAY_PORT=8788 \
    CURSOR_GATEWAY_HOME=/data \
    CURSOR_GATEWAY_REPO_ROOT=/app

EXPOSE 8788
VOLUME ["/data"]

CMD ["python", "-m", "cursor_gateway"]
