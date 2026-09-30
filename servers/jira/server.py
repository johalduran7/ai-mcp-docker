import base64
import html
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

import requests
import urllib3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lvx-jira")
from version_notifier import install as _install_version_notifier
_install_version_notifier(mcp, "lvx-jira")

# TLS verification is intentionally always off: this server is only ever
# run on a host with the corporate VPN active, which MITM-inspects HTTPS
# traffic with a self-signed cert the container's CA store doesn't trust
# (same rationale as servers/github/server.py and servers/grafana/server.py).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Site / auth configuration ──────────────────────────────────────────────
# Two sites are supported side-by-side and are selected per tool call via
# ``site``, the same pattern the Confluence MCP server uses:
#
#   * ``onprem`` → MyCompany internal Jira Server/Data Center at
#                   ``https://jira.mycompany.com``.
#                   Auth: Personal Access Token via ``Authorization: Bearer``.
#
#   * ``cloud``   → MyCompany Jira Cloud instance.
#                   Base URL default: ``https://mycompany-prod.atlassian.net``
#                   Auth: HTTP Basic with ``JIRA_CLOUD_EMAIL`` +
#                   ``JIRA_CLOUD_TOKEN`` — the scheme classic Atlassian
#                   Cloud API tokens require. If the email is not set, the
#                   server falls back to Bearer, which works for scoped
#                   API tokens / OAuth PATs but 403s on classic tokens.
#
# If exactly one of the two tokens is configured, tools default to that
# site when the caller passes ``site="auto"`` (the default). If both are
# configured, the caller must be explicit.

SITES: tuple[str, ...] = ("onprem", "cloud")

_DEFAULT_ONPREM_BASE = "https://jira.mycompany.com"
_DEFAULT_CLOUD_BASE = "https://mycompany-prod.atlassian.net"


def _onprem_base_url() -> str:
    return os.environ.get("JIRA_BASE_URL", _DEFAULT_ONPREM_BASE).rstrip("/")


def _cloud_base_url() -> str:
    return os.environ.get("JIRA_CLOUD_BASE_URL", _DEFAULT_CLOUD_BASE).rstrip("/")


def _onprem_token() -> Optional[str]:
    return os.environ.get("JIRA_TOKEN") or None


def _cloud_token() -> Optional[str]:
    return os.environ.get("JIRA_CLOUD_TOKEN") or None


def _cloud_email() -> Optional[str]:
    return os.environ.get("JIRA_CLOUD_EMAIL") or None


def _configured_sites() -> list[str]:
    out: list[str] = []
    if _onprem_token():
        out.append("onprem")
    if _cloud_token():
        out.append("cloud")
    return out


def _resolve_site(site: str) -> str:
    """Return a concrete site name (``onprem`` or ``cloud``) or raise.

    ``site="auto"`` (or empty) resolves to the sole configured site. If
    neither is configured, raise. If both are configured and the caller
    asked for ``auto``, raise and force them to be explicit.
    """
    normalized = (site or "auto").strip().lower()
    configured = _configured_sites()
    if not configured:
        raise RuntimeError(
            "No Jira tokens are configured. Set JIRA_TOKEN (onprem) "
            "and/or JIRA_CLOUD_EMAIL + JIRA_CLOUD_TOKEN (cloud) in "
            "servers/jira/.env."
        )
    if normalized in ("auto", ""):
        if len(configured) == 1:
            return configured[0]
        raise ValueError(
            "Both onprem and cloud Jira tokens are configured — pass "
            "site='onprem' or site='cloud' explicitly."
        )
    if normalized not in SITES:
        raise ValueError(f"site must be one of {SITES} (or 'auto'); got '{site}'.")
    if normalized not in configured:
        raise RuntimeError(
            f"site '{normalized}' is requested but its token is not set. "
            f"Configured sites: {configured or 'none'}."
        )
    return normalized


def _jira_base_url(site: str) -> str:
    if site == "onprem":
        return _onprem_base_url()
    if site == "cloud":
        return _cloud_base_url()
    raise ValueError(f"Unknown site: {site}")


def _jira_headers(site: str) -> dict[str, str]:
    if site == "onprem":
        token = _onprem_token()
        if not token:
            raise RuntimeError("JIRA_TOKEN is not set. Add it to servers/jira/.env.")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
    if site == "cloud":
        token = _cloud_token()
        if not token:
            raise RuntimeError(
                "JIRA_CLOUD_TOKEN is not set. Add it to servers/jira/.env."
            )
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
    raise ValueError(f"Unknown site: {site}")


def _jira_api_path(site: str, path: str) -> str:
    """Jira Cloud (``cloud``) requires an explicit REST API version
    (``2`` or ``3``) — unlike Server/Data Center (``onprem``), which
    accepts ``latest`` as an alias for the current version. Every call
    site in this file is written against ``/rest/api/latest/...``; this
    transparently swaps that segment for Cloud so nothing else has to
    change."""
    if site == "cloud":
        return path.replace("/rest/api/latest/", "/rest/api/2/", 1)
    return path

# ── Ready-to-deploy policy ───────────────────────────────────────────────────
# CANONICAL DEFINITION — do not reinterpret this at the AI-host level.
#
#   * ``NON_PRODUCTION_ENVS`` below is the COMPLETE, EXHAUSTIVE list of
#     non-production environments. Every env in this set is non-production,
#     including ``au1``, ``eu1``, ``uk2``, ``uk3`` — the geographic prefix
#     (au/eu/uk/na) does NOT determine production status.
#   * ``PRODUCTION_ENVS`` below is the COMPLETE, EXHAUSTIVE list of
#     production environments (``na3, na4, na5, na6``).
#   * Any other env label returned by Jira is classified as ``unknown`` —
#     it is NEITHER non-production NOR production, and the AI must not
#     invent a classification for it.
#
# ``NON_PRODUCTION_ENVS`` is kept in sync with ``REDEPLOYABLE_ENVS`` on the
# gitlab server so a single env label means the same thing on both sides.
NON_PRODUCTION_ENVS: frozenset[str] = frozenset(
    {"au1", "eu1", "stg4", "tst2", "tst3", "uk2", "uk3"}
)

PRODUCTION_ENVS: frozenset[str] = frozenset(
    {"na3", "na4", "na5", "na6"}
)

# Custom fields:
#   * customfield_16400 → environment (typed value like "NA4", "EU1", ...).
#   * customfield_12619 → order key used by the deploy queue JQL.
JIRA_ENV_FIELD = "customfield_16400"
JIRA_ORDER_FIELD = "customfield_12619"

# Broad token match for envs in ticket summaries when the env field isn't set:
# NA1..NA9, EU1..EU9, UK1..UK9, AU1..AU9, TST1..TST9, STG1..STG9.
_ENV_SUMMARY_RE = re.compile(
    r"\b(NA[0-9]|EU[0-9]|UK[0-9]|AU[0-9]|TST[0-9]|STG[0-9])\b",
    re.IGNORECASE,
)


def _extract_env(env_field_value: Any, summary: Optional[str]) -> Optional[str]:
    """Normalize the env label to lowercase. Prefer the env custom field; fall
    back to a regex match in the summary."""
    if isinstance(env_field_value, dict):
        raw = env_field_value.get("value") or env_field_value.get("name")
    elif isinstance(env_field_value, list) and env_field_value:
        first = env_field_value[0]
        raw = first.get("value") if isinstance(first, dict) else first
    else:
        raw = env_field_value

    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()

    if summary:
        match = _ENV_SUMMARY_RE.search(summary)
        if match:
            return match.group(1).lower()

    return None


def _classify_env(env: Optional[str]) -> str:
    if not env:
        return "unknown"
    if env in NON_PRODUCTION_ENVS:
        return "non_production"
    if env in PRODUCTION_ENVS:
        return "production"
    return "unknown"


@mcp.tool()
def list_jira_tickets(
    jql: str = "project != DM AND issuetype in ('Change Request') ORDER BY updated DESC",
    max_results: int = 20,
    site: str = "auto",
) -> list[dict[str, Any]]:
    """Return a minimal list of Jira tickets for a given JQL query.

    Args:
        jql: JQL query string.
        max_results: Cap on returned tickets (1..100). Default 20.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
    """
    if max_results < 1:
        raise ValueError("max_results must be >= 1")
    if max_results > 100:
        max_results = 100
    site_resolved = _resolve_site(site)

    status_code, result, err = _jira_search_jql(
        site_resolved,
        jql,
        fields=["summary", "status", "assignee", "priority", "updated"],
        max_results=max_results,
    )
    if status_code >= 400:
        raise RuntimeError(f"Jira search failed ({status_code}): {err}")

    items: list[dict[str, Any]] = []
    for issue in result["issues"]:
        fields = issue.get("fields", {})
        assignee = fields.get("assignee") or {}
        priority = fields.get("priority") or {}
        status = fields.get("status") or {}
        items.append(
            {
                "key": issue.get("key"),
                "summary": fields.get("summary"),
                "status": status.get("name"),
                "priority": priority.get("name"),
                "assignee": assignee.get("displayName"),
                "updated": fields.get("updated"),
            }
        )

    return items


@mcp.tool()
def list_ready_to_deploy_tickets(
    env: str = "",
    scope: str = "all",
    since_days: int = 7,
    max_results: int = 50,
) -> dict[str, Any]:
    """Return DevOps tickets currently ready to deploy, with env classification.

    **Environment taxonomy — read this carefully, do not reinterpret it.**

    Non-production envs (COMPLETE list): ``au1, eu1, stg4, tst2, tst3, uk2, uk3``.
    Every env in that list is non-production. The geographic prefix
    (``au`` / ``eu`` / ``uk`` / ``na``) does NOT determine production
    status — ``au1``, ``eu1``, ``uk2``, ``uk3`` are all non-production.

    Production envs (COMPLETE list): ``na3, na4, na5, na6``.

    Any other env label is classified as ``unknown`` — do NOT invent a
    production/non-production label for it.

    Base JQL (fixed):
        project = DevOps AND comment ~ APPROVED AND Status = "Ready for deployment"
        AND "Schedule Date" > -<since_days>d
        ORDER BY cf[12619] ASC, createdDate DESC

    Env resolution per ticket:
      * primary source is ``customfield_16400`` (env pick-list, e.g. ``NA4``).
      * fallback: a token like ``NA4`` / ``EU1`` / ``STG4`` found in the summary.
      * result is normalized to lowercase; ``env_class`` is one of
        ``non_production`` (env is in the non-prod whitelist),
        ``production`` (env is in the prod whitelist),
        or ``unknown`` (env is neither, or couldn't be extracted).

    Args:
        env: Optional exact env filter (case-insensitive, e.g. ``"na4"``).
        scope: ``"all"`` (default), ``"non_production"``, or ``"production"``.
            Applied after env extraction so unknown envs are excluded from
            production/non-production filters.
        since_days: Look-back window for the Schedule Date filter. Default 7.
        max_results: Server-side cap (1..200). Default 50.

    Returns a dict with ``tickets`` (filtered), plus counts, the JQL used,
    and an ``env_policy`` block spelling out the taxonomy above so the AI
    host cannot mis-classify envs when reporting to the user.
    """
    if max_results < 1:
        raise ValueError("max_results must be >= 1")
    max_results = min(max_results, 200)
    if since_days < 1:
        raise ValueError("since_days must be >= 1")

    scope_lower = (scope or "all").strip().lower()
    if scope_lower not in ("all", "production", "non_production"):
        raise ValueError("scope must be 'all', 'production', or 'non_production'.")

    env_filter = env.strip().lower() if env else ""
    # This tool encodes MyCompany-specific project/field conventions (DevOps
    # project, cf[16400]/cf[12619]) that don't generalize to cloud Jira, so
    # it's intentionally pinned to the onprem site rather than exposing a
    # `site` parameter.
    site_resolved = "onprem"

    jql = (
        'project = DevOps AND comment ~ APPROVED AND Status = "Ready for deployment" '
        f'AND "Schedule Date" > -{since_days}d '
        'ORDER BY cf[12619] ASC, createdDate DESC'
    )

    response = requests.get(
        f"{_jira_base_url(site_resolved)}/rest/api/latest/search",
        headers=_jira_headers(site_resolved),
        params={
            "jql": jql,
            "maxResults": max_results,
            "fields": ",".join(
                [
                    "summary",
                    "status",
                    "assignee",
                    "priority",
                    "created",
                    "updated",
                    JIRA_ENV_FIELD,
                    JIRA_ORDER_FIELD,
                ]
            ),
        },
        timeout=30,
        verify=False,
    )
    response.raise_for_status()
    payload = response.json()

    base = _jira_base_url(site_resolved)
    all_tickets: list[dict[str, Any]] = []
    for issue in payload.get("issues", []):
        fields = issue.get("fields", {}) or {}
        summary = fields.get("summary")
        assignee = fields.get("assignee") or {}
        priority = fields.get("priority") or {}
        status = fields.get("status") or {}

        env_value = _extract_env(fields.get(JIRA_ENV_FIELD), summary)
        env_class = _classify_env(env_value)

        key = issue.get("key")
        all_tickets.append(
            {
                "key": key,
                "url": f"{base}/browse/{key}" if key else None,
                "summary": summary,
                "status": status.get("name"),
                "priority": priority.get("name"),
                "assignee": assignee.get("displayName"),
                "created": fields.get("created"),
                "updated": fields.get("updated"),
                "env": env_value,
                "env_class": env_class,
                "order_key": fields.get(JIRA_ORDER_FIELD),
            }
        )

    filtered: list[dict[str, Any]] = []
    for ticket in all_tickets:
        if env_filter and ticket.get("env") != env_filter:
            continue
        if scope_lower == "non_production" and ticket["env_class"] != "non_production":
            continue
        if scope_lower == "production" and ticket["env_class"] != "production":
            continue
        filtered.append(ticket)

    # Per-env grouping so the AI host doesn't have to re-derive it (and can't
    # get it wrong). Only envs actually present in the filtered result set are
    # included; each entry lists the ticket keys under that env.
    tickets_by_env: dict[str, list[str]] = {}
    for ticket in filtered:
        env_key = ticket.get("env") or "unknown"
        tickets_by_env.setdefault(env_key, []).append(ticket.get("key") or "")

    env_policy = {
        "non_production_envs": sorted(NON_PRODUCTION_ENVS),
        "production_envs": sorted(PRODUCTION_ENVS),
        "rule": (
            "An env is non-production if and only if it appears in "
            "`non_production_envs`. An env is production if and only if it "
            "appears in `production_envs`. Both lists are exhaustive. Any "
            "other env label is classified as `unknown` — do NOT invent a "
            "production/non-production label for it, and do NOT infer status "
            "from the env's geographic prefix (au / eu / uk / na). All of "
            "au1, eu1, uk2, uk3 are non-production; only na3, na4, na5, na6 "
            "are production."
        ),
    }

    instruction = (
        "When reporting these tickets to the user, group them by `env` (see "
        "`tickets_by_env`) and label each env using `env_policy.rule`. Do NOT "
        "restate the env lists from memory — quote `env_policy.non_production_envs` "
        "and `env_policy.production_envs` verbatim. Never re-classify "
        "au1/eu1/uk2/uk3 as production based on their geographic prefix, and "
        "never call an env production unless it is in `env_policy.production_envs`."
    )

    return {
        "jql": jql,
        "since_days": since_days,
        "scope": scope_lower,
        "env_filter": env_filter or None,
        "total_returned_by_jira": len(all_tickets),
        "match_count": len(filtered),
        "env_policy": env_policy,
        "tickets_by_env": tickets_by_env,
        "tickets": filtered,
        "instruction": instruction,
    }


# ── Freestyle / discovery ────────────────────────────────────────────────────
# These tools let the AI host send arbitrary JQL and discover what fields
# and projects exist. They are all READ-ONLY. Write tools live at the
# bottom of this file and rely on the AI host's per-call approval prompt
# (VS Code's MCP tool-approval dialog, or the equivalent) for user
# consent — no server-side confirmation token is used.

# Default fields returned when the caller doesn't specify. Includes the
# MyCompany-specific custom fields so callers who care about env / deploy
# ordering get them without extra plumbing.
_DEFAULT_SEARCH_FIELDS: tuple[str, ...] = (
    "summary",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "reporter",
    "labels",
    "created",
    "updated",
    "resolution",
    JIRA_ENV_FIELD,
    JIRA_ORDER_FIELD,
)


def _jira_get(
    site: str, path: str, params: Optional[dict[str, Any]] = None
) -> requests.Response:
    """Wrapper for ``GET /rest/api/latest/<path>`` against ``site``. Only
    ``GET`` requests are used anywhere in this server."""
    response = requests.get(
        f"{_jira_base_url(site)}{_jira_api_path(site, path)}",
        headers=_jira_headers(site),
        params=params,
        timeout=30,
        verify=False,
    )
    return response


