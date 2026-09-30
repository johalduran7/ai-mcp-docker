import base64
import configparser
import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lvx-aws")
from version_notifier import install as _install_version_notifier
_install_version_notifier(mcp, "lvx-aws")


def _aws_config_path() -> Path:
    configured = os.environ.get("AWS_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".aws" / "config"


def _read_aws_profiles() -> list[str]:
    return sorted(_read_profile_metadata().keys())


def _read_profile_metadata() -> dict[str, dict[str, Any]]:
    """Return ``{profile_name: {kind, region, sso_session?, sso_start_url?, sso_region?, sso_account_id?, sso_role_name?, role_arn?, source_profile?}}``.

    ``kind`` is ``"sso"`` when the profile (or its ``sso-session`` block)
    provides an SSO configuration, otherwise ``"iam"``. Everything else is
    a passthrough of the relevant keys from ``~/.aws/config`` so callers
    don't have to reopen the file.
    """
    config_path = _aws_config_path()
    parser = configparser.RawConfigParser()
    if not config_path.exists():
        return {}
    parser.read(config_path)

    sso_sessions: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        if section.startswith("sso-session "):
            name = section.replace("sso-session ", "", 1)
            sso_sessions[name] = {
                k: parser.get(section, k) for k in parser.options(section)
            }

    out: dict[str, dict[str, Any]] = {}
    for section in parser.sections():
        if section == "default":
            profile = "default"
        elif section.startswith("profile "):
            profile = section.replace("profile ", "", 1)
        else:
            continue

        opts = {k: parser.get(section, k) for k in parser.options(section)}
        meta: dict[str, Any] = {"kind": "iam", "region": opts.get("region")}

        sso_session_name = opts.get("sso_session")
        if sso_session_name and sso_session_name in sso_sessions:
            block = sso_sessions[sso_session_name]
            meta["kind"] = "sso"
            meta["sso_session"] = sso_session_name
            meta["sso_start_url"] = block.get("sso_start_url")
            meta["sso_region"] = block.get("sso_region")
            meta["sso_account_id"] = opts.get("sso_account_id")
            meta["sso_role_name"] = opts.get("sso_role_name")
        elif opts.get("sso_start_url"):
            meta["kind"] = "sso"
            meta["sso_session"] = None
            meta["sso_start_url"] = opts.get("sso_start_url")
            meta["sso_region"] = opts.get("sso_region")
            meta["sso_account_id"] = opts.get("sso_account_id")
            meta["sso_role_name"] = opts.get("sso_role_name")
        else:
            if opts.get("role_arn"):
                meta["role_arn"] = opts.get("role_arn")
            if opts.get("source_profile"):
                meta["source_profile"] = opts.get("source_profile")
            if opts.get("mfa_serial"):
                meta["mfa_serial"] = opts.get("mfa_serial")

        out[profile] = meta

    return out


def _read_sso_sessions() -> dict[str, dict[str, str]]:
    """Return every ``[sso-session <name>]`` block from ``~/.aws/config``."""
    config_path = _aws_config_path()
    parser = configparser.RawConfigParser()
    if not config_path.exists():
        return {}
    parser.read(config_path)
    return {
        section.replace("sso-session ", "", 1): {
            k: parser.get(section, k) for k in parser.options(section)
        }
        for section in parser.sections()
        if section.startswith("sso-session ")
    }


def _profile_kind(profile_name: str) -> str:
    """Return ``"sso"`` or ``"iam"`` for ``profile_name`` (defaults to ``iam``)."""
    return _read_profile_metadata().get(profile_name, {}).get("kind", "iam")


def _resolve_profile(profile_query: str) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a possibly-partial profile name against ``~/.aws/config``.

    Behavior:
      * Exact match → return that profile.
      * Single case-insensitive substring match → return that profile.
      * Multiple matches, exactly one of which is IAM (non-SSO) → return the
        IAM match. This is the "``na3`` should default to ``lv-na3``, not
        the SSO twin" hierarchy: users must be explicit (spell more of the
        SSO profile's name) to pick an SSO profile when an IAM twin exists.
        SSO detection is metadata-driven (presence of ``sso_session`` /
        ``sso_start_url`` in the profile block) — there is no hardcoded
        naming convention like ``mycompany-*``.
      * Multiple matches otherwise → ``ambiguous_profile`` payload with the
        ``matches`` list (each item includes ``kind``) so the AI host can
        ask the user which one to use.
      * Zero matches → ``profile_not_found`` payload including the
        available profiles.

    Returns:
        ``(resolved_profile_name, error_payload)`` — exactly one populated.
    """
    metadata = _read_profile_metadata()
    profiles = sorted(metadata.keys())
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
        iam_matches = [p for p in matches if metadata.get(p, {}).get("kind") != "sso"]
        if len(iam_matches) == 1:
            return iam_matches[0], None
        detailed = [
            {"name": p, "kind": metadata.get(p, {}).get("kind", "iam")}
            for p in matches
        ]
        return None, {
            "ok": False,
            "profile_query": profile_query,
            "ambiguous_profile": True,
            "matches": matches,
            "matches_detailed": detailed,
            "instruction": (
                f"Multiple AWS profiles match '{profile_query}': {', '.join(matches)}. "
                "Ask the user which one to use, then call the same tool again with the full profile_name. "
                "Note: substrings that hit both an IAM and an SSO profile default to the IAM one; "
                "to pick the SSO profile, spell more of its name (SSO vs IAM is decided by the profile's "
                "config block, not by any naming convention)."
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
            "Ask the user to pick one of the available profiles and call the same tool again."
        ),
    }


def _resolve_session_profile(profile_name: str, session_profile: str | None) -> str:
    if session_profile:
        return session_profile
    if profile_name == "default":
        return "default"
    if _profile_kind(profile_name) == "sso":
        return profile_name
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
    meta = _read_profile_metadata().get(profile_name, {})
    if meta.get("kind") == "sso":
        effective_region = (
            region or meta.get("region") or _region_from_config(profile_name)
            or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        )
        return boto3.Session(profile_name=profile_name, region_name=effective_region)
    effective_profile = _resolve_session_profile(profile_name, session_profile)
    effective_region = region or _region_from_config(profile_name) or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    return boto3.Session(profile_name=effective_profile, region_name=effective_region)


def _short_version(version: str) -> str:
    """`27.0` → `27`. Matches MyCompany `u<major>` resource naming."""
    return str(version).split(".", 1)[0].strip()


def _tag_value(tags: list[dict[str, Any]] | None, key: str) -> str | None:
    """Return the value of ``key`` from an AWS ``Tags`` list.

    Handles the AWS inconsistency where most services (EC2, ASG, IAM, …)
    return tags as ``{"Key": ..., "Value": ...}`` (upper-case) while ECS
    returns them as ``{"key": ..., "value": ...}`` (lower-case). Both
    shapes are accepted transparently so callers don't have to care
    which service produced the tag list.
    """
    if not tags:
        return None
    for tag in tags:
        tag_key = tag.get("Key") if "Key" in tag else tag.get("key")
        if tag_key == key:
            return tag.get("Value") if "Value" in tag else tag.get("value")
    return None


# ── MyCompany component naming exceptions ──────────────────────────────────────
# These mirror the mappings used by devops/repositories.nosync/deploy_tags_ecs_asg.py
# and lambda_scripts/lvx-confluence-component-version/scrub_inactive_components.py
# so the AWS side and the config-repo side reconcile to the same component name.

ECS_COMPONENT_MAP: dict[str, str] = {
    # AWS-side name → repo/friendly name
    "emailgateway": "email_gateway",
}

ASG_COMPONENT_MAP: dict[str, str] = {
    # AWS-side parsed name → repo/friendly name
    "recordingpurge": "recording_purge",
    "customscripts": "custom_scripts",
}

# ASG pool "core" names that do not follow the standard
# <component>-<major>-asg-<env> convention. Value = (component, version).
ASG_CORE_OVERRIDES: dict[str, tuple[str, str]] = {
    "sftp-2ftpatg": ("ftpatg", "2.0"),
    "sftp-2ftpin":  ("ftpin",  "2.0"),
    "sftp-2ftpout": ("ftpout", "2.0"),
}

# Case-insensitive tag keys treated as the authoritative version of an ASG pool.
ASG_VERSION_TAG_KEYS: tuple[str, ...] = ("version", "lv_version")

_ECS_SERVICE_RE = re.compile(r"^(.+?)-(?:u?\d[\d_]*)-service-.+$")
_ECS_SERVICE_VERSION_RE = re.compile(r"-u(\d+)-service-")
_ECS_CLUSTER_VERSION_RE = re.compile(r"-u(\d+)-cluster-")

# ── Refresh policy ───────────────────────────────────────────────────────────
# Only these envs may be refreshed via this MCP server. All other envs are
# rejected at the tool boundary — the AI host must not attempt to bypass this.
REFRESHABLE_ENVS: frozenset[str] = frozenset({"au1", "eu1", "perf1", "stg4", "tst2", "tst3", "uk2", "uk3"})

# ASG instance-refresh preferences (mirrors redeploy_refresh_ecs_asg.py).
_ASG_REFRESH_PREFERENCES = {"MinHealthyPercentage": 50, "InstanceWarmup": 60}


def _reject_non_refreshable_env(env: str) -> dict[str, Any] | None:
    """Return an error payload if ``env`` is not in the refresh whitelist."""
    if env in REFRESHABLE_ENVS:
        return None
    return {
        "ok": False,
        "env": env,
        "refresh_disallowed": True,
        "allowed_envs": sorted(REFRESHABLE_ENVS),
        "error": f"Env '{env}' is not in the refresh whitelist.",
        "instruction": (
            "Refreshing components is only allowed for envs: "
            f"{', '.join(sorted(REFRESHABLE_ENVS))}. "
            "Do not attempt to refresh any other env; report this restriction to the user."
        ),
    }


def _extract_ecs_component(service_name: str) -> str:
    """`acm-u27-service-tst3` → `acm`. Applies ECS_COMPONENT_MAP."""
    match = _ECS_SERVICE_RE.match(service_name)
    parsed = match.group(1) if match else service_name
    return ECS_COMPONENT_MAP.get(parsed, parsed)


def _extract_ecs_version_from_cluster(cluster_name: str) -> str:
    """`fargate-u27-cluster-tst3` → `27.0`."""
    match = _ECS_CLUSTER_VERSION_RE.search(cluster_name)
    return f"{match.group(1)}.0" if match else ""


def _parse_asg_name(asg_name: str, env: str) -> tuple[str, str]:
    """Extract (component, version) from an ASG name, or ("", "") if unparseable.

    Handles patterns such as:
      * ``agentproxy-4-asg-eu1``           → ("agentproxy", "4.0")
      * ``lms-12inbound-primary-asg-eu1``  → ("lms", "12.0")
      * ``eu1-acd-27a-asg-eu1``            → ("acd", "27.0")
      * ``agent-screenrecording-4-asg-eu1``→ ("agent-screenrecording", "4.0")
      * ``sftp-2ftpatg-asg-eu1``           → ("ftpatg", "2.0")   (override)

    Applies ``ASG_CORE_OVERRIDES`` and ``ASG_COMPONENT_MAP`` for consistency
    with the config-repo side.
    """
    suffix = f"-asg-{env}"
    if not asg_name.endswith(suffix):
        return "", ""
    core = asg_name[: -len(suffix)]

    env_prefix = f"{env}-"
    if core.startswith(env_prefix):
        core = core[len(env_prefix):]

    if core in ASG_CORE_OVERRIDES:
        return ASG_CORE_OVERRIDES[core]

    segments = core.split("-")
    version_idx: int | None = None
    for i in range(len(segments) - 1, -1, -1):
        if segments[i] and segments[i][0].isdigit():
            version_idx = i
            break
    if version_idx is None or version_idx == 0:
        return "", ""

    component = "-".join(segments[:version_idx])
    version_match = re.match(r"(\d+)", segments[version_idx])
    version = f"{version_match.group(1)}.0" if version_match else ""
    return ASG_COMPONENT_MAP.get(component, component), version


def _extract_asg_version_tag(tags: list[dict[str, Any]] | None) -> str | None:
    """Return the value of ``Version`` or ``lv_version`` (case-insensitive), if any."""
    if not tags:
        return None
    for tag in tags:
        key = (tag.get("Key") or "").strip().lower()
        if key in ASG_VERSION_TAG_KEYS:
            val = tag.get("Value")
            return str(val).strip() if val is not None else None
    return None


# ── MyCompany revision extraction ─────────────────────────────────────────────
# MyCompany tracks TWO distinct version identifiers per running component:
#
#   * ``lv_component_version`` — the "branch" / release train, e.g. ``27.0``.
#     For ASG pools it lives on the ``Version`` / ``lv_version`` tag.
#     For ECS it's implicit in the cluster name (``fargate-u27-cluster-<env>``
#     → ``27.0``). This is what ``list_env_components`` already returns as
#     the ``version`` field.
#
#   * ``lv_component_revision`` — the FULL patch version, e.g. ``27.0.24``
#     (sometimes with suffix like ``27.0.24-AUTO``). It is a **TAG** on
#     both ASG and ECS resources — no image inspection, no task-definition
#     lookup required:
#       * ASG → ``lv_component_revision`` tag on the ASG itself
#         (propagated to running instances). Read from
#         ``describe_auto_scaling_groups`` in the same call we already
#         make. Fallback: EC2 instance tag if the ASG-level tag is
#         missing (defensive; not expected in practice).
#       * ECS → ``lv_component_revision`` tag on the ECS service. Read
#         via ``describe_services(include=['TAGS'])`` in the same batched
#         call we already make.
#
# ``list_env_components`` uses these bulk tag reads so callers can build a
# full env-wide (component × version × revision) table in ONE tool call
# instead of looping ``get_asg_pool_status`` / ``get_ecs_service_status``
# per component.

# Reverse of ASG_CORE_OVERRIDES: {friendly_component_name: core_name}. Used by
# `get_asg_pool_status` so a caller passing the friendly MyCompany name
# (``ftpin``) still resolves the aliased ASG pool (``sftp-2ftpin-asg-<env>``).
_ASG_CORE_OVERRIDES_REVERSE: dict[str, str] = {
    component: core for core, (component, _version) in ASG_CORE_OVERRIDES.items()
}

# Tag keys that carry MyCompany's canonical component identity on BOTH
# ASG and ECS resources. Same tags, same shape, both sides. Case matched
# exactly — MyCompany tags use the lowercase snake_case form.
_LV_COMPONENT_VERSION_TAG = "lv_component_version"
_LV_COMPONENT_REVISION_TAG = "lv_component_revision"


def _asg_version_from_tags(asg: dict[str, Any]) -> str | None:
    """Read ``lv_component_version`` directly from an ASG object's ``Tags``."""
    return _tag_value(asg.get("Tags"), _LV_COMPONENT_VERSION_TAG)


def _asg_revision_from_tags(asg: dict[str, Any]) -> str | None:
    """Read ``lv_component_revision`` directly from an ASG object's ``Tags``.

    ``describe_auto_scaling_groups`` returns tags inline, so this needs no
    extra API call. Returns ``None`` when the tag isn't set.
    """
    return _tag_value(asg.get("Tags"), _LV_COMPONENT_REVISION_TAG)


def _bulk_asg_tags_from_instances(
    ec2_client, asg_names: list[str]
) -> dict[str, dict[str, str | None]]:
    """Defensive fallback for ASGs missing pool-level MyCompany tags.

    Uses a SINGLE (paginated) ``ec2.describe_instances`` call filtered by
    ``tag:aws:autoscaling:groupName IN [asg_names]`` +
    ``instance-state-name = running``, then picks the first running
    instance's ``lv_component_version`` / ``lv_component_revision`` tags
    per ASG.

    Only used for ASGs where the pool-level tags are missing — in the
    normal MyCompany setup ASG tags with ``PropagateAtLaunch=true`` mean
    both the ASG and its instances carry both tags, so this fallback
    should rarely fire.
    """
    result: dict[str, dict[str, str | None]] = {
        name: {"version": None, "revision": None} for name in asg_names
    }
    if not asg_names:
        return result

    # AWS caps filter values at 200 per filter — chunk defensively.
    chunk_size = 200
    for i in range(0, len(asg_names), chunk_size):
        chunk = asg_names[i : i + chunk_size]
        paginator = ec2_client.get_paginator("describe_instances")
        for page in paginator.paginate(
            Filters=[
                {"Name": "tag:aws:autoscaling:groupName", "Values": chunk},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        ):
            for reservation in page.get("Reservations", []):
                for ins in reservation.get("Instances", []):
                    asg_name: str | None = None
                    version: str | None = None
                    revision: str | None = None
                    for tag in ins.get("Tags") or []:
                        key = tag.get("Key")
                        val = tag.get("Value")
                        if key == "aws:autoscaling:groupName":
                            asg_name = val
                        elif key == _LV_COMPONENT_VERSION_TAG:
                            version = val
                        elif key == _LV_COMPONENT_REVISION_TAG:
                            revision = val
                    if not asg_name:
                        continue
                    slot = result.get(asg_name)
                    if slot is None:
                        continue
                    if version and slot["version"] is None:
                        slot["version"] = version
                    if revision and slot["revision"] is None:
                        slot["revision"] = revision
    return result


def _bulk_ecs_tags(
    ecs_client, cluster_to_services: dict[str, list[str]]
) -> dict[tuple[str, str], dict[str, str | None]]:
    """Return ``{(cluster, service): {version, revision}}`` for the given services.

    Reads the ``lv_component_version`` and ``lv_component_revision``
    **tags** on each ECS service via batched
    ``ecs.describe_services(include=['TAGS'])`` (10 services per call —
    AWS hard limit). No task-definition lookup and no image-tag parsing.
    Same tags MyCompany stamps on ASGs.
    """
    result: dict[tuple[str, str], dict[str, str | None]] = {}
    for cluster, services in cluster_to_services.items():
        for svc in services:
            result[(cluster, svc)] = {"version": None, "revision": None}
        for i in range(0, len(services), 10):
            batch = services[i : i + 10]
            resp = ecs_client.describe_services(
                cluster=cluster, services=batch, include=["TAGS"]
            )
            for svc in resp.get("services", []):
                svc_name = svc.get("serviceName")
                if not svc_name:
                    continue
                tags = svc.get("tags")
                result[(cluster, svc_name)] = {
                    "version": _tag_value(tags, _LV_COMPONENT_VERSION_TAG),
                    "revision": _tag_value(tags, _LV_COMPONENT_REVISION_TAG),
                }
    return result


def _session_expired(exc: Exception) -> bool:
    """Detect any credential expiry — IAM (awsume) *or* SSO. The dispatch
    to IAM vs SSO refresh happens inside ``_try_refresh_and_call`` based
    on the profile's ``kind``."""
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
        # SSO markers (botocore SSOTokenProvider / sso-oidc)
        "ssotokenloaderror",
        "unauthorizedexception",
        "sso session associated with this profile has expired",
        "sso session",
        "sso login",
        "sso authentication",
        "invalidgranterror",
        "invalidgrantexception",
        "the sso authentication has expired",
        "invalid_grant",
        "expiredtokenexception",
    )
    return any(marker in message for marker in markers)


def _sso_looking(exc: Exception) -> bool:
    """Narrower check: does this error clearly come from the SSO path?
    Used to route IAM-credential errors that also match ``_session_expired``
    onto the correct refresh path."""
    message = str(exc).lower()
    sso_markers = (
        "ssotokenloaderror",
        "unauthorizedexception",
        "sso session",
        "sso login",
        "sso authentication",
        "invalidgranterror",
        "invalidgrantexception",
        "invalid_grant",
    )
    return any(marker in message for marker in sso_markers)


def _needs_mfa(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "enter mfa",
        "mfa token",
        "multi-factor",
        "mfa code",
    )
    return any(marker in lowered for marker in markers)


# Serialize all awsume invocations. awsume writes the shared credentials
# file non-atomically; two parallel awsume processes racing on the same
# section produce duplicate keys and configparser then refuses to read
# the file at all (blocking every subsequent aws/awsume call). This lock
# is held only around the awsume subprocess + the pre-repair pass.
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
    ``None`` on any failure (network, timeout, bad payload) — caller then
    leaves TZ unset and behavior falls back to the previous (buggy) path.
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
                headers={"User-Agent": "lvx-aws"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                tz = resp.read().decode().strip()
            if "/" in tz:  # basic sanity — IANA zones contain a slash
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

    Serialized via ``_AWSUME_LOCK`` so parallel refreshes (e.g. from
    ``run_aws_cli``'s profile fan-out) can't race on the shared
    credentials file.
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
        needs_mfa = _needs_mfa(combined_output) or (not mfa_code.strip() and "eof" in combined_output.lower())
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


# ── SSO device-auth refresh ──────────────────────────────────────────────────
# Any profile whose config block declares an `sso_session` or `sso_start_url`
# is classified as SSO by `_read_profile_metadata()` — there is no hardcoded
# naming convention (`mycompany-*` etc.). SSO profiles cannot be refreshed via
# awsume; they use OIDC device-code auth, and the user must click an "Allow"
# link in a browser to approve the session (which for MyCompany requires
# FortiClient VPN to be ON).
#
# The refresh flow is stateful across tool calls to keep the AI-host UX clean:
#
#   1. First tool call hits an expired SSO token → ``_try_sso_refresh_and_call``
#      runs, sees no pending device auth, calls ``sso-oidc.register_client`` +
#      ``start_device_authorization``, remembers the (client_id, client_secret,
#      device_code, url, expires_at) in ``_SSO_PENDING`` keyed by sso_session,
#      and returns ``needs_sso_login`` with the ``verification_uri_complete`` +
#      VPN warning. The tool response is NOT ``ok``.
#   2. AI host shows the URL to the user. User clicks, signs in, approves.
#   3. AI host re-invokes the SAME tool. It fails again with expired SSO →
#      refresh runs, sees pending state, polls ``create_token``. If the user
#      already approved, we get a token, write it to ``~/.aws/sso/cache`` in
#      the exact format botocore expects, forget the pending state, RETRY the
#      original call, and return the result. The user never sees the second
#      URL — they just see the answer.
#   4. If the user hasn't approved yet, we return ``still_pending: true`` with
#      the same URL so the AI can ask them to click it and try again.
#   5. If the device auth window (typically 10 min) has expired, we discard
#      it and start a new one on the next call.

_SSO_PENDING: dict[str, dict[str, Any]] = {}
# Real lock — serializes device-auth init so parallel SSO refreshes for the
# same sso_session don't each register their own OIDC client with AWS.
_SSO_PENDING_LOCK = threading.Lock()

_SSO_VPN_WARNING = (
    "IMPORTANT: FortiClient VPN must be turned ON to reach MyCompany SSO "
    "endpoints (identitycenter.amazonaws.com / device.sso.<region>.amazonaws.com). "
    "If the sign-in page fails to load, verify the VPN is connected before retrying."
)


def _sso_cache_dir() -> Path:
    root = os.environ.get("AWS_CONFIG_FILE")
    base = Path(root).expanduser().parent if root else (Path.home() / ".aws")
    d = base / "sso" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sso_cache_key(sso_session: str | None, sso_start_url: str | None) -> str | None:
    """Return the SHA1 hex used by botocore's ``SSOTokenProvider`` to key
    the on-disk token cache. Session-based configs (``sso_session = …``)
    hash the session name; legacy configs hash the start URL."""
    if sso_session:
        return hashlib.sha1(sso_session.encode("utf-8")).hexdigest()
    if sso_start_url:
        return hashlib.sha1(sso_start_url.encode("utf-8")).hexdigest()
    return None


def _read_sso_cache(
    sso_session: str | None, sso_start_url: str | None
) -> dict[str, Any] | None:
    key = _sso_cache_key(sso_session, sso_start_url)
    if not key:
        return None
    path = _sso_cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_sso_cache(
    sso_session: str | None,
    sso_start_url: str,
    sso_region: str,
    token_resp: dict[str, Any],
    client_id: str,
    client_secret: str,
    client_registration_expires_at: int | None = None,
) -> Path | None:
    """Write the token in the exact shape botocore's ``SSOTokenProvider``
    reads. Includes both ``sessionName`` (new schema) and ``startUrl``
    (legacy) so either code path picks it up."""
    key = _sso_cache_key(sso_session, sso_start_url)
    if not key:
        return None
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(token_resp.get("expiresIn") or 3600)
    )
    payload: dict[str, Any] = {
        "startUrl": sso_start_url,
        "region": sso_region,
        "accessToken": token_resp.get("accessToken"),
        "expiresAt": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clientId": client_id,
        "clientSecret": client_secret,
    }
    if sso_session:
        payload["sessionName"] = sso_session
    if token_resp.get("refreshToken"):
        payload["refreshToken"] = token_resp["refreshToken"]
    if client_registration_expires_at:
        payload["registrationExpiresAt"] = datetime.fromtimestamp(
            client_registration_expires_at, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = _sso_cache_dir() / f"{key}.json"
    path.write_text(json.dumps(payload, separators=(",", ":")))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _sso_pending_key(sso_session: str | None, sso_start_url: str | None) -> str:
    return sso_session or sso_start_url or "unknown"


def _start_sso_device_auth(
    sso_session: str | None,
    sso_start_url: str,
    sso_region: str,
) -> dict[str, Any]:
    """Kick off an OIDC device-authorization flow. Serialized under
    ``_SSO_PENDING_LOCK`` so parallel refreshes for the same sso_session
    don't each register their own OIDC client with AWS — the first
    arrival wins and everyone else reuses its device code."""
    key = _sso_pending_key(sso_session, sso_start_url)
    with _SSO_PENDING_LOCK:
        existing = _SSO_PENDING.get(key)
        if existing and time.time() < existing["expires_at"]:
            return existing
        oidc = boto3.client("sso-oidc", region_name=sso_region)
        reg = oidc.register_client(
            clientName=f"lvx-aws-{sso_session or 'legacy'}",
            clientType="public",
            scopes=["sso:account:access"],
        )
        auth = oidc.start_device_authorization(
            clientId=reg["clientId"],
            clientSecret=reg["clientSecret"],
            startUrl=sso_start_url,
        )
        pending = {
            "sso_session": sso_session,
            "sso_start_url": sso_start_url,
            "sso_region": sso_region,
            "client_id": reg["clientId"],
            "client_secret": reg["clientSecret"],
            "client_registration_expires_at": reg.get("clientSecretExpiresAt"),
            "device_code": auth["deviceCode"],
            "user_code": auth["userCode"],
            "verification_uri": auth.get("verificationUri"),
            "verification_uri_complete": auth.get("verificationUriComplete")
                or auth.get("verificationUri"),
            "expires_at": time.time() + int(auth.get("expiresIn") or 600),
            "interval": max(1, int(auth.get("interval") or 5)),
        }
        _SSO_PENDING[key] = pending
        return pending


def _redeem_sso_pending(pending: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Try to redeem a pending device auth. Returns ``(status, data)`` where
    status is one of ``"approved"`` / ``"pending"`` / ``"expired"``. On
    ``approved``, ``data`` is the ``create_token`` response and the caller
    should write the SSO cache."""
    if time.time() >= pending["expires_at"]:
        return "expired", None
    oidc = boto3.client("sso-oidc", region_name=pending["sso_region"])
    try:
        token = oidc.create_token(
            clientId=pending["client_id"],
            clientSecret=pending["client_secret"],
            grantType="urn:ietf:params:oauth:grant-type:device_code",
            deviceCode=pending["device_code"],
        )
    except oidc.exceptions.AuthorizationPendingException:
        return "pending", None
    except oidc.exceptions.SlowDownException:
        return "pending", None
    except (oidc.exceptions.ExpiredTokenException,
            oidc.exceptions.InvalidGrantException):
        return "expired", None
    return "approved", token


def _sso_needs_login_payload(
    profile_name: str,
    sso_session: str | None,
    sso_start_url: str,
    sso_region: str,
    pending: dict[str, Any],
    status_hint: str = "",
    original_exc: Exception | None = None,
) -> dict[str, Any]:
    remaining = max(0, int(pending["expires_at"] - time.time()))
    payload: dict[str, Any] = {
        "ok": False,
        "profile_name": profile_name,
        "needs_sso_login": True,
        "sso_session": sso_session,
        "sso_start_url": sso_start_url,
        "sso_region": sso_region,
        "verification_uri_complete": pending["verification_uri_complete"],
        "verification_uri": pending["verification_uri"],
        "user_code": pending["user_code"],
        "expires_in_seconds": remaining,
        "poll_interval_seconds": pending["interval"],
        "vpn_warning": _SSO_VPN_WARNING,
        "instruction": (
            "Show the `verification_uri_complete` link to the user verbatim — "
            "it already carries the code, no manual entry needed. Also relay "
            "the FortiClient VPN warning. Once the user says they've clicked "
            "the link and approved the sign-in, call the SAME tool again with "
            "the SAME arguments — the server will detect the newly-approved "
            "session and continue automatically. If they need more time, they "
            f"have {remaining}s before this device-code request expires."
        ),
    }
    if status_hint:
        payload["sso_status_hint"] = status_hint
    if original_exc is not None:
        payload["error"] = str(original_exc)
    return payload


def _try_sso_refresh_and_call(
    profile_name: str,
    sso_session: str | None,
    sso_start_url: str | None,
    sso_region: str | None,
    original_exc: Exception,
    caller,
) -> dict[str, Any]:
    """SSO analogue of ``_try_awsume_refresh_and_call``.

    Two-phase: (1) if there's a pending device code, poll to see if the
    user just approved and, on success, retry the caller silently.
    (2) otherwise start a fresh device auth and return the URL for the
    user to click.
    """
    if not sso_start_url or not sso_region:
        return {
            "ok": False,
            "profile_name": profile_name,
            "error": str(original_exc),
            "session_expired": True,
            "sso_misconfigured": True,
            "instruction": (
                f"Profile '{profile_name}' looks like an SSO profile but "
                "either its `sso_start_url` or `sso_region` is missing from "
                "~/.aws/config. Ask the user to fix the config; do not "
                "attempt to guess the values."
            ),
        }

    pending_key = _sso_pending_key(sso_session, sso_start_url)
    pending = _SSO_PENDING.get(pending_key)

    if pending:
        status, token = _redeem_sso_pending(pending)
        if status == "approved" and token:
            _write_sso_cache(
                sso_session=sso_session,
                sso_start_url=sso_start_url,
                sso_region=sso_region,
                token_resp=token,
                client_id=pending["client_id"],
                client_secret=pending["client_secret"],
                client_registration_expires_at=pending.get(
                    "client_registration_expires_at"
                ),
            )
            _SSO_PENDING.pop(pending_key, None)
            # Force botocore to re-read the cache on the retry.
            try:
                result = caller()
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "error": str(original_exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                    "sso_refresh_succeeded": True,
                    "instruction": (
                        "SSO login completed and a fresh token is cached, but "
                        "the retried AWS call still failed. Report the "
                        "`retry_error` to the user."
                    ),
                }
            if isinstance(result, dict):
                result["refreshed_sso"] = True
            return result
        if status == "pending":
            return _sso_needs_login_payload(
                profile_name=profile_name,
                sso_session=sso_session,
                sso_start_url=sso_start_url,
                sso_region=sso_region,
                pending=pending,
                status_hint="authorization_pending",
                original_exc=original_exc,
            )
        # Expired device code → drop and start fresh below.
        _SSO_PENDING.pop(pending_key, None)

    try:
        fresh = _start_sso_device_auth(
            sso_session=sso_session,
            sso_start_url=sso_start_url,
            sso_region=sso_region,
        )
    except (ClientError, BotoCoreError) as start_exc:
        return {
            "ok": False,
            "profile_name": profile_name,
            "error": str(original_exc),
            "sso_start_error": str(start_exc),
            "session_expired": True,
            "vpn_warning": _SSO_VPN_WARNING,
            "instruction": (
                "Could not start the SSO device-authorization flow. Most common "
                "cause is FortiClient VPN being OFF — the SSO OIDC endpoint is "
                "only reachable on-VPN. Ask the user to verify VPN status and "
                "retry."
            ),
        }
    return _sso_needs_login_payload(
        profile_name=profile_name,
        sso_session=sso_session,
        sso_start_url=sso_start_url,
        sso_region=sso_region,
        pending=fresh,
        status_hint="fresh_device_authorization",
        original_exc=original_exc,
    )


def _reject_if_sso(profile_name: str, tool_name: str) -> dict[str, Any] | None:
    """MyCompany-specific tools call this at the top of their body to refuse
    SSO profiles cleanly instead of failing halfway through with a naming
    mismatch."""
    if _profile_kind(profile_name) != "sso":
        return None
    return {
        "ok": False,
        "profile_name": profile_name,
        "profile_kind": "sso",
        "mycompany_only": True,
        "error": (
            f"'{tool_name}' is a MyCompany-specific tool — it assumes MyCompany "
            "naming conventions (component/version/env), tag keys "
            "(lv_component_*), and log-group layout that generic (non-MyCompany) "
            "SSO or IAM accounts don't use."
        ),
        "instruction": (
            "For non-MyCompany accounts (including any SSO-authenticated profile), "
            "use the generic tools: "
            "`run_aws_cli` for arbitrary AWS calls, `describe_env_instances` "
            "/ `describe_env_tasks` for env-wide EC2/ECS inventory, "
            "`describe_env_log_groups` / `query_log_groups` for CloudWatch "
            "queries against explicit log groups, and `list_ecs_clusters` "
            "for cluster listing. If the user truly wants MyCompany behavior, "
            "they need to pass a MyCompany IAM profile."
        ),
    }


def _try_refresh_and_call(
    profile_name: str,
    session_profile: str,
    mfa_code: str,
    original_exc: Exception,
    caller,
) -> dict[str, Any]:
    """Dispatch credential refresh to the right backend for this profile.

    IAM (awsume) and SSO (device-auth) share the same public entry point
    so no calling tool has to know which flavor it's dealing with.
    """
    kind = _profile_kind(profile_name)
    if kind == "sso" or _sso_looking(original_exc):
        meta = _read_profile_metadata().get(profile_name, {})
        return _try_sso_refresh_and_call(
            profile_name=profile_name,
            sso_session=meta.get("sso_session"),
            sso_start_url=meta.get("sso_start_url"),
            sso_region=meta.get("sso_region") or meta.get("region"),
            original_exc=original_exc,
            caller=caller,
        )
    return _try_awsume_refresh_and_call(
        profile_name=profile_name,
        session_profile=session_profile,
        mfa_code=mfa_code,
        original_exc=original_exc,
        caller=caller,
    )


def _try_awsume_refresh_and_call(
    profile_name: str,
    session_profile: str,
    mfa_code: str,
    original_exc: Exception,
    caller,
) -> dict[str, Any]:
    """Attempt awsume refresh (cached session first, then MFA if provided) and re-run the caller.

    Refresh is fully internal: the container has awsume installed and mounts
    ``~/.aws`` and ``~/.awsume`` from the host. The user is never asked to run
    anything in their terminal. The only user interaction we may request is the
    MFA code (surfaced via ``needs_mfa: true``), which the MCP host should
    prompt for and then re-invoke the same tool with ``mfa_code=<code>``.

    Awsume's exit code is not a reliable success signal — it can exit non-zero
    when it printed a benign shell-integration warning even though it did
    write fresh credentials to ``~/.aws/credentials``. The authoritative
    signal is whether the retried AWS call succeeds. So we always retry the
    caller once after running awsume and interpret the outcome:

      * Retry succeeds → return the successful result (silent to the user).
      * Retry fails with an expired-token error AND awsume asked for MFA →
        return ``needs_mfa: true`` for the host to prompt.
      * Retry fails for any other reason → surface the underlying error.
        Never ask the user for an MFA code or to run anything in their
        terminal in this branch.
    """
    refresh = _run_awsume(
        profile_name=profile_name,
        session_profile=session_profile,
        mfa_code=mfa_code,
    )

    # Always retry the AWS call regardless of awsume's exit code.
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

        # We already tried with an MFA code and it still failed — do not
        # loop asking for another code.
        if mfa_code.strip():
            payload["needs_mfa"] = False
            payload["mfa_attempt_failed"] = True
            payload["instruction"] = (
                f"Awsume refresh failed for profile '{profile_name}' even after an MFA code was supplied. "
                "Do not ask the user to run anything in their terminal. Report the errors above."
            )
            return payload

        # Only prompt for MFA when awsume said it needs one AND the retry
        # confirms the credentials are still expired.
        if refresh.get("needs_mfa") and retry_expired:
            payload["needs_mfa"] = True
            payload["instruction"] = (
                f"Prompt the user for the current MFA code for profile '{profile_name}', "
                "then call this same tool again with mfa_code=<code>. "
                "Do NOT ask the user to run awsume in their terminal — the refresh is handled internally."
            )
            return payload

        # Retry failed for a non-MFA reason (or awsume never asked for MFA).
        payload["needs_mfa"] = False
        payload["instruction"] = (
            f"Awsume refresh for profile '{profile_name}' did not restore working credentials, "
            "but this is NOT an MFA issue. Do NOT ask the user for an MFA code, and do NOT tell "
            "them to run awsume or any other command in their terminal. Just report the errors above "
            "so the user can investigate."
        )
        return payload

    # Retry succeeded — awsume actually did its job even if its own exit
    # code was non-zero.
    if isinstance(result, dict):
        result["refreshed_with_awsume"] = True
        if refresh.get("stdout"):
            result["refresh_output"] = refresh["stdout"]
    return result


@mcp.tool()
def list_config_profiles() -> list[str]:
    """List AWS base profiles read from ~/.aws/config."""
    return _read_aws_profiles()


@mcp.tool()
def repair_credentials_file() -> dict[str, Any]:
    """Repair the shared AWS credentials file if parallel awsume writes
    left it with duplicate keys.

    Rewrites ``$AWS_SHARED_CREDENTIALS_FILE`` (default
    ``~/.aws/credentials``) with the last value winning per key inside
    each section — matching what the most-recent awsume writer intended.
    No-op when the file already parses cleanly. Safe to call anytime.
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

    ``profile_name`` accepts either an exact profile or a substring; a unique
    match is auto-resolved (e.g. ``stg4`` → ``lv-stg4-dr``). If it matches
    multiple profiles, the response contains ``ambiguous_profile: true`` and a
    ``matches`` list so the host can ask the user which one to use.

    Tries the awsume cached MFA session first (no MFA required). If awsume asks for MFA,
    the response contains needs_mfa=true so the host can prompt the user for the code and call again.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    denied = _reject_if_sso(profile_name, "awsume_refresh_session")
    if denied:
        denied["instruction"] = (
            f"Profile '{profile_name}' is SSO-based. awsume does not apply. "
            "Use the `sso_login` tool to authenticate — it will return a URL "
            "the user must click to approve. For MyCompany SSO, FortiClient "
            "VPN must be ON."
        )
        return denied

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
def auth_status(
    profile_name: str,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Validate active AWS credentials for a profile using STS GetCallerIdentity.

    ``profile_name`` accepts a substring (see ``awsume_refresh_session`` for the
    resolution contract).

    On expired session, tries to refresh via awsume automatically. Only surfaces an MFA prompt if awsume needs it.
    """
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
        identity = session.client("sts").get_caller_identity()
        return {
            "ok": True,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": session.region_name,
            "account": identity.get("Account"),
            "arn": identity.get("Arn"),
            "user_id": identity.get("UserId"),
        }

    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "session_profile": _resolve_session_profile(profile_name, session_profile or None),
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "error": str(exc),
        }


@mcp.tool()
def list_ecs_clusters(
    profile_name: str,
    session_profile: str = "",
    region: str = "",
    max_results: int = 100,
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List ECS clusters for the given profile/session profile.

    ``profile_name`` accepts a substring (see ``awsume_refresh_session`` for the
    resolution contract).

    On expired session, tries to refresh via awsume automatically. Only surfaces an MFA prompt if awsume needs it.
    """
    if max_results < 1:
        raise ValueError("max_results must be >= 1")
    if max_results > 100:
        max_results = 100

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
        ecs = session.client("ecs")
        response = ecs.list_clusters(maxResults=max_results)
        cluster_arns = response.get("clusterArns", [])
        names = [arn.split("/")[-1] for arn in cluster_arns]
        return {
            "ok": True,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": session.region_name,
            "cluster_count": len(cluster_arns),
            "clusters": names,
            "cluster_arns": cluster_arns,
        }

    try:
        return call()
    except (ClientError, BotoCoreError, ValueError) as exc:
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "session_profile": _resolve_session_profile(profile_name, session_profile or None),
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "error": str(exc),
        }


@mcp.tool()
def get_ecs_service_status(
    profile_name: str,
    env: str,
    version: str,
    component: str,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Return the current status of the ECS service for a MyCompany component.

    **Single-service tool.** If the caller has (or is about to build) a
    list of components to check, do NOT loop this tool — use
    ``bulk_get_ecs_service_status`` (batched + parallel, same payload per
    service) instead. For env-wide rollout / revision / image / drift
    questions across many services, use ``describe_env_tasks`` (one call,
    all tasks with tags, groupable in memory) instead of looping either
    tool.

    ``profile_name`` accepts a substring (see ``awsume_refresh_session`` for the
    resolution contract).

    Resolves cluster and service names using the standard naming convention:
      cluster = ``fargate-u<major>-cluster-<env>``
      service = ``<component>-u<major>-service-<env>``

    Returns the service's desired/running/pending counts, task definition, container
    images, and per-task last status and image. Read-only.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    denied = _reject_if_sso(profile_name, "get_ecs_service_status")
    if denied:
        return denied

    short_v = _short_version(version)
    cluster = f"fargate-u{short_v}-cluster-{env}"
    service = f"{component}-u{short_v}-service-{env}"

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        ecs = session.client("ecs")

        svc_res = ecs.describe_services(
            cluster=cluster, services=[service], include=["TAGS"]
        )
        services = svc_res.get("services", [])
        if not services:
            return {
                "ok": False,
                "profile_name": profile_name,
                "session_profile": _resolve_session_profile(profile_name, session_profile or None),
                "region": session.region_name,
                "cluster": cluster,
                "service": service,
                "error": f"Service {service} not found in cluster {cluster}.",
            }

        service_info = services[0]
        task_def_arn = service_info.get("taskDefinition")
        # MyCompany stamps ``lv_component_version`` and ``lv_component_revision``
        # on the ECS service (same tags MyCompany stamps on ASGs). Read both
        # directly — no image tag parsing, no task-definition lookup.
        service_tags = service_info.get("tags")
        service_version: str | None = _tag_value(
            service_tags, _LV_COMPONENT_VERSION_TAG
        )
        service_revision: str | None = _tag_value(
            service_tags, _LV_COMPONENT_REVISION_TAG
        )

        images: list[str] = []
        if task_def_arn:
            td_res = ecs.describe_task_definition(taskDefinition=task_def_arn)
            for c in td_res.get("taskDefinition", {}).get("containerDefinitions", []):
                image = c.get("image")
                if image:
                    images.append(image)

        tasks_out: list[dict[str, Any]] = []
        list_res = ecs.list_tasks(cluster=cluster, serviceName=service)
        task_arns = list_res.get("taskArns", [])
        if task_arns:
            details = ecs.describe_tasks(cluster=cluster, tasks=task_arns)
            for t in details.get("tasks", []):
                cont_image = next(
                    (
                        c.get("image")
                        for c in t.get("containers", [])
                        if component in (c.get("name") or "")
                    ),
                    None,
                )
                started_at = t.get("startedAt")
                tasks_out.append(
                    {
                        "task_id": (t.get("taskArn") or "").split("/")[-1],
                        "last_status": t.get("lastStatus"),
                        "desired_status": t.get("desiredStatus"),
                        "started_at": started_at.isoformat() if started_at else None,
                        "image": cont_image,
                    }
                )

        # Service-level version + revision come from the
        # ``lv_component_version`` / ``lv_component_revision`` tags on
        # the service itself (see ``describe_services`` above).
        return {
            "ok": True,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": session.region_name,
            "cluster": cluster,
            "service": service,
            "status": service_info.get("status"),
            "desired_count": service_info.get("desiredCount"),
            "running_count": service_info.get("runningCount"),
            "pending_count": service_info.get("pendingCount"),
            "task_definition": task_def_arn,
            "version": service_version,
            "revision": service_revision,
            "container_images": images,
            "task_count": len(tasks_out),
            "tasks": tasks_out,
        }

    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "session_profile": _resolve_session_profile(profile_name, session_profile or None),
                    "cluster": cluster,
                    "service": service,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "cluster": cluster,
            "service": service,
            "error": str(exc),
        }


# ── Bulk ECS service status ─────────────────────────────────────────────────
# ``get_ecs_service_status`` is single-component. When the AI host wants to
# inspect N components in one env it should NOT loop the single tool — that
# thrashes the model with N tool calls when we can group by cluster and
# batch ``describe_services`` (10 services per call, AWS hard limit).

# AWS hard limit — ``ecs.describe_services`` accepts at most 10 services
# per call.
_ECS_DESCRIBE_SERVICES_BATCH = 10


def _parse_ecs_component_specs(
    specs: list[Any], default_version: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize ECS bulk-status specs.

    Accepts, per element:
      * ``{"component": "acm", "version": "27.0"}`` — canonical.
      * ``{"component": "acm"}`` — inherits ``default_version``.
      * ``"acm@27.0"`` / ``"acm:27.0"`` — shorthand.
      * ``"acm"`` — inherits ``default_version``.

    Returns ``(parsed, malformed)``. ``parsed`` items are
    ``{"component": str, "version": str}`` with a normalized short major
    version (via ``_short_version``) so cluster names line up.
    """
    parsed: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []

    for entry in specs or []:
        component: str = ""
        version: str = ""

        if isinstance(entry, dict):
            component = str(entry.get("component") or "").strip()
            v_raw = entry.get("version")
            version = str(v_raw).strip() if v_raw not in (None, "") else ""
        elif isinstance(entry, str):
            candidate = entry.strip()
            if not candidate:
                continue
            # Accept ``acm@27.0`` or ``acm:27.0``.
            for sep in ("@", ":"):
                if sep in candidate:
                    left, right = candidate.split(sep, 1)
                    component = left.strip()
                    version = right.strip()
                    break
            else:
                component = candidate
        else:
            malformed.append(
                {"input": entry, "error": "spec must be a dict or a string."}
            )
            continue

        if not component:
            malformed.append({"input": entry, "error": "spec missing `component`."})
            continue
        if not version:
            version = (default_version or "").strip()
        if not version:
            malformed.append(
                {
                    "input": entry,
                    "error": (
                        "spec missing `version` and no `default_version` "
                        "provided at the top level."
                    ),
                }
            )
            continue
        parsed.append({"component": component, "version": _short_version(version)})
    return parsed, malformed


def _describe_services_batched(
    ecs_client, cluster: str, service_names: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(services, failures)``.

    ``services`` is the concatenation of every ``describe_services`` batch
    (``services`` key) with tags included. ``failures`` is the list of
    service names AWS said it couldn't find (its ``failures`` payload).
    """
    services: list[dict[str, Any]] = []
    failures: list[str] = []
    for i in range(0, len(service_names), _ECS_DESCRIBE_SERVICES_BATCH):
        batch = service_names[i : i + _ECS_DESCRIBE_SERVICES_BATCH]
        resp = ecs_client.describe_services(
            cluster=cluster, services=batch, include=["TAGS"]
        )
        services.extend(resp.get("services", []))
        for f in resp.get("failures", []) or []:
            arn = f.get("arn") or f.get("resourceArn") or ""
            failures.append(arn.split("/")[-1] if arn else str(f))
    return services, failures


@mcp.tool()
def bulk_get_ecs_service_status(
    profile_name: str,
    env: str,
    components: list[Any],
    version: str = "",
    max_workers: int = 5,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Return ECS service status for many MyCompany components in ONE call. **Read-only.**

    **Use this whenever the AI host would otherwise loop
    ``get_ecs_service_status`` per component.** Pass ALL the components in
    a single call — the tool batches `describe_services` server-side (10
    per call, AWS hard limit) and parallelizes across clusters. Never
    call this in a loop yourself.

    If the question is about aggregate rollout / revision progress across
    services rather than per-service status fields, prefer
    ``describe_env_tasks`` (one call for the whole env, tasks + tags in a
    single response) and group in memory.

    Same per-service payload as ``get_ecs_service_status`` (status,
    desired/running/pending counts, task definition, ``lv_component_version``
    and ``lv_component_revision`` tags), but batched and parallel:

      * Components are grouped by resolved cluster
        (``fargate-u<major>-cluster-<env>``).
      * Each cluster's services are fetched via batched
        ``describe_services(include=['TAGS'])`` — 10 per call (AWS hard
        limit).
      * Clusters are queried in parallel threads (default 5, capped at
        20). boto3 clients are thread-safe.

    Use whenever the AI host would otherwise loop
    ``get_ecs_service_status`` per component — this is meaningfully
    faster and slashes the tool-call count.

    Args:
        profile_name: AWS profile substring.
        env: Environment code (e.g. ``"uk2"``).
        components: List of component specs. Each entry may be:
              * ``{"component":"acm","version":"27.0"}`` — canonical.
              * ``{"component":"acm"}`` — inherits ``version`` param.
              * ``"acm@27.0"`` / ``"acm:27.0"`` — shorthand.
              * ``"acm"`` — inherits ``version`` param.
            Bare component names without a resolvable version land in
            ``malformed``.
        version: Default version applied to any spec that doesn't set
            one. Ignored for specs with an explicit version. Accepts
            ``"27.0"`` or ``"27"``.
        max_workers: Parallel cluster-worker cap (1..20). Default 5.

    Response shape:
        ``{ok, env, requested_count, resolved_count, success_count,
        failure_count, malformed, workers, total_duration_seconds,
        results: [{component, version, cluster, service, ok, status,
        desired_count, running_count, pending_count, task_definition,
        version, revision, tags?, error?}, ...]}``.

        Result rows carry ``ok=False`` and an ``error`` field when the
        service is missing from the cluster (AWS
        ``describe_services.failures``) so callers can distinguish "not
        deployed" from "deployment unhealthy".
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    denied = _reject_if_sso(profile_name, "bulk_get_ecs_service_status")
    if denied:
        return denied

    parsed_specs, malformed = _parse_ecs_component_specs(components or [], version)
    if not parsed_specs:
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "requested_count": len(components or []),
            "resolved_count": 0,
            "malformed": malformed,
            "error": (
                "components must be a non-empty list of specs (dict or "
                "string) with a resolvable version."
            ),
        }

    workers = max(1, min(int(max_workers or 1), 20))

    # Group specs by cluster (major-version scoped) so we can batch
    # describe_services per cluster. Also remember the ordered service
    # names per cluster for stable output.
    cluster_to_specs: dict[str, list[dict[str, Any]]] = {}
    service_to_spec: dict[tuple[str, str], dict[str, Any]] = {}
    for spec in parsed_specs:
        component = spec["component"]
        short_v = spec["version"]
        cluster = f"fargate-u{short_v}-cluster-{env}"
        service = f"{component}-u{short_v}-service-{env}"
        cluster_to_specs.setdefault(cluster, []).append(
            {"component": component, "version": short_v, "service": service}
        )
        service_to_spec[(cluster, service)] = {
            "component": component,
            "version": short_v,
        }

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        ecs = session.client("ecs")

        def _do_cluster(cluster: str) -> list[dict[str, Any]]:
            entries = cluster_to_specs[cluster]
            service_names = [e["service"] for e in entries]
            rows: list[dict[str, Any]] = []
            try:
                services, missing = _describe_services_batched(
                    ecs, cluster, service_names
                )
            except (ClientError, BotoCoreError) as exc:
                # Whole-cluster failure — every requested service in
                # this cluster gets the same error attached.
                for entry in entries:
                    rows.append(
                        {
                            "component": entry["component"],
                            "version": entry["version"],
                            "cluster": cluster,
                            "service": entry["service"],
                            "ok": False,
                            "error": (
                                f"describe_services failed for cluster "
                                f"{cluster}: {exc}"
                            ),
                        }
                    )
                return rows

            found_by_name = {svc.get("serviceName"): svc for svc in services}
            for entry in entries:
                svc_name = entry["service"]
                svc = found_by_name.get(svc_name)
                if svc is None:
                    rows.append(
                        {
                            "component": entry["component"],
                            "version": entry["version"],
                            "cluster": cluster,
                            "service": svc_name,
                            "ok": False,
                            "error": (
                                f"Service {svc_name} not found in cluster "
                                f"{cluster}."
                            ),
                            "missing_from_aws": svc_name in missing,
                        }
                    )
                    continue
                tags = svc.get("tags")
                rows.append(
                    {
                        "component": entry["component"],
                        "version": entry["version"],
                        "cluster": cluster,
                        "service": svc_name,
                        "ok": True,
                        "status": svc.get("status"),
                        "desired_count": svc.get("desiredCount"),
                        "running_count": svc.get("runningCount"),
                        "pending_count": svc.get("pendingCount"),
                        "task_definition": svc.get("taskDefinition"),
                        # MyCompany tag-based version / revision (same as
                        # ``get_ecs_service_status`` — no image parsing,
                        # no task-definition lookups).
                        "lv_component_version": _tag_value(
                            tags, _LV_COMPONENT_VERSION_TAG
                        ),
                        "lv_component_revision": _tag_value(
                            tags, _LV_COMPONENT_REVISION_TAG
                        ),
                    }
                )
            return rows

        started = time.monotonic()
        results: list[dict[str, Any]] = []
        clusters = list(cluster_to_specs.keys())
        # boto3 clients are thread-safe; share across workers.
        if len(clusters) == 1:
            results.extend(_do_cluster(clusters[0]))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_do_cluster, cluster): cluster
                    for cluster in clusters
                }
                for fut in as_completed(futures):
                    try:
                        results.extend(fut.result())
                    except Exception as exc:  # noqa: BLE001
                        cluster = futures[fut]
                        for entry in cluster_to_specs[cluster]:
                            results.append(
                                {
                                    "component": entry["component"],
                                    "version": entry["version"],
                                    "cluster": cluster,
                                    "service": entry["service"],
                                    "ok": False,
                                    "error": str(exc),
                                }
                            )
        duration = round(time.monotonic() - started, 3)

        # Stable output: sort by (component, version) so diffs are
        # deterministic across runs.
        results.sort(
            key=lambda r: (r.get("component") or "", r.get("version") or "")
        )
        succeeded = [r for r in results if r.get("ok")]
        failed = [r for r in results if not r.get("ok")]

        return {
            "ok": len(failed) == 0,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": session.region_name,
            "env": env,
            "requested_count": len(components or []),
            "resolved_count": len(parsed_specs),
            "cluster_count": len(clusters),
            "success_count": len(succeeded),
            "failure_count": len(failed),
            "workers": workers,
            "total_duration_seconds": duration,
            "malformed": malformed,
            "results": results,
        }

    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "env": env,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "error": str(exc),
        }


@mcp.tool()
def get_asg_pool_status(
    profile_name: str,
    env: str,
    version: str,
    component: str,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Return the status of the ASG pool(s) backing a MyCompany component.

    **Single-component tool.** If the caller has (or is about to build) a
    list of components / pools to check — or the user asks anything about
    rollout, refresh progress, revisions, or drift across MULTIPLE pools
    — do NOT loop this tool. Use ``describe_env_instances`` instead: it
    returns every EC2 in the env in a single paginated response with
    `asg_name`, `lv_component_revision`, `asg_lifecycle_state`, and full
    tags per row. Group by `asg_name` in memory to answer any per-pool
    question (e.g. `% new = count(rows where
    lv_component_revision == <new>) / count(rows)` per pool). One call
    for 46 pools instead of 46.

    ``profile_name`` accepts a substring (see ``awsume_refresh_session`` for the
    resolution contract).

    Matches ASGs whose name **contains** ``<component>-<major>`` and **ends with**
    ``-asg-<env>`` (mirrors the pattern used by deploy_tags_ecs_asg.py). Also
    honors ``ASG_CORE_OVERRIDES`` — passing ``component="ftpin"`` resolves
    ``sftp-2ftpin-asg-<env>`` even though the ASG name doesn't literally
    contain ``ftpin-2``. Both the standard token and the override core name
    are tried on every ASG.

    For each matched pool returns min/max/desired plus each running instance's id,
    type, state, private IP, and its ``lv_component_revision`` tag. The pool
    also carries a top-level ``revision`` field (first running instance's
    ``lv_component_revision``) so callers don't need to peek into
    ``running_instances`` to correlate with GitLab. Read-only.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    denied = _reject_if_sso(profile_name, "get_asg_pool_status")
    if denied:
        return denied

    short_v = _short_version(version)
    token = f"{component}-{short_v}"
    # Also match the override core name (e.g. component=ftpin →
    # override_core=sftp-2ftpin) so aliased ASG pools are found on the same
    # call. ``token`` still applies to the majority of components.
    override_core = _ASG_CORE_OVERRIDES_REVERSE.get(component)
    suffix = f"-asg-{env}"
    pattern = f"contains('{token}') && ends_with('{suffix}')"
    if override_core:
        pattern += f" || contains('{override_core}')"

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        autoscaling = session.client("autoscaling")
        ec2 = session.client("ec2")

        matched_asgs: list[dict[str, Any]] = []
        paginator = autoscaling.get_paginator("describe_auto_scaling_groups")
        for page in paginator.paginate():
            for asg in page.get("AutoScalingGroups", []):
                name = asg.get("AutoScalingGroupName", "")
                if not name.endswith(suffix):
                    continue
                if token in name or (override_core and override_core in name):
                    matched_asgs.append(asg)

        if not matched_asgs:
            return {
                "ok": False,
                "profile_name": profile_name,
                "session_profile": _resolve_session_profile(profile_name, session_profile or None),
                "region": session.region_name,
                "pattern": pattern,
                "pool_count": 0,
                "pools": [],
                "error": f"No ASG found matching {pattern}.",
            }

        pools: list[dict[str, Any]] = []
        for asg in matched_asgs:
            name = asg["AutoScalingGroupName"]
            instances_res = ec2.describe_instances(
                Filters=[
                    {"Name": "tag:aws:autoscaling:groupName", "Values": [name]},
                    {"Name": "instance-state-name", "Values": ["running"]},
                ]
            )
            running: list[dict[str, Any]] = []
            for res in instances_res.get("Reservations", []):
                for ins in res.get("Instances", []):
                    ins_tags = ins.get("Tags")
                    running.append(
                        {
                            "instance_id": ins.get("InstanceId"),
                            "instance_type": ins.get("InstanceType"),
                            "state": (ins.get("State") or {}).get("Name"),
                            "private_ip": ins.get("PrivateIpAddress"),
                            "lv_component_version": _tag_value(
                                ins_tags, _LV_COMPONENT_VERSION_TAG
                            ),
                            "lv_component_revision": _tag_value(
                                ins_tags, _LV_COMPONENT_REVISION_TAG
                            ),
                        }
                    )
            # Pool-level version + revision come from the ASG's
            # ``lv_component_version`` / ``lv_component_revision`` tags
            # (already fetched by describe_auto_scaling_groups). Falls
            # back to the first running instance's value if the ASG-level
            # tag is missing.
            pool_version: str | None = _asg_version_from_tags(asg) or next(
                (r["lv_component_version"] for r in running if r["lv_component_version"]),
                None,
            )
            pool_revision: str | None = _asg_revision_from_tags(asg) or next(
                (r["lv_component_revision"] for r in running if r["lv_component_revision"]),
                None,
            )
            pools.append(
                {
                    "asg_name": name,
                    "version": pool_version,
                    "revision": pool_revision,
                    "min_size": asg.get("MinSize"),
                    "max_size": asg.get("MaxSize"),
                    "desired_capacity": asg.get("DesiredCapacity"),
                    "instance_count": len(running),
                    "running_instances": running,
                }
            )

        return {
            "ok": True,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": session.region_name,
            "pattern": pattern,
            "pool_count": len(pools),
            "pools": pools,
        }

    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "session_profile": _resolve_session_profile(profile_name, session_profile or None),
                    "pattern": pattern,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "pattern": pattern,
            "error": str(exc),
        }


@mcp.tool()
def list_env_components(
    profile_name: str,
    env: str,
    version: str = "",
    include_revision: bool = True,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List every MyCompany component discovered in AWS for the given env.

    Scans both ECS and ASG:

    * ECS clusters matching ``fargate-u<major>-cluster-<env>``. For each
      matching cluster the tool enumerates services and extracts the component
      name from the service name (``<component>-u<major>-service-<env>``),
      applying ``ECS_COMPONENT_MAP`` (e.g. ``emailgateway`` → ``email_gateway``).
    * Auto Scaling Groups whose name ends with ``-asg-<env>``. Parses
      ``(component, version)`` from the pool name using MyCompany conventions
      (including ``ASG_CORE_OVERRIDES`` for irregular pools like
      ``sftp-2ftpatg`` and ``ASG_COMPONENT_MAP`` for renames like
      ``recordingpurge`` → ``recording_purge``). The pool's ``Version`` /
      ``lv_version`` tag, when present, is treated as the authoritative
      version and takes precedence over the version parsed from the name.

    ``version`` is optional. When provided (e.g. ``27.0`` or ``27``) only
    components whose major version matches are returned.

    ``profile_name`` accepts a substring (see ``awsume_refresh_session``).

    **Version + revision are included by default.** MyCompany stamps the
    same two tags on both ASG and ECS resources:

      * ``lv_component_version`` — the branch (e.g. ``27.0``).
      * ``lv_component_revision`` — the full patch version (e.g. ``27.0.24``).

    Each ECS and ASG row carries both a ``version`` and ``revision``
    field read straight from these tags. The merged ``components`` row
    carries ``revisions: {ecs?, asg?}`` so callers can correlate against
    GitLab's ``oss_component_svn_revision`` / ``lv_component_build`` in
    one shot — no per-component fan-out via ``get_asg_pool_status`` /
    ``get_ecs_service_status``. No image parsing, no task-definition
    lookups:

      * ASG → both tags on the ASG itself (already returned by
        ``describe_auto_scaling_groups``). Zero extra AWS calls. Falls
        back to a single bulk ``ec2.describe_instances`` for any ASG
        where either tag is missing at the pool level.
      * ECS → both tags on the ECS service, read via batched
        ``describe_services(include=['TAGS'])`` (10 services per call,
        AWS hard limit).

    Set ``include_revision=False`` if you only need the component list
    and want to skip the tag reads entirely (revision gets set to
    ``None`` and ``version`` falls back to what's parseable from the
    resource name).

    Read-only.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    denied = _reject_if_sso(profile_name, "list_env_components")
    if denied:
        return denied

    short_v = _short_version(version) if version else ""
    asg_suffix = f"-asg-{env}"
    cluster_pattern = re.compile(rf"^fargate-u\d+-cluster-{re.escape(env)}$")

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        ecs = session.client("ecs")
        autoscaling = session.client("autoscaling")

        # ── ECS ─────────────────────────────────────────────────────────────
        ecs_items: list[dict[str, Any]] = []
        candidate_clusters: list[str] = []
        for page in ecs.get_paginator("list_clusters").paginate():
            for arn in page.get("clusterArns", []):
                name = arn.split("/")[-1]
                if not cluster_pattern.match(name):
                    continue
                if short_v and name != f"fargate-u{short_v}-cluster-{env}":
                    continue
                candidate_clusters.append(name)

        # Track services per cluster so we can bulk-fetch revisions after.
        cluster_to_services: dict[str, list[str]] = {c: [] for c in candidate_clusters}
        for cluster in candidate_clusters:
            cluster_version = _extract_ecs_version_from_cluster(cluster)
            for page in ecs.get_paginator("list_services").paginate(cluster=cluster):
                for arn in page.get("serviceArns", []):
                    svc_name = arn.split("/")[-1]
                    cluster_to_services[cluster].append(svc_name)
                    ecs_items.append(
                        {
                            "component": _extract_ecs_component(svc_name),
                            "version": cluster_version,
                            "revision": None,
                            "cluster": cluster,
                            "service": svc_name,
                        }
                    )

        # ── ASG ─────────────────────────────────────────────────────────────
        asg_items: list[dict[str, Any]] = []
        matched_asg_objects: list[dict[str, Any]] = []
        unparseable: list[str] = []
        for page in autoscaling.get_paginator("describe_auto_scaling_groups").paginate():
            for asg in page.get("AutoScalingGroups", []):
                asg_name = asg.get("AutoScalingGroupName", "")
                if not asg_name.endswith(asg_suffix):
                    continue
                parsed_component, parsed_version = _parse_asg_name(asg_name, env)
                if not parsed_component:
                    unparseable.append(asg_name)
                    continue
                # Prefer the authoritative ``lv_component_version`` tag
                # MyCompany stamps on every ASG. Fall back to the legacy
                # ``Version`` / ``lv_version`` tag and then to the parsed
                # name so older pools still resolve.
                lv_component_version = _asg_version_from_tags(asg)
                legacy_tag_version = _extract_asg_version_tag(asg.get("Tags"))
                effective_version = (
                    lv_component_version or legacy_tag_version or parsed_version
                )
                if short_v and _short_version(effective_version) != short_v:
                    continue
                asg_items.append(
                    {
                        "component": parsed_component,
                        "version": effective_version,
                        "revision": None,
                        "parsed_version": parsed_version,
                        "tag_version": lv_component_version or legacy_tag_version,
                        "asg_name": asg_name,
                        "min_size": asg.get("MinSize"),
                        "max_size": asg.get("MaxSize"),
                        "desired_capacity": asg.get("DesiredCapacity"),
                    }
                )
                # Keep the raw ASG dict alongside the parsed row so the
                # tag fetch below can read ``lv_component_version`` /
                # ``lv_component_revision`` directly (no extra AWS call).
                matched_asg_objects.append(asg)

        # ── Bulk tag fan-out ────────────────────────────────────────────────
        # Both sides read the ``lv_component_version`` and
        # ``lv_component_revision`` TAGS — no image parsing, no
        # task-definition lookups. Same tags on both sides.
        #   * ASG → tags live on the ASG itself (already fetched by
        #     ``describe_auto_scaling_groups`` above). Fallback: single
        #     ``ec2.describe_instances`` for any ASG missing the tags.
        #   * ECS → tags live on the service. Batched
        #     ``describe_services(include=['TAGS'])`` (10 per call, AWS
        #     hard limit) gets everything in one round-trip per batch.
        if include_revision:
            # ASG: read straight from the ASG tags we already have.
            asg_tags_by_name: dict[str, dict[str, str | None]] = {}
            for asg_obj in matched_asg_objects:
                asg_tags_by_name[asg_obj["AutoScalingGroupName"]] = {
                    "version": _asg_version_from_tags(asg_obj),
                    "revision": _asg_revision_from_tags(asg_obj),
                }
            # Belt-and-suspenders fallback: for any ASG missing either
            # tag at the pool level, look at running instance tags in a
            # SINGLE bulk EC2 call.
            missing = [
                name
                for name, tags in asg_tags_by_name.items()
                if not tags.get("revision") or not tags.get("version")
            ]
            if missing:
                ec2 = session.client("ec2")
                fallback = _bulk_asg_tags_from_instances(ec2, missing)
                for name, tags in fallback.items():
                    slot = asg_tags_by_name[name]
                    if not slot.get("version") and tags.get("version"):
                        slot["version"] = tags["version"]
                    if not slot.get("revision") and tags.get("revision"):
                        slot["revision"] = tags["revision"]
            for item in asg_items:
                tags = asg_tags_by_name.get(item["asg_name"], {})
                item["revision"] = tags.get("revision")
                # Let the ``lv_component_version`` tag win if it disagrees
                # with what we picked earlier (rare — earlier pick already
                # tried this tag first).
                if tags.get("version"):
                    item["version"] = tags["version"]
            if any(cluster_to_services.values()):
                ecs_tags = _bulk_ecs_tags(ecs, cluster_to_services)
                for item in ecs_items:
                    tags = ecs_tags.get((item["cluster"], item["service"]), {})
                    item["revision"] = tags.get("revision")
                    if tags.get("version"):
                        item["version"] = tags["version"]

        # ── Merge: one row per (component, version) with the deployment types
        # AND per-type revision map so callers can correlate against GitLab
        # in a single tool response.
        merged_types: dict[tuple[str, str], set[str]] = {}
        merged_revisions: dict[tuple[str, str], dict[str, str | None]] = {}
        for item in ecs_items:
            key = (item["component"], item["version"])
            merged_types.setdefault(key, set()).add("ecs")
            if item.get("revision"):
                # First non-null wins if multiple services share the key.
                merged_revisions.setdefault(key, {}).setdefault(
                    "ecs", item["revision"]
                )
        for item in asg_items:
            key = (item["component"], item["version"])
            merged_types.setdefault(key, set()).add("asg")
            if item.get("revision"):
                merged_revisions.setdefault(key, {}).setdefault(
                    "asg", item["revision"]
                )

        components = [
            {
                "component": comp,
                "version": ver,
                "types": sorted(types),
                "revisions": merged_revisions.get((comp, ver), {}),
            }
            for (comp, ver), types in sorted(merged_types.items())
        ]

        return {
            "ok": True,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": session.region_name,
            "env": env,
            "version_filter": version or None,
            "include_revision": include_revision,
            "component_count": len(components),
            "ecs_count": len(ecs_items),
            "asg_count": len(asg_items),
            "asg_unparseable_count": len(unparseable),
            "components": components,
            "ecs": sorted(ecs_items, key=lambda x: (x["component"], x["version"])),
            "asg": sorted(asg_items, key=lambda x: (x["component"], x["version"])),
            "asg_unparseable": unparseable,
        }

    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "session_profile": _resolve_session_profile(profile_name, session_profile or None),
                    "env": env,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "env": env,
            "error": str(exc),
        }


# ── Refresh: find + execute ──────────────────────────────────────────────────

def _find_ecs_targets(
    ecs_client,
    env: str,
    keyword: str,
    version: str,
) -> list[dict[str, Any]]:
    """Return ECS services matching ``keyword`` (case-insensitive substring) in
    the given env. If ``version`` is set, restrict to that major-version cluster.
    """
    short_v = _short_version(version) if version else ""
    keyword_lower = keyword.lower()
    matching_clusters: list[str] = []
    for page in ecs_client.get_paginator("list_clusters").paginate():
        for arn in page.get("clusterArns", []):
            name = arn.split("/")[-1]
            m = _ECS_CLUSTER_VERSION_RE.search(name)
            if not m or not name.endswith(f"-cluster-{env}"):
                continue
            if short_v and m.group(1) != short_v:
                continue
            matching_clusters.append(name)

    results: list[dict[str, Any]] = []
    for cluster in matching_clusters:
        for page in ecs_client.get_paginator("list_services").paginate(cluster=cluster):
            for svc_arn in page.get("serviceArns", []):
                svc_name = svc_arn.split("/")[-1]
                if keyword_lower not in svc_name.lower():
                    continue
                svc_version = _extract_ecs_version_from_cluster(cluster)
                results.append(
                    {
                        "type": "ecs",
                        "cluster": cluster,
                        "service": svc_name,
                        "target": f"{cluster}/{svc_name}",
                        "component": _extract_ecs_component(svc_name),
                        "version": svc_version,
                    }
                )
    return results


def _find_asg_targets(
    autoscaling_client,
    env: str,
    keyword: str,
    version: str,
) -> list[dict[str, Any]]:
    """Return ASG pools matching ``keyword`` in the given env. If ``version``
    is set, restrict to pools whose (tag_version or parsed_version) matches
    the requested major version."""
    short_v = _short_version(version) if version else ""
    keyword_lower = keyword.lower()
    asg_suffix = f"-asg-{env}"

    results: list[dict[str, Any]] = []
    for page in autoscaling_client.get_paginator("describe_auto_scaling_groups").paginate():
        for asg in page.get("AutoScalingGroups", []):
            name = asg.get("AutoScalingGroupName", "")
            if not name.endswith(asg_suffix) or keyword_lower not in name.lower():
                continue
            parsed_component, parsed_version = _parse_asg_name(name, env)
            tag_version = _extract_asg_version_tag(asg.get("Tags"))
            effective_version = tag_version or parsed_version
            if short_v and _short_version(effective_version) != short_v:
                continue
            results.append(
                {
                    "type": "asg",
                    "asg_name": name,
                    "target": name,
                    "component": parsed_component or name,
                    "version": effective_version,
                    "parsed_version": parsed_version,
                    "tag_version": tag_version,
                    "min_size": asg.get("MinSize"),
                    "max_size": asg.get("MaxSize"),
                    "desired_capacity": asg.get("DesiredCapacity"),
                }
            )
    return results


@mcp.tool()
def find_refresh_targets(
    profile_name: str,
    env: str,
    component: str,
    type: str,
    version: str = "",
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Discover ECS services or ASG pools that would be refreshed.

    Read-only. Use this before ``refresh_targets`` to show the user exactly
    which resources would be touched, get their confirmation, then call
    ``refresh_targets`` with the ``target`` strings from the ``targets`` array
    the user approves.

    Args:
        profile_name: AWS profile (substring accepted; see ``awsume_refresh_session``).
        env: Environment code. Must be one of ``au1, eu1, perf1, stg4, tst2, tst3, uk2, uk3``.
        component: Case-insensitive substring to match against ECS service
            names or ASG pool names (e.g. ``acm``, ``agent-screenrecording``).
        type: ``"ecs"`` or ``"asg"``.
        version: Optional major version filter (``"27.0"`` or ``"27"``).

    Returns:
        A payload with a ``targets`` array; each item has ``target`` (the
        exact string to pass to ``refresh_targets``), plus ``component``,
        ``version``, and context fields.
    """
    if type not in ("ecs", "asg"):
        return {"ok": False, "error": "type must be 'ecs' or 'asg'."}

    denied = _reject_non_refreshable_env(env)
    if denied:
        return denied

    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    sso_denied = _reject_if_sso(profile_name, "find_refresh_targets")
    if sso_denied:
        return sso_denied

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        if type == "ecs":
            targets = _find_ecs_targets(session.client("ecs"), env, component, version)
        else:
            targets = _find_asg_targets(session.client("autoscaling"), env, component, version)

        return {
            "ok": True,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": session.region_name,
            "env": env,
            "type": type,
            "component_query": component,
            "version_filter": version or None,
            "match_count": len(targets),
            "targets": targets,
            "instruction": (
                "Present these targets to the user and ask for explicit confirmation. "
                "On approval, call refresh_targets with the same profile_name, env, type, "
                "and the exact `target` strings from the approved entries. Never expand "
                "or fabricate the list."
            ),
        }

    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "session_profile": _resolve_session_profile(profile_name, session_profile or None),
                    "env": env,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "env": env,
            "error": str(exc),
        }


def _resolve_ecs_cluster_for_service(service: str, env: str) -> str | None:
    """Given ``acm-u27-service-tst3``, return ``fargate-u27-cluster-tst3``."""
    m = _ECS_SERVICE_VERSION_RE.search(service)
    if not m:
        return None
    return f"fargate-u{m.group(1)}-cluster-{env}"


def _refresh_one_ecs(ecs_client, env: str, target: str) -> dict[str, Any]:
    """Refresh a single ECS service. ``target`` may be ``cluster/service`` or ``service``."""
    if "/" in target:
        cluster, service = target.split("/", 1)
    else:
        service = target
        cluster = _resolve_ecs_cluster_for_service(service, env) or ""
    if not cluster:
        return {"target": target, "ok": False, "error": f"Could not resolve cluster for service '{service}'."}
    try:
        ecs_client.update_service(cluster=cluster, service=service, forceNewDeployment=True)
        return {
            "target": target,
            "cluster": cluster,
            "service": service,
            "ok": True,
            "action": "force_new_deployment",
        }
    except (ClientError, BotoCoreError) as exc:
        return {"target": target, "cluster": cluster, "service": service, "ok": False, "error": str(exc)}


def _refresh_one_asg(autoscaling_client, target: str) -> dict[str, Any]:
    """Refresh a single ASG pool with a rolling instance refresh."""
    try:
        resp = autoscaling_client.start_instance_refresh(
            AutoScalingGroupName=target,
            Strategy="Rolling",
            Preferences=_ASG_REFRESH_PREFERENCES,
        )
        return {
            "target": target,
            "asg_name": target,
            "ok": True,
            "action": "start_instance_refresh",
            "instance_refresh_id": resp.get("InstanceRefreshId"),
        }
    except (ClientError, BotoCoreError) as exc:
        return {"target": target, "asg_name": target, "ok": False, "error": str(exc)}


@mcp.tool()
def refresh_targets(
    profile_name: str,
    env: str,
    type: str,
    targets: list[str],
    max_workers: int = 5,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Refresh the given ECS services or ASG pools in parallel within a single env.

    ECS → ``update-service --force-new-deployment``.
    ASG → ``start-instance-refresh --strategy Rolling`` with
    ``MinHealthyPercentage=50`` and ``InstanceWarmup=60``.

    Rules enforced by this tool:
      * ``env`` must be in the refresh whitelist (``au1, eu1, perf1, stg4, tst2, tst3, uk2, uk3``).
      * ``targets`` must be non-empty. Each string must have been produced by
        ``find_refresh_targets`` for the same env — do not fabricate names.
      * Parallelism is confined to this single call, so all refreshes share the
        same AWS profile (no cross-env profile switching mid-flight).

    Args:
        profile_name: AWS profile (substring accepted).
        env: Environment code.
        type: ``"ecs"`` or ``"asg"``.
        targets: For ECS, ``"cluster/service"`` or ``"service"`` strings. For
            ASG, the pool name.
        max_workers: Parallel workers (1..20). Default 5.

    Returns a per-target result plus success/failure counts.
    """
    if type not in ("ecs", "asg"):
        return {"ok": False, "error": "type must be 'ecs' or 'asg'."}
    if not targets:
        return {"ok": False, "error": "targets must be a non-empty list."}

    denied = _reject_non_refreshable_env(env)
    if denied:
        return denied

    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    sso_denied = _reject_if_sso(profile_name, "refresh_targets")
    if sso_denied:
        return sso_denied

    workers = max(1, min(int(max_workers or 1), 20))

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        client = session.client("ecs" if type == "ecs" else "autoscaling")

        def _do(t: str) -> dict[str, Any]:
            if type == "ecs":
                return _refresh_one_ecs(client, env, t)
            return _refresh_one_asg(client, t)

        results: list[dict[str, Any]] = []
        # boto3 clients are thread-safe; safe to share across workers.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_do, t): t for t in targets}
            for fut in as_completed(futures):
                results.append(fut.result())

        succeeded = [r for r in results if r.get("ok")]
        failed = [r for r in results if not r.get("ok")]
        return {
            "ok": len(failed) == 0,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": session.region_name,
            "env": env,
            "type": type,
            "requested_count": len(targets),
            "success_count": len(succeeded),
            "failure_count": len(failed),
            "workers": workers,
            "results": results,
        }

    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "session_profile": _resolve_session_profile(profile_name, session_profile or None),
                    "env": env,
                    "type": type,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "env": env,
            "type": type,
            "error": str(exc),
        }


# ── CloudWatch Logs (component log groups) ──────────────────────────────────
# MyCompany log groups follow the convention ``/<env>/<component>_<branch>``,
# e.g. ``/au1/ACD_27.0``, ``/au1/agent-compliance_27.0``, ``/au1/inputfilter_8.0``.
# Everything below wraps CloudWatch Logs Insights so the AI host can answer:
#   * "pull the latest errors from these components"
#   * "count errors in the last N minutes/days"
#   * "look for the text '...' in <component> on <version>"
#   * "latest healthcheck results for <component>"
# The query is bounded (log groups, time window, row limit, wait budget) and
# the tool is READ-ONLY — it only calls ``logs:StartQuery`` /
# ``logs:GetQueryResults`` / ``logs:DescribeLogGroups`` / ``logs:StopQuery``.

# A MyCompany component version is ``N.N`` (rarely ``N.N.N``). Log-group names
# use this literal after the trailing ``_`` (``.../ACD_27.0``,
# ``.../email_gateway_27.0``). This regex greedily captures the component
# so multi-word names like ``agent-compliance`` and ``email_gateway`` stay
# intact and only the trailing ``_<version>`` is stripped.
_LOG_GROUP_COMPONENT_RE = re.compile(r"^(.+)_(\d+\.\d+(?:\.\d+)?)$")
_LOG_GROUP_PATH_RE = re.compile(
    r"^/?(?P<env>[^/]+)/(?P<component>.+)_(?P<version>\d+\.\d+(?:\.\d+)?)$"
)

# Substring pattern for the ``error_only=True`` shortcut. Case-insensitive
# match on the message field via CloudWatch Insights ``(?i)`` regex prefix.
_ERROR_PATTERN_KEYWORDS = "ERROR|Exception|FATAL|SEVERE|Traceback"

# Hard caps for query safety.
_LOGS_MAX_ROW_LIMIT = 10000
_LOGS_MAX_LOG_GROUPS_PER_QUERY = 50  # CloudWatch Logs Insights hard limit
_LOGS_MAX_WAIT_SECONDS = 120


def _log_group_name(env: str, component: str, version: str) -> str:
    """Compose the standard MyCompany log-group path."""
    return f"/{env.strip('/')}/{component}_{version}"


def _parse_component_specs(specs: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize component specs to ``[{'name': str, 'version': Optional[str]}]``.

    Accepts, per element:
      * ``{"name": "ACD", "version": "27.0"}`` — exact.
      * ``{"name": "ACD"}`` — version omitted; caller will match all versions.
      * ``"ACD_27.0"`` — parsed via the log-group filename convention.
      * ``"ACD"`` — bare component name, all versions.
      * ``"agent-compliance_27.0"`` — hyphenated names are fine; only the
        trailing ``_<major.minor>`` is stripped.

    Returns ``(parsed, malformed)`` where ``malformed`` collects entries the
    tool couldn't interpret (so the AI can surface them without failing the
    whole call).
    """
    parsed: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for entry in specs or []:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            version = entry.get("version")
            version_str = str(version).strip() if version not in (None, "") else None
            if not name:
                malformed.append({"input": entry, "error": "component spec missing `name`."})
                continue
            parsed.append({"name": name, "version": version_str})
            continue

        if isinstance(entry, str):
            candidate = entry.strip()
            if not candidate:
                continue
            m = _LOG_GROUP_COMPONENT_RE.match(candidate)
            if m:
                parsed.append({"name": m.group(1), "version": m.group(2)})
            else:
                parsed.append({"name": candidate, "version": None})
            continue

        malformed.append({"input": entry, "error": "component spec must be a dict or a string."})
    return parsed, malformed


def _parse_iso_datetime(value: str) -> datetime | None:
    """Accept RFC3339 / ISO-8601 strings, return a tz-aware UTC datetime.
    Returns ``None`` on failure so callers can fall back to defaults.
    Naive datetimes are assumed to be UTC.
    """
    if not value or not str(value).strip():
        return None
    stripped = str(value).strip()
    if stripped.endswith("Z"):
        stripped = stripped[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(stripped)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_time_window(
    since_minutes: int,
    since_hours: int,
    since_days: int,
    start_time: str = "",
    end_time: str = "",
) -> tuple[int, int, dict[str, Any]]:
    """Return ``(start_epoch, end_epoch, meta)``. Defaults to the last 60 minutes.

    Priority when multiple are set:
      1. Explicit ``start_time`` / ``end_time`` (ISO-8601 / RFC3339) — if
         either is provided, they win. The other side falls back to a
         sensible default (``now`` for end, ``end - 1h`` for start).
      2. ``since_minutes`` > ``since_hours`` > ``since_days`` — finest wins,
         so a caller that specifies minutes gets what they asked for even
         if the AI also fills in a default hours value.
      3. If none set → last 60 minutes.
    """
    now = datetime.now(timezone.utc)
    explicit_start = _parse_iso_datetime(start_time)
    explicit_end = _parse_iso_datetime(end_time)

    if explicit_start or explicit_end:
        end_dt = explicit_end or now
        start_dt = explicit_start or (end_dt - timedelta(hours=1))
        start_epoch = int(start_dt.timestamp())
        end_epoch = int(end_dt.timestamp())
        return start_epoch, end_epoch, {
            "lookback": "explicit",
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "seconds": max(0, end_epoch - start_epoch),
        }

    if since_minutes and since_minutes > 0:
        seconds = int(since_minutes) * 60
        label = f"{since_minutes}m"
    elif since_hours and since_hours > 0:
        seconds = int(since_hours) * 3600
        label = f"{since_hours}h"
    elif since_days and since_days > 0:
        seconds = int(since_days) * 86400
        label = f"{since_days}d"
    else:
        seconds = 3600
        label = "60m (default)"

    end_epoch = int(now.timestamp())
    start_epoch = end_epoch - seconds
    return start_epoch, end_epoch, {"lookback": label, "seconds": seconds}


def _list_env_log_groups(logs_client, env: str) -> list[dict[str, Any]]:
    """Return every log group whose name starts with ``/<env>/``."""
    env_prefix = f"/{env.strip('/')}/"
    paginator = logs_client.get_paginator("describe_log_groups")
    groups: list[dict[str, Any]] = []
    for page in paginator.paginate(logGroupNamePrefix=env_prefix):
        for lg in page.get("logGroups", []):
            name = lg.get("logGroupName") or ""
            if name.startswith(env_prefix):
                groups.append(
                    {
                        "log_group_name": name,
                        "stored_bytes": lg.get("storedBytes"),
                        "retention_in_days": lg.get("retentionInDays"),
                    }
                )
    return groups


def _fuzzy_match_log_groups(
    requested: str, log_groups: list[dict[str, Any]], limit: int = 8
) -> list[dict[str, Any]]:
    """Rank log-group names by similarity to ``requested`` (a component or
    full group path)."""
    if not requested or not log_groups:
        return []
    req_lower = requested.lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for lg in log_groups:
        name = (lg.get("log_group_name") or "").lower()
        if not name:
            continue
        score = difflib.SequenceMatcher(None, req_lower, name).ratio()
        if req_lower in name or name.endswith("/" + req_lower):
            score = max(score, 0.9)
        scored.append((score, lg))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"log_group_name": lg["log_group_name"], "similarity": round(score, 3)}
        for score, lg in scored[:limit]
        if score >= 0.4
    ]


def _resolve_component_log_groups(
    logs_client, env: str, specs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn parsed component specs into (matched_log_groups, unresolved, all_env_groups).

    ``matched_log_groups`` contains dicts ``{component, version, log_group_name}``
    for everything we could find. ``unresolved`` contains dicts with fuzzy
    candidates for anything that didn't match. ``all_env_groups`` is the
    cached env listing so callers can surface it to the user.
    """
    all_groups = _list_env_log_groups(logs_client, env)
    all_names = [g["log_group_name"] for g in all_groups]
    matched: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for spec in specs:
        name = spec["name"]
        version = spec.get("version")
        expected_prefix = f"/{env.strip('/')}/{name}_"

        if version:
            expected = _log_group_name(env, name, version)
            if expected in all_names:
                matched.append(
                    {"component": name, "version": version, "log_group_name": expected}
                )
                continue
            unresolved.append(
                {
                    "requested": {"name": name, "version": version},
                    "expected_log_group": expected,
                    "candidates": _fuzzy_match_log_groups(f"{name}_{version}", all_groups),
                }
            )
            continue

        # No version — pick every log group whose name starts with
        # ``/{env}/{name}_``. Parse the trailing version out of each hit.
        hits: list[dict[str, Any]] = []
        for group_name in all_names:
            if not group_name.startswith(expected_prefix):
                continue
            parsed = _LOG_GROUP_PATH_RE.match(group_name)
            if parsed and parsed.group("component") == name:
                hits.append(
                    {
                        "component": name,
                        "version": parsed.group("version"),
                        "log_group_name": group_name,
                    }
                )
        if hits:
            matched.extend(hits)
        else:
            unresolved.append(
                {
                    "requested": {"name": name, "version": None},
                    "expected_log_group_prefix": expected_prefix,
                    "candidates": _fuzzy_match_log_groups(name, all_groups),
                }
            )

    return matched, unresolved, all_groups


def _format_filter_clause(field: str, value: Any) -> str | None:
    """Compose a single CloudWatch Insights ``filter <field> ...`` clause.

    Value handling:
      * ``bool`` → ``field = true`` / ``field = false``
      * ``int`` / ``float`` → ``field = <value>``
      * ``list`` / ``tuple`` → ``field in [<v1>, <v2>, ...]`` with per-item
        quoting for strings and bare literals for numbers.
      * everything else → ``field = "<str value>"`` (quotes escaped).
    """
    field = (field or "").strip()
    if not field:
        return None
    if isinstance(value, bool):
        return f"filter {field} = {'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"filter {field} = {value}"
    if isinstance(value, (list, tuple)):
        rendered: list[str] = []
        for item in value:
            if isinstance(item, bool):
                rendered.append("true" if item else "false")
            elif isinstance(item, (int, float)):
                rendered.append(str(item))
            else:
                escaped = str(item).replace('"', r'\"')
                rendered.append(f'"{escaped}"')
        if not rendered:
            return None
        return f"filter {field} in [{', '.join(rendered)}]"
    escaped = str(value).replace('"', r'\"')
    return f'filter {field} = "{escaped}"'


def _build_insights_query(
    pattern: str,
    error_only: bool,
    count_only: bool,
    limit: int,
    filters: dict[str, Any] | None = None,
    raw_filter: str = "",
    group_by: str = "",
    extra_fields: list[str] | None = None,
) -> str:
    """Compose a CloudWatch Logs Insights query for the given filters.

    Semantics:
      * ``pattern`` — literal substring on ``@message`` (case-insensitive,
        regex-escaped so users' literal strings work as-is).
      * ``error_only`` — shortcut ``ERROR|Exception|FATAL|SEVERE|Traceback``
        AND-filter on ``@message``.
      * ``filters`` — dict of ``{json_field: value}`` for exact-match /
        ``in [...]`` filters against the auto-parsed JSON fields
        (``logType``, ``logLevel``, ``statusCode``, ``traceId``, …).
      * ``raw_filter`` — a single Insights filter expression appended
        verbatim (e.g. ``"statusCode >= 400"``), for range / compound
        conditions the dict form can't express.
      * ``count_only`` + ``group_by`` — switches to
        ``stats count() as matches by <group_by>``, ordered by count desc.
      * ``extra_fields`` — additional JSON field names to project on
        per-row output (ignored in count mode).
    """
    filter_clauses: list[str] = []
    if pattern and pattern.strip():
        escaped = re.escape(pattern.strip())
        filter_clauses.append(f"filter @message like /(?i){escaped}/")
    if error_only:
        filter_clauses.append(f"filter @message like /(?i){_ERROR_PATTERN_KEYWORDS}/")
    for key, value in (filters or {}).items():
        clause = _format_filter_clause(key, value)
        if clause:
            filter_clauses.append(clause)
    if raw_filter and raw_filter.strip():
        stripped = raw_filter.strip()
        # Add the ``filter`` prefix if the caller didn't include it.
        if not stripped.lower().startswith("filter "):
            stripped = f"filter {stripped}"
        filter_clauses.append(stripped)

    row_limit = max(1, min(int(limit), _LOGS_MAX_ROW_LIMIT))

    if count_only:
        parts: list[str] = ["fields @timestamp, @message", *filter_clauses]
        gb = (group_by or "").strip()
        if gb:
            parts.append(f"stats count() as matches by {gb}")
            parts.append("sort matches desc")
            parts.append(f"limit {row_limit}")
        else:
            parts.append("stats count() as matches")
        return "\n| ".join(parts)

    projected = ["@timestamp", "@message", "@logStream", "@log"]
    for f in (extra_fields or []):
        name = (f or "").strip()
        if name and name not in projected:
            projected.append(name)
    parts = [
        "fields " + ", ".join(projected),
        *filter_clauses,
        "sort @timestamp desc",
        f"limit {row_limit}",
    ]
    return "\n| ".join(parts)


def _wait_for_insights_query(
    logs_client, query_id: str, max_wait_seconds: int
) -> dict[str, Any]:
    """Poll ``get_query_results`` until the query is done or the local
    deadline expires. Cancels the query on local timeout so we don't leave
    an in-flight query bill running."""
    wait_budget = max(1, min(int(max_wait_seconds), _LOGS_MAX_WAIT_SECONDS))
    deadline = time.monotonic() + wait_budget
    result: dict[str, Any] = {}
    while True:
        result = logs_client.get_query_results(queryId=query_id)
        status = (result.get("status") or "").strip()
        if status in ("Complete", "Failed", "Cancelled", "Timeout"):
            return result
        if time.monotonic() >= deadline:
            try:
                logs_client.stop_query(queryId=query_id)
            except (ClientError, BotoCoreError):
                pass
            result["_timed_out_locally"] = True
            result["_wait_budget_seconds"] = wait_budget
            return result
        time.sleep(0.75)


def _flatten_insights_row(row: list[dict[str, Any]]) -> dict[str, Any]:
    """CloudWatch returns each row as ``[{field, value}, ...]``. Flatten to
    a plain dict and split ``@log`` into just the log-group name if present."""
    out: dict[str, Any] = {}
    for cell in row:
        field = cell.get("field")
        value = cell.get("value")
        if field is None:
            continue
        out[field] = value
    log_ref = out.get("@log")
    if isinstance(log_ref, str) and ":" in log_ref:
        # Format is "<account>:<log-group-name>"
        out["log_group_name"] = log_ref.split(":", 1)[1]
    return out


# ── CloudWatch Log Analytics deep-link builder ──────────────────────────────
# Log Analytics (the new CloudWatch console tab at ``#log-analytics``) accepts
# a query with the source log group(s), time window, and query body inlined
# into a single ``a.query`` param. Its hash-fragment state uses AWS Console's
# "crush" encoding — a rison-lite format where strings are prefixed with ``~'``
# and every character outside ``[A-Za-z0-9_.-]`` is escaped as ``*XX``
# (lowercase hex, e.g. space → ``*20``, newline → ``*0a``, ``@`` → ``*40``).
#
# We surface the deep link alongside every ``query_component_logs`` /
# ``bulk_query_component_logs`` response so the AI host can hand the user
# a click-through to the exact query in the console.

# Preserved char set for AWS Console crush encoding. Everything else → ``*XX``.
_CRUSH_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)

# Per-profile STS-derived account id cache. Fetched lazily the first time we
# build a Log Analytics URL for a given profile so we can compose the
# ``arn:aws:logs:<region>:<account>:log-group:<name>`` reference. Cheap
# single STS call, cached for the lifetime of the process.
_account_id_cache: dict[str, str] = {}


def _console_crush_encode(value: str) -> str:
    """Encode ``value`` using AWS Console's ``*XX`` hex escaping.

    Preserves ``[A-Za-z0-9_.-]``. Everything else (spaces, quotes,
    newlines, ``@``, ``/``, ``:``, ``|``, brackets, punctuation …) is
    replaced with ``*<lowercase hex>``. Characters outside the BMP are
    escaped byte-by-byte via UTF-8 so multibyte content still round-trips.
    """
    parts: list[str] = []
    for ch in value:
        if ch in _CRUSH_SAFE:
            parts.append(ch)
            continue
        code = ord(ch)
        if code < 0x80:
            parts.append(f"*{code:02x}")
        else:
            for byte in ch.encode("utf-8"):
                parts.append(f"*{byte:02x}")
    return "".join(parts)


def _iso_utc_millis(epoch_seconds: int) -> str:
    """Return ``YYYY-MM-DDTHH:MM:SS.000Z`` for Log Analytics START/END."""
    return datetime.fromtimestamp(
        int(epoch_seconds), tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _log_group_arn(region: str, account_id: str, log_group_name: str) -> str:
    return (
        f"arn:aws:logs:{region}:{account_id}:log-group:{log_group_name}"
    )


def _account_id_from_session(session, cache_key: str) -> str | None:
    """Return the account id for ``session``, cached by ``cache_key``.

    Returns ``None`` (silently) on failure so URL-building degrades to
    "no console link" instead of sinking the whole query response.
    """
    cached = _account_id_cache.get(cache_key)
    if cached:
        return cached
    try:
        account = (
            session.client("sts").get_caller_identity().get("Account")
        )
    except (ClientError, BotoCoreError):
        return None
    if account:
        _account_id_cache[cache_key] = account
        return account
    return None


def _build_log_analytics_url(
    region: str,
    account_id: str,
    log_group_names: list[str],
    start_epoch: int,
    end_epoch: int,
    query_body: str,
) -> str:
    """Compose the CloudWatch Log Analytics deep-link URL.

    The final query pasted into the console is::

        SOURCE "<arn>"[, "<arn>", ...] START=<iso> END=<iso> |
        <query_body>

    ``query_body`` is the pipeline the tool already builds
    (``fields ... | filter ... | sort ... | limit ...``). Multiple log
    groups are inlined into one SOURCE clause via comma-separated ARN
    strings so a single Log Analytics query covers the full match set.
    """
    arn_list = ", ".join(
        f'"{_log_group_arn(region, account_id, name)}"'
        for name in log_group_names
    )
    start_iso = _iso_utc_millis(start_epoch)
    end_iso = _iso_utc_millis(end_epoch)
    body = (query_body or "").lstrip()
    full_query = (
        f"SOURCE {arn_list} START={start_iso} END={end_iso} |\n{body}"
    )
    encoded = _console_crush_encode(full_query)
    query_id = str(uuid.uuid4())
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#log-analytics"
        f"?active=~'a"
        f"&a.id=~'{query_id}"
        f"&a.pos=~'0"
        f"&a.label=~'Query*201"
        f"&a.type=~'query"
        f"&a.tz=~'UTC"
        f"&a.query=~'{encoded}"
    )


@mcp.tool()
def list_component_log_groups(
    profile_name: str,
    env: str,
    component_filter: str = "",
    version_filter: str = "",
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List CloudWatch log groups under ``/<env>/`` for MyCompany components. **Read-only.**

    Log groups follow the convention ``/<env>/<component>_<version>``, e.g.
    ``/au1/ACD_27.0``. Use this tool to discover what components have logs
    in an env before calling ``query_component_logs`` — especially when the
    user isn't sure of the exact component name or version.

    Args:
        profile_name: AWS profile substring (see ``awsume_refresh_session``).
        env: Env folder (any env; this is read-only).
        component_filter: Optional case-insensitive substring on the
            component portion of the log-group name.
        version_filter: Optional exact version filter (e.g. ``"27.0"``).
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    denied = _reject_if_sso(profile_name, "list_component_log_groups")
    if denied:
        return denied

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        logs_client = session.client("logs")
        groups = _list_env_log_groups(logs_client, env)

        cf_lower = (component_filter or "").strip().lower()
        vf_exact = (version_filter or "").strip()

        parsed_rows: list[dict[str, Any]] = []
        for lg in groups:
            path = lg["log_group_name"]
            m = _LOG_GROUP_PATH_RE.match(path)
            component = m.group("component") if m else None
            version = m.group("version") if m else None
            if cf_lower and (not component or cf_lower not in component.lower()):
                continue
            if vf_exact and version != vf_exact:
                continue
            parsed_rows.append(
                {
                    "log_group_name": path,
                    "component": component,
                    "version": version,
                    "stored_bytes": lg.get("stored_bytes"),
                    "retention_in_days": lg.get("retention_in_days"),
                }
            )

        parsed_rows.sort(
            key=lambda r: (
                r["component"] or "",
                float(r["version"]) if r.get("version") else 0.0,
            )
        )
        return {
            "ok": True,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": session.region_name,
            "env": env,
            "component_filter": component_filter or None,
            "version_filter": version_filter or None,
            "match_count": len(parsed_rows),
            "log_groups": parsed_rows,
        }

    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "env": env,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "error": str(exc),
        }


@mcp.tool()
def query_component_logs(
    profile_name: str,
    env: str,
    components: list[Any],
    pattern: str = "",
    error_only: bool = False,
    since_minutes: int = 0,
    since_hours: int = 0,
    since_days: int = 0,
    start_time: str = "",
    end_time: str = "",
    limit: int = 100,
    count_only: bool = False,
    filters: dict[str, Any] | None = None,
    raw_filter: str = "",
    group_by: str = "",
    fields: list[str] | None = None,
    max_wait_seconds: int = 30,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Query CloudWatch Logs for one or more MyCompany components in a single env. **Read-only.**

    Runs a bounded CloudWatch Logs Insights query across the resolved log
    groups. MyCompany log messages are JSON, so CloudWatch auto-parses their
    fields (``logType``, ``logLevel``, ``statusCode``, ``traceId``,
    ``requestURL``, ``logger``, ``threadName``, …) which you can use in
    ``filters`` / ``raw_filter`` / ``group_by`` / ``fields``.

    Powers use cases like:

      * *"pull the latest errors from ACD in au1 on 27.0"* —
        ``components=[{"name":"ACD","version":"27.0"}], error_only=True``.
      * *"count errors in the last 30 minutes"* —
        ``error_only=True, since_minutes=30, count_only=True``.
      * *"look for the text 'ORA-00028' in scheduling on 27.0"* —
        ``pattern="ORA-00028"``.
      * *"grab everything for ACD in au1 on July 10th UTC"* —
        ``start_time="2026-07-10T00:00:00Z",
        end_time="2026-07-10T23:59:59Z"``.
      * *"latest healthcheck results for ACD in au1"* —
        ``pattern="healthcheck"``.
      * *"which logstream has more errors"* —
        ``count_only=True, group_by="@logStream", error_only=True``.
      * *"count HTTP status codes on access logs"* —
        ``count_only=True, group_by="statusCode",
        filters={"logType":"access"}``.
      * *"only ERROR-level application logs"* —
        ``filters={"logType":"application","logLevel":"ERROR"}``.
      * *"5xx access-log errors by logstream"* —
        ``count_only=True, group_by="@logStream",
        filters={"logType":"access"},
        raw_filter="statusCode >= 500"``.

    Args:
        profile_name: AWS profile substring.
        env: Env folder (e.g. ``"au1"``). Any env is allowed — read-only.
        components: List of components to query. Each entry may be:
              * ``{"name":"ACD","version":"27.0"}`` — exact log group.
              * ``{"name":"ACD"}`` — every version present in env.
              * ``"ACD_27.0"`` — shorthand.
              * ``"ACD"`` — bare component name (all versions).
            Missing components land in ``unresolved`` with fuzzy candidates.
        pattern: Optional substring against ``@message`` (case-insensitive).
        error_only: AND-filter ``ERROR|Exception|FATAL|SEVERE|Traceback``
            on ``@message``.
        filters: Dict of ``{json_field: value}`` for exact / ``in [...]``
            matches against JSON fields the CloudWatch parser exposes
            (``logType``, ``logLevel``, ``statusCode``, ``traceId``, …).
            Values may be scalars (``"ERROR"``, ``500``, ``true``) or
            lists (``[500, 502, 503]``) which produce ``field in […]``.
        raw_filter: A single Insights filter expression appended verbatim
            for conditions the dict form can't express — range
            comparisons (``"statusCode >= 500"``), compound conditions
            (``"(logLevel = 'ERROR' or logLevel = 'FATAL') and elapsedTime > 1000"``),
            etc. The literal ``filter`` prefix is added automatically
            if you omit it.
        group_by: Field name to group by (requires ``count_only=True``).
            Common choices: ``"@logStream"`` (which host/task/stream had
            the most hits), ``"statusCode"``, ``"logType"``, ``"logLevel"``,
            ``"logger"``. Results come back sorted by count desc as
            ``groups: [{<group_by>: value, matches: n}, …]``.
        fields: Extra JSON field names to project on per-row output
            (ignored in count mode). Useful for pulling ``statusCode``,
            ``requestURL``, ``logLevel`` alongside the timestamp/message.
        since_minutes / since_hours / since_days: Lookback window.
            Priority when multiple are set: minutes > hours > days.
            Defaults to 60 minutes if all are 0.
        start_time / end_time: Explicit ISO-8601 / RFC3339 window
            (e.g. ``"2026-07-10T00:00:00Z"``). When either is provided
            it wins over ``since_*``. If only one side is given, the
            other defaults sensibly (``end_time=now`` for start-only,
            ``start_time=end_time-1h`` for end-only). Naive datetimes
            are treated as UTC.
        limit: Max rows to return. Capped at 10000.
        count_only: Return counts instead of rows. Combine with
            ``group_by`` for per-group counts.
        max_wait_seconds: Local wait budget (default 30, capped 120).
            On local timeout the server-side query is cancelled.

    Returns per-target counts / row samples plus any ``unresolved``
    entries with fuzzy-matched log-group candidates.

    Every successful response also carries a ``console_url`` — a
    CloudWatch **Log Analytics** deep link that reproduces the exact
    same query (log groups + time window + pipeline). Surface it to the
    user verbatim; do NOT hand-build any CloudWatch URL yourself. The
    URL points at the new ``#log-analytics`` tab, not the legacy
    ``#logsV2:log-groups`` view.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    denied = _reject_if_sso(profile_name, "query_component_logs")
    if denied:
        return denied

    parsed_specs, malformed = _parse_component_specs(components or [])
    if not parsed_specs and not malformed:
        return {
            "ok": False,
            "error": "components must be a non-empty list of specs (dict or string).",
        }

    start_epoch, end_epoch, window_meta = _resolve_time_window(
        since_minutes, since_hours, since_days, start_time, end_time
    )

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        logs_client = session.client("logs")

        matched, unresolved, _all_groups = _resolve_component_log_groups(
            logs_client, env, parsed_specs
        )

        if not matched:
            return {
                "ok": False,
                "profile_name": profile_name,
                "env": env,
                "window": window_meta,
                "match_count": 0,
                "unresolved": unresolved,
                "malformed": malformed,
                "error": (
                    "No component log groups resolved. Show the user the "
                    "`unresolved[*].candidates` (closest matching log groups "
                    "in this env), ask them to pick, then re-invoke with the "
                    "correct components."
                ),
                "instruction": (
                    "Do NOT guess a log-group name. Present candidates and "
                    "ask the user."
                ),
            }

        log_group_names = [m["log_group_name"] for m in matched]
        if len(log_group_names) > _LOGS_MAX_LOG_GROUPS_PER_QUERY:
            return {
                "ok": False,
                "profile_name": profile_name,
                "env": env,
                "error": (
                    f"Too many log groups resolved ({len(log_group_names)}). "
                    f"CloudWatch Logs Insights caps at {_LOGS_MAX_LOG_GROUPS_PER_QUERY} "
                    "per query. Narrow the request (add versions, drop components) "
                    "and re-invoke."
                ),
                "matched": matched,
            }

        query_string = _build_insights_query(
            pattern=pattern,
            error_only=error_only,
            count_only=count_only,
            limit=limit,
            filters=filters,
            raw_filter=raw_filter,
            group_by=group_by,
            extra_fields=fields,
        )

        try:
            start_resp = logs_client.start_query(
                logGroupNames=log_group_names,
                startTime=start_epoch,
                endTime=end_epoch,
                queryString=query_string,
                limit=max(1, min(int(limit), _LOGS_MAX_ROW_LIMIT)),
            )
        except (ClientError, BotoCoreError) as exc:
            return {
                "ok": False,
                "profile_name": profile_name,
                "env": env,
                "error": f"start_query failed: {exc}",
                "matched": matched,
            }

        query_id = start_resp.get("queryId")
        result = _wait_for_insights_query(logs_client, query_id, max_wait_seconds)
        status = (result.get("status") or "").strip()

        rows = [
            _flatten_insights_row(r)
            for r in result.get("results") or []
        ]

        # Log Analytics deep link (best-effort — needs the account id via
        # STS; if that fails we just omit the URL rather than sink the
        # response).
        account_id = _account_id_from_session(
            session,
            cache_key=_resolve_session_profile(
                profile_name, session_profile or None
            ),
        )
        console_url = (
            _build_log_analytics_url(
                region=session.region_name,
                account_id=account_id,
                log_group_names=log_group_names,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                query_body=query_string,
            )
            if account_id
            else None
        )

        payload: dict[str, Any] = {
            "ok": status == "Complete",
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": session.region_name,
            "env": env,
            "query_id": query_id,
            "query_status": status or "unknown",
            "query_string": query_string,
            "console_url": console_url,
            "window": window_meta,
            "matched": matched,
            "matched_log_groups": log_group_names,
            "unresolved": unresolved,
            "malformed": malformed,
            "count_only": count_only,
            "statistics": result.get("statistics"),
        }
        if count_only:
            gb = (group_by or "").strip()
            if gb:
                # Grouped-count response. Rows look like
                # ``[{<group_by>: value, 'matches': '42'}, ...]`` after
                # flattening; CloudWatch sometimes strips the leading
                # ``@`` from the projected field name in the row, so
                # accept both forms.
                groups: list[dict[str, Any]] = []
                gb_alt = gb.lstrip("@")
                total = 0
                for r in rows:
                    gb_value = r.get(gb)
                    if gb_value is None and gb_alt != gb:
                        gb_value = r.get(gb_alt)
                    try:
                        m_val = int(r.get("matches") or 0)
                    except (TypeError, ValueError):
                        m_val = 0
                    total += m_val
                    groups.append({"value": gb_value, "matches": m_val})
                payload["group_by"] = gb
                payload["group_count"] = len(groups)
                payload["match_count"] = total
                payload["groups"] = groups
            else:
                match_count = 0
                if rows and "matches" in rows[0]:
                    try:
                        match_count = int(rows[0]["matches"])
                    except (TypeError, ValueError):
                        match_count = 0
                payload["match_count"] = match_count
        else:
            payload["row_count"] = len(rows)
            payload["rows"] = rows
        if status != "Complete":
            payload["ok"] = False
            payload["instruction"] = (
                "The Insights query did not complete within the wait budget. "
                "Either raise `max_wait_seconds`, narrow the time window, "
                "reduce the number of log groups, or add a `pattern` to shrink "
                "the working set."
            )
        return payload

    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "env": env,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "error": str(exc),
        }


# ── Bulk / parallel per-component log query ─────────────────────────────────
# CloudWatch Logs Insights concurrent-query limit is 30 per account by
# default. Cap at 20 to stay comfortably under it and avoid throttling
# other tools that may be sharing the account.
_LOGS_BULK_MAX_WORKERS = 20


def _run_single_component_query(
    logs_client,
    match: dict[str, Any],
    query_string: str,
    start_epoch: int,
    end_epoch: int,
    row_limit: int,
    max_wait_seconds: int,
    count_only: bool,
    group_by: str,
    region: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Run the query for a single (component, version, log_group) target
    and return a normalized result dict. Errors are captured as
    ``{"ok": False, "error": ...}`` so a failing target doesn't sink the
    whole batch. When ``region`` and ``account_id`` are supplied, the
    result carries a ``console_url`` deep-link into CloudWatch Log
    Analytics for that single log group + time window + query."""
    log_group_name = match["log_group_name"]
    base: dict[str, Any] = {
        "component": match["component"],
        "version": match["version"],
        "log_group_name": log_group_name,
    }
    if region and account_id:
        base["console_url"] = _build_log_analytics_url(
            region=region,
            account_id=account_id,
            log_group_names=[log_group_name],
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            query_body=query_string,
        )
    try:
        start_resp = logs_client.start_query(
            logGroupNames=[log_group_name],
            startTime=start_epoch,
            endTime=end_epoch,
            queryString=query_string,
            limit=row_limit,
        )
    except (ClientError, BotoCoreError) as exc:
        return {**base, "ok": False, "error": f"start_query failed: {exc}"}

    query_id = start_resp.get("queryId")
    result = _wait_for_insights_query(logs_client, query_id, max_wait_seconds)
    status = (result.get("status") or "").strip()
    rows = [_flatten_insights_row(r) for r in result.get("results") or []]

    payload: dict[str, Any] = {
        **base,
        "ok": status == "Complete",
        "query_id": query_id,
        "query_status": status or "unknown",
        "statistics": result.get("statistics"),
    }
    if count_only:
        gb = (group_by or "").strip()
        if gb:
            groups: list[dict[str, Any]] = []
            gb_alt = gb.lstrip("@")
            total = 0
            for r in rows:
                gb_value = r.get(gb)
                if gb_value is None and gb_alt != gb:
                    gb_value = r.get(gb_alt)
                try:
                    m_val = int(r.get("matches") or 0)
                except (TypeError, ValueError):
                    m_val = 0
                total += m_val
                groups.append({"value": gb_value, "matches": m_val})
            payload["group_count"] = len(groups)
            payload["match_count"] = total
            payload["groups"] = groups
        else:
            match_count = 0
            if rows and "matches" in rows[0]:
                try:
                    match_count = int(rows[0]["matches"])
                except (TypeError, ValueError):
                    match_count = 0
            payload["match_count"] = match_count
    else:
        payload["row_count"] = len(rows)
        payload["rows"] = rows
    if status != "Complete":
        payload["ok"] = False
    return payload


@mcp.tool()
def bulk_query_component_logs(
    profile_name: str,
    env: str,
    components: list[Any],
    pattern: str = "",
    error_only: bool = False,
    since_minutes: int = 0,
    since_hours: int = 0,
    since_days: int = 0,
    start_time: str = "",
    end_time: str = "",
    limit: int = 100,
    count_only: bool = False,
    filters: dict[str, Any] | None = None,
    raw_filter: str = "",
    group_by: str = "",
    fields: list[str] | None = None,
    max_wait_seconds: int = 30,
    max_workers: int = 5,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Run the SAME CloudWatch Insights query per component in PARALLEL. **Read-only.**

    Same query semantics as ``query_component_logs`` (pattern /
    error_only / filters / raw_filter / group_by / fields / time
    window), but fires ONE Insights query per matched
    (component, version, log_group) target in parallel threads and
    returns per-target results instead of aggregating them into a
    single query.

    Use when:

      * The AI needs per-component counts / rows for the same
        inquiry (e.g. *"how many ERROR entries per component in
        stg4 over the last hour"*), without the client having to
        loop ``query_component_logs`` N times.
      * The requested set exceeds CloudWatch's 50-log-group-per-query
        cap that ``query_component_logs`` enforces.
      * You want a per-component ``match_count`` bucket to sort or
        rank components by log-noise / error volume before drilling
        into one specific target.

    Args:
        components: Same shape as ``query_component_logs``. A missing
            version means "all versions present in env" — the
            expansion happens BEFORE fan-out so those extra targets
            also get their own parallel query.
        max_workers: Parallel workers (1..20). Default 5. Capped at
            20 to stay under CloudWatch's default 30-concurrent-Insights-queries
            account limit.
        All other args: Same as ``query_component_logs``.

    Response shape:
        ``{ok, env, window, query_string, console_url, count_only,
        group_by?, target_count, success_count, failure_count,
        workers, results: [{component, version, log_group_name,
        console_url, ok, query_status, row_count|match_count|groups,
        rows?, statistics}, ...], unresolved, malformed}``.

        Both the top-level ``console_url`` (union of every matched log
        group in one Log Analytics query) and each row's
        ``console_url`` (single log group scoped to that
        component+version) deep-link into the new CloudWatch
        ``#log-analytics`` tab with the exact same query and time
        window this tool ran. Surface them verbatim to the user;
        never hand-build any CloudWatch URL.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    denied = _reject_if_sso(profile_name, "bulk_query_component_logs")
    if denied:
        return denied

    parsed_specs, malformed = _parse_component_specs(components or [])
    if not parsed_specs and not malformed:
        return {
            "ok": False,
            "error": "components must be a non-empty list of specs (dict or string).",
        }

    start_epoch, end_epoch, window_meta = _resolve_time_window(
        since_minutes, since_hours, since_days, start_time, end_time
    )
    row_limit = max(1, min(int(limit), _LOGS_MAX_ROW_LIMIT))
    workers = max(1, min(int(max_workers or 1), _LOGS_BULK_MAX_WORKERS))
    query_string = _build_insights_query(
        pattern=pattern,
        error_only=error_only,
        count_only=count_only,
        limit=limit,
        filters=filters,
        raw_filter=raw_filter,
        group_by=group_by,
        extra_fields=fields,
    )

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        logs_client = session.client("logs")

        matched, unresolved, _all_groups = _resolve_component_log_groups(
            logs_client, env, parsed_specs
        )

        if not matched:
            return {
                "ok": False,
                "profile_name": profile_name,
                "env": env,
                "window": window_meta,
                "target_count": 0,
                "unresolved": unresolved,
                "malformed": malformed,
                "error": (
                    "No component log groups resolved. Show the user the "
                    "`unresolved[*].candidates` (closest matching log "
                    "groups in this env), ask them to pick, then re-invoke "
                    "with the correct components."
                ),
                "instruction": (
                    "Do NOT guess a log-group name. Present candidates "
                    "and ask the user."
                ),
            }

        started = time.monotonic()
        results: list[dict[str, Any]] = []
        # Fetch account id once so every per-target row (and the
        # aggregate URL below) can carry a Log Analytics deep link.
        # Best-effort — a failed STS call just skips URL generation.
        account_id = _account_id_from_session(
            session,
            cache_key=_resolve_session_profile(
                profile_name, session_profile or None
            ),
        )
        region_name = session.region_name
        # boto3 clients are thread-safe; safe to share across workers.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _run_single_component_query,
                    logs_client,
                    m,
                    query_string,
                    start_epoch,
                    end_epoch,
                    row_limit,
                    max_wait_seconds,
                    count_only,
                    group_by,
                    region_name,
                    account_id,
                ): m
                for m in matched
            }
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    m = futures[fut]
                    results.append(
                        {
                            "component": m["component"],
                            "version": m["version"],
                            "log_group_name": m["log_group_name"],
                            "ok": False,
                            "error": str(exc),
                        }
                    )
        duration = round(time.monotonic() - started, 3)

        # Keep the output stable: sort by (component, version) so
        # callers can diff runs without shuffling.
        results.sort(
            key=lambda r: (r.get("component") or "", r.get("version") or "")
        )
        succeeded = [r for r in results if r.get("ok")]
        failed = [r for r in results if not r.get("ok")]

        # Aggregate URL: single Log Analytics query that spans every
        # target's log group in one shot (comma-separated SOURCE ARNs)
        # so the AI host can hand the user a single click-through to see
        # the union of hits across components.
        aggregate_console_url: str | None = None
        if account_id:
            aggregate_console_url = _build_log_analytics_url(
                region=region_name,
                account_id=account_id,
                log_group_names=[m["log_group_name"] for m in matched],
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                query_body=query_string,
            )

        payload: dict[str, Any] = {
            "ok": len(failed) == 0,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(profile_name, session_profile or None),
            "region": region_name,
            "env": env,
            "window": window_meta,
            "query_string": query_string,
            "console_url": aggregate_console_url,
            "count_only": count_only,
            "target_count": len(matched),
            "success_count": len(succeeded),
            "failure_count": len(failed),
            "workers": workers,
            "total_duration_seconds": duration,
            "unresolved": unresolved,
            "malformed": malformed,
            "results": results,
        }
        gb = (group_by or "").strip()
        if count_only and gb:
            payload["group_by"] = gb
        return payload

    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(profile_name, session_profile or None),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "env": env,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "error": str(exc),
        }


# ── AWS CLI bridge (freestyle escape hatch) ─────────────────────────────────
# When the specialized tools above don't cover the ask, the AI host can send
# a raw ``aws …`` command through ``run_aws_cli``. Two hard rules keep this
# safe regardless of what the AI sends:
#
#   1. Every command is classified as read-only or mutating based on the
#      AWS CLI verb prefix (``describe-``, ``list-``, ``get-``, …).
#      Mutations require ``confirmed_by_user=True`` with the exact
#      resolved argv shown back to the user first — same two-step
#      contract as ``deploy_tag_targets`` / ``redeploy_targets`` on the
#      gitlab side.
#   2. Commands are split with ``shlex`` and executed via ``subprocess``
#      with ``shell=False``. Shell metacharacters (``&``, ``|``, ``;``,
#      ``>``, ``$()``, backticks, …) are rejected outright — we can
#      never spawn a subshell no matter what the AI passes in.
#
# The tool always uses the awsume-refreshed session profile via
# ``--profile`` injection, supports parallel fan-out across multiple
# profiles (for cross-env read-only inspection), auto-parses JSON stdout,
# and honors the same auto-refresh path as the boto3-based tools.

_AWS_CLI_READ_ONLY_PREFIXES: tuple[str, ...] = (
    "describe-",
    "list-",
    "get-",
    "search-",
    "select-",
    "lookup-",
    "head-",
    "show-",
    "estimate-",
    "preview-",
    "simulate-",
    "check-",
    "test-",
    "validate-",
    "batch-get-",
    "batch-describe-",
)

# ``<service>:<operation>`` allowlist for commands whose verbs don't fit
# the read-only prefixes but are known-read-only.
_AWS_CLI_READ_ONLY_EXACT: frozenset[str] = frozenset(
    {
        "sts:decode-authorization-message",
        "logs:start-query",
        "logs:stop-query",
        "s3api:head-object",
        "s3api:head-bucket",
        "iam:generate-service-last-accessed-details",
        "iam:generate-credential-report",
        "sts:assume-role",  # read from IAM's POV — writes short-lived creds locally
        "sts:assume-role-with-web-identity",
        "sts:assume-role-with-saml",
    }
)

# Short verbs that appear as the ``operation`` (or bare command) and are
# inherently read-only.
_AWS_CLI_READ_ONLY_LITERAL_VERBS: frozenset[str] = frozenset(
    {"help", "wait", "version"}
)

# Shell metacharacters that must never appear in a submitted command.
# Because we call ``subprocess.run`` with ``shell=False`` these characters
# can't actually escape our sandbox, but any of them showing up in the
# input is either an injection attempt or an AI that doesn't understand
# the tool contract — either way we reject.
_AWS_CLI_FORBIDDEN_METACHARS = re.compile(r"[;&|`><]|\$\(")

_AWS_CLI_MAX_COMMAND_LENGTH = 4000
_AWS_CLI_MAX_TIMEOUT_SECONDS = 600
_AWS_CLI_MAX_WORKERS = 20
_AWS_CLI_STDOUT_CAP = 20000
_AWS_CLI_STDERR_CAP = 5000


def _sanitize_and_split_aws_command(
    command: str,
) -> tuple[list[str], dict[str, Any] | None]:
    """Return ``(argv, error_payload)``. Exactly one is populated."""
    if not command or not command.strip():
        return [], {"ok": False, "error": "command is empty."}
    if len(command) > _AWS_CLI_MAX_COMMAND_LENGTH:
        return [], {
            "ok": False,
            "error": (
                f"command exceeds {_AWS_CLI_MAX_COMMAND_LENGTH} chars — "
                "refusing overlong input."
            ),
        }
    match = _AWS_CLI_FORBIDDEN_METACHARS.search(command)
    if match:
        return [], {
            "ok": False,
            "error": (
                f"command contains forbidden shell metacharacter {match.group()!r}. "
                "This tool never spawns a subshell, so pipes, redirects, and "
                "command chaining are refused. Send a single `aws …` command."
            ),
        }
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return [], {
            "ok": False,
            "error": f"Failed to parse command via shlex: {exc}",
        }
    if not argv:
        return [], {"ok": False, "error": "command parsed to empty argv."}
    if argv[0] != "aws":
        return [], {
            "ok": False,
            "error": (
                f"command must start with `aws`; got {argv[0]!r}. Send the "
                "full command including the `aws` prefix."
            ),
        }
    return argv, None


def _classify_aws_cli_command(argv: list[str]) -> dict[str, Any]:
    """Return classification info for a validated argv (argv[0] == 'aws')."""
    if len(argv) < 2:
        return {
            "ok": False,
            "read_only": None,
            "error": "Command must include a service (e.g. `aws ec2 …`).",
        }
    service = argv[1]
    operation = argv[2] if len(argv) >= 3 else ""

    # ``aws help`` / ``aws version`` are 2-token invocations — the
    # "verb" lives at argv[1] instead of argv[2]. Handle before we look
    # at prefixes so they don't get flagged as mutating.
    if not operation and service in _AWS_CLI_READ_ONLY_LITERAL_VERBS:
        return {
            "ok": True,
            "read_only": True,
            "service": service,
            "operation": operation,
            "reason": (
                f"'{service}' is a bare read-only verb (help/wait/version)."
            ),
        }

    if operation and any(
        operation.startswith(p) for p in _AWS_CLI_READ_ONLY_PREFIXES
    ):
        return {
            "ok": True,
            "read_only": True,
            "service": service,
            "operation": operation,
            "reason": (
                f"Operation '{operation}' starts with a read-only verb prefix."
            ),
        }
    service_op = f"{service}:{operation}"
    if service_op in _AWS_CLI_READ_ONLY_EXACT:
        return {
            "ok": True,
            "read_only": True,
            "service": service,
            "operation": operation,
            "reason": f"'{service_op}' is on the read-only allowlist.",
        }
    if operation in _AWS_CLI_READ_ONLY_LITERAL_VERBS:
        return {
            "ok": True,
            "read_only": True,
            "service": service,
            "operation": operation,
            "reason": (
                f"'{operation}' is a read-only literal verb (help/wait/version)."
            ),
        }
    return {
        "ok": True,
        "read_only": False,
        "service": service,
        "operation": operation,
        "reason": (
            f"Operation '{operation}' does not match any read-only verb "
            "prefix or allowlist entry — treating as MUTATING. Requires "
            "explicit user confirmation via `confirmed_by_user=True`."
        ),
    }


def _inject_profile_and_region(
    argv: list[str], session_profile: str, region: str | None
) -> list[str]:
    """Add ``--profile`` / ``--region`` to argv unless already present."""
    has_profile = any(
        a == "--profile" or a.startswith("--profile=") for a in argv
    )
    has_region = any(
        a == "--region" or a.startswith("--region=") for a in argv
    )
    injected = list(argv)
    if not has_profile:
        injected.extend(["--profile", session_profile])
    if not has_region and region:
        injected.extend(["--region", region])
    return injected


def _stderr_indicates_expired(stderr: str) -> bool:
    if not stderr:
        return False
    lowered = stderr.lower()
    markers = (
        "expiredtoken",
        "the security token included in the request is expired",
        "invalidclienttokenid",
        "unable to locate credentials",
        "the config profile",
    )
    return any(marker in lowered for marker in markers)


def _run_single_aws_cli_command(
    argv: list[str], timeout_seconds: int
) -> dict[str, Any]:
    """Execute a resolved argv via subprocess (shell=False). Never uses shell."""
    started = time.monotonic()
    timeout = max(1, min(int(timeout_seconds), _AWS_CLI_MAX_TIMEOUT_SECONDS))
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": (
                "`aws` executable not found in PATH inside the container. "
                "The awscli package should be installed in the lvx-aws image."
            ),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": f"command timed out after {exc.timeout}s.",
            "returncode": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "timed_out": True,
        }
    duration = round(time.monotonic() - started, 3)

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    stdout_json: Any = None
    trimmed = stdout.strip()
    if trimmed:
        try:
            stdout_json = json.loads(trimmed)
        except (ValueError, TypeError):
            stdout_json = None

    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "duration_seconds": duration,
        "stdout": stdout[:_AWS_CLI_STDOUT_CAP],
        "stdout_truncated": len(stdout) > _AWS_CLI_STDOUT_CAP,
        "stdout_json": stdout_json,
        "stderr": stderr[:_AWS_CLI_STDERR_CAP],
        "stderr_truncated": len(stderr) > _AWS_CLI_STDERR_CAP,
    }


def _run_cli_with_refresh(
    argv: list[str],
    profile_name: str,
    session_profile: str,
    timeout_seconds: int,
    auto_refresh_on_expired: bool,
    mfa_code: str,
    force_new_sso: bool = False,
) -> dict[str, Any]:
    """Run the CLI command; on expired-credentials stderr, dispatch the
    refresh to the right backend for the profile — SSO device-auth
    (writes the token to ``~/.aws/sso/cache`` so the subprocess retry
    can read it) for SSO profiles, awsume for IAM. The IAM path is the
    original inline awsume-then-retry logic and is unchanged.

    ``force_new_sso=True`` drops any cached pending device code before
    dispatch — use it when the previous URL/code has become stale
    (e.g. "Invalid code" on the AWS approval page). Ignored for IAM
    profiles."""
    first = _run_single_aws_cli_command(argv, timeout_seconds)
    if first.get("ok") or not auto_refresh_on_expired:
        return first
    if not _stderr_indicates_expired(first.get("stderr", "")):
        return first

    if _profile_kind(profile_name) == "sso":
        # SSO retry needs to raise on expired stderr so the shared
        # `_try_sso_refresh_and_call` helper (boto3-shaped, expects
        # `caller` to raise) can drive the redemption + retry.
        def _cli_caller() -> dict[str, Any]:
            r = _run_single_aws_cli_command(argv, timeout_seconds)
            if not r.get("ok") and _stderr_indicates_expired(r.get("stderr", "")):
                raise RuntimeError(r.get("stderr") or "credentials expired")
            return r

        meta = _read_profile_metadata().get(profile_name, {})
        sso_session = meta.get("sso_session")
        sso_start_url = meta.get("sso_start_url")
        sso_region = meta.get("sso_region") or meta.get("region")
        if force_new_sso and sso_start_url:
            with _SSO_PENDING_LOCK:
                _SSO_PENDING.pop(
                    _sso_pending_key(sso_session, sso_start_url), None
                )
        return _try_sso_refresh_and_call(
            profile_name=profile_name,
            sso_session=sso_session,
            sso_start_url=sso_start_url,
            sso_region=sso_region,
            original_exc=RuntimeError(
                first.get("stderr") or "credentials expired"
            ),
            caller=_cli_caller,
        )

    # IAM (awsume) path — original inline logic, untouched.
    refresh = _run_awsume(
        profile_name=profile_name,
        session_profile=session_profile,
        mfa_code=mfa_code,
    )
    retry = _run_single_aws_cli_command(argv, timeout_seconds)
    if retry.get("ok"):
        retry["refreshed_with_awsume"] = True
        if refresh.get("stdout"):
            retry["refresh_output"] = refresh["stdout"]
        return retry

    payload = {
        **retry,
        "session_expired": True,
        "refresh_error": refresh.get("error"),
    }
    still_expired = _stderr_indicates_expired(retry.get("stderr", ""))
    if mfa_code.strip():
        payload["needs_mfa"] = False
        payload["mfa_attempt_failed"] = True
        payload["instruction"] = (
            f"awsume refresh for profile '{profile_name}' failed even after "
            "an MFA code was supplied. Report the errors above; do not ask "
            "the user for another MFA code."
        )
    elif refresh.get("needs_mfa") and still_expired:
        payload["needs_mfa"] = True
        payload["instruction"] = (
            f"awsume needs an MFA code for profile '{profile_name}'. Prompt "
            "the user for the code and re-invoke `run_aws_cli` with the "
            "same command and `mfa_code=<code>`. Do NOT ask the user to run "
            "awsume in their terminal."
        )
    else:
        payload["needs_mfa"] = False
        payload["instruction"] = (
            f"awsume did not restore working credentials for profile "
            f"'{profile_name}', but this is NOT an MFA issue. Report the "
            "errors above so the user can investigate."
        )
    return payload


@mcp.tool()
def run_aws_cli(
    command: str,
    profile_name: str = "",
    profile_names: list[str] | None = None,
    confirmed_by_user: bool = False,
    dry_run: bool = False,
    max_workers: int = 5,
    timeout_seconds: int = 60,
    region: str = "",
    session_profile: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
    force_new_sso: bool = False,
) -> dict[str, Any]:
    """Freestyle AWS CLI bridge. Runs a raw ``aws …`` command with the
    resolved awsume session profile injected as ``--profile``. Supports
    parallel fan-out across multiple profiles for cross-env read-only
    inspection.

    **Safety contract:**

      * The command must start with ``aws`` (no shells, no pipes, no
        redirects — enforced by rejection of ``; & | > $()`` and
        backticks, and by ``subprocess.run(shell=False)``).
      * The operation is classified as **read-only** if it starts with a
        known read verb prefix (``describe-`` / ``list-`` / ``get-`` /
        ``search-`` / ``head-`` / ``batch-get-`` / …) or is on the
        explicit allowlist (``sts:assume-role``, ``logs:start-query`` /
        ``stop-query``, ``s3api:head-*``, …). Everything else is
        classified as **mutating**.
      * Mutating commands are refused unless ``confirmed_by_user=True``.
        Without confirmation, the tool returns the exact resolved
        argv(s) it would run so the AI host can show them to the user
        for approval — same two-step contract as
        ``deploy_tag_targets`` on the gitlab side.

    Args:
        command: The full CLI command, e.g. ``"aws ec2 describe-instances
            --filters Name=tag:Environment,Values=stg4"``. The leading
            ``aws`` is required; ``--profile`` / ``--region`` are injected
            automatically when absent.
        profile_name: Single AWS profile (substring match, same as every
            other tool). Use this for single-profile runs.
        profile_names: Multiple profiles to fan out across in parallel.
            Ignored when ``profile_name`` is set. Each name is resolved
            independently — resolution failures don't abort the batch.
        confirmed_by_user: Required (True) for any mutating command.
            Ignored for read-only commands.
        dry_run: If True, resolve everything (profile, argv with
            ``--profile`` / ``--region`` injected) and return the
            classification + resolved argv per target, WITHOUT executing.
            Great for "show me what would run" prompts.
        max_workers: Cap on parallel workers (1..20). Default 5.
        timeout_seconds: Per-command timeout (1..600). Default 60.
        region: Optional ``--region`` override, injected when the command
            doesn't already include one. Falls back to the profile's
            region from ``~/.aws/config``.
        auto_refresh_on_expired / mfa_code: Same
            awsume auto-refresh semantics as the other tools.

    Response shape:
      * Single profile + read-only OR confirmed: ``{ok, command,
        resolved_argv, classification, profile, result}`` where
        ``result`` contains ``returncode``, ``duration_seconds``,
        ``stdout``, ``stdout_json`` (auto-parsed when valid),
        ``stderr``, and truncation flags.
      * Multiple profiles: ``{ok, command, classification, results: [
        {profile_name, resolved_profile, resolved_argv, result}, … ],
        success_count, failure_count, total_duration_seconds}``.
      * Mutation without confirmation: ``{ok: False,
        needs_confirmation: True, mutating: True, classification,
        resolved_argvs: [{profile_name, resolved_argv, displayed_command},
        …], instruction}``.
      * No profile specified: ``{ok: False, needs_profile: True,
        available_profiles, instruction}``.
    """
    # ── 1. Parse + classify. Both are pure functions on the input. ──
    argv, err = _sanitize_and_split_aws_command(command)
    if err:
        return err

    classification = _classify_aws_cli_command(argv)
    if not classification.get("ok"):
        return {"ok": False, "error": classification.get("error"), "argv": argv}

    is_read_only = bool(classification.get("read_only"))

    # ── 2. Resolve the target profile(s). ─────────────────────────────
    targets: list[str] = []
    if profile_names:
        targets = [p for p in profile_names if p and p.strip()]
    elif profile_name and profile_name.strip():
        targets = [profile_name.strip()]

    if not targets:
        return {
            "ok": False,
            "needs_profile": True,
            "command": command,
            "classification": classification,
            "available_profiles": _read_aws_profiles(),
            "instruction": (
                "No profile was specified. Ask the user which AWS profile "
                "(or profiles) to run this command against, then re-invoke "
                "`run_aws_cli` with `profile_name=<name>` or "
                "`profile_names=[<n1>, <n2>, ...]`."
            ),
        }

    resolved_targets: list[dict[str, Any]] = []
    resolution_errors: list[dict[str, Any]] = []
    for raw in targets:
        resolved, err = _resolve_profile(raw)
        if err:
            resolution_errors.append({"input": raw, **err})
            continue
        session = _resolve_session_profile(resolved, session_profile or None)
        prof_region = region or _region_from_config(resolved)
        resolved_targets.append(
            {
                "input": raw,
                "profile_name": resolved,
                "session_profile": session,
                "region": prof_region,
                "resolved_argv": _inject_profile_and_region(
                    argv, session_profile=session, region=prof_region
                ),
            }
        )

    if not resolved_targets:
        return {
            "ok": False,
            "command": command,
            "classification": classification,
            "resolution_errors": resolution_errors,
            "error": (
                "No profile could be resolved. See `resolution_errors` for "
                "the per-profile failure reasons."
            ),
        }

    # ── 3. Mutation gate. ─────────────────────────────────────────────
    def _display(rt: dict[str, Any]) -> str:
        return " ".join(shlex.quote(a) for a in rt["resolved_argv"])

    if not is_read_only and not confirmed_by_user:
        return {
            "ok": False,
            "needs_confirmation": True,
            "mutating": True,
            "command": command,
            "classification": classification,
            "resolved_argvs": [
                {
                    "profile_name": rt["profile_name"],
                    "session_profile": rt["session_profile"],
                    "region": rt["region"],
                    "resolved_argv": rt["resolved_argv"],
                    "displayed_command": _display(rt),
                }
                for rt in resolved_targets
            ],
            "resolution_errors": resolution_errors,
            "instruction": (
                "This command is classified as MUTATING (see "
                "`classification.reason`). Show the user each entry in "
                "`resolved_argvs[*].displayed_command` — those are the "
                "exact commands that will be executed, one per profile. "
                "Only after the user explicitly approves may you call "
                "`run_aws_cli` again with `confirmed_by_user=True` and "
                "the same `command` + `profile_name` / `profile_names`. "
                "Do NOT set `confirmed_by_user=True` on the user's behalf."
            ),
        }

    # ── 4. Dry run — resolve everything, execute nothing. ────────────
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "command": command,
            "classification": classification,
            "read_only": is_read_only,
            "confirmed_by_user": confirmed_by_user,
            "resolved_argvs": [
                {
                    "profile_name": rt["profile_name"],
                    "session_profile": rt["session_profile"],
                    "region": rt["region"],
                    "resolved_argv": rt["resolved_argv"],
                    "displayed_command": _display(rt),
                }
                for rt in resolved_targets
            ],
            "resolution_errors": resolution_errors,
        }

    # ── 5. Execute (single or parallel). ──────────────────────────────
    def _run_target(rt: dict[str, Any]) -> dict[str, Any]:
        exec_result = _run_cli_with_refresh(
            argv=rt["resolved_argv"],
            profile_name=rt["profile_name"],
            session_profile=rt["session_profile"],
            timeout_seconds=timeout_seconds,
            auto_refresh_on_expired=auto_refresh_on_expired,
            mfa_code=mfa_code,
            force_new_sso=force_new_sso,
        )
        return {
            "profile_name": rt["profile_name"],
            "session_profile": rt["session_profile"],
            "region": rt["region"],
            "resolved_argv": rt["resolved_argv"],
            "displayed_command": _display(rt),
            "result": exec_result,
        }

    started = time.monotonic()
    workers = max(1, min(int(max_workers), _AWS_CLI_MAX_WORKERS))

    if len(resolved_targets) == 1:
        results = [_run_target(resolved_targets[0])]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_target, rt): rt for rt in resolved_targets}
            results = []
            for fut in as_completed(futures):
                results.append(fut.result())
        # Keep results in the input order for deterministic output.
        order = {rt["profile_name"]: i for i, rt in enumerate(resolved_targets)}
        results.sort(key=lambda r: order.get(r["profile_name"], 0))

    duration = round(time.monotonic() - started, 3)
    succeeded = [r for r in results if r["result"].get("ok")]
    failed = [r for r in results if not r["result"].get("ok")]

    return {
        "ok": len(failed) == 0,
        "command": command,
        "classification": classification,
        "read_only": is_read_only,
        "confirmed_by_user": confirmed_by_user,
        "profile_count": len(resolved_targets),
        "success_count": len(succeeded),
        "failure_count": len(failed),
        "workers": workers,
        "total_duration_seconds": duration,
        "resolution_errors": resolution_errors,
        "results": results,
    }


# ── Env-wide inventory: EC2 instances + ECS tasks ──────────────────────────
# Compact, cursor-paginated inventory tools that answer "give me every EC2
# instance / ECS task in env <X>" in a single call — with ASG membership,
# task-definition data, and the full tag map already joined. The AI host
# never has to chunk describe-instances calls, cross-reference ASG
# membership, or fan-out per component. Aggregation (AMI drift, unmanaged
# EC2s, sidecar version drift, architecture migration, staleness audits,
# tag compliance, …) is done client-side against these rows.

# MyCompany tag conventions are inconsistent across resources and change
# over time — different envs, different services, different Lambdas
# stamp env under different keys (``Environment``, ``env``, ``lv_env``,
# …) and component identity under different keys (``lv_component``,
# ``Component``, ``lv_component_name``, …). Instead of hardcoding a
# candidate list that goes stale, we DISCOVER the actual keys in use
# from the account itself:
#
#   * Env tag key → one ``ec2.describe_tags`` call filtered by tag
#     ``value`` matching the env (case variants), scoped to instance
#     resources. Whichever tag key(s) turn up become the env filter.
#     If more than one key carries the env value, we query with every
#     key and merge/dedupe by InstanceId so no instances are missed.
#   * Component tag key → any tag key on the resulting instance whose
#     name contains the word ``component`` (case-insensitive). First
#     non-empty value wins per instance.
#
# Both discoveries happen per-call, so as soon as an env starts using
# a new tag key the tools pick it up on the very next invocation. No
# code change required.
_ASG_GROUP_NAME_TAG = "aws:autoscaling:groupName"


def _env_value_candidates(env: str) -> list[str]:
    """Return the case variants we search for when discovering the env
    tag key. AWS tag filters are case-sensitive, but MyCompany stamps
    env values inconsistently (``uk2`` / ``UK2`` / ``Uk2``)."""
    stripped = (env or "").strip()
    if not stripped:
        return []
    return sorted({stripped, stripped.lower(), stripped.upper()})


def _discover_env_tag_keys(ec2_client, env: str) -> dict[str, int]:
    """Return ``{tag_key: instance_count}`` for every EC2 tag key whose
    value equals ``env`` (any case variant) in the current account/region.

    One ``ec2.describe_tags`` call (paginated) — the answer is
    authoritative for THIS account/region right now, no hardcoded
    assumptions. Empty dict means the account genuinely has no
    instance tagged with this env value — either the env doesn't exist
    here or the caller has the wrong profile/region.
    """
    values = _env_value_candidates(env)
    if not values:
        return {}
    counts: dict[str, int] = {}
    paginator = ec2_client.get_paginator("describe_tags")
    for page in paginator.paginate(
        Filters=[
            {"Name": "resource-type", "Values": ["instance"]},
            {"Name": "value", "Values": values},
        ]
    ):
        for tag in page.get("Tags", []):
            key = tag.get("Key")
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


_COMPONENT_KEY_RE = re.compile(r"component", re.IGNORECASE)


def _first_component_tag_value(tag_map: dict[str, str]) -> str | None:
    """Return the first non-empty value from ``tag_map`` for any key whose
    name contains ``component`` (case-insensitive) — but skip keys that
    look like version/revision metadata rather than identity
    (``lv_component_version`` / ``lv_component_revision``).

    Empty strings count as missing so a resource stamped with e.g.
    ``lv_component=""`` doesn't shadow a populated fallback like
    ``Component=jobscheduler``.
    """
    # Prefer keys that don't look like version/revision. Iterate in a
    # stable order (sorted by key length ascending — shorter keys like
    # ``Component`` beat longer ones like ``lv_component_name``) so the
    # picked value is predictable across calls.
    identity_keys = sorted(
        (
            k
            for k in tag_map
            if _COMPONENT_KEY_RE.search(k)
            and not k.lower().endswith(("_version", "_revision"))
        ),
        key=lambda k: (len(k), k),
    )
    for key in identity_keys:
        val = tag_map.get(key)
        if val:
            return val
    return None

# EC2 InstanceState.Name buckets. ``lifecycle_state`` in the returned rows
# is the EC2 State.Name (matches the example in the spec). ``asg_lifecycle_state``
# is added when the instance is ASG-managed so callers can distinguish
# ``Terminating:Wait`` etc.
_ENV_INV_DEFAULT_STATES: tuple[str, ...] = ("running", "pending")
_ENV_INV_TERMINATING_STATES: tuple[str, ...] = (
    "running", "pending", "shutting-down", "stopping", "stopped", "terminated",
)

_ENV_INV_DEFAULT_PAGE_SIZE = 500
_ENV_INV_MAX_PAGE_SIZE = 2000

_ENV_INSTANCE_FIELDS: tuple[str, ...] = (
    "instance_id",
    "asg_name",
    "lv_component",
    "lv_component_version",
    "lv_component_revision",
    "ami_id",
    "ami_name",
    "instance_type",
    "architecture",
    "lifecycle_state",
    "asg_lifecycle_state",
    "health_status",
    "launch_time",
    "availability_zone",
    "private_ip",
    "public_ip",
    "vpc_id",
    "subnet_id",
    "spot_instance",
    "tags",
)

_ENV_TASK_FIELDS: tuple[str, ...] = (
    "cluster",
    "service",
    "task_id",
    "task_definition_arn",
    "task_definition_revision",
    "lv_component",
    "lv_component_version",
    "lv_component_revision",
    "launch_type",
    "platform_version",
    "cpu",
    "memory",
    "architecture",
    "last_status",
    "desired_status",
    "health_status",
    "created_at",
    "started_at",
    "container_images",
    "tags",
)

_ECS_VALID_DESIRED_STATUSES: frozenset[str] = frozenset({"RUNNING", "PENDING", "STOPPED"})


def _tags_as_dict(tags: list[dict[str, Any]] | None) -> dict[str, str]:
    """Flatten an AWS Tag list to ``{key: value}``. Handles both the EC2/ASG
    ``{"Key"/"Value"}`` shape and the ECS ``{"key"/"value"}`` shape."""
    if not tags:
        return {}
    out: dict[str, str] = {}
    for tag in tags:
        key = tag.get("Key") if "Key" in tag else tag.get("key")
        if key is None:
            continue
        val = tag.get("Value") if "Value" in tag else tag.get("value")
        out[str(key)] = "" if val is None else str(val)
    return out


def _iso_utc(dt: Any) -> str | None:
    """Normalize datetimes coming from boto3 into an ISO-8601 UTC string."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    try:
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return str(dt)


def _cursor_query_hash(*parts: Any) -> str:
    """Short deterministic hash of the query parameters. Baked into every
    cursor so a cursor issued for one filter set can't be silently reused
    against a different one."""
    canon = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _encode_env_cursor(offset: int, query_hash: str) -> str:
    """Opaque URL-safe base64 payload of ``{offset, qh}``."""
    payload = json.dumps(
        {"offset": offset, "qh": query_hash}, sort_keys=True, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_env_cursor(
    cursor: str, expected_hash: str
) -> tuple[int | None, str | None]:
    """Return ``(offset, error)``. ``offset`` is 0 when ``cursor`` is blank."""
    if not cursor:
        return 0, None
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + pad).encode()).decode()
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None, "cursor is not a valid opaque token."
    offset = data.get("offset")
    qh = data.get("qh")
    if not isinstance(offset, int) or offset < 0:
        return None, "cursor has invalid offset."
    if qh != expected_hash:
        return None, (
            "cursor was issued for a different filter set — restart pagination "
            "without a cursor (drop the `cursor` argument)."
        )
    return offset, None


def _clamp_page_size(page_size: Any) -> int:
    try:
        n = int(page_size) if page_size else _ENV_INV_DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        n = _ENV_INV_DEFAULT_PAGE_SIZE
    return max(1, min(n, _ENV_INV_MAX_PAGE_SIZE))


def _format_env_rows(
    page: list[dict[str, Any]], fields: tuple[str, ...], output_format: str
) -> list[Any]:
    """Serialize a page in either ``rows`` (list-of-lists, default) or
    ``objects`` (list-of-dicts) shape."""
    if output_format == "objects":
        return [{f: r.get(f) for f in fields} for r in page]
    return [[r.get(f) for f in fields] for r in page]


def _bulk_ami_names(ec2, ami_ids: list[str]) -> dict[str, str]:
    """Return ``{ami_id: name}`` via chunked ``describe_images`` calls.

    Uses ``Filters=[{Name: image-id, ...}]`` (not ``ImageIds``) so
    deregistered AMIs don't fail the whole batch — they just don't come
    back in the response."""
    result: dict[str, str] = {}
    if not ami_ids:
        return result
    for i in range(0, len(ami_ids), 100):
        chunk = ami_ids[i : i + 100]
        try:
            resp = ec2.describe_images(
                Filters=[{"Name": "image-id", "Values": chunk}]
            )
        except (ClientError, BotoCoreError):
            continue
        for img in resp.get("Images", []):
            aid = img.get("ImageId")
            name = img.get("Name")
            if aid and name:
                result[aid] = name
    return result


@mcp.tool()
def describe_env_instances(
    profile_name: str,
    env: str,
    component: str = "",
    asg_name: str = "",
    include_terminating: bool = False,
    lifecycle_states: list[str] | None = None,
    env_tag_key: str = "",
    cursor: str = "",
    page_size: int = _ENV_INV_DEFAULT_PAGE_SIZE,
    format: str = "rows",
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Return the complete EC2 instance inventory for one env in ONE call
    (with cursor pagination for large envs).

    **WHEN TO USE THIS TOOL (batch-first, do NOT loop).**

    Any question that touches MORE THAN ONE EC2, ASG pool, or component in
    the same env should be answered from a SINGLE call to this tool. The
    returned rows carry `asg_name`, `lv_component`, `lv_component_version`,
    `lv_component_revision`, `ami_id`, `ami_name`, `launch_time`,
    `lifecycle_state`, `asg_lifecycle_state`, `health_status`, and the full
    raw `tags` map — that's enough to derive most "how are my pools /
    components doing" answers by grouping in memory. Concretely:

      * **Instance-refresh / EC2 replacement progress across N ASG pools.**
        Do NOT loop `aws autoscaling describe-instance-refreshes` (or the
        equivalent boto call) per ASG. Call this tool ONCE for the env,
        group rows by `asg_name`, then compute per-pool `% new =
        count(rows where lv_component_revision == <new revision>) /
        count(rows)`. `asg_lifecycle_state` (`Pending:Wait`,
        `Terminating:Wait`, `InService`) tells you which specific EC2s are
        mid-swap. One call for all 46 pools instead of 46 CLI calls.
      * **Which pools are running which version / revision.** Group by
        `asg_name`, count distinct `lv_component_version` /
        `lv_component_revision` per pool.
      * **Cross-component AMI or instance-type drift.** Group by
        `lv_component`, aggregate over `ami_id` / `instance_type`.
      * **Stuck / unhealthy instances across the env.** Filter rows where
        `health_status != 'Healthy'` or `asg_lifecycle_state.startswith
        ('Terminating:Wait')` — no per-ASG call needed.

    Only fall back to `get_asg_pool_status` (single pool) or `run_aws_cli`
    (raw CLI) when the user is asking about ONE specific pool AND the
    answer needs a field that isn't in the row schema. Pagination handles
    payload size (see the pagination contract below); the AI must NOT
    short-circuit to per-ASG calls just to avoid paging.

    **No hardcoded tag keys.** MyCompany stamps env under different tag
    keys on different resources (``Environment`` on some, ``env`` /
    ``lv_env`` / ``Env`` on others) and env VALUES may be lowercase or
    uppercase. Instead of maintaining a stale candidate list, the tool
    DISCOVERS the actual env tag key(s) in the account:

      * One ``ec2.describe_tags`` call filtered by tag ``value``
        matching every case variant of ``env`` returns every tag
        (key, count) that carries the env value on EC2 instances RIGHT
        NOW in this account/region.
      * The tool then queries ``describe_instances`` once per
        discovered key (in most envs, one) and merges/dedupes by
        InstanceId, so instances tagged under a different key still
        show up.
      * When a new env or a new tag key appears, discovery picks it
        up on the very next call — no code change needed.

    Pass ``env_tag_key`` to pin the discovery (skip the probe) if you
    already know the right key.

    The ``lv_component`` convenience field is similarly discovered per
    row: any tag key whose name contains ``component`` (case-insensitive,
    excluding ``_version`` / ``_revision`` suffixes) wins the first
    non-empty value. The full raw tag map is always returned as
    ``tags`` so aggregations that need any other tag can read it
    directly.

    Each row is fully joined with:

      * ASG membership — ``asg_name`` from the ``aws:autoscaling:groupName``
        tag. ``null`` when the instance is standalone/unmanaged.
      * ASG-side health/lifecycle — ``health_status`` and
        ``asg_lifecycle_state`` from a single bulk
        ``describe_auto_scaling_instances`` for every ASG-managed instance.
      * AMI name — via a single bulk ``describe_images`` on distinct AMIs.
      * MyCompany tags — ``lv_component`` / ``lv_component_version`` /
        ``lv_component_revision`` extracted straight from the instance tag
        map so callers don't need a second call to resolve which component
        owns an instance.
      * The full tag map (``tags``) so the same tool answers any future
        tag-based question without needing a new tool per tag.

    **Why the compact ``rows`` format is the default.** With 18 fields
    per instance and up to 2000 rows per page, repeating field names on
    every object is a huge wire-size waste. Rows are ordered exactly like
    the top-level ``fields`` array; ``format="objects"`` gives dicts for
    readability at the cost of size.

    **Pagination contract.**
      * The tool enumerates every matching instance server-side (paginated
        internally), sorts deterministically, then slices for the caller.
      * ``total_count`` is the true total on every page (page 1 already
        knows the whole size).
      * ``truncated: true`` NEVER appears without a ``next_cursor``; the
        agent must keep calling with the returned cursor until
        ``next_cursor`` is ``null``.
      * Cursors are opaque URL-safe base64 payloads. Each cursor carries a
        hash of the filter set — a cursor issued for one filter cannot be
        reused for a different filter (restart pagination without a
        cursor if you change filters).

    **Filtering.**
      * ``env`` — required. Applied server-side via ``tag:env`` filter on
        ``ec2.describe_instances`` (no chunking by ASG-ID limits).
      * ``lifecycle_states`` — explicit list of EC2 ``State.Name`` values
        (e.g. ``["running", "pending"]``). Takes precedence over
        ``include_terminating``.
      * ``include_terminating`` — when true and ``lifecycle_states`` is
        unset, expands the default (``running`` + ``pending``) to also
        include ``shutting-down`` / ``stopping`` / ``stopped`` /
        ``terminated``.
      * ``component`` — case-insensitive substring; matches
        ``lv_component`` OR ``asg_name``.
      * ``asg_name`` — case-insensitive substring on ``asg_name``. Combine
        with ``component`` when you want both narrowed.

    Args:
        profile_name: AWS profile substring (see ``awsume_refresh_session``
            for the resolution contract).
        env: Env code (e.g. ``"uk2"``). Applied as ``tag:env=<env>``.
        component: Substring filter (case-insensitive) — matches
            ``lv_component`` tag or ``asg_name``.
        asg_name: Substring filter on ``asg_name``.
        include_terminating: See above.
        lifecycle_states: Explicit EC2 state filter, takes precedence.
        cursor: Opaque cursor returned from a prior response's
            ``next_cursor``. Do NOT construct by hand.
        page_size: Rows per page (1..2000). Default 500.
        format: ``"rows"`` (default, compact) or ``"objects"``.

    Returns:
        ``{ok, env, total_count, returned_count, offset, truncated,
        next_cursor, page_size, format, filters, fields, rows|objects}``.
        See ``_ENV_INSTANCE_FIELDS`` for the field order.

    Read-only.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    output_format = (format or "rows").strip().lower()
    if output_format not in ("rows", "objects"):
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "error": "format must be 'rows' or 'objects'.",
        }
    page_n = _clamp_page_size(page_size)

    if lifecycle_states:
        state_values = [
            str(s).strip().lower() for s in lifecycle_states if str(s).strip()
        ]
        if not state_values:
            return {
                "ok": False,
                "profile_name": profile_name,
                "env": env,
                "error": (
                    "lifecycle_states, when provided, must contain at least "
                    "one non-empty value."
                ),
            }
    elif include_terminating:
        state_values = list(_ENV_INV_TERMINATING_STATES)
    else:
        state_values = list(_ENV_INV_DEFAULT_STATES)

    component_needle = (component or "").strip().lower() or None
    asg_needle = (asg_name or "").strip().lower() or None
    env_tag_key_pinned = (env_tag_key or "").strip()

    query_hash = _cursor_query_hash(
        "instances",
        env,
        tuple(sorted(state_values)),
        component_needle,
        asg_needle,
        env_tag_key_pinned,
    )
    offset, cursor_err = _decode_env_cursor(cursor, query_hash)
    if cursor_err:
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "error": cursor_err,
        }

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        ec2 = session.client("ec2")
        autoscaling = session.client("autoscaling")

        # 1. Discover which tag key(s) carry the env value on EC2 in
        # this account/region. No hardcoded list — this is one small
        # `describe_tags` call whose answer IS the source of truth.
        # Caller can pin it via `env_tag_key` to skip discovery.
        env_value_variants = _env_value_candidates(env)
        if env_tag_key_pinned:
            keys_to_use = [env_tag_key_pinned]
            discovered_counts: dict[str, int] = {}
        else:
            discovered_counts = _discover_env_tag_keys(ec2, env)
            keys_to_use = sorted(
                discovered_counts.keys(),
                key=lambda k: (-discovered_counts[k], k),
            )

        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        env_tag_keys_used: list[str] = []

        # 2. For every discovered (or user-pinned) env tag key, fetch
        # instances filtered by env + state. Merge across keys and
        # dedupe by InstanceId so multi-tag setups don't double-count.
        for candidate in keys_to_use:
            filters = [
                {"Name": f"tag:{candidate}", "Values": env_value_variants},
                {"Name": "instance-state-name", "Values": state_values},
            ]
            candidate_hit = False
            for page in ec2.get_paginator("describe_instances").paginate(
                Filters=filters
            ):
                for reservation in page.get("Reservations", []):
                    for ins in reservation.get("Instances", []):
                        iid = ins.get("InstanceId")
                        if not iid or iid in seen_ids:
                            continue
                        seen_ids.add(iid)
                        candidate_hit = True
                        tag_map = _tags_as_dict(ins.get("Tags"))
                        rows.append(
                            {
                                "instance_id": iid,
                                "asg_name": tag_map.get(_ASG_GROUP_NAME_TAG),
                                "lv_component": _first_component_tag_value(tag_map),
                                "lv_component_version": tag_map.get(
                                    _LV_COMPONENT_VERSION_TAG
                                ),
                                "lv_component_revision": tag_map.get(
                                    _LV_COMPONENT_REVISION_TAG
                                ),
                                "ami_id": ins.get("ImageId"),
                                "ami_name": None,
                                "instance_type": ins.get("InstanceType"),
                                "architecture": ins.get("Architecture"),
                                "lifecycle_state": (ins.get("State") or {}).get("Name"),
                                "asg_lifecycle_state": None,
                                "health_status": None,
                                "launch_time": _iso_utc(ins.get("LaunchTime")),
                                "availability_zone": (ins.get("Placement") or {}).get(
                                    "AvailabilityZone"
                                ),
                                "private_ip": ins.get("PrivateIpAddress"),
                                "public_ip": ins.get("PublicIpAddress"),
                                "vpc_id": ins.get("VpcId"),
                                "subnet_id": ins.get("SubnetId"),
                                "spot_instance": ins.get("InstanceLifecycle") == "spot",
                                "tags": tag_map,
                            }
                        )
            if candidate_hit:
                env_tag_keys_used.append(candidate)

        # 2. ASG-side health + lifecycle for ASG-managed instances (one
        # bulk paginated call, no per-instance loop). AWS caps the
        # InstanceIds list at 50 per describe_auto_scaling_instances call.
        asg_managed_ids = [
            r["instance_id"]
            for r in rows
            if r["asg_name"] and r["instance_id"]
        ]
        if asg_managed_ids:
            health_by_id: dict[str, dict[str, str | None]] = {}
            for i in range(0, len(asg_managed_ids), 50):
                chunk = asg_managed_ids[i : i + 50]
                resp = autoscaling.describe_auto_scaling_instances(InstanceIds=chunk)
                for entry in resp.get("AutoScalingInstances", []):
                    iid = entry.get("InstanceId")
                    if iid:
                        health_by_id[iid] = {
                            "health_status": entry.get("HealthStatus"),
                            "asg_lifecycle_state": entry.get("LifecycleState"),
                        }
            for r in rows:
                data = health_by_id.get(r["instance_id"])
                if data:
                    r["health_status"] = data["health_status"]
                    r["asg_lifecycle_state"] = data["asg_lifecycle_state"]

        # 3. AMI names — one bulk describe_images per 100 distinct AMIs.
        distinct_amis = sorted({r["ami_id"] for r in rows if r.get("ami_id")})
        ami_name_by_id = _bulk_ami_names(ec2, distinct_amis)
        for r in rows:
            aid = r.get("ami_id")
            if aid and aid in ami_name_by_id:
                r["ami_name"] = ami_name_by_id[aid]

        # 4. Post-filters (case-insensitive substring on already-joined data).
        if component_needle:
            rows = [
                r
                for r in rows
                if component_needle in (r.get("lv_component") or "").lower()
                or component_needle in (r.get("asg_name") or "").lower()
            ]
        if asg_needle:
            rows = [
                r for r in rows if asg_needle in (r.get("asg_name") or "").lower()
            ]

        # 5. Deterministic sort so pagination is stable across calls.
        rows.sort(
            key=lambda r: (
                r.get("lv_component") or "~",
                r.get("asg_name") or "~",
                r.get("instance_id") or "",
            )
        )

        total = len(rows)
        page = rows[offset : offset + page_n]
        end = offset + len(page)
        truncated = end < total
        next_cursor = _encode_env_cursor(end, query_hash) if truncated else None

        payload: dict[str, Any] = {
            "ok": True,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(
                profile_name, session_profile or None
            ),
            "region": session.region_name,
            "env": env,
            "total_count": total,
            "returned_count": len(page),
            "offset": offset,
            "truncated": truncated,
            "next_cursor": next_cursor,
            "page_size": page_n,
            "format": output_format,
            "filters": {
                "component": component or None,
                "asg_name": asg_name or None,
                "lifecycle_states": state_values,
                "include_terminating": include_terminating,
                "env_tag_key_pinned": env_tag_key_pinned or None,
            },
            "env_tag_discovery": {
                "pinned": env_tag_key_pinned or None,
                "env_value_variants_searched": env_value_variants,
                "discovered_counts": discovered_counts,
                "keys_used": env_tag_keys_used,
            },
            "fields": list(_ENV_INSTANCE_FIELDS),
        }
        # If discovery came back empty AND the caller didn't pin a key,
        # surface it prominently so the AI host can tell the user
        # "no EC2 instance in this account/region is tagged with a value
        # matching '<env>'" instead of silently returning 0 rows.
        if not env_tag_keys_used and not env_tag_key_pinned and total == 0:
            payload["warning"] = (
                f"No EC2 tag on any instance in this account/region carries "
                f"the value '{env}' (case variants tried: "
                f"{env_value_variants}). Either the env doesn't live here, "
                "the profile/region is wrong, or MyCompany stamps env under "
                "a value string this tool didn't try. Verify with "
                "`aws ec2 describe-tags --filters Name=resource-type,Values=instance` "
                "and, if needed, pass `env_tag_key=<key>` explicitly."
            )
        payload["rows" if output_format == "rows" else "objects"] = _format_env_rows(
            page, _ENV_INSTANCE_FIELDS, output_format
        )
        return payload

    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(
                        profile_name, session_profile or None
                    ),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "env": env,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "error": str(exc),
        }


