# =============================================================================
# Multi-stage Dockerfile for the MyCompany MCP images.
#
# For each MCP server we expose TWO named targets:
#
#   * runtime-<name>    Cache-friendly runtime layer. NO BUILD_VERSION baked
#                       in — so its image ID is a stable content hash that
#                       the publish pipeline's source-hash check (see
#                       .github/scripts/detect_changed_services.py) compares
#                       against the registered SHA to detect real changes.
#
#   * versioned-<name>  Extends runtime-<name>, adds BUILD_VERSION and
#                       POLL_INTERVAL_MINUTES envs. This is what gets pushed
#                       to GHCR as ghcr.io/<owner>/lvx-<name>:latest.
#
# docker-compose.yml uses target=versioned-<name>.
#
# version_notifier.py is a shared module used by every server for the
# on-demand check_for_updates tool. It lives on PYTHONPATH=/app/lib so any
# server can `from version_notifier import install` without path hackery.
# =============================================================================

# ─── Base: python + bash + awsume + version_notifier (no per-server deps) ──
# Per-server requirements.txt installs happen independently in each
# runtime-<name> stage below — NOT merged here — so a dependency change in
# one server can't invalidate every other server's build cache or force an
# unrelated rebuild/republish.

FROM python:3.12-slim AS base

# Links every image built from this Dockerfile (all runtime-*/versioned-*
# stages inherit it) to this repo on GHCR's package page — otherwise a
# manually-pushed image sits unlinked under the org with no README/PR/commit
# trail. Namespace (johalduran7) must match the GHCR image owner.
LABEL org.opencontainers.image.source="https://github.com/johalduran7/ai-mcp-docker"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/root \
    PYTHONPATH=/app/lib

# bash is required for awsume-configure shell integration.
RUN apt-get update && apt-get install -y --no-install-recommends bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# awscli + awsume are the only truly shared runtime deps (setuptools is
# needed for awsume-configure on Python 3.12+). Per-server deps are
# installed independently per runtime-<name> stage below.
RUN pip install --no-cache-dir setuptools awscli awsume

# awsume shell integration so `awsume` (invoked from bash inside the aws
# server) picks up cached MFA sessions the same way it does on the host.
RUN awsume-configure --shell bash \
    --autocomplete-file /root/.bash_completion \
    --alias-file /root/.bashrc

# Shared library — every server imports `from version_notifier import install`.
# Lives on PYTHONPATH=/app/lib (set above in ENV).
COPY resources/version_notifier.py /app/lib/version_notifier.py


# ─── Runtime stages (per-server, NO BUILD_VERSION) ─────────────────────────
# Each stage copies ONLY that server's code so its image ID (used as the
# release SHA) changes only when that specific server's files change.

FROM base AS runtime-jira
COPY servers/jira/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY servers/jira /app/servers/jira
CMD ["python", "/app/servers/jira/server.py"]

FROM base AS runtime-confluence
COPY servers/confluence/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY servers/confluence /app/servers/confluence
CMD ["python", "/app/servers/confluence/server.py"]

FROM base AS runtime-aws
COPY servers/aws/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY servers/aws /app/servers/aws
CMD ["python", "/app/servers/aws/server.py"]

FROM base AS runtime-gitlab
COPY servers/gitlab/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY servers/gitlab /app/servers/gitlab
CMD ["python", "/app/servers/gitlab/server.py"]

FROM base AS runtime-github
COPY servers/github/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY servers/github /app/servers/github
CMD ["python", "/app/servers/github/server.py"]

FROM base AS runtime-grafana
COPY servers/grafana/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY servers/grafana /app/servers/grafana
CMD ["python", "/app/servers/grafana/server.py"]

FROM base AS runtime-opensearch
COPY servers/opensearch/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY servers/opensearch /app/servers/opensearch
CMD ["python", "/app/servers/opensearch/server.py"]


# The argocd runtime layer additionally installs the `argocd` CLI. Kept
# out of the shared base so the other server images stay slim — only
# runtime-argocd (and its versioned-argocd child) pull the ~50 MB binary.
FROM base AS runtime-argocd
COPY servers/argocd/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
ARG ARGOCD_CLI_VERSION=v3.3.0
# `socat` powers the loopback relay in entrypoint.sh so argocd's SSO
# callback (bound to 127.0.0.1 only) is reachable via docker -p publish.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates socat \
    && curl -fsSL -o /usr/local/bin/argocd \
        "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_CLI_VERSION}/argocd-linux-amd64" \
    && chmod 0755 /usr/local/bin/argocd \
    && argocd version --client --short \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*
COPY servers/argocd /app/servers/argocd
RUN chmod +x /app/servers/argocd/entrypoint.sh
ENTRYPOINT ["/app/servers/argocd/entrypoint.sh"]
CMD ["python", "/app/servers/argocd/server.py"]


# ─── Versioned stages (add BUILD_VERSION on top) ───────────────────────────
# These are what the release script pushes to GHCR. BUILD_VERSION is baked
# in as an ENV so the check_for_updates MCP tool can read it at runtime.

FROM runtime-jira AS versioned-jira
ARG BUILD_VERSION=dev
ARG POLL_INTERVAL_MINUTES=10
ENV BUILD_VERSION=${BUILD_VERSION} \
    POLL_INTERVAL_MINUTES=${POLL_INTERVAL_MINUTES}

FROM runtime-confluence AS versioned-confluence
ARG BUILD_VERSION=dev
ARG POLL_INTERVAL_MINUTES=10
ENV BUILD_VERSION=${BUILD_VERSION} \
    POLL_INTERVAL_MINUTES=${POLL_INTERVAL_MINUTES}

FROM runtime-aws AS versioned-aws
ARG BUILD_VERSION=dev
ARG POLL_INTERVAL_MINUTES=10
ENV BUILD_VERSION=${BUILD_VERSION} \
    POLL_INTERVAL_MINUTES=${POLL_INTERVAL_MINUTES}

FROM runtime-gitlab AS versioned-gitlab
ARG BUILD_VERSION=dev
ARG POLL_INTERVAL_MINUTES=10
ENV BUILD_VERSION=${BUILD_VERSION} \
    POLL_INTERVAL_MINUTES=${POLL_INTERVAL_MINUTES}

FROM runtime-github AS versioned-github
ARG BUILD_VERSION=dev
ARG POLL_INTERVAL_MINUTES=10
ENV BUILD_VERSION=${BUILD_VERSION} \
    POLL_INTERVAL_MINUTES=${POLL_INTERVAL_MINUTES}

FROM runtime-grafana AS versioned-grafana
ARG BUILD_VERSION=dev
ARG POLL_INTERVAL_MINUTES=10
ENV BUILD_VERSION=${BUILD_VERSION} \
    POLL_INTERVAL_MINUTES=${POLL_INTERVAL_MINUTES}

FROM runtime-opensearch AS versioned-opensearch
ARG BUILD_VERSION=dev
ARG POLL_INTERVAL_MINUTES=10
ENV BUILD_VERSION=${BUILD_VERSION} \
    POLL_INTERVAL_MINUTES=${POLL_INTERVAL_MINUTES}

FROM runtime-argocd AS versioned-argocd
ARG BUILD_VERSION=dev
ARG POLL_INTERVAL_MINUTES=10
ENV BUILD_VERSION=${BUILD_VERSION} \
    POLL_INTERVAL_MINUTES=${POLL_INTERVAL_MINUTES}


# Default target so `docker build .` with no --target still produces
# something (the jira image, matching the previous single-stage default).
FROM versioned-jira AS default