def _custom_field_name_map(site: str) -> dict[str, str]:
    """Return ``{customfield_id: human_name}`` from ``/rest/api/latest/field``.

    Silently returns ``{}`` on failure so search still returns useful data
    even when field metadata is unreachable.
    """
    try:
        res = _jira_get(site, "/rest/api/latest/field")
        if res.status_code != 200:
            return {}
        out: dict[str, str] = {}
        for entry in res.json() or []:
            fid = entry.get("id") or ""
            name = entry.get("name") or ""
            if fid and name:
                out[fid] = name
        return out
    except (requests.RequestException, ValueError):
        return {}


def _normalize_ticket_fields(
    fields: dict[str, Any], custom_names: dict[str, str]
) -> dict[str, Any]:
    """Return a copy of the Jira ``fields`` dict with common Atlassian
    dict wrappers flattened to plain scalars so the AI can consume them
    directly, and a companion ``by_name`` view keyed by human names for
    any ``customfield_*`` entries we can resolve."""
    flat: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        flat[key] = _flatten_field_value(value)

    by_name: dict[str, Any] = {}
    for key, value in flat.items():
        if key.startswith("customfield_"):
            human = custom_names.get(key)
            if human:
                by_name[human] = value
    return {"raw": fields, "flat": flat, "by_name": by_name}


def _flatten_field_value(value: Any) -> Any:
    """Reduce Atlassian's ``{value|name|displayName}`` wrappers to a scalar
    when there's an obvious "primary" attribute; otherwise pass through."""
    if isinstance(value, dict):
        for key in ("value", "name", "displayName", "emailAddress", "key"):
            if key in value and isinstance(value[key], (str, int, float, bool)):
                return value[key]
        return value
    if isinstance(value, list):
        return [_flatten_field_value(v) for v in value]
    return value


def _strip_html(fragment: Optional[str]) -> str:
    """Unescape HTML entities and strip tags down to plain,
    whitespace-collapsed text. Used for Activity Streams titles/content
    and Jira comment bodies, both of which arrive as HTML/wiki markup."""
    unescaped = html.unescape(fragment or "")
    text = re.sub(r"<[^>]+>", "", unescaped)
    return " ".join(text.split())


_ACTIVITY_ATOM_NS = "http://www.w3.org/2005/Atom"
# Matches the issue-key anchor embedded in an Activity Streams entry title,
# e.g. ``<a href=".../browse/DEVOPS-54374">DEVOPS-54374 - NA5 - ACD 27.0.56</a>``.
_ACTIVITY_ISSUE_LINK_RE = re.compile(
    r'href="[^"]*/browse/([A-Z][A-Z0-9]*-\d+)"[^>]*>([^<]*)</a>'
)


def _parse_activity_feed(xml_text: str) -> list[dict[str, Any]]:
    """Parse an Atlassian Activity Streams Atom feed (``GET /activity``)
    into ``[{action, issue_key, issue_url, summary, title, excerpt,
    author, timestamp}]``. Tolerant of malformed/empty feeds — returns
    ``[]`` rather than raising."""
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    ns = {"atom": _ACTIVITY_ATOM_NS}
    out: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        title_raw = entry.findtext("atom:title", default="", namespaces=ns) or ""
        content_raw = entry.findtext("atom:content", default="", namespaces=ns) or ""
        category_el = entry.find("atom:category", ns)
        action = (category_el.get("term") if category_el is not None else None) or "unknown"
        published = entry.findtext("atom:published", default=None, namespaces=ns)

        author_el = entry.find("atom:author", ns)
        author_name = (
            author_el.findtext("atom:name", default=None, namespaces=ns)
            if author_el is not None
            else None
        )

        # Entry-level links only (not the avatar photo link nested inside
        # <author>). Prefer a link that matches the issue_key extracted
        # from the title below (over just "contains /browse/") so a
        # same-titled-but-different link (e.g. a self/edit link, or a
        # /browse/ link for a different issue) can't get returned as this
        # entry's issue_url. Falls back to the loose "/browse/" match only
        # when no issue_key could be extracted from the title; returns
        # None (not "the first link, whatever it is") when nothing matches.
        candidate_links = [
            le.get("href") for le in entry.findall("atom:link", ns) if le.get("href")
        ]

        title_unescaped = html.unescape(title_raw)
        issue_key = None
        summary = None
        match = _ACTIVITY_ISSUE_LINK_RE.search(title_unescaped)
        if match:
            issue_key = match.group(1)
            summary = match.group(2).strip()

        if issue_key:
            issue_url = next((h for h in candidate_links if f"/browse/{issue_key}" in h), None)
        else:
            issue_url = next((h for h in candidate_links if "/browse/" in h), None)

        out.append(
            {
                "action": action,
                "issue_key": issue_key,
                "issue_url": issue_url,
                "summary": summary,
                "title": _strip_html(title_raw),
                "excerpt": _strip_html(content_raw)[:500] or None,
                "author": author_name,
                "timestamp": published,
            }
        )
    return out


def _extract_comment_matches(
    issue: dict[str, Any], person_needle: str, base: str
) -> list[dict[str, Any]]:
    """Scan one search-result issue's embedded ``comment`` field for
    comments whose author matches ``person_needle`` (case-insensitive
    substring against display name or email). Used by
    ``find_comments_by_person``."""
    key = issue.get("key")
    fields = issue.get("fields") or {}
    summary = fields.get("summary")
    comment_block = fields.get("comment")
    if isinstance(comment_block, dict):
        raw_comments = comment_block.get("comments") or []
    elif isinstance(comment_block, list):
        raw_comments = comment_block
    else:
        raw_comments = []

    out: list[dict[str, Any]] = []
    for c in raw_comments:
        if not isinstance(c, dict):
            continue
        author = c.get("author") or {}
        author_name = (author.get("displayName") or "").strip()
        author_email = (author.get("emailAddress") or "").strip()
        if (
            person_needle not in author_name.lower()
            and person_needle not in author_email.lower()
        ):
            continue
        body = c.get("body")
        excerpt = _strip_html(body) if isinstance(body, str) else None
        out.append(
            {
                "issue_key": key,
                "issue_url": f"{base}/browse/{key}" if key else None,
                "summary": summary,
                "comment_id": c.get("id"),
                "comment_author": author_name or author_email or None,
                "comment_created": c.get("created"),
                "comment_excerpt": (excerpt[:500] if excerpt else None),
            }
        )
    return out


# ── Current-user auto-discovery ─────────────────────────────────────────────
# The identity behind ``JIRA_TOKEN`` is needed any time the caller wants
# "tickets assigned to me", "the DevOps Resource is currentUser()", or a
# JQL like ``assignee = currentUser()``. Exposing it as a dedicated tool
# means the AI host does NOT have to reason about identity from another
# MCP server (Confluence, GitLab, etc.) — those may or may not be the
# same account, and cross-system inference is exactly the kind of guess
# we want to avoid.

_current_user_cache: dict[str, dict[str, Any]] = {}


def _fetch_current_user(site: str) -> dict[str, Any]:
    """Return ``{ok, display_name, email, username, account_id, ...}`` for
    the identity behind ``site``'s token. Cached per site for the process
    lifetime — the token doesn't change without a container restart."""
    cached = _current_user_cache.get(site)
    if cached is not None:
        return cached
    try:
        response = _jira_get(site, "/rest/api/latest/myself")
    except Exception as exc:  # noqa: BLE001
        result: dict[str, Any] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        _current_user_cache[site] = result
        return result
    if response.status_code >= 400:
        result = {
            "ok": False,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
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
        "email": data.get("emailAddress"),
        # Server/Data Center uses ``name`` as the login; Cloud uses ``accountId``.
        "username": data.get("name"),
        "account_id": data.get("accountId"),
        "user_key": data.get("key"),
        "active": data.get("active"),
        "time_zone": data.get("timeZone"),
        "locale": data.get("locale"),
        "self_url": data.get("self"),
    }
    _current_user_cache[site] = result
    return result


@mcp.tool()
def whoami_jira(site: str = "auto") -> dict[str, Any]:
    """Return the identity attached to ``site``'s token. **Read-only.**

    Wraps ``GET /rest/api/latest/myself``. Result is cached per site for
    process lifetime (the token is fixed at startup, so the answer never
    changes).

    **Use this any time you need the current user's Jira username** —
    e.g. before composing JQL that references the caller
    (``assignee = currentUser()`` works in Jira, but if you need to
    plug a literal username into a non-JQL context, get it from here),
    when reporting "tickets assigned to me", or when validating that a
    ticket's Assignee / DevOps Resource / other user-typed field is
    already the token owner. Do NOT infer the Jira token owner from
    another MCP server's identity (Confluence, GitLab, etc.) — those
    may be different accounts.

    Args:
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).

    Returns ``{ok, site, display_name, email, username, account_id, user_key,
    active, time_zone, locale, self_url}``. On failure, ``ok=False``
    with the upstream status/error.
    """
    site_resolved = _resolve_site(site)
    return {"site": site_resolved, **_fetch_current_user(site_resolved)}


@mcp.tool()
def search_jira_tickets(
    jql: str,
    fields: Optional[list[str]] = None,
    max_results: int = 20,
    start_at: int = 0,
    next_page_token: Optional[str] = None,
    expand: Optional[list[str]] = None,
    include_field_names: bool = True,
    site: str = "auto",
) -> dict[str, Any]:
    """Freestyle JQL search with a rich, pagination-aware response. **Read-only.**

    This is the flexible companion to ``list_jira_tickets`` / ``list_ready_to_deploy_tickets``.
    Pass any JQL the user gives you (or that you compose) and get back
    tickets with their raw + normalized fields, plus paging metadata so
    you can fetch subsequent pages.

    Args:
        jql: Any valid JQL. Use ``list_jira_projects`` / ``list_jira_fields``
            first if you don't know the project keys or custom-field IDs
            required. The user's example works verbatim:
            ``project = DevOps AND (cf[16400] in (STG4) OR summary ~ "*STG4*" …)
            AND comment ~ APPROVED AND Status = "Ready for deployment"
            AND (ASSIGNEE != currentUser()) ORDER BY createdDate DESC``.
        fields: Optional list of Jira field ids or names. Defaults to a
            useful set (summary, status, priority, assignee, reporter,
            labels, created, updated, resolution, plus the MyCompany env
            and deploy-order custom fields). Pass ``["*all"]`` to fetch
            every field on each ticket.
        max_results: Page size (1..100). Default 20.
        start_at: Offset for pagination. **onprem only** — cloud (Jira
            Cloud) has no offset pagination (Atlassian removed it); use
            ``next_page_token`` there instead. Ignored on cloud.
        next_page_token: **cloud only.** Pass the ``next_page_token`` from
            a previous response to fetch the next page. Ignored on onprem.
        expand: Optional Jira expand list (``["renderedFields", "names",
            "schema", "transitions", "changelog"]``, etc.).
        include_field_names: When ``True`` (default), attach a
            ``custom_field_names`` map ``{customfield_16400: "Environment", ...}``
            so the AI can report tickets with human-friendly field names.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).

    Returns ``{ok, jql, total, start_at, max_results, next_start_at,
    has_more, next_page_token, count, custom_field_names, tickets, note}``
    where each ticket is ``{key, url, fields: {raw, flat, by_name}}``. On
    cloud, ``total``/``start_at``/``next_start_at`` are ``None`` — page
    with ``next_page_token`` instead. ``note`` is only set (non-``None``)
    when zero tickets matched, and suggests checking whether the JQL
    belongs to the other Jira instance (``site``).
    """
    if not jql or not jql.strip():
        raise ValueError("jql is required.")
    if max_results < 1:
        raise ValueError("max_results must be >= 1.")
    max_results = min(int(max_results), 100)
    start_at = max(0, int(start_at))
    site_resolved = _resolve_site(site)

    fields_list = list(fields) if fields else list(_DEFAULT_SEARCH_FIELDS)

    status_code, result, err = _jira_search_jql(
        site_resolved,
        jql,
        fields=fields_list,
        max_results=max_results,
        start_at=start_at,
        next_page_token=next_page_token,
        expand=expand,
    )
    if status_code >= 400:
        return {
            "ok": False,
            "jql": jql,
            "status_code": status_code,
            "error": err,
            "instruction": (
                "Jira rejected the JQL. Show the error to the user, "
                "double-check field IDs via `list_jira_fields`, project "
                "keys via `list_jira_projects`, and re-invoke with a "
                "corrected query."
            ),
        }
    returned = result["issues"]

    custom_names = _custom_field_name_map(site_resolved) if include_field_names else {}
    base = _jira_base_url(site_resolved)
    tickets: list[dict[str, Any]] = []
    for issue in returned:
        key = issue.get("key")
        tickets.append(
            {
                "key": key,
                "url": f"{base}/browse/{key}" if key else None,
                "id": issue.get("id"),
                "fields": _normalize_ticket_fields(
                    issue.get("fields") or {}, custom_names
                ),
            }
        )

    total = result["total"]
    result_start_at = result["start_at"]
    next_start = (
        result_start_at + len(returned) if result_start_at is not None else None
    )
    has_next_start = (
        total is not None and next_start is not None and next_start < total
    )
    zero_results_note = (
        f"No tickets matched this JQL on site='{site_resolved}'. If you "
        "expected results, this JQL may belong to the other Jira instance "
        "— retry with site='onprem' or site='cloud', or confirm the "
        "project/fields exist there via list_jira_projects / list_jira_fields."
        if not tickets
        else None
    )
    return {
        "ok": True,
        "jql": jql,
        "total": total,
        "start_at": result_start_at,
        "max_results": max_results,
        "count": len(tickets),
        "has_more": not result["is_last"],
        "next_start_at": next_start if has_next_start else None,
        "next_page_token": result["next_page_token"],
        "custom_field_names": custom_names if include_field_names else None,
        "fields_requested": fields_list,
        "expand": expand or None,
        "tickets": tickets,
        "note": zero_results_note,
    }