@mcp.tool()
def describe_env_tasks(
    profile_name: str,
    env: str,
    component: str = "",
    cluster: str = "",
    service: str = "",
    desired_statuses: list[str] | None = None,
    cursor: str = "",
    page_size: int = _ENV_INV_DEFAULT_PAGE_SIZE,
    format: str = "rows",
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Return the complete ECS/Fargate task inventory for one env in ONE
    call (with cursor pagination for large envs).

    **WHEN TO USE THIS TOOL (batch-first, do NOT loop).**

    Any question that touches MORE THAN ONE ECS service, task, or
    component in the same env should be answered from a SINGLE call to
    this tool. Each row carries `service`, `cluster`, `task_id`,
    `task_definition_arn`, `task_definition_revision`, `lv_component`,
    `lv_component_version`, `lv_component_revision`, `container_images`
    (list, sidecars preserved), `desired_status`, `last_status`, and the
    full raw `tags` map — enough to derive most "how are my services /
    tasks doing" answers by grouping in memory. Concretely:

      * **Rollout progress for a redeployed ECS component.** Group by
        `service`, then bucket rows by `lv_component_revision` — the
        ratio of new-revision tasks to total tells you % rolled. Do NOT
        loop `describe_services` / `list_tasks` per service.
      * **Which services are running which version / revision across the env.**
        Group by `service` (or `lv_component`), count distinct
        `lv_component_revision`.
      * **Container image / task-definition drift.** Group by
        `container_images` or `task_definition_arn` across services.
      * **Stuck or crash-looping tasks.** Filter rows where
        `desired_status != last_status`, or `last_status` is `STOPPED`
        with the right `stopped_reason` — one call, no per-service
        loop.

    Only fall back to `get_ecs_service_status` (single service) or
    `run_aws_cli` when the user is asking about ONE specific service AND
    needs a field that isn't in the row schema. Pagination handles
    payload size; the AI must NOT short-circuit to per-service calls to
    avoid paging.

    Same philosophy as ``describe_env_instances`` — one call, full rows,
    stable ordering, cursor pagination. Each task row is fully joined
    with:

      * Owning ``service`` — parsed from the task's ``group`` field
        (``service:<name>``). ``null`` for standalone/one-off tasks.
      * Task-definition metadata — ``container_images`` (list, preserved
        so sidecar drift is directly comparable) and ``architecture``
        (from ``runtimePlatform.cpuArchitecture``) via a single
        ``describe_task_definition`` call per distinct task-def ARN.
      * MyCompany tags — ``lv_component`` / ``lv_component_version`` /
        ``lv_component_revision`` extracted from the task's tag map
        (``describe_tasks(include=['TAGS'])``).
      * The full tag map (``tags``) so future tag-based questions don't
        need a new tool.

    **Cluster selection.** The tool matches every ECS cluster whose name
    ends with ``-<env>`` (MyCompany convention:
    ``fargate-u<major>-cluster-<env>``, plus any other env-suffixed
    clusters). Pass ``cluster`` (substring, case-insensitive) to narrow
    to a specific cluster and override the suffix match.

    **Pagination contract.** Identical to ``describe_env_instances``:
    every matching task is enumerated server-side first, sorted
    deterministically, then sliced. ``total_count`` is the true total on
    every page. ``truncated: true`` NEVER appears without a
    ``next_cursor``. Cursors carry a hash of the filter set so they can't
    be silently reused across different filters.

    **Filtering.**
      * ``env`` — required. Selects clusters by name suffix (see above).
      * ``cluster`` — case-insensitive substring on cluster name; when
        set, overrides the env-suffix match.
      * ``service`` — case-insensitive substring on the parsed
        ``service`` name.
      * ``component`` — case-insensitive substring on ``lv_component``
        tag OR service name.
      * ``desired_statuses`` — list of ECS ``desiredStatus`` values
        (``RUNNING`` / ``PENDING`` / ``STOPPED``). Default: all three.
        NOTE: including ``STOPPED`` walks ECS's stopped-task retention
        window, which can significantly expand the result set on busy
        clusters; pass ``["RUNNING"]`` to keep it tight.

    Args:
        profile_name: AWS profile substring.
        env: Env code (e.g. ``"uk3"``).
        component: Substring filter on ``lv_component`` tag or service
            name.
        cluster: Substring filter on cluster name (overrides env-suffix
            match).
        service: Substring filter on parsed service name.
        desired_statuses: Explicit ECS desired-status filter.
        cursor: Opaque cursor from a prior response.
        page_size: Rows per page (1..2000). Default 500.
        format: ``"rows"`` (default, compact) or ``"objects"``.

    Returns:
        ``{ok, env, total_count, returned_count, offset, truncated,
        next_cursor, page_size, format, filters, cluster_count, fields,
        rows|objects}``. See ``_ENV_TASK_FIELDS`` for the field order.
        ``container_images`` is kept as a list per row so
        sidecar-version comparisons don't require re-parsing.

    Read-only.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    output_format = (format or "rows").strip().lower()
    if output_format not in ("rows", "objects"):
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "error": "format must be 'rows' or 'objects'.",
        }
    page_n = _clamp_page_size(page_size)

    if desired_statuses:
        desired_values = [
            str(s).strip().upper() for s in desired_statuses if str(s).strip()
        ]
        if not desired_values:
            return {
                "ok": False,
                "profile_name": profile_name,
                "env": env,
                "error": (
                    "desired_statuses, when provided, must contain at least "
                    "one non-empty value."
                ),
            }
        bad = [v for v in desired_values if v not in _ECS_VALID_DESIRED_STATUSES]
        if bad:
            return {
                "ok": False,
                "profile_name": profile_name,
                "env": env,
                "error": (
                    f"desired_statuses contains invalid values: {bad}. Must "
                    f"be a subset of {sorted(_ECS_VALID_DESIRED_STATUSES)}."
                ),
            }
    else:
        desired_values = ["RUNNING", "PENDING", "STOPPED"]

    component_needle = (component or "").strip().lower() or None
    cluster_needle = (cluster or "").strip().lower() or None
    service_needle = (service or "").strip().lower() or None

    query_hash = _cursor_query_hash(
        "tasks",
        env,
        tuple(sorted(desired_values)),
        component_needle,
        cluster_needle,
        service_needle,
    )
    offset, cursor_err = _decode_env_cursor(cursor, query_hash)
    if cursor_err:
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "error": cursor_err,
        }

    env_suffix = f"-{env}"

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        ecs = session.client("ecs")

        # 1. Enumerate clusters. Either the caller narrowed via
        # ``cluster`` (substring) or we match every cluster whose name
        # ends with ``-<env>``.
        all_cluster_arns: list[str] = []
        for page in ecs.get_paginator("list_clusters").paginate():
            all_cluster_arns.extend(page.get("clusterArns", []))
        clusters_matched: list[str] = []
        for arn in all_cluster_arns:
            name = arn.split("/")[-1]
            if cluster_needle:
                if cluster_needle not in name.lower():
                    continue
            elif not name.endswith(env_suffix):
                continue
            clusters_matched.append(name)

        rows: list[dict[str, Any]] = []
        task_def_arns_needed: set[str] = set()

        # 2. list_tasks per (cluster, desired_status) then describe_tasks
        # in batches of 100 (AWS hard limit).
        for cluster_name in clusters_matched:
            task_arns: list[str] = []
            for st in desired_values:
                for page in ecs.get_paginator("list_tasks").paginate(
                    cluster=cluster_name, desiredStatus=st
                ):
                    task_arns.extend(page.get("taskArns", []))
            if not task_arns:
                continue
            for i in range(0, len(task_arns), 100):
                chunk = task_arns[i : i + 100]
                resp = ecs.describe_tasks(
                    cluster=cluster_name, tasks=chunk, include=["TAGS"]
                )
                for task in resp.get("tasks", []):
                    tag_map = _tags_as_dict(task.get("tags"))
                    task_arn = task.get("taskArn") or ""
                    task_id = task_arn.split("/")[-1] if task_arn else None
                    group = task.get("group") or ""
                    service_name = (
                        group.split(":", 1)[1] if group.startswith("service:") else None
                    )
                    td_arn = task.get("taskDefinitionArn")
                    td_rev: int | None = None
                    if td_arn:
                        rev_part = td_arn.rsplit(":", 1)[-1]
                        try:
                            td_rev = int(rev_part)
                        except (TypeError, ValueError):
                            td_rev = None
                        task_def_arns_needed.add(td_arn)
                    rows.append(
                        {
                            "cluster": cluster_name,
                            "service": service_name,
                            "task_id": task_id,
                            "task_definition_arn": td_arn,
                            "task_definition_revision": td_rev,
                            "lv_component": _first_component_tag_value(tag_map),
                            "lv_component_version": tag_map.get(_LV_COMPONENT_VERSION_TAG),
                            "lv_component_revision": tag_map.get(_LV_COMPONENT_REVISION_TAG),
                            "launch_type": task.get("launchType"),
                            "platform_version": task.get("platformVersion"),
                            "cpu": task.get("cpu"),
                            "memory": task.get("memory"),
                            "architecture": None,
                            "last_status": task.get("lastStatus"),
                            "desired_status": task.get("desiredStatus"),
                            "health_status": task.get("healthStatus"),
                            "created_at": _iso_utc(task.get("createdAt")),
                            "started_at": _iso_utc(task.get("startedAt")),
                            "container_images": [],
                            "tags": tag_map,
                        }
                    )

        # 3. Task-definition fan-out: one describe_task_definition per
        # distinct ARN (task defs are immutable per revision so a single
        # call is authoritative). Fills container_images + architecture.
        if task_def_arns_needed:
            td_cache: dict[str, dict[str, Any]] = {}
            for td_arn in sorted(task_def_arns_needed):
                try:
                    resp = ecs.describe_task_definition(taskDefinition=td_arn)
                    td_cache[td_arn] = resp.get("taskDefinition") or {}
                except (ClientError, BotoCoreError):
                    td_cache[td_arn] = {}
            for r in rows:
                td = td_cache.get(r["task_definition_arn"] or "", {})
                containers = td.get("containerDefinitions") or []
                r["container_images"] = [
                    c.get("image") for c in containers if c.get("image")
                ]
                runtime = td.get("runtimePlatform") or {}
                r["architecture"] = runtime.get("cpuArchitecture")

        # 4. Post-filters.
        if service_needle:
            rows = [
                r for r in rows if service_needle in (r.get("service") or "").lower()
            ]
        if component_needle:
            rows = [
                r
                for r in rows
                if component_needle in (r.get("lv_component") or "").lower()
                or component_needle in (r.get("service") or "").lower()
            ]

        # 5. Deterministic sort so pagination is stable.
        rows.sort(
            key=lambda r: (
                r.get("cluster") or "",
                r.get("service") or "~",
                r.get("task_id") or "",
            )
        )

        total = len(rows)
        page = rows[offset : offset + page_n]
        end = offset + len(page)
        truncated = end < total
        next_cursor = _encode_env_cursor(end, query_hash) if truncated else None

        payload: dict[str, Any] = {
            "ok": True,
            "profile_name": profile_name,
            "session_profile": _resolve_session_profile(
                profile_name, session_profile or None
            ),
            "region": session.region_name,
            "env": env,
            "total_count": total,
            "returned_count": len(page),
            "offset": offset,
            "truncated": truncated,
            "next_cursor": next_cursor,
            "page_size": page_n,
            "format": output_format,
            "filters": {
                "component": component or None,
                "cluster": cluster or None,
                "service": service or None,
                "desired_statuses": desired_values,
            },
            "cluster_count": len(clusters_matched),
            "fields": list(_ENV_TASK_FIELDS),
        }
        payload["rows" if output_format == "rows" else "objects"] = _format_env_rows(
            page, _ENV_TASK_FIELDS, output_format
        )
        return payload

    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(
                        profile_name, session_profile or None
                    ),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "env": env,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {
            "ok": False,
            "profile_name": profile_name,
            "env": env,
            "error": str(exc),
        }


