# Hangar: a web UI for Claude Code sessions, backed by tmux.
FROM python:3.12-slim-bookworm

ARG UID=1000
ARG GID=1000
# Claude Code version to install: stable, latest, or a version number like 2.1.310.
ARG CLAUDE_VERSION=stable
# Extra Debian packages your sessions need (compilers, language runtimes, ...), space separated.
ARG EXTRA_PACKAGES=""

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISABLE_AUTOUPDATER=1 \
    SHELL=/bin/bash \
    HANGAR_DATA_DIR=/data \
    HANGAR_PORT=7878

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tmux git curl ca-certificates ripgrep jq less nano procps openssh-client tini zstd unzip \
      $EXTRA_PACKAGES \
 && rm -rf /var/lib/apt/lists/*

# Claude Code native build, installed system-wide. Rebuild the image to update it.
RUN mkdir -p /tmp/ci \
 && HOME=/tmp/ci bash -c "curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_VERSION}" \
 && cp -L /tmp/ci/.local/bin/claude /usr/local/bin/claude \
 && rm -rf /tmp/ci \
 && claude --version

RUN groupadd -g ${GID} hangar 2>/dev/null || true \
 && useradd -m -u ${UID} -g ${GID} -s /bin/bash hangar \
 && mkdir -p /data && chown ${UID}:${GID} /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hangar ./hangar
COPY docker/entrypoint.sh /usr/local/bin/hangar-entrypoint
RUN chmod +x /usr/local/bin/hangar-entrypoint

USER hangar
EXPOSE 7878
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD curl -fs http://127.0.0.1:7878/api/me -o /dev/null -w '%{http_code}' | grep -qE '200|401' || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "hangar-entrypoint"]