@mcp.tool()
def get_jira_ticket(
    issue_key: str,
    fields: Optional[list[str]] = None,
    expand: Optional[list[str]] = None,
    include_field_names: bool = True,
    site: str = "auto",
) -> dict[str, Any]:
    """Fetch a single Jira ticket by key with all its properties. **Read-only.**

    Args:
        issue_key: e.g. ``"DEVOPS-52986"``.
        fields: Optional list of field ids/names to project. Defaults to
            every field (``"*all"``).
        expand: Optional Jira expand list (``["renderedFields", "names",
            "schema", "transitions", "changelog"]``, …).
        include_field_names: Include the ``custom_field_names`` map so
            the caller can look up human names for ``customfield_*`` ids.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
            Use ``site="cloud"`` for MyCompany tickets like
            ``https://mycompany-prod.atlassian.net/browse/OB-45834``.
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    site_resolved = _resolve_site(site)

    params: dict[str, Any] = {}
    if fields:
        params["fields"] = ",".join(fields)
    else:
        params["fields"] = "*all"
    if expand:
        params["expand"] = ",".join(expand)

    response = _jira_get(
        site_resolved, f"/rest/api/latest/issue/{issue_key.strip()}", params=params
    )
    if response.status_code >= 400:
        return {
            "ok": False,
            "issue_key": issue_key,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
        }
    data = response.json()
    custom_names = _custom_field_name_map(site_resolved) if include_field_names else {}
    base = _jira_base_url(site_resolved)
    key = data.get("key") or issue_key
    return {
        "ok": True,
        "key": key,
        "id": data.get("id"),
        "url": f"{base}/browse/{key}",
        "fields": _normalize_ticket_fields(data.get("fields") or {}, custom_names),
        "custom_field_names": custom_names if include_field_names else None,
    }


@mcp.tool()
def list_jira_projects(
    search: str = "",
    max_results: int = 100,
    site: str = "auto",
) -> dict[str, Any]:
    """List Jira projects visible to the current token. **Read-only.**

    Args:
        search: Optional case-insensitive substring filter on project key
            or name (e.g. ``"devops"``, ``"support"``).
        max_results: Cap on returned projects. Default 100.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
    """
    site_resolved = _resolve_site(site)
    response = _jira_get(site_resolved, "/rest/api/latest/project")
    if response.status_code >= 400:
        return {
            "ok": False,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
        }
    data = response.json() or []
    search_lower = (search or "").strip().lower()
    projects: list[dict[str, Any]] = []
    for p in data:
        key = p.get("key") or ""
        name = p.get("name") or ""
        if search_lower and search_lower not in key.lower() and search_lower not in name.lower():
            continue
        projects.append(
            {
                "key": key,
                "id": p.get("id"),
                "name": name,
                "project_type_key": p.get("projectTypeKey"),
                "category": (p.get("projectCategory") or {}).get("name"),
                "lead": (p.get("lead") or {}).get("displayName"),
                "url": f"{_jira_base_url(site_resolved)}/browse/{key}" if key else None,
            }
        )
        if len(projects) >= max_results:
            break
    projects.sort(key=lambda x: x["key"])
    return {
        "ok": True,
        "search": search or None,
        "match_count": len(projects),
        "projects": projects,
    }


@mcp.tool()
def list_jira_fields(
    search: str = "",
    custom_only: bool = False,
    max_results: int = 200,
    site: str = "auto",
) -> dict[str, Any]:
    """List Jira field definitions (including custom fields). **Read-only.**

    Use this to discover the ``customfield_<id>`` values referenced in
    JQL (e.g. ``cf[16400]``). Returns each field's id, human name, custom
    flag, searchable flag, and schema when available.

    Args:
        search: Optional case-insensitive substring filter on the field
            name or id (e.g. ``"environment"``, ``"schedule"``).
        custom_only: If ``True``, only return fields where ``custom=True``.
        max_results: Cap on returned fields. Default 200.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
    """
    site_resolved = _resolve_site(site)
    response = _jira_get(site_resolved, "/rest/api/latest/field")
    if response.status_code >= 400:
        return {
            "ok": False,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
        }
    data = response.json() or []
    search_lower = (search or "").strip().lower()
    out: list[dict[str, Any]] = []
    for entry in data:
        fid = entry.get("id") or ""
        name = entry.get("name") or ""
        is_custom = bool(entry.get("custom"))
        if custom_only and not is_custom:
            continue
        if search_lower and search_lower not in fid.lower() and search_lower not in name.lower():
            continue
        out.append(
            {
                "id": fid,
                "name": name,
                "custom": is_custom,
                "searchable": bool(entry.get("searchable")),
                "schema": entry.get("schema"),
                "clause_names": entry.get("clauseNames") or [],
            }
        )
        if len(out) >= max_results:
            break
    out.sort(key=lambda x: (not x["custom"], x["name"].lower()))
    return {
        "ok": True,
        "search": search or None,
        "custom_only": custom_only,
        "match_count": len(out),
        "fields": out,
    }


# ── User activity ───────────────────────────────────────────────────────────
# "Did this person comment on anything recently?" needs two very different
# implementations depending on site:
#
#   * onprem exposes the classic Activity Streams feed (``/activity``),
#     the same one powering the "Activity Stream" tab on ViewProfile.jspa.
#     It's filterable by username server-side — fast and precise.
#   * cloud has an Activity Streams marketplace app installed too, but its
#     ``streams=user IS <id>`` filter keys off an internal identifier that
#     isn't exposed via ``accountId`` or email through any Jira REST
#     endpoint — verified empty results for both. There is no reliable
#     way to filter that feed by person, so cloud gets a different tool:
#     a bounded scan of tickets' embedded comments.


@mcp.tool()
def get_jira_user_activity(
    username: str,
    max_results: int = 20,
    activity_types: Optional[list[str]] = None,
    site: str = "onprem",
) -> dict[str, Any]:
    """Return a user's recent Jira activity (comments, transitions, etc.).
    **Read-only. onprem only** — cloud's Activity Streams app can't be
    filtered by any public user identifier; use ``find_comments_by_person``
    for cloud instead.

    Wraps ``GET /activity?streams=user IS <username>&providers=issues``,
    the same Atom feed that powers the "Activity Stream" tab on
    ``https://jira.mycompany.com/secure/ViewProfile.jspa?name=<username>``.

    Args:
        username: Jira username, e.g. ``"sbaretto"`` (the ``name=`` value
            from the ViewProfile.jspa URL).
        max_results: Cap on returned activity entries. Default 20.
        activity_types: Optional filter on Jira's activity ``category``
            term (e.g. ``["comment"]`` to only show comments). ``None``
            (default) returns every activity type Jira includes.
        site: Must be ``"onprem"`` — any other value is refused (see
            docstring above for why cloud isn't supported here).

    Returns ``{ok, username, count, activities}`` where each activity is
    ``{action, issue_key, issue_url, summary, title, excerpt, author,
    timestamp}``.
    """
    if not username or not username.strip():
        raise ValueError("username is required.")
    site_resolved = _resolve_site(site)
    if site_resolved != "onprem":
        return {
            "ok": False,
            "error": (
                "get_jira_user_activity only supports site='onprem'. "
                "cloud's Activity Streams app filters by an internal id "
                "that isn't exposed via accountId or email — use "
                "find_comments_by_person instead for cloud."
            ),
        }
    username = username.strip()
    max_results = max(1, min(int(max_results), 100))

    response = _jira_get(
        site_resolved,
        "/activity",
        params={
            "maxResults": max_results,
            "streams": f"user IS {username}",
            "providers": "issues",
        },
    )
    if response.status_code >= 400:
        return {
            "ok": False,
            "username": username,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
        }

    activities = _parse_activity_feed(response.text or "")
    if activity_types:
        wanted = {t.strip().lower() for t in activity_types if t and t.strip()}
        activities = [a for a in activities if (a.get("action") or "").lower() in wanted]

    return {
        "ok": True,
        "username": username,
        "count": len(activities),
        "activities": activities,
    }


@mcp.tool()
def find_comments_by_person(
    person: str,
    jql_scope: str = "",
    updated_since_days: int = 30,
    max_issues_scanned: int = 200,
    start_at: int = 0,
    next_page_token: Optional[str] = None,
    site: str = "auto",
) -> dict[str, Any]:
    """Find comments authored by a specific person. **Read-only.**
    Primarily for cloud (Jira Cloud), where the Activity Streams app can't
    be filtered by a public identifier — see ``get_jira_user_activity`` for
    the faster onprem-native alternative.

    JQL has no native "commented by" clause (confirmed against Atlassian's
    JQL fields reference — only free-text ``comment ~ "..."`` exists, which
    searches comment *content*, not authorship). This tool works in two
    phases:

    1. **Linked-ticket pass** (fast, precise): searches
       ``assignee = <person> OR reporter = <person> OR watcher = <person>``
       — these ARE native, indexed JQL user fields — and scans just those
       tickets' comments first. Catches the common case where the person
       commented on a ticket they're actually associated with. Only runs
       on the first call of a scan (skipped when resuming via
       ``start_at``/``next_page_token``, so it isn't repeated every page).
    2. **Brute-force pass** (fallback, bounded): pages through tickets
       ordered by ``updated DESC`` requesting only the ``comment`` field,
       filtering comment authors client-side. On a busy, high-automation
       Jira Cloud instance, "most recently updated" is often dominated by
       bots/integrations — a person's genuine comment from days ago may
       never surface within a small scan budget. **Narrowing
       ``jql_scope`` (e.g. ``"project = OB"``) is the single biggest lever
       for this pass actually succeeding** — an unscoped scan of a large,
       busy instance may need many resumed calls to reach an old ticket.

    On cloud, Jira's search API also caps embedded comments at 20 per
    issue, so very long comment threads may hide older matches.

    **If a call comes back ``truncated: true``**, phase 2 stopped after
    ``max_issues_scanned`` tickets without finishing the scope — this does
    NOT mean the person has no matches, only that they weren't found in
    the tickets scanned so far. Re-invoke this same tool with identical
    ``person``/``jql_scope``/``updated_since_days`` and pass the returned
    ``resume.start_at`` (onprem) or ``resume.next_page_token`` (cloud)
    back into the matching argument to continue exactly where phase 2
    left off, instead of restarting from page 1. Repeat until
    ``truncated`` is ``false``.

    Args:
        person: Email address or (partial) display name to match against
            each comment's author, e.g. ``"shannon.baretto@mycompany.com"`` or
            ``"Shannon Baretto"``. Also used verbatim in the phase-1 JQL
            (Jira resolves ``assignee``/``reporter``/``watcher`` by full
            name, email, or account id).
        jql_scope: Optional JQL fragment to narrow the scan (e.g.
            ``"project = OB"`` or ``"project in (OB, CX_Outbound)"``).
            ANDed with the ``updated_since_days`` filter in both phases.
            Empty (default) scans the whole site — strongly recommended
            to narrow this on large instances.
        updated_since_days: Only scan tickets updated within this many
            days. Default 30.
        max_issues_scanned: Hard cap on tickets fetched by phase 2 in THIS
            call (1..2000). Default 200. Phase 1 has its own small fixed
            budget (up to 100) and doesn't count against this. The scan
            stops once this many tickets have been examined by phase 2 in
            this call, even if more exist in scope — use the ``resume``
            cursor on a follow-up call to keep going.
        start_at: Resume cursor for **onprem** — pass the previous
            response's ``resume.start_at`` to continue a truncated phase-2
            scan. Ignored on cloud. Default 0 (start from the beginning).
            When non-zero, phase 1 is skipped (already covered on the
            first call).
        next_page_token: Resume cursor for **cloud** — pass the previous
            response's ``resume.next_page_token`` to continue a truncated
            phase-2 scan. Ignored on onprem. Default ``None`` (start from
            the beginning). When set, phase 1 is skipped.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).

    Returns ``{ok, person, site, linked_jql_used, linked_jql_error,
    scan_jql_used, issues_scanned, truncated, match_count, matches,
    resume, instruction}`` where each match is ``{issue_key, issue_url,
    summary, comment_id, comment_author, comment_created,
    comment_excerpt}``, deduplicated across both phases. ``resume`` is
    non-``None`` only when ``truncated`` is ``True``.
    """
    if not person or not person.strip():
        raise ValueError("person is required (email or display name).")
    if updated_since_days < 1:
        raise ValueError("updated_since_days must be >= 1.")
    max_issues_scanned = max(1, min(int(max_issues_scanned), 2000))
    site_resolved = _resolve_site(site)
    person_stripped = person.strip()
    person_needle = person_stripped.lower()
    person_escaped = person_stripped.replace('"', '\\"')
    base = _jira_base_url(site_resolved)

    common_parts = [f"updated >= -{updated_since_days}d"]
    if jql_scope and jql_scope.strip():
        common_parts.append(f"({jql_scope.strip()})")
    scan_jql = " AND ".join(common_parts) + " ORDER BY updated DESC"

    matches: list[dict[str, Any]] = []
    seen: set[tuple[Optional[str], Optional[str]]] = set()

    def _add_matches(found: list[dict[str, Any]]) -> None:
        for m in found:
            fp = (m.get("issue_key"), m.get("comment_id"))
            if fp in seen:
                continue
            seen.add(fp)
            matches.append(m)

    is_resume_call = start_at > 0 or bool(next_page_token)
    linked_jql_used: Optional[str] = None
    linked_jql_error: Optional[str] = None
    phase1_scanned = 0

    if not is_resume_call:
        linked_clause = (
            f'(assignee = "{person_escaped}" OR reporter = "{person_escaped}" '
            f'OR watcher = "{person_escaped}")'
        )
        linked_jql_used = " AND ".join(common_parts + [linked_clause]) + " ORDER BY updated DESC"
        linked_status, linked_result, linked_err = _jira_search_jql(
            site_resolved, linked_jql_used, fields=["summary", "comment"], max_results=100
        )
        if linked_status >= 400:
            linked_jql_error = linked_err
        else:
            for issue in linked_result["issues"]:
                phase1_scanned += 1
                _add_matches(_extract_comment_matches(issue, person_needle, base))

    scanned = 0
    cur_start_at = max(0, int(start_at))
    cur_next_page_token = next_page_token
    hit_natural_end = False

    while scanned < max_issues_scanned:
        page_size = max(1, min(100, max_issues_scanned - scanned))
        status_code, result, err = _jira_search_jql(
            site_resolved,
            scan_jql,
            fields=["summary", "comment"],
            max_results=page_size,
            start_at=cur_start_at,
            next_page_token=cur_next_page_token,
        )
        if status_code >= 400:
            return {
                "ok": False,
                "person": person,
                "site": site_resolved,
                "status_code": status_code,
                "error": err,
            }
        issues = result["issues"]
        if not issues:
            hit_natural_end = True
            break

        for issue in issues:
            scanned += 1
            _add_matches(_extract_comment_matches(issue, person_needle, base))

        if result["is_last"]:
            hit_natural_end = True
            break
        if result["next_page_token"]:
            cur_next_page_token = result["next_page_token"]
        elif result["start_at"] is not None:
            cur_start_at = result["start_at"] + len(issues)
        else:
            hit_natural_end = True
            break

    truncated = not hit_natural_end and scanned >= max_issues_scanned
    matches.sort(key=lambda m: m.get("comment_created") or "", reverse=True)
    resume = (
        {
            "start_at": cur_start_at if not cur_next_page_token else None,
            "next_page_token": cur_next_page_token,
        }
        if truncated
        else None
    )
    return {
        "ok": True,
        "person": person,
        "site": site_resolved,
        "linked_jql_used": linked_jql_used,
        "linked_jql_error": linked_jql_error,
        "linked_tickets_scanned": phase1_scanned,
        "scan_jql_used": scan_jql,
        "issues_scanned": scanned,
        "truncated": truncated,
        "resume": resume,
        "instruction": (
            "Phase 2 (brute-force scan) stopped after max_issues_scanned "
            "before covering the full scope — this does NOT mean no "
            "matches exist beyond this point. Re-invoke with the same "
            "person/jql_scope/updated_since_days and pass "
            "`resume.start_at` or `resume.next_page_token` back in to "
            "continue, or narrow jql_scope for a faster complete scan."
            if truncated
            else None
        ),
        "match_count": len(matches),
        "matches": matches,
    }


# ── Deployment-ticket shortcuts ─────────────────────────────────────────────
# Two tools purpose-built for the onprem-deploy-planner skill: fetching the
# full deploy-ticket context in a single call, and pulling arbitrary field
# values by human name or id. Both are read-only, share the existing
# ``_jira_get`` / ``_custom_field_name_map`` / ``_flatten_field_value``
# infrastructure, and touch no existing tools or constants.

# Rollout / Rollback custom-field ids on MyCompany Deployment tickets.
_JIRA_ROLLOUT_FIELD = "customfield_11300"
_JIRA_ROLLBACK_FIELD = "customfield_11301"

# Extract the component token from a Deployment ticket summary. Ticket
# summaries follow the pattern ``<ENV> - <component> <version>`` (or
# ``<ENV>: <component> <description>``). The component word is captured up
# to the next whitespace so version numbers, colons, and free-text tails
# don't bleed in.
_SUMMARY_COMPONENT_RE = re.compile(
    r"^\s*(?:NA[0-9]|EU[0-9]|UK[0-9]|AU[0-9]|TST[0-9]|STG[0-9])\s*[-:]\s*"
    r"(?P<component>[A-Za-z0-9][\w\-]*)",
    re.IGNORECASE,
)

# Custom-field name aliases we recognise as "the environment field" when
# scanning the field-name map. Deployment tickets may use any of these
# instead of the change-request ``customfield_16400``.
_ENV_FIELD_NAME_ALIASES: frozenset[str] = frozenset(
    {"environment", "environments", "target environment", "target env"}
)


def _resolve_deployment_env(
    fields: dict[str, Any],
    summary: Optional[str],
    custom_names: dict[str, str],
) -> Optional[str]:
    """Resolve the env for a deployment ticket by trying, in order:
    every ``customfield_*`` whose human name matches ``_ENV_FIELD_NAME_ALIASES``,
    the change-request ``JIRA_ENV_FIELD`` fallback, and finally the env
    token in the summary. Reuses ``_extract_env`` for shape handling."""
    candidate_ids: list[str] = []
    for fid, name in custom_names.items():
        if (name or "").strip().lower() in _ENV_FIELD_NAME_ALIASES:
            candidate_ids.append(fid)
    if JIRA_ENV_FIELD not in candidate_ids:
        candidate_ids.append(JIRA_ENV_FIELD)
    for fid in candidate_ids:
        env = _extract_env(fields.get(fid), None)
        if env:
            return env
    return _extract_env(None, summary)


def _extract_component_from_summary(summary: Optional[str]) -> Optional[str]:
    if not summary:
        return None
    match = _SUMMARY_COMPONENT_RE.match(summary)
    return match.group("component") if match else None


def _flatten_jira_comment(comment: dict[str, Any]) -> dict[str, Any]:
    author = comment.get("author") or {}
    return {
        "id": comment.get("id"),
        "author": author.get("displayName") or author.get("name"),
        "created": comment.get("created"),
        "updated": comment.get("updated"),
        "body": comment.get("body"),
    }


def _flatten_jira_issue_link(link: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return ``{key, summary, status, link_type_name, direction, link_label}``
    for one issue-link entry. ``direction`` is ``"inward"`` when the linked
    issue is on the ``inwardIssue`` side, ``"outward"`` otherwise, so
    ``Cloners`` "is cloned by" (inward) is distinguishable from "clones"
    (outward). ``None`` when neither side has a linked issue."""
    type_info = link.get("type") or {}
    link_type_name = type_info.get("name")
    inward = link.get("inwardIssue")
    outward = link.get("outwardIssue")
    if inward:
        target = inward
        direction = "inward"
        link_label = type_info.get("inward")
    elif outward:
        target = outward
        direction = "outward"
        link_label = type_info.get("outward")
    else:
        return None
    target_fields = target.get("fields") or {}
    return {
        "key": target.get("key"),
        "summary": target_fields.get("summary"),
        "status": (target_fields.get("status") or {}).get("name"),
        "link_type_name": link_type_name,
        "direction": direction,
        "link_label": link_label,
    }


@mcp.tool()
def get_deployment_ticket_context(issue_key: str, site: str = "auto") -> dict[str, Any]:
    """Return everything the deploy-planner skill needs from a Deployment
    ticket, in ONE call. **Read-only.**

    Wraps ``GET /rest/api/latest/issue/<key>?fields=*all`` and shapes the
    response for downstream planning:

      * ``environment`` — resolved by trying every custom field whose
        human name is ``Environment`` (via the field-name map), then the
        change-request ``customfield_16400`` fallback, then the env token
        in the summary (``NA5``, ``UK2``, ``STG4`` …). No hardcoded second
        id; if Jira admins add / rename an env field the resolver picks
        it up on the next call.
      * ``component`` — parsed from the summary
        (``<ENV> - <component> <version>`` or
        ``<ENV>: <component> <description>``).
      * ``description`` — passed through so the caller doesn't need a
        second fetch.
      * ``rollout_instructions`` / ``rollback_instructions`` —
        ``customfield_11300`` / ``customfield_11301``, flattened. Note:
        these ids are MyCompany conventions — on ``site="cloud"`` they may
        not resolve to the equivalent fields.
      * ``comments`` — every comment as
        ``{id, author, created, updated, body}``.
      * ``siblings`` — every issue link as
        ``{key, summary, status, link_type_name, direction, link_label}``.
        ``direction`` distinguishes "is cloned by" (``inward``) from
        "clones" (``outward``) so the planner can find the closed sibling
        without heuristics.

    Args:
        issue_key: e.g. ``"DEVOPS-52986"``.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    issue_key = issue_key.strip()
    site_resolved = _resolve_site(site)

    response = _jira_get(
        site_resolved, f"/rest/api/latest/issue/{issue_key}", params={"fields": "*all"}
    )
    if response.status_code >= 400:
        return {
            "ok": False,
            "issue_key": issue_key,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
        }
    data = response.json() or {}
    fields = data.get("fields") or {}
    custom_names = _custom_field_name_map(site_resolved)
    summary = fields.get("summary") or ""

    environment = _resolve_deployment_env(fields, summary, custom_names)
    component = _extract_component_from_summary(summary)

    comment_block = fields.get("comment") or {}
    if isinstance(comment_block, dict):
        raw_comments = comment_block.get("comments") or []
    elif isinstance(comment_block, list):
        raw_comments = comment_block
    else:
        raw_comments = []
    comments = [
        _flatten_jira_comment(c) for c in raw_comments if isinstance(c, dict)
    ]

    raw_links = fields.get("issuelinks") or []
    siblings: list[dict[str, Any]] = []
    for link in raw_links:
        if not isinstance(link, dict):
            continue
        shaped = _flatten_jira_issue_link(link)
        if shaped and shaped.get("key"):
            siblings.append(shaped)

    status = (fields.get("status") or {}).get("name")
    issuetype = (fields.get("issuetype") or {}).get("name")
    base = _jira_base_url(site_resolved)
    return {
        "ok": True,
        "key": issue_key,
        "url": f"{base}/browse/{issue_key}",
        "summary": summary,
        "status": status,
        "issuetype": issuetype,
        "environment": environment,
        "component": component,
        "description": fields.get("description"),
        "rollout_instructions": _flatten_field_value(
            fields.get(_JIRA_ROLLOUT_FIELD)
        ),
        "rollback_instructions": _flatten_field_value(
            fields.get(_JIRA_ROLLBACK_FIELD)
        ),
        "comments": comments,
        "siblings": siblings,
    }


_CUSTOMFIELD_ID_RE = re.compile(r"^customfield_\d+$")


@mcp.tool()
def get_jira_field_values(
    issue_key: str,
    field_names: list[str],
    site: str = "auto",
) -> dict[str, Any]:
    """Fetch specific Jira fields by id or human name. **Read-only.**

    Each entry in ``field_names`` is resolved as follows:

      * ``customfield_\\d+`` → passed through as-is (no name lookup).
      * A known human name (via the field-name map) → resolved to its
        ``customfield_*`` id.
      * Anything else → passed through as-is (works for system field names
        like ``summary``, ``status``, ``description``, ``assignee``,
        ``reporter``, ``labels``).

    Returned ``values`` is keyed by the caller's original input (so a
    request for ``"Environment"`` comes back on ``"Environment"``, and a
    request for ``customfield_16400`` on ``customfield_16400``). Values are
    flattened via ``_flatten_field_value`` — Atlassian
    ``{value|name|displayName}`` wrappers reduce to scalars.

    Args:
        issue_key: e.g. ``"DEVOPS-52986"``.
        field_names: One or more field ids or human names.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    if not isinstance(field_names, list) or not field_names:
        raise ValueError("field_names must be a non-empty list.")
    issue_key = issue_key.strip()
    site_resolved = _resolve_site(site)

    custom_names = _custom_field_name_map(site_resolved)
    name_to_id: dict[str, str] = {
        (name or "").strip().lower(): fid for fid, name in custom_names.items() if name
    }

    resolved: list[tuple[str, str]] = []
    for raw in field_names:
        if not isinstance(raw, str) or not raw.strip():
            continue
        name = raw.strip()
        if _CUSTOMFIELD_ID_RE.match(name):
            resolved.append((raw, name))
            continue
        looked_up = name_to_id.get(name.lower())
        resolved.append((raw, looked_up or name))

    if not resolved:
        return {
            "ok": False,
            "issue_key": issue_key,
            "error": "field_names had no non-empty entries.",
        }

    unique_ids = sorted({fid for _, fid in resolved})
    response = _jira_get(
        site_resolved,
        f"/rest/api/latest/issue/{issue_key}",
        params={"fields": ",".join(unique_ids)},
    )
    if response.status_code >= 400:
        return {
            "ok": False,
            "issue_key": issue_key,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
        }
    data = response.json() or {}
    fields = data.get("fields") or {}

    values: dict[str, Any] = {}
    for caller_input, jira_id in resolved:
        values[caller_input] = _flatten_field_value(fields.get(jira_id))

    return {
        "ok": True,
        "issue_key": issue_key,
        "url": f"{_jira_base_url(site_resolved)}/browse/{issue_key}",
        "resolved_ids": {caller: jid for caller, jid in resolved},
        "values": values,
    }


