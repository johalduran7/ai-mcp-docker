"""Read-only Grafana / Loki MCP server for MyCompany + MyCompany environments.

Companion to the AWS log tools: those hit CloudWatch Logs directly, while
this server queries a Grafana instance (which proxies Loki under the hood)
so the AI can run arbitrary LogQL queries and inspect labels / datasources.

**THIS SERVER IS STRICTLY READ-ONLY.**

Every HTTP request is a ``GET``. There are no tools that create,
modify, delete, transition, comment, or otherwise change anything on
the Grafana side. The freestyle ``grafana_get`` tool refuses any path
that doesn't start with ``/api/`` and never accepts an alternative
method.

Two Grafana "providers" are supported, selected per-call via the
``provider`` argument (default ``"lvx"``):

  * ``"lvx"`` — the original MyCompany Grafana, one per env at
    ``http://logging-ui.service.<env>.mycompany.net:8080``, HTTP Basic
    auth with LDAP credentials. **Requires the Cisco AnyConnect VPN.**

  * ``"mycompany"`` — MyCompany org's own Grafana (the ``mon-*`` hosts a user
    means when they say "mon"), one per env from a hardcoded map (see
    ``MyCompany_GRAFANA_ENV_URLS`` below), reached via Okta SSO (username +
    password + MFA) instead of LDAP Basic auth. **Requires the
    Forticlient VPN.** Call ``mycompany_grafana_login`` first — it runs the
    Okta primary-auth + MFA + cookie-exchange dance and caches the
    authenticated session **in memory only**, for the lifetime of this
    container process (no host volume — the cookies are meaningless
    outside this process, so there's nothing worth persisting to disk;
    a fresh container needs to log in again, MFA included).

If a user's request is ambiguous about which Grafana they mean, ask
before running anything: "MyCompany Grafana (Cisco VPN)" vs. "MyCompany
Grafana (Forticlient VPN)". A connection failure against a ``"mycompany"``
target almost always means the user is on the wrong VPN client — say
so up front.

Configuration (env vars):
  * ``GRAFANA_USERNAME`` / ``GRAFANA_PASSWORD`` — required for
    ``provider="lvx"``. LDAP credentials.
  * ``GRAFANA_URL_TEMPLATE``— optional. Default
    ``http://logging-ui.service.{env}.mycompany.net:8080``.
    ``{env}`` is substituted for the env argument on every call.
  * ``GRAFANA_LOKI_UID_<ENV>`` — optional per-env override for the Loki
    datasource UID (e.g. ``GRAFANA_LOKI_UID_NA3=P8E80F9AEF21F6940``).
    When unset, the server auto-discovers by calling ``/api/datasources``
    and picking the datasource with ``type == "loki"``.
  * ``OKTA_USERNAME`` / ``OKTA_PASSWORD`` — required for
    ``provider="mycompany"``. ``OKTA_DOMAIN`` is optional (default
    ``"mycompany.okta.com"``, see ``DEFAULT_OKTA_DOMAIN`` below);
    everything else about the MyCompany side (the per-env ``mon-*`` URLs) is
    hardcoded — see ``MyCompany_GRAFANA_ENV_URLS``.
"""

import concurrent.futures
import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import requests
import urllib3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lvx-grafana")
from version_notifier import install as _install_version_notifier
_install_version_notifier(mcp, "lvx-grafana")

# lvx (MyCompany) Grafana — HTTP Basic / LDAP, one per env via URL template.
DEFAULT_URL_TEMPLATE = "http://logging-ui.service.{env}.mycompany.net:8080"

# mycompany (MyCompany org) Grafana — Okta SSO, hardcoded per-env URLs. These don't
# follow a clean `{env}` template (e.g. the `mon-na1` hostname alone spans
# 4 different domains for dev/test/staging/prod), so it's an explicit map.
DEFAULT_OKTA_DOMAIN = "mycompany.okta.com"
MyCompany_GRAFANA_ENV_URLS: dict[str, str] = {
    "tst2": "https://mon-na1.mycompany-dev.com",
    "tst3": "https://mon-na1.mycompany-test.com",
    "stg4": "https://mon-na1.mycompany-staging.com",
    "na4": "https://mon-na1.mycompany.com",
    "na3": "https://mon-na3.mycompany.com",
    "na6": "https://mon-na3.mycompany.com",
    "na5": "https://mon-ca1.mycompany.com",
    "eu1": "https://mon-eu1.mycompany.com",
    "uk2": "https://mon-uk2.mycompany-sov1.uk",
    "uk3": "https://mon-uk3.mycompany-sov2.uk",
    "au1": "https://mon-au1.mycompany.com",
    "perf1": "https://mon-na1.mycompany-perf.com",
}

# mycompany Grafana's constant product label, always paired with `service_name`
# (the MyCompany-side equivalent of lvx's `component` label).
MyCompany_DEFAULT_LABELS: dict[str, str] = {"openapm_product_name": "mycompany"}

_PROVIDERS = frozenset({"lvx", "mycompany"})

_VPN_HINTS = {
    "lvx": "lvx Grafana requires the Cisco AnyConnect VPN — confirm you're connected and try again.",
    "mycompany": "MyCompany Grafana (mon-*) requires the Forticlient VPN — confirm you're connected and try again.",
}

_ENV_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,15}$")

# Freestyle GET tool path safety: must start with /api/, no traversal.
_ALLOWED_PATH_RE = re.compile(r"^/api/[A-Za-z0-9_\-./]+$")

# Module-level Loki UID cache, keyed by "<provider>:<env>". MCP servers
# persist across many tool calls in a single AI-host session, so caching
# avoids one extra HTTP round-trip per query.
_LOKI_UID_CACHE: dict[str, str] = {}
_LOKI_UID_LOCK = threading.Lock()

# Component discovery map cache, keyed by "<provider>:<env>":
# {"map": {service_name: signals_dict}, "summary": {...}, "built_at": iso}.
# Built by discover_service_map (multithreaded Loki sampling), read by
# search_service_map. In-memory only — lost on container restart.
_SERVICE_MAP_CACHE: dict[str, dict[str, Any]] = {}
_SERVICE_MAP_LOCK = threading.Lock()

# mycompany (Okta/cookie) session state — in-memory only, never written to disk
# or a host volume. Lost on container restart; mycompany_grafana_login must be
# called again in that case (MFA included).
_MyCompany_LOCK = threading.Lock()
_mycompany_session: Optional[requests.Session] = None
# base_url -> True once /api/user has confirmed a live session for that host.
_mycompany_authenticated_hosts: dict[str, bool] = {}
# Single in-flight Okta MFA challenge (state_token/factor_id) awaiting a
# passcode from a follow-up mycompany_grafana_login(mfa_code=...) call.
_mycompany_pending_mfa: dict[str, Any] = {}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config / auth helpers ────────────────────────────────────────────────

def _env_or_raise(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Provide it via your AI host MCP configuration."
        )
    return value


def _check_provider(provider: str) -> str:
    provider_clean = (provider or "lvx").strip().lower()
    if provider_clean not in _PROVIDERS:
        raise ValueError(
            f"provider '{provider}' is invalid — must be 'lvx' (MyCompany Grafana, "
            "Cisco VPN) or 'mycompany' (MyCompany Grafana mon-*, Forticlient VPN)."
        )
    return provider_clean


def _okta_domain() -> str:
    return (os.environ.get("OKTA_DOMAIN") or DEFAULT_OKTA_DOMAIN).strip()


def _lvx_base_url(env: str) -> str:
    env_clean = (env or "").strip().lower()
    if not env_clean or not _ENV_RE.match(env_clean):
        raise ValueError(
            f"env '{env}' is not a valid MyCompany env label "
            "(lowercase alphanumeric + hyphens, up to 16 chars, e.g. 'na3', 'stg4')."
        )
    template = os.environ.get("GRAFANA_URL_TEMPLATE", DEFAULT_URL_TEMPLATE)
    if "{env}" not in template:
        raise RuntimeError(
            "GRAFANA_URL_TEMPLATE must contain the literal `{env}` placeholder."
        )
    url = template.format(env=env_clean).rstrip("/")
    # Sanity-check the resulting URL parses cleanly.
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"GRAFANA_URL_TEMPLATE produced an invalid URL: {url!r}")
    return url


