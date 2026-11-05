FROM node:24-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS frontend
WORKDIR /app/web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM python:3.12-slim-bookworm@sha256:da54eb86849fe19be7584d8b3f291fcf0c53c196760ab3d70dc6382f681ef9c0 AS backend
WORKDIR /app
RUN pip install --no-cache-dir uv==0.9.18
COPY backend/pyproject.toml backend/uv.lock ./backend/
COPY backend/src ./backend/src
COPY schemas ./schemas
COPY skills/dataagent-retrieval ./skills/dataagent-retrieval
RUN uv sync --directory backend --locked --no-dev
COPY --from=frontend /app/web/dist /app/static
ENV PATH=/app/backend/.venv/bin:$PATH DATAAGENT_LISTEN=0.0.0.0:8000 DATAAGENT_STATIC_DIR=/app/static
EXPOSE 8000
CMD ["semantica-dataagent", "serve"]