# ── Profile listing (kind-aware) ─────────────────────────────────────────────

@mcp.tool()
def list_profiles(kind: str = "") -> dict[str, Any]:
    """List every AWS profile in ``~/.aws/config`` with its kind (``iam``
    or ``sso``) plus resolution metadata. **Read-only.**

    Kind is decided by inspecting each profile's config block — presence
    of ``sso_session`` (or legacy ``sso_start_url``) means SSO, otherwise
    IAM. No profile-name convention is assumed.

    Prefer this over ``list_config_profiles`` whenever the user asks
    "which SSO profiles do I have?", "list my MyCompany profiles", or wants
    to know how a substring will resolve.

    Args:
        kind: Optional filter — ``"sso"`` or ``"iam"``. Empty returns
            both.

    Response shape:
        ``{ok, iam_count, sso_count, profiles: [{name, kind, region,
        sso_session?, sso_start_url?, sso_region?, sso_account_id?,
        sso_role_name?, role_arn?, source_profile?, mfa_serial?}, ...]}``
    """
    kind_filter = (kind or "").strip().lower()
    if kind_filter and kind_filter not in ("iam", "sso"):
        return {"ok": False, "error": "kind must be 'iam', 'sso', or empty."}
    meta = _read_profile_metadata()
    rows: list[dict[str, Any]] = []
    for name in sorted(meta.keys()):
        row = {"name": name, **meta[name]}
        if kind_filter and row.get("kind") != kind_filter:
            continue
        rows.append(row)
    return {
        "ok": True,
        "kind_filter": kind_filter or None,
        "iam_count": sum(1 for r in meta.values() if r.get("kind") == "iam"),
        "sso_count": sum(1 for r in meta.values() if r.get("kind") == "sso"),
        "profiles": rows,
    }