def _mycompany_base_url(env: str) -> str:
    env_clean = (env or "").strip().lower()
    url = MyCompany_GRAFANA_ENV_URLS.get(env_clean)
    if not url:
        valid = ", ".join(sorted(MyCompany_GRAFANA_ENV_URLS))
        raise ValueError(
            f"env '{env}' has no MyCompany Grafana (mon-*) mapping. Valid envs: {valid}."
        )
    return url.rstrip("/")


def _grafana_base_url(env: str, provider: str = "lvx") -> str:
    return _mycompany_base_url(env) if provider == "mycompany" else _lvx_base_url(env)


def _auth() -> tuple[str, str]:
    return _env_or_raise("GRAFANA_USERNAME"), _env_or_raise("GRAFANA_PASSWORD")


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": "lvx-grafana-mcp",
    }


# ── mycompany (Okta SSO) session management ──────────────────────────────────

# Max concurrent connections kept warm to mon-* hosts. Must be >= the
# largest `max_workers` any caller passes to discover_service_map, or
# threads beyond this count just queue for a pooled connection instead of
# running in parallel (requests' default HTTPAdapter pool is only 10).
_MyCompany_POOL_SIZE = 32


def _get_mycompany_session() -> requests.Session:
    global _mycompany_session
    with _MyCompany_LOCK:
        if _mycompany_session is None:
            session = requests.Session()
            session.verify = False  # corporate proxy / self-signed certs
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=_MyCompany_POOL_SIZE, pool_maxsize=_MyCompany_POOL_SIZE
            )
            session.mount("https://", adapter)
            session.headers.update({
                **_headers(),
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            })
            _mycompany_session = session
        return _mycompany_session


def _mycompany_session_is_valid(base_url: str) -> bool:
    """Cheap check: does the cached session have a live Grafana session
    for this specific mon-* host? Hits /api/user (requires auth)."""
    session = _get_mycompany_session()
    try:
        resp = session.get(f"{base_url}/api/user", timeout=15)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return False
    ok = resp.status_code == 200 and "application/json" in (resp.headers.get("Content-Type") or "")
    with _MyCompany_LOCK:
        _mycompany_authenticated_hosts[base_url] = ok
    return ok


def _complete_grafana_sso(base_url: str) -> bool:
    """Hit {base_url}/login/okta with the current session cookies. If the
    session already carries valid Okta cookies (e.g. from a previous login
    against a *different* mon-* host), this silently completes SSO for the
    new host with no further MFA. Returns whether it worked."""
    session = _get_mycompany_session()
    try:
        session.get(f"{base_url}/login/okta", allow_redirects=True, timeout=30)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return False
    return _mycompany_session_is_valid(base_url)