# ── Write operations ─────────────────────────────────────────────────────────
# Every write tool below requires an explicit ``confirmed_by_user=True`` on
# top of whatever approval prompt the AI host itself shows — a server-side
# guarantee that doesn't depend on the host UI being present. The first
# call (confirmed_by_user=False, the default) never touches Jira; it only
# returns a preview of what would happen via ``_confirmation_gate`` below.
# Only a second, explicitly-confirmed call performs the write.
#
# The tools still enforce Jira-side correctness on top of that:
#
#   * ``transition_jira_ticket`` fetches the ticket's currently allowed
#     workflow transitions and refuses any transition that is not in
#     that set — the workflow is the source of truth.
#   * ``assign_jira_ticket`` uses Jira's assignable-user search. When a
#     query matches more than one user it returns a ``candidates`` list
#     and refuses to guess; no write is issued in that case.
#   * ``edit_jira_ticket`` fetches Jira's ``editmeta`` and refuses any
#     field that isn't editable in the current workflow state or that
#     has a dedicated tool (``status``, ``assignee``, ``comment``,
#     ``issuetype``, ``project``, ``reporter``, ``attachment``,
#     ``worklog``, ``issuelinks``, ``subtasks``).
#   * ``clone_jira_ticket`` never modifies the source; it creates a new
#     ticket with a whitelisted subset of source fields plus overrides.
#   * ``move_jira_ticket`` performs a project move as an ordered
#     sequence (create in target project → link source ↔ new → comment
#     on source → optional source transition) in one tool call.


# Caps applied to every confirmation preview — a caller can otherwise balloon
# the tool response (or over-expose content that doesn't need round-tripping
# through the LLM just to confirm a write) via an oversized comment body,
# diff, or clone/create/move payload.
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
            f"This will perform '{action}' on Jira ({site}). Show this "
            "preview to the user and, only after they explicitly approve, "
            "re-invoke this same tool with confirmed_by_user=True to "
            "execute it. Nothing has been written yet."
        ),
    }


def _jira_post(
    site: str,
    path: str,
    json_body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    """Wrapper for ``POST /rest/api/latest/<path>`` against ``site`` with
    the auth headers and JSON content type Jira expects for writes."""
    return requests.post(
        f"{_jira_base_url(site)}{_jira_api_path(site, path)}",
        headers={**_jira_headers(site), "Content-Type": "application/json"},
        json=json_body,
        params=params,
        timeout=30,
        verify=False,
    )


def _jira_put(
    site: str,
    path: str,
    json_body: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    """Wrapper for ``PUT /rest/api/latest/<path>`` against ``site``."""
    return requests.put(
        f"{_jira_base_url(site)}{_jira_api_path(site, path)}",
        headers={**_jira_headers(site), "Content-Type": "application/json"},
        json=json_body,
        params=params,
        timeout=30,
        verify=False,
    )


def _jira_delete(
    site: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
) -> requests.Response:
    """Wrapper for ``DELETE /rest/api/latest/<path>`` against ``site``."""
    return requests.delete(
        f"{_jira_base_url(site)}{_jira_api_path(site, path)}",
        headers=_jira_headers(site),
        params=params,
        timeout=30,
        verify=False,
    )


def _jira_error_message(response: requests.Response) -> str:
    """Build a safe error string from a Jira API error response.

    Parses only Jira's documented typed error fields (``errorMessages``
    and ``errors``) instead of returning the raw response body verbatim,
    so infra noise (HTML gateway pages, stack traces) or arbitrary
    payload content never reaches the tool result. Falls back to a
    generic status-only message when the body isn't Jira's expected
    JSON error shape.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        parts: list[str] = []
        for msg in body.get("errorMessages") or []:
            if isinstance(msg, str) and msg.strip():
                parts.append(msg.strip())
        errors = body.get("errors")
        if isinstance(errors, dict):
            for field, msg in errors.items():
                if isinstance(msg, str) and msg.strip():
                    parts.append(f"{field}: {msg.strip()}")
        if parts:
            return "; ".join(parts)[:800]
    return f"Jira request failed with status {response.status_code}."


def _jira_search_jql(
    site: str,
    jql: str,
    fields: Optional[list[str]] = None,
    max_results: int = 50,
    start_at: int = 0,
    next_page_token: Optional[str] = None,
    expand: Optional[list[str]] = None,
) -> tuple[int, dict[str, Any], Optional[str]]:
    """Site-aware JQL search. Atlassian removed ``GET/POST /rest/api/{2|3}/search``
    for Jira Cloud (410 Gone, effective 2025-05-01) — ``cloud`` uses the
    replacement ``POST /rest/api/3/search/jql``, which pages via
    ``nextPageToken`` and does not return a ``total`` count. ``onprem``
    (Server/Data Center) is unaffected by that Cloud-only removal and keeps
    using the classic ``GET /rest/api/latest/search`` (``startAt``/``total``).

    Returns ``(status_code, normalized, error_body)`` where ``normalized`` is
    ``{issues, total, next_page_token, is_last, start_at}``. On cloud,
    ``total`` and ``start_at`` are always ``None`` since that endpoint
    doesn't expose them — page with ``next_page_token`` instead.
    """
    if site == "cloud":
        body: dict[str, Any] = {"jql": jql, "maxResults": max_results}
        if fields:
            body["fields"] = fields
        if expand:
            body["expand"] = ",".join(expand)
        if next_page_token:
            body["nextPageToken"] = next_page_token
        response = _jira_post(site, "/rest/api/3/search/jql", json_body=body)
        if response.status_code >= 400:
            return response.status_code, {}, _jira_error_message(response)
        payload = response.json() or {}
        next_token = payload.get("nextPageToken")
        return (
            response.status_code,
            {
                "issues": payload.get("issues") or [],
                "total": None,
                "next_page_token": next_token,
                "is_last": bool(payload.get("isLast", not next_token)),
                "start_at": None,
            },
            None,
        )

    params: dict[str, Any] = {"jql": jql, "startAt": start_at, "maxResults": max_results}
    if fields:
        params["fields"] = ",".join(fields)
    if expand:
        params["expand"] = ",".join(expand)
    response = _jira_get(site, "/rest/api/latest/search", params=params)
    if response.status_code >= 400:
        return response.status_code, {}, _jira_error_message(response)
    payload = response.json() or {}
    issues = payload.get("issues") or []
    total = int(payload.get("total") or 0)
    next_start = start_at + len(issues)
    return (
        response.status_code,
        {
            "issues": issues,
            "total": total,
            "next_page_token": None,
            "is_last": next_start >= total,
            "start_at": start_at,
        },
        None,
    )


def _fetch_issue_summary_status(
    site: str, issue_key: str
) -> tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    """Return ``(status_code, summary, current_status, error_body)`` for a
    lightweight issue fetch that only needs summary + status."""
    response = _jira_get(
        site, f"/rest/api/latest/issue/{issue_key}", params={"fields": "summary,status"}
    )
    if response.status_code >= 400:
        return response.status_code, None, None, _jira_error_message(response)
    data = response.json() or {}
    fields = data.get("fields") or {}
    status = (fields.get("status") or {}).get("name")
    return response.status_code, fields.get("summary"), status, None


def _fetch_available_transitions(
    site: str, issue_key: str
) -> tuple[int, list[dict[str, Any]], Optional[str]]:
    """Return ``(status_code, transitions, error_body)``. Each transition
    includes its ``fields`` map (transition-screen schema) so callers can
    validate / pass through fields required by the transition."""
    response = _jira_get(
        site,
        f"/rest/api/latest/issue/{issue_key}/transitions",
        params={"expand": "transitions.fields"},
    )
    if response.status_code >= 400:
        return response.status_code, [], _jira_error_message(response)
    return response.status_code, (response.json() or {}).get("transitions") or [], None


def _summarize_transition_fields(
    transition: dict[str, Any],
) -> list[dict[str, Any]]:
    """Flatten a transition's ``fields`` map into the same shape
    ``get_jira_editable_fields`` uses."""
    fields_meta = transition.get("fields") or {}
    out: list[dict[str, Any]] = []
    for fid, meta in fields_meta.items():
        if not isinstance(meta, dict):
            continue
        allowed_values = None
        av = meta.get("allowedValues")
        if isinstance(av, list) and av:
            allowed_values = [
                {"id": v.get("id"), "value": v.get("value"), "name": v.get("name")}
                for v in av
                if isinstance(v, dict)
            ]
        out.append(
            {
                "id": fid,
                "name": meta.get("name"),
                "required": bool(meta.get("required")),
                "operations": meta.get("operations") or [],
                "schema": meta.get("schema"),
                "allowed_values": allowed_values,
            }
        )
    out.sort(key=lambda e: (not e["required"], (e["name"] or "").lower()))
    return out


@mcp.tool()
def get_jira_transitions(issue_key: str, site: str = "auto") -> dict[str, Any]:
    """List the workflow transitions currently allowed from the ticket's
    status. **Read-only.**

    Use this before ``transition_jira_ticket`` — the returned set is the
    ONLY set of moves the project's workflow permits right now. Never
    invent transition names or ids from memory.

    Args:
        issue_key: e.g. ``"DEVOPS-52986"``.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).

    Returns ``{ok, issue_key, summary, current_status, count, transitions}``
    where each transition is ``{id, name, to_status}``.
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    issue_key = issue_key.strip()
    site_resolved = _resolve_site(site)

    status_code, summary, current_status, err = _fetch_issue_summary_status(
        site_resolved, issue_key
    )
    if status_code and status_code >= 400:
        return {
            "ok": False,
            "issue_key": issue_key,
            "status_code": status_code,
            "error": err,
        }

    tr_code, transitions_raw, tr_err = _fetch_available_transitions(
        site_resolved, issue_key
    )
    if tr_code >= 400:
        return {
            "ok": False,
            "issue_key": issue_key,
            "status_code": tr_code,
            "error": tr_err,
        }

    transitions = [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "to_status": (t.get("to") or {}).get("name"),
            "screen_fields": _summarize_transition_fields(t),
        }
        for t in transitions_raw
    ]
    return {
        "ok": True,
        "issue_key": issue_key,
        "url": f"{_jira_base_url(site_resolved)}/browse/{issue_key}",
        "summary": summary,
        "current_status": current_status,
        "count": len(transitions),
        "transitions": transitions,
        "instruction": (
            "These are the ONLY transitions the Jira workflow currently "
            f"allows from status '{current_status}'. When calling "
            "transition_jira_ticket, pass either the transition id or the "
            "exact name from this list — do NOT invent transitions and do "
            "NOT reuse transitions cached from a previous ticket. Each "
            "transition's `screen_fields` lists every field its transition "
            "screen exposes; `required=true` entries MUST be supplied via "
            "the `fields` argument of transition_jira_ticket or Jira will "
            "reject the transition with `Missing Required Field Information`."
        ),
    }


