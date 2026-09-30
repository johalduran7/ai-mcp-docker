"""Read-only OpenSearch MCP server for MyCompany environments.

Companion to the AWS MCP server. Given an ``env`` (e.g. ``stg4``) and an AWS
``profile_name`` (substring accepted, e.g. ``stg4`` → ``lv-stg4-dr``), this
server:

  1. Uses cached in-container knowledge of the OpenSearch domains for that
     env if present. On cache miss it lists the domains via
     ``opensearch.list_domain_names`` + ``describe_domain`` and caches
     ``{domain: endpoint}``.
  2. Fetches HTTP Basic credentials for the target domain from AWS Secrets
     Manager. The secret name is ``<env>/devops/databases`` and the JSON
     payload contains keys of the form:

       * ``<domain_no_env>_opensearch_username``
       * ``<domain_no_env>_opensearch_password``

     where ``<domain_no_env>`` is the domain name with a trailing ``-<env>``
     stripped (if present) and dashes converted to underscores. So
     ``qm-logger-stg4`` → ``qm_logger_opensearch_username`` /
     ``qm_logger_opensearch_password``.
  3. Talks to the OpenSearch cluster over HTTPS using ``requests`` with
     HTTP Basic auth, and returns parsed JSON / text.

**THIS SERVER IS STRICTLY READ-ONLY.**

Every OpenSearch call is a ``GET``. There are no write, update, delete,
snapshot-create, reindex, or settings-mutation tools. The freestyle
``opensearch_get`` tool refuses any method other than GET and any path
that is not on the read-only allow-list.

Credential model (mirrors lvx-aws)
----------------------------------

* Base profiles read from ``~/.aws/config`` (mounted from the host).
* Session profiles expected in ``~/.aws/credentials`` (session profile
  naming: ``<base>-session``).
* Awsume is installed inside the container and reuses the host
  ``~/.awsume`` cache. On any expired-token error the tool transparently
  runs ``awsume <base> -o <base>-session`` and retries the call.
* If awsume actually needs an MFA code (not just a cached refresh), the
  tool returns ``needs_mfa: true`` and the AI host must prompt the user
  for the code and re-invoke the same tool with ``mfa_code=<code>``.

The user is never asked to run anything in their terminal. The container
handles refresh internally.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lvx-opensearch")
from version_notifier import install as _install_version_notifier
_install_version_notifier(mcp, "lvx-opensearch")


# ── AWS profile / session helpers (mirrors lvx-aws) ─────────────────────────


def _aws_config_path() -> Path:
    configured = os.environ.get("AWS_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".aws" / "config"


def _read_aws_profiles() -> list[str]:
    config_path = _aws_config_path()
    parser = configparser.RawConfigParser()
    if not config_path.exists():
        return []
    parser.read(config_path)

    profiles: list[str] = []
    for section in parser.sections():
        if section == "default":
            profiles.append("default")
            continue
        if section.startswith("profile "):
            profiles.append(section.replace("profile ", "", 1))
    return sorted(set(profiles))


def _resolve_profile(profile_query: str) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a possibly-partial profile name against ``~/.aws/config``.

    Behavior:
      * Exact match → return that profile.
      * Single case-insensitive substring match → return that profile.
      * Multiple matches → return an ``ambiguous_profile`` payload with the
        list of ``matches`` so the AI host can ask which one to use.
      * Zero matches → return a ``profile_not_found`` payload.
    """
    profiles = _read_aws_profiles()
    query = (profile_query or "").strip()

    if not query:
        return None, {
            "ok": False,
            "profile_query": profile_query,
            "error": "profile_name is required.",
            "available_profiles": profiles,
        }

    if query in profiles:
        return query, None

    query_lower = query.lower()
    matches = [p for p in profiles if query_lower in p.lower()]

    if len(matches) == 1:
        return matches[0], None

    if len(matches) > 1:
        return None, {
            "ok": False,
            "profile_query": profile_query,
            "ambiguous_profile": True,
            "matches": matches,
            "instruction": (
                f"Multiple AWS profiles match '{profile_query}': {', '.join(matches)}. "
                "Ask the user which one to use, then call the same tool again with "
                "the full profile_name."
            ),
        }

    return None, {
        "ok": False,
        "profile_query": profile_query,
        "profile_not_found": True,
        "available_profiles": profiles,
        "instruction": (
            f"No AWS profile matches '{profile_query}'. "
            f"Available profiles: {', '.join(profiles) if profiles else '(none)'}. "
            "Ask the user to pick one of the available profiles and call the same "
            "tool again."
        ),
    }


def _resolve_session_profile(profile_name: str, session_profile: str | None) -> str:
    if session_profile:
        return session_profile
    if profile_name == "default":
        return "default"
    return f"{profile_name}-session"


def _region_from_config(profile_name: str) -> str | None:
    config_path = _aws_config_path()
    parser = configparser.RawConfigParser()
    if not config_path.exists():
        return None
    parser.read(config_path)

    section = "default" if profile_name == "default" else f"profile {profile_name}"
    if parser.has_option(section, "region"):
        return parser.get(section, "region")
    return None


