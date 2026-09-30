"""On-demand version check for MCP images.

Wire-up
-------
Two lines at the top of every ``server.py``, immediately after the
``mcp = FastMCP(...)`` line::

    from version_notifier import install
    install(mcp, "lvx-<name>")   # e.g. "lvx-aws"

The GitHub MCP calls ``install(mcp, "lvx-github", fetches_versions=True)``.
It is the only tool that fetches the private version registry. Every other
MCP accepts that returned manifest and compares it with its own local
``BUILD_VERSION``.

What the tool does per invocation
---------------------------------
1. The GitHub MCP fetches the JSON registry from the no-cache GitHub Contents
    API using its ``GITHUB_TOKEN``.
2. The AI host passes that manifest to each MCP's ``check_for_updates`` tool.
3. Each MCP compares its local ``BUILD_VERSION`` with its own manifest entry.

Everything is stdlib-only (``urllib`` + ``json``) so no extra pip dependency
is added to any server image.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

_LOG = logging.getLogger("version_notifier")
if not _LOG.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[version_notifier] %(message)s"))
    _LOG.addHandler(_h)
    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False

# GitHub Contents API. We deliberately do NOT use raw.githubusercontent.com
# because it's fronted by Fastly with a ~5 min TTL that IGNORES query-param
# cache-busts and Cache-Control request headers, so a check_for_updates call
# made shortly after a release returns pre-release JSON. The Contents API
# is not edge-cached and returns the current file. Sending
# `Accept: application/vnd.github.v3.raw` makes GitHub return the raw file
# body (not the base64 envelope), so downstream parsing is unchanged.
_DEFAULT_VERSIONS_URL = (
    "https://api.github.com/repos/johalduran7/ai-mcp-docker/contents/versions/mcp-version.json?ref=versions"
)


def _current_version() -> str:
    return os.environ.get("BUILD_VERSION", "dev").strip() or "dev"


def _versions_url() -> str:
    return os.environ.get("MCP_VERSIONS_URL", _DEFAULT_VERSIONS_URL)


def _ghcr_owner() -> str:
    return os.environ.get("GHCR_OWNER", "johalduran7").strip() or "johalduran7"


def _ghcr_repo() -> str:
    return os.environ.get("GHCR_REPO", "ai-mcp-docker").strip() or "ai-mcp-docker"


def _pull_command(mcp_name: str) -> str:
    return f"docker pull ghcr.io/{_ghcr_owner()}/{_ghcr_repo()}/{mcp_name}:latest"


def _ghcr_image(mcp_name: str) -> str:
    return f"ghcr.io/{_ghcr_owner()}/{_ghcr_repo()}/{mcp_name}:latest"


def _fetch_versions(url: str, timeout: float = 5.0) -> dict | None:
    """Fetch and parse the version registry. Returns ``None`` on any
    network / parse / shape error — the caller reports it as an error field
    on the response so the user sees why the check failed.

    Uses the GitHub Contents API rather than raw.githubusercontent.com.
    The raw endpoint is Fastly-cached ~5 min AND ignores query-string cache
    busting, so a check_for_updates call made shortly after a release will
    return pre-release JSON. The Contents API is not edge-cached; combined
    with ``Accept: application/vnd.github.v3.raw`` it returns the current
    file body directly (no base64 decode needed).

    Corporate MITM proxies (Zscaler / Netskope / Palo Alto) present a
    self-signed cert in the TLS chain, which fails Python's default
    verification with ``[SSL: CERTIFICATE_VERIFY_FAILED] self-signed
    certificate in certificate chain``. We try verified TLS first; on
    an SSL failure (surfaced by urllib as either ``ssl.SSLError`` or
    ``urllib.error.URLError`` whose ``.reason`` is an ``ssl.SSLError``)
    we retry once with an unverified context so the update check keeps
    working behind those proxies. The registry is private, so this tool,
    which is registered only by the GitHub MCP, uses its ``GITHUB_TOKEN``
    for the Contents API request.
    """
    headers = {
        "User-Agent": "mcp-version-check/1",
        "Accept": "application/vnd.github.v3.raw",
        "Cache-Control": "no-cache",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        _LOG.info("GITHUB_TOKEN is not set; cannot fetch private version registry")
        return None
    headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    allow_insecure = os.environ.get("MCP_VERSIONS_INSECURE_SSL", "1") != "0"

    def _open(ctx: ssl.SSLContext | None) -> str:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 (public API URL)
            return resp.read().decode("utf-8")

    try:
        try:
            raw = _open(None)
        except (ssl.SSLError, urllib.error.URLError) as first_exc:
            reason = getattr(first_exc, "reason", None)
            is_ssl = isinstance(first_exc, ssl.SSLError) or isinstance(reason, ssl.SSLError)
            if not is_ssl or not allow_insecure:
                raise
            _LOG.info("TLS verify failed (%s); retrying with unverified context", first_exc)
            raw = _open(ssl._create_unverified_context())  # noqa: SLF001
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
        return None
    except Exception as exc:  # noqa: BLE001
        _LOG.info("version fetch failed: %s", exc)
        return None


def _do_check_for_updates(mcp_name: str, versions: dict | None) -> dict:
    """Compare ``mcp_name``'s baked version with a GitHub-fetched manifest."""
    local = _current_version()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload: dict = {
        "server": mcp_name,
        "local_version": local,
        "remote_version": None,
        "update_available": False,
        "release_message": "",
        "image": _ghcr_image(mcp_name),
        "pull_command": _pull_command(mcp_name),
        "checked_at": now,
    }

    if local == "dev":
        payload["note"] = (
            "This container was built without a BUILD_VERSION stamp "
            "(likely a local `docker compose build`, not a release). "
            "Version tracking is disabled for developer builds."
        )
        return payload

    if not isinstance(versions, dict):
        payload["error"] = (
            "A version manifest was not supplied. First call the GitHub MCP's "
            "`check_for_updates` tool, then pass its `versions` result here."
        )
        return payload

    entry = versions.get(mcp_name)
    if not isinstance(entry, dict):
        payload["note"] = (
            f"'{mcp_name}' is not registered in the remote version manifest yet. "
            "This is normal for a new server that hasn't gone through a release "
            "cycle. Once it's released, this tool will start reporting updates."
        )
        return payload

    remote_version = str(entry.get("version", "")).strip()
    remote_message = str(entry.get("release-message", "")).strip()
    payload["remote_version"] = remote_version or None
    payload["release_message"] = remote_message
    if remote_version and remote_version != local:
        payload["update_available"] = True
        payload["upgrade_instructions"] = [
            f"Run `{payload['pull_command']}` on the host to fetch the new image.",
            "Fully quit and reopen the AI host (Claude Desktop tray → Quit, "
            "or VS Code: reload window) so the next tool call spins up a "
            "container from the freshly-pulled image.",
            "Re-run `check_for_updates` after restart to confirm "
            "local_version now matches remote_version.",
        ]
    elif remote_version and remote_version == local:
        payload["note"] = "You are running the latest released version."
    else:
        payload["note"] = (
            "The remote manifest has an entry for this server but no version "
            "field. Cannot determine whether an update is available."
        )
    return payload