@mcp.tool()
def add_jira_comment(
    issue_key: str, body: str, site: str = "auto", confirmed_by_user: bool = False
) -> dict[str, Any]:
    """Add a comment to a Jira ticket. **Requires confirmation.**

    Args:
        issue_key: e.g. ``"DEVOPS-52986"`` or ``"OB-45834"``.
        body: Comment text. Jira wiki markup is supported.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
            Use ``site="cloud"`` for MyCompany tickets.
        confirmed_by_user: Must be ``True`` to actually post the comment.
            The first call (default ``False``) only returns a preview —
            show it to the user and re-invoke with ``True`` once they
            approve.
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    if not body or not body.strip():
        raise ValueError("body is required.")

    issue_key = issue_key.strip()
    site_resolved = _resolve_site(site)
    base = _jira_base_url(site_resolved)

    gate = _confirmation_gate(
        "add_comment",
        site_resolved,
        confirmed_by_user,
        preview={"issue_key": issue_key, "body": body, "url": f"{base}/browse/{issue_key}"},
    )
    if gate is not None:
        return gate

    response = _jira_post(
        site_resolved,
        f"/rest/api/latest/issue/{issue_key}/comment",
        json_body={"body": body},
    )
    if response.status_code >= 400:
        return {
            "ok": False,
            "action": "add_comment",
            "issue_key": issue_key,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
        }
    data = response.json() or {}
    return {
        "ok": True,
        "action": "add_comment",
        "issue_key": issue_key,
        "comment_id": data.get("id"),
        "author": (data.get("author") or {}).get("displayName"),
        "created": data.get("created"),
        "body": data.get("body"),
        "url": f"{base}/browse/{issue_key}",
    }


@mcp.tool()
def transition_jira_ticket(
    issue_key: str,
    transition: str,
    comment: str = "",
    fields: Optional[dict[str, Any]] = None,
    site: str = "auto",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Move a ticket through the workflow. **Only transitions currently
    permitted by the project's workflow are accepted.**

    The tool first calls Jira to list the allowed transitions from the
    ticket's current status and refuses any ``transition`` that is not
    in that set — the workflow is the source of truth.

    Args:
        issue_key: e.g. ``"DEVOPS-52986"``.
        transition: Either the transition id (``"31"``) or the exact
            transition name (``"Ready for deployment"``) from
            ``get_jira_transitions``. Case-insensitive on name.
        comment: Optional comment to post atomically with the transition.
        fields: Optional dict of fields required by the transition
            screen — e.g. ``{"Number of components deployed": 1}`` or
            ``{"customfield_19024": 1}``. Keys may be Jira ids or human
            names (resolved via the transition's own screen schema,
            which is not the same as ``editmeta``). Values follow the
            same rules as ``edit_jira_ticket.fields``. Any field the
            transition marks ``required=true`` MUST be supplied here or
            Jira responds with ``Missing Required Field Information``.
            Discover screen fields via ``get_jira_transitions`` — each
            entry's ``screen_fields`` lists them.
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    if not transition or not transition.strip():
        raise ValueError("transition (id or exact name) is required.")

    issue_key = issue_key.strip()
    transition_input = transition.strip()
    fields = fields or {}
    site_resolved = _resolve_site(site)
    base = _jira_base_url(site_resolved)

    status_code, _, current_status, err = _fetch_issue_summary_status(
        site_resolved, issue_key
    )
    if status_code and status_code >= 400:
        return {
            "ok": False,
            "action": "transition",
            "issue_key": issue_key,
            "status_code": status_code,
            "error": err,
        }

    tr_code, available, tr_err = _fetch_available_transitions(site_resolved, issue_key)
    if tr_code >= 400:
        return {
            "ok": False,
            "action": "transition",
            "issue_key": issue_key,
            "status_code": tr_code,
            "error": tr_err,
        }

    matched = None
    for t in available:
        if (
            str(t.get("id")) == transition_input
            or (t.get("name") or "").strip().lower() == transition_input.lower()
        ):
            matched = t
            break

    if matched is None:
        return {
            "ok": False,
            "action": "transition",
            "issue_key": issue_key,
            "current_status": current_status,
            "requested_transition": transition_input,
            "error": (
                f"Transition '{transition_input}' is not allowed from status "
                f"'{current_status}'. The workflow only permits the "
                "transitions listed under `allowed_transitions`."
            ),
            "allowed_transitions": [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "to_status": (t.get("to") or {}).get("name"),
                }
                for t in available
            ],
        }

    target_status = (matched.get("to") or {}).get("name")

    # Resolve caller-supplied field keys against the transition's own screen
    # schema (NOT editmeta — transition screens are independent). Rejections
    # short-circuit the write so no partial state can result.
    screen_fields_meta = matched.get("fields") or {}
    screen_name_to_id: dict[str, str] = {
        (meta.get("name") or "").strip().lower(): fid
        for fid, meta in screen_fields_meta.items()
        if isinstance(meta, dict) and meta.get("name")
    }
    resolved_fields: dict[str, Any] = {}
    field_rejections: list[dict[str, Any]] = []
    for raw_key, raw_value in fields.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        name = raw_key.strip()
        target_id = None
        if name in screen_fields_meta:
            target_id = name
        else:
            target_id = screen_name_to_id.get(name.lower())
        if target_id is None:
            field_rejections.append(
                {
                    "requested_key": raw_key,
                    "reason": (
                        f"Field is not on the transition screen for "
                        f"'{matched.get('name')}' (id={matched.get('id')}). "
                        "Call get_jira_transitions to see this transition's "
                        "exhaustive `screen_fields` list."
                    ),
                }
            )
            continue
        schema = (screen_fields_meta[target_id] or {}).get("schema") or {}
        resolved_fields[target_id] = _normalize_edit_value(schema, raw_value)

    if field_rejections:
        return {
            "ok": False,
            "action": "transition",
            "issue_key": issue_key,
            "transition": {"id": matched.get("id"), "name": matched.get("name")},
            "rejected_fields": field_rejections,
            "screen_fields": _summarize_transition_fields(matched),
            "instruction": (
                "One or more supplied fields are not on this transition's "
                "screen. No write was issued. Fix the field key(s) using "
                "`screen_fields` above and retry."
            ),
        }

    # Also flag missing required fields up-front — Jira would 400 with a
    # generic "Missing Required Field Information" otherwise.
    missing_required = [
        {"id": fid, "name": (meta or {}).get("name")}
        for fid, meta in screen_fields_meta.items()
        if isinstance(meta, dict)
        and meta.get("required")
        and fid not in resolved_fields
    ]
    if missing_required:
        return {
            "ok": False,
            "action": "transition",
            "issue_key": issue_key,
            "transition": {"id": matched.get("id"), "name": matched.get("name")},
            "missing_required_fields": missing_required,
            "screen_fields": _summarize_transition_fields(matched),
            "instruction": (
                "The transition screen requires the fields listed under "
                "`missing_required_fields`. Supply them via the `fields` "
                "argument and retry. No write was issued."
            ),
        }

    body: dict[str, Any] = {"transition": {"id": str(matched.get("id"))}}
    if resolved_fields:
        body["fields"] = resolved_fields
    if comment:
        body["update"] = {"comment": [{"add": {"body": comment}}]}

    gate = _confirmation_gate(
        "transition",
        site_resolved,
        confirmed_by_user,
        preview={
            "issue_key": issue_key,
            "from_status": current_status,
            "transition": {"id": matched.get("id"), "name": matched.get("name")},
            "to_status": target_status,
            "fields_to_set": resolved_fields or None,
            "comment": comment or None,
            "url": f"{base}/browse/{issue_key}",
        },
    )
    if gate is not None:
        return gate

    response = _jira_post(
        site_resolved,
        f"/rest/api/latest/issue/{issue_key}/transitions",
        json_body=body,
    )
    if response.status_code >= 400:
        return {
            "ok": False,
            "action": "transition",
            "issue_key": issue_key,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
            "attempted_body": body,
        }

    # Re-fetch to report the resulting status.
    _, _, new_status, _ = _fetch_issue_summary_status(site_resolved, issue_key)
    return {
        "ok": True,
        "action": "transition",
        "issue_key": issue_key,
        "from_status": current_status,
        "transition_used": {
            "id": matched.get("id"),
            "name": matched.get("name"),
        },
        "to_status": new_status or target_status,
        "comment_added": bool(comment),
        "fields_set": list(resolved_fields.keys()) or None,
        "url": f"{base}/browse/{issue_key}",
    }


def _search_assignable_users(
    site: str, issue_key: str, query: str, max_results: int = 20
) -> tuple[int, list[dict[str, Any]], Optional[str]]:
    """Search users assignable to ``issue_key`` whose username / display
    name / email matches ``query``."""
    response = _jira_get(
        site,
        "/rest/api/latest/user/assignable/search",
        params={
            "issueKey": issue_key,
            "username": query,
            "maxResults": max_results,
        },
    )
    if response.status_code >= 400:
        return response.status_code, [], _jira_error_message(response)
    return response.status_code, response.json() or [], None


@mcp.tool()
def assign_jira_ticket(
    issue_key: str,
    assignee: str,
    site: str = "auto",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Assign a ticket to a user. **Requires confirmation.** Handles
    ambiguity by returning candidates when the query matches more than one
    assignable user.

    Args:
        issue_key: e.g. ``"DEVOPS-52986"``.
        assignee: Username, display name, email prefix, or a partial name
            (``"john"``). Jira's assignable-user search is used to resolve
            it. If more than one assignable user matches, the tool returns
            a ``candidates`` list and refuses to guess — the caller must
            re-invoke with the exact ``username``.
            To unassign, pass ``"-1"``, ``"unassigned"``, ``"none"``, or
            ``"null"``.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
        confirmed_by_user: Must be ``True`` to actually assign/unassign.
            The first call (default ``False``) only returns a preview.
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    if not assignee or not assignee.strip():
        raise ValueError("assignee is required.")

    issue_key = issue_key.strip()
    query = assignee.strip()
    site_resolved = _resolve_site(site)
    base = _jira_base_url(site_resolved)

    # Unassign path
    if query.lower() in ("-1", "unassigned", "none", "null"):
        gate = _confirmation_gate(
            "unassign",
            site_resolved,
            confirmed_by_user,
            preview={"issue_key": issue_key, "url": f"{base}/browse/{issue_key}"},
        )
        if gate is not None:
            return gate

        response = _jira_put(
            site_resolved,
            f"/rest/api/latest/issue/{issue_key}/assignee",
            json_body={"name": None},
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "action": "assign",
                "issue_key": issue_key,
                "status_code": response.status_code,
                "error": _jira_error_message(response),
            }
        return {
            "ok": True,
            "action": "assign",
            "issue_key": issue_key,
            "new_assignee": None,
            "url": f"{base}/browse/{issue_key}",
        }

    status_code, users, err = _search_assignable_users(
        site_resolved, issue_key, query, max_results=20
    )
    if status_code >= 400:
        return {
            "ok": False,
            "action": "assign",
            "issue_key": issue_key,
            "status_code": status_code,
            "error": err or "Failed to search assignable users.",
        }

    if not users:
        return {
            "ok": False,
            "action": "assign",
            "issue_key": issue_key,
            "query": query,
            "error": f"No assignable users found matching '{query}'.",
        }

    # If the query matches an exact username or exact display name, prefer
    # that single user over the broader fuzzy result set.
    query_lower = query.lower()
    exact = [
        u
        for u in users
        if (u.get("name") or "").lower() == query_lower
        or (u.get("displayName") or "").lower() == query_lower
    ]
    if len(exact) == 1:
        users = exact

    if len(users) > 1:
        candidates = [
            {
                "username": u.get("name"),
                "display_name": u.get("displayName"),
                "email": u.get("emailAddress"),
                "active": u.get("active"),
            }
            for u in users
        ]
        return {
            "ok": False,
            "action": "assign",
            "issue_key": issue_key,
            "query": query,
            "ambiguous": True,
            "match_count": len(users),
            "candidates": candidates,
            "instruction": (
                f"Query '{query}' matched {len(users)} assignable users. "
                "Ask the user which one, and re-invoke assign_jira_ticket "
                "with the exact `username` (NOT the display name)."
            ),
        }

    user = users[0]
    username = user.get("name")
    resolved = {
        "username": username,
        "display_name": user.get("displayName"),
        "email": user.get("emailAddress"),
    }

    gate = _confirmation_gate(
        "assign",
        site_resolved,
        confirmed_by_user,
        preview={"issue_key": issue_key, "new_assignee": resolved, "url": f"{base}/browse/{issue_key}"},
    )
    if gate is not None:
        return gate

    response = _jira_put(
        site_resolved,
        f"/rest/api/latest/issue/{issue_key}/assignee",
        json_body={"name": username},
    )
    if response.status_code >= 400:
        return {
            "ok": False,
            "action": "assign",
            "issue_key": issue_key,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
        }
    return {
        "ok": True,
        "action": "assign",
        "issue_key": issue_key,
        "new_assignee": resolved,
        "url": f"{base}/browse/{issue_key}",
    }


@mcp.tool()
def list_jira_link_types(site: str = "auto") -> dict[str, Any]:
    """List the issue-link types available on this Jira instance. **Read-only.**

    Use this before ``link_jira_tickets`` to confirm the exact link type
    name (e.g. ``"Relates"``, ``"Blocks"``, ``"Cloners"``, ``"Duplicate"``)
    and the wording for each direction — link type names and phrasing vary
    per Jira instance and must not be guessed.

    Args:
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).

    Returns ``{ok, count, link_types}`` where each entry is
    ``{id, name, inward, outward}`` — ``inward``/``outward`` are the
    phrases Jira displays for each direction (e.g. name="Blocks",
    inward="is blocked by", outward="blocks").
    """
    site_resolved = _resolve_site(site)
    response = _jira_get(site_resolved, "/rest/api/latest/issueLinkType")
    if response.status_code >= 400:
        return {
            "ok": False,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
        }
    data = response.json() or {}
    link_types = [
        {
            "id": lt.get("id"),
            "name": lt.get("name"),
            "inward": lt.get("inward"),
            "outward": lt.get("outward"),
        }
        for lt in (data.get("issueLinkTypes") or [])
    ]
    return {"ok": True, "count": len(link_types), "link_types": link_types}


@mcp.tool()
def link_jira_tickets(
    inward_issue: str,
    outward_issue: str,
    link_type: str = "Relates",
    comment: str = "",
    site: str = "auto",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Create an issue link between two existing tickets. **Requires confirmation.**

    Wraps ``POST /rest/api/latest/issueLink``. ``link_type`` is validated
    against ``list_jira_link_types`` first and refused if it doesn't
    exist on this Jira instance — link type names are never guessed.

    Args:
        inward_issue: Key of the issue on the "inward" side. For
            link_type ``"Blocks"`` this is the ticket that "is blocked
            by" ``outward_issue``.
        outward_issue: Key of the issue on the "outward" side. For
            link_type ``"Blocks"`` this is the ticket that "blocks"
            ``inward_issue``. For symmetric types like ``"Relates"``
            (relates to / relates to) direction doesn't matter.
        link_type: Exact link type name from ``list_jira_link_types``
            (case-insensitive). Default ``"Relates"``.
        comment: Optional comment posted on ``inward_issue`` atomically
            with the link.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
        confirmed_by_user: Must be ``True`` to actually create the link.
            The first call (default ``False``) only returns a preview.
    """
    if not inward_issue or not inward_issue.strip():
        raise ValueError("inward_issue is required.")
    if not outward_issue or not outward_issue.strip():
        raise ValueError("outward_issue is required.")
    inward_issue = inward_issue.strip()
    outward_issue = outward_issue.strip()
    site_resolved = _resolve_site(site)
    base = _jira_base_url(site_resolved)

    lt_response = _jira_get(site_resolved, "/rest/api/latest/issueLinkType")
    if lt_response.status_code >= 400:
        return {
            "ok": False,
            "action": "link",
            "status_code": lt_response.status_code,
            "error": _jira_error_message(lt_response),
        }
    available = (lt_response.json() or {}).get("issueLinkTypes") or []
    matched = next(
        (
            lt
            for lt in available
            if (lt.get("name") or "").strip().lower() == link_type.strip().lower()
        ),
        None,
    )
    if matched is None:
        return {
            "ok": False,
            "action": "link",
            "requested_link_type": link_type,
            "error": f"Link type '{link_type}' does not exist on this Jira instance.",
            "available_link_types": [
                {
                    "name": lt.get("name"),
                    "inward": lt.get("inward"),
                    "outward": lt.get("outward"),
                }
                for lt in available
            ],
        }

    body: dict[str, Any] = {
        "type": {"name": matched.get("name")},
        "inwardIssue": {"key": inward_issue},
        "outwardIssue": {"key": outward_issue},
    }
    if comment:
        body["comment"] = {"body": comment}

    gate = _confirmation_gate(
        "link",
        site_resolved,
        confirmed_by_user,
        preview={
            "inward_issue": inward_issue,
            "outward_issue": outward_issue,
            "link_type": matched.get("name"),
            "inward_phrase": f"{inward_issue} {matched.get('inward')} {outward_issue}",
            "outward_phrase": f"{outward_issue} {matched.get('outward')} {inward_issue}",
            "comment": comment or None,
            "inward_url": f"{base}/browse/{inward_issue}",
            "outward_url": f"{base}/browse/{outward_issue}",
        },
    )
    if gate is not None:
        return gate

    response = _jira_post(site_resolved, "/rest/api/latest/issueLink", json_body=body)
    if response.status_code >= 400:
        return {
            "ok": False,
            "action": "link",
            "inward_issue": inward_issue,
            "outward_issue": outward_issue,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
        }
    return {
        "ok": True,
        "action": "link",
        "inward_issue": inward_issue,
        "outward_issue": outward_issue,
        "link_type": matched.get("name"),
        "inward_url": f"{base}/browse/{inward_issue}",
        "outward_url": f"{base}/browse/{outward_issue}",
    }


