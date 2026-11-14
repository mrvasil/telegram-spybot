FROM ghcr.io/astral-sh/uv:0.11.23@sha256:d0a0a753ab981624b49c97abc98821c1c09f4ca69d1ef5cee69c501be3d88479 AS uv
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" DATA_DIR=/data HOME=/tmp
COPY pyproject.toml uv.lock .python-version ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && uv cache clean && \
    mkdir -p /data && chmod 700 /data && chown 1000:1000 /data
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 CMD ["spybot", "health", "--data-dir", "/data"]
ENTRYPOINT ["spybot"]
CMD ["run"]