def _fetch_version_manifest() -> dict:
    """Fetch the fresh private registry for the GitHub MCP to hand to peers."""
    url = _versions_url()
    remote = _fetch_versions(url)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload: dict = {
        "server": "lvx-github",
        "checked_at": now,
        "versions_url": url,
    }

    if remote is None:
        payload["error"] = (
            f"Could not fetch the version registry at {url}. Check "
            "network connectivity from the container and retry."
        )
        return payload

    payload["versions"] = remote
    return payload


def install(mcp, mcp_name: str, *, fetches_versions: bool = False) -> None:
    """Register the ``check_for_updates`` tool on a FastMCP instance.

    The GitHub MCP passes ``fetches_versions=True`` and returns the fresh
    registry manifest. Other MCPs receive that manifest as ``versions`` and
    compare it with their own locally baked ``BUILD_VERSION``.
    """
    try:
        @mcp.tool()
        def check_for_updates(versions: dict | None = None) -> dict:
            """Check this MCP's version against GitHub's current manifest.

            For an upgrade request, call the GitHub MCP's
            ``check_for_updates`` first. It returns a fresh ``versions``
            manifest from the private registry. Pass that manifest to this
            tool, which compares this MCP's own ``BUILD_VERSION``. The GitHub
            MCP ignores ``versions`` and returns the fresh manifest itself.
            """
            if fetches_versions:
                return _fetch_version_manifest()
            return _do_check_for_updates(mcp_name, versions)

        _LOG.info("check_for_updates tool registered for %s", mcp_name)
    except Exception as exc:  # noqa: BLE001
        _LOG.info("failed to register check_for_updates tool: %s", exc)