def _okta_primary_auth(username: str, password: str) -> dict[str, Any]:
    session = _get_mycompany_session()
    resp = session.post(
        f"https://{_okta_domain()}/api/v1/authn",
        json={"username": username, "password": password},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code not in (200, 401):
        resp.raise_for_status()
    data = resp.json() if resp.content else {}
    data["_http_status"] = resp.status_code
    return data


def _okta_verify_mfa(state_token: str, factor_id: str, passcode: str) -> dict[str, Any]:
    session = _get_mycompany_session()
    resp = session.post(
        f"https://{_okta_domain()}/api/v1/authn/factors/{factor_id}/verify",
        json={"stateToken": state_token, "passCode": passcode},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )
    data = resp.json() if resp.content else {}
    data["_http_status"] = resp.status_code
    return data


def _okta_extract_totp_factor(auth_data: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Return (state_token, factor_id) for the first TOTP-capable factor."""
    state_token = auth_data.get("stateToken")
    embedded = auth_data.get("_embedded", {}) or {}
    factors = embedded.get("factors") or embedded.get("authenticators") or []
    if not factors and "factor" in embedded:
        factors = [embedded["factor"]]
    for f in factors:
        f_type = f.get("factorType") or f.get("type")
        if f_type in ("token:software:totp", "app:totp", "token"):
            return state_token, f.get("id")
    return state_token, None


def _finish_okta_session_exchange(session_token: str, base_url: str) -> bool:
    """Exchange an Okta sessionToken for the `sid` cookie, then complete
    Grafana's SSO handshake for the target host."""
    session = _get_mycompany_session()
    base_okta_url = f"https://{_okta_domain()}"
    target_app_url = (
        f"{base_okta_url}/app/UserHome?iss="
        f"{urllib.parse.quote(base_okta_url, safe='')}&session_hint=AUTHENTICATED"
    )
    redirect_url = (
        f"{base_okta_url}/login/sessionCookieRedirect?token={session_token}"
        f"&redirectUrl={urllib.parse.quote(target_app_url, safe='')}"
    )
    try:
        session.get(redirect_url, allow_redirects=True, timeout=30)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return False
    return _complete_grafana_sso(base_url)


def _ensure_mycompany_authenticated(base_url: str) -> None:
    """Raise RuntimeError with a clear next-step message if there is no
    live mycompany-Grafana session for this host yet. Never attempts MFA itself
    — that only happens inside the explicit mycompany_grafana_login tool."""
    with _MyCompany_LOCK:
        already_ok = _mycompany_authenticated_hosts.get(base_url, False)
    if already_ok and _mycompany_session_is_valid(base_url):
        return
    # One more chance: maybe the session already has valid Okta cookies
    # from a login against a different mon-* host.
    if _complete_grafana_sso(base_url):
        return
    raise RuntimeError(
        "No authenticated MyCompany Grafana session for this env. Call "
        "mycompany_grafana_login(env=...) first. If that also fails, confirm "
        "you're connected to the Forticlient VPN."
    )


# ── HTTP dispatch (provider-aware) ───────────────────────────────────────

def _grafana_get(
    env: str, path: str, params: Optional[dict[str, Any]] = None,
    timeout: int = 30, provider: str = "lvx", require_auth: bool = True,
) -> requests.Response:
    """Single low-level GET against a per-env Grafana instance. Every HTTP
    call in this server routes through here. Dispatches on ``provider``:
    ``"lvx"`` uses HTTP Basic (LDAP); ``"mycompany"`` uses the cached
    Okta-authenticated cookie session. ``require_auth=False`` skips the
    mycompany-session check — only used for the unauthenticated ``/api/health``
    probe in ``grafana_health``."""
    provider_clean = _check_provider(provider)
    base_url = _grafana_base_url(env, provider_clean)
    url = f"{base_url}{path}"
    if provider_clean == "mycompany":
        if require_auth:
            _ensure_mycompany_authenticated(base_url)
        session = _get_mycompany_session()
        try:
            return session.get(url, params=params, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            raise RuntimeError(
                f"MyCompany Grafana at {url} is not reachable ({exc.__class__.__name__}). "
                f"{_VPN_HINTS['mycompany']}"
            ) from exc
    try:
        return requests.get(
            url,
            headers=_headers(),
            params=params,
            auth=_auth(),
            timeout=timeout,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise RuntimeError(
            f"Grafana at {url} is not reachable ({exc.__class__.__name__}). "
            f"{_VPN_HINTS['lvx']}"
        ) from exc


# ── Time-window resolution ──────────────────────────────────────────────

def _parse_iso_datetime(value: str) -> Optional[datetime]:
    """Accept RFC3339 / ISO-8601 strings, return a tz-aware datetime.
    Returns None on failure so callers can fall back to defaults."""
    if not value or not value.strip():
        return None
    stripped = value.strip()
    # Python's fromisoformat accepts most flavors as of 3.11; normalize Z.
    if stripped.endswith("Z"):
        stripped = stripped[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(stripped)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_time_window_ns(
    since_minutes: int,
    since_hours: int,
    since_days: int,
    start_time: str,
    end_time: str,
) -> tuple[int, int, dict[str, Any]]:
    """Return ``(start_ns, end_ns, meta)`` — Loki wants nanoseconds since epoch.

    Priority when multiple are set: explicit ``start_time`` / ``end_time`` >
    ``since_minutes`` > ``since_hours`` > ``since_days``. Defaults to the
    last 60 minutes.
    """
    now = datetime.now(timezone.utc)
    explicit_start = _parse_iso_datetime(start_time)
    explicit_end = _parse_iso_datetime(end_time)

    if explicit_start or explicit_end:
        start_dt = explicit_start or (now - timedelta(hours=1))
        end_dt = explicit_end or now
        label = f"{start_dt.isoformat()} → {end_dt.isoformat()}"
    else:
        if since_minutes and since_minutes > 0:
            span = timedelta(minutes=int(since_minutes))
            label = f"{since_minutes}m"
        elif since_hours and since_hours > 0:
            span = timedelta(hours=int(since_hours))
            label = f"{since_hours}h"
        elif since_days and since_days > 0:
            span = timedelta(days=int(since_days))
            label = f"{since_days}d"
        else:
            span = timedelta(minutes=60)
            label = "60m (default)"
        start_dt = now - span
        end_dt = now

    start_ns = int(start_dt.timestamp() * 1_000_000_000)
    end_ns = int(end_dt.timestamp() * 1_000_000_000)
    return start_ns, end_ns, {
        "lookback": label,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
    }


# ── Loki datasource UID resolution ──────────────────────────────────────

def _resolve_loki_uid(env: str, override: str = "", provider: str = "lvx") -> str:
    """Return the Loki datasource UID for the given env.

    Priority: explicit ``override`` argument > ``GRAFANA_LOKI_UID_<ENV>``
    (or ``GRAFANA_LOKI_UID_MyCompany_<ENV>`` for ``provider="mycompany"``) env var >
    runtime discovery via ``/api/datasources``. The result is cached per
    ``<provider>:<env>`` for the lifetime of the process.
    """
    if override and override.strip():
        return override.strip()
    provider_clean = _check_provider(provider)
    env_key = (env or "").strip().lower()
    cache_key = f"{provider_clean}:{env_key}"
    var_prefix = "GRAFANA_LOKI_UID_MyCompany_" if provider_clean == "mycompany" else "GRAFANA_LOKI_UID_"
    env_var = f"{var_prefix}{env_key.upper().replace('-', '_')}"
    from_env = os.environ.get(env_var)
    if from_env and from_env.strip():
        return from_env.strip()

    with _LOKI_UID_LOCK:
        cached = _LOKI_UID_CACHE.get(cache_key)
        if cached:
            return cached

    resp = _grafana_get(env_key, "/api/datasources", provider=provider_clean)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Grafana /api/datasources returned {resp.status_code} for env "
            f"'{env_key}' (provider={provider_clean}): {resp.text[:300]}"
        )
    data = resp.json() or []
    loki_entries = [d for d in data if (d.get("type") or "").lower() == "loki"]
    if not loki_entries:
        raise RuntimeError(
            f"No Loki datasource found in Grafana for env '{env_key}' "
            f"(provider={provider_clean}). Set {env_var} in your MCP host "
            "config to override."
        )
    # Prefer one literally named "Loki" when multiple exist; else first.
    preferred = next(
        (d for d in loki_entries if (d.get("name") or "").lower() == "loki"),
        loki_entries[0],
    )
    uid = preferred.get("uid") or ""
    if not uid:
        raise RuntimeError(
            f"Loki datasource in env '{env_key}' has no uid — Grafana response "
            "is malformed. Report to your DevOps team."
        )
    with _LOKI_UID_LOCK:
        _LOKI_UID_CACHE[cache_key] = uid
    return uid


def _loki_proxy_path(loki_uid: str, endpoint: str) -> str:
    """Compose the Grafana proxy path for a Loki API endpoint.
    ``endpoint`` is the Loki path (starting with ``/loki/api/v1/…``)."""
    return f"/api/datasources/proxy/uid/{loki_uid}{endpoint}"


# ── LogQL convenience builder ───────────────────────────────────────────
# Loki is the default datasource everywhere on this server. Since almost
# every LogQL query the AI writes looks like
# ``{component="ACD",host="10.x"} |= "ERROR"``, the tools accept a
# ``labels`` / ``line_filter`` / ``line_regex`` shortcut that composes
# the LogQL for you. Raw ``query`` is still supported for anything the
# shortcut doesn't express (rate/sum/aggregate expressions, unwrap, …).

_LOKI_LABEL_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _escape_loki_string(value: str) -> str:
    """Escape a string for embedding in a LogQL double-quoted literal."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _build_logql_from_shortcuts(
    labels: Optional[dict[str, str]],
    line_filter: str,
    line_regex: str,
) -> Optional[str]:
    """Compose a LogQL query string from label matchers + line filters.

    Returns ``None`` when no shortcut inputs are provided (so the caller
    can fall back to whatever raw ``query`` was passed in). Raises
    ``ValueError`` when shortcuts are provided but malformed.
    """
    line_filter = (line_filter or "").strip()
    line_regex = (line_regex or "").strip()
    have_labels = bool(labels)
    have_filters = bool(line_filter or line_regex)
    if not have_labels and not have_filters:
        return None
    if have_filters and not have_labels:
        raise ValueError(
            "`line_filter` / `line_regex` require at least one label matcher "
            "in `labels`. LogQL requires a stream selector — pass e.g. "
            "`labels={\"component\":\"ACD\"}` or send a full raw `query`."
        )

    matchers: list[str] = []
    for key, value in (labels or {}).items():
        key_clean = (key or "").strip()
        if not _LOKI_LABEL_KEY_RE.match(key_clean):
            raise ValueError(
                f"Invalid Loki label name {key!r} — must match [A-Za-z_][A-Za-z0-9_]*."
            )
        matchers.append(f'{key_clean}="{_escape_loki_string(value)}"')

    parts: list[str] = ["{" + ",".join(matchers) + "}"]
    if line_filter:
        parts.append(f'|= "{_escape_loki_string(line_filter)}"')
    if line_regex:
        # LogQL regex is RE2. Pass the pattern through verbatim inside the
        # quoted string; only quotes/backslashes need escaping.
        parts.append(f'|~ "{_escape_loki_string(line_regex)}"')
    return " ".join(parts)


def _apply_mycompany_defaults(provider: str, labels: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
    """For ``provider="mycompany"``, auto-inject the constant
    ``openapm_product_name=mycompany`` label so callers only need to pass
    ``service_name`` (the MyCompany-side equivalent of lvx's `component`
    label). Applied even when ``labels`` is empty/``None`` so every mycompany
    query defaults to the mycompany product unless the caller explicitly
    overrides ``openapm_product_name`` in ``labels``. No-op for
    ``provider="lvx"``."""
    if provider != "mycompany":
        return labels
    merged = dict(MyCompany_DEFAULT_LABELS)
    merged.update(labels or {})
    return merged


# ── Component discovery (multithreaded service_name -> component map) ──
# Loki's `service_name` label only tells you the k8s deployment umbrella —
# some, like MyCompany's `apps-outbound`, host several distinct sub-components
# that only show up as an embedded `"service":"..."` field inside the JSON
# log body (e.g. `OPSS`, `OPA`), never as a queryable label. These helpers
# sample real log lines to surface that hidden structure and cache it so
# a user's vague "check the logs for X" can be resolved to concrete
# `service_name` / `openapm_product_name` filter criteria.

# Match-everything stream selectors used to enumerate label values across
# the *entire* env, bypassing `_apply_mycompany_defaults`'s hard-coded
# `openapm_product_name="mycompany"` matcher (which would hide every other
# product on MyCompany Grafana).
_MATCH_ALL_SELECTOR = {
    "mycompany": '{openapm_product_name=~".+"}',
    "lvx": '{component=~".+"}',
}

_EMBEDDED_SERVICE_RE = re.compile(r'"service"\s*:\s*"([^"]+)"', re.IGNORECASE)
_TRAILING_HASH_RE = re.compile(r"-[0-9a-f]{8,}$")


def _strip_trailing_hash(name: str) -> str:
    """Strip a trailing k8s-style hash suffix so pod/container names like
    ``outbound-preagent-71808b1c527f75297cb9a6c724d6188dcc771cca067ccea002bed935403ee6f2``
    collapse down to the readable ``outbound-preagent``."""
    return _TRAILING_HASH_RE.sub("", name or "")


def _fetch_label_values_raw(
    env: str,
    loki_uid: str,
    label: str,
    match_all_query: str,
    start_ns: int,
    end_ns: int,
    provider: str,
) -> list[str]:
    """Fetch every value of ``label`` across the whole env, using an explicit
    ``match_all_query`` stream selector instead of the mycompany-provider default
    (``openapm_product_name="mycompany"``) so values from every product show up."""
    resp = _grafana_get(
        env,
        _loki_proxy_path(loki_uid, f"/loki/api/v1/label/{label}/values"),
        params={"start": start_ns, "end": end_ns, "query": match_all_query},
        provider=provider,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"label/{label}/values returned {resp.status_code}: {(resp.text or '')[:300]}"
        )
    body = resp.json() or {}
    return body.get("data") or []


def _extract_component_signals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive component-discovery signals from a batch of flattened Loki
    rows: embedded ``"service":"..."`` JSON fields in the message body, plus
    namespace / pod / container name bases (hash-stripped) from stream labels."""
    embedded_services: set[str] = set()
    namespaces: set[str] = set()
    pod_bases: set[str] = set()
    container_bases: set[str] = set()
    product_names: set[str] = set()

    for row in rows:
        message = row.get("message") or ""
        for match in _EMBEDDED_SERVICE_RE.finditer(message):
            embedded_services.add(match.group(1))

        stream = row.get("stream") or {}
        for key, value in stream.items():
            if not value:
                continue
            key_lower = key.lower()
            if key_lower == "openapm_product_name":
                product_names.add(value)
            elif "namespace" in key_lower:
                namespaces.add(value)
            elif "pod" in key_lower:
                pod_bases.add(_strip_trailing_hash(value))
            elif "container" in key_lower:
                container_bases.add(_strip_trailing_hash(value))

    return {
        "sample_size": len(rows),
        "embedded_services": sorted(embedded_services),
        "namespaces": sorted(namespaces),
        "pod_bases": sorted(pod_bases),
        "container_bases": sorted(container_bases),
        "openapm_product_names": sorted(product_names),
    }


# ── Loki response flattening ────────────────────────────────────────────

def _flatten_loki_result(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a Loki ``data.result`` block into a flat list of rows the AI
    can consume without re-parsing.

    Handles both ``streams`` (log lines) and ``matrix`` (metrics)
    result types. For streams, each entry becomes
    ``{timestamp_ns, timestamp_iso, stream: {...}, message}``.
    """
    if not isinstance(data, dict):
        return []
    result_type = data.get("resultType")
    result = data.get("result") or []
    rows: list[dict[str, Any]] = []
    if result_type == "streams":
        for stream in result:
            labels = stream.get("stream") or {}
            for value in stream.get("values") or []:
                if not isinstance(value, list) or len(value) < 2:
                    continue
                ns_str, msg = value[0], value[1]
                try:
                    ns = int(ns_str)
                    iso = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()
                except (ValueError, TypeError):
                    ns, iso = None, None
                rows.append(
                    {
                        "timestamp_ns": ns,
                        "timestamp_iso": iso,
                        "stream": labels,
                        "message": msg,
                    }
                )
    elif result_type in ("matrix", "vector"):
        for series in result:
            labels = series.get("metric") or {}
            values = series.get("values") or ([series.get("value")] if series.get("value") else [])
            for v in values:
                if not isinstance(v, (list, tuple)) or len(v) < 2:
                    continue
                ts, val = v[0], v[1]
                rows.append(
                    {
                        "timestamp": ts,
                        "labels": labels,
                        "value": val,
                    }
                )
    return rows


# ── Tools (all read-only) ───────────────────────────────────────────────

@mcp.tool()
def list_datasources(env: str, provider: str = "lvx") -> dict[str, Any]:
    """List every Grafana datasource in the target env. **Read-only.**

    Args:
        env: env label like ``"na3"``, ``"eu1"``, ``"stg4"``.
        provider: ``"lvx"`` (MyCompany Grafana, Cisco VPN, default) or
            ``"mycompany"`` (MyCompany Grafana mon-*, Forticlient VPN — call
            ``mycompany_grafana_login`` first).

    Returns each datasource's id, uid, name, type, and proxy url so the
    AI can pick the right one before running a query. Use this if the
    auto-discovered Loki UID doesn't match what you want to query.
    """
    try:
        provider_clean = _check_provider(provider)
        resp = _grafana_get(env, "/api/datasources", provider=provider_clean)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "env": env, "provider": provider, "error": str(exc)}
    if resp.status_code != 200:
        return {
            "ok": False,
            "env": env,
            "provider": provider_clean,
            "status_code": resp.status_code,
            "error": (resp.text or "")[:300],
        }
    data = resp.json() or []
    trimmed = [
        {
            "id": d.get("id"),
            "uid": d.get("uid"),
            "name": d.get("name"),
            "type": d.get("type"),
            "type_name": d.get("typeName"),
            "url": d.get("url"),
            "is_default": bool(d.get("isDefault")),
            "read_only": bool(d.get("readOnly")),
        }
        for d in data
    ]
    return {
        "ok": True,
        "env": env,
        "provider": provider_clean,
        "base_url": _grafana_base_url(env, provider_clean),
        "count": len(trimmed),
        "datasources": trimmed,
    }


@mcp.tool()
def list_loki_labels(
    env: str,
    since_minutes: int = 0,
    since_hours: int = 0,
    since_days: int = 0,
    start_time: str = "",
    end_time: str = "",
    loki_uid: str = "",
    provider: str = "lvx",
) -> dict[str, Any]:
    """List Loki label names available in the given env. **Read-only.**

    Use this to discover what dimensions you can query on. For
    ``provider="lvx"``: ``component``, ``host``, ``image_name``,
    ``profile``, … For ``provider="mycompany"``: ``service_name`` (the
    component equivalent), ``openapm_product_name``, … Defaults to the
    last 60 minutes; override with ``since_minutes`` / ``since_hours`` /
    ``since_days`` or explicit ``start_time`` / ``end_time`` (ISO-8601 /
    RFC3339).
    """
    try:
        provider_clean = _check_provider(provider)
        uid = _resolve_loki_uid(env, override=loki_uid, provider=provider_clean)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "env": env, "provider": provider, "error": str(exc)}
    start_ns, end_ns, window = _resolve_time_window_ns(
        since_minutes, since_hours, since_days, start_time, end_time
    )
    try:
        resp = _grafana_get(
            env,
            _loki_proxy_path(uid, "/loki/api/v1/labels"),
            params={"start": start_ns, "end": end_ns},
            provider=provider_clean,
        )
    except RuntimeError as exc:
        return {"ok": False, "env": env, "provider": provider_clean, "loki_uid": uid, "error": str(exc)}
    if resp.status_code != 200:
        return {
            "ok": False,
            "env": env,
            "provider": provider_clean,
            "loki_uid": uid,
            "status_code": resp.status_code,
            "error": (resp.text or "")[:300],
        }
    body = resp.json() or {}
    labels = body.get("data") or []
    return {
        "ok": True,
        "env": env,
        "provider": provider_clean,
        "loki_uid": uid,
        "window": window,
        "label_count": len(labels),
        "labels": labels,
    }


@mcp.tool()
def list_loki_label_values(
    env: str,
    label: str,
    labels: Optional[dict[str, str]] = None,
    since_minutes: int = 0,
    since_hours: int = 0,
    since_days: int = 0,
    start_time: str = "",
    end_time: str = "",
    loki_uid: str = "",
    provider: str = "lvx",
) -> dict[str, Any]:
    """List values of a single Loki label in the given env. **Read-only.**

    Example (lvx): ``list_loki_label_values(env="na3", label="component")``
    returns every component seen in Loki logs during the window.

    Example (mycompany): ``list_loki_label_values(env="au1", label="service_name",
    provider="mycompany")`` scrapes every ``service_name`` (the MyCompany-side
    equivalent of ``component``) seen on that env's mon-* Grafana — this
    is the "list of components" call for mycompany. For ``provider="mycompany"``,
    results are always scoped to ``openapm_product_name="mycompany"`` by
    default; pass ``labels={"openapm_product_name": "other-product"}`` to
    look at a different product instead, or add more matchers via
    ``labels`` to narrow further.
    """
    if not label or not label.strip():
        return {"ok": False, "env": env, "error": "`label` is required."}
    label_clean = label.strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", label_clean):
        return {
            "ok": False,
            "env": env,
            "error": f"`label` {label_clean!r} is not a valid Loki label name.",
        }
    try:
        provider_clean = _check_provider(provider)
        uid = _resolve_loki_uid(env, override=loki_uid, provider=provider_clean)
        effective_labels = _apply_mycompany_defaults(provider_clean, labels)
        stream_query = _build_logql_from_shortcuts(effective_labels, "", "")
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "env": env, "provider": provider, "error": str(exc)}
    start_ns, end_ns, window = _resolve_time_window_ns(
        since_minutes, since_hours, since_days, start_time, end_time
    )
    params: dict[str, Any] = {"start": start_ns, "end": end_ns}
    if stream_query:
        params["query"] = stream_query
    try:
        resp = _grafana_get(
            env,
            _loki_proxy_path(uid, f"/loki/api/v1/label/{label_clean}/values"),
            params=params,
            provider=provider_clean,
        )
    except RuntimeError as exc:
        return {"ok": False, "env": env, "provider": provider_clean, "loki_uid": uid, "error": str(exc)}
    if resp.status_code != 200:
        return {
            "ok": False,
            "env": env,
            "provider": provider_clean,
            "loki_uid": uid,
            "status_code": resp.status_code,
            "error": (resp.text or "")[:300],
        }
    body = resp.json() or {}
    values = body.get("data") or []
    return {
        "ok": True,
        "env": env,
        "provider": provider_clean,
        "loki_uid": uid,
        "label": label_clean,
        "stream_query": stream_query,
        "window": window,
        "value_count": len(values),
        "values": values,
    }


@mcp.tool()
def query_loki(
    env: str,
    query: str = "",
    labels: Optional[dict[str, str]] = None,
    line_filter: str = "",
    line_regex: str = "",
    since_minutes: int = 0,
    since_hours: int = 0,
    since_days: int = 0,
    start_time: str = "",
    end_time: str = "",
    limit: int = 100,
    direction: str = "backward",
    step: str = "",
    loki_uid: str = "",
    provider: str = "lvx",
) -> dict[str, Any]:
    """Run a LogQL ``query_range`` against Loki. **Read-only.**

    Loki is the default (and typically only) log datasource on this
    server. Two ways to specify the query:

      1. **Convenience path (recommended for common cases)** — pass
         ``labels`` + optional ``line_filter`` / ``line_regex``. The
         tool builds the LogQL for you:

         .. code-block:: python

            labels = {"component": "ACD", "host": "10.158.75.219"}
            line_filter = "ERROR"
            # → {component="ACD",host="10.158.75.219"} |= "ERROR"

         Standard MyCompany (``provider="lvx"``) labels seen in Loki:
         ``component``, ``host``, ``image_id``, ``image_name``,
         ``profile``. For MyCompany Grafana (``provider="mycompany"``), pass just
         ``service_name`` (the component equivalent) — the constant
         ``openapm_product_name="mycompany"`` matcher is auto-injected, e.g.
         ``labels={"service_name": "sbc"}`` →
         ``{openapm_product_name="mycompany",service_name="sbc"}``.
         Use ``list_loki_labels`` / ``list_loki_label_values`` to discover
         more dimensions on either provider.

      2. **Freestyle path** — pass ``query`` with any LogQL expression,
         including aggregations and metric queries like
         ``'sum by (host) (rate({component="ACD"} |~ "ERROR" [5m]))'``.
         When ``query`` is non-empty it wins; the shortcut params are
         ignored (with a note in the response).

    Args:
        env: env label (``na3``, ``eu1``, ``stg4``, …).
        query: Full LogQL expression. Takes precedence over the shortcut
            params when both are provided.
        labels: Convenience label matchers ``{label_name: value}``. Each
            entry becomes ``label_name="value"`` inside the stream
            selector. All string values are exact-match; for regex or
            negation, use raw ``query``.
        line_filter: Substring appended as ``|= "..."``. Case-sensitive
            in Loki — use ``line_regex`` with ``(?i)`` for
            case-insensitive.
        line_regex: RE2 pattern appended as ``|~ "..."``.
        since_minutes / since_hours / since_days: Lookback window
            (default 60 minutes). Priority: minutes > hours > days.
        start_time / end_time: Explicit ISO-8601 / RFC3339 window
            (overrides ``since_*``). E.g. ``"2026-07-14T00:00:00Z"``.
        limit: Max entries to return (1..5000). Default 100.
        direction: ``"backward"`` (newest first, default) or ``"forward"``.
        step: Optional step for metric queries (e.g. ``"30s"``, ``"1m"``).
        loki_uid: Optional override for the Loki datasource UID. Empty
            → auto-discover per env (Loki is the default; only override
            when explicitly targeting a non-default Loki instance).
        provider: ``"lvx"`` (MyCompany Grafana, Cisco VPN, default) or
            ``"mycompany"`` (MyCompany Grafana mon-*, Forticlient VPN — call
            ``mycompany_grafana_login`` first).

    Returns ``{ok, env, provider, loki_uid, window, query, query_source,
    result_type, entry_count, rows, statistics, raw}`` where
    ``query_source`` is ``"raw"`` (freestyle) or ``"shortcut"`` (built
    from labels/line filters).
    """
    try:
        provider_clean = _check_provider(provider)
    except ValueError as exc:
        return {"ok": False, "env": env, "error": str(exc)}

    query_clean = (query or "").strip()
    shortcut_query: Optional[str] = None
    if not query_clean:
        try:
            effective_labels = _apply_mycompany_defaults(provider_clean, labels)
            shortcut_query = _build_logql_from_shortcuts(effective_labels, line_filter, line_regex)
        except ValueError as exc:
            return {"ok": False, "env": env, "provider": provider_clean, "error": str(exc)}

    effective_query = query_clean or shortcut_query
    if not effective_query:
        return {
            "ok": False,
            "env": env,
            "provider": provider_clean,
            "error": (
                "Provide either `query` (full LogQL) or the shortcut params "
                "(`labels` — required — plus optional `line_filter` / `line_regex`)."
            ),
        }
    try:
        uid = _resolve_loki_uid(env, override=loki_uid, provider=provider_clean)
    except RuntimeError as exc:
        return {"ok": False, "env": env, "provider": provider_clean, "error": str(exc)}

    start_ns, end_ns, window = _resolve_time_window_ns(
        since_minutes, since_hours, since_days, start_time, end_time
    )

    row_limit = max(1, min(int(limit), 5000))
    direction_clean = direction.strip().lower() if direction else "backward"
    if direction_clean not in ("backward", "forward"):
        direction_clean = "backward"

    params: dict[str, Any] = {
        "query": effective_query,
        "start": start_ns,
        "end": end_ns,
        "limit": row_limit,
        "direction": direction_clean,
    }
    if step and step.strip():
        params["step"] = step.strip()

    started = time.monotonic()
    try:
        resp = _grafana_get(
            env,
            _loki_proxy_path(uid, "/loki/api/v1/query_range"),
            params=params,
            timeout=60,
            provider=provider_clean,
        )
    except RuntimeError as exc:
        return {"ok": False, "env": env, "provider": provider_clean, "loki_uid": uid, "query": effective_query, "error": str(exc)}
    elapsed = round(time.monotonic() - started, 3)

    if resp.status_code != 200:
        return {
            "ok": False,
            "env": env,
            "provider": provider_clean,
            "loki_uid": uid,
            "query": effective_query,
            "status_code": resp.status_code,
            "error": (resp.text or "")[:500],
        }

    body = resp.json() or {}
    data = body.get("data") or {}
    rows = _flatten_loki_result(data)
    return {
        "ok": body.get("status") == "success",
        "env": env,
        "provider": provider_clean,
        "loki_uid": uid,
        "query": effective_query,
        "query_source": "raw" if query_clean else "shortcut",
        "shortcut_ignored": bool(query_clean and (labels or line_filter.strip() or line_regex.strip())),
        "window": window,
        "direction": direction_clean,
        "limit": row_limit,
        "step": params.get("step"),
        "duration_seconds": elapsed,
        "result_type": data.get("resultType"),
        "entry_count": len(rows),
        "rows": rows,
        "statistics": data.get("stats"),
        "raw": data,
    }


@mcp.tool()
def query_loki_instant(
    env: str,
    query: str = "",
    labels: Optional[dict[str, str]] = None,
    line_filter: str = "",
    line_regex: str = "",
    at_time: str = "",
    limit: int = 100,
    direction: str = "backward",
    loki_uid: str = "",
    provider: str = "lvx",
) -> dict[str, Any]:
    """Run an instant Loki query (single point in time). **Read-only.**

    Same convenience/freestyle contract as ``query_loki`` — pass either
    a raw LogQL ``query`` or the ``labels`` / ``line_filter`` / ``line_regex``
    shortcut (for ``provider="mycompany"``, pass just ``service_name`` — the
    constant ``openapm_product_name="mycompany"`` matcher is auto-injected).
    Instant queries are best for metric expressions like
    ``'sum by (host) (rate({component="ACD"} |~ "ERROR" [5m]))'`` when
    you want the current value at ``at_time`` (default: now) rather than
    a range.

    Args:
        provider: ``"lvx"`` (MyCompany Grafana, Cisco VPN, default) or
            ``"mycompany"`` (MyCompany Grafana mon-*, Forticlient VPN — call
            ``mycompany_grafana_login`` first).
    """
    try:
        provider_clean = _check_provider(provider)
    except ValueError as exc:
        return {"ok": False, "env": env, "error": str(exc)}

    query_clean = (query or "").strip()
    shortcut_query: Optional[str] = None
    if not query_clean:
        try:
            effective_labels = _apply_mycompany_defaults(provider_clean, labels)
            shortcut_query = _build_logql_from_shortcuts(effective_labels, line_filter, line_regex)
        except ValueError as exc:
            return {"ok": False, "env": env, "provider": provider_clean, "error": str(exc)}

    effective_query = query_clean or shortcut_query
    if not effective_query:
        return {
            "ok": False,
            "env": env,
            "provider": provider_clean,
            "error": (
                "Provide either `query` (full LogQL) or the shortcut params "
                "(`labels` — required — plus optional `line_filter` / `line_regex`)."
            ),
        }
    try:
        uid = _resolve_loki_uid(env, override=loki_uid, provider=provider_clean)
    except RuntimeError as exc:
        return {"ok": False, "env": env, "provider": provider_clean, "error": str(exc)}

    at_dt = _parse_iso_datetime(at_time) or datetime.now(timezone.utc)
    at_ns = int(at_dt.timestamp() * 1_000_000_000)
    row_limit = max(1, min(int(limit), 5000))
    direction_clean = direction.strip().lower() if direction else "backward"
    if direction_clean not in ("backward", "forward"):
        direction_clean = "backward"

    params: dict[str, Any] = {
        "query": effective_query,
        "time": at_ns,
        "limit": row_limit,
        "direction": direction_clean,
    }
    try:
        resp = _grafana_get(
            env,
            _loki_proxy_path(uid, "/loki/api/v1/query"),
            params=params,
            timeout=60,
            provider=provider_clean,
        )
    except RuntimeError as exc:
        return {"ok": False, "env": env, "provider": provider_clean, "loki_uid": uid, "query": effective_query, "error": str(exc)}
    if resp.status_code != 200:
        return {
            "ok": False,
            "env": env,
            "provider": provider_clean,
            "loki_uid": uid,
            "query": effective_query,
            "status_code": resp.status_code,
            "error": (resp.text or "")[:500],
        }
    body = resp.json() or {}
    data = body.get("data") or {}
    rows = _flatten_loki_result(data)
    return {
        "ok": body.get("status") == "success",
        "env": env,
        "provider": provider_clean,
        "loki_uid": uid,
        "query": effective_query,
        "query_source": "raw" if query_clean else "shortcut",
        "shortcut_ignored": bool(query_clean and (labels or line_filter.strip() or line_regex.strip())),
        "at_time": at_dt.isoformat(),
        "limit": row_limit,
        "direction": direction_clean,
        "result_type": data.get("resultType"),
        "entry_count": len(rows),
        "rows": rows,
        "raw": data,
    }


@mcp.tool()
def grafana_get(
    env: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
    provider: str = "lvx",
) -> dict[str, Any]:
    """Freestyle read-only GET against the Grafana REST API. **Read-only.**

    For anything the specialized tools don't cover — dashboards, folders,
    alert rules, orgs, users, teams, plugins, etc. Only ``GET`` requests
    are allowed. The path must start with ``/api/`` and cannot contain
    ``..`` or absolute URLs — this is a strict API-shape check, not a
    proxy, so it can never accidentally hit an off-Grafana host.

    Args:
        env: env label (``na3``, ``eu1``, ``stg4``, …).
        path: e.g. ``"/api/search"``, ``"/api/dashboards/uid/<uid>"``,
            ``"/api/datasources/proxy/uid/<uid>/loki/api/v1/series"``.
        params: Optional query-string params. Passed through verbatim.
        provider: ``"lvx"`` (MyCompany Grafana, Cisco VPN, default) or
            ``"mycompany"`` (MyCompany Grafana mon-*, Forticlient VPN — call
            ``mycompany_grafana_login`` first).

    Returns ``{ok, env, provider, path, status_code, response, raw_text}``.
    If the response body is JSON it's parsed into ``response``; otherwise
    the raw text is returned under ``raw_text``.
    """
    try:
        provider_clean = _check_provider(provider)
    except ValueError as exc:
        return {"ok": False, "env": env, "error": str(exc)}
    if not path or not path.strip():
        return {"ok": False, "env": env, "provider": provider_clean, "error": "`path` is required."}
    cleaned = path.strip()
    # Explicit ``..`` reject — the regex below allows literal ``.`` in the
    # char class, so a raw ``..`` sequence would sneak through. Belt +
    # suspenders: also refuse leading ``//`` and any control chars.
    if ".." in cleaned:
        return {
            "ok": False,
            "env": env,
            "provider": provider_clean,
            "path": path,
            "error": (
                "path contains `..` — path traversal is refused. The freestyle "
                "tool talks to Grafana's REST API only."
            ),
        }
    if not _ALLOWED_PATH_RE.match(cleaned):
        return {
            "ok": False,
            "env": env,
            "provider": provider_clean,
            "path": path,
            "error": (
                "path must match `^/api/[A-Za-z0-9_-./]+$` — no absolute "
                "URLs, no query strings (use `params`), no path traversal. "
                "The freestyle tool talks to Grafana's REST API only."
            ),
        }
    try:
        resp = _grafana_get(env, cleaned, params=params, timeout=60, provider=provider_clean)
    except RuntimeError as exc:
        return {"ok": False, "env": env, "provider": provider_clean, "path": cleaned, "error": str(exc)}

    content_type = (resp.headers.get("Content-Type") or "").lower()
    body_parsed: Any = None
    raw_text: Optional[str] = None
    if "application/json" in content_type:
        try:
            body_parsed = resp.json()
        except (ValueError, json.JSONDecodeError):
            raw_text = resp.text[:20000]
    else:
        raw_text = resp.text[:20000]

    return {
        "ok": 200 <= resp.status_code < 300,
        "env": env,
        "provider": provider_clean,
        "path": cleaned,
        "params": params or None,
        "status_code": resp.status_code,
        "content_type": content_type,
        "response": body_parsed,
        "raw_text": raw_text,
    }


@mcp.tool()
def grafana_health(env: str, provider: str = "lvx") -> dict[str, Any]:
    """Verify Grafana connectivity and auth for an env. **Read-only.**

    Hits ``/api/health`` (which does not require auth) and then
    ``/api/datasources`` (which does). Returns a compact status payload
    the AI can show the user to confirm the env is reachable and auth
    works before running heavier queries.

    Args:
        env: env label (``na3``, ``eu1``, ``stg4``, …).
        provider: ``"lvx"`` (MyCompany Grafana, Cisco VPN, default; LDAP
            Basic auth) or ``"mycompany"`` (MyCompany Grafana mon-*, Forticlient
            VPN; Okta SSO — call ``mycompany_grafana_login`` first if
            ``auth_ok`` comes back false here).
    """
    try:
        provider_clean = _check_provider(provider)
        base_url = _grafana_base_url(env, provider_clean)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "env": env, "error": str(exc)}

    result: dict[str, Any] = {"env": env, "provider": provider_clean, "base_url": base_url}
    try:
        # require_auth=False: /api/health needs no auth on either provider,
        # and we want a pure reachability signal before judging auth status.
        health = _grafana_get(env, "/api/health", timeout=15, provider=provider_clean, require_auth=False)
        result["health_status_code"] = health.status_code
        try:
            result["health"] = health.json()
        except ValueError:
            result["health_text"] = health.text[:300]
    except RuntimeError as exc:
        return {**result, "ok": False, "error": str(exc)}

    try:
        ds = _grafana_get(env, "/api/datasources", timeout=15, provider=provider_clean)
    except RuntimeError as exc:
        return {**result, "ok": False, "auth_ok": False, "error": str(exc)}

    result["auth_status_code"] = ds.status_code
    result["auth_ok"] = ds.status_code == 200
    if ds.status_code == 200:
        data = ds.json() or []
        result["datasource_count"] = len(data)
        result["datasource_types"] = sorted({(d.get("type") or "") for d in data})
    else:
        result["auth_error"] = (ds.text or "")[:300]

    result["ok"] = result.get("health_status_code") == 200 and result.get("auth_ok", False)
    return result


@mcp.tool()
def mycompany_grafana_login(env: str, mfa_code: str = "") -> dict[str, Any]:
    """Authenticate against MyCompany Grafana (mon-*) for an env via Okta SSO.

    MyCompany Grafana requires the **Forticlient VPN** (as opposed to lvx
    Grafana's Cisco VPN) and Okta login (username + password + MFA)
    instead of LDAP Basic auth. Call this once per env before any
    ``provider="mycompany"`` tool call; the resulting session is cached in
    memory for the lifetime of this container process — never written to
    disk or a host volume, since the cookies are meaningless outside this
    process. A fresh container needs to log in again (MFA included).

    Flow:
      1. Call with no ``mfa_code``. If a session is already live for this
         env (or Okta cookies from a previous different-env login still
         work), returns immediately with no MFA prompt.
      2. Otherwise runs Okta primary auth using ``OKTA_USERNAME`` /
         ``OKTA_PASSWORD``. If Okta requires MFA, returns
         ``needs_mfa: true`` — ask the user for their 6-digit
         authenticator code and call again with ``mfa_code=...``.
      3. On success, completes the Grafana OAuth handshake for this env's
         mon-* host and confirms the session is live.

    If this fails outright (not just needs_mfa), tell the user to confirm
    they're on the Forticlient VPN before retrying.

    Args:
        env: env label — one of the ``MyCompany_GRAFANA_ENV_URLS`` keys
            (``tst2``, ``tst3``, ``stg4``, ``na4``, ``na3``, ``na6``,
            ``na5``, ``eu1``, ``uk2``, ``uk3``, ``au1``, ``perf1``).
        mfa_code: 6-digit TOTP code, only needed on the follow-up call
            after a ``needs_mfa: true`` response.
    """
    try:
        base_url = _mycompany_base_url(env)
    except ValueError as exc:
        return {"ok": False, "env": env, "error": str(exc)}

    if _mycompany_session_is_valid(base_url):
        return {"ok": True, "env": env, "base_url": base_url, "already_authenticated": True}

    mfa_code_clean = (mfa_code or "").strip()

    # Silent path: existing Okta cookies (from a different env's login)
    # might be enough to complete SSO here with no MFA prompt.
    if not mfa_code_clean and _complete_grafana_sso(base_url):
        return {"ok": True, "env": env, "base_url": base_url, "authenticated": True}

    if mfa_code_clean:
        with _MyCompany_LOCK:
            pending = dict(_mycompany_pending_mfa)
        if (
            not pending
            or not pending.get("state_token")
            or not pending.get("factor_id")
            or pending.get("env") != env
        ):
            return {
                "ok": False,
                "env": env,
                "error": "No pending MFA challenge for this env — call mycompany_grafana_login(env=...) "
                         "without mfa_code first to start a fresh login.",
            }
        try:
            verify_data = _okta_verify_mfa(pending["state_token"], pending["factor_id"], mfa_code_clean)
        except requests.exceptions.RequestException as exc:
            return {"ok": False, "env": env, "error": f"{exc.__class__.__name__}: {_VPN_HINTS['mycompany']}"}
        if verify_data.get("status") != "SUCCESS":
            return {
                "ok": False, "env": env,
                "error": f"MFA verification failed: {verify_data.get('status', 'unknown')}",
            }
        session_token = verify_data.get("sessionToken")
        with _MyCompany_LOCK:
            _mycompany_pending_mfa.clear()
        if not session_token or not _finish_okta_session_exchange(session_token, base_url):
            return {
                "ok": False, "env": env,
                "error": f"MFA succeeded but the Grafana session exchange failed. {_VPN_HINTS['mycompany']}",
            }
        return {"ok": True, "env": env, "base_url": base_url, "authenticated": True}

    # Fresh primary auth.
    username = os.environ.get("OKTA_USERNAME")
    password = os.environ.get("OKTA_PASSWORD")
    if not username or not password:
        return {
            "ok": False, "env": env,
            "error": "OKTA_USERNAME / OKTA_PASSWORD are not set. Provide them via "
                     "your AI host MCP configuration.",
        }
    try:
        auth_data = _okta_primary_auth(username, password)
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "env": env, "error": f"{exc.__class__.__name__}: {_VPN_HINTS['mycompany']}"}

    status = auth_data.get("status")
    if status == "SUCCESS":
        session_token = auth_data.get("sessionToken")
        if not session_token or not _finish_okta_session_exchange(session_token, base_url):
            return {
                "ok": False, "env": env,
                "error": f"Okta login succeeded but the Grafana session exchange failed. {_VPN_HINTS['mycompany']}",
            }
        return {"ok": True, "env": env, "base_url": base_url, "authenticated": True}

    if status == "MFA_REQUIRED":
        state_token, factor_id = _okta_extract_totp_factor(auth_data)
        if not factor_id:
            return {"ok": False, "env": env, "error": "Okta requires MFA but no TOTP factor was found on the account."}
        with _MyCompany_LOCK:
            _mycompany_pending_mfa.clear()
            _mycompany_pending_mfa.update({"state_token": state_token, "factor_id": factor_id, "env": env})
        return {
            "ok": False, "env": env, "needs_mfa": True,
            "message": "MFA required — ask the user for their 6-digit authenticator code, "
                       "then call mycompany_grafana_login(env=..., mfa_code='123456').",
        }

    if auth_data.get("_http_status") == 401:
        return {
            "ok": False, "env": env,
            "error": f"Okta authentication failed (invalid username/password, or password "
                     f"auth may be disabled by org policy in favor of a passwordless factor). "
                     f"{_VPN_HINTS['mycompany']}",
        }

    return {"ok": False, "env": env, "error": f"Unexpected Okta auth status: {status!r}"}


@mcp.tool()
def discover_service_map(
    env: str,
    provider: str = "mycompany",
    since_hours: int = 6,
    sample_limit: int = 200,
    max_workers: int = 24,
    service_names: Optional[list[str]] = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Build (or refresh) a service_name → component map for an env. **Read-only.**

    Concurrently samples recent log lines for every ``service_name`` label
    value in the env (or a caller-supplied subset), discovering:
      - the ``openapm_product_name`` value(s) it runs under
      - embedded ``"service":"..."`` JSON fields inside the log body — some
        service_names, like MyCompany's ``apps-outbound``, host several distinct
        sub-components this way (e.g. ``OPSS``, ``OPA``) that never appear
        as a queryable label
      - namespace / pod / container name bases (trailing k8s hash stripped)

    The result is cached in memory (keyed by ``provider:env``) so
    ``search_service_map`` can answer free-text component lookups instantly
    afterward, without re-scanning Loki. Call this once per env before using
    ``search_service_map``; re-run with ``force_refresh=True`` to rebuild.

    Best-effort: some service_names emit plain-text (non-JSON) logs or didn't
    log during the sample window, so their entries may be sparse — widen
    ``since_hours`` / ``sample_limit`` for more completeness at the cost of
    speed. Per-service_name failures are collected in ``errors`` rather than
    aborting the whole scan.

    Args:
        env: env label (``na3``, ``eu1``, ``stg4``, …).
        provider: ``"lvx"`` or ``"mycompany"`` (default — this is what "mycompany" /
            "mon" Grafana usually means).
        since_hours: lookback window per service_name sample. Default 6.
        sample_limit: max log lines sampled per service_name (1..1000).
        max_workers: thread-pool size for the concurrent scan (1..32, default
            24). Kept <= the mycompany session's connection pool size
            (`_MyCompany_POOL_SIZE`) so extra threads don't just queue for a
            pooled connection instead of running in parallel.
        service_names: optional explicit subset to scan instead of every
            service_name in the env — much faster for a targeted refresh.
        force_refresh: bypass the cache and rebuild from scratch. Ignored
            (always rescans) when ``service_names`` is given.
    """
    try:
        provider_clean = _check_provider(provider)
        uid = _resolve_loki_uid(env, provider=provider_clean)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "env": env, "error": str(exc)}

    cache_key = f"{provider_clean}:{(env or '').strip().lower()}"
    if not force_refresh and not service_names:
        with _SERVICE_MAP_LOCK:
            cached = _SERVICE_MAP_CACHE.get(cache_key)
        if cached:
            return {
                "ok": True, "env": env, "provider": provider_clean, "cached": True,
                "built_at": cached.get("built_at"), **cached.get("summary", {}),
            }

    # mycompany Grafana needs a live session before fanning out threads — check
    # once up front instead of once per thread (_ensure_mycompany_authenticated
    # hits /api/user every call).
    if provider_clean == "mycompany":
        try:
            base_url = _mycompany_base_url(env)
            _ensure_mycompany_authenticated(base_url)
        except (RuntimeError, ValueError) as exc:
            return {"ok": False, "env": env, "provider": provider_clean, "error": str(exc)}

    start_ns, end_ns, window = _resolve_time_window_ns(0, since_hours, 0, "", "")
    match_all_query = _MATCH_ALL_SELECTOR.get(provider_clean, '{job=~".+"}')

    if service_names:
        names = sorted({n.strip() for n in service_names if n and n.strip()})
    else:
        try:
            names = _fetch_label_values_raw(
                env, uid, "service_name", match_all_query, start_ns, end_ns, provider_clean
            )
        except RuntimeError as exc:
            return {"ok": False, "env": env, "provider": provider_clean, "error": str(exc)}

    if not names:
        return {
            "ok": False, "env": env, "provider": provider_clean,
            "error": "No service_name values found in this window.",
        }

    row_limit = max(1, min(int(sample_limit), 1000))

    def _scan_one(name: str) -> dict[str, Any]:
        query = f'{{service_name="{_escape_loki_string(name)}"}}'
        params: dict[str, Any] = {
            "query": query, "start": start_ns, "end": end_ns,
            "limit": row_limit, "direction": "backward",
        }
        try:
            resp = _grafana_get(
                env, _loki_proxy_path(uid, "/loki/api/v1/query_range"),
                params=params, timeout=30, provider=provider_clean, require_auth=False,
            )
        except RuntimeError as exc:
            return {"error": str(exc)}
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"}
        body = resp.json() or {}
        rows = _flatten_loki_result(body.get("data") or {})
        return _extract_component_signals(rows)

    service_map: dict[str, Any] = {}
    errors: dict[str, str] = {}
    workers = max(1, min(int(max_workers), _MyCompany_POOL_SIZE))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_scan_one, name): name for name in names}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                signals = future.result()
            except Exception as exc:  # noqa: BLE001 — collect per-service failures, don't abort the scan
                errors[name] = str(exc)
                continue
            if "error" in signals:
                errors[name] = signals["error"]
            else:
                service_map[name] = signals

    with _SERVICE_MAP_LOCK:
        existing = _SERVICE_MAP_CACHE.get(cache_key, {"map": {}, "summary": {}})
        existing["map"].update(service_map)
        total_cached = len(existing["map"])
        existing["summary"] = {
            "window": window,
            "service_name_count": len(names),
            "scanned_ok": len(service_map),
            "scan_errors": len(errors),
            "total_cached_service_names": total_cached,
        }
        existing["built_at"] = datetime.now(timezone.utc).isoformat()
        _SERVICE_MAP_CACHE[cache_key] = existing
        summary = dict(existing["summary"])

    return {
        "ok": True, "env": env, "provider": provider_clean, "cached": False,
        **summary, "errors": errors or None,
    }


@mcp.tool()
def search_service_map(
    env: str,
    keyword: str,
    provider: str = "mycompany",
) -> dict[str, Any]:
    """Resolve a free-text component name to concrete Loki filter criteria. **Read-only.**

    Reads the in-memory cache built by ``discover_service_map`` — call that
    first (once per env) or this returns an error telling you to. Never
    queries Loki itself; this is a pure cache lookup, so it's instant.

    Matches ``keyword`` (case-insensitive substring) against every discovered
    signal for each ``service_name``: the service_name itself,
    ``openapm_product_name``, embedded ``"service":"..."`` JSON values,
    namespaces, and pod/container name bases (``matches``). Also checks
    ``keyword`` against whole product names via a reverse index, returning
    every ``service_name`` under a matching product (``product_matches``) —
    e.g. keyword ``"QM"`` returns every service_name whose
    ``openapm_product_name`` is ``"QM"``, not just ones with "QM" in the
    name. For each hit, returns which field(s) matched plus ready-to-use
    filter criteria — typically ``service_name`` and, on MyCompany Grafana,
    ``openapm_product_name`` (defaults to ``"mycompany"`` if unset, but most
    non-mycompany components need it set explicitly, e.g. ``"shared_eks01"``
    for MyCompany's outbound stack) — that plug straight into
    ``query_loki(labels={...})`` or a raw LogQL stream selector.

    Args:
        env: env label (``na3``, ``eu1``, ``stg4``, …).
        keyword: free-text component fragment, e.g. ``"outbound"``,
            ``"OPSS"``, ``"sbc"``.
        provider: ``"lvx"`` or ``"mycompany"`` (default).
    """
    try:
        provider_clean = _check_provider(provider)
    except ValueError as exc:
        return {"ok": False, "env": env, "error": str(exc)}
    keyword_clean = (keyword or "").strip().lower()
    if not keyword_clean:
        return {"ok": False, "env": env, "provider": provider_clean, "error": "`keyword` is required."}

    cache_key = f"{provider_clean}:{(env or '').strip().lower()}"
    with _SERVICE_MAP_LOCK:
        cached = _SERVICE_MAP_CACHE.get(cache_key)
    if not cached or not cached.get("map"):
        return {
            "ok": False, "env": env, "provider": provider_clean,
            "error": "No component map cached for this env/provider yet — call "
                     "discover_service_map(env=..., provider=...) first.",
        }

    matches: list[dict[str, Any]] = []
    for service_name, signals in cached["map"].items():
        hit_fields: list[str] = []
        if keyword_clean in service_name.lower():
            hit_fields.append("service_name")
        for field in ("openapm_product_names", "embedded_services", "namespaces", "pod_bases", "container_bases"):
            values = signals.get(field) or []
            if any(keyword_clean in str(v).lower() for v in values):
                hit_fields.append(field)
        if not hit_fields:
            continue
        product_names = signals.get("openapm_product_names") or []
        suggested_labels: dict[str, str] = {"service_name": service_name}
        if provider_clean == "mycompany" and len(product_names) == 1:
            suggested_labels["openapm_product_name"] = product_names[0]
        matches.append({
            "service_name": service_name,
            "matched_fields": hit_fields,
            "openapm_product_names": product_names,
            "embedded_services": signals.get("embedded_services") or [],
            "suggested_labels": suggested_labels,
        })

    matches.sort(key=lambda m: m["service_name"])

    # Reverse index: does `keyword` match a whole openapm_product_name
    # rather than (or in addition to) an individual service_name? A user
    # asking "show me everything in QM" wants the full product's
    # service_name list, not just the ones where "QM" happens to also
    # show up in an embedded/namespace/pod field.
    product_to_services: dict[str, list[str]] = {}
    for service_name, signals in cached["map"].items():
        for product_name in signals.get("openapm_product_names") or []:
            product_to_services.setdefault(product_name, []).append(service_name)
    product_matches = [
        {
            "openapm_product_name": product_name,
            "service_names": sorted(service_list),
            "suggested_labels": {"openapm_product_name": product_name},
        }
        for product_name, service_list in product_to_services.items()
        if keyword_clean in product_name.lower()
    ]
    product_matches.sort(key=lambda m: m["openapm_product_name"])

    return {
        "ok": True,
        "env": env,
        "provider": provider_clean,
        "keyword": keyword,
        "built_at": cached.get("built_at"),
        "match_count": len(matches),
        "matches": matches,
        "product_match_count": len(product_matches),
        "product_matches": product_matches,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
