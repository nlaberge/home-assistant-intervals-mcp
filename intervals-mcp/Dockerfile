ARG PYTHON_IMAGE=python:3.11-slim-bookworm
ARG UV_VERSION=0.13.0
ARG INTERVALS_VERSION=5.6.0
ARG INTERVALS_COMMIT=92a630bfc4ffe7906c9bf8d2280c1ea41a561d66
ARG TUNNEL_CLIENT_VERSION=v0.0.16

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ${PYTHON_IMAGE} AS intervals-builder
ARG INTERVALS_VERSION
ARG INTERVALS_COMMIT

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=uv /uvx /usr/local/bin/uvx

RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/intervals
RUN git init . \
   && git remote add origin https://github.com/hhopke/intervals-icu-mcp.git \
   && git fetch --depth 1 origin "${INTERVALS_COMMIT}" \
   && git -c advice.detachedHead=false checkout --detach FETCH_HEAD

ENV SETUPTOOLS_SCM_PRETEND_VERSION=${INTERVALS_VERSION}
RUN uv sync --frozen --no-dev

FROM ghcr.io/openai/tunnel-client:${TUNNEL_CLIENT_VERSION} AS tunnel-client

FROM ${PYTHON_IMAGE}
ARG INTERVALS_COMMIT
ARG TUNNEL_CLIENT_VERSION

LABEL \
    io.hass.type="app" \
    org.opencontainers.image.title="Home Assistant Intervals.icu MCP Secure Tunnel" \
    org.opencontainers.image.description="Intervals.icu MCP server and OpenAI Secure MCP Tunnel client" \
    org.opencontainers.image.version="0.1.2" \
    org.opencontainers.image.source="https://github.com/nlaberge/home-assistant-intervals-mcp" \
    org.opencontainers.image.vendor="Personal Home Assistant app"

ENV PATH="/opt/intervals/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEALTH_LISTEN_ADDR="127.0.0.1:8766" \
    HEALTH_SHOW_DETAILS="true" \
    LOG_LEVEL="info" \
    LOG_FORMAT="json"

COPY --from=intervals-builder /opt/intervals/.venv /opt/intervals/.venv
COPY --from=intervals-builder /opt/intervals/src /opt/intervals/src
COPY --from=intervals-builder /opt/intervals/pyproject.toml /opt/intervals/pyproject.toml
COPY --from=tunnel-client /usr/bin/tunnel-client /usr/bin/tunnel-client

WORKDIR /opt/intervals
COPY run.sh /run.sh
COPY supervisor.py /opt/intervals/supervisor.py

RUN chmod 0755 /run.sh \
    && mkdir -p /run/ha-intervals

HEALTHCHECK --interval=60s --timeout=10s --start-period=5m --retries=3 \
    CMD ["python", "/opt/intervals/supervisor.py", "healthcheck"]

ENTRYPOINT ["/run.sh"]
