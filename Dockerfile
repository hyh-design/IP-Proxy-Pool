FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XDG_CACHE_HOME=/tmp/.cache
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 ippool \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app ippool
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY docker/entrypoint.sh /usr/local/bin/ip-pool-entrypoint
RUN chmod 0555 /usr/local/bin/ip-pool-entrypoint \
    && chown -R 10001:10001 /app
USER 10001:10001
ENTRYPOINT ["ip-pool-entrypoint"]
CMD ["--help"]
