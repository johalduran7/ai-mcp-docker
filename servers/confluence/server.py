"""MyCompany Confluence MCP server.

Two sites are supported side-by-side and are selected per tool call:

  * ``cloud``   → MyCompany Confluence Cloud instance.
                  Base URL default: ``https://mycompany-prod.atlassian.net/wiki``
                  Auth: HTTP Basic with ``CONFLUENCE_CLOUD_EMAIL`` +
                  ``CONFLUENCE_CLOUD_TOKEN`` — the scheme classic
                  Atlassian Cloud API tokens require. If the email is
                  not set, the server falls back to Bearer, which works
                  for scoped API tokens / OAuth PATs but returns 403
                  ``"Current user not permitted to use Confluence"`` on
                  classic tokens.

  * ``onprem`` → MyCompany internal Confluence Data Center at
                  ``https://library.mycompany.com``.
                  Auth: Personal Access Token via ``Authorization: Bearer``.

If exactly one of the two tokens is configured, tools default to that
site when the caller passes ``site="auto"`` (the default). If both
are configured, the caller must pick one — or ``"both"`` on the
search-style tools that fan out.

Every tool that talks to Confluence goes through this file. Read tools
are always safe. Write tools rely on the AI host's per-tool-call
approval dialog for user consent — the same pattern used by the Jira,
AWS, and GitLab MCP servers in this repo.
"""

from __future__ import annotations

import base64
import concurrent.futures
import difflib
import html
import html.entities
import hashlib
import os
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

import requests
import urllib3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lvx-confluence")
from version_notifier import install as _install_version_notifier
_install_version_notifier(mcp, "lvx-confluence")

# TLS verification is intentionally always off: this server is only ever
# run on a host with the corporate VPN active, which MITM-inspects HTTPS
# traffic with a self-signed cert the container's CA store doesn't trust
# (same rationale as servers/github/server.py and servers/jira/server.py).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Site / auth configuration ──────────────────────────────────────────────

_DEFAULT_CLOUD_BASE = "https://mycompany-prod.atlassian.net/wiki"
_DEFAULT_ONPREM_BASE = "https://library.mycompany.com"

SITES: tuple[str, ...] = ("cloud", "onprem")


def _cloud_base_url() -> str:
    return os.environ.get("CONFLUENCE_CLOUD_BASE_URL", _DEFAULT_CLOUD_BASE).rstrip("/")


def _onprem_base_url() -> str:
    return os.environ.get("CONFLUENCE_ONPREM_BASE_URL", _DEFAULT_ONPREM_BASE).rstrip("/")


def _cloud_token() -> Optional[str]:
    return os.environ.get("CONFLUENCE_CLOUD_TOKEN") or None


def _cloud_email() -> Optional[str]:
    return os.environ.get("CONFLUENCE_CLOUD_EMAIL") or None


def _onprem_token() -> Optional[str]:
    return os.environ.get("CONFLUENCE_ONPREM_TOKEN") or None


def _configured_sites() -> list[str]:
    out: list[str] = []
    if _cloud_token():
        out.append("cloud")
    if _onprem_token():
        out.append("onprem")
    return out


def _resolve_site(site: str) -> str:
    """Return a concrete site name (``cloud`` or ``onprem``) or raise.

    ``site="auto"`` (or empty) resolves to the sole configured site.
    If neither is configured, raise. If both are configured and the
    caller asked for ``auto``, raise and force them to be explicit.
    ``"both"`` is NOT accepted here — it is only meaningful on the
    fan-out search tools which handle it themselves.
    """
    normalized = (site or "auto").strip().lower()
    configured = _configured_sites()
    if not configured:
        raise RuntimeError(
            "No Confluence tokens are configured. Set CONFLUENCE_CLOUD_TOKEN "
            "and/or CONFLUENCE_ONPREM_TOKEN in servers/confluence/.env."
        )
    if normalized in ("auto", ""):
        if len(configured) == 1:
            return configured[0]
        raise ValueError(
            "Both cloud and onprem tokens are configured — pass "
            "site='cloud' or site='onprem' explicitly."
        )
    if normalized not in SITES:
        raise ValueError(
            f"site must be one of {SITES} (or 'auto'/'both'); got '{site}'."
        )
    if normalized not in configured:
        raise RuntimeError(
            f"site '{normalized}' is requested but its token is not set. "
            f"Configured sites: {configured or 'none'}."
        )
    return normalized


def _resolve_sites_for_fanout(site: str) -> list[str]:
    """Resolve the site argument for fan-out search tools.

    ``"both"`` returns every configured site. Anything else defers to
    ``_resolve_site``.
    """
    normalized = (site or "auto").strip().lower()
    if normalized == "both":
        configured = _configured_sites()
        if not configured:
            raise RuntimeError(
                "No Confluence tokens are configured. Set "
                "CONFLUENCE_CLOUD_TOKEN and/or CONFLUENCE_ONPREM_TOKEN."
            )
        return configured
    return [_resolve_site(site)]


def _base_url(site: str) -> str:
    if site == "cloud":
        return _cloud_base_url()
    if site == "onprem":
        return _onprem_base_url()
    raise ValueError(f"Unknown site: {site}")


def _headers(site: str) -> dict[str, str]:
    if site == "cloud":
        token = _cloud_token()
        if not token:
            raise RuntimeError("CONFLUENCE_CLOUD_TOKEN is not set.")
        email = _cloud_email()
        # Classic Atlassian Cloud API tokens need Basic (email:token).
        # Bearer only works for scoped API tokens / OAuth PATs.
        if email:
            encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
            return {
                "Authorization": f"Basic {encoded}",
                "Accept": "application/json",
            }
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
    if site == "onprem":
        token = _onprem_token()
        if not token:
            raise RuntimeError("CONFLUENCE_ONPREM_TOKEN is not set.")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
    raise ValueError(f"Unknown site: {site}")


# ── Low-level HTTP wrappers ─────────────────────────────────────────────────

_DEFAULT_TIMEOUT = 30


def _get(site: str, path: str, params: Optional[dict[str, Any]] = None) -> requests.Response:
    return requests.get(
        f"{_base_url(site)}{path}",
        headers=_headers(site),
        params=params,
        timeout=_DEFAULT_TIMEOUT,
        verify=False,
    )


