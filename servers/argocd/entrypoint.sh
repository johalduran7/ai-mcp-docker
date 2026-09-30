#!/bin/bash
# argocd MCP entrypoint — starts a loopback relay so `argocd login --sso`
# is reachable through Docker's -p port publish, then execs the MCP server.
#
# argocd's OAuth2 callback listener binds to 127.0.0.1:$ARGOCD_SSO_PORT only.
# Docker's -p publish forwards inbound traffic to the container's bridge
# IP, not to its loopback interface, so without a relay the callback never
# lands. We use socat to bridge <bridge_ip>:$PORT -> 127.0.0.1:$PORT.
set -e

PORT="${ARGOCD_SSO_PORT:-8085}"

# Prefer the primary bridge IP; fall back to 0.0.0.0 if `hostname -I` is
# empty (host-network mode, minimal image variants, etc.).
CONTAINER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -z "${CONTAINER_IP}" ]; then
  CONTAINER_IP="0.0.0.0"
fi

if command -v socat >/dev/null 2>&1; then
  socat "TCP-LISTEN:${PORT},bind=${CONTAINER_IP},fork,reuseaddr" \
        "TCP:127.0.0.1:${PORT}" \
        >/tmp/argocd-sso-relay.log 2>&1 &
  echo "[argocd-mcp] SSO relay ${CONTAINER_IP}:${PORT} -> 127.0.0.1:${PORT}" >&2
else
  echo "[argocd-mcp] WARNING: socat not installed; SSO callback via docker -p will not work" >&2
fi

exec "$@"