@mcp.tool()
def list_sso_sessions() -> dict[str, Any]:
    """List every ``[sso-session <name>]`` block in ``~/.aws/config`` and the
    profiles bound to each. **Read-only.**

    Useful when the user asks "which SSO sessions am I set up for?" or
    when the AI needs to know that N profiles all share the same
    device-auth login.
    """
    sessions = _read_sso_sessions()
    meta = _read_profile_metadata()
    bindings: dict[str, list[dict[str, Any]]] = {name: [] for name in sessions}
    for pname, pmeta in meta.items():
        if pmeta.get("kind") == "sso" and pmeta.get("sso_session") in bindings:
            bindings[pmeta["sso_session"]].append(
                {
                    "profile_name": pname,
                    "sso_account_id": pmeta.get("sso_account_id"),
                    "sso_role_name": pmeta.get("sso_role_name"),
                    "region": pmeta.get("region"),
                }
            )
    sessions_out: list[dict[str, Any]] = []
    for name in sorted(sessions.keys()):
        block = sessions[name]
        cache = _read_sso_cache(name, block.get("sso_start_url"))
        expires_at = cache.get("expiresAt") if cache else None
        token_valid: bool | None = None
        if cache and expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                token_valid = exp > datetime.now(timezone.utc)
            except ValueError:
                token_valid = None
        sessions_out.append(
            {
                "sso_session": name,
                "sso_start_url": block.get("sso_start_url"),
                "sso_region": block.get("sso_region"),
                "sso_registration_scopes": block.get("sso_registration_scopes"),
                "cached_token_present": cache is not None,
                "cached_token_valid": token_valid,
                "cached_token_expires_at": expires_at,
                "profiles": sorted(bindings[name], key=lambda x: x["profile_name"]),
            }
        )
    return {
        "ok": True,
        "session_count": len(sessions_out),
        "vpn_warning": _SSO_VPN_WARNING,
        "sessions": sessions_out,
    }


