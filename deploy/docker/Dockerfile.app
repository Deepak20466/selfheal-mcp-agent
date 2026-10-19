# app-pod: the monitored demo FastAPI app (apps/target_app).
#
# Multi-stage: the "builder" stage has a C toolchain (asyncpg/argon2-cffi
# both ship wheels for common platforms, but building on an uncommon one
# still needs headers) and installs this project's own base dependencies
# into a venv; the runtime stage copies only that venv + the source tree
# into a slim, non-root image. See CLAUDE.md's "Multi-app support" /
# "Terminal-only v1.0" phase log for why there is no [api]/[dev] extra here
# -- app-pod never talks to an AI backend, it only gets monitored by one.
FROM python:3.11-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY core ./core
COPY apps ./apps
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY sentinel ./sentinel
COPY mcp_server ./mcp_server
COPY healer ./healer
COPY cli ./cli
COPY scripts ./scripts
# The package's runtime/development dependencies are bounded in pyproject.toml.
# The base pip is upgraded so image builds receive current installer fixes.
# hadolint ignore=DL3013
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

FROM python:3.11-slim AS runtime

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --from=builder /build /app
COPY deploy/docker/http_healthcheck.py /app/http_healthcheck.py
RUN chown -R app:app /app /home/app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER 10001:10001
EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "/app/http_healthcheck.py", "APP_PORT", "8001"]

# Shell expansion preserves runtime port overrides.
# hadolint ignore=DL3025
CMD ["sh", "-c", "uvicorn apps.target_app.main:app --host 0.0.0.0 --port ${APP_PORT:-8001}"]