# ── Generic field edit + clone ───────────────────────────────────────────────
# Fields that must NEVER be edited through ``edit_jira_ticket`` because they
# have their own dedicated, safer tool or endpoint. Attempting to set any of
# these is refused before any write is issued.
_EDIT_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "status",       # go through transition_jira_ticket
        "assignee",     # go through assign_jira_ticket (disambiguation)
        "reporter",     # can silently break permissions; use Jira UI
        "comment",      # go through add_jira_comment
        "attachment",   # different endpoint
        "worklog",      # different endpoint
        "issuelinks",   # go through link_jira_tickets
        "subtasks",     # structural change
        "issuetype",    # structural change; use clone_jira_ticket if needed
        "project",      # structural change; use clone_jira_ticket if needed
    }
)


def _fetch_editmeta(site: str, issue_key: str) -> tuple[int, dict[str, Any], Optional[str]]:
    """Return ``(status_code, fields_meta, error_body)``. ``fields_meta`` is
    Jira's ``editmeta.fields`` map: ``{field_id: {name, schema, operations,
    allowedValues, required, ...}}``."""
    response = _jira_get(site, f"/rest/api/latest/issue/{issue_key}/editmeta")
    if response.status_code >= 400:
        return response.status_code, {}, _jira_error_message(response)
    return response.status_code, (response.json() or {}).get("fields") or {}, None


def _resolve_field_key(
    key: str,
    editmeta: dict[str, Any],
    custom_name_map: dict[str, str],
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a caller-supplied field key (id like ``customfield_16400`` or
    a human name like ``Environment`` / ``Rollout Instructions``) to the
    canonical Jira id and its human name. Returns ``(None, None)`` if it
    can't be resolved from the ``editmeta`` (i.e. not editable on this
    ticket in its current workflow state)."""
    if not key:
        return None, None
    key_stripped = key.strip()
    if key_stripped in editmeta:
        return key_stripped, editmeta[key_stripped].get("name") or key_stripped

    key_lower = key_stripped.lower()
    for fid, meta in editmeta.items():
        if (meta.get("name") or "").strip().lower() == key_lower:
            return fid, meta.get("name")

    # Try the global custom-field name map as a last resort — even if the
    # global map knows about the field, we still require it to be in the
    # editmeta below (returning None here).
    for fid, human in custom_name_map.items():
        if human.strip().lower() == key_lower and fid in editmeta:
            return fid, human

    return None, None


def _normalize_edit_value(schema: Optional[dict[str, Any]], value: Any) -> Any:
    """Normalize a caller-supplied value into the shape Jira's edit
    endpoint expects, based on ``schema``. Dicts and lists of dicts are
    passed through unchanged so power users can supply raw payloads.

    Simple mappings for common shapes:

    * ``option``, ``array of option``   → wrap strings as ``{"value": v}``
    * ``priority``, ``resolution``, ``version``, ``component`` (and array
      variants)                         → wrap strings as ``{"name": v}``
    * ``user`` (and array variants)     → wrap strings as ``{"name": v}``
    * ``string``, ``number``, ``date``, ``datetime`` → passthrough
    * ``array of string`` (labels)      → passthrough as list of strings
    """
    if schema is None:
        return value
    stype = (schema.get("type") or "").lower()
    items = (schema.get("items") or "").lower() if isinstance(schema.get("items"), str) else ""

    def _wrap_scalar(kind: str, v: Any) -> Any:
        if isinstance(v, dict):
            return v
        if kind == "option":
            return {"value": v}
        if kind in ("priority", "resolution", "version", "component",
                    "issuetype", "user", "group", "project"):
            return {"name": v}
        return v

    if stype == "array":
        seq = value if isinstance(value, list) else [value]
        if items in ("string", ""):
            return list(seq)
        return [_wrap_scalar(items, v) for v in seq]
    return _wrap_scalar(stype, value)


def _summarize_current_value(field_id: str, current_fields: dict[str, Any]) -> Any:
    """Return a JSON-serializable, flattened current value for the diff."""
    return _flatten_field_value(current_fields.get(field_id))


@mcp.tool()
def get_jira_editable_fields(issue_key: str, site: str = "auto") -> dict[str, Any]:
    """Scan a ticket for the fields that can be edited RIGHT NOW. **Read-only.**

    Uses Jira's ``/issue/{key}/editmeta`` endpoint so the returned set
    reflects what the project's workflow and the caller's permissions
    actually allow at this instant — not what's theoretically possible.

    Use this before ``edit_jira_ticket`` to pick valid field ids/names,
    inspect schemas (single vs multi, allowed values), and see current
    values.

    Args:
        issue_key: e.g. ``"DEVOPS-52986"``.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).

    Returns ``{ok, issue_key, url, current_status, count, fields}`` where
    each ``fields`` entry is:
    ``{id, name, required, operations, schema, allowed_values, current_value}``.
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    issue_key = issue_key.strip()
    site_resolved = _resolve_site(site)

    # Grab current values (all fields) in one shot to build the diff view.
    issue_response = _jira_get(
        site_resolved, f"/rest/api/latest/issue/{issue_key}", params={"fields": "*all"}
    )
    if issue_response.status_code >= 400:
        return {
            "ok": False,
            "issue_key": issue_key,
            "status_code": issue_response.status_code,
            "error": _jira_error_message(issue_response),
        }
    issue_data = issue_response.json() or {}
    current_fields = issue_data.get("fields") or {}
    current_status = (
        (current_fields.get("status") or {}).get("name") if current_fields else None
    )

    edit_code, editmeta, edit_err = _fetch_editmeta(site_resolved, issue_key)
    if edit_code >= 400:
        return {
            "ok": False,
            "issue_key": issue_key,
            "status_code": edit_code,
            "error": edit_err,
        }

    entries: list[dict[str, Any]] = []
    for fid, meta in editmeta.items():
        allowed_values = None
        av = meta.get("allowedValues")
        if isinstance(av, list) and av:
            allowed_values = [
                {
                    "id": v.get("id"),
                    "value": v.get("value"),
                    "name": v.get("name"),
                }
                for v in av
                if isinstance(v, dict)
            ]
        entries.append(
            {
                "id": fid,
                "name": meta.get("name"),
                "required": bool(meta.get("required")),
                "operations": meta.get("operations") or [],
                "schema": meta.get("schema"),
                "allowed_values": allowed_values,
                "current_value": _summarize_current_value(fid, current_fields),
                "forbidden_by_gate": fid in _EDIT_FORBIDDEN_FIELDS,
            }
        )
    entries.sort(key=lambda e: (e["forbidden_by_gate"], (e["name"] or "").lower()))

    return {
        "ok": True,
        "issue_key": issue_key,
        "url": f"{_jira_base_url(site_resolved)}/browse/{issue_key}",
        "current_status": current_status,
        "count": len(entries),
        "fields": entries,
        "forbidden_fields_note": (
            "Fields with `forbidden_by_gate=true` cannot be changed "
            "through edit_jira_ticket even if Jira lists them as editable. "
            "Use the dedicated tool instead: `status` → transition_jira_ticket, "
            "`assignee` → assign_jira_ticket, `comment` → add_jira_comment, "
            "`issuelinks` → link_jira_tickets."
        ),
        "instruction": (
            "This is the exhaustive set of fields editable on this ticket "
            "right now. Do not attempt to edit a field that is not in this "
            "list — Jira's editmeta is the source of truth. Pass either "
            "`id` (customfield_16400) or `name` (Environment) to "
            "edit_jira_ticket."
        ),
    }