@mcp.tool()
def sso_status(profile_name: str = "", sso_session: str = "") -> dict[str, Any]:
    """Report SSO cached-token status without any refresh side-effects.
    **Read-only.**

    Pass either ``profile_name`` (SSO profile — the tool looks up its
    session) or ``sso_session`` directly. Returns whether a cached token
    exists, whether it's still valid, and how much time is left.
    """
    if not profile_name and not sso_session:
        return {
            "ok": False,
            "error": "Provide either profile_name or sso_session.",
        }
    sso_start_url: str | None = None
    sso_region: str | None = None
    resolved_profile: str | None = None
    if profile_name:
        resolved, err = _resolve_profile(profile_name)
        if err:
            return err
        resolved_profile = resolved
        meta = _read_profile_metadata().get(resolved, {})
        if meta.get("kind") != "sso":
            return {
                "ok": False,
                "profile_name": resolved,
                "error": (
                    f"Profile '{resolved}' is not SSO-based — it uses IAM "
                    "credentials. Use `auth_status` instead."
                ),
            }
        sso_session = sso_session or meta.get("sso_session") or ""
        sso_start_url = meta.get("sso_start_url")
        sso_region = meta.get("sso_region") or meta.get("region")

    if sso_session and (not sso_start_url or not sso_region):
        block = _read_sso_sessions().get(sso_session)
        if not block:
            return {
                "ok": False,
                "sso_session": sso_session,
                "error": (
                    f"No `[sso-session {sso_session}]` block found in "
                    "~/.aws/config."
                ),
            }
        sso_start_url = sso_start_url or block.get("sso_start_url")
        sso_region = sso_region or block.get("sso_region")

    cache = _read_sso_cache(sso_session or None, sso_start_url)
    expires_at = cache.get("expiresAt") if cache else None
    token_valid: bool | None = None
    seconds_remaining: int | None = None
    if cache and expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            token_valid = exp > datetime.now(timezone.utc)
            seconds_remaining = max(0, int((exp - datetime.now(timezone.utc)).total_seconds()))
        except ValueError:
            token_valid = None

    pending = _SSO_PENDING.get(_sso_pending_key(sso_session or None, sso_start_url))
    pending_info: dict[str, Any] | None = None
    if pending:
        pending_info = {
            "verification_uri_complete": pending["verification_uri_complete"],
            "user_code": pending["user_code"],
            "expires_in_seconds": max(0, int(pending["expires_at"] - time.time())),
        }

    return {
        "ok": True,
        "profile_name": resolved_profile,
        "sso_session": sso_session or None,
        "sso_start_url": sso_start_url,
        "sso_region": sso_region,
        "cached_token_present": cache is not None,
        "cached_token_valid": token_valid,
        "cached_token_expires_at": expires_at,
        "cached_token_seconds_remaining": seconds_remaining,
        "pending_device_authorization": pending_info,
        "vpn_warning": _SSO_VPN_WARNING,
    }


