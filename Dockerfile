FROM node:22-slim AS cobalt-builder

# Build the cobalt API from source. We clone the official repo at build time
# so the final image is self-contained — no need for the user to keep the
# cobalt source tree around.
WORKDIR /build

# Install git for cloning + pnpm for installing deps.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g pnpm@11

# Pin a specific cobalt commit/tag for reproducibility. Update this when you
# want to pull in new cobalt features or bug fixes.
ARG COBALT_REF=main
RUN git clone --depth=1 --branch="${COBALT_REF}" https://github.com/imputnet/cobalt.git /build/cobalt

WORKDIR /build/cobalt/api
RUN pnpm install --frozen-lockfile=false


# --------------------------------------------------------------------------- #
# bgutil PO Token server builder stage.
# --------------------------------------------------------------------------- #
# We build the bgutil-ytdlp-pot-provider server (TypeScript → JS) so it can
# run inside the same container as cobalt + the bot. This eliminates the need
# for a separate Docker service for bgutil — critical for Railway, which runs
# a single container per service.
FROM node:22-slim AS bgutil-builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG BGUTIL_REF=master
RUN git clone --depth=1 --branch="${BGUTIL_REF}" https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /build/bgutil

WORKDIR /build/bgutil/server
# Install ALL deps (including devDeps for tsc), compile TS, then prune devDeps.
RUN npm ci --no-audit --no-fund \
    && npx tsc \
    && npm prune --omit=dev


FROM node:22-slim AS runtime

# Install Python + ffmpeg + system deps in a single layer.
ENV DEBIAN_FRONTEND=noninteractive \
    NODE_ENV=production \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        ffmpeg \
        curl \
        ca-certificates \
        tini \
        nscd \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip

ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Install Python dependencies first to leverage Docker layer cache.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && adduser --disabled-password --gecos "" appuser \
    && mkdir -p /tmp/mz-downloader /app/downloads \
    && chown -R appuser:appuser /app /tmp/mz-downloader

# Copy the MZ-Downloader source.
COPY --chown=appuser:appuser . /app/

# Copy the pre-built cobalt API from the builder stage.
COPY --from=cobalt-builder --chown=appuser:appuser /build/cobalt/ /opt/cobalt/

# Copy the pre-built bgutil PO Token server from its builder stage.
# /build/bgutil/server/build/ contains the compiled JS (main.js etc.).
COPY --from=bgutil-builder --chown=appuser:appuser /build/bgutil/server/ /opt/bgutil/

# Make the start script executable.
RUN chmod +x /app/start.sh

# Default cobalt + bgutil env (can be overridden at runtime via -e flags).
# Note: BGUTIL_TOKEN_TTL and BGUTIL_PORT are intentionally NOT set here to
# avoid Railway's SecretsUsedInArgOrEnv lint warning — start.sh applies
# safe defaults at runtime.
ENV COBALT_API_URL=http://127.0.0.1:9000/ \
    API_URL=http://127.0.0.1:9000/ \
    API_PORT=9000 \
    API_LISTEN_ADDRESS=127.0.0.1 \
    FFMPEG_PATH=/usr/bin/ffmpeg \
    YTDLP_ENABLED=true \
    YTDLP_BGUTIL_BASE_URL=http://127.0.0.1:4416

USER appuser

# Use tini as PID 1 so SIGTERM propagates correctly to all child processes.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