@mcp.tool()
def edit_jira_ticket(
    issue_key: str,
    fields: dict[str, Any],
    notify_users: bool = True,
    site: str = "auto",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Edit one or more fields on a Jira ticket. **Requires confirmation.**

    Any field Jira reports as editable on this ticket via ``editmeta`` can
    be changed here (summary, description, priority, labels, environment,
    rollout instructions, custom fields, etc.). Fields that must go
    through a dedicated tool (``status``, ``assignee``, ``comment``,
    ``issuetype``, ``project``, ``reporter``, ``attachment``, ``worklog``,
    ``issuelinks``, ``subtasks``) are refused.

    Args:
        issue_key: e.g. ``"DEVOPS-52986"``.
        fields: Dict of updates. Keys may be Jira ids (``customfield_16400``)
            OR human names (``Environment``, ``Rollout Instructions``,
            ``Summary``, ``Description``). Values can be:

            * a plain string / number for text, numeric, and single-select
              fields (the tool will wrap it in the correct ``{"value": ...}``
              / ``{"name": ...}`` envelope based on the field's schema);
            * a list of strings for multi-select / labels / components /
              versions;
            * a fully-formed Jira dict (``{"value": "STG4"}``,
              ``{"name": "High"}``) if you want to bypass normalization;
            * ``None`` to clear the field.
        notify_users: Passed through to Jira as ``?notifyUsers=``. Default
            ``True``.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
        confirmed_by_user: Must be ``True`` to actually write. The first
            call (default ``False``) only returns the field diff preview.

    On success, returns the per-field diff (current → new) so the AI can
    report what actually changed.
    """
    if not issue_key or not issue_key.strip():
        raise ValueError("issue_key is required.")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields must be a non-empty dict of field updates.")

    issue_key = issue_key.strip()
    site_resolved = _resolve_site(site)
    base = _jira_base_url(site_resolved)

    # Pull the ticket + editmeta so we can validate, normalize, and diff.
    issue_response = _jira_get(
        site_resolved, f"/rest/api/latest/issue/{issue_key}", params={"fields": "*all"}
    )
    if issue_response.status_code >= 400:
        return {
            "ok": False,
            "action": "edit",
            "issue_key": issue_key,
            "status_code": issue_response.status_code,
            "error": _jira_error_message(issue_response),
        }
    issue_data = issue_response.json() or {}
    current_fields = issue_data.get("fields") or {}

    edit_code, editmeta, edit_err = _fetch_editmeta(site_resolved, issue_key)
    if edit_code >= 400:
        return {
            "ok": False,
            "action": "edit",
            "issue_key": issue_key,
            "status_code": edit_code,
            "error": edit_err,
        }
    custom_name_map = _custom_field_name_map(site_resolved)

    resolved: dict[str, dict[str, Any]] = {}
    diff: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    for raw_key, raw_value in fields.items():
        fid, human = _resolve_field_key(raw_key, editmeta, custom_name_map)
        if fid is None:
            rejections.append(
                {
                    "requested_key": raw_key,
                    "reason": (
                        "Field is not editable on this ticket right now "
                        "(not present in Jira's editmeta). Call "
                        "get_jira_editable_fields to see the exhaustive "
                        "editable set."
                    ),
                }
            )
            continue
        if fid in _EDIT_FORBIDDEN_FIELDS:
            rejections.append(
                {
                    "requested_key": raw_key,
                    "resolved_id": fid,
                    "resolved_name": human,
                    "reason": (
                        f"Field '{fid}' must go through its dedicated tool "
                        "(status→transition_jira_ticket, "
                        "assignee→assign_jira_ticket, "
                        "comment→add_jira_comment, "
                        "issuelinks→link_jira_tickets, "
                        "issuetype/project→clone_jira_ticket)."
                    ),
                }
            )
            continue

        meta = editmeta[fid]
        schema = meta.get("schema") or {}
        normalized = _normalize_edit_value(schema, raw_value)
        resolved[fid] = {
            "name": human,
            "new_value": normalized,
        }
        diff.append(
            {
                "field_id": fid,
                "field_name": human,
                "current": _summarize_current_value(fid, current_fields),
                "new": normalized,
                "required": bool(meta.get("required")),
            }
        )

    if rejections:
        return {
            "ok": False,
            "action": "edit",
            "issue_key": issue_key,
            "rejected_fields": rejections,
            "resolved_fields": [
                {"field_id": fid, "field_name": v.get("name")}
                for fid, v in resolved.items()
            ],
            "instruction": (
                "One or more fields cannot be edited through this tool. "
                "See `rejected_fields` for the reason. Fix the request "
                "(remove or replace those fields, or use the dedicated "
                "tool) and try again. No write is issued when any field "
                "is rejected."
            ),
        }

    if not resolved:
        return {
            "ok": False,
            "action": "edit",
            "issue_key": issue_key,
            "error": "No editable fields were provided.",
        }

    gate = _confirmation_gate(
        "edit",
        site_resolved,
        confirmed_by_user,
        preview={"issue_key": issue_key, "diff": diff, "url": f"{base}/browse/{issue_key}"},
    )
    if gate is not None:
        return gate

    put_body = {"fields": {fid: v["new_value"] for fid, v in resolved.items()}}
    response = _jira_put(
        site_resolved,
        f"/rest/api/latest/issue/{issue_key}",
        json_body=put_body,
        params={"notifyUsers": "true" if notify_users else "false"},
    )
    if response.status_code == 204:
        return {
            "ok": True,
            "action": "edit",
            "issue_key": issue_key,
            "updated_fields": [d["field_id"] for d in diff],
            "diff": diff,
            "url": f"{base}/browse/{issue_key}",
        }
    return {
        "ok": False,
        "action": "edit",
        "issue_key": issue_key,
        "status_code": response.status_code,
        "error": _jira_error_message(response),
    }


# Fields we copy from the source when cloning. Any override wins.
# These are *candidates* — each one is only actually copied if it also
# appears on the target project/issuetype's create screen (via createmeta).
_CLONE_COPY_FIELDS: tuple[str, ...] = (
    "summary",
    "description",
    "priority",
    "labels",
    "components",
    "versions",
    "fixVersions",
    "environment",
    JIRA_ENV_FIELD,
    JIRA_ORDER_FIELD,
)

# Project-scoped fields whose values (ids) belong to the source project and
# will not resolve in a different target project. Dropped from cross-project
# moves regardless of whether the target's create screen exposes them.
_PROJECT_SCOPED_FIELDS: frozenset[str] = frozenset(
    {"components", "versions", "fixVersions"}
)


def _fetch_source_issue_for_clone(
    site: str,
    issue_key: str,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Return ``(source_data, error)`` for the source of a clone/move.

    Tries the direct issue endpoint first. Some Jira instances 404 on
    ``/issue/{key}`` for closed tickets even when the same token can see
    them via ``/search`` (different permission checks in older Jira
    versions), so on 404 we fall back to a JQL lookup by key. If both
    fail, both statuses/bodies are returned so the caller can surface a
    diagnosable error.
    """
    direct = _jira_get(
        site, f"/rest/api/latest/issue/{issue_key}", params={"fields": "*all"}
    )
    if direct.status_code < 400:
        try:
            return direct.json() or {}, None
        except ValueError:
            return None, {
                "status_code": direct.status_code,
                "error": "Jira returned a non-JSON body for the source issue.",
            }

    if direct.status_code != 404:
        return None, {
            "status_code": direct.status_code,
            "error": _jira_error_message(direct),
            "attempted": f"GET /rest/api/latest/issue/{issue_key}",
        }

    # 404 fallback: try to find the issue via JQL. Escape any double
    # quotes in the key just in case.
    safe_key = issue_key.replace('"', '\\"')
    search_jql = f'key = "{safe_key}"'
    search_status, search_result, search_err = _jira_search_jql(
        site, search_jql, fields=["*all"], max_results=1
    )
    if search_status >= 400:
        return None, {
            "status_code": 404,
            "error": (
                f"Direct fetch returned 404 and search fallback also failed "
                f"({search_status}). Original: {_jira_error_message(direct)} | "
                f"Search: {(search_err or '')[:200]}"
            ),
            "attempted": [
                f"GET /rest/api/latest/issue/{issue_key}",
                f"JQL search fallback: {search_jql}",
            ],
        }

    issues = search_result["issues"]
    if not issues:
        return None, {
            "status_code": 404,
            "error": (
                f"Issue {issue_key!r} was not found via /issue/{{key}} (404) "
                "and search returned no matches. The token may lack "
                "permission to view it, or the key is wrong."
            ),
            "attempted": [
                f"GET /rest/api/latest/issue/{issue_key}",
                f"JQL search fallback: {search_jql}",
            ],
        }
    return issues[0], None


def _fetch_createmeta(
    site: str, project_key: str, issuetype_name: str
) -> tuple[int, dict[str, Any], Optional[str]]:
    """Return ``(status_code, fields_meta, error_body)`` for the create
    screen of ``project_key`` + ``issuetype_name``.

    Uses the paginated v2 createmeta endpoints introduced in Jira 9.x.
    The legacy ``/issue/createmeta?projectKeys=...&issuetypeNames=...``
    endpoint was removed and now falls through to
    ``/issue/{key=createmeta}`` returning 404 ``"Issue Does Not Exist"``.

    Two-step flow:
      1. ``GET /rest/api/latest/issue/createmeta/{projectKey}/issuetypes``
         → resolve ``issuetype_name`` to an ``issueTypeId`` (case-insensitive)
      2. ``GET /rest/api/latest/issue/createmeta/{projectKey}/issuetypes/{issueTypeId}``
         → paginated ``values[]`` of field metadata

    ``fields_meta`` is flattened into ``{field_id: {name, required, schema,
    allowedValues, defaultValue, hasDefaultValue, operations,
    autoCompleteUrl}}`` — the shape expected by
    ``_build_create_payload_from_source``. Anything not in this map will
    make Jira 400 with ``"Field X cannot be set. It is not on the
    appropriate screen, or unknown."``.
    """
    target_name = (issuetype_name or "").strip().lower()

    # ── Step 1: resolve issuetype name → id ──────────────────────────────
    issuetype_id: Optional[str] = None
    start_at = 0
    while True:
        response = _jira_get(
            site,
            f"/rest/api/latest/issue/createmeta/{project_key}/issuetypes",
            params={"startAt": start_at, "maxResults": 50},
        )
        if response.status_code >= 400:
            return response.status_code, {}, (
                f"createmeta issuetypes lookup failed for project "
                f"{project_key!r} ({response.status_code}): "
                f"{_jira_error_message(response)}"
            )
        try:
            page = response.json() or {}
        except ValueError:
            return response.status_code, {}, (
                "Jira returned a non-JSON body for the createmeta "
                "issuetypes lookup."
            )
        for entry in page.get("values") or []:
            if (entry.get("name") or "").strip().lower() == target_name:
                issuetype_id = str(entry.get("id") or "")
                break
        if issuetype_id or page.get("isLast", True):
            break
        start_at += page.get("maxResults") or 50

    if not issuetype_id:
        return 404, {}, (
            f"Issuetype {issuetype_name!r} is not available on project "
            f"{project_key!r}, or the token lacks Create Issues permission "
            "there."
        )

    # ── Step 2: fetch fields for that issuetype id ──────────────────────
    fields_meta: dict[str, Any] = {}
    start_at = 0
    while True:
        response = _jira_get(
            site,
            f"/rest/api/latest/issue/createmeta/{project_key}/issuetypes/{issuetype_id}",
            params={"startAt": start_at, "maxResults": 50},
        )
        if response.status_code >= 400:
            return response.status_code, {}, (
                f"createmeta fields lookup failed for "
                f"{project_key}/{issuetype_name} ({response.status_code}): "
                f"{_jira_error_message(response)}"
            )
        try:
            page = response.json() or {}
        except ValueError:
            return response.status_code, {}, (
                "Jira returned a non-JSON body for the createmeta "
                "fields lookup."
            )
        for entry in page.get("values") or []:
            fid = entry.get("fieldId")
            if not fid:
                continue
            fields_meta[fid] = {
                "name": entry.get("name"),
                "required": entry.get("required", False),
                "schema": entry.get("schema") or {},
                "allowedValues": entry.get("allowedValues"),
                "defaultValue": entry.get("defaultValue"),
                "hasDefaultValue": entry.get("hasDefaultValue", False),
                "operations": entry.get("operations") or [],
                "autoCompleteUrl": entry.get("autoCompleteUrl"),
            }
        if page.get("isLast", True):
            break
        start_at += page.get("maxResults") or 50

    return 200, fields_meta, None


def _build_create_payload_from_source(
    source_fields: dict[str, Any],
    overrides: dict[str, Any],
    createmeta: dict[str, Any],
    custom_name_map: dict[str, str],
    target_project_key: str,
    target_issuetype_name: str,
    *,
    apply_clone_prefix: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Assemble a ``POST /issue`` body from the source ticket's fields plus
    caller overrides. Fields copied from the source AND overrides are both
    filtered through ``createmeta`` (the target project + issuetype's
    create screen) so we never send a field Jira will reject.

    Returns ``(payload, unresolvable_overrides, skipped_source_fields)``
    — ``skipped_source_fields`` lists source fields we dropped because
    they aren't on the target create screen (useful for observability).
    """
    payload: dict[str, Any] = {
        "fields": {
            "project": {"key": target_project_key},
            "issuetype": {"name": target_issuetype_name},
        }
    }

    # project/issuetype have already been consumed to build createmeta;
    # strip them so they don't fall through as "overrides" below.
    remaining_overrides = {
        k: v for k, v in (overrides or {}).items() if k not in ("project", "issuetype")
    }

    override_summary = remaining_overrides.pop("summary", None)
    if override_summary is None:
        override_summary = remaining_overrides.pop("Summary", None)
    if override_summary is not None:
        payload["fields"]["summary"] = override_summary
    elif source_fields.get("summary"):
        src_summary = source_fields["summary"]
        payload["fields"]["summary"] = (
            f"[CLONE] {src_summary}" if apply_clone_prefix else src_summary
        )

    source_project_key = (source_fields.get("project") or {}).get("key")
    cross_project = bool(
        source_project_key and source_project_key != target_project_key
    )

    skipped_source_fields: list[dict[str, Any]] = []
    for fid in _CLONE_COPY_FIELDS:
        if fid == "summary":
            continue
        if fid not in source_fields:
            continue
        val = source_fields[fid]
        if val is None:
            continue
        # Jira sometimes rejects empty-list values even when the field is
        # on the screen; skip and let the target's default apply.
        if isinstance(val, list) and not val:
            continue
        if cross_project and fid in _PROJECT_SCOPED_FIELDS:
            skipped_source_fields.append(
                {
                    "field_id": fid,
                    "reason": (
                        f"Source ids belong to project {source_project_key!r} "
                        f"and cannot be reused in {target_project_key!r}."
                    ),
                }
            )
            continue
        if fid not in createmeta:
            skipped_source_fields.append(
                {
                    "field_id": fid,
                    "reason": (
                        "Not on the create screen for "
                        f"{target_project_key}/{target_issuetype_name}."
                    ),
                }
            )
            continue
        payload["fields"][fid] = val

    unresolvable: list[dict[str, Any]] = []
    for raw_key, raw_value in remaining_overrides.items():
        # Override lookup: try id directly, then human name via createmeta,
        # then via the global custom-field name map.
        target_id = None
        if raw_key in createmeta:
            target_id = raw_key
        else:
            key_lower = raw_key.strip().lower()
            for fid, meta in createmeta.items():
                if (meta.get("name") or "").strip().lower() == key_lower:
                    target_id = fid
                    break
            if target_id is None:
                for fid, human in custom_name_map.items():
                    if human.strip().lower() == key_lower and fid in createmeta:
                        target_id = fid
                        break
        if target_id is None:
            # description is settable structurally even when not exposed
            # in createmeta on some configurations.
            if raw_key in ("description", "Description"):
                payload["fields"]["description"] = raw_value
                continue
            unresolvable.append(
                {
                    "requested_key": raw_key,
                    "reason": (
                        "Not on the create screen for "
                        f"{target_project_key}/{target_issuetype_name}."
                    ),
                }
            )
            continue

        meta = createmeta[target_id]
        schema = meta.get("schema") or {}
        payload["fields"][target_id] = _normalize_edit_value(schema, raw_value)

    return payload, unresolvable, skipped_source_fields


@mcp.tool()
def clone_jira_ticket(
    source_issue_key: str,
    overrides: Optional[dict[str, Any]] = None,
    notify_users: bool = True,
    site: str = "auto",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Create a new ticket by cloning ``source_issue_key`` and applying
    ``overrides``. **Requires confirmation.**

    Copies these fields from the source: ``summary`` (prefixed with
    ``[CLONE]`` unless overridden), ``description``, ``priority``,
    ``labels``, ``components``, ``versions``, ``fixVersions``,
    ``environment`` and the MyCompany env/order custom fields. Everything
    else (assignee, status, comments, worklogs, attachments, links) is
    NOT copied — assign, transition, and comment on the clone separately
    after creation.

    Args:
        source_issue_key: e.g. ``"DEVOPS-52986"``.
        overrides: Optional dict of ``{field_id_or_name: value}`` to
            override the copied source values (or add fields that weren't
            on the source). Values follow the same rules as
            ``edit_jira_ticket.fields``. Use ``{"summary": "..."}`` to
            replace the auto ``[CLONE]`` prefix, ``{"project": "SUPPORT"}``
            to clone into another project, etc.
        notify_users: Passed to Jira as ``?notifyUsers=``. Default ``True``.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
            Source and target ticket are assumed to be on the same site.
        confirmed_by_user: Must be ``True`` to actually create the clone.
            The first call (default ``False``) only returns a preview of
            the resolved payload.
    """
    if not source_issue_key or not source_issue_key.strip():
        raise ValueError("source_issue_key is required.")
    source_issue_key = source_issue_key.strip()
    overrides = overrides or {}
    site_resolved = _resolve_site(site)
    base = _jira_base_url(site_resolved)

    src_data, src_err = _fetch_source_issue_for_clone(site_resolved, source_issue_key)
    if src_err is not None:
        return {
            "ok": False,
            "action": "clone",
            "source_issue_key": source_issue_key,
            **src_err,
        }
    assert src_data is not None  # for the type checker
    src_fields = src_data.get("fields") or {}

    # Resolve the target project + issuetype from overrides, falling back
    # to the source. These drive the createmeta lookup below, which is
    # what tells us exactly which fields the create screen accepts.
    override_project = overrides.get("project")
    if isinstance(override_project, dict):
        target_project_key = override_project.get("key") or override_project.get("name")
    else:
        target_project_key = override_project
    if not target_project_key:
        target_project_key = (src_fields.get("project") or {}).get("key")

    override_issuetype = overrides.get("issuetype")
    if isinstance(override_issuetype, dict):
        target_issuetype_name = override_issuetype.get("name")
    else:
        target_issuetype_name = override_issuetype
    if not target_issuetype_name:
        target_issuetype_name = (src_fields.get("issuetype") or {}).get("name")

    if not target_project_key or not target_issuetype_name:
        return {
            "ok": False,
            "action": "clone",
            "source_issue_key": source_issue_key,
            "error": (
                "Could not determine target project and issuetype from the "
                "source ticket or overrides."
            ),
        }

    cm_code, createmeta, cm_err = _fetch_createmeta(
        site_resolved, target_project_key, target_issuetype_name
    )
    if cm_code >= 400 or not createmeta:
        return {
            "ok": False,
            "action": "clone",
            "source_issue_key": source_issue_key,
            "status_code": cm_code,
            "error": cm_err
            or (
                f"Failed to fetch create screen for "
                f"{target_project_key}/{target_issuetype_name}."
            ),
        }

    custom_name_map = _custom_field_name_map(site_resolved)

    payload, unresolvable, skipped_source_fields = _build_create_payload_from_source(
        src_fields,
        overrides,
        createmeta,
        custom_name_map,
        target_project_key,
        target_issuetype_name,
    )
    if unresolvable:
        return {
            "ok": False,
            "action": "clone",
            "source_issue_key": source_issue_key,
            "target_project": target_project_key,
            "target_issuetype": target_issuetype_name,
            "unresolvable_overrides": unresolvable,
            "instruction": (
                "One or more overrides are not on the create screen for "
                f"{target_project_key}/{target_issuetype_name}. Remove or "
                "rename them (see list_jira_fields, or inspect the target "
                "issuetype's create screen) and try again."
            ),
        }

    gate = _confirmation_gate(
        "clone",
        site_resolved,
        confirmed_by_user,
        preview={
            "source_issue_key": source_issue_key,
            "target_project": target_project_key,
            "target_issuetype": target_issuetype_name,
            "payload_fields": payload.get("fields"),
        },
    )
    if gate is not None:
        return gate

    response = _jira_post(
        site_resolved,
        "/rest/api/latest/issue",
        json_body=payload,
        params={"notifyUsers": "true" if notify_users else "false"},
    )
    if response.status_code >= 400:
        return {
            "ok": False,
            "action": "clone",
            "source_issue_key": source_issue_key,
            "target_project": target_project_key,
            "target_issuetype": target_issuetype_name,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
            "attempted_payload": payload,
            "skipped_source_fields": skipped_source_fields,
        }
    data = response.json() or {}
    new_key = data.get("key")
    return {
        "ok": True,
        "action": "clone",
        "source_issue_key": source_issue_key,
        "target_project": target_project_key,
        "target_issuetype": target_issuetype_name,
        "new_issue_key": new_key,
        "new_issue_id": data.get("id"),
        "new_issue_url": f"{base}/browse/{new_key}" if new_key else None,
        "source_url": f"{base}/browse/{source_issue_key}",
        "skipped_source_fields": skipped_source_fields,
    }


@mcp.tool()
def create_jira_ticket(
    project: str,
    issuetype: str,
    fields: Optional[dict[str, Any]] = None,
    notify_users: bool = True,
    site: str = "auto",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Create a new Jira ticket from scratch (no source template).
    **Requires confirmation.**

    Wrapper around ``POST /rest/api/latest/issue``. Every field is
    pre-validated against the target project + issuetype's create
    screen (via the v2 createmeta endpoints) so Jira never sees a
    payload with a field it would reject as "not on the appropriate
    screen".

    Args:
        project: Target project key, e.g. ``"DEVOPS"``.
        issuetype: Target issue type name, e.g. ``"Task"``. Case-insensitive.
        fields: Dict of ``{field_id_or_name: value}`` for the new issue.
            - ``summary`` is required by Jira for every issuetype.
            - Many projects also require ``description`` (DEVOPS/Task does).
            - Custom fields can be given by human name
              (``"Environment": "STG4"``) or by id
              (``"customfield_12702": [{"value": "STG4"}]``).
            - Same value-normalization rules as ``edit_jira_ticket.fields``
              and ``clone_jira_ticket.overrides``.
        notify_users: Passed to Jira as ``?notifyUsers=``. Default ``True``.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
        confirmed_by_user: Must be ``True`` to actually create the ticket.
            The first call (default ``False``) only returns a preview of
            the resolved payload.

    Returns dict with ``ok``, ``new_issue_key``, ``new_issue_url`` on
    success; on failure, ``status_code``, ``error``, and either
    ``unresolvable_overrides`` (fields not on the create screen) or the
    raw Jira response body.
    """
    project = (project or "").strip()
    issuetype = (issuetype or "").strip()
    if not project or not issuetype:
        raise ValueError("project and issuetype are required.")
    fields = fields or {}
    site_resolved = _resolve_site(site)
    base = _jira_base_url(site_resolved)

    cm_code, createmeta, cm_err = _fetch_createmeta(site_resolved, project, issuetype)
    if cm_code >= 400 or not createmeta:
        return {
            "ok": False,
            "action": "create",
            "project": project,
            "issuetype": issuetype,
            "status_code": cm_code,
            "error": cm_err
            or f"Failed to fetch create screen for {project}/{issuetype}.",
        }

    custom_name_map = _custom_field_name_map(site_resolved)

    # Reuse the clone payload builder with empty source_fields — same
    # override-resolution + schema-normalization path, no source values.
    payload, unresolvable, _skipped = _build_create_payload_from_source(
        source_fields={},
        overrides=fields,
        createmeta=createmeta,
        custom_name_map=custom_name_map,
        target_project_key=project,
        target_issuetype_name=issuetype,
        apply_clone_prefix=False,
    )
    if unresolvable:
        return {
            "ok": False,
            "action": "create",
            "project": project,
            "issuetype": issuetype,
            "unresolvable_overrides": unresolvable,
            "instruction": (
                f"One or more fields are not on the create screen for "
                f"{project}/{issuetype}. Remove or rename them (see "
                "list_jira_fields) and try again."
            ),
        }

    if "summary" not in payload.get("fields", {}):
        return {
            "ok": False,
            "action": "create",
            "project": project,
            "issuetype": issuetype,
            "error": "'summary' is required and was not provided in 'fields'.",
        }

    gate = _confirmation_gate(
        "create",
        site_resolved,
        confirmed_by_user,
        preview={"project": project, "issuetype": issuetype, "payload_fields": payload.get("fields")},
    )
    if gate is not None:
        return gate

    response = _jira_post(
        site_resolved,
        "/rest/api/latest/issue",
        json_body=payload,
        params={"notifyUsers": "true" if notify_users else "false"},
    )
    if response.status_code >= 400:
        return {
            "ok": False,
            "action": "create",
            "project": project,
            "issuetype": issuetype,
            "status_code": response.status_code,
            "error": _jira_error_message(response),
            "attempted_payload": payload,
        }
    data = response.json() or {}
    new_key = data.get("key")
    return {
        "ok": True,
        "action": "create",
        "project": project,
        "issuetype": issuetype,
        "new_issue_key": new_key,
        "new_issue_id": data.get("id"),
        "new_issue_url": f"{base}/browse/{new_key}" if new_key else None,
    }


# ── Move ticket between projects ─────────────────────────────────────────────
# Jira's REST API does NOT expose an atomic project move (the UI's Move
# wizard is not REST-accessible). ``move_jira_ticket`` performs the closest
# safe equivalent as an ordered, previewable, single-token sequence:
#
#   1. Create a new ticket in the target project (like clone but WITHOUT
#      the ``[CLONE]`` prefix on the summary).
#   2. Add an issue link between source and new (``Cloners`` by default).
#   3. Post a pointer comment on the source referencing the new ticket.
#   4. Optionally transition the source through a workflow-allowed
#      transition to close it out.
#
# The default source-comment body uses the literal placeholder
# ``{new_ticket_key}``. The preview shows the placeholder verbatim so the
# user knows a substitution will happen; on confirm, the placeholder is
# substituted with the real key from step 1. This keeps the preview
# deterministic and the fingerprint stable.
_MOVE_DEFAULT_COMMENT_TEMPLATE = (
    "Moved to {new_ticket_key} in a different project."
)
_MOVE_COMMENT_KEY_PLACEHOLDER = "{new_ticket_key}"


@mcp.tool()
def move_jira_ticket(
    source_issue_key: str,
    target_project: str,
    target_issuetype: str = "",
    overrides: Optional[dict[str, Any]] = None,
    add_link: bool = True,
    link_type: str = "Cloners",
    close_source: bool = False,
    close_transition: str = "",
    source_move_comment: Optional[str] = None,
    notify_users: bool = True,
    site: str = "auto",
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Move a ticket to another project. **Requires confirmation.**

    Jira's REST API does NOT support an atomic project move (the Jira UI's
    Move wizard is not REST-accessible). This tool performs the closest
    safe equivalent as an ordered sequence:

    1. Create a new ticket in ``target_project`` (issuetype from
       ``target_issuetype`` or the source's issuetype), copying the same
       fields as ``clone_jira_ticket`` but WITHOUT the ``[CLONE]`` prefix
       on the summary.
    2. If ``add_link`` (default), link source ↔ new using ``link_type``
       (default ``"Cloners"`` — the source is marked "is cloned by" the
       new ticket).
    3. Post a pointer comment on the source referencing the new ticket.
       Default: ``"Moved to <new_key> in a different project."`` Override
       with ``source_move_comment`` (any ``{new_ticket_key}`` in your
       text is substituted with the real key). Pass ``""`` to skip.
    4. If ``close_source=True``, transition the source through
       ``close_transition``. Rejected if the transition is not permitted
       by the source ticket's current workflow.

    Any step failing mid-way is reported in ``step_results`` and later
    steps are skipped. The source is NOT automatically rolled back —
    Jira has no transaction across these endpoints.

    Args:
        source_issue_key: e.g. ``"DEVOPS-52986"``.
        target_project: Target project key, e.g. ``"SUPPORT"``.
        target_issuetype: Optional issuetype for the new ticket. Defaults
            to the source's issuetype. Must exist in the target project.
        overrides: Optional dict of extra field overrides. Same rules as
            ``clone_jira_ticket.overrides``. The ``project`` override is
            set automatically.
        add_link: Link source ↔ new. Default True.
        link_type: Issue link type name. Default ``"Cloners"``.
        close_source: Transition the source after the move. Default False.
        close_transition: Transition id or exact name on the source.
            Required when ``close_source=True``.
        source_move_comment: Comment body posted on the source. ``None``
            (default) uses the built-in template. ``""`` skips. Any
            occurrence of ``{new_ticket_key}`` is substituted with the
            real key.
        notify_users: Passed to Jira as ``?notifyUsers=``. Default True.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
        confirmed_by_user: Must be ``True`` to actually execute the move
            sequence. The first call (default ``False``) only returns a
            preview of the planned steps — nothing is created, linked,
            commented, or transitioned.
    """
    if not source_issue_key or not source_issue_key.strip():
        raise ValueError("source_issue_key is required.")
    if not target_project or not target_project.strip():
        raise ValueError("target_project is required.")
    if close_source and not (close_transition or "").strip():
        raise ValueError(
            "close_transition is required when close_source=True."
        )

    source_issue_key = source_issue_key.strip()
    target_project = target_project.strip()
    target_issuetype = (target_issuetype or "").strip()
    link_type_norm = (link_type or "Cloners").strip() or "Cloners"
    overrides = dict(overrides or {})
    site_resolved = _resolve_site(site)
    base = _jira_base_url(site_resolved)

    comment_template = (
        _MOVE_DEFAULT_COMMENT_TEMPLATE
        if source_move_comment is None
        else source_move_comment
    )

    # Fetch source (with fallback via search when /issue/{key} 404s)
    src_data, src_err = _fetch_source_issue_for_clone(site_resolved, source_issue_key)
    if src_err is not None:
        return {
            "ok": False,
            "action": "move",
            "source_issue_key": source_issue_key,
            **src_err,
        }
    assert src_data is not None
    src_fields = src_data.get("fields") or {}

    # Resolve target issuetype (explicit or fall back to source's).
    resolved_issuetype = (
        target_issuetype
        or (src_fields.get("issuetype") or {}).get("name")
    )
    if not resolved_issuetype:
        return {
            "ok": False,
            "action": "move",
            "source_issue_key": source_issue_key,
            "error": (
                "Could not determine target issuetype (source has none and "
                "no target_issuetype was supplied)."
            ),
        }

    # Fetch the target's create screen — Jira 400s on any field not on it.
    cm_code, createmeta, cm_err = _fetch_createmeta(
        site_resolved, target_project, resolved_issuetype
    )
    if cm_code >= 400 or not createmeta:
        return {
            "ok": False,
            "action": "move",
            "source_issue_key": source_issue_key,
            "status_code": cm_code,
            "error": cm_err
            or (
                f"Failed to fetch create screen for "
                f"{target_project}/{resolved_issuetype}."
            ),
        }

    custom_name_map = _custom_field_name_map(site_resolved)

    # A move keeps the summary verbatim (no [CLONE] prefix) unless the
    # caller overrides it.
    if (
        "summary" not in overrides
        and "Summary" not in overrides
        and src_fields.get("summary")
    ):
        overrides["summary"] = src_fields["summary"]

    create_payload, unresolvable, skipped_source_fields = (
        _build_create_payload_from_source(
            src_fields,
            overrides,
            createmeta,
            custom_name_map,
            target_project,
            resolved_issuetype,
            apply_clone_prefix=False,
        )
    )
    if unresolvable:
        return {
            "ok": False,
            "action": "move",
            "source_issue_key": source_issue_key,
            "target_project": target_project,
            "target_issuetype": resolved_issuetype,
            "unresolvable_overrides": unresolvable,
            "instruction": (
                "One or more overrides are not on the create screen for "
                f"{target_project}/{resolved_issuetype}. Fix them (see "
                "list_jira_fields or inspect the target issuetype's "
                "create screen) and try again."
            ),
        }

    # Validate close_transition against the source's current workflow
    validated_transition = None
    if close_source:
        tr_code, available, tr_err = _fetch_available_transitions(
            site_resolved, source_issue_key
        )
        if tr_code >= 400:
            return {
                "ok": False,
                "action": "move",
                "source_issue_key": source_issue_key,
                "status_code": tr_code,
                "error": tr_err,
            }
        needle = close_transition.strip()
        for t in available:
            if (
                str(t.get("id")) == needle
                or (t.get("name") or "").strip().lower() == needle.lower()
            ):
                validated_transition = t
                break
        if validated_transition is None:
            return {
                "ok": False,
                "action": "move",
                "source_issue_key": source_issue_key,
                "requested_transition": needle,
                "error": (
                    f"Transition {needle!r} is not allowed from the "
                    "source ticket's current status. The workflow only "
                    "permits the transitions listed under "
                    "`allowed_transitions`."
                ),
                "allowed_transitions": [
                    {
                        "id": t.get("id"),
                        "name": t.get("name"),
                        "to_status": (t.get("to") or {}).get("name"),
                    }
                    for t in available
                ],
            }

    gate = _confirmation_gate(
        "move",
        site_resolved,
        confirmed_by_user,
        preview={
            "source_issue_key": source_issue_key,
            "target_project": target_project,
            "target_issuetype": resolved_issuetype,
            "planned_steps": {
                "1_create": create_payload.get("fields"),
                "2_link": {"link_type": link_type_norm} if add_link else None,
                "3_comment_on_source": comment_template or None,
                "4_close_source": (
                    {"id": validated_transition.get("id"), "name": validated_transition.get("name")}
                    if validated_transition is not None
                    else None
                ),
            },
        },
    )
    if gate is not None:
        return gate

    step_results: list[dict[str, Any]] = []

    # Step 1: create in target project
    create_resp = _jira_post(
        site_resolved,
        "/rest/api/latest/issue",
        json_body=create_payload,
        params={"notifyUsers": "true" if notify_users else "false"},
    )
    if create_resp.status_code >= 400:
        step_results.append(
            {
                "step": "create_in_target_project",
                "ok": False,
                "status_code": create_resp.status_code,
                "error": _jira_error_message(create_resp),
            }
        )
        return {
            "ok": False,
            "action": "move",
            "source_issue_key": source_issue_key,
            "step_results": step_results,
            "new_issue_key": None,
            "instruction": (
                "Step 1 failed — no new ticket was created. Source is "
                "unchanged."
            ),
        }
    create_data = create_resp.json() or {}
    new_key = create_data.get("key")
    new_id = create_data.get("id")
    step_results.append(
        {
            "step": "create_in_target_project",
            "ok": True,
            "new_issue_key": new_key,
            "new_issue_id": new_id,
        }
    )

    # Step 2: link source ↔ new
    if add_link and new_key:
        link_body = {
            "type": {"name": link_type_norm},
            "inwardIssue": {"key": source_issue_key},
            "outwardIssue": {"key": new_key},
        }
        link_resp = _jira_post(
            site_resolved, "/rest/api/latest/issueLink", json_body=link_body
        )
        step_results.append(
            {
                "step": "link_source_to_new",
                "ok": link_resp.status_code < 400,
                "status_code": link_resp.status_code,
                "error": (
                    _jira_error_message(link_resp)
                    if link_resp.status_code >= 400
                    else None
                ),
                "link_type": link_type_norm,
            }
        )
        if link_resp.status_code >= 400:
            return {
                "ok": False,
                "action": "move",
                "source_issue_key": source_issue_key,
                "new_issue_key": new_key,
                "new_issue_url": f"{base}/browse/{new_key}",
                "source_url": f"{base}/browse/{source_issue_key}",
                "step_results": step_results,
                "instruction": (
                    "Step 2 (link) failed. The new ticket was created "
                    "but is NOT linked to the source. Later steps were "
                    "skipped."
                ),
            }

    # Step 3: comment on source
    if comment_template and new_key:
        final_body = comment_template.replace(
            _MOVE_COMMENT_KEY_PLACEHOLDER, new_key
        )
        comment_resp = _jira_post(
            site_resolved,
            f"/rest/api/latest/issue/{source_issue_key}/comment",
            json_body={"body": final_body},
        )
        step_results.append(
            {
                "step": "comment_on_source",
                "ok": comment_resp.status_code < 400,
                "status_code": comment_resp.status_code,
                "error": (
                    _jira_error_message(comment_resp)
                    if comment_resp.status_code >= 400
                    else None
                ),
                "body_posted": final_body,
            }
        )
        if comment_resp.status_code >= 400:
            return {
                "ok": False,
                "action": "move",
                "source_issue_key": source_issue_key,
                "new_issue_key": new_key,
                "new_issue_url": f"{base}/browse/{new_key}",
                "source_url": f"{base}/browse/{source_issue_key}",
                "step_results": step_results,
                "instruction": (
                    "Step 3 (source comment) failed. The new ticket and "
                    "link exist. Add the pointer comment manually or "
                    "call add_jira_comment separately."
                ),
            }

    # Step 4: transition source
    if close_source and validated_transition is not None:
        tr_body = {"transition": {"id": str(validated_transition.get("id"))}}
        tr_resp = _jira_post(
            site_resolved,
            f"/rest/api/latest/issue/{source_issue_key}/transitions",
            json_body=tr_body,
        )
        step_results.append(
            {
                "step": "transition_source",
                "ok": tr_resp.status_code < 400,
                "status_code": tr_resp.status_code,
                "error": (
                    _jira_error_message(tr_resp)
                    if tr_resp.status_code >= 400
                    else None
                ),
                "transition": {
                    "id": validated_transition.get("id"),
                    "name": validated_transition.get("name"),
                },
            }
        )
        if tr_resp.status_code >= 400:
            return {
                "ok": False,
                "action": "move",
                "source_issue_key": source_issue_key,
                "new_issue_key": new_key,
                "new_issue_url": f"{base}/browse/{new_key}",
                "source_url": f"{base}/browse/{source_issue_key}",
                "step_results": step_results,
                "instruction": (
                    "Step 4 (source transition) failed. Everything else "
                    "succeeded. Transition the source manually or via "
                    "transition_jira_ticket."
                ),
            }

    return {
        "ok": True,
        "action": "move",
        "source_issue_key": source_issue_key,
        "new_issue_key": new_key,
        "new_issue_id": new_id,
        "new_issue_url": f"{base}/browse/{new_key}" if new_key else None,
        "source_url": f"{base}/browse/{source_issue_key}",
        "step_results": step_results,
        "skipped_source_fields": skipped_source_fields,
    }


# ── Freestyle escape hatch ───────────────────────────────────────────────────
# When a caller needs to hit a Jira REST endpoint that isn't wrapped by a
# dedicated tool (watchers, remote links, worklogs, instance-specific
# endpoints, etc.), use this instead of guessing there's no way to do it.
# GET is read-only and always safe; POST/PUT/DELETE are writes and go
# through the same ``_confirmation_gate`` every other write tool uses.


@mcp.tool()
def jira_request(
    path: str,
    method: str = "GET",
    site: str = "auto",
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    confirmed_by_user: bool = False,
) -> dict[str, Any]:
    """Freestyle Jira REST call. **GET is safe; POST/PUT/DELETE are writes and require confirmation.**

    Use this when the dedicated tools don't cover what you need — e.g.
    ``/rest/api/latest/issue/{key}/watchers``,
    ``/rest/api/latest/issue/{key}/remotelink``,
    ``/rest/api/latest/issue/{key}/worklog``, a Cloud-only endpoint, or
    any REST call this server doesn't already wrap. For issue links
    specifically, prefer ``link_jira_tickets`` / ``list_jira_link_types``
    — they validate the link type for you.

    Args:
        path: REST path relative to the site's base URL, starting with
            ``/``. Examples: ``"/rest/api/latest/issue/DEVOPS-123/watchers"``,
            ``"/rest/api/latest/issueLinkType"``.
        method: One of ``GET`` (default), ``POST``, ``PUT``, ``DELETE``.
            Case-insensitive.
        site: ``onprem``, ``cloud``, or ``auto`` (the sole configured).
        params: Optional query string parameters.
        json_body: Optional JSON body (for POST/PUT).
        confirmed_by_user: Must be ``True`` for POST/PUT/DELETE to
            actually execute. The first call (default ``False``) for
            those verbs only returns a preview of the request — GET
            executes immediately regardless of this flag since it's
            read-only.

    Returns ``{ok, site, method, path, url, status_code, json, text}``.
    ``json`` is the parsed response body when Jira returned JSON;
    otherwise ``None`` and ``text`` holds the raw body (capped at 2000
    chars).
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
    base = _jira_base_url(site_resolved)

    if method_norm != "GET":
        gate = _confirmation_gate(
            f"jira_request {method_norm}",
            site_resolved,
            confirmed_by_user,
            preview={
                "method": method_norm,
                "path": path_norm,
                "params": params,
                "json_body": json_body,
                "url": f"{base}{path_norm}",
            },
        )
        if gate is not None:
            return gate

    if method_norm == "GET":
        response = _jira_get(site_resolved, path_norm, params=params)
    elif method_norm == "POST":
        response = _jira_post(site_resolved, path_norm, json_body=json_body, params=params)
    elif method_norm == "PUT":
        response = _jira_put(site_resolved, path_norm, json_body=json_body, params=params)
    else:
        response = _jira_delete(site_resolved, path_norm, params=params)

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
        "url": f"{base}{path_norm}",
        "status_code": response.status_code,
        "json": parsed_json,
        "text": None if parsed_json is not None else text_body[:2000],
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")