@mcp.tool()
def sso_login(
    profile_name: str = "",
    sso_session: str = "",
    wait: bool = False,
    max_wait_seconds: int = 180,
    force_new: bool = False,
) -> dict[str, Any]:
    """Start (or resume) an SSO device-authorization flow.

    Returns the ``verification_uri_complete`` URL the user must click to
    approve the session — with the FortiClient VPN warning. If a token
    is already cached and still valid, returns immediately with
    ``already_logged_in: true`` and no action required.

    **Recommended flow (do this):**

      1. Call ``sso_login(profile_name=...)`` with ``wait=False`` (the
         default). The response returns instantly with the URL — show it
         to the user right away along with ``vpn_warning``.
      2. Wait for the user to say they've clicked and approved.
      3. Either call ANY AWS tool with the same profile — the SSO refresh
         path detects the fresh approval and continues automatically —
         or call ``sso_login(..., wait=True)`` to explicitly confirm
         login before proceeding.

    **Do NOT call ``sso_login(wait=True)`` on the very first attempt.**
    The URL is only in the response; blocking before returning it means
    the user can't see the link and the tool sits idle until timeout.
    To protect against this, the server forcibly ignores ``wait=True``
    when it just started a fresh device-authorization (nothing was
    pending). Wait is only honored when a prior call already handed the
    URL to the user.

    Args:
        profile_name: An SSO profile (substring accepted). The tool
            looks up its ``sso_session`` and start URL from
            ``~/.aws/config``.
        sso_session: Alternative to ``profile_name`` — pass the session
            name directly (e.g. ``"mycompany"``).
        wait: If ``True`` AND a pending device auth already exists
            (i.e. this is the SECOND+ call for the same login), poll
            ``sso-oidc.create_token`` up to ``max_wait_seconds`` and
            return ``logged_in: true`` once the user approves. On a
            fresh call with no pending session, ``wait`` is ignored
            and the URL is returned immediately so the user can see
            it.
        max_wait_seconds: 1..600, default 180. Only used when the wait
            actually runs.
    """
    if not profile_name and not sso_session:
        return {"ok": False, "error": "Provide either profile_name or sso_session."}

    sso_start_url: str | None = None
    sso_region: str | None = None
    resolved_profile: str | None = None
    if profile_name:
        resolved, err = _resolve_profile(profile_name)
        if err:
            return err
        resolved_profile = resolved
        meta = _read_profile_metadata().get(resolved, {})
        if meta.get("kind") != "sso":
            return {
                "ok": False,
                "profile_name": resolved,
                "error": (
                    f"Profile '{resolved}' is not SSO-based. This tool only "
                    "applies to profiles whose config block declares an "
                    "`sso_session` or `sso_start_url`."
                ),
            }
        sso_session = sso_session or meta.get("sso_session") or ""
        sso_start_url = meta.get("sso_start_url")
        sso_region = meta.get("sso_region") or meta.get("region")

    if sso_session and (not sso_start_url or not sso_region):
        block = _read_sso_sessions().get(sso_session)
        if not block:
            return {
                "ok": False,
                "sso_session": sso_session,
                "error": (
                    f"No `[sso-session {sso_session}]` block found in ~/.aws/config."
                ),
            }
        sso_start_url = sso_start_url or block.get("sso_start_url")
        sso_region = sso_region or block.get("sso_region")

    if not sso_start_url or not sso_region:
        return {
            "ok": False,
            "sso_session": sso_session or None,
            "error": (
                "Missing sso_start_url or sso_region — check ~/.aws/config."
            ),
        }

    cache = _read_sso_cache(sso_session or None, sso_start_url)
    if cache:
        expires_at = cache.get("expiresAt")
        try:
            exp = datetime.fromisoformat((expires_at or "").replace("Z", "+00:00"))
            if exp > datetime.now(timezone.utc):
                return {
                    "ok": True,
                    "profile_name": resolved_profile,
                    "sso_session": sso_session or None,
                    "already_logged_in": True,
                    "cached_token_expires_at": expires_at,
                    "cached_token_seconds_remaining": max(
                        0, int((exp - datetime.now(timezone.utc)).total_seconds())
                    ),
                }
        except ValueError:
            pass

    pending_key = _sso_pending_key(sso_session or None, sso_start_url)
    if force_new:
        with _SSO_PENDING_LOCK:
            _SSO_PENDING.pop(pending_key, None)
    pending = _SSO_PENDING.get(pending_key)
    if pending and time.time() >= pending["expires_at"]:
        _SSO_PENDING.pop(pending_key, None)
        pending = None

    just_created = False
    if pending is None:
        try:
            pending = _start_sso_device_auth(
                sso_session=sso_session or None,
                sso_start_url=sso_start_url,
                sso_region=sso_region,
            )
            just_created = True
        except (ClientError, BotoCoreError) as exc:
            return {
                "ok": False,
                "profile_name": resolved_profile,
                "sso_session": sso_session or None,
                "sso_start_error": str(exc),
                "vpn_warning": _SSO_VPN_WARNING,
                "instruction": (
                    "Could not start SSO device-authorization. The most common "
                    "cause is FortiClient VPN being OFF. Ask the user to verify."
                ),
            }

    # Only honor `wait=True` when the URL has already been handed to
    # the user in a prior call. On a fresh device auth the URL only
    # lives in THIS response, so blocking would leave the user with
    # nothing to click.
    if wait and not just_created:
        wait_budget = max(1, min(int(max_wait_seconds or 1), 600))
        deadline = time.time() + wait_budget
        interval = pending["interval"]
        while time.time() < deadline:
            status, token = _redeem_sso_pending(pending)
            if status == "approved" and token:
                _write_sso_cache(
                    sso_session=sso_session or None,
                    sso_start_url=sso_start_url,
                    sso_region=sso_region,
                    token_resp=token,
                    client_id=pending["client_id"],
                    client_secret=pending["client_secret"],
                    client_registration_expires_at=pending.get(
                        "client_registration_expires_at"
                    ),
                )
                _SSO_PENDING.pop(pending_key, None)
                return {
                    "ok": True,
                    "profile_name": resolved_profile,
                    "sso_session": sso_session or None,
                    "logged_in": True,
                    "verification_uri_complete": pending["verification_uri_complete"],
                }
            if status == "expired":
                _SSO_PENDING.pop(pending_key, None)
                break
            time.sleep(interval)

    # Return the URL for the user to click. Repeated calls after
    # approval will land here again, but the SSO refresh path (or a
    # second `sso_login(wait=True)` call) will pick up the approved
    # state and return `logged_in: true`.
    remaining = max(0, int(pending["expires_at"] - time.time()))
    payload: dict[str, Any] = {
        "ok": False,
        "profile_name": resolved_profile,
        "sso_session": sso_session or None,
        "sso_start_url": sso_start_url,
        "sso_region": sso_region,
        "needs_sso_login": True,
        "verification_uri_complete": pending["verification_uri_complete"],
        "verification_uri": pending["verification_uri"],
        "user_code": pending["user_code"],
        "expires_in_seconds": remaining,
        "poll_interval_seconds": pending["interval"],
        "vpn_warning": _SSO_VPN_WARNING,
        "instruction": (
            "Show the `verification_uri_complete` link to the user verbatim — "
            "it already carries the code. Relay the VPN warning. Once they "
            "confirm they've approved the sign-in, call ANY AWS tool again "
            "with the same profile (or `sso_login` with `wait=true`) — the "
            "server will detect the fresh session and continue."
        ),
    }
    if wait and just_created:
        payload["wait_skipped"] = True
        payload["wait_skipped_reason"] = (
            "This was a fresh device authorization. Blocking to wait for "
            "approval would have hidden the URL from the user. Show the URL "
            "now; on the next call `wait=True` will be honored."
        )
    return payload


