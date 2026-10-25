# Hangar: a web UI for Claude Code sessions, backed by tmux.
FROM python:3.12-slim-bookworm

ARG UID=1000
ARG GID=1000
# Claude Code version to install: stable, latest, or a version number like 2.1.310.
ARG CLAUDE_VERSION=stable
# Extra Debian packages your sessions need (compilers, language runtimes, ...), space separated.
ARG EXTRA_PACKAGES=""
# Let Claude install packages with sudo (each sudo command still asks you first). Set to false to turn off.
ARG ALLOW_SUDO=true

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
      tmux git curl ca-certificates ripgrep jq less nano procps openssh-client tini zstd unzip sudo \
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
 && mkdir -p /data /etc/hangar && chown ${UID}:${GID} /data \
 && apt-mark showmanual > /etc/hangar/base-packages.txt \
 && if [ "$ALLOW_SUDO" = "true" ]; then \
      echo 'hangar ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/hangar && chmod 440 /etc/sudoers.d/hangar; \
    fi

# Claude asks before every sudo command, and knows it's in a container where it can install packages.
COPY docker/claude-settings.json docker/container-prompt.md /etc/hangar/
COPY docker/apt-remember /etc/apt/apt.conf.d/99hangar-remember
RUN if [ "$ALLOW_SUDO" != "true" ]; then \
      printf '# Running inside Hangar\n\nThis session runs in a Docker container without sudo. If a task needs a missing system tool, tell the user to add it to EXTRA_PACKAGES in .env and rebuild.\n' > /etc/hangar/container-prompt.md; \
    fi
ENV HANGAR_CLAUDE_BASE_ARGS="--settings /etc/hangar/claude-settings.json --append-system-prompt-file /etc/hangar/container-prompt.md"

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