def _post(
    site: str,
    path: str,
    json_body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return requests.post(
        f"{_base_url(site)}{path}",
        headers={**_headers(site), "Content-Type": "application/json"},
        json=json_body,
        params=params,
        timeout=_DEFAULT_TIMEOUT,
        verify=False,
    )


def _put(
    site: str,
    path: str,
    json_body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    return requests.put(
        f"{_base_url(site)}{path}",
        headers={**_headers(site), "Content-Type": "application/json"},
        json=json_body,
        params=params,
        timeout=_DEFAULT_TIMEOUT,
        verify=False,
    )


def _delete(site: str, path: str, params: Optional[dict[str, Any]] = None) -> requests.Response:
    return requests.delete(
        f"{_base_url(site)}{path}",
        headers=_headers(site),
        params=params,
        timeout=_DEFAULT_TIMEOUT,
        verify=False,
    )


def _error_payload(site: str, response: requests.Response) -> dict[str, Any]:
    # Only documented typed fields from Confluence's JSON error envelope are
    # surfaced here — never the raw response body (Rule 1: no raw external
    # payloads in a tool response).
    message = f"Confluence request failed with status {response.status_code}."
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        typed_message = body.get("message") or body.get("reason")
        if isinstance(typed_message, str) and typed_message.strip():
            message = typed_message.strip()[:400]
    return {
        "ok": False,
        "site": site,
        "status_code": response.status_code,
        "error": message,
    }


# ── Common shaping / helpers ────────────────────────────────────────────────

# Confluence returns page URLs relative to the base. We want a fully-
# qualified URL that opens in the browser.
def _page_url(site: str, page: dict[str, Any]) -> Optional[str]:
    base = _base_url(site)
    # ``_links.webui`` is the canonical UI path on both Cloud and Server.
    links = page.get("_links") or {}
    web = links.get("webui")
    if web:
        # webui is typically ``/spaces/KEY/pages/12345/Title``. On Cloud
        # the base already ends in /wiki so we don't need to add anything.
        return f"{base}{web}"
    # Fall back to tinyui if present.
    tiny = links.get("tinyui")
    if tiny:
        return f"{base}{tiny}"
    pid = page.get("id")
    if pid:
        return f"{base}/pages/viewpage.action?pageId={pid}"
    return None


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_WS_RE = re.compile(r"\s+")


def _strip_html_to_text(html_body: str) -> str:
    """Cheap HTML → plain text conversion for view/storage bodies.

    Not perfect (no bs4 dependency) but good enough for the AI to reason
    over page contents without dragging the entire storage-format XHTML
    through the context window.
    """
    if not html_body:
        return ""
    # Line-break sensitive tags → newline before stripping.
    normalized = re.sub(r"</(p|div|li|h[1-6]|tr|br)>", "\n", html_body, flags=re.IGNORECASE)
    normalized = re.sub(r"<br\s*/?>", "\n", normalized, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", normalized)
    text = html.unescape(text)
    text = _MULTI_WS_RE.sub(" ", text).strip()
    # Restore paragraph-ish line breaks the multi-whitespace collapse ate.
    return re.sub(r" ?\n ?", "\n", text)


def _shape_page(site: str, page: dict[str, Any], *, include_body: bool = False) -> dict[str, Any]:
    """Return a compact, AI-friendly view of a Confluence content item.

    ``include_body`` extracts both the raw storage/view HTML AND a text
    projection so the AI can decide which to consume.
    """
    if not isinstance(page, dict):
        return {}
    space = page.get("space") or {}
    version = page.get("version") or {}
    history = page.get("history") or {}
    body = page.get("body") or {}
    storage = (body.get("storage") or {}).get("value") if isinstance(body, dict) else None
    view = (body.get("view") or {}).get("value") if isinstance(body, dict) else None

    shaped: dict[str, Any] = {
        "site": site,
        "id": page.get("id"),
        "type": page.get("type"),
        "status": page.get("status"),
        "title": page.get("title"),
        "space_key": space.get("key"),
        "space_name": space.get("name"),
        "version": version.get("number"),
        "version_by": (version.get("by") or {}).get("displayName"),
        "when": version.get("when") or history.get("lastUpdated", {}).get("when"),
        "created_by": (history.get("createdBy") or {}).get("displayName"),
        "created_when": history.get("createdDate"),
        "url": _page_url(site, page),
    }
    if include_body:
        if storage is not None:
            shaped["body_storage"] = storage
            shaped["body_text"] = _strip_html_to_text(storage)
        elif view is not None:
            shaped["body_view"] = view
            shaped["body_text"] = _strip_html_to_text(view)
    return shaped


def _shape_search_result(site: str, item: dict[str, Any]) -> dict[str, Any]:
    """Confluence ``/search`` returns items wrapped in ``{content, excerpt,
    resultGlobalContainer, ...}``. Flatten to the same shape as
    ``_shape_page`` plus the excerpt."""
    content = item.get("content") or {}
    shaped = _shape_page(site, content)
    excerpt_html = item.get("excerpt") or ""
    shaped.update(
        {
            "excerpt": _strip_html_to_text(excerpt_html),
            "last_modified": item.get("lastModified"),
            "friendly_last_modified": item.get("friendlyLastModified"),
        }
    )
    return shaped


# ── Current-user auto-discovery ─────────────────────────────────────────────
# The email tied to a token is not required for any auth path (both
# sites use Bearer), but we still surface "who am I" when asked. The
# lookup is cached per-site for the process lifetime so introspection
# tools don't hammer the endpoint on every call.

_current_user_cache: dict[str, dict[str, Any]] = {}


def _fetch_current_user(site: str) -> dict[str, Any]:
    """Return ``{ok, display_name, email, username, account_id, ...}`` for
    the identity behind the token. Cached per site for the process
    lifetime — the token is fixed at startup, so the answer never changes."""
    cached = _current_user_cache.get(site)
    if cached is not None:
        return cached
    try:
        response = _get(site, "/rest/api/user/current")
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _current_user_cache[site] = result
        return result
    if response.status_code >= 400:
        result = {
            "ok": False,
            "status_code": response.status_code,
            "error": (response.text or "")[:300],
        }
        _current_user_cache[site] = result
        return result
    try:
        data = response.json() or {}
    except ValueError:
        data = {}
    result = {
        "ok": True,
        "display_name": data.get("displayName"),
        "email": data.get("email") or data.get("emailAddress"),
        "username": data.get("username"),
        "account_id": data.get("accountId"),
        "user_key": data.get("userKey"),
        "account_type": data.get("accountType"),
    }
    _current_user_cache[site] = result
    return result


def _current_user_cql_ref(site: str) -> Optional[str]:
    """Return the token identity's CQL-usable id (username on onprem,
    accountId on cloud), unquoted. ``None`` if lookup fails."""
    user = _fetch_current_user(site)
    if not user.get("ok"):
        return None
    if site == "cloud":
        return user.get("account_id") or user.get("username")
    return user.get("username") or user.get("user_key") or user.get("account_id")


# ── Introspection ───────────────────────────────────────────────────────────


@mcp.tool()
def whoami_confluence(site: str = "auto") -> dict[str, Any]:
    """Return the identity attached to the token for a site. **Read-only.**

    Use this any time you need the current user's username / accountId
    (e.g. before filtering "pages I modified"). Result is cached per
    site for the process lifetime.

    Args:
        site: ``cloud``, ``onprem``, or ``auto`` (the sole configured).

    Returns ``{ok, site, display_name, email, username, account_id,
    user_key, account_type}``. On failure, ``ok=False`` with the upstream
    status/error.
    """
    resolved = _resolve_site(site)
    user = _fetch_current_user(resolved)
    return {"site": resolved, **user}


@mcp.tool()
def list_confluence_sites(include_current_user: bool = True) -> dict[str, Any]:
    """Report which Confluence sites this MCP is configured to talk to.

    Use this first when you're not sure whether ``cloud``, ``onprem``,
    or both are wired up. Returns each site's base URL, whether its
    token is set, and — unless ``include_current_user=False`` — the
    identity attached to the token (display name / email / username)
    auto-discovered from ``/rest/api/user/current``. Never returns the
    token itself. The current-user lookup is cached per site for the
    process lifetime.
    """
    configured = _configured_sites()
    sites: dict[str, dict[str, Any]] = {
        "cloud": {
            "base_url": _cloud_base_url(),
            "token_set": bool(_cloud_token()),
            "email_set": bool(_cloud_email()),
            "auth_mode": "basic" if _cloud_email() else "bearer",
            "purpose": (
                "MyCompany Confluence Cloud (Atlassian Cloud). Set "
                "CONFLUENCE_CLOUD_EMAIL + CONFLUENCE_CLOUD_TOKEN to use "
                "HTTP Basic auth — the scheme classic Atlassian Cloud "
                "API tokens require. Without the email the server falls "
                "back to Bearer, which only works for scoped API tokens "
                "/ OAuth PATs and 403s on classic tokens."
            ),
        },
        "onprem": {
            "base_url": _onprem_base_url(),
            "token_set": bool(_onprem_token()),
            "auth_mode": "bearer",
            "purpose": (
                "MyCompany internal Confluence Data Center. Configure "
                "CONFLUENCE_ONPREM_TOKEN with a Personal Access Token."
            ),
        },
    }
    if include_current_user:
        for f in configured:
            sites[f]["current_user"] = _fetch_current_user(f)
    return {
        "ok": True,
        "configured": configured,
        "sites": sites,
        "instruction": (
            "Only sites with a token set are usable. If both are "
            "configured, callers must pass site='cloud' or "
            "site='onprem' on every tool (or 'both' on search-style "
            "tools). If only one is configured, site='auto' works."
        ),
    }


# ── Spaces ──────────────────────────────────────────────────────────────────


@mcp.tool()
def list_confluence_spaces(
    site: str = "auto",
    search: str = "",
    space_type: str = "",
    max_results: int = 100,
    start_at: int = 0,
) -> dict[str, Any]:
    """List spaces visible to the token. **Read-only.**

    Args:
        site: ``cloud``, ``onprem``, or ``auto`` (the sole configured
            site). Fan-out ``"both"`` is NOT accepted here — use one
            call per site if you need both sides.
        search: Optional case-insensitive substring filter over space
            key / name / description. Applied client-side after the fetch.
        space_type: Optional Confluence space type filter, e.g.
            ``global``, ``personal``, ``archived``. Passed to Confluence.
        max_results: Page size (1..250). Default 100.
        start_at: Offset for pagination. Default 0.
    """
    if max_results < 1:
        raise ValueError("max_results must be >= 1.")
    max_results = min(int(max_results), 250)
    start_at = max(0, int(start_at))
    site_resolved = _resolve_site(site)

    params: dict[str, Any] = {
        "limit": max_results,
        "start": start_at,
        "expand": "description.plain,homepage",
    }
    if space_type.strip():
        params["type"] = space_type.strip().lower()

    response = _get(site_resolved, "/rest/api/space", params=params)
    if response.status_code >= 400:
        return _error_payload(site_resolved, response)
    payload = response.json() or {}
    results = payload.get("results") or []

    needle = (search or "").strip().lower()
    base = _base_url(site_resolved)
    shaped: list[dict[str, Any]] = []
    for entry in results:
        key = entry.get("key") or ""
        name = entry.get("name") or ""
        desc = ((entry.get("description") or {}).get("plain") or {}).get("value") or ""
        if needle and needle not in key.lower() and needle not in name.lower() and needle not in desc.lower():
            continue
        homepage = entry.get("homepage") or {}
        shaped.append(
            {
                "site": site_resolved,
                "id": entry.get("id"),
                "key": key,
                "name": name,
                "type": entry.get("type"),
                "description": desc,
                "homepage_id": homepage.get("id"),
                "homepage_title": homepage.get("title"),
                "url": f"{base}/spaces/{key}" if key else None,
            }
        )

    return {
        "ok": True,
        "site": site_resolved,
        "search": search or None,
        "space_type": space_type or None,
        "total_returned": len(results),
        "match_count": len(shaped),
        "start_at": start_at,
        "next_start_at": start_at + len(results) if len(results) == max_results else None,
        "spaces": shaped,
    }


# ── Recent activity ─────────────────────────────────────────────────────────


@mcp.tool()
def list_recent_pages(
    site: str = "auto",
    space_key: str = "",
    since_days: int = 7,
    max_results: int = 25,
    contributor: str = "",
    creator: str = "",
) -> dict[str, Any]:
    """List pages modified in the last ``since_days``. **Read-only.**

    Wrapper around ``/rest/api/search`` with a CQL filter of
    ``type=page AND lastmodified > now("-<N>d")``. Optionally scoped by
    ``space_key`` and by the user who touched (``contributor``) or
    authored (``creator``) the page.

    Args:
        site: ``cloud``, ``onprem``, ``auto``, or ``both`` (fan out).
        space_key: Optional space key to scope the search to.
        since_days: Look-back window in days. Default 7.
        max_results: Page size per site (1..100). Default 25.
        contributor: Filter to pages this user has edited (any version).
            Pass ``"me"`` to auto-resolve to the token's identity
            (username on onprem, accountId on cloud). This is the right
            filter for questions like "the last page I modified".
        creator: Filter to pages authored by this user. Same ``"me"``
            shortcut applies.
    """
    if since_days < 1:
        raise ValueError("since_days must be >= 1.")
    if max_results < 1:
        raise ValueError("max_results must be >= 1.")
    max_results = min(int(max_results), 100)
    sites_to_call = _resolve_sites_for_fanout(site)

    contributor_norm = (contributor or "").strip()
    creator_norm = (creator or "").strip()

    per_site: dict[str, dict[str, Any]] = {}
    for f in sites_to_call:
        parts = ['type = "page"', f'lastmodified > now("-{int(since_days)}d")']
        if space_key.strip():
            parts.append(f'space = "{space_key.strip().replace(chr(34), chr(92)+chr(34))}"')

        resolved_contributor = contributor_norm
        if resolved_contributor.lower() == "me":
            ref = _current_user_cql_ref(f)
            if not ref:
                per_site[f] = {
                    "ok": False,
                    "site": f,
                    "error": (
                        "Could not resolve 'me' — /rest/api/user/current "
                        "failed for this site. Call whoami_confluence "
                        "to diagnose."
                    ),
                }
                continue
            resolved_contributor = ref
        if resolved_contributor:
            parts.append(f'contributor = "{resolved_contributor}"')

        resolved_creator = creator_norm
        if resolved_creator.lower() == "me":
            ref = _current_user_cql_ref(f)
            if not ref:
                per_site[f] = {
                    "ok": False,
                    "site": f,
                    "error": "Could not resolve 'me' for creator filter.",
                }
                continue
            resolved_creator = ref
        if resolved_creator:
            parts.append(f'creator = "{resolved_creator}"')

        cql = " AND ".join(parts) + " ORDER BY lastmodified DESC"

        response = _get(
            f,
            "/rest/api/search",
            params={"cql": cql, "limit": max_results, "expand": "content.space,content.version,content.history"},
        )
        if response.status_code >= 400:
            per_site[f] = _error_payload(f, response)
            per_site[f]["cql"] = cql
            continue
        payload = response.json() or {}
        results = payload.get("results") or []
        per_site[f] = {
            "ok": True,
            "count": len(results),
            "cql": cql,
            "pages": [_shape_search_result(f, r) for r in results],
        }

    return {
        "ok": all(v.get("ok") for v in per_site.values()),
        "since_days": since_days,
        "space_key": space_key or None,
        "contributor": contributor or None,
        "creator": creator or None,
        "sites": sites_to_call,
        "results": per_site,
    }


# ── Search ──────────────────────────────────────────────────────────────────


def _run_search(
    site: str,
    cql: str,
    max_results: int,
    start_at: int,
    expand: Optional[list[str]],
) -> dict[str, Any]:
    """Single-site CQL search. Returned dict is uniform across sites."""
    expand_list = expand or ["content.space", "content.version", "content.history"]
    response = _get(
        site,
        "/rest/api/search",
        params={
            "cql": cql,
            "limit": max_results,
            "start": start_at,
            "expand": ",".join(expand_list),
        },
    )
    if response.status_code >= 400:
        payload = _error_payload(site, response)
        payload["cql"] = cql
        return payload
    body = response.json() or {}
    results = body.get("results") or []
    total = body.get("totalSize") or body.get("size") or len(results)
    next_start = start_at + len(results)
    return {
        "ok": True,
        "site": site,
        "cql": cql,
        "start_at": start_at,
        "count": len(results),
        "total": total,
        "has_more": bool(body.get("_links", {}).get("next")) or next_start < total,
        "next_start_at": next_start if next_start < total else None,
        "results": [_shape_search_result(site, r) for r in results],
    }


@mcp.tool()
def search_confluence(
    cql: str,
    site: str = "auto",
    max_results: int = 25,
    start_at: int = 0,
    expand: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Freestyle CQL search. **Read-only.**

    Args:
        cql: Any valid CQL. Examples:
             ``text ~ "OAQS deployment" AND type = page``
             ``space = "IN" AND title ~ "runbook"``
             ``label = "oaqs" AND lastmodified > now("-30d")``
             ``type = user AND user.fullname ~ "John"``
        site: ``cloud``, ``onprem``, ``auto``, or ``both``. When
            ``"both"``, this tool runs the same CQL against every
            configured site **in parallel** using a thread pool and
            returns a merged view.
        max_results: Page size per site (1..100). Default 25.
        start_at: Offset. Default 0.
        expand: Optional CQL expand list. Defaults to
            ``["content.space","content.version","content.history"]``.
    """
    if not cql or not cql.strip():
        raise ValueError("cql is required.")
    if max_results < 1:
        raise ValueError("max_results must be >= 1.")
    max_results = min(int(max_results), 100)
    start_at = max(0, int(start_at))
    sites_to_call = _resolve_sites_for_fanout(site)

    if len(sites_to_call) == 1:
        result = _run_search(sites_to_call[0], cql, max_results, start_at, expand)
        return {
            "ok": result.get("ok", False),
            "sites": sites_to_call,
            "cql": cql,
            "results": {sites_to_call[0]: result},
        }

    # Parallel fan-out across every configured site.
    per_site: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sites_to_call)) as pool:
        futures = {
            pool.submit(_run_search, f, cql, max_results, start_at, expand): f
            for f in sites_to_call
        }
        for future in concurrent.futures.as_completed(futures):
            f = futures[future]
            try:
                per_site[f] = future.result()
            except Exception as exc:  # noqa: BLE001
                per_site[f] = {
                    "ok": False,
                    "site": f,
                    "cql": cql,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    total_hits = sum(v.get("count", 0) for v in per_site.values() if v.get("ok"))
    return {
        "ok": all(v.get("ok") for v in per_site.values()),
        "sites": sites_to_call,
        "cql": cql,
        "total_hits_across_sites": total_hits,
        "results": per_site,
    }


@mcp.tool()
def batch_search_confluence(
    queries: list[str],
    site: str = "auto",
    max_results_per_query: int = 15,
    max_workers: int = 8,
    expand: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Run many CQL queries in parallel. **Read-only.**

    Designed for cases like sweeping every OAQS-deployment runbook, or
    checking a batch of ticket/component keywords across all Confluence
    pages in one call. Each query fans out per site (see ``site``),
    and all combinations run concurrently through a thread pool.

    Args:
        queries: List of CQL strings. Empty strings are skipped.
        site: ``cloud``, ``onprem``, ``auto``, or ``both``. ``"both"``
            runs each CQL against every configured site.
        max_results_per_query: Page size per query per site (1..100).
            Default 15.
        max_workers: Thread pool size. Default 8.
        expand: Optional CQL expand list applied to every query. Same
            default as ``search_confluence``.
    """
    if not isinstance(queries, list) or not queries:
        raise ValueError("queries must be a non-empty list of CQL strings.")
    if max_results_per_query < 1:
        raise ValueError("max_results_per_query must be >= 1.")
    max_results_per_query = min(int(max_results_per_query), 100)
    max_workers = max(1, min(int(max_workers), 32))
    sites_to_call = _resolve_sites_for_fanout(site)

    cleaned = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not cleaned:
        raise ValueError("queries had no non-empty entries.")

    jobs: list[tuple[str, str]] = [(q, f) for q in cleaned for f in sites_to_call]

    results_by_query: dict[str, dict[str, dict[str, Any]]] = {q: {} for q in cleaned}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(_run_search, f, q, max_results_per_query, 0, expand): (q, f)
            for (q, f) in jobs
        }
        for future in concurrent.futures.as_completed(future_map):
            q, f = future_map[future]
            try:
                results_by_query[q][f] = future.result()
            except Exception as exc:  # noqa: BLE001
                results_by_query[q][f] = {
                    "ok": False,
                    "site": f,
                    "cql": q,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    # Summary — total hits per query and per site, so the AI can pick
    # the interesting rows without iterating the whole payload.
    summary: list[dict[str, Any]] = []
    for q in cleaned:
        per = results_by_query[q]
        summary.append(
            {
                "cql": q,
                "hits_by_site": {f: per[f].get("count", 0) for f in sites_to_call if f in per},
                "ok_by_site": {f: per[f].get("ok", False) for f in sites_to_call if f in per},
                "total_hits": sum(per[f].get("count", 0) for f in sites_to_call if f in per and per[f].get("ok")),
            }
        )

    return {
        "ok": all(
            per[f].get("ok", False)
            for per in results_by_query.values()
            for f in per
        ),
        "sites": sites_to_call,
        "query_count": len(cleaned),
        "job_count": len(jobs),
        "max_workers": max_workers,
        "summary": summary,
        "results": results_by_query,
    }


# ── Page fetch ──────────────────────────────────────────────────────────────

# Callers often paste a full page URL. We extract the page id (or the
# space+title pair) so the caller doesn't have to.
_PAGE_ID_QS_RE = re.compile(r"[?&]pageId=(\d+)", re.IGNORECASE)
_PAGE_PATH_RE = re.compile(r"/pages/(\d+)(?:/|$)")


def _extract_page_id_from_url(url: str) -> Optional[str]:
    if not url or not url.strip():
        return None
    m = _PAGE_PATH_RE.search(url)
    if m:
        return m.group(1)
    m = _PAGE_ID_QS_RE.search(url)
    if m:
        return m.group(1)
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "pageId" in qs and qs["pageId"]:
        return qs["pageId"][0]
    return None


@mcp.tool()
def get_confluence_page(
    page_id: str = "",
    url: str = "",
    site: str = "auto",
    include_body: bool = True,
    body_format: str = "storage",
    expand: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Fetch a single Confluence page. **Read-only.**

    Args:
        page_id: Numeric page id (as a string). Either this or ``url``
            is required.
        url: A full page URL — the tool extracts the page id from either
            a ``/pages/<id>/...`` path or a ``?pageId=<id>`` query.
        site: ``cloud``, ``onprem``, or ``auto``.
        include_body: When True, include page body in the returned
            payload plus a plain-text projection so the AI can consume
            it without HTML noise.
        body_format: ``storage`` (Confluence XHTML, the source of truth)
            or ``view`` (rendered HTML). Ignored when ``include_body``
            is False.
        expand: Optional Confluence ``expand`` list. Defaults include
            body + version + space + ancestors + child page count.
    """
    if not page_id and not url:
        raise ValueError("Provide page_id or url.")
    if not page_id and url:
        extracted = _extract_page_id_from_url(url)
        if not extracted:
            raise ValueError(f"Could not extract page id from url: {url!r}")
        page_id = extracted
    body_format = (body_format or "storage").strip().lower()
    if body_format not in ("storage", "view"):
        raise ValueError("body_format must be 'storage' or 'view'.")
    site_resolved = _resolve_site(site)

    default_expand = ["version", "space", "history", "ancestors", "children.page.size"]
    if include_body:
        default_expand.append(f"body.{body_format}")
    expand_list = expand or default_expand

    response = _get(
        site_resolved,
        f"/rest/api/content/{page_id.strip()}",
        params={"expand": ",".join(expand_list)},
    )
    if response.status_code >= 400:
        return {**_error_payload(site_resolved, response), "page_id": page_id}
    data = response.json() or {}
    shaped = _shape_page(site_resolved, data, include_body=include_body)
    ancestors = [
        {"id": a.get("id"), "title": a.get("title")}
        for a in (data.get("ancestors") or [])
        if isinstance(a, dict)
    ]
    children = data.get("children") or {}
    child_page_count = ((children.get("page") or {}).get("size")) if isinstance(children, dict) else None
    return {
        "ok": True,
        **shaped,
        "ancestors": ancestors,
        "child_page_count": child_page_count,
    }


# ── Chunked / grep-able page reader ─────────────────────────────────────────
# Confluence pages can be huge. Instead of sending the whole storage/view body
# through the AI's context window, `read_confluence_page` streams the page in
# bounded chunks with cursor pagination, keeps a heading path with each chunk
# so the AI knows where it is, and can grep the page server-side to skip
# everything that doesn't match. Every response embeds the page's canonical
# URL so the AI can always cite / link back to the source.

_HEADING_TAG_RE = re.compile(r"<(h[1-6])[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)


def _split_page_into_sections(storage_html: str) -> list[dict[str, Any]]:
    """Split storage XHTML at heading boundaries. Return a list of
    ``{heading_path: [...], text: str}`` where ``heading_path`` is the
    stack of headings (h1 > h2 > ...) leading to that section's body."""
    if not storage_html:
        return []
    matches = list(_HEADING_TAG_RE.finditer(storage_html))
    stack: list[tuple[int, str]] = []
    sections: list[dict[str, Any]] = []

    def _flush(body_html: str) -> None:
        text = _strip_html_to_text(body_html)
        if text.strip():
            sections.append(
                {"heading_path": [t for _, t in stack], "text": text}
            )

    if not matches:
        _flush(storage_html)
        return sections

    cursor = 0
    for m in matches:
        _flush(storage_html[cursor:m.start()])
        level = int(m.group(1)[1])
        title = _strip_html_to_text(m.group(2))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        cursor = m.end()
    _flush(storage_html[cursor:])
    return sections


def _chunk_sections(sections: list[dict[str, Any]], chunk_chars: int) -> list[dict[str, Any]]:
    """Bucket section text into chunks bounded by ``chunk_chars``.

    Paragraph boundaries (``\\n\\n``) are respected when a section is too
    large. Every chunk keeps its section's heading path so the AI can
    orient itself even when the caller starts mid-page.
    """
    chunks: list[dict[str, Any]] = []
    for sec in sections:
        text = sec["text"].strip()
        if not text:
            continue
        heading_path = sec["heading_path"]
        if len(text) <= chunk_chars:
            chunks.append({"heading_path": heading_path, "text": text})
            continue
        paragraphs = re.split(r"\n\s*\n", text) if "\n\n" in text else text.split("\n")
        current = ""
        for para in paragraphs:
            para_clean = para.strip()
            if not para_clean:
                continue
            candidate = (current + "\n\n" + para_clean) if current else para_clean
            if len(candidate) > chunk_chars and current:
                chunks.append({"heading_path": heading_path, "text": current})
                current = para_clean
            else:
                current = candidate
        if current:
            chunks.append({"heading_path": heading_path, "text": current})
    return chunks


def _grep_chunk(text: str, regex: re.Pattern[str], context_chars: int) -> list[dict[str, Any]]:
    """Return per-match snippets ``{match, snippet, position}`` centered on
    each hit with ``context_chars`` on each side."""
    hits: list[dict[str, Any]] = []
    for m in regex.finditer(text):
        start = max(0, m.start() - context_chars)
        end = min(len(text), m.end() + context_chars)
        hits.append(
            {
                "match": m.group(0),
                "snippet": text[start:end],
                "position": [m.start(), m.end()],
            }
        )
    return hits


@mcp.tool()
def read_confluence_page(
    page_id: str = "",
    url: str = "",
    site: str = "auto",
    pattern: str = "",
    chunk_chars: int = 2500,
    cursor: str = "",
    max_chunks: int = 5,
    context_chars: int = 300,
) -> dict[str, Any]:
    """Read a Confluence page as **paginated, AI-friendly text chunks**. **Read-only.**

    Purpose: pages can be huge; feeding the whole body into the AI wastes
    tokens. This tool fetches the page once, converts storage XHTML to
    plain text with heading structure preserved, and returns bounded
    chunks with cursor pagination. Set ``pattern`` to filter server-side
    so only chunks containing matches come back — the AI never sees the
    rest of the page.

    Every response includes ``page.url`` (the canonical Confluence UI
    link) so the AI can always cite / link back to the source, even when
    it only receives partial content.

    Args:
        page_id: Numeric page id. Either this or ``url`` is required.
        url: A full Confluence page URL — id is extracted from
            ``/pages/<id>/...`` or ``?pageId=<id>``.
        site: ``cloud``, ``onprem``, or ``auto``.
        pattern: Optional case-insensitive regex. When set, only chunks
            that contain at least one match are returned, and each
            returned chunk includes a ``matches`` array of
            ``{match, snippet, position}``. When empty (default), chunks
            are returned sequentially.
        chunk_chars: Target maximum characters per chunk (500..10000).
            Default 2500 (~500 tokens).
        cursor: Opaque continuation token from a previous call's
            ``next_cursor``. Empty string starts from the beginning.
        max_chunks: How many chunks to return in this call (1..25).
            Default 5.
        context_chars: When ``pattern`` is set, how many characters of
            context to include on each side of each match (50..2000).
            Default 300.

    Returns ``{ok, site, mode, page, total_chunks_in_page,
    returned_chunks, cursor, next_cursor, has_more, chunks, instruction}``.
    In ``grep`` mode also includes ``pattern`` and ``total_matching_chunks``.
    """
    if not page_id and not url:
        raise ValueError("Provide page_id or url.")
    if not page_id and url:
        extracted = _extract_page_id_from_url(url)
        if not extracted:
            raise ValueError(f"Could not extract page id from url: {url!r}")
        page_id = extracted
    site_resolved = _resolve_site(site)
    max_chunks = max(1, min(int(max_chunks), 25))
    chunk_chars = max(500, min(int(chunk_chars), 10000))
    context_chars = max(50, min(int(context_chars), 2000))
    start_index = int(cursor) if cursor and cursor.isdigit() else 0

    response = _get(
        site_resolved,
        f"/rest/api/content/{page_id.strip()}",
        params={"expand": "body.storage,version,space,history"},
    )
    if response.status_code >= 400:
        return {**_error_payload(site_resolved, response), "page_id": page_id}
    data = response.json() or {}
    storage_html = ((data.get("body") or {}).get("storage") or {}).get("value") or ""

    sections = _split_page_into_sections(storage_html)
    all_chunks = _chunk_sections(sections, chunk_chars)
    total_chunks = len(all_chunks)
    total_chars = sum(len(c["text"]) for c in all_chunks)

    version_info = data.get("version") or {}
    history_info = data.get("history") or {}
    space_info = data.get("space") or {}
    page_meta: dict[str, Any] = {
        "id": data.get("id") or page_id,
        "title": data.get("title"),
        "space_key": space_info.get("key"),
        "space_name": space_info.get("name"),
        "version": version_info.get("number"),
        "last_modified": version_info.get("when")
        or (history_info.get("lastUpdated") or {}).get("when"),
        "url": _page_url(site_resolved, data),
    }

    if pattern:
        try:
            regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        except re.error as exc:
            return {
                "ok": False,
                "site": site_resolved,
                "mode": "grep",
                "pattern": pattern,
                "error": f"Invalid regex: {exc}",
                "page": page_meta,
            }
        matching: list[dict[str, Any]] = []
        for idx, ch in enumerate(all_chunks):
            hits = _grep_chunk(ch["text"], regex, context_chars)
            if hits:
                matching.append(
                    {
                        "chunk_index": idx,
                        "heading_path": ch["heading_path"],
                        "text": ch["text"],
                        "chars": len(ch["text"]),
                        "match_count": len(hits),
                        "matches": hits,
                    }
                )
        page_slice = matching[start_index : start_index + max_chunks]
        next_index = start_index + len(page_slice)
        has_more = next_index < len(matching)
        return {
            "ok": True,
            "site": site_resolved,
            "mode": "grep",
            "pattern": pattern,
            "page": page_meta,
            "total_chunks_in_page": total_chunks,
            "total_matching_chunks": len(matching),
            "total_chars_in_page": total_chars,
            "returned_chunks": len(page_slice),
            "cursor": str(start_index),
            "next_cursor": str(next_index) if has_more else None,
            "has_more": has_more,
            "chunks": page_slice,
            "instruction": (
                f"Only chunks matching /{pattern}/i are returned. "
                f"Call read_confluence_page again with cursor='{next_index}' "
                "for the next batch."
                if has_more
                else "All matching chunks returned. Full source at page.url."
            ),
        }

    page_slice = all_chunks[start_index : start_index + max_chunks]
    next_index = start_index + len(page_slice)
    has_more = next_index < total_chunks
    return {
        "ok": True,
        "site": site_resolved,
        "mode": "sequential",
        "page": page_meta,
        "total_chunks_in_page": total_chunks,
        "total_chars_in_page": total_chars,
        "returned_chunks": len(page_slice),
        "cursor": str(start_index),
        "next_cursor": str(next_index) if has_more else None,
        "has_more": has_more,
        "chunks": [
            {
                "chunk_index": start_index + i,
                "heading_path": c["heading_path"],
                "text": c["text"],
                "chars": len(c["text"]),
            }
            for i, c in enumerate(page_slice)
        ],
        "instruction": (
            f"Call read_confluence_page with cursor='{next_index}' for the "
            f"next {max_chunks} chunks. Full source at page.url."
            if has_more
            else "End of page reached. Full source at page.url."
        ),
    }


@mcp.tool()
def get_confluence_page_by_title(
    space_key: str,
    title: str,
    site: str = "auto",
    include_body: bool = True,
    body_format: str = "storage",
) -> dict[str, Any]:
    """Fetch a single page by ``space_key`` + exact ``title``. **Read-only.**

    Uses ``/rest/api/content?spaceKey=&title=`` which requires an exact
    (case-insensitive) title match. If no page matches, returns
    ``ok=False`` with ``candidates=[]``. When multiple pages match
    (rare — Confluence enforces title uniqueness inside a space), all
    are returned in ``candidates`` and no body is inlined.
    """
    if not space_key.strip() or not title.strip():
        raise ValueError("space_key and title are required.")
    site_resolved = _resolve_site(site)
    body_format = (body_format or "storage").strip().lower()
    if body_format not in ("storage", "view"):
        raise ValueError("body_format must be 'storage' or 'view'.")

    expand = ["version", "space", "history"]
    if include_body:
        expand.append(f"body.{body_format}")

    response = _get(
        site_resolved,
        "/rest/api/content",
        params={
            "spaceKey": space_key.strip(),
            "title": title.strip(),
            "expand": ",".join(expand),
            "limit": 5,
        },
    )
    if response.status_code >= 400:
        return _error_payload(site_resolved, response)
    payload = response.json() or {}
    results = payload.get("results") or []
    if not results:
        return {
            "ok": False,
            "site": site_resolved,
            "space_key": space_key,
            "title": title,
            "error": "No page found with that space_key + title.",
            "candidates": [],
        }
    if len(results) > 1:
        return {
            "ok": False,
            "site": site_resolved,
            "space_key": space_key,
            "title": title,
            "error": "Multiple pages matched (unusual for Confluence).",
            "candidates": [_shape_page(site_resolved, r) for r in results],
        }
    return {"ok": True, **_shape_page(site_resolved, results[0], include_body=include_body)}


@mcp.tool()
def list_page_children(
    page_id: str,
    site: str = "auto",
    max_results: int = 50,
    start_at: int = 0,
) -> dict[str, Any]:
    """List child pages of a page. **Read-only.**

    Args:
        page_id: Parent page id.
        site: ``cloud``, ``onprem``, or ``auto``.
        max_results: 1..250. Default 50.
        start_at: Offset. Default 0.
    """
    if not page_id.strip():
        raise ValueError("page_id is required.")
    if max_results < 1:
        raise ValueError("max_results must be >= 1.")
    max_results = min(int(max_results), 250)
    site_resolved = _resolve_site(site)

    response = _get(
        site_resolved,
        f"/rest/api/content/{page_id.strip()}/child/page",
        params={
            "limit": max_results,
            "start": max(0, int(start_at)),
            "expand": "version,space,history",
        },
    )
    if response.status_code >= 400:
        return {**_error_payload(site_resolved, response), "page_id": page_id}
    payload = response.json() or {}
    results = payload.get("results") or []
    return {
        "ok": True,
        "site": site_resolved,
        "parent_page_id": page_id,
        "count": len(results),
        "children": [_shape_page(site_resolved, r) for r in results],
    }


@mcp.tool()
def list_page_attachments(
    page_id: str,
    site: str = "auto",
    max_results: int = 100,
) -> dict[str, Any]:
    """List attachments on a page. **Read-only.**"""
    if not page_id.strip():
        raise ValueError("page_id is required.")
    if max_results < 1:
        raise ValueError("max_results must be >= 1.")
    max_results = min(int(max_results), 250)
    site_resolved = _resolve_site(site)

    response = _get(
        site_resolved,
        f"/rest/api/content/{page_id.strip()}/child/attachment",
        params={"limit": max_results, "expand": "version,history"},
    )
    if response.status_code >= 400:
        return {**_error_payload(site_resolved, response), "page_id": page_id}
    payload = response.json() or {}
    results = payload.get("results") or []
    base = _base_url(site_resolved)
    shaped = []
    for a in results:
        links = a.get("_links") or {}
        download = links.get("download")
        shaped.append(
            {
                "id": a.get("id"),
                "title": a.get("title"),
                "media_type": ((a.get("metadata") or {}).get("mediaType")) or a.get("mediaType"),
                "file_size": ((a.get("extensions") or {}).get("fileSize")),
                "version": (a.get("version") or {}).get("number"),
                "download_url": f"{base}{download}" if download else None,
            }
        )
    return {
        "ok": True,
        "site": site_resolved,
        "page_id": page_id,
        "count": len(shaped),
        "attachments": shaped,
    }


@mcp.tool()
def get_page_history(
    page_id: str,
    site: str = "auto",
    max_results: int = 25,
) -> dict[str, Any]:
    """List version history for a page. **Read-only.**"""
    if not page_id.strip():
        raise ValueError("page_id is required.")
    if max_results < 1:
        raise ValueError("max_results must be >= 1.")
    max_results = min(int(max_results), 100)
    site_resolved = _resolve_site(site)

    response = _get(
        site_resolved,
        f"/rest/api/content/{page_id.strip()}/version",
        params={"limit": max_results},
    )
    if response.status_code >= 400:
        return {**_error_payload(site_resolved, response), "page_id": page_id}
    payload = response.json() or {}
    results = payload.get("results") or []
    versions = [
        {
            "number": v.get("number"),
            "when": v.get("when"),
            "message": v.get("message"),
            "minor_edit": v.get("minorEdit"),
            "by": (v.get("by") or {}).get("displayName"),
            "by_username": (v.get("by") or {}).get("username")
            or (v.get("by") or {}).get("accountId"),
        }
        for v in results
    ]
    return {
        "ok": True,
        "site": site_resolved,
        "page_id": page_id,
        "count": len(versions),
        "versions": versions,
    }


# ── Users ───────────────────────────────────────────────────────────────────


@mcp.tool()
def search_confluence_users(
    query: str,
    site: str = "auto",
    max_results: int = 25,
) -> dict[str, Any]:
    """Find Confluence users by fullname / username / email. **Read-only.**

    Uses the CQL user-search path (``type=user AND user.fullname ~
    "query"``) so the same call works against Cloud and Data Center.
    """
    if not query or not query.strip():
        raise ValueError("query is required.")
    if max_results < 1:
        raise ValueError("max_results must be >= 1.")
    max_results = min(int(max_results), 100)
    site_resolved = _resolve_site(site)

    safe = query.strip().replace('"', '\\"')
    cql = f'type = "user" AND user.fullname ~ "{safe}"'
    response = _get(
        site_resolved,
        "/rest/api/search",
        params={"cql": cql, "limit": max_results},
    )
    if response.status_code >= 400:
        return _error_payload(site_resolved, response)
    payload = response.json() or {}
    results = payload.get("results") or []
    users: list[dict[str, Any]] = []
    for r in results:
        user = r.get("user") or {}
        users.append(
            {
                "display_name": user.get("displayName") or r.get("title"),
                "username": user.get("username"),
                "account_id": user.get("accountId"),
                "user_key": user.get("userKey"),
                "email": user.get("email"),
                "type": user.get("type"),
            }
        )
    return {
        "ok": True,
        "site": site_resolved,
        "cql": cql,
        "query": query,
        "count": len(users),
        "users": users,
    }


# ── Labels (read) ───────────────────────────────────────────────────────────


@mcp.tool()
def list_page_labels(
    page_id: str,
    site: str = "auto",
) -> dict[str, Any]:
    """List labels on a page. **Read-only.**"""
    if not page_id.strip():
        raise ValueError("page_id is required.")
    site_resolved = _resolve_site(site)
    response = _get(
        site_resolved, f"/rest/api/content/{page_id.strip()}/label"
    )
    if response.status_code >= 400:
        return {**_error_payload(site_resolved, response), "page_id": page_id}
    payload = response.json() or {}
    results = payload.get("results") or []
    return {
        "ok": True,
        "site": site_resolved,
        "page_id": page_id,
        "count": len(results),
        "labels": [
            {"name": r.get("name"), "prefix": r.get("prefix"), "id": r.get("id")}
            for r in results
        ],
    }


# ── Write operations ─────────────────────────────────────────────────────────
# Every write tool below requires an explicit ``confirmed_by_user=True`` on
# top of whatever approval prompt the AI host itself shows — a server-side
# guarantee that doesn't depend on the host UI being present. The first
# call (confirmed_by_user=False, the default) never touches Confluence; it
# only returns a preview of what would happen via ``_confirmation_gate``
# below. Only a second, explicitly-confirmed call performs the write. Same
# pattern as the Jira, AWS, and GitLab MCP servers in this repo.
#
# ``patch_confluence_page_fragment`` additionally keeps its own
# ``dry_run``/``confirm_token`` mechanism — that protects against writing
# over a page that changed since it was last fetched (a staleness check),
# which is a different problem than human sign-off, so both gates apply
# there.


# Caps applied to every confirmation preview — a caller can otherwise balloon
# the tool response (or over-expose content that doesn't need round-tripping
# through the LLM just to confirm a write) via an oversized comment body,
# page body, or diff.
_PREVIEW_MAX_STR_LEN = 4000
_PREVIEW_MAX_ITEMS = 25
_PREVIEW_MAX_DEPTH = 6


def _bound_preview_value(value: Any, _depth: int = 0) -> Any:
    """Recursively cap strings/lists/dicts inside a confirmation preview.
    Values are capped in place (keys kept) up to ``_PREVIEW_MAX_ITEMS``;
    beyond that, remaining keys/items are collapsed into a single
    truncation marker rather than echoed back in full."""
    if _depth > _PREVIEW_MAX_DEPTH:
        return "…(nested too deep, truncated)"
    if isinstance(value, str):
        if len(value) > _PREVIEW_MAX_STR_LEN:
            return value[:_PREVIEW_MAX_STR_LEN] + f"…(truncated, {len(value)} chars total)"
        return value
    if isinstance(value, dict):
        items = list(value.items())
        bounded = {k: _bound_preview_value(v, _depth + 1) for k, v in items[:_PREVIEW_MAX_ITEMS]}
        if len(items) > _PREVIEW_MAX_ITEMS:
            bounded["…truncated_keys"] = f"…({len(items) - _PREVIEW_MAX_ITEMS} more keys truncated)"
        return bounded
    if isinstance(value, list):
        bounded = [_bound_preview_value(v, _depth + 1) for v in value[:_PREVIEW_MAX_ITEMS]]
        if len(value) > _PREVIEW_MAX_ITEMS:
            bounded.append(f"…({len(value) - _PREVIEW_MAX_ITEMS} more items truncated)")
        return bounded
    return value


def _confirmation_gate(
    action: str, site: str, confirmed_by_user: bool, preview: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Return a ``needs_confirmation`` payload when ``confirmed_by_user`` is
    False, or ``None`` to proceed with the write. Every write tool in this
    server calls this first — no write executes until the caller explicitly
    re-invokes the same tool with ``confirmed_by_user=True``."""
    if confirmed_by_user:
        return None
    return {
        "ok": False,
        "needs_confirmation": True,
        "action": action,
        "site": site,
        "preview": _bound_preview_value(preview),
        "instruction": (
            f"This will perform '{action}' on Confluence ({site}). Show this "
            "preview to the user and, only after they explicitly approve, "
            "re-invoke this same tool with confirmed_by_user=True to "
            "execute it. Nothing has been written yet. Never set "
            "confirmed_by_user=True on the user's behalf."
        ),
    }


@mcp.tool()
def create_confluence_page(
    space_key: str,
    title: str,
    body: str,
    site: str = "auto",
    parent_page_id: str = "",
    body_format: str = "storage",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Create a new Confluence page. **Write. Requires `confirmed_by_user=True`.**

    The first call (default ``confirmed_by_user=False``) performs no write
    and instead returns a preview of the page that would be created. Show
    that preview to the user and, only after they explicitly approve,
    re-invoke this same tool with ``confirmed_by_user=True``. Never set
    this flag on the user's behalf.

    Args:
        space_key: Target space key (e.g. ``"IN"``).
        title: Page title. Must be unique within the space.
        body: Page body. Confluence storage XHTML by default; ``"view"``
            (rendered HTML) also accepted.
        site: ``cloud``, ``onprem``, or ``auto``.
        parent_page_id: Optional parent page id — creates the new page
            as a child of that page.
        body_format: ``storage`` (default) or ``view``.
        confirmed_by_user: Must be ``True`` to actually create the page.
    """
    if not space_key.strip() or not title.strip() or body is None:
        raise ValueError("space_key, title, and body are required.")
    body_format = (body_format or "storage").strip().lower()
    if body_format not in ("storage", "view"):
        raise ValueError("body_format must be 'storage' or 'view'.")
    site_resolved = _resolve_site(site)

    payload: dict[str, Any] = {
        "type": "page",
        "title": title.strip(),
        "space": {"key": space_key.strip()},
        "body": {body_format: {"value": body, "representation": body_format}},
    }
    if parent_page_id.strip():
        payload["ancestors"] = [{"id": parent_page_id.strip()}]

    gate = _confirmation_gate(
        "create_page",
        site_resolved,
        confirmed_by_user,
        {
            "space_key": space_key.strip(),
            "title": title.strip(),
            "parent_page_id": parent_page_id.strip() or None,
            "body_format": body_format,
            "body": body,
        },
    )
    if gate is not None:
        return gate

    response = _post(site_resolved, "/rest/api/content", json_body=payload)
    if response.status_code >= 400:
        return {
            **_error_payload(site_resolved, response),
            "action": "create_page",
            "space_key": space_key,
            "title": title,
        }
    data = response.json() or {}
    return {
        "ok": True,
        "action": "create_page",
        "site": site_resolved,
        "page_id": data.get("id"),
        "title": data.get("title"),
        "space_key": (data.get("space") or {}).get("key"),
        "version": (data.get("version") or {}).get("number"),
        "url": _page_url(site_resolved, data),
    }


@mcp.tool()
def update_confluence_page(
    page_id: str,
    body: str,
    site: str = "auto",
    title: str = "",
    body_format: str = "storage",
    version_comment: str = "",
    minor_edit: bool = False,
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Update an existing page's body (and optionally its title). **Write.
    Requires `confirmed_by_user=True`.**

    The tool first fetches the current version so the required version bump
    is computed for you — the caller does NOT need to pass a version number.
    The first call (default ``confirmed_by_user=False``) performs no write
    and instead returns a preview — including a unified diff when
    ``body_format="storage"`` — of what would change. Show that preview to
    the user and, only after they explicitly approve, re-invoke this same
    tool with ``confirmed_by_user=True``. Never set this flag on the user's
    behalf.

    Args:
        page_id: Target page id.
        body: New body content.
        site: ``cloud``, ``onprem``, or ``auto``.
        title: New title. Defaults to the current title if omitted.
        body_format: ``storage`` (default) or ``view``.
        version_comment: Optional edit comment shown in the page history.
        minor_edit: Mark this update as a minor edit. Default False.
        confirmed_by_user: Must be ``True`` to actually write.
    """
    if not page_id.strip() or body is None:
        raise ValueError("page_id and body are required.")
    body_format = (body_format or "storage").strip().lower()
    if body_format not in ("storage", "view"):
        raise ValueError("body_format must be 'storage' or 'view'.")
    site_resolved = _resolve_site(site)

    current, err = _fetch_page_body_for_patch(site_resolved, page_id.strip())
    if err is not None:
        return {**err, "action": "update_page", "page_id": page_id}
    assert current is not None
    current_version = (current.get("version") or {}).get("number") or 0
    next_version = int(current_version) + 1
    new_title = title.strip() or current.get("title")
    space_key = (current.get("space") or {}).get("key")
    current_body = ((current.get("body") or {}).get("storage") or {}).get("value") or ""

    preview: dict[str, Any] = {
        "page_id": page_id.strip(),
        "title_before": current.get("title"),
        "title_after": new_title,
        "version_before": current_version,
        "version_after": next_version,
    }
    if body_format == "storage":
        preview["diff"] = _storage_unified_diff(current_body, body)
    else:
        preview["new_body_view"] = body
    gate = _confirmation_gate("update_page", site_resolved, confirmed_by_user, preview)
    if gate is not None:
        return gate

    payload: dict[str, Any] = {
        "id": page_id.strip(),
        "type": current.get("type") or "page",
        "title": new_title,
        "space": {"key": space_key},
        "body": {body_format: {"value": body, "representation": body_format}},
        "version": {"number": next_version, "minorEdit": bool(minor_edit)},
    }
    if version_comment.strip():
        payload["version"]["message"] = version_comment.strip()

    response = _put(
        site_resolved,
        f"/rest/api/content/{page_id.strip()}",
        json_body=payload,
    )
    if response.status_code >= 400:
        return {
            **_error_payload(site_resolved, response),
            "action": "update_page",
            "page_id": page_id,
            "attempted_version": next_version,
        }
    data = response.json() or {}
    return {
        "ok": True,
        "action": "update_page",
        "site": site_resolved,
        "page_id": data.get("id"),
        "title": data.get("title"),
        "previous_version": current_version,
        "new_version": (data.get("version") or {}).get("number"),
        "url": _page_url(site_resolved, data),
    }


# Confluence storage format uses these two custom namespace prefixes for
# macros (ac:) and internal resource references (ri:). An XML parser
# refuses to parse a fragment using undeclared prefixes, so every
# well-formedness check binds them on a throwaway wrapper root.
_STORAGE_NS_WRAPPER = (
    '<root xmlns:ac="http://confluence.atlassian.com/ac" '
    'xmlns:ri="http://confluence.atlassian.com/ri">{0}</root>'
)

# XML only predefines these 5 entities; ElementTree already handles them
# (and numeric refs like &#160;) without help. Everything else — &nbsp;,
# &rarr;, &mdash;, &ldquo;, etc. — is legitimate in Confluence storage
# format but NOT valid standalone XML, so it must be neutralized to a
# literal character before parsing, or well-formed content gets rejected.
_XML_PREDEFINED_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_HTML_ENTITY_RE = re.compile(r"&(#?\w+);")


def _neutralize_html_entities(body: str) -> str:
    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name.startswith("#") or name in _XML_PREDEFINED_ENTITIES:
            return match.group(0)
        char = html.entities.html5.get(name + ";") or html.entities.html5.get(name)
        return char if char is not None else match.group(0)

    return _HTML_ENTITY_RE.sub(_sub, body)


def _check_storage_well_formed(body: str) -> Optional[str]:
    """Return ``None`` if ``body`` is well-formed Confluence storage XHTML,
    else a short human-readable parse-error description. This checks tag
    structure/nesting, not entity legitimacy — legitimate HTML entities
    are neutralized to literal characters first (see
    ``_neutralize_html_entities``)."""
    try:
        ET.fromstring(_STORAGE_NS_WRAPPER.format(_neutralize_html_entities(body)))
    except ET.ParseError as exc:
        return str(exc)
    return None


_PATCH_PREVIEW_PREFIX = "confluence_patch_"
_PATCH_PREVIEW_TTL_SECONDS = 15 * 60


def _new_patch_preview_dir() -> str:
    """Create a fresh temp dir for a dry-run body preview, first sweeping
    any prior preview dirs older than the TTL so confidential page content
    (written for the optional confluence_xhtml_preview.py workflow) doesn't
    accumulate on disk indefinitely."""
    base = tempfile.gettempdir()
    cutoff = time.time() - _PATCH_PREVIEW_TTL_SECONDS
    try:
        entries = os.listdir(base)
    except OSError:
        entries = []
    for name in entries:
        if not name.startswith(_PATCH_PREVIEW_PREFIX):
            continue
        stale_dir = os.path.join(base, name)
        try:
            if os.path.getmtime(stale_dir) < cutoff:
                shutil.rmtree(stale_dir, ignore_errors=True)
        except OSError:
            continue
    return tempfile.mkdtemp(prefix=_PATCH_PREVIEW_PREFIX)


def _fetch_page_body_for_patch(site: str, page_id: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Fetch a page's current version + title + space + storage body,
    needed by both the fragment-patch flow (to compute the replacement
    locally instead of requiring the caller to supply the full body) and
    by ``update_confluence_page`` (to build a diff preview)."""
    response = _get(
        site,
        f"/rest/api/content/{page_id}",
        params={"expand": "body.storage,version,space"},
    )
    if response.status_code >= 400:
        return None, _error_payload(site, response)
    return response.json() or {}, None


def _fragment_confirm_token(site: str, page_id: str, version: int, old_fragment: str, new_fragment: str) -> str:
    """Bind a dry-run preview to the exact site, page version, + edit it
    previewed.

    A write is only accepted if the caller echoes back the token a dry
    run just produced. Since the token is recomputed here from the
    *live* version fetched at write time, a stale token (page edited by
    someone else since the dry run, a token from a different
    old/new_fragment pair, or one computed against the other configured
    site) will not match — forcing a fresh dry run instead of silently
    writing over changed content. Each part is length-prefixed before
    hashing so a colon (or any other character) inside a fragment can't
    make two different operations hash to the same digest.
    """
    parts = [site, page_id, str(version), old_fragment, new_fragment]
    digest_input = "".join(f"{len(p)}\x00{p}" for p in parts).encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()[:16]


def _storage_unified_diff(current_body: str, new_body: str, context: int = 3) -> str:
    """Git-commit-style unified diff between two storage bodies.

    Confluence storage format is stored as one long line with no
    newlines, so a naive line-based diff would just show "the whole
    line changed" on both sides. Both bodies are first split at tag
    boundaries (a newline inserted exactly where one tag immediately
    follows another, i.e. only where zero characters of real content
    sit between them) — this never touches text content, only adds
    insignificant inter-tag whitespace, so it's purely a diffing aid
    and changes nothing about what gets sent to Confluence.
    """
    a = current_body.replace("><", ">\n<").splitlines()
    b = new_body.replace("><", ">\n<").splitlines()
    diff_lines = difflib.unified_diff(
        a, b, fromfile="current", tofile="proposed", n=context, lineterm=""
    )
    return "\n".join(diff_lines)


@mcp.tool()
def patch_confluence_page_fragment(
    page_id: str,
    old_fragment: str,
    new_fragment: str,
    site: str = "auto",
    expected_count: int = 1,
    dry_run: bool = True,
    confirm_token: str = "",
    version_comment: str = "",
    minor_edit: bool = False,
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Replace one small, exact substring of a page's storage body. **Write**
    (unless ``dry_run=True``, the default — then it's read-only). Requires
    `confirmed_by_user=True` in addition to a matching `confirm_token` to
    actually commit.

    Unlike ``update_confluence_page``, the caller never has to supply the
    full page body. This tool fetches the current body itself, does the
    substitution server-side, and — for adds, not just replacements —
    the same primitive works: pass an existing anchor string as
    ``old_fragment`` and ``anchor + new markup`` as ``new_fragment``.

    Safety mechanism: ``old_fragment`` must occur in the current body
    exactly ``expected_count`` times (default 1). If it occurs a
    different number of times, nothing is changed and an error is
    returned — this refuses to guess when the anchor is ambiguous or
    already gone (e.g. someone else edited the page since you last
    looked).

    Format checker: the resulting body is parsed as XML (with the
    Confluence ``ac:``/``ri:`` namespaces bound) before anything is
    written. Malformed markup (unbalanced tags, etc.) is rejected —
    dry run or not — instead of ever being sent to Confluence.

    ``dry_run=True`` (default) fetches the live body, validates, computes
    the replacement, and returns a ``diff`` field — a git-commit-style
    unified diff (``---``/``+++`` headers, ``@@`` hunks, ``+``/``-``
    lines) — plus a length-delta summary, all WITHOUT calling
    Confluence's update endpoint. Review that diff; it should show only
    the intended hunk. The full current/proposed bodies are also written
    to two local temp files (paths in the response) if you want to feed
    ``proposed_body_path`` to ``confluence_xhtml_preview.py`` for a
    rendered look before committing. These temp files are pruned after
    15 minutes — re-run with ``dry_run=True`` to regenerate them if you
    waited longer than that. Call again with ``dry_run=False`` only after
    you've reviewed those.

    Args:
        page_id: Target page id.
        old_fragment: Exact existing substring to anchor on (or replace).
        new_fragment: Its replacement (or ``old_fragment`` + new markup,
            for additions).
        site: ``cloud``, ``onprem``, or ``auto``.
        expected_count: Required number of occurrences of
            ``old_fragment`` in the current body. Defaults to 1.
        dry_run: When True (default), validate and preview only — no
            write. Set False to actually commit.
        confirm_token: Required when ``dry_run=False``. Must be the
            ``confirm_token`` a prior ``dry_run=True`` call on this exact
            ``old_fragment``/``new_fragment`` pair returned. Recomputed
            here against the page's *current* live version, so it goes
            stale the moment the page changes or the fragments differ —
            there is no way to write without a fresh matching dry run.
        version_comment: Optional edit comment shown in the page history.
        minor_edit: Mark this update as a minor edit. Default False.
        confirmed_by_user: Required (``True``) when ``dry_run=False``, on
            top of ``confirm_token``. The dry run only proves the diff
            matches the current page version — it does not by itself
            prove a human reviewed it. Show the dry run's ``diff`` to the
            user and only re-invoke with both ``dry_run=False`` and
            ``confirmed_by_user=True`` after they explicitly approve.
            Never set this flag on the user's behalf.
    """
    if not page_id.strip():
        raise ValueError("page_id is required.")
    if not page_id.strip().isdigit():
        raise ValueError("page_id must be numeric (Confluence's content id), not a path or URL.")
    if not old_fragment:
        raise ValueError("old_fragment must be a non-empty string.")
    if expected_count < 1:
        raise ValueError("expected_count must be >= 1.")
    site_resolved = _resolve_site(site)

    current, err = _fetch_page_body_for_patch(site_resolved, page_id.strip())
    if err is not None:
        return {**err, "action": "patch_page_fragment", "page_id": page_id}
    assert current is not None
    current_body = ((current.get("body") or {}).get("storage") or {}).get("value")
    if current_body is None:
        return {
            "ok": False,
            "action": "patch_page_fragment",
            "page_id": page_id,
            "error": "Could not read the current storage body from Confluence's response.",
        }

    actual_count = current_body.count(old_fragment)
    if actual_count != expected_count:
        return {
            "ok": False,
            "action": "patch_page_fragment",
            "page_id": page_id,
            "error": (
                f"old_fragment occurs {actual_count} time(s) in the current body, "
                f"expected exactly {expected_count}. Refusing to guess — pick a more "
                "specific anchor, or pass the actual count as expected_count if that "
                "many replacements are genuinely intended."
            ),
            "actual_count": actual_count,
            "expected_count": expected_count,
        }

    new_body = current_body.replace(old_fragment, new_fragment)
    parse_error = _check_storage_well_formed(new_body)
    if parse_error is not None:
        return {
            "ok": False,
            "action": "patch_page_fragment",
            "page_id": page_id,
            "error": f"Resulting body is not well-formed storage XHTML: {parse_error}",
            "hint": "Check that new_fragment has balanced, properly nested tags.",
        }

    length_delta = len(new_body) - len(current_body)
    current_version = (current.get("version") or {}).get("number") or 0
    fresh_token = _fragment_confirm_token(
        site_resolved, page_id.strip(), int(current_version), old_fragment, new_fragment
    )

    if dry_run:
        tmp_dir = _new_patch_preview_dir()
        current_path = os.path.join(tmp_dir, "current_body.xhtml")
        proposed_path = os.path.join(tmp_dir, "proposed_body.xhtml")
        with open(current_path, "w", encoding="utf-8") as f:
            f.write(current_body)
        with open(proposed_path, "w", encoding="utf-8") as f:
            f.write(new_body)
        diff_text = _storage_unified_diff(current_body, new_body)
        return {
            "ok": True,
            "action": "patch_page_fragment",
            "dry_run": True,
            "site": site_resolved,
            "page_id": page_id,
            "match_count": actual_count,
            "well_formed": True,
            "length_delta": length_delta,
            "diff": diff_text,
            "current_body_path": current_path,
            "proposed_body_path": proposed_path,
            "confirm_token": fresh_token,
            "instruction": (
                "Nothing was written. Review the `diff` field above (git-style "
                "unified diff) — it should show only the intended hunk. Show it "
                "to the user and, only after they explicitly approve, call again "
                f"with dry_run=False, confirm_token='{fresh_token}', and "
                "confirmed_by_user=True to commit. The token is only valid for "
                "this exact page version and fragment pair. Never set "
                "confirmed_by_user=True on the user's behalf."
            ),
        }

    if not confirm_token or confirm_token != fresh_token:
        return {
            "ok": False,
            "action": "patch_page_fragment",
            "page_id": page_id,
            "error": (
                "Missing or stale confirm_token. Call this tool with dry_run=True "
                "first (for this exact old_fragment/new_fragment pair), review the "
                "diff, then pass the confirm_token it returns. This also means a "
                "token from an earlier dry run goes stale automatically if the page "
                "was edited since — you'll get this same error and need a fresh one."
            ),
        }

    if not confirmed_by_user:
        return {
            "ok": False,
            "needs_confirmation": True,
            "action": "patch_page_fragment",
            "site": site_resolved,
            "page_id": page_id,
            "diff": _storage_unified_diff(current_body, new_body),
            "instruction": (
                "confirm_token matched, but confirmed_by_user=True is also "
                "required. Show the `diff` above to the user and, only after "
                "they explicitly approve, re-invoke this same tool with "
                "confirmed_by_user=True (keeping the same confirm_token). Never "
                "set confirmed_by_user=True on the user's behalf."
            ),
        }

    next_version = int(current_version) + 1
    new_title = current.get("title")
    space_key = (current.get("space") or {}).get("key")

    payload: dict[str, Any] = {
        "id": page_id.strip(),
        "type": current.get("type") or "page",
        "title": new_title,
        "space": {"key": space_key},
        "body": {"storage": {"value": new_body, "representation": "storage"}},
        "version": {"number": next_version, "minorEdit": bool(minor_edit)},
    }
    if version_comment.strip():
        payload["version"]["message"] = version_comment.strip()

    response = _put(
        site_resolved,
        f"/rest/api/content/{page_id.strip()}",
        json_body=payload,
    )
    if response.status_code >= 400:
        return {
            **_error_payload(site_resolved, response),
            "action": "patch_page_fragment",
            "page_id": page_id,
            "attempted_version": next_version,
        }
    data = response.json() or {}
    return {
        "ok": True,
        "action": "patch_page_fragment",
        "dry_run": False,
        "site": site_resolved,
        "page_id": data.get("id"),
        "title": data.get("title"),
        "match_count": actual_count,
        "length_delta": length_delta,
        "previous_version": current_version,
        "new_version": (data.get("version") or {}).get("number"),
        "url": _page_url(site_resolved, data),
    }


@mcp.tool()
def delete_confluence_page(
    page_id: str,
    site: str = "auto",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Trash a Confluence page. **Write. Destructive. Requires
    `confirmed_by_user=True`.**

    Sends the page to the space's trash. On Confluence Data Center this
    is reversible from the trash UI for a limited time; on Cloud the
    behavior is space-configurable. The first call (default
    ``confirmed_by_user=False``) performs no write and instead returns a
    preview of the page that would be trashed. Show that preview to the
    user and, only after they explicitly approve, re-invoke this same tool
    with ``confirmed_by_user=True``. Never set this flag on the user's
    behalf.

    Args:
        page_id: Target page id.
        site: ``cloud``, ``onprem``, or ``auto``.
        confirmed_by_user: Must be ``True`` to actually trash the page.
    """
    if not page_id.strip():
        raise ValueError("page_id is required.")
    site_resolved = _resolve_site(site)

    lookup = _get(site_resolved, f"/rest/api/content/{page_id.strip()}", params={"expand": "space"})
    if lookup.status_code >= 400:
        return {
            **_error_payload(site_resolved, lookup),
            "action": "delete_page",
            "page_id": page_id,
        }
    lookup_data = lookup.json() or {}
    current_title = lookup_data.get("title")
    current_space = (lookup_data.get("space") or {}).get("key")

    gate = _confirmation_gate(
        "delete_page",
        site_resolved,
        confirmed_by_user,
        {"page_id": page_id.strip(), "title": current_title, "space_key": current_space},
    )
    if gate is not None:
        return gate

    response = _delete(site_resolved, f"/rest/api/content/{page_id.strip()}")
    if response.status_code >= 400:
        return {
            **_error_payload(site_resolved, response),
            "action": "delete_page",
            "page_id": page_id,
        }
    return {
        "ok": True,
        "action": "delete_page",
        "site": site_resolved,
        "page_id": page_id,
        "status_code": response.status_code,
        "instruction": (
            "Page moved to space trash. Recovery is available from the "
            "space's trash UI where the retention policy allows it."
        ),
    }


@mcp.tool()
def add_confluence_comment(
    page_id: str,
    body: str,
    site: str = "auto",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Post a comment to a page. **Write. Requires `confirmed_by_user=True`.**

    The first call (default ``confirmed_by_user=False``) performs no write
    and instead returns a preview of the comment. Show that preview to the
    user and, only after they explicitly approve, re-invoke this same tool
    with ``confirmed_by_user=True``. Never set this flag on the user's
    behalf.
    """
    if not page_id.strip() or not body.strip():
        raise ValueError("page_id and body are required.")
    site_resolved = _resolve_site(site)
    payload = {
        "type": "comment",
        "container": {"id": page_id.strip(), "type": "page"},
        "body": {"storage": {"value": body, "representation": "storage"}},
    }
    gate = _confirmation_gate(
        "add_comment", site_resolved, confirmed_by_user, {"page_id": page_id.strip(), "body": body}
    )
    if gate is not None:
        return gate

    response = _post(site_resolved, "/rest/api/content", json_body=payload)
    if response.status_code >= 400:
        return {
            **_error_payload(site_resolved, response),
            "action": "add_comment",
            "page_id": page_id,
        }
    data = response.json() or {}
    return {
        "ok": True,
        "action": "add_comment",
        "site": site_resolved,
        "page_id": page_id,
        "comment_id": data.get("id"),
        "url": _page_url(site_resolved, data),
    }


@mcp.tool()
def add_confluence_labels(
    page_id: str,
    labels: list[str],
    site: str = "auto",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Add one or more labels to a page. **Write. Requires
    `confirmed_by_user=True`.**

    The first call (default ``confirmed_by_user=False``) performs no write
    and instead returns a preview of the labels that would be added. Show
    that preview to the user and, only after they explicitly approve,
    re-invoke this same tool with ``confirmed_by_user=True``. Never set
    this flag on the user's behalf.

    Args:
        page_id: Target page id.
        labels: List of label names (strings). Duplicates are silently
            deduped by Confluence.
        site: ``cloud``, ``onprem``, or ``auto``.
        confirmed_by_user: Must be ``True`` to actually add the labels.
    """
    if not page_id.strip():
        raise ValueError("page_id is required.")
    if not isinstance(labels, list) or not labels:
        raise ValueError("labels must be a non-empty list of strings.")
    cleaned = [str(l).strip() for l in labels if isinstance(l, str) and str(l).strip()]
    if not cleaned:
        raise ValueError("labels had no non-empty entries.")
    site_resolved = _resolve_site(site)

    gate = _confirmation_gate(
        "add_labels", site_resolved, confirmed_by_user, {"page_id": page_id.strip(), "labels": cleaned}
    )
    if gate is not None:
        return gate

    payload = [{"prefix": "global", "name": name} for name in cleaned]
    response = _post(
        site_resolved,
        f"/rest/api/content/{page_id.strip()}/label",
        json_body=payload,
    )
    if response.status_code >= 400:
        return {
            **_error_payload(site_resolved, response),
            "action": "add_labels",
            "page_id": page_id,
        }
    data = response.json() or {}
    results = data.get("results") or data if isinstance(data, list) else data.get("results") or []
    return {
        "ok": True,
        "action": "add_labels",
        "site": site_resolved,
        "page_id": page_id,
        "added": cleaned,
        "current_labels": [
            {"name": r.get("name"), "prefix": r.get("prefix"), "id": r.get("id")}
            for r in (results if isinstance(results, list) else [])
        ],
    }


# ── Freestyle escape hatch ───────────────────────────────────────────────────
# When a caller needs to hit a Confluence REST endpoint that isn't wrapped
# by a dedicated tool, use this. GET is read-only and always safe; POST/PUT/
# DELETE are writes gated by ``_confirmation_gate`` (confirmed_by_user=True
# required), same as every other write tool in this server.


@mcp.tool()
def confluence_request(
    path: str,
    method: str = "GET",
    site: str = "auto",
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Freestyle Confluence REST call. **GET is safe; POST/PUT/DELETE are
    writes and require `confirmed_by_user=True`.**

    Use this when the dedicated tools don't cover what you need — e.g.
    listing space permissions, poking at ``/rest/api/settings/...``,
    calling a Cloud-only v2 endpoint under ``/api/v2/...``, hitting
    ``/rest/api/user/current`` yourself, or exercising a CQL variant the
    wrapper tools don't expose.

    For POST/PUT/DELETE, the first call (default ``confirmed_by_user=False``)
    performs no write and instead returns a preview of the request. Show
    that preview to the user and, only after they explicitly approve,
    re-invoke this same tool with ``confirmed_by_user=True``. Never set
    this flag on the user's behalf.

    Args:
        path: REST path relative to the site's base URL, starting with
            ``/``. Examples: ``"/rest/api/user/current"``,
            ``"/rest/api/space/DEVOPS/permission"``,
            ``"/rest/api/content/12345/restriction/byOperation"``.
        method: One of ``GET`` (default), ``POST``, ``PUT``, ``DELETE``.
            Case-insensitive.
        site: ``cloud``, ``onprem``, or ``auto``.
        params: Optional query string parameters.
        json_body: Optional JSON body (for POST/PUT).
        confirmed_by_user: Must be ``True`` for POST/PUT/DELETE to
            actually execute. Ignored for GET.

    Returns ``{ok, site, method, path, status_code, json, text, url}``.
    ``json`` is the parsed response body when the server returned JSON;
    otherwise it's ``None`` and ``text`` holds the raw body.
    """
    if not path or not path.strip():
        raise ValueError("path is required and must start with '/'.")
    path_norm = path.strip()
    if not path_norm.startswith("/"):
        path_norm = "/" + path_norm
    method_norm = (method or "GET").strip().upper()
    if method_norm not in ("GET", "POST", "PUT", "DELETE"):
        raise ValueError("method must be one of GET, POST, PUT, DELETE.")
    site_resolved = _resolve_site(site)

    if method_norm != "GET":
        gate = _confirmation_gate(
            f"confluence_request:{method_norm}",
            site_resolved,
            confirmed_by_user,
            {"path": path_norm, "params": params, "json_body": json_body},
        )
        if gate is not None:
            return gate

    if method_norm == "GET":
        response = _get(site_resolved, path_norm, params=params)
    elif method_norm == "POST":
        response = _post(site_resolved, path_norm, json_body=json_body, params=params)
    elif method_norm == "PUT":
        response = _put(site_resolved, path_norm, json_body=json_body, params=params)
    else:
        response = _delete(site_resolved, path_norm, params=params)

    parsed_json: Optional[Any] = None
    text_body = response.text or ""
    if text_body:
        try:
            parsed_json = response.json()
        except ValueError:
            parsed_json = None

    return {
        "ok": response.status_code < 400,
        "site": site_resolved,
        "method": method_norm,
        "path": path_norm,
        "url": f"{_base_url(site_resolved)}{path_norm}",
        "status_code": response.status_code,
        "json": parsed_json,
        "text": None if parsed_json is not None else text_body[:2000],
    }


if __name__ == "__main__":
    mcp.run()