# ── Generic (non-MyCompany) log tools ──────────────────────────────────────────

@mcp.tool()
def describe_env_log_groups(
    profile_name: str,
    name_prefix: str = "",
    name_contains: str = "",
    max_results: int = 500,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """List CloudWatch log groups without any MyCompany naming assumption.
    **Read-only.**

    Works for both IAM and SSO profiles. Unlike ``list_component_log_groups``
    (which is baked to the ``/<env>/<component>_<version>`` convention),
    this tool takes an arbitrary ``name_prefix`` (server-side filter) and
    optional ``name_contains`` substring (client-side filter).

    Args:
        profile_name: AWS profile substring.
        name_prefix: Optional exact prefix passed to
            ``describe_log_groups(logGroupNamePrefix=…)``. Empty lists
            everything (careful in large accounts).
        name_contains: Optional case-insensitive substring filter on the
            log-group name.
        max_results: Cap on rows returned (1..5000). Default 500.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    cap = max(1, min(int(max_results or 500), 5000))
    contains_needle = (name_contains or "").strip().lower() or None

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        logs_client = session.client("logs")
        paginator = logs_client.get_paginator("describe_log_groups")
        kwargs: dict[str, Any] = {}
        if name_prefix:
            kwargs["logGroupNamePrefix"] = name_prefix
        rows: list[dict[str, Any]] = []
        for page in paginator.paginate(**kwargs):
            for lg in page.get("logGroups", []):
                name = lg.get("logGroupName") or ""
                if contains_needle and contains_needle not in name.lower():
                    continue
                rows.append(
                    {
                        "log_group_name": name,
                        "stored_bytes": lg.get("storedBytes"),
                        "retention_in_days": lg.get("retentionInDays"),
                        "creation_time": lg.get("creationTime"),
                    }
                )
                if len(rows) >= cap:
                    break
            if len(rows) >= cap:
                break
        rows.sort(key=lambda r: r["log_group_name"])
        return {
            "ok": True,
            "profile_name": profile_name,
            "region": session.region_name,
            "name_prefix": name_prefix or None,
            "name_contains": name_contains or None,
            "match_count": len(rows),
            "truncated": len(rows) >= cap,
            "log_groups": rows,
        }

    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(
                        profile_name, session_profile or None
                    ),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {"ok": False, "profile_name": profile_name, "error": str(exc)}


@mcp.tool()
def query_log_groups(
    profile_name: str,
    log_group_names: list[str],
    pattern: str = "",
    error_only: bool = False,
    since_minutes: int = 0,
    since_hours: int = 0,
    since_days: int = 0,
    start_time: str = "",
    end_time: str = "",
    limit: int = 100,
    count_only: bool = False,
    filters: dict[str, Any] | None = None,
    raw_filter: str = "",
    group_by: str = "",
    fields: list[str] | None = None,
    max_wait_seconds: int = 30,
    session_profile: str = "",
    region: str = "",
    auto_refresh_on_expired: bool = True,
    mfa_code: str = "",
) -> dict[str, Any]:
    """Run a CloudWatch Logs Insights query against an explicit list of log
    groups. **Read-only.** Works for both IAM and SSO profiles.

    Same query DSL as ``query_component_logs`` — ``pattern`` / ``error_only``
    / ``filters`` / ``raw_filter`` / ``group_by`` / ``fields`` / time
    window — but you supply the exact log-group names instead of
    MyCompany component specs. Ideal for accounts (including any SSO
    account) where the log-group layout doesn't follow MyCompany
    conventions.
    """
    resolved, profile_err = _resolve_profile(profile_name)
    if profile_err:
        return profile_err
    profile_name = resolved

    names = [n.strip() for n in (log_group_names or []) if n and n.strip()]
    if not names:
        return {
            "ok": False,
            "profile_name": profile_name,
            "error": "log_group_names must be a non-empty list.",
        }
    if len(names) > _LOGS_MAX_LOG_GROUPS_PER_QUERY:
        return {
            "ok": False,
            "profile_name": profile_name,
            "error": (
                f"Too many log groups ({len(names)}). CloudWatch Insights caps "
                f"at {_LOGS_MAX_LOG_GROUPS_PER_QUERY} per query. Split into "
                "multiple calls."
            ),
        }

    start_epoch, end_epoch, window_meta = _resolve_time_window(
        since_minutes, since_hours, since_days, start_time, end_time
    )
    query_string = _build_insights_query(
        pattern=pattern,
        error_only=error_only,
        count_only=count_only,
        limit=limit,
        filters=filters,
        raw_filter=raw_filter,
        group_by=group_by,
        extra_fields=fields,
    )

    def call() -> dict[str, Any]:
        session = _session_for(
            profile_name=profile_name,
            session_profile=session_profile or None,
            region=region or None,
        )
        logs_client = session.client("logs")

        try:
            start_resp = logs_client.start_query(
                logGroupNames=names,
                startTime=start_epoch,
                endTime=end_epoch,
                queryString=query_string,
                limit=max(1, min(int(limit), _LOGS_MAX_ROW_LIMIT)),
            )
        except (ClientError, BotoCoreError) as exc:
            return {
                "ok": False,
                "profile_name": profile_name,
                "error": f"start_query failed: {exc}",
                "log_group_names": names,
            }

        query_id = start_resp.get("queryId")
        result = _wait_for_insights_query(logs_client, query_id, max_wait_seconds)
        status = (result.get("status") or "").strip()
        rows = [_flatten_insights_row(r) for r in result.get("results") or []]

        account_id = _account_id_from_session(
            session,
            cache_key=_resolve_session_profile(
                profile_name, session_profile or None
            ),
        )
        console_url = (
            _build_log_analytics_url(
                region=session.region_name,
                account_id=account_id,
                log_group_names=names,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                query_body=query_string,
            )
            if account_id
            else None
        )

        payload: dict[str, Any] = {
            "ok": status == "Complete",
            "profile_name": profile_name,
            "region": session.region_name,
            "query_id": query_id,
            "query_status": status or "unknown",
            "query_string": query_string,
            "console_url": console_url,
            "window": window_meta,
            "log_group_names": names,
            "count_only": count_only,
            "statistics": result.get("statistics"),
        }
        if count_only:
            gb = (group_by or "").strip()
            if gb:
                groups: list[dict[str, Any]] = []
                gb_alt = gb.lstrip("@")
                total = 0
                for r in rows:
                    gb_value = r.get(gb)
                    if gb_value is None and gb_alt != gb:
                        gb_value = r.get(gb_alt)
                    try:
                        m_val = int(r.get("matches") or 0)
                    except (TypeError, ValueError):
                        m_val = 0
                    total += m_val
                    groups.append({"value": gb_value, "matches": m_val})
                payload["group_by"] = gb
                payload["group_count"] = len(groups)
                payload["match_count"] = total
                payload["groups"] = groups
            else:
                match_count = 0
                if rows and "matches" in rows[0]:
                    try:
                        match_count = int(rows[0]["matches"])
                    except (TypeError, ValueError):
                        match_count = 0
                payload["match_count"] = match_count
        else:
            payload["row_count"] = len(rows)
            payload["rows"] = rows
        if status != "Complete":
            payload["ok"] = False
            payload["instruction"] = (
                "The Insights query did not complete within the wait budget. "
                "Raise `max_wait_seconds`, narrow the time window, or reduce "
                "the log-group set."
            )
        return payload

    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        if auto_refresh_on_expired and _session_expired(exc):
            try:
                return _try_refresh_and_call(
                    profile_name=profile_name,
                    session_profile=_resolve_session_profile(
                        profile_name, session_profile or None
                    ),
                    mfa_code=mfa_code,
                    original_exc=exc,
                    caller=call,
                )
            except Exception as retry_exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "profile_name": profile_name,
                    "error": str(exc),
                    "retry_error": str(retry_exc),
                    "session_expired": True,
                }
        return {"ok": False, "profile_name": profile_name, "error": str(exc)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
