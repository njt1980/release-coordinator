# Release Coordinator showcase: one container, FastAPI serving the built frontend.
# Build context: the repository root (see .dockerignore, an allowlist).

FROM node:18.20.4-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npx vite build

FROM python:3.11.18-slim
# The commit the image is built from; validation bundles quote it.
ARG RC_TOOL_VERSION=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RC_FRONTEND_DIST=/app/frontend/dist \
    RC_TOOL_VERSION=${RC_TOOL_VERSION}
WORKDIR /app
COPY deploy/showcase/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY src/ ./src/
COPY mock_telemetry.json ./
# Quoted by validation bundles, at their repository paths.
COPY docs/intended-use.md ./docs/intended-use.md
COPY tests/benchmarks/discovery/baseline.json ./tests/benchmarks/discovery/baseline.json
COPY tests/benchmarks/discovery/apps/ ./tests/benchmarks/discovery/apps/
COPY --from=frontend /build/dist ./frontend/dist
RUN useradd --create-home --uid 10001 app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