def _session_for(profile_name: str, session_profile: str | None, region: str | None) -> boto3.Session:
    effective_profile = _resolve_session_profile(profile_name, session_profile)
    effective_region = (
        region
        or _region_from_config(profile_name)
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    return boto3.Session(profile_name=effective_profile, region_name=effective_region)


# ── awsume refresh (mirrors lvx-aws) ─────────────────────────────────────────


def _session_expired(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = (
        "expiredtoken",
        "token has expired",
        "security token included in the request is expired",
        "expired token",
        # A cached session whose window has fully passed comes back as
        # ``InvalidClientTokenId: The security token included in the
        # request is invalid`` instead of ExpiredToken. Same underlying
        # cause (stale awsume session), same fix (re-run awsume).
        "invalidclienttokenid",
        "the security token included in the request is invalid",
        "unable to locate credentials",
        "the config profile",
        "could not be found",
    )
    return any(marker in message for marker in markers)


def _needs_mfa(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "enter mfa",
        "mfa token",
        "multi-factor",
        "mfa code",
    )
    return any(marker in lowered for marker in markers)


# Serialize awsume invocations inside this process. awsume writes the
# shared credentials file non-atomically; two parallel awsume processes
# racing on the same section produce duplicate keys and configparser
# then refuses to read the file at all. This lock only covers this
# container — the lvx-aws container mounts the same file and its own
# lock covers writes on that side. `_dedupe_credentials_file` runs at
# the top of every `_run_awsume` here so cross-container corruption
# still self-heals on the next refresh.
_AWSUME_LOCK = threading.Lock()


def _credentials_file_path() -> Path:
    configured = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".aws" / "credentials"


def _dedupe_credentials_file() -> dict[str, Any]:
    """Best-effort repair of the shared credentials file.

    Rewrites the file with duplicate keys per section collapsed (last
    value wins — that's what the most-recent awsume writer intended
    anyway). No-ops when the file parses cleanly, so it's safe to call
    on every awsume invocation.
    """
    path = _credentials_file_path()
    if not path.exists():
        return {"repaired": False, "reason": "credentials file does not exist."}
    parser = configparser.RawConfigParser()
    try:
        parser.read(path)
        return {"repaired": False, "reason": "no duplicate keys found."}
    except configparser.DuplicateOptionError:
        pass
    except configparser.Error as exc:
        return {"repaired": False, "error": f"parse failed: {exc}"}

    try:
        raw = path.read_text()
    except OSError as exc:
        return {"repaired": False, "error": f"read failed: {exc}"}

    sections: dict[str, dict[str, str]] = {}
    section_order: list[str] = []
    current: str | None = None
    duplicates_found = 0
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            if current and current not in sections:
                sections[current] = {}
                section_order.append(current)
            continue
        if current is None or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in sections[current]:
            duplicates_found += 1
        sections[current][key] = value.strip()

    out_lines: list[str] = []
    for name in section_order:
        out_lines.append(f"[{name}]")
        for k, v in sections[name].items():
            out_lines.append(f"{k} = {v}")
        out_lines.append("")
    tmp = path.with_suffix(path.suffix + ".mcp_tmp")
    try:
        tmp.write_text("\n".join(out_lines))
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError as exc:
        return {"repaired": False, "error": f"write failed: {exc}"}
    return {
        "repaired": True,
        "duplicates_collapsed": duplicates_found,
        "sections": len(section_order),
    }


# Sentinel: unset = not yet fetched; "" = fetch failed (negative-cached).
_HOST_TZ_CACHE: str | None = None
_HOST_TZ_LOCK = threading.Lock()


def _fetch_host_tz() -> str | None:
    """Return the caller's IANA TZ via ip-api.com. Cached process-wide.

    awsume writes the MFA cache ``Expiration`` as a naive local-time string,
    so the container must interpret it in the host user's TZ or it will
    falsely conclude the cache expired and re-prompt for MFA. Returns
    ``None`` on any failure — caller then leaves TZ unset.
    """
    global _HOST_TZ_CACHE
    if _HOST_TZ_CACHE is not None:
        return _HOST_TZ_CACHE or None
    with _HOST_TZ_LOCK:
        if _HOST_TZ_CACHE is not None:
            return _HOST_TZ_CACHE or None
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://ip-api.com/line/?fields=timezone",
                headers={"User-Agent": "lvx-opensearch"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                tz = resp.read().decode().strip()
            if "/" in tz:
                _HOST_TZ_CACHE = tz
                return tz
        except Exception:  # noqa: BLE001
            pass
        _HOST_TZ_CACHE = ""
        return None


def _run_awsume(
    profile_name: str,
    session_profile: str,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Run awsume for a profile. If mfa_code is empty, rely on cached MFA session.

    Serialized via ``_AWSUME_LOCK`` and preceded by a best-effort
    ``_dedupe_credentials_file`` pass so a file corrupted by a parallel
    awsume writer (in this container or the sibling lvx-aws container)
    self-heals on the next invocation.
    """
    awsume_bin = os.environ.get("AWSUME_BIN", "awsume")

    command = [awsume_bin, profile_name, "-o", session_profile]
    env = os.environ.copy()
    env["HOME"] = env.get("HOME", str(Path.home()))
    host_tz = _fetch_host_tz()
    if host_tz:
        env["TZ"] = host_tz

    stdin_input = f"{mfa_code.strip()}\n" if mfa_code.strip() else ""

    with _AWSUME_LOCK:
        repair = _dedupe_credentials_file()
        result = subprocess.run(
            command,
            input=stdin_input,
            text=True,
            capture_output=True,
            env=env,
            timeout=180,
            check=False,
        )

    combined_output = f"{result.stdout}\n{result.stderr}".strip()

    if result.returncode != 0:
        needs_mfa = _needs_mfa(combined_output) or (
            not mfa_code.strip() and "eof" in combined_output.lower()
        )
        return {
            "ok": False,
            "profile_name": profile_name,
            "session_profile": session_profile,
            "needs_mfa": needs_mfa,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "credentials_repair": repair,
            "error": (result.stderr or result.stdout or "awsume failed").strip(),
        }

    return {
        "ok": True,
        "profile_name": profile_name,
        "session_profile": session_profile,
        "needs_mfa": False,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "credentials_repair": repair,
    }


def _try_refresh_and_call(
    profile_name: str,
    session_profile: str,
    mfa_code: str,
    original_exc: Exception,
    caller,
) -> dict[str, Any]:
    """Attempt awsume refresh and re-run the caller.

    Mirrors the ``_try_refresh_and_call`` helper in lvx-aws: awsume's exit
    code is unreliable, so the authoritative success signal is whether the
    retried call succeeds. Only prompts for MFA when awsume actually asked
    for one AND the retry still fails with an expired-token error.
    """
    refresh = _run_awsume(
        profile_name=profile_name,
        session_profile=session_profile,
        mfa_code=mfa_code,
    )

    try:
        result = caller()
    except Exception as retry_exc:  # noqa: BLE001
        retry_expired = _session_expired(retry_exc)
        payload: dict[str, Any] = {
            "ok": False,
            "profile_name": profile_name,
            "session_profile": session_profile,
            "error": str(original_exc),
            "retry_error": str(retry_exc),
            "session_expired": True,
        }
        if refresh.get("error"):
            payload["refresh_error"] = refresh["error"]
        if refresh.get("stdout"):
            payload["refresh_output"] = refresh["stdout"]

        if mfa_code.strip():
            payload["needs_mfa"] = False
            payload["mfa_attempt_failed"] = True
            payload["instruction"] = (
                f"Awsume refresh failed for profile '{profile_name}' even after an MFA "
                "code was supplied. Do not ask the user to run anything in their "
                "terminal. Report the errors above."
            )
            return payload

        if refresh.get("needs_mfa") and retry_expired:
            payload["needs_mfa"] = True
            payload["instruction"] = (
                f"Prompt the user for the current MFA code for profile '{profile_name}', "
                "then call this same tool again with mfa_code=<code>. "
                "Do NOT ask the user to run awsume in their terminal — the refresh "
                "is handled internally."
            )
            return payload

        payload["needs_mfa"] = False
        payload["instruction"] = (
            f"Awsume refresh for profile '{profile_name}' did not restore working "
            "credentials, but this is NOT an MFA issue. Do NOT ask the user for an "
            "MFA code, and do NOT tell them to run awsume or any other command in "
            "their terminal. Just report the errors above so the user can investigate."
        )
        return payload

    if isinstance(result, dict):
        result["refreshed_with_awsume"] = True
        if refresh.get("stdout"):
            result["refresh_output"] = refresh["stdout"]
    return result


def _run_with_refresh(
    profile_name: str,
    session_profile: str,
    region: str,
    auto_refresh_on_expired: bool,
    mfa_code: str,
    caller,
) -> dict[str, Any]:
    """Standard lvx-opensearch tool wrapper: call → on expired, refresh & retry."""
    effective_session = _resolve_session_profile(profile_name, session_profile or None)
    try:
        return caller()
    except Exception as exc:  # noqa: BLE001
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=effective_session,
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=caller,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "session_profile": effective_session,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "session_profile": effective_session,
            "error": str(exc),
        }


# ── Env / domain / secret helpers ────────────────────────────────────────────

# Container-lifetime cache. Rebuilt on cold start; goes away on container
# restart, which is fine — a stale endpoint value would just cause the next
# HTTP call to fail and the user can re-list.
#
# Structure:
#   _DOMAIN_CACHE[(profile_name, env)] = {
#       "<domain-name>": {
#           "endpoint": "search-...amazonaws.com",
#           "engine_version": "OpenSearch_2.11",
#           "status": "Active" | "Processing" | ...,
#       },
#       ...
#   }
_DOMAIN_CACHE: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
_DOMAIN_CACHE_LOCK = threading.Lock()

# Secret payloads are also cached per (profile, env). The secret contains
# credentials for every domain in the env, so one Secrets Manager call
# feeds every subsequent tool invocation in the container's lifetime.
_SECRET_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_SECRET_CACHE_LOCK = threading.Lock()

_ENV_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,15}$")
_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9\-]{2,27}$")
# MyCompany client IDs are numeric (2–10 digits). Used by the client-oriented
# convenience tools (resolve_client, check_writable_indices, …).
_CLIENT_ID_RE = re.compile(r"^\d{2,10}$")

# MyCompany naming conventions on the QM domains:
#   index alias   = ``<client_id>_qm``           (e.g. ``100262_qm``)
#   index pattern = ``<client_id>_qm-*``         (e.g. ``100262_qm-000001``,
#                                                 ``100262_qm-000002``, …)
# One client can have many indices tied to a single alias. The write-index
# is the one currently receiving new docs (``is_write_index: true`` on the
# alias binding).
_QM_INDEX_SUFFIX = "_qm"

# Freestyle READONLY GET tool path safety. Must be a rooted path that
# maps to a well-known read-only OpenSearch endpoint. Anything else is
# refused — no traversal, no query mutations.
_READONLY_PATH_PREFIXES: tuple[str, ...] = (
    "/_cat/",
    "/_cluster/health",
    "/_cluster/state",
    "/_cluster/stats",
    "/_cluster/settings",
    "/_nodes",
    "/_snapshot",
    "/_alias",
    "/_aliases",
    "/_mapping",
    "/_settings",
    "/_search",
    "/_count",
    "/_stats",
    "/_tasks",
    "/_ilm",
    "/_ism",
    "/_plugins",
    "/_opendistro",
    # Template + data-stream + pipeline endpoints. All are read-only via GET;
    # write verbs (PUT/POST/DELETE) never enter this server. Needed for
    # comparing / auditing / exporting template shapes across envs
    # (index templates, component templates, legacy templates, data streams,
    # ingest pipelines, and stored scripts).
    "/_index_template",
    "/_component_template",
    "/_template",
    "/_data_stream",
    "/_ingest/pipeline",
    "/_scripts",
)
# Also allow any per-index read-only path: /<index>/_search, /<index>/_count,
# /<index>/_mapping, /<index>/_settings, /<index>/_alias, /<index>/_stats,
# /<index>/_shard_stores, /<index>/_segments, /<index>/_doc/<id>.
_READONLY_INDEX_PATH_RE = re.compile(
    r"^/[a-zA-Z0-9_.\-*,]+/"
    r"(?:_search|_count|_mapping|_settings|_alias|_aliases|_stats"
    r"|_shard_stores|_segments|_doc/[A-Za-z0-9_.\-]+)"
    r"(?:/.*)?$"
)


def _validate_env(env: str) -> str:
    env_clean = (env or "").strip().lower()
    if not env_clean or not _ENV_RE.match(env_clean):
        raise ValueError(
            f"env '{env}' is not a valid MyCompany env label "
            "(lowercase alphanumeric + hyphens, up to 16 chars, e.g. 'stg4', 'na3')."
        )
    return env_clean


def _validate_domain(domain: str) -> str:
    domain_clean = (domain or "").strip().lower()
    if not domain_clean or not _DOMAIN_RE.match(domain_clean):
        raise ValueError(
            f"domain '{domain}' is not a valid OpenSearch domain name "
            "(lowercase alphanumeric + hyphens, 3–28 chars, e.g. 'qm-logger-stg4')."
        )
    return domain_clean


def _validate_client_id(client_id: str) -> str:
    cid = (client_id or "").strip()
    if not cid or not _CLIENT_ID_RE.match(cid):
        raise ValueError(
            f"client_id '{client_id}' is not a valid MyCompany client id "
            "(numeric, 2–10 digits, e.g. '106105', '100262')."
        )
    return cid


def _client_index_pattern(client_id: str) -> str:
    """``106105`` → ``106105_qm-*`` — MyCompany QM index wildcard for a client."""
    return f"{client_id}{_QM_INDEX_SUFFIX}-*"


def _client_alias(client_id: str) -> str:
    """``106105`` → ``106105_qm`` — MyCompany QM alias for a client."""
    return f"{client_id}{_QM_INDEX_SUFFIX}"


def _resolve_index_target(
    index_pattern: str,
    client_id: str,
    default_pattern: str = "*",
) -> tuple[str, str | None]:
    """Return the effective index pattern.

    Rules:
      * If ``client_id`` is provided, it overrides ``index_pattern`` with
        ``<client_id>_qm-*`` (the MyCompany QM naming convention).
      * Else use ``index_pattern`` verbatim (fallback to ``default_pattern``).

    Returns ``(effective_pattern, resolved_client_id_or_None)``.
    """
    cid = (client_id or "").strip()
    if cid:
        cid = _validate_client_id(cid)
        return _client_index_pattern(cid), cid
    pattern = (index_pattern or default_pattern).strip() or default_pattern
    return pattern, None


def _resolve_alias_target(alias: str, client_id: str) -> tuple[str, str | None]:
    """Return the effective alias name.

    Rules:
      * If ``client_id`` is provided, it overrides ``alias`` with
        ``<client_id>_qm``.
      * Else use ``alias`` verbatim (required if client_id is empty).

    Returns ``(effective_alias, resolved_client_id_or_None)``.
    """
    cid = (client_id or "").strip()
    if cid:
        cid = _validate_client_id(cid)
        return _client_alias(cid), cid
    aliased = (alias or "").strip()
    if not aliased:
        raise ValueError(
            "alias (or client_id) is required. Pass client_id='<numeric>' to "
            "auto-derive the MyCompany QM alias '<client_id>_qm', or provide alias "
            "explicitly."
        )
    return aliased, None


def _secret_key_prefix(domain: str, env: str) -> str:
    """``qm-logger-stg4`` + env ``stg4`` → ``qm_logger``.

    Strips a trailing ``-<env>`` from the domain (if present) and converts
    remaining dashes to underscores. This is what the Secrets Manager JSON
    payload uses as the key prefix for username/password.
    """
    core = domain
    suffix = f"-{env}"
    if core.endswith(suffix):
        core = core[: -len(suffix)]
    return core.replace("-", "_")


def _fetch_domains(session: boto3.Session) -> dict[str, dict[str, Any]]:
    """List every OpenSearch domain in the account and describe each one.

    Returns ``{domain_name: {endpoint, engine_version, status}}``.
    """
    client = session.client("opensearch")
    names_resp = client.list_domain_names(EngineType="OpenSearch")
    names: list[str] = [
        d.get("DomainName") for d in names_resp.get("DomainNames", []) if d.get("DomainName")
    ]

    # Also include Elasticsearch-engine domains that live in the same
    # account under the OpenSearch service (some legacy envs still run
    # ``Elasticsearch_7.x``).
    try:
        legacy = client.list_domain_names(EngineType="Elasticsearch")
        for d in legacy.get("DomainNames", []):
            name = d.get("DomainName")
            if name and name not in names:
                names.append(name)
    except (ClientError, BotoCoreError):
        pass

    out: dict[str, dict[str, Any]] = {}
    # AWS caps describe_domains at 5 names per call.
    for i in range(0, len(names), 5):
        batch = names[i : i + 5]
        resp = client.describe_domains(DomainNames=batch)
        for status in resp.get("DomainStatusList", []):
            name = status.get("DomainName")
            if not name:
                continue
            endpoint = status.get("Endpoint") or ""
            # VPC domains keep their endpoint under ``Endpoints`` as a dict.
            if not endpoint:
                endpoints = status.get("Endpoints") or {}
                endpoint = endpoints.get("vpc") or endpoints.get("vpcv2") or next(
                    iter(endpoints.values()), ""
                )
            out[name] = {
                "endpoint": endpoint,
                "engine_version": status.get("EngineVersion"),
                "status": "Processing" if status.get("Processing") else "Active",
                "arn": status.get("ARN"),
            }
    return out


def _get_cached_domains(
    profile_name: str,
    env: str,
) -> dict[str, dict[str, Any]] | None:
    with _DOMAIN_CACHE_LOCK:
        return _DOMAIN_CACHE.get((profile_name, env))


def _set_cached_domains(
    profile_name: str,
    env: str,
    domains: dict[str, dict[str, Any]],
) -> None:
    with _DOMAIN_CACHE_LOCK:
        _DOMAIN_CACHE[(profile_name, env)] = domains


def _ensure_domains(
    session: boto3.Session,
    profile_name: str,
    env: str,
    force_refresh: bool,
) -> dict[str, dict[str, Any]]:
    if not force_refresh:
        cached = _get_cached_domains(profile_name, env)
        if cached is not None:
            return cached
    domains = _fetch_domains(session)
    _set_cached_domains(profile_name, env, domains)
    return domains


def _fetch_secret_payload(
    session: boto3.Session,
    env: str,
) -> dict[str, Any]:
    """Fetch ``<env>/devops/databases`` from Secrets Manager and JSON-decode."""
    client = session.client("secretsmanager")
    resp = client.get_secret_value(SecretId=f"{env}/devops/databases")
    raw = resp.get("SecretString")
    if raw is None:
        raise RuntimeError(
            f"Secret '{env}/devops/databases' has no SecretString (binary secret)."
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Secret '{env}/devops/databases' is not valid JSON: {exc}"
        ) from exc


def _get_cached_secret(profile_name: str, env: str) -> dict[str, Any] | None:
    with _SECRET_CACHE_LOCK:
        return _SECRET_CACHE.get((profile_name, env))


def _set_cached_secret(profile_name: str, env: str, payload: dict[str, Any]) -> None:
    with _SECRET_CACHE_LOCK:
        _SECRET_CACHE[(profile_name, env)] = payload


def _ensure_secret(
    session: boto3.Session,
    profile_name: str,
    env: str,
    force_refresh: bool,
) -> dict[str, Any]:
    if not force_refresh:
        cached = _get_cached_secret(profile_name, env)
        if cached is not None:
            return cached
    payload = _fetch_secret_payload(session, env)
    _set_cached_secret(profile_name, env, payload)
    return payload


def _credentials_for(
    session: boto3.Session,
    profile_name: str,
    env: str,
    domain: str,
    force_refresh: bool,
) -> tuple[str, str]:
    payload = _ensure_secret(session, profile_name, env, force_refresh)
    prefix = _secret_key_prefix(domain, env)
    user_key = f"{prefix}_opensearch_username"
    pass_key = f"{prefix}_opensearch_password"
    if user_key not in payload or pass_key not in payload:
        # Re-fetch once in case the secret rotated after we cached it.
        if not force_refresh:
            payload = _ensure_secret(session, profile_name, env, force_refresh=True)
        if user_key not in payload or pass_key not in payload:
            available = sorted(
                k for k in payload.keys() if k.endswith("_opensearch_username")
            )
            raise RuntimeError(
                f"Secret '{env}/devops/databases' has no keys '{user_key}' / '{pass_key}' "
                f"for domain '{domain}'. Available username keys: {available}"
            )
    return str(payload[user_key]), str(payload[pass_key])


def _resolve_endpoint(
    session: boto3.Session,
    profile_name: str,
    env: str,
    domain: str,
    force_refresh: bool,
) -> tuple[str, dict[str, dict[str, Any]]]:
    domains = _ensure_domains(session, profile_name, env, force_refresh)
    if domain not in domains:
        if not force_refresh:
            domains = _ensure_domains(session, profile_name, env, force_refresh=True)
        if domain not in domains:
            raise RuntimeError(
                f"OpenSearch domain '{domain}' not found in profile '{profile_name}'. "
                f"Available domains: {sorted(domains.keys())}"
            )
    endpoint = domains[domain].get("endpoint")
    if not endpoint:
        raise RuntimeError(
            f"OpenSearch domain '{domain}' has no resolvable endpoint yet "
            "(is it still Processing?)."
        )
    return endpoint, domains


# ── HTTP transport ──────────────────────────────────────────────────────────


def _classify_network_error(exc: BaseException) -> dict[str, Any]:
    """Classify a ``requests.RequestException`` for VPN diagnosis.

    OpenSearch domain endpoints in MyCompany envs are only reachable from the
    corporate VPN. If the AWS SDK step succeeded (we got a real endpoint)
    but the direct HTTPS call fails with a DNS / connection-refused /
    timeout error, the overwhelmingly likely cause is that the user is
    off VPN (or on the wrong VPN profile for the env).

    Returns ``{"vpn_hint": bool, "network_error_kind": str, "instruction": str}``.
    Non-network errors get ``vpn_hint=False`` so we don't mislead the user.
    """
    # requests wraps urllib3 exceptions; walk the chain looking for
    # familiar network-layer error strings.
    chain: list[str] = []
    cur: BaseException | None = exc
    for _ in range(6):
        if cur is None:
            break
        chain.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__

    joined = " | ".join(chain).lower()

    connect_timeout = isinstance(exc, requests.exceptions.ConnectTimeout) or (
        "connecttimeout" in joined
    )
    read_timeout = isinstance(exc, requests.exceptions.ReadTimeout) or "readtimeout" in joined
    dns_failure = any(
        marker in joined
        for marker in (
            "name or service not known",
            "nodename nor servname provided",
            "temporary failure in name resolution",
            "getaddrinfo failed",
            "no address associated with hostname",
            "gaierror",
        )
    )
    conn_refused = any(
        marker in joined
        for marker in (
            "connection refused",
            "no route to host",
            "network is unreachable",
            "host is unreachable",
        )
    )
    is_connection_err = isinstance(exc, requests.exceptions.ConnectionError)

    vpn_kind = ""
    if dns_failure:
        vpn_kind = "dns_failure"
    elif conn_refused:
        vpn_kind = "connection_refused"
    elif connect_timeout:
        vpn_kind = "connect_timeout"
    elif read_timeout:
        vpn_kind = "read_timeout"
    elif is_connection_err:
        vpn_kind = "connection_error"

    if vpn_kind:
        return {
            "vpn_hint": True,
            "network_error_kind": vpn_kind,
            "instruction": (
                "The OpenSearch domain exists in AWS, but the HTTPS request to its "
                "endpoint failed at the network layer. This almost always means the "
                "corporate VPN is not connected (or a different VPN profile than the "
                "one required for this env is active). Ask the user to double-check "
                "that the VPN is up for the target env, then retry the same tool call. "
                "Do NOT ask the user to run awsume or any other credentials command — "
                "this is a network reachability issue, not a credential issue."
            ),
        }

    return {"vpn_hint": False, "network_error_kind": "other", "instruction": ""}


def _os_get(
    endpoint: str,
    path: str,
    username: str,
    password: str,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Issue a GET against the OpenSearch cluster and return structured output.

    Always sets ``format=json`` on ``_cat`` endpoints so the caller gets
    parsed JSON. Other endpoints already return JSON by default.

    On network-layer failures (DNS, refused, timeout) attaches a
    ``vpn_hint: true`` payload so the AI host tells the user to check the VPN.
    """
    if not path.startswith("/"):
        path = "/" + path
    url = f"https://{endpoint}{path}"

    query = dict(params or {})
    if path.startswith("/_cat/") and "format" not in query:
        query["format"] = "json"

    try:
        resp = requests.get(
            url,
            auth=(username, password),
            params=query,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as exc:
        classified = _classify_network_error(exc)
        return {
            "ok": False,
            "url": url,
            "error": f"HTTP request failed: {exc}",
            **classified,
        }

    content_type = resp.headers.get("Content-Type", "")
    body: Any
    if "application/json" in content_type:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
    else:
        body = resp.text

    ok = 200 <= resp.status_code < 300
    return {
        "ok": ok,
        "url": url,
        "status_code": resp.status_code,
        "body": body,
    }


def _readonly_path_allowed(path: str) -> bool:
    if not path.startswith("/"):
        return False
    if ".." in path:
        return False
    if any(path.startswith(p) for p in _READONLY_PATH_PREFIXES):
        return True
    return bool(_READONLY_INDEX_PATH_RE.match(path))


def _attach_transport_extras(out: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Bubble ``vpn_hint`` / ``network_error_kind`` / ``instruction`` from an
    ``_os_get`` result into a tool's response dict.

    Called at every tool return site so callers get a consistent shape when
    the OpenSearch domain exists in AWS but the HTTPS call fails at the
    network layer.
    """
    if result.get("ok"):
        return out
    if result.get("vpn_hint"):
        out["vpn_hint"] = True
        out["network_error_kind"] = result.get("network_error_kind")
        out["instruction"] = result.get("instruction", "")
    return out


# ── MCP tools ────────────────────────────────────────────────────────────────


@mcp.tool()
def list_config_profiles() -> list[str]:
    """List AWS base profiles read from ~/.aws/config."""
    return _read_aws_profiles()


@mcp.tool()
def repair_credentials_file() -> dict[str, Any]:
    """Repair the shared AWS credentials file if parallel awsume writes
    (from this container or the sibling lvx-aws container) left it with
    duplicate keys.

    Rewrites ``$AWS_SHARED_CREDENTIALS_FILE`` (default
    ``~/.aws/credentials``) with the last value winning per key inside
    each section. No-op when the file already parses cleanly. Safe to
    call anytime.
    """
    with _AWSUME_LOCK:
        result = _dedupe_credentials_file()
    result["credentials_file"] = str(_credentials_file_path())
    result["ok"] = "error" not in result
    return result


@mcp.tool()
def awsume_refresh_session(
    profile_name: str,
    mfa_code: str = "",
    session_profile: str = "",
) -> dict[str, Any]:
    """Refresh credentials for a profile via awsume.

    ``profile_name`` accepts an exact profile or substring (e.g. ``stg4`` →
    ``lv-stg4-dr``). Tries the cached MFA session first; only asks for an
    MFA code (via ``needs_mfa: true``) when awsume actually needs one.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    effective_session = _resolve_session_profile(profile_name, session_profile or None)
    refresh = _run_awsume(
        profile_name=profile_name,
        session_profile=effective_session,
        mfa_code=mfa_code,
    )

    if refresh["ok"]:
        refresh["credentials_file"] = os.environ.get(
            "AWS_SHARED_CREDENTIALS_FILE",
            str(Path.home() / ".aws" / "credentials"),
        )

    return refresh


@mcp.tool()
def list_opensearch_domains(
    profile_name: str,
    env: str,
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List OpenSearch domains for the given profile/env.

    Uses the container-lifetime cache. Set ``force_refresh=True`` to bypass
    the cache and re-fetch from AWS (also re-caches the result).

    ``profile_name`` accepts a substring (see ``awsume_refresh_session``).
    """
    env = _validate_env(env)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        cache_hit_before = (not force_refresh) and _get_cached_domains(profile_name, env) is not None
        domains = _ensure_domains(session, profile_name, env, force_refresh)
        return {
            "ok": True,
            "profile_name": profile_name,
            "env": env,
            "region": session.region_name,
            "from_cache": cache_hit_before,
            "domain_count": len(domains),
            "domains": {
                name: {
                    "endpoint": info.get("endpoint"),
                    "engine_version": info.get("engine_version"),
                    "status": info.get("status"),
                }
                for name, info in sorted(domains.items())
            },
        }

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def check_indices(
    profile_name: str,
    env: str,
    domain: str,
    index_pattern: str = "*",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List indices on the domain (via ``_cat/indices``).

    ``index_pattern`` accepts wildcards (``*``, ``qm-*``, ``.kibana*``, …).
    Returns each index's health, status, doc counts, and storage size.
    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        pattern = (index_pattern or "*").strip() or "*"
        result = _os_get(endpoint, f"/_cat/indices/{pattern}", user, pwd)
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "index_pattern": pattern,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "indices": result.get("body") if result["ok"] else None,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def count_docs(
    profile_name: str,
    env: str,
    domain: str,
    index: str,
    query_json: str = "",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Return the document count for ``index`` (via ``_count``).

    ``index`` may include commas and wildcards (e.g. ``qm-2025*,qm-2026*``).
    ``query_json`` is optional; when supplied, must be a valid OpenSearch
    Query DSL JSON body (e.g. ``{"query":{"term":{"campaignId":"11111"}}}``).
    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    body: dict[str, Any] | None = None
    if query_json.strip():
        try:
            body = json.loads(query_json)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"query_json is not valid JSON: {exc}"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "query_json must decode to a JSON object."}

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        url = f"https://{endpoint}/{index}/_count"
        try:
            resp = requests.get(
                url,
                auth=(user, pwd),
                json=body,
                timeout=30,
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as exc:
            classified = _classify_network_error(exc)
            return {
                "ok": False,
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "endpoint": endpoint,
                "index": index,
                "url": url,
                "error": f"HTTP request failed: {exc}",
                **classified,
            }
        content_type = resp.headers.get("Content-Type", "")
        parsed: Any
        if "application/json" in content_type:
            try:
                parsed = resp.json()
            except ValueError:
                parsed = resp.text
        else:
            parsed = resp.text
        ok = 200 <= resp.status_code < 300
        return {
            "ok": ok,
            "profile_name": profile_name,
            "env": env,
            "domain": domain,
            "endpoint": endpoint,
            "index": index,
            "url": url,
            "status_code": resp.status_code,
            "count": parsed.get("count") if ok and isinstance(parsed, dict) else None,
            "body": parsed,
        }

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def check_aliases(
    profile_name: str,
    env: str,
    domain: str,
    alias_pattern: str = "*",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List index aliases on the domain (via ``_cat/aliases``).

    ``alias_pattern`` accepts wildcards. Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        pattern = (alias_pattern or "*").strip() or "*"
        result = _os_get(endpoint, f"/_cat/aliases/{pattern}", user, pwd)
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "alias_pattern": pattern,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "aliases": result.get("body") if result["ok"] else None,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def list_snapshot_repos(
    profile_name: str,
    env: str,
    domain: str,
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List snapshot repositories registered on the domain (via ``_snapshot``).

    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(endpoint, "/_snapshot/_all", user, pwd)
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "repositories": result.get("body") if result["ok"] else None,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def list_snapshots(
    profile_name: str,
    env: str,
    domain: str,
    repository: str,
    snapshot_pattern: str = "_all",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List snapshots in ``repository`` (via ``_snapshot/<repo>/<pattern>``).

    ``snapshot_pattern`` defaults to ``_all`` and accepts wildcards.
    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    repo = (repository or "").strip()
    if not repo:
        return {"ok": False, "error": "repository is required."}

    pattern = (snapshot_pattern or "_all").strip() or "_all"

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(endpoint, f"/_snapshot/{repo}/{pattern}", user, pwd)
        body = result.get("body") if result["ok"] else None
        snapshots: Any = None
        if isinstance(body, dict):
            snapshots = body.get("snapshots") or body
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "repository": repo,
                "snapshot_pattern": pattern,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "snapshots": snapshots,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def cluster_health(
    profile_name: str,
    env: str,
    domain: str,
    level: str = "cluster",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Get cluster health (via ``_cluster/health``).

    ``level`` may be ``cluster`` (default), ``indices``, or ``shards``.
    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    if level not in {"cluster", "indices", "shards"}:
        return {"ok": False, "error": "level must be one of: cluster, indices, shards."}

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(endpoint, "/_cluster/health", user, pwd, params={"level": level})
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "health": result.get("body") if result["ok"] else None,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def cluster_stats(
    profile_name: str,
    env: str,
    domain: str,
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Get cluster stats (via ``_cluster/stats``). Read-only."""
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(endpoint, "/_cluster/stats", user, pwd)
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "stats": result.get("body") if result["ok"] else None,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def list_nodes(
    profile_name: str,
    env: str,
    domain: str,
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List cluster nodes with per-node stats (via ``_cat/nodes``). Read-only."""
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(
            endpoint,
            "/_cat/nodes",
            user,
            pwd,
            params={
                "v": "true",
                "h": "name,ip,node.role,master,heap.percent,ram.percent,cpu,load_1m,disk.used_percent",
            },
        )
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "nodes": result.get("body") if result["ok"] else None,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def get_capacity(
    profile_name: str,
    env: str,
    domain: str,
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Report per-node disk allocation and total capacity (via ``_cat/allocation``).

    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        allocation = _os_get(endpoint, "/_cat/allocation", user, pwd, params={"v": "true"})
        cluster_stats_result = _os_get(endpoint, "/_cluster/stats", user, pwd)
        summary: dict[str, Any] = {}
        if cluster_stats_result["ok"] and isinstance(cluster_stats_result.get("body"), dict):
            indices = cluster_stats_result["body"].get("indices") or {}
            nodes = cluster_stats_result["body"].get("nodes") or {}
            fs = nodes.get("fs") or {}
            summary = {
                "indices_count": indices.get("count"),
                "docs_count": (indices.get("docs") or {}).get("count"),
                "store_size_bytes": (indices.get("store") or {}).get("size_in_bytes"),
                "fs_total_bytes": fs.get("total_in_bytes"),
                "fs_free_bytes": fs.get("free_in_bytes"),
                "fs_available_bytes": fs.get("available_in_bytes"),
            }
        return _attach_transport_extras(
            {
                "ok": allocation["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "endpoint": endpoint,
                "status_code": allocation.get("status_code"),
                "allocation": allocation.get("body") if allocation["ok"] else None,
                "summary": summary,
                "error": None if allocation["ok"] else allocation.get("body") or allocation.get("error"),
            },
            allocation,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def opensearch_get(
    profile_name: str,
    env: str,
    domain: str,
    path: str,
    query_params_json: str = "",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Freestyle **READ-ONLY** GET against the OpenSearch cluster.

    ``path`` must be a rooted path that maps to a read-only endpoint. The
    allow-list covers ``_cat/*``, ``_cluster/health|state|stats|settings``,
    ``_nodes*``, ``_snapshot*``, ``_alias*``, ``_mapping*``, ``_settings*``,
    ``_search``, ``_count``, ``_stats``, ``_tasks*``, ``_ilm*``, ``_ism*``,
    ``_plugins/*``, ``_opendistro/*``, ``_index_template*``,
    ``_component_template*``, ``_template*``, ``_data_stream*``,
    ``_ingest/pipeline*``, ``_scripts*``, and per-index read-only endpoints
    like ``/<index>/_search``, ``/<index>/_count``, ``/<index>/_mapping``,
    ``/<index>/_settings``, ``/<index>/_alias``, ``/<index>/_stats``.

    ``query_params_json`` is an optional JSON object of query-string
    parameters, e.g. ``{"pretty":"true","level":"indices"}``.

    Any path outside the allow-list, any relative path, or any path
    containing ``..`` is refused. This tool never issues POST/PUT/DELETE/
    HEAD/PATCH.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    path_clean = (path or "").strip()
    if not _readonly_path_allowed(path_clean):
        return {
            "ok": False,
            "error": (
                f"path '{path}' is not on the read-only allow-list. "
                "This tool refuses arbitrary paths — only known read-only endpoints "
                "(_cat/*, _cluster/health|state|stats|settings, _nodes*, _snapshot*, "
                "_alias*, _mapping*, _settings*, _search, _count, _stats, _tasks*, "
                "_ilm*, _ism*, _plugins/*, _opendistro/*) and per-index /<index>/_search "
                "|_count|_mapping|_settings|_alias|_stats are allowed."
            ),
        }

    params: dict[str, Any] = {}
    if query_params_json.strip():
        try:
            decoded = json.loads(query_params_json)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"query_params_json is not valid JSON: {exc}"}
        if not isinstance(decoded, dict):
            return {"ok": False, "error": "query_params_json must decode to a JSON object."}
        params = decoded

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(endpoint, path_clean, user, pwd, params=params)
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "endpoint": endpoint,
                "path": path_clean,
                "status_code": result.get("status_code"),
                "body": result.get("body"),
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


# ── MyCompany client-oriented tools ────────────────────────────────────────────
#
# MyCompany QM domains follow a strict naming convention:
#   index pattern = ``<client_id>_qm-<NNNNNN>``    e.g. ``100262_qm-000001``
#   alias         = ``<client_id>_qm``             e.g. ``100262_qm``
#
# One client can have multiple indices tied to a single alias. Only ONE of
# those indices is the current write-index for the alias at any given time.
# The tools below take a ``client_id`` (numeric, 2–10 digits) and auto-derive
# both the index pattern and the alias so callers don't have to compose them.


@mcp.tool()
def resolve_client(
    profile_name: str,
    env: str,
    domain: str,
    client_id: str,
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """One-shot lookup for a MyCompany client_id on a QM OpenSearch domain.

    Given ``client_id`` (e.g. ``106105``), returns in a single call:

      * ``indices``: every ``<client_id>_qm-*`` index (via ``_cat/indices``),
        including health, status, doc count, storage size.
      * ``alias`` binding for ``<client_id>_qm`` (via ``_alias/<alias>``).
      * ``write_index``: the current write-index for the alias (the one
        whose alias binding has ``is_write_index: true``), or ``None`` when
        no explicit write-index is configured (older aliases sometimes
        rely on OpenSearch's default rules).

    Read-only. Use this whenever a user asks something like *"is there an
    index for client 106105 in stg4?"* — one call resolves both the
    pattern and the alias in the same step.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    client_id = _validate_client_id(client_id)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    pattern = _client_index_pattern(client_id)
    alias = _client_alias(client_id)

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)

        indices_result = _os_get(endpoint, f"/_cat/indices/{pattern}", user, pwd)
        alias_result = _os_get(endpoint, f"/_alias/{alias}", user, pwd)

        # Extract write index (if declared).
        write_index: str | None = None
        alias_bindings: dict[str, Any] = {}
        if alias_result["ok"] and isinstance(alias_result.get("body"), dict):
            body = alias_result["body"]
            for idx_name, idx_meta in body.items():
                idx_aliases = (idx_meta or {}).get("aliases") or {}
                binding = idx_aliases.get(alias) or {}
                alias_bindings[idx_name] = binding
                if binding.get("is_write_index") is True:
                    write_index = idx_name

        # Collect index names from the _cat response for a compact summary.
        index_names: list[str] = []
        if indices_result["ok"] and isinstance(indices_result.get("body"), list):
            for row in indices_result["body"]:
                if isinstance(row, dict) and row.get("index"):
                    index_names.append(row["index"])

        overall_ok = indices_result["ok"] and alias_result["ok"]
        out: dict[str, Any] = {
            "ok": overall_ok,
            "profile_name": profile_name,
            "env": env,
            "domain": domain,
            "client_id": client_id,
            "endpoint": endpoint,
            "index_pattern": pattern,
            "alias": alias,
            "index_count": len(index_names),
            "index_names": sorted(index_names),
            "indices": indices_result.get("body") if indices_result["ok"] else None,
            "alias_bindings": alias_bindings,
            "write_index": write_index,
            "errors": {
                "indices": None if indices_result["ok"] else indices_result.get("body") or indices_result.get("error"),
                "alias": None if alias_result["ok"] else alias_result.get("body") or alias_result.get("error"),
            },
        }
        # Bubble vpn_hint from whichever call failed first.
        _attach_transport_extras(out, indices_result)
        _attach_transport_extras(out, alias_result)
        return out

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def check_writable_indices(
    profile_name: str,
    env: str,
    domain: str,
    alias: str = "",
    client_id: str = "",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Return the current write-index for an alias.

    Provide either ``alias`` explicitly, or ``client_id`` to auto-derive
    ``<client_id>_qm`` (the MyCompany QM naming convention). Calls
    ``GET /_alias/<alias>`` and reports which of the bound indices has
    ``is_write_index: true``.

    The response also lists every non-write index bound to the same alias
    (``read_indices``) so callers can see the full read set at a glance.

    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    try:
        effective_alias, resolved_client = _resolve_alias_target(alias, client_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(endpoint, f"/_alias/{effective_alias}", user, pwd)

        write_index: str | None = None
        read_indices: list[str] = []
        bindings: dict[str, Any] = {}
        if result["ok"] and isinstance(result.get("body"), dict):
            for idx_name, idx_meta in result["body"].items():
                idx_aliases = (idx_meta or {}).get("aliases") or {}
                binding = idx_aliases.get(effective_alias) or {}
                bindings[idx_name] = binding
                if binding.get("is_write_index") is True:
                    write_index = idx_name
                else:
                    read_indices.append(idx_name)

        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "alias": effective_alias,
                "client_id": resolved_client,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "write_index": write_index,
                "read_indices": sorted(read_indices),
                "bindings": bindings,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def list_closed_indices(
    profile_name: str,
    env: str,
    domain: str,
    index_pattern: str = "*",
    client_id: str = "",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List **closed** indices matching a pattern.

    Uses ``_cat/indices/<pattern>?expand_wildcards=closed,open`` and filters
    the response down to rows with ``status == "close"``. Provide
    ``client_id`` to auto-derive ``<client_id>_qm-*`` (MyCompany QM
    convention), or pass ``index_pattern`` directly.

    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    try:
        pattern, resolved_client = _resolve_index_target(index_pattern, client_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(
            endpoint,
            f"/_cat/indices/{pattern}",
            user,
            pwd,
            params={"expand_wildcards": "closed,open"},
        )
        closed: list[Any] = []
        if result["ok"] and isinstance(result.get("body"), list):
            for row in result["body"]:
                if isinstance(row, dict) and row.get("status") == "close":
                    closed.append(row)
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "client_id": resolved_client,
                "index_pattern": pattern,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "closed_index_count": len(closed),
                "closed_indices": closed,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def check_shards(
    profile_name: str,
    env: str,
    domain: str,
    index_pattern: str = "*",
    client_id: str = "",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List primary and replica shards for the matched indices.

    Uses ``_cat/shards/<pattern>``. Response includes shard number,
    primary-vs-replica marker (``prirep`` field: ``p``/``r``), state
    (``STARTED``/``INITIALIZING``/``UNASSIGNED``), doc count per shard,
    store size, and the node hosting the shard.

    Provide ``client_id`` to auto-derive ``<client_id>_qm-*``.

    The response also carries a compact ``summary`` block with
    ``primary_count``, ``replica_count``, and ``unassigned_count`` so
    callers can spot allocation issues in one glance.

    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    try:
        pattern, resolved_client = _resolve_index_target(index_pattern, client_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(
            endpoint,
            f"/_cat/shards/{pattern}",
            user,
            pwd,
            params={
                "h": "index,shard,prirep,state,docs,store,node,unassigned.reason",
            },
        )
        summary: dict[str, int] = {
            "primary_count": 0,
            "replica_count": 0,
            "unassigned_count": 0,
        }
        if result["ok"] and isinstance(result.get("body"), list):
            for row in result["body"]:
                if not isinstance(row, dict):
                    continue
                prirep = row.get("prirep")
                state = row.get("state")
                if prirep == "p":
                    summary["primary_count"] += 1
                elif prirep == "r":
                    summary["replica_count"] += 1
                if state == "UNASSIGNED":
                    summary["unassigned_count"] += 1
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "client_id": resolved_client,
                "index_pattern": pattern,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "shards": result.get("body") if result["ok"] else None,
                "summary": summary,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def check_segments(
    profile_name: str,
    env: str,
    domain: str,
    index_pattern: str = "*",
    client_id: str = "",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List Lucene segments for the matched indices.

    Uses ``_cat/segments/<pattern>``. Response includes per-segment name,
    generation, docs count, deleted-docs count, size on disk, size in
    memory, whether the segment is committed and searchable, and the
    compound-file flag.

    Provide ``client_id`` to auto-derive ``<client_id>_qm-*``.

    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    try:
        pattern, resolved_client = _resolve_index_target(index_pattern, client_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(endpoint, f"/_cat/segments/{pattern}", user, pwd)
        segment_count = 0
        total_docs = 0
        if result["ok"] and isinstance(result.get("body"), list):
            for row in result["body"]:
                if not isinstance(row, dict):
                    continue
                segment_count += 1
                try:
                    total_docs += int(row.get("docs.count", 0) or 0)
                except (TypeError, ValueError):
                    pass
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "client_id": resolved_client,
                "index_pattern": pattern,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "segment_count": segment_count,
                "total_docs_across_segments": total_docs,
                "segments": result.get("body") if result["ok"] else None,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def get_doc(
    profile_name: str,
    env: str,
    domain: str,
    index: str,
    doc_id: str,
    source_includes: str = "",
    source_excludes: str = "",
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Fetch a single document by id (via ``GET /<index>/_doc/<id>``).

    ``index`` may be an alias (e.g. ``100262_qm``) or a concrete index
    (e.g. ``100262_qm-000001``). The response includes the document's
    ``_id``, ``_index``, ``_version``, ``_seq_no``, ``_primary_term``,
    and ``_source``.

    ``source_includes`` / ``source_excludes`` are comma-separated field
    lists to project or drop specific fields from ``_source``. Standard
    OpenSearch semantics.

    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    idx = (index or "").strip()
    doc = (doc_id or "").strip()
    if not idx:
        return {"ok": False, "error": "index is required."}
    if not doc:
        return {"ok": False, "error": "doc_id is required."}

    params: dict[str, Any] = {}
    if source_includes.strip():
        params["_source_includes"] = source_includes.strip()
    if source_excludes.strip():
        params["_source_excludes"] = source_excludes.strip()

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(endpoint, f"/{idx}/_doc/{doc}", user, pwd, params=params)
        body = result.get("body")
        found = False
        if isinstance(body, dict):
            found = bool(body.get("found"))
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "index": idx,
                "doc_id": doc,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "found": found,
                "document": body if result["ok"] else None,
                "error": None if result["ok"] else body or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


@mcp.tool()
def get_index_fields(
    profile_name: str,
    env: str,
    domain: str,
    index: str,
    force_refresh: bool = False,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Return the field names and types for an index (via ``_mapping``).

    ``index`` may be an alias or a concrete index name, and may include
    wildcards (e.g. ``100262_qm-*``). The response includes:

      * ``fields``: a flat map of dotted field paths → OpenSearch field
        type (e.g. ``{"campaignId": "keyword", "call.duration": "long"}``).
        Nested properties are flattened with dots so callers can plug the
        keys directly into a Query DSL body.
      * ``raw_mapping``: the untouched ``_mapping`` response per index.

    Read-only.
    """
    env = _validate_env(env)
    domain = _validate_domain(domain)
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    idx = (index or "").strip()
    if not idx:
        return {"ok": False, "error": "index is required."}

    def _flatten(properties: dict[str, Any], prefix: str = "") -> dict[str, str]:
        flat: dict[str, str] = {}
        for name, meta in (properties or {}).items():
            if not isinstance(meta, dict):
                continue
            path = f"{prefix}{name}" if not prefix else f"{prefix}.{name}"
            nested = meta.get("properties")
            if isinstance(nested, dict) and nested:
                flat.update(_flatten(nested, path))
                if "type" in meta:
                    flat[path] = str(meta["type"])
            else:
                flat[path] = str(meta.get("type", "object"))
        return flat

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        endpoint, _ = _resolve_endpoint(session, profile_name, env, domain, force_refresh)
        user, pwd = _credentials_for(session, profile_name, env, domain, force_refresh)
        result = _os_get(endpoint, f"/{idx}/_mapping", user, pwd)
        fields_by_index: dict[str, dict[str, str]] = {}
        if result["ok"] and isinstance(result.get("body"), dict):
            for idx_name, mapping in result["body"].items():
                mappings = ((mapping or {}).get("mappings") or {})
                properties = mappings.get("properties") or {}
                fields_by_index[idx_name] = _flatten(properties)
        # Merge every index's fields into one flat map so the common case
        # (single index / alias resolving to one mapping) is trivial.
        merged: dict[str, str] = {}
        for m in fields_by_index.values():
            merged.update(m)
        return _attach_transport_extras(
            {
                "ok": result["ok"],
                "profile_name": profile_name,
                "env": env,
                "domain": domain,
                "index": idx,
                "endpoint": endpoint,
                "status_code": result.get("status_code"),
                "field_count": len(merged),
                "fields": merged,
                "fields_by_index": fields_by_index,
                "raw_mapping": result.get("body") if result["ok"] else None,
                "error": None if result["ok"] else result.get("body") or result.get("error"),
            },
            result,
        )

    return _run_with_refresh(
        profile_name=profile_name,
        session_profile=session_profile,
        region=region,
        auto_refresh_on_expired=auto_refresh_on_expired,
        mfa_code=mfa_code,
        caller=call,
    )


if __name__ == "__main__":
    mcp.run()
