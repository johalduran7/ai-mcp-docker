"""Read-only inspection + gated LV_CD_DEPLOY redeploy for MyCompany component-config
repositories.

Inspection tools (``list_component_branches``, ``get_component_config_file``,
``detect_component_deployment_type``, ``get_component_config_summary``) are
strictly read-only.

Redeploy tools (``find_redeploy_targets``, ``redeploy_targets``) implement a
two-step confirmation flow that mirrors the local ``redeploy_refresh_ecs_asg.py``
script: the first tool fetches the target config file and shows the exact diff
(toggling the trailing newline) so the AI host can display it and get user
confirmation; only then may the caller invoke the second tool, which posts an
``LV_CD_DEPLOY`` commit to trigger the pipeline.

Redeploy is restricted to the whitelist envs
(``eu1, perf1, stg4, tst2, tst3, uk2, uk3``) — envs are folder names in the config
repos, so this restriction is enforced against the exact ``env`` path segment.

────────────────────────────────────────────────────────────────────────────
ENV IS ALWAYS REQUIRED — READ THIS BEFORE CALLING ANY TOOL
────────────────────────────────────────────────────────────────────────────
Every tool in this server operates against a specific env, and ``env`` is
a **folder path segment inside a ``<component>-config`` repository on
GitLab** — e.g. ``stg4/asg_config.json``, ``eu1/fargate_config.yml``.

Rules for the AI host:

  1. **NEVER call AWS-side MCP tools** (``mcp_lvx-aws_list_env_components``,
     awsume profile inventories, ECS/ASG service enumeration, etc.) to
     answer any question about deploys, components, envs, or config
     content. AWS profiles are NOT envs. AWS service names
     (``lv-<env>-<component>``) are NOT the GitLab repo names. Reaching
     for AWS wastes calls and produces bogus results (e.g. ``consul``,
     region-specific services) that don't exist as GitLab configs.

  2. **NEVER try to "list envs" or enumerate components without an env.**
     Repos are inconsistent — one component may live under ``stg4/`` and
     ``eu1/``, another under ``tst3/`` only. There is no global "list of
     envs" to enumerate against. Ask the user for the env instead.

  3. If the user hasn't specified an env, **ASK them**. Don't guess, don't
     probe multiple envs, don't fall back to AWS. Say something like:
     "Which env — ``stg4``, ``eu1``, ``tst3``, ``uk2``, …?" and wait.

  4. Component names passed to these tools are ALWAYS the repo-side name
     (``acd``, ``email_gateway``, ``recording_purge``, …) — never the
     AWS-side ``lv-<env>-<component>`` form. If the caller has an
     AWS-side name it must strip the ``lv-<env>-`` prefix before invoking
     any tool here; the ``ECS_COMPONENT_MAP`` / ``ASG_COMPONENT_MAP``
     tables handle the remaining irregular cases
     (``emailgateway``→``email_gateway``, etc.).
"""

import base64
import difflib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

import requests
import yaml
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lvx-gitlab")
from version_notifier import install as _install_version_notifier
_install_version_notifier(mcp, "lvx-gitlab")

DEFAULT_ENDPOINT = "https://vcs.build.mycompany.net/eng"

FARGATE_FILES = ("fargate_config.yml", "fargate_config.yaml")
ASG_FILES = ("asg_config.js", "asg_config.json")

# Keys we surface in the config summary. Matches names commonly seen in the
# fargate_config.* and asg_config.* files across MyCompany component-config repos.
SUMMARY_KEYS = {
    "version", "component_version",
    "image", "image_tag", "tag",
    "min", "max", "desired",
    "min_size", "max_size", "desired_capacity",
    "min_capacity", "max_capacity",
    "nuke", "nuke_on_deploy",
    "instance_type", "capacity_type",
    "cpu", "memory",
}

# Wrapper keys we look inside when the top-level dict groups values.
SUMMARY_WRAPPERS = ("asg", "ecs", "fargate", "config", "service")


# ── Redeploy policy ──────────────────────────────────────────────────────────
# Envs are folder names in the config repos (e.g. `tst3/fargate_config.yml`).
# Only these are allowed as redeploy targets; anything else is rejected at the
# tool boundary — the AI host must not bypass this.
REDEPLOYABLE_ENVS: frozenset[str] = frozenset({"eu1", "perf1", "stg4", "tst2", "tst3", "uk2", "uk3"})

# Component-name exceptions mirroring redeploy_refresh_ecs_asg.py so the AWS side
# (service/pool names) and the config-repo side (repo name) reconcile.
ECS_COMPONENT_MAP: dict[str, str] = {
    # AWS-side name → repo name
    "emailgateway": "email_gateway",
}
ASG_COMPONENT_MAP: dict[str, str] = {
    # AWS-side parsed name → repo name
    "recordingpurge": "recording_purge",
    "customscripts": "custom_scripts",
}

# ASG components whose config commit does NOT auto-trigger the CD pipeline;
# for these we POST /projects/:id/pipeline explicitly after the commit
# (matches redeploy_refresh_ecs_asg.py).
ASG_PIPELINE_TRIGGER_COMPONENTS: frozenset[str] = frozenset({"contact", "recording"})

LV_CD_DEPLOY_COMMIT_MESSAGE = "LV_CD_DEPLOY"

# ── Deploy-history policy ────────────────────────────────────────────────────
# Commit-message marker written by the CD pipeline. A deployment "candidate"
# is a commit carrying this marker on a version branch. The marker filter is
# waived for LV_CD_DEPLOY_BYPASS_COMPONENTS (components deployed via API
# instead of the CD pipeline). Everything else (pipeline success on the
# branch, historical active-state re-parse) still applies to those.
LV_CD_DEPLOY_MARKER = "LV_CD_DEPLOY"
LV_CD_DEPLOY_BYPASS_COMPONENTS: frozenset[str] = frozenset({"contact", "recording"})

_VERSION_BRANCH_RE = re.compile(r"^\d{1,2}\.0$")


# ── CDN policy (devops/cdn @ gitlab-ci) ─────────────────────────────────────
# The CDN repo hosts one JSON file per (component, version-series) under each
# env folder, e.g. ``stg4/designer-27.0.json`` or ``prod/lvp-ui-27.0test.json``.
# The body carries an ``lv_component_version`` key like ``"27.0.51"`` — that's
# what deploys actually change.
#
# TERMINOLOGY — do NOT confuse these two:
#   * ``version_label`` (aka the historical field name ``branch``): the string
#     baked into the filename between the component and ``.json`` (e.g.
#     ``27.0``, ``27.0test``, ``26.1``). It is NOT a git branch. It's how the
#     CDN filenames identify a component's version series. The tool inputs and
#     outputs use ``version_label`` for this field; ``branch`` is still
#     accepted on input as a legacy alias.
#   * ``repo_branch``: the actual git branch we commit to, always
#     ``gitlab-ci``. Every CDN commit lands there.
#
# Unlike the eng ``-config`` repos, the CDN does NOT follow the ``LV_CD_DEPLOY``
# commit-message convention on its own (whatever HEAD of ``gitlab-ci`` has is
# what ships) — but the CDN deploy tools STILL append ``LV_CD_DEPLOY`` to the
# commit message by default because every deploy call from the user is
# treated as an LV_CD_DEPLOY unless they explicitly opt out
# (``lv_cd_deploy: false``).
#
# Env policy is a DENYLIST (not a whitelist): any env folder in the CDN repo
# is deployable UNLESS it's explicitly blocked. New env folders auto-inherit
# the "allowed" default — no image rebuild required to onboard them. The
# only blocked entries are:
#   * ``CDN_ENV_BLACKLIST`` — root folders that aren't envs (bin, global)
#     plus the special ``prod`` folder (per-file exception below).
#   * ``CDN_NON_DEPLOYABLE_ENVS`` — real prod region envs. CDN deploys are
#     NEVER allowed here. Adding an env to this set is the only way to
#     opt-out; removing it re-enables deploys.
#   * ``prod/`` folder exception — dependency fixtures whose basename
#     contains ``staging`` or ``test`` (e.g. ``prod/lvp-ui-27.0test.json``)
#     are updatable; real prod files (``prod/lvp-ui-27.0.json`` etc.) stay
#     blocked. See ``_cdn_env_allows_file``.
CDN_GITLAB_GROUP = "devops"
CDN_PROJECT_NAME = "cdn"
CDN_DEFAULT_BRANCH = "gitlab-ci"

# Root-folder names that live at the top of the CDN repo but are NOT env
# folders — matches lambda_handler.CDN_ENV_BLACKLIST. Also used as part of
# the deploy-time denylist below.
CDN_ENV_BLACKLIST: frozenset[str] = frozenset({"bin", "prod", "global"})

# Real prod region env folders. CDN deploys are ALWAYS rejected here.
# Add an env to this set to opt it out; remove one to re-enable deploys.
# Any env NOT in this set (and not in ``CDN_ENV_BLACKLIST``) is deployable
# by default — new envs onboard automatically.
CDN_NON_DEPLOYABLE_ENVS: frozenset[str] = frozenset({"na3", "na4", "na5", "na6"})

# Filename convention: ``<component>-<branch>.json`` where ``<branch>`` is a
# 1- or 2-digit major + ``.0`` (e.g. ``designer-27.0.json``). ``component``
# is greedy so multi-word names like ``agent-login-ui`` are preserved.
_CDN_FILENAME_RE = re.compile(r"^(.+)-(\d{1,2}\.0)\.json$")

# The single field that a CDN "tag deploy" changes. Same string-level regex
# approach as ``_ASG_REVISION_RE`` so the diff stays minimal (no whole-file
# reformat).
_CDN_VERSION_RE = re.compile(r'("lv_component_version":\s*")([^"]+)(")')

# ``prod`` is not deployable in general (it's in ``CDN_ENV_BLACKLIST``).
# Exception: files inside ``prod/`` whose basename contains ``staging`` or
# ``test`` (case-insensitive) are dependency-only fixtures (e.g.
# ``prod/lvp-ui-27.0test.json``) that the CDN deploy tools ARE allowed to
# update. Real prod files (``prod/lvp-ui-27.0.json`` etc.) remain blocked.
# Prod region envs (``CDN_NON_DEPLOYABLE_ENVS``) are always blocked — no
# per-file exception applies to them.
_CDN_PROD_FILENAME_ALLOW_RE = re.compile(r"(staging|test)", re.IGNORECASE)


def _cdn_prod_file_is_allowed(file_name: str) -> bool:
    """Whether a file basename under ``prod/`` may be updated by CDN deploy
    tools. See ``_CDN_PROD_FILENAME_ALLOW_RE``."""
    return bool(_CDN_PROD_FILENAME_ALLOW_RE.search(file_name or ""))


def _cdn_env_allows_file(env: str, file_name_or_path: str) -> bool:
    """Central per-file gate for CDN deploys.

    Denylist semantics — any env is allowed unless it's in the deny sets:
      * Prod region envs (``CDN_NON_DEPLOYABLE_ENVS``) — always blocked.
      * Non-env root folders (``CDN_ENV_BLACKLIST``) — blocked EXCEPT the
        special ``prod`` folder, which is blocked at the folder level but
        allows per-file updates when the basename matches
        ``_CDN_PROD_FILENAME_ALLOW_RE`` (staging/test dependency files).
      * Anything else — allowed.
    """
    if env == "prod":
        base = (file_name_or_path or "").rsplit("/", 1)[-1]
        return _cdn_prod_file_is_allowed(base)
    if env in CDN_NON_DEPLOYABLE_ENVS or env in CDN_ENV_BLACKLIST:
        return False
    return True


# ── Configuration ────────────────────────────────────────────────────────────

def _endpoint() -> tuple[str, str]:
    """Return (api_base_url, group_path) parsed from GITLAB_ENDPOINT."""
    raw = os.environ.get("GITLAB_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"GITLAB_ENDPOINT is invalid: {raw!r}")
    base = f"{parsed.scheme}://{parsed.netloc}"
    group = parsed.path.strip("/")
    if not group:
        raise RuntimeError(
            "GITLAB_ENDPOINT must include the group path (e.g. https://host/eng)."
        )
    return base, group


def _headers() -> dict[str, str]:
    token = os.environ.get("GITLAB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITLAB_TOKEN is not set. Provide it via your AI host MCP configuration."
        )
    return {"PRIVATE-TOKEN": token, "Accept": "application/json"}


def _encoded_project(component: str) -> str:
    _, group = _endpoint()
    return quote(f"{group}/{component}-config", safe="")


def _api(path: str) -> str:
    base, _ = _endpoint()
    return f"{base}/api/v4{path}"


def _get(url: str, params: Optional[dict[str, Any]] = None) -> requests.Response:
    try:
        return requests.get(url, headers=_headers(), params=params, timeout=30)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        base, _ = _endpoint()
        raise RuntimeError(
            f"GitLab endpoint {base} is not reachable ({exc.__class__.__name__}). "
            "Double-check your VPN connection and try again."
        ) from exc


def _fetch_file(component: str, env: str, filename: str, branch: str) -> Optional[dict[str, Any]]:
    encoded_file = quote(f"{env}/{filename}", safe="")
    url = _api(f"/projects/{_encoded_project(component)}/repository/files/{encoded_file}")
    res = _get(url, params={"ref": branch})
    if res.status_code != 200:
        return None
    return res.json()


def _fetch_file_at_path(component: str, file_path: str, branch: str) -> Optional[dict[str, Any]]:
    """Fetch a file by its full repo path (e.g. ``eu1/subfolder/scheduling.properties``)."""
    encoded_file = quote(file_path, safe="")
    url = _api(f"/projects/{_encoded_project(component)}/repository/files/{encoded_file}")
    res = _get(url, params={"ref": branch})
    if res.status_code != 200:
        return None
    return res.json()


def _list_env_tree_files(
    repo_component: str, env: str, branch: str, recursive: bool
) -> Optional[list[dict[str, Any]]]:
    """Return list of ``{path, name, type}`` files under ``<env>/`` (blobs only)
    or ``None`` if the env folder itself couldn't be listed."""
    items: list[dict[str, Any]] = []
    page = 1
    got_any_response = False
    while True:
        params: dict[str, Any] = {
            "ref": branch,
            "path": env,
            "per_page": 100,
            "page": page,
        }
        if recursive:
            params["recursive"] = "true"
        url = _api(f"/projects/{_encoded_project(repo_component)}/repository/tree")
        try:
            res = _get(url, params=params)
        except RuntimeError:
            return None
        if res.status_code != 200:
            if page == 1:
                return None
            break
        got_any_response = True
        data = res.json()
        if not data:
            break
        items.extend(data)
        if "next" not in res.links:
            break
        page += 1
    if not got_any_response:
        return None
    return [
        {"path": it["path"], "name": it.get("name"), "type": it.get("type")}
        for it in items
        if it.get("type") == "blob"
    ]


def _decode(file_json: dict[str, Any]) -> str:
    return base64.b64decode(file_json["content"]).decode("utf-8")


# ── Parsers ──────────────────────────────────────────────────────────────────

def _parse_asg_js(text: str) -> Any:
    """Node-style `module.exports = { ... };` — strip and parse as JSON."""
    stripped = re.sub(r"^\s*module\.exports\s*=\s*", "", text.strip()).rstrip(";").strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _parse_config(filename: str, text: str) -> Any:
    try:
        if filename.endswith((".yml", ".yaml")):
            return yaml.safe_load(text)
        if filename.endswith(".json"):
            return json.loads(text)
        if filename.endswith(".js"):
            return _parse_asg_js(text)
    except Exception:
        return None
    return None


# ── Concurrency helpers ─────────────────────────────────────────────────────
# Every bulk tool (``find_*_targets`` / ``deploy_*_targets`` / ``redeploy_targets``
# / ``list_env_deployments``) accepts a ``workers`` argument so multi-component
# work can fan out across GitLab requests. The cap keeps us polite to the API
# and matches the shape already used by ``list_env_deployments``.
_WORKERS_MIN = 1
_WORKERS_MAX = 30
_WORKERS_DEFAULT = 10


def _cap_workers(workers: Optional[int]) -> int:
    """Clamp ``workers`` to ``[_WORKERS_MIN, _WORKERS_MAX]``."""
    try:
        n = int(workers) if workers is not None else _WORKERS_DEFAULT
    except (TypeError, ValueError):
        n = _WORKERS_DEFAULT
    return max(_WORKERS_MIN, min(n, _WORKERS_MAX))


def _threaded_map(
    fn: Any,
    items: list[Any],
    workers: int,
) -> list[tuple[int, Any, Optional[BaseException]]]:
    """Run ``fn(item)`` for every item in parallel, preserving input order.

    Returns a list of ``(index, result, exception)`` tuples where exactly one
    of ``result`` / ``exception`` is set. Caller decides how to turn errors
    into per-item error entries — mirrors the pattern in ``list_env_deployments``
    so failures never abort the whole batch.
    """
    n = len(items)
    if n == 0:
        return []
    out: list[tuple[int, Any, Optional[BaseException]]] = [None] * n  # type: ignore[list-item]
    if n == 1 or workers <= 1:
        for idx, item in enumerate(items):
            try:
                out[idx] = (idx, fn(item), None)
            except BaseException as exc:  # noqa: BLE001
                out[idx] = (idx, None, exc)
        return out
    with ThreadPoolExecutor(max_workers=min(workers, n)) as pool:
        futures = {pool.submit(fn, item): idx for idx, item in enumerate(items)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                out[idx] = (idx, fut.result(), None)
            except BaseException as exc:  # noqa: BLE001
                out[idx] = (idx, None, exc)
    return out


# ── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_component_branches(component: str, search: str = "") -> list[str]:
    """List branches for `<group>/<component>-config`. Optional `search` filters names
    (e.g. `search='27.0'` to find the u27.0 branch)."""
    url = _api(f"/projects/{_encoded_project(component)}/repository/branches")
    branches: list[str] = []
    page = 1
    while True:
        params: dict[str, Any] = {"per_page": 100, "page": page}
        if search:
            params["search"] = search
        res = _get(url, params=params)
        if res.status_code != 200:
            break
        data = res.json()
        if not data:
            break
        branches.extend(b["name"] for b in data)
        if "next" not in res.links:
            break
        page += 1
    return branches


@mcp.tool()
def get_component_config_file(
    component: str,
    env: str,
    branch: str,
    filename: str = "",
) -> dict[str, Any]:
    """Return the raw config file for a component in an env at a branch.

    If `filename` is empty, standard fargate then asg filenames are tried in order.
    Raises FileNotFoundError if no matching config file exists.
    """
    candidates = [filename] if filename else list(FARGATE_FILES) + list(ASG_FILES)
    for name in candidates:
        file_json = _fetch_file(component, env, name, branch)
        if file_json:
            return {
                "component": component,
                "env": env,
                "branch": branch,
                "file_path": f"{env}/{name}",
                "content": _decode(file_json),
            }
    raise FileNotFoundError(
        f"No config file found for {component}/{env} on branch {branch}."
    )


@mcp.tool()
def list_env_files(
    component: str,
    env: str,
    branch: str,
    recursive: bool = True,
) -> dict[str, Any]:
    """List every file under ``<env>/`` in a component-config repo.

    Use this before ``find_tag_deploy_targets`` when the user wants to modify
    a companion file (e.g. ``eu1/scheduling.properties``) alongside the
    ``fargate_config.yml`` / ``asg_config.js`` tag change. Present the returned
    ``files`` list to the user, ask which one(s) to edit, collect the new
    content, then pass those as ``extra_updates`` to ``find_tag_deploy_targets``.

    Args:
        component: Component name (AWS-side or repo-side; the ECS and ASG
            name maps are both tried).
        env: Env folder (any env; this is read-only).
        branch: Version branch (e.g. ``"27.0"``).
        recursive: Include nested subfolders. Default ``True``.

    Read-only.
    """
    tried: set[str] = set()
    for map_name in ("ecs", "asg"):
        repo_component = _normalize_component_for_repo(component, map_name)
        if repo_component in tried:
            continue
        tried.add(repo_component)
        files = _list_env_tree_files(repo_component, env, branch, recursive)
        if files is not None:
            return {
                "ok": True,
                "component": component,
                "repo_component": repo_component,
                "env": env,
                "branch": branch,
                "recursive": recursive,
                "file_count": len(files),
                "files": files,
            }
    return {
        "ok": False,
        "component": component,
        "env": env,
        "branch": branch,
        "error": (
            f"Could not list files under '{env}/' for component '{component}' on branch "
            f"'{branch}'. Verify the component, env, and branch."
        ),
        "tried_repo_components": sorted(tried),
    }


@mcp.tool()
def detect_component_deployment_type(
    component: str,
    env: str,
    branch: str,
) -> dict[str, Any]:
    """Detect whether a component uses ECS (fargate), ASG, both, or none for a
    given env/branch, based on which config files exist in the repo."""
    found: dict[str, list[str]] = {"ecs": [], "asg": []}
    for name in FARGATE_FILES:
        if _fetch_file(component, env, name, branch):
            found["ecs"].append(name)
    for name in ASG_FILES:
        if _fetch_file(component, env, name, branch):
            found["asg"].append(name)

    if found["ecs"] and found["asg"]:
        deployment_type = "both"
    elif found["ecs"]:
        deployment_type = "ecs"
    elif found["asg"]:
        deployment_type = "asg"
    else:
        deployment_type = "none"

    return {
        "component": component,
        "env": env,
        "branch": branch,
        "deployment_type": deployment_type,
        "files": found,
    }


@mcp.tool()
def get_component_config_summary(
    component: str,
    env: str,
    branch: str,
    filename: str = "",
) -> dict[str, Any]:
    """Parse the component config file and return commonly used settings
    (version, min, max, desired, nuke, image, cpu, memory, ...) when present."""
    file_info = get_component_config_file(component, env, branch, filename)
    parsed = _parse_config(file_info["file_path"].rsplit("/", 1)[-1], file_info["content"])

    summary: dict[str, Any] = {}
    raw_keys: list[str] = []
    if isinstance(parsed, dict):
        raw_keys = list(parsed.keys())
        for key in SUMMARY_KEYS:
            if key in parsed:
                summary[key] = parsed[key]
        for wrapper in SUMMARY_WRAPPERS:
            inner = parsed.get(wrapper)
            if isinstance(inner, dict):
                for key in SUMMARY_KEYS:
                    if key in inner and key not in summary:
                        summary[key] = inner[key]

    return {
        "component": component,
        "env": env,
        "branch": branch,
        "file_path": file_info["file_path"],
        "parsed_ok": parsed is not None,
        "summary": summary,
        "raw_keys": raw_keys,
    }


# ── Redeploy: helpers ────────────────────────────────────────────────────────

def _reject_non_redeployable_env(env: str) -> Optional[dict[str, Any]]:
    """Return an error payload if ``env`` is not in the redeploy whitelist."""
    if env in REDEPLOYABLE_ENVS:
        return None
    return {
        "ok": False,
        "env": env,
        "redeploy_disallowed": True,
        "allowed_envs": sorted(REDEPLOYABLE_ENVS),
        "error": f"Env '{env}' is not in the redeploy whitelist.",
        "instruction": (
            "Redeploy is only allowed for envs: "
            f"{', '.join(sorted(REDEPLOYABLE_ENVS))}. "
            "Do not attempt to redeploy any other env; report this restriction to the user."
        ),
    }


def _normalize_component_for_repo(component: str, deploy_type: str) -> str:
    """Return the repo-side component name (accepts either AWS-side or repo-side input)."""
    mapping = ECS_COMPONENT_MAP if deploy_type == "ecs" else ASG_COMPONENT_MAP
    return mapping.get(component, component)


def _toggle_trailing_newline(content: str) -> str:
    """Add a trailing newline if missing, strip it if present. Deterministic."""
    if content.endswith("\n"):
        return content[:-1]
    return content + "\n"


def _diff_action_label(original: str, proposed: str) -> str:
    if original.endswith("\n") and not proposed.endswith("\n"):
        return "remove_trailing_newline"
    if not original.endswith("\n") and proposed.endswith("\n"):
        return "add_trailing_newline"
    return "content_changed"


def _make_unified_diff(original: str, proposed: str, file_path: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
    )


def _resolve_redeploy_target(
    component: str,
    env: str,
    branch: str,
    deploy_type: str,
) -> Optional[dict[str, Any]]:
    """Fetch the config file for a component and prepare the diff payload.

    If ``deploy_type`` is ``""`` we auto-detect: fargate files first (ECS),
    then asg files (ASG). Returns None if no config file is found.
    """
    candidates: list[tuple[str, str, str]] = []  # (type, filename, repo_component)
    if deploy_type in ("", "ecs"):
        repo_component = _normalize_component_for_repo(component, "ecs")
        for fname in FARGATE_FILES:
            candidates.append(("ecs", fname, repo_component))
    if deploy_type in ("", "asg"):
        repo_component = _normalize_component_for_repo(component, "asg")
        for fname in ASG_FILES:
            candidates.append(("asg", fname, repo_component))

    for detected_type, filename, repo_component in candidates:
        file_json = _fetch_file(repo_component, env, filename, branch)
        if not file_json:
            continue

        original_content = _decode(file_json)
        new_content = _toggle_trailing_newline(original_content)
        file_path = f"{env}/{filename}"
        _, group = _endpoint()
        return {
            "component": component,
            "repo_component": repo_component,
            "env": env,
            "branch": branch,
            "type": detected_type,
            "repo_path": f"{group}/{repo_component}-config",
            "file_path": file_path,
            "filename": filename,
            "new_content": new_content,
            "diff": _make_unified_diff(original_content, new_content, file_path),
            "action": _diff_action_label(original_content, new_content),
            "has_changes": original_content != new_content,
            "last_commit_id": file_json.get("last_commit_id") or file_json.get("commit_id"),
        }
    return None


def _post_commit(
    repo_component: str,
    branch: str,
    file_path: str,
    new_content: str,
    commit_message: str,
    last_commit_id: Optional[str] = None,
) -> requests.Response:
    action: dict[str, Any] = {
        "action": "update",
        "file_path": file_path,
        "content": new_content,
    }
    if last_commit_id:
        # Prevent racing another writer: reject if the file changed since we fetched it.
        action["last_commit_id"] = last_commit_id
    return _post_commit_multi(repo_component, branch, commit_message, [action])


def _post_commit_multi(
    repo_component: str,
    branch: str,
    commit_message: str,
    actions: list[dict[str, Any]],
) -> requests.Response:
    """Commit multiple file updates atomically. Each action should be a fully
    formed GitLab action dict (``action``, ``file_path``, ``content``, and
    optionally ``last_commit_id``)."""
    encoded_project = _encoded_project(repo_component)
    payload = {"branch": branch, "commit_message": commit_message, "actions": actions}
    try:
        return requests.post(
            _api(f"/projects/{encoded_project}/repository/commits"),
            headers=_headers(),
            json=payload,
            timeout=30,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        base, _ = _endpoint()
        raise RuntimeError(
            f"GitLab endpoint {base} is not reachable ({exc.__class__.__name__}). "
            "Double-check your VPN connection and try again."
        ) from exc


def _trigger_asg_pipeline(repo_component: str, branch: str, env: str) -> dict[str, Any]:
    encoded_project = _encoded_project(repo_component)
    try:
        res = requests.post(
            _api(f"/projects/{encoded_project}/pipeline"),
            headers=_headers(),
            json={
                "ref": branch,
                "variables": [
                    {"key": "LV_ENV", "value": env},
                    {"key": "LV_CD_TYPE", "value": "asg"},
                ],
            },
            timeout=30,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        return {"triggered": False, "error": f"{exc.__class__.__name__}: not reachable"}
    if res.status_code in (200, 201):
        data = res.json()
        return {"triggered": True, "pipeline_url": data.get("web_url"), "pipeline_id": data.get("id")}
    return {"triggered": False, "status_code": res.status_code, "error": res.text[:300]}


# ── Redeploy: tools ──────────────────────────────────────────────────────────

@mcp.tool()
def find_redeploy_targets(
    components: list[str],
    env: str,
    branch: str,
    type: str = "",
    workers: int = _WORKERS_DEFAULT,
) -> dict[str, Any]:
    """Preview the LV_CD_DEPLOY change for one or more components. **Read-only.**

    For each component this tool fetches the config file (``fargate_config.yml``
    / ``fargate_config.yaml`` for ECS, ``asg_config.js`` / ``asg_config.json``
    for ASG), computes the deterministic *toggle trailing newline* change, and
    returns a unified diff plus the exact ``new_content`` that would be
    committed.

    **This tool ONLY toggles the trailing newline** — it can't be used to
    edit any other content in the file. If the user's ask includes ANY
    other file edit (a different config field, a companion file like
    ``docker.options``, a tag bump, …), do NOT chain this tool with
    another commit tool — that would produce multiple back-to-back commits
    on the same branch and race the concurrency check. Instead use
    ``find_multi_file_commit_targets`` + ``commit_multi_file_targets``,
    which lands every file change in ONE atomic commit.

    Args:
        components: One or more component names. Both AWS-side and repo-side
            names are accepted — ``emailgateway`` / ``email_gateway``,
            ``recordingpurge`` / ``recording_purge``, etc. — via the standard
            MyCompany naming maps.
        env: Environment folder name. Must be one of
            ``eu1, perf1, stg4, tst2, tst3, uk2, uk3``.
        branch: Version branch (e.g. ``"27.0"``).
        type: ``"ecs"``, ``"asg"``, or ``""`` to auto-detect (fargate first,
            then asg).
        workers: Parallel workers used to fetch each component's config file
            (1..30). Default 10. Bulk previews scale ~linearly with this.

    Returns a payload with a ``targets`` array (kept in input order). Present
    each target's ``diff`` field to the user, get explicit confirmation, then
    pass the same target objects to ``redeploy_targets``. Never fabricate or
    expand the target list.

    **Ambiguity handling — components with BOTH ECS and ASG configs.**
    A few components (e.g. ``siq-complete-asr``) ship BOTH
    ``fargate_config.*`` and ``asg_config.*`` in the same env/branch. When
    called with ``type=""`` (auto-detect) these are routed to the
    ``needs_info`` bucket instead of being silently defaulted to ECS. The AI
    host must ask the user which deployment type to redeploy — ``"ecs"`` or
    ``"asg"`` (or both, in which case call the tool twice) — and re-invoke
    with ``type`` set explicitly. When the caller already passed
    ``type="ecs"`` or ``type="asg"`` this ambiguity check is skipped.
    """
    if type and type not in ("ecs", "asg"):
        return {"ok": False, "error": "type must be 'ecs', 'asg', or '' for auto-detect."}
    if not components:
        return {"ok": False, "error": "components must be a non-empty list."}

    denied = _reject_non_redeployable_env(env)
    if denied:
        return denied

    workers_capped = _cap_workers(workers)

    def _one(component: str) -> dict[str, Any]:
        """Returns ``{"_kind": "target"|"needs_info"|"error", "payload": ...}``."""
        # Always probe so we know if the component has BOTH configs — used
        # both for the auto-detect ambiguity check AND for stamping the
        # ``both_configs_present`` flag on the returned target so
        # ``redeploy_targets`` knows to fire the ASG API pipeline trigger.
        types_present = _detect_deploy_types_present(component, env, branch)
        both = len(types_present) > 1
        if not type and both:
            return {
                "_kind": "needs_info",
                "payload": {
                    "component": component,
                    "env": env,
                    "branch": branch,
                    "types_present": types_present,
                    "missing": ["type"],
                    "reason": (
                        f"Component '{component}' has BOTH ECS (fargate) "
                        f"and ASG config files in env '{env}' on branch "
                        f"'{branch}'. Ask the user which one to redeploy "
                        "— 'ecs' or 'asg' — then re-invoke "
                        "find_redeploy_targets with `type` set explicitly. "
                        "To redeploy both, call the tool twice (once per "
                        "type); the diff and commit are per-file."
                    ),
                },
            }
        resolved = _resolve_redeploy_target(component, env, branch, type)
        if resolved is None:
            return {
                "_kind": "error",
                "payload": {
                    "component": component,
                    "env": env,
                    "branch": branch,
                    "error": (
                        f"No config file found for '{component}' in env "
                        f"'{env}' on branch '{branch}'."
                    ),
                },
            }
        if both:
            # Signal to ``redeploy_targets`` that this component ships both
            # ECS and ASG configs. On the ASG deploy path the CD auto-trigger
            # picks up the ECS side, so we must POST /pipeline explicitly
            # (same treatment as ``contact`` / ``recording``).
            resolved["both_configs_present"] = True
            resolved["types_present"] = types_present
        return {"_kind": "target", "payload": resolved}

    outcomes = _threaded_map(_one, list(components), workers_capped)

    targets: list[dict[str, Any]] = []
    needs_info: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for idx, out, exc in outcomes:
        component = components[idx]
        if exc is not None:
            errors.append(
                {
                    "component": component,
                    "env": env,
                    "branch": branch,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            continue
        kind = out.get("_kind")
        payload = out.get("payload")
        if kind == "target":
            targets.append(payload)
        elif kind == "needs_info":
            needs_info.append(payload)
        else:
            errors.append(payload)

    return {
        "ok": True,
        "env": env,
        "branch": branch,
        "type_filter": type or None,
        "workers": workers_capped,
        "match_count": len(targets),
        "needs_info_count": len(needs_info),
        "error_count": len(errors),
        "targets": targets,
        "needs_info": needs_info,
        "errors": errors,
        "instruction": (
            "First, if `needs_info` is non-empty, handle it: for each entry "
            "with `missing: ['type']`, the component has BOTH ECS and ASG "
            "configs in this env/branch. Ask the user which deployment type "
            "to redeploy — 'ecs' or 'asg' (or both, requiring two separate "
            "invocations) — then re-invoke `find_redeploy_targets` with "
            "`type` set explicitly for those components. Do NOT default to "
            "one type on your own. "
            "Second, display each target's `diff` field to the user (standard "
            "unified diff), then ask for explicit confirmation. On approval, "
            "call `redeploy_targets` with the exact target objects the user "
            "confirmed — each must retain `new_content` so the commit matches "
            "what the user saw. Never fabricate or expand the target list."
        ),
    }


@mcp.tool()
def redeploy_targets(
    targets: list[dict[str, Any]],
    confirmed_by_user: bool = False,
    commit_message: str = LV_CD_DEPLOY_COMMIT_MESSAGE,
    trigger_pipeline_for_asg_components: bool = True,
    workers: int = _WORKERS_DEFAULT,
) -> dict[str, Any]:
    """Commit the confirmed LV_CD_DEPLOY change for each target.

    **Two-step contract (enforced):**
      1. You must first call ``find_redeploy_targets`` to get the ``diff`` per
         target. Show every diff to the user.
      2. Only after the user explicitly approves may you call this tool with
         ``confirmed_by_user=True``. Each target must retain the ``diff``
         field from the preview. Targets missing ``diff`` are rejected.

    Each target must contain at least ``env``, ``branch``, ``file_path``,
    ``new_content``, ``diff`` (from the preview), plus one of ``component``
    or ``repo_component``. The ``type`` field (``ecs`` / ``asg``) is optional
    but used for the ASG pipeline-trigger side-effect and for component-name
    normalization when ``repo_component`` isn't supplied.

    Env whitelist is enforced per target. Any target outside the whitelist is
    skipped and reported as a failure — the successful commits still go
    through for the allowed targets.

    For ASG components in ``ASG_PIPELINE_TRIGGER_COMPONENTS`` (``contact``,
    ``recording``) an additional pipeline is triggered after the commit, with a
    short 5s wait so GitLab can enqueue the config-driven pipeline first.
    The same explicit trigger fires for any target carrying
    ``both_configs_present=True`` (components that ship BOTH ECS and ASG
    configs, e.g. ``siq-complete-asr``) because on those repos the CD
    auto-trigger only picks up the ECS side.

    Multi-target commits run in parallel across ``workers`` threads (1..30,
    default 10). Per-target failures are isolated — one bad commit does not
    abort the batch. ``results`` are returned in the same order as the input
    ``targets``.

    Returns per-target commit info (``commit_id``, ``commit_url``) plus
    success/failure counts.
    """
    if not targets:
        return {"ok": False, "error": "targets must be a non-empty list."}

    if not confirmed_by_user:
        return {
            "ok": False,
            "needs_confirmation": True,
            "requested_count": len(targets),
            "instruction": (
                "Diff review is mandatory. Present the `diff` field of every "
                "target from `find_redeploy_targets` to the user, get explicit "
                "approval, and only then call this tool again with "
                "`confirmed_by_user=True`. Do NOT set the flag on the user's behalf."
            ),
        }

    workers_capped = _cap_workers(workers)

    def _process(t: dict[str, Any]) -> dict[str, Any]:
        component = (t.get("component") or "").strip()
        env = (t.get("env") or "").strip()
        branch = (t.get("branch") or "").strip()
        file_path = (t.get("file_path") or "").strip()
        new_content = t.get("new_content")
        deploy_type = (t.get("type") or "").strip().lower()
        repo_component = (t.get("repo_component") or "").strip()
        last_commit_id = t.get("last_commit_id") or None
        diff = t.get("diff")

        if not (component and env and branch and file_path) or new_content is None:
            return {
                "component": component,
                "ok": False,
                "error": (
                    "Target missing required fields "
                    "(component, env, branch, file_path, new_content)."
                ),
            }

        if not diff:
            return {
                "component": component,
                "ok": False,
                "error": (
                    "Target has no `diff` field — refusing to commit. "
                    "Regenerate the target via `find_redeploy_targets` "
                    "and show the diff to the user before committing."
                ),
            }

        denied = _reject_non_redeployable_env(env)
        if denied:
            return {"component": component, "env": env, "ok": False, "error": denied["error"]}

        if not repo_component:
            repo_component = (
                _normalize_component_for_repo(component, deploy_type)
                if deploy_type in ("ecs", "asg")
                else component
            )

        entry: dict[str, Any] = {
            "component": component,
            "repo_component": repo_component,
            "env": env,
            "branch": branch,
            "file_path": file_path,
            "type": deploy_type or None,
        }

        try:
            res = _post_commit(
                repo_component=repo_component,
                branch=branch,
                file_path=file_path,
                new_content=new_content,
                commit_message=commit_message,
                last_commit_id=last_commit_id,
            )
        except RuntimeError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            return entry

        if res.status_code in (200, 201):
            data = res.json()
            entry["ok"] = True
            entry["commit_id"] = data.get("id")
            entry["commit_url"] = data.get("web_url")

            # ASG-side deploys need an explicit CD pipeline trigger for
            # two categories of components:
            #   * ``contact`` / ``recording`` — hardcoded in
            #     ``ASG_PIPELINE_TRIGGER_COMPONENTS`` (config commit alone
            #     does not fire CD).
            #   * Components that ship BOTH ECS and ASG configs (e.g.
            #     ``siq-complete-asr``) — the CD auto-trigger picks up
            #     the ECS side, so we must POST /pipeline explicitly for
            #     the ASG side. Detected via ``both_configs_present``
            #     stamped by ``find_redeploy_targets``.
            both_configs_present = bool(t.get("both_configs_present"))
            if (
                deploy_type == "asg"
                and trigger_pipeline_for_asg_components
                and (
                    component in ASG_PIPELINE_TRIGGER_COMPONENTS
                    or both_configs_present
                )
            ):
                time.sleep(5)
                entry["pipeline"] = _trigger_asg_pipeline(repo_component, branch, env)
        else:
            entry["ok"] = False
            entry["status_code"] = res.status_code
            entry["error"] = (res.text or "")[:500]

        return entry

    outcomes = _threaded_map(_process, list(targets), workers_capped)

    results: list[dict[str, Any]] = []
    for idx, entry, exc in outcomes:
        if exc is not None:
            t = targets[idx]
            results.append(
                {
                    "component": (t.get("component") or "").strip(),
                    "env": (t.get("env") or "").strip(),
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
        else:
            results.append(entry)

    succeeded = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    return {
        "ok": len(failed) == 0,
        "commit_message": commit_message,
        "workers": workers_capped,
        "requested_count": len(targets),
        "success_count": len(succeeded),
        "failure_count": len(failed),
        "results": results,
    }


# ── Tag deploy: helpers ──────────────────────────────────────────────────────
# Ports the version-tag update logic from
# devops/repositories.nosync/deploy_tags_ecs_asg.py. Unlike ``redeploy_targets``
# (which only toggles a trailing newline to re-trigger the pipeline), this
# path actually changes the deployed version:
#   * ECS: rewrites ``lv_component_build`` in fargate_config.yml / .yaml.
#   * ASG: rewrites ``oss_component_svn_revision`` in asg_config.js /
#          asg_config.json via a string-level regex, preserving any
#          existing suffix like ``-AUTO`` when the incoming tag is
#          numeric-only.
# The commit message format is fixed: ``Deployed <tag> in <env> - <ticket> -
# LV_CD_DEPLOY`` so the CD pipeline picks it up.

# Match ONLY quoted ``"oss_component_svn_revision": "..."`` — same as the
# reference script.
_ASG_REVISION_RE = re.compile(r'("oss_component_svn_revision":\s*")([^"]+)(")')

# Surgical match for the ``lv_component_build`` line in an ECS fargate
# config. Captures the prefix (``lv_component_build:`` + spaces), any
# quote character, the value itself, the closing quote, and the trailing
# whitespace / newline. Replacing only the value byte-preserves everything
# else in the file — critical because previously we did a full YAML
# ``safe_load`` + ``dump`` round-trip that would silently reformat list
# indentation, coerce integers to floats, strip comments, and drop the
# trailing newline. Users only asked for a version bump; anything else
# in the diff is a bug.
_ECS_BUILD_RE = re.compile(
    r"""(?m)                              # multiline: ^ / $ per line
    ^(?P<prefix>\s*lv_component_build\s*:\s*)  # key + colon + spaces
    (?P<open_q>[\"']?)                        # optional opening quote
    (?P<value>[^\"'\r\n#]+?)                  # the version value
    (?P<close_q>[\"']?)                       # optional closing quote
    (?P<trail>\s*(?:\#[^\r\n]*)?)$            # optional inline comment + EOL
    """,
    re.VERBOSE,
)

# A deploy tag must look like a full version literal: ``N.N`` or ``N.N.N``
# with an optional suffix separated by either ``-`` (e.g. ``-AUTO`` /
# ``-SNAPSHOT``) or ``/`` (e.g. CDN path suffixes like ``/mfe``). Bare
# integers like ``26`` are rejected — they're almost always a parsing
# mistake where the AI split ``27.0.26`` into branch ``27.0`` + tag ``26``.
_DEPLOY_TAG_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:[-/][A-Za-z0-9._/\-]+)?$")


def _validate_deploy_tag_format(tag: str) -> Optional[str]:
    """HARD check: fail if ``tag`` doesn't look like a full version literal.

    Catches the AI misparse where ``27.0.26`` gets split into branch
    ``27.0`` + tag ``26``. Returns an error string or ``None``.
    """
    if not _DEPLOY_TAG_RE.match(tag):
        return (
            f"Tag '{tag}' is not a full version literal (expected `N.N` or "
            "`N.N.N`, optionally with a suffix separated by `-` or `/` — "
            "e.g. `27.0.26`, `4.0.10-AUTO`, `27.0.53/mfe`). Common cause: "
            "the AI split a version like `27.0.26` into branch=`27.0` + "
            "tag=`26`. Ask the user for the full tag literally as they "
            "provided it, including any `/suffix` (do NOT drop suffixes)."
        )
    return None


def _check_tag_branch_major_mismatch(tag: str, branch: str) -> Optional[str]:
    """SOFT check: return a warning string when the tag's major differs
    from the branch's major, or ``None`` when they match / can't be parsed.

    This is deliberately NOT a hard failure. Some components legitimately
    deploy a version whose major differs from the branch folder they
    live under (e.g. ``bi-integration`` on branch ``27.0`` shipping
    ``26.2.1``). The user always sees the diff before approval, so a
    warning surfaced alongside the diff is enough friction — blocking
    would prevent valid deploys.
    """
    branch_major_m = re.match(r"^(\d+)", branch)
    tag_major_m = re.match(r"^(\d+)", tag)
    if (
        branch_major_m
        and tag_major_m
        and branch_major_m.group(1) != tag_major_m.group(1)
    ):
        return (
            f"Tag '{tag}' major ({tag_major_m.group(1)}) does not match branch "
            f"'{branch}' major ({branch_major_m.group(1)}). This is allowed — "
            "some components legitimately deploy a version whose major differs "
            "from the branch folder they live under. Double-check the diff "
            "and confirm the intent with the user before proceeding."
        )
    return None


def _update_content_for_tag(deploy_type: str, content: str, tag: str) -> str:
    """Return the transformed config content for a tag deploy.

    Both ECS and ASG paths do a byte-preserving surgical regex substitution
    on ONLY the version-carrying line. This is deliberately not a YAML /
    JSON round-trip: users ask for a version bump, and anything else in
    the diff (list indentation, integer-vs-float, comments, trailing
    newline) is a bug they'd have to notice and reject. The ``-AUTO``
    suffix is preserved when the incoming tag is numeric-only, matching
    the local ``deploy_tags_ecs_asg.py`` behavior.
    """
    if deploy_type == "ecs":
        def _ecs_replace(match: "re.Match[str]") -> str:
            groups = match.groupdict()
            current = (groups["value"] or "").strip()
            updated = tag
            if "-" in current and "-" not in tag:
                suffix = current.split("-", 1)[1]
                updated = f"{tag}-{suffix}"
            return f"{groups['prefix']}{groups['open_q']}{updated}{groups['close_q']}{groups['trail']}"

        new_content, n = _ECS_BUILD_RE.subn(_ecs_replace, content)
        if n == 0:
            raise ValueError(
                "ECS config has no `lv_component_build:` line to update. "
                "The file may be malformed or the field is nested under a "
                "block this tool doesn't currently support."
            )
        if n > 1:
            raise ValueError(
                f"ECS config has {n} `lv_component_build:` lines — refusing "
                "to update ambiguously. Inspect the file and remove duplicates."
            )
        return new_content

    def _asg_replace(match: "re.Match[str]") -> str:
        current = match.group(2)
        updated = tag
        # Preserve suffixes like ``-AUTO`` when the incoming tag is numeric-only.
        if "-" in current and "-" not in tag:
            suffix = current.split("-", 1)[1]
            updated = f"{tag}-{suffix}"
        return f"{match.group(1)}{updated}{match.group(3)}"

    return _ASG_REVISION_RE.sub(_asg_replace, content)


def _build_tag_commit_message(tag: str, env: str, ticket: str) -> str:
    """``Deployed 27.0.9 in eu1 - DEVOPS-52986 - LV_CD_DEPLOY`` — or, when
    ``ticket`` is empty, ``Deployed 27.0.9 in eu1 - LV_CD_DEPLOY``."""
    parts = [f"Deployed {tag} in {env}"]
    if ticket:
        parts.append(ticket)
    parts.append(LV_CD_DEPLOY_COMMIT_MESSAGE)
    return " - ".join(parts)


def _find_tag_config_file(
    repo_component: str,
    env: str,
    branch: str,
    deploy_type: str,
) -> Optional[tuple[str, dict[str, Any]]]:
    """Fetch the config file for a tag deploy: fargate for ECS, asg for ASG."""
    candidates = FARGATE_FILES if deploy_type == "ecs" else ASG_FILES
    for filename in candidates:
        file_json = _fetch_file(repo_component, env, filename, branch)
        if file_json:
            return filename, file_json
    return None


def _detect_deploy_type_for(
    component: str, env: str, branch: str
) -> Optional[str]:
    """Probe fargate and asg config files in the env/branch. Returns 'ecs' /
    'asg' when found, else None."""
    # ECS candidate uses the ECS name-map (e.g. emailgateway -> email_gateway).
    ecs_repo_component = _normalize_component_for_repo(component, "ecs")
    for filename in FARGATE_FILES:
        if _fetch_file(ecs_repo_component, env, filename, branch):
            return "ecs"
    # ASG candidate uses the ASG map (e.g. recordingpurge -> recording_purge).
    asg_repo_component = _normalize_component_for_repo(component, "asg")
    for filename in ASG_FILES:
        if _fetch_file(asg_repo_component, env, filename, branch):
            return "asg"
    return None


def _detect_deploy_types_present(
    component: str, env: str, branch: str
) -> list[str]:
    """Return every deploy type present for a (component, env, branch).

    Unlike ``_detect_deploy_type_for`` which returns the first hit, this probes
    BOTH the fargate and asg name-maps and returns every type whose config file
    exists. Used by the bulk find-* tools to detect the ambiguous case where a
    component ships BOTH ``fargate_config.*`` and ``asg_config.*`` in the same
    env/branch (e.g. ``siq-complete-asr-config`` in ``stg4``). The caller then
    routes the entry to ``needs_info`` so the AI must ask the user which
    deployment type to act on before proceeding.

    Returns ``[]`` when nothing is found, ``["ecs"]`` or ``["asg"]`` for the
    unambiguous single-type case, or ``["asg", "ecs"]`` (sorted) when both
    exist.
    """
    present: set[str] = set()
    ecs_repo_component = _normalize_component_for_repo(component, "ecs")
    for filename in FARGATE_FILES:
        if _fetch_file(ecs_repo_component, env, filename, branch):
            present.add("ecs")
            break
    asg_repo_component = _normalize_component_for_repo(component, "asg")
    for filename in ASG_FILES:
        if _fetch_file(asg_repo_component, env, filename, branch):
            present.add("asg")
            break
    return sorted(present)


def _fuzzy_match_env_files(
    requested_path: str,
    env_files: list[dict[str, Any]],
    limit: int = 8,
    min_score: float = 0.45,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` files from ``env_files`` sorted by similarity
    to ``requested_path``. Exact basename matches are always included first.

    Used when the user asks to modify a companion file whose exact path
    doesn't exist in the repo — we surface the closest candidates so the AI
    host can present them for the user to pick from.
    """
    if not env_files or not requested_path:
        return []
    req_base = requested_path.rsplit("/", 1)[-1].lower()
    req_full = requested_path.lower()

    scored: list[tuple[float, dict[str, Any]]] = []
    for f in env_files:
        path = f.get("path") or ""
        if not path:
            continue
        base = path.rsplit("/", 1)[-1].lower()
        base_score = difflib.SequenceMatcher(None, req_base, base).ratio()
        path_score = difflib.SequenceMatcher(None, req_full, path.lower()).ratio()
        score = max(base_score, path_score)
        # Boost when basename is an exact match or one contains the other.
        if base == req_base:
            score = 1.0
        elif req_base and (req_base in base or base in req_base):
            score = max(score, 0.85)
        if score >= min_score:
            scored.append((score, f))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"path": f["path"], "name": f.get("name"), "similarity": round(score, 3)}
        for score, f in scored[:limit]
    ]


def _resolve_extra_update(
    repo_component: str,
    env: str,
    branch: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Fetch the current content of a companion file and build its diff payload.

    ``extra`` must contain ``file_path`` (full repo path, e.g.
    ``eu1/scheduling.properties``) and ``new_content``. Raises on validation
    errors or missing files so the caller can surface them per-target.
    """
    file_path = (extra.get("file_path") or "").strip()
    new_content = extra.get("new_content")
    if not file_path or new_content is None:
        raise ValueError(
            "extra_updates entries require both `file_path` and `new_content`."
        )
    env_prefix = f"{env}/"
    if not file_path.startswith(env_prefix):
        raise ValueError(
            f"extra_updates.file_path '{file_path}' must live under '{env_prefix}'."
        )

    file_json = _fetch_file_at_path(repo_component, file_path, branch)
    if not file_json:
        raise FileNotFoundError(
            f"Extra file not found: '{file_path}' on branch '{branch}'."
        )
    original_content = _decode(file_json)
    return {
        "file_path": file_path,
        "new_content": new_content,
        "diff": _make_unified_diff(original_content, new_content, file_path),
        "has_changes": original_content != new_content,
        "last_commit_id": file_json.get("last_commit_id") or file_json.get("commit_id"),
    }


def _resolve_tag_deploy_target(
    component: str,
    env: str,
    branch: str,
    deploy_type: str,
    tag: str,
    ticket: str,
    extra_updates: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Fetch the config file and build the target payload for a single deploy.

    ``extra_updates`` is an optional list of ``{file_path, new_content}`` items
    for companion files that will land in the *same* commit as the tag change.
    """
    repo_component = _normalize_component_for_repo(component, deploy_type)
    file_info = _find_tag_config_file(repo_component, env, branch, deploy_type)
    if file_info is None:
        raise FileNotFoundError(
            f"No {deploy_type} config file for '{component}' in env '{env}' on branch '{branch}'."
        )
    filename, file_json = file_info
    original_content = _decode(file_json)
    try:
        new_content = _update_content_for_tag(deploy_type, original_content, tag)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Failed to apply tag '{tag}' to {env}/{filename}: {exc}"
        ) from exc

    file_path = f"{env}/{filename}"
    _, group = _endpoint()

    # Resolve companion file updates. Missing files → unresolved_extras with
    # fuzzy-matched candidates (so the caller can ask the user to disambiguate)
    # instead of failing the whole target. Malformed entries (missing fields,
    # wrong env prefix) still raise ValueError.
    resolved_extras: list[dict[str, Any]] = []
    unresolved_extras: list[dict[str, Any]] = []
    env_files_cache: Optional[list[dict[str, Any]]] = None
    for extra in (extra_updates or []):
        try:
            resolved_extras.append(_resolve_extra_update(repo_component, env, branch, extra))
        except FileNotFoundError as exc:
            if env_files_cache is None:
                env_files_cache = _list_env_tree_files(repo_component, env, branch, True) or []
            requested_path = (extra.get("file_path") or "").strip()
            unresolved_extras.append(
                {
                    "requested": {
                        "file_path": requested_path,
                        "new_content_provided": extra.get("new_content") is not None,
                    },
                    "error": str(exc),
                    "candidates": _fuzzy_match_env_files(requested_path, env_files_cache),
                }
            )

    return {
        "component": component,
        "repo_component": repo_component,
        "env": env,
        "branch": branch,
        "type": deploy_type,
        "tag": tag,
        "ticket": ticket,
        "repo_path": f"{group}/{repo_component}-config",
        "file_path": file_path,
        "filename": filename,
        "new_content": new_content,
        "diff": _make_unified_diff(original_content, new_content, file_path),
        "commit_message": _build_tag_commit_message(tag, env, ticket),
        "has_changes": original_content != new_content,
        "last_commit_id": file_json.get("last_commit_id") or file_json.get("commit_id"),
        "extra_updates": resolved_extras,
        "unresolved_extras": unresolved_extras,
    }


def _fetch_pipeline_for_commit(
    repo_component: str, sha: str, branch: str, wait_seconds: float = 2.0
) -> Optional[dict[str, Any]]:
    """After committing, look up the pipeline triggered by ``sha`` on ``branch``."""
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    pipelines = _get_pipelines_for_sha(repo_component, sha, ref=branch)
    if not pipelines:
        return None
    p = pipelines[0]
    return {"pipeline_url": p.get("web_url"), "pipeline_id": p.get("id"), "status": p.get("status")}


# ── Tag deploy: tools ────────────────────────────────────────────────────────

@mcp.tool()
def find_tag_deploy_targets(
    deployments: list[dict[str, Any]],
    workers: int = _WORKERS_DEFAULT,
) -> dict[str, Any]:
    """Preview a batch of tag deployments. **Read-only.**

    Accepts partial input. For each deployment the tool tries to fill in the
    blanks and reports what it needs from the user:

      * Required from the user: ``component``, ``env``, ``branch``, ``tag``.
        The ``tag`` MUST be the full version literal the user provided
        (e.g. ``"27.0.26"`` or ``"4.0.10-AUTO"``) — do NOT split ``27.0.26``
        into branch=``27.0`` + tag=``26``. The server rejects bare integer
        tags and tags whose major doesn't match the branch's major.
      * Optional / auto-detected: ``type`` (``ecs`` / ``asg``) — inferred by
        probing ``fargate_config.yml``/``.yaml`` first, then
        ``asg_config.js``/``.json``. **Ambiguity**: a few components ship
        BOTH kinds of config in the same env/branch (e.g.
        ``siq-complete-asr`` in ``stg4``). These land in ``needs_info`` with
        ``missing: ['type']`` and ``types_present: ['asg', 'ecs']`` — the AI
        must ask the user which type to deploy and re-invoke with ``type``
        set explicitly (or submit one entry per type to deploy both).
      * **Optional but recommended**: ``ticket`` (e.g. ``DEVOPS-52986``).
        When provided it's included in the commit message
        (``Deployed <tag> in <env> - <ticket> - LV_CD_DEPLOY``). When
        omitted, that segment is dropped
        (``Deployed <tag> in <env> - LV_CD_DEPLOY``) and the target carries
        a non-blocking warning suggesting the AI ask the user if they'd
        like to add one — the deploy is never blocked on it.
      * **Optional** ``extra_updates`` — a list of companion files to update
        in the *same* commit as the tag change. Each entry:
        ``{"file_path": "<env>/<sub/path/file.ext>", "new_content": "..."}``.
        Use ``list_env_files`` first so the user can pick which companion
        file(s) to modify; supply the desired new content per file. The
        tool fetches each file, computes its diff, and returns it under the
        target's ``extra_updates`` array — the AI host **must** display those
        diffs alongside the main tag diff for user approval. If a requested
        ``file_path`` doesn't exist, the target is routed to ``needs_info``
        with the closest matching files under ``unresolved_extras[*].candidates``
        so the user can pick / correct the path.

    Two possible companion-file flows:
      1. **User asks upfront** ("deploy 27.0.26 of scheduling in eu1 and also
         update `scheduling.properties`"). Call ``list_env_files`` if needed to
         confirm the exact file path, then pass ``extra_updates`` in this call.
      2. **User asks nothing** about companions. Every ready target comes back
         with a ``companion_files_hint`` field; the AI host **must** ask the
         user "do you also need to modify any companion config files in this
         env?" before asking for final approval on the tag change. On yes,
         call ``list_env_files``, collect new content, re-invoke this tool
         with ``extra_updates``.

    Per-deployment behavior when everything is present:
      * ECS → rewrites ``lv_component_build`` in ``fargate_config.yml`` / ``.yaml``.
      * ASG → rewrites ``oss_component_svn_revision`` in ``asg_config.js`` /
        ``.json`` via a string-level regex; a numeric-only incoming tag
        preserves any existing suffix like ``-AUTO``.
      * Any ``extra_updates`` land in the same commit as the tag change.

    Response shape:
      * ``targets``    — ready-to-deploy entries with ``diff``, ``new_content``,
                          ``commit_message`` (format:
                          ``Deployed <tag> in <env> - <ticket> - LV_CD_DEPLOY``),
                          and ``extra_updates`` (with per-file ``diff`` /
                          ``new_content`` / ``has_changes``).
      * ``needs_info`` — entries whose deploy is blocked pending one more
                          piece of info from the user (usually ``ticket``).
      * ``errors``     — hard failures (missing config file, disallowed env,
                          bad ``extra_updates`` path, etc.).

    The AI host **must** display each ready target's ``diff`` AND every
    ``extra_updates[*].diff`` to the user, get explicit confirmation, then
    call ``deploy_tag_targets`` with the approved target objects unchanged.
    Do NOT skip diff review.

    **When NOT to use this tool.** ``deploy_tag_targets`` locks the main
    file's ``new_content`` to the tag rewrite (``lv_component_build`` for
    ECS, ``oss_component_svn_revision`` for ASG). If the user asks for a
    change that ALSO edits other fields inside the same main config file
    (e.g. ``instance_type``, ``min_size``, ``max_size``, scaling
    thresholds), use ``find_multi_file_commit_targets`` +
    ``commit_multi_file_targets`` instead — that tool pair takes an
    arbitrary set of file updates and commits them ATOMICALLY in a single
    commit. NEVER split a multi-file change across two separate
    ``deploy_tag_targets`` / ``redeploy_targets`` / ``commit_multi_file_targets``
    invocations — that races the concurrency check and forces
    ``last_commit_id`` to be dropped.

    Bulk previews fan out across ``workers`` threads (1..30, default 10).
    Per-entry failures are isolated. ``targets`` / ``needs_info`` / ``errors``
    are kept in input order.
    """
    if not deployments:
        return {"ok": False, "error": "deployments must be a non-empty list."}

    workers_capped = _cap_workers(workers)

    targets: list[dict[str, Any]] = []
    needs_info: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def _process(entry: dict[str, Any]) -> dict[str, Any]:
        """Return one bucketed dict: ``{"_kind": "target"|"needs_info"|"error", "payload": ...}``."""
        component = (entry.get("component") or "").strip()
        env = (entry.get("env") or "").strip()
        branch = (entry.get("branch") or "").strip()
        deploy_type = (entry.get("type") or "").strip().lower()
        tag = (entry.get("tag") or "").strip()
        ticket = (entry.get("ticket") or "").strip()
        extra_updates_in = entry.get("extra_updates") or []

        provided_summary = {
            "component": component or None,
            "env": env or None,
            "branch": branch or None,
            "type": deploy_type or None,
            "tag": tag or None,
            "ticket": ticket or None,
            "extra_updates_count": len(extra_updates_in),
        }

        # ── 1. Core fields must be present. Otherwise ask the user. ──────
        core_missing = [
            name
            for name, val in (
                ("component", component),
                ("env", env),
                ("branch", branch),
                ("tag", tag),
            )
            if not val
        ]
        if core_missing:
            return {
                "_kind": "needs_info",
                "payload": {
                    "provided": provided_summary,
                    "missing": core_missing,
                    "reason": (
                        "Missing core deploy fields. Ask the user for these "
                        "values, then call find_tag_deploy_targets again with "
                        "the complete entry."
                    ),
                },
            }

        # ── 2. Env whitelist. ────────────────────────────────────────────
        denied = _reject_non_redeployable_env(env)
        if denied:
            return {
                "_kind": "error",
                "payload": {
                    "provided": provided_summary,
                    "error": denied["error"],
                    "allowed_envs": denied["allowed_envs"],
                },
            }

        # ── 2b. Tag format (HARD) — reject bare integers etc. ────────────
        tag_error = _validate_deploy_tag_format(tag)
        if tag_error:
            return {
                "_kind": "error",
                "payload": {"provided": provided_summary, "error": tag_error},
            }

        # ── 2c. Tag/branch major cross-check (SOFT — warning only). ──────
        # Attach to the target after resolution so the diff + warning are
        # shown together and the user can approve knowingly.
        tag_major_warning = _check_tag_branch_major_mismatch(tag, branch)

        # ── 3. Type: use given, else auto-detect from config file. ───────
        if deploy_type and deploy_type not in ("ecs", "asg"):
            return {
                "_kind": "error",
                "payload": {
                    "provided": provided_summary,
                    "error": f"Invalid type '{deploy_type}' — must be 'ecs' or 'asg' (or omit to auto-detect).",
                },
            }

        # Always probe present deploy types — needed for the auto-detect
        # ambiguity check AND for stamping ``both_configs_present`` on the
        # returned target so ``deploy_tag_targets`` knows to fire the ASG
        # API pipeline trigger.
        types_present = _detect_deploy_types_present(component, env, branch)
        both = len(types_present) > 1
        if not deploy_type:
            if not types_present:
                return {
                    "_kind": "error",
                    "payload": {
                        "provided": provided_summary,
                        "error": (
                            f"Could not auto-detect deploy type: no fargate or asg "
                            f"config found for '{component}' in env '{env}' on branch '{branch}'. "
                            "Verify component / env / branch."
                        ),
                    },
                }
            if both:
                # A few components ship BOTH fargate_config.* and asg_config.*
                # in the same env/branch (e.g. siq-complete-asr in stg4).
                # Refuse to guess — force the AI to ask the user.
                return {
                    "_kind": "needs_info",
                    "payload": {
                        "provided": provided_summary,
                        "missing": ["type"],
                        "types_present": types_present,
                        "reason": (
                            f"Component '{component}' has BOTH ECS (fargate) "
                            f"and ASG config files in env '{env}' on branch "
                            f"'{branch}'. Ask the user which deployment type "
                            "to update — 'ecs' or 'asg' — then re-invoke "
                            "find_tag_deploy_targets with `type` set "
                            "explicitly. If the user wants to deploy the tag "
                            "to both, submit two entries (one per type)."
                        ),
                    },
                }
            deploy_type = types_present[0]

        # ── 4. Ticket is optional — recommended but never blocking. When
        # omitted, ``_build_tag_commit_message`` drops that segment and a
        # non-blocking warning is attached below so the AI can offer the
        # user a chance to add one without gating the deploy on it.

        # ── 5. Build the diff / new_content / commit_message. ────────────
        try:
            target = _resolve_tag_deploy_target(
                component=component,
                env=env,
                branch=branch,
                deploy_type=deploy_type,
                tag=tag,
                ticket=ticket,
                extra_updates=extra_updates_in,
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            return {
                "_kind": "error",
                "payload": {
                    "provided": provided_summary,
                    "type_used": deploy_type,
                    "error": str(exc),
                },
            }

        # ── 5b. Companion file resolution outcomes. ──────────────────────
        # If any requested extra_updates could not be located, route the
        # target to needs_info with candidate suggestions so the AI has to
        # come back to the user before proceeding.
        unresolved = target.pop("unresolved_extras", [])
        if unresolved:
            return {
                "_kind": "needs_info",
                "payload": {
                    "provided": {**provided_summary, "type": deploy_type},
                    "missing": ["extra_updates_resolution"],
                    "unresolved_extras": unresolved,
                    "resolved_extras": [
                        {"file_path": e["file_path"], "has_changes": e["has_changes"]}
                        for e in target.get("extra_updates", [])
                    ],
                    "reason": (
                        "One or more requested companion files could not be "
                        "found in the repo. For each entry under "
                        "`unresolved_extras`, show the user the `candidates` "
                        "list (closest matching files in the env) and ask them "
                        "to pick the correct file path — or run `list_env_files` "
                        "to see the full listing. Then re-invoke "
                        "`find_tag_deploy_targets` with the corrected "
                        "`extra_updates`. Do NOT guess a path yourself."
                    ),
                },
            }

        # If no companion updates were requested, hint the AI to ask the
        # user whether any are needed before final approval.
        if not target.get("extra_updates"):
            target["companion_files_hint"] = (
                "No companion file updates were requested. Before asking for "
                "final approval on this deployment, ask the user: 'Do you also "
                "need to modify any companion config files (e.g. property "
                "files) in this env as part of this deployment?' If yes, call "
                "`list_env_files` for this component/env/branch, show the file "
                "list, collect the user's new content per file, then re-invoke "
                "`find_tag_deploy_targets` with `extra_updates` populated."
            )

        # Soft warnings (major mismatch, etc.) — the deploy is still
        # allowed; the AI must display these to the user alongside the diff.
        if tag_major_warning:
            target.setdefault("warnings", []).append(tag_major_warning)
        if not ticket:
            target.setdefault("warnings", []).append(
                "No ticket was provided — the commit message will omit a "
                "ticket reference. Consider asking the user if they'd like "
                "to include one (e.g. 'DEVOPS-52986'), but this is optional; "
                "proceed without one if they confirm."
            )

        # Stamp both-configs marker so ``deploy_tag_targets`` fires the
        # ASG API pipeline trigger for these components (same treatment as
        # ``contact`` / ``recording``).
        if both:
            target["both_configs_present"] = True
            target["types_present"] = types_present

        return {"_kind": "target", "payload": target}

    outcomes = _threaded_map(_process, list(deployments), workers_capped)

    for idx, out, exc in outcomes:
        if exc is not None:
            errors.append(
                {
                    "provided": {"index": idx},
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            continue
        kind = out.get("_kind")
        payload = out.get("payload")
        if kind == "target":
            targets.append(payload)
        elif kind == "needs_info":
            needs_info.append(payload)
        else:
            errors.append(payload)

    return {
        "ok": True,
        "workers": workers_capped,
        "requested_count": len(deployments),
        "match_count": len(targets),
        "needs_info_count": len(needs_info),
        "error_count": len(errors),
        "targets": targets,
        "needs_info": needs_info,
        "errors": errors,
        "instruction": (
            "First, if `needs_info` is non-empty, handle it: "
            "(a) entries with missing core fields (`component`, `env`, "
            "`branch`, `tag`) — ask the user for those values and re-invoke "
            "this tool with the completed entries; "
            "(b) entries with `missing: ['type']` — the component has BOTH "
            "ECS (fargate) and ASG configs in this env/branch (see "
            "`types_present`). Ask the user which deployment type to update "
            "— 'ecs' or 'asg' — and re-invoke with `type` set explicitly. "
            "If the user wants both, submit two entries (one per type). Do "
            "NOT default to one type on your own. "
            "(c) entries with `missing: ['extra_updates_resolution']` — show "
            "the user each `unresolved_extras[*].requested.file_path` alongside "
            "its `candidates` list (the closest matching files in the env), "
            "ask them to pick the correct path (or call `list_env_files` to "
            "browse), then re-invoke this tool with the corrected "
            "`extra_updates`. DO NOT invent values or auto-pick a candidate. "
            "Second, for each ready target you MUST present ALL of the "
            "following to the user before asking for approval: "
            "(1) the full `diff` (main file, standard unified diff), "
            "(2) every `extra_updates[*].diff`, "
            "(3) the `commit_message` field verbatim, "
            "(4) the resolved `component`, `env`, `branch`, `type`, `tag`, `ticket`, "
            "(5) every string in `warnings` (if the field is present) — these "
            "are non-blocking cautions like tag/branch major mismatch that the "
            "user should acknowledge before you proceed. "
            "Third, if a target carries `companion_files_hint`, follow it — "
            "ASK the user whether any companion config files in this env need "
            "to be modified as part of this deployment BEFORE asking for final "
            "approval. Use `list_env_files` to show options and re-invoke "
            "this tool with `extra_updates` populated if the user says yes. "
            "Only after the user explicitly approves may you call "
            "`deploy_tag_targets` with the exact target objects unchanged and "
            "`confirmed_by_user=True`. Targets where `has_changes` is false "
            "will produce a no-op commit."
        ),
    }


@mcp.tool()
def deploy_tag_targets(
    targets: list[dict[str, Any]],
    confirmed_by_user: bool = False,
    trigger_pipeline_for_asg_components: bool = True,
    wait_for_pipeline_seconds: float = 2.0,
    workers: int = _WORKERS_DEFAULT,
) -> dict[str, Any]:
    """Commit the confirmed tag deployments to their respective config repos.

    **Two-step contract (enforced):**
      1. You must first call ``find_tag_deploy_targets`` to get the ``diff``
         for the main file *and* every ``extra_updates[*].diff`` per target.
         Show every one of those diffs to the user.
      2. Only after the user explicitly approves may you call this tool with
         ``confirmed_by_user=True``. Each target must retain the ``diff``
         from the preview; each ``extra_updates`` entry must also retain its
         own ``diff``. Any target or extra missing ``diff`` is rejected.

    Each target must contain ``env``, ``branch``, ``file_path``,
    ``new_content``, ``commit_message``, and ``diff`` — plus either
    ``component`` or ``repo_component`` and (recommended) ``type``, ``tag``,
    ``ticket``. These are produced by ``find_tag_deploy_targets`` — pass them
    back unchanged after user approval.

    If a target carries ``extra_updates`` (``{file_path, new_content, diff,
    last_commit_id?}``), all those companion files land in the *same* commit
    as the tag change — one commit per target, one CD pipeline trigger.

    Per-target behavior:
      * Env whitelist is re-checked.
      * The commit uses each target's own ``commit_message`` (already in the
        ``Deployed <tag> in <env> - <ticket> - LV_CD_DEPLOY`` format).
      * ``last_commit_id`` is honored per file to prevent racing another writer.
      * For ASG components in ``ASG_PIPELINE_TRIGGER_COMPONENTS``
        (``contact``, ``recording``) an extra pipeline is triggered after
        the commit — same as ``redeploy_targets``. The same trigger fires
        for any target carrying ``both_configs_present=True`` (components
        with both ECS and ASG configs, e.g. ``siq-complete-asr``) because
        the CD auto-trigger on those repos only picks up the ECS side.
      * After a successful commit we optionally wait ``wait_for_pipeline_seconds``
        and look up the pipeline actually triggered by the commit; the URL
        (if found) is returned as ``pipeline.pipeline_url``.

    Multi-target commits run in parallel across ``workers`` threads (1..30,
    default 10). Per-target failures are isolated. ``results`` are returned
    in the same order as the input ``targets``.

    Returns per-target commit info and success/failure counts.
    """
    if not targets:
        return {"ok": False, "error": "targets must be a non-empty list."}

    if not confirmed_by_user:
        return {
            "ok": False,
            "needs_confirmation": True,
            "requested_count": len(targets),
            "instruction": (
                "Diff review is mandatory. Present the main `diff` field AND "
                "every `extra_updates[*].diff` from `find_tag_deploy_targets` "
                "to the user, get explicit approval, and only then call this "
                "tool again with `confirmed_by_user=True`. Do NOT set the flag "
                "on the user's behalf."
            ),
        }

    workers_capped = _cap_workers(workers)

    def _process(t: dict[str, Any]) -> dict[str, Any]:
        component = (t.get("component") or "").strip()
        env = (t.get("env") or "").strip()
        branch = (t.get("branch") or "").strip()
        file_path = (t.get("file_path") or "").strip()
        new_content = t.get("new_content")
        deploy_type = (t.get("type") or "").strip().lower()
        repo_component = (t.get("repo_component") or "").strip()
        commit_message = (t.get("commit_message") or "").strip()
        tag = (t.get("tag") or "").strip()
        ticket = (t.get("ticket") or "").strip()
        last_commit_id = t.get("last_commit_id") or None
        extra_updates = t.get("extra_updates") or []
        diff = t.get("diff")

        if not (component and env and branch and file_path and commit_message) or new_content is None:
            return {
                "component": component,
                "ok": False,
                "error": (
                    "Target missing required fields "
                    "(component, env, branch, file_path, new_content, commit_message)."
                ),
            }

        if not diff:
            return {
                "component": component,
                "ok": False,
                "error": (
                    "Target has no `diff` field — refusing to commit. "
                    "Regenerate the target via `find_tag_deploy_targets` "
                    "and show the diff to the user before committing."
                ),
            }

        # Every extra update must also carry a `diff` field from the preview.
        missing_extra_diff_idx: Optional[int] = None
        for ex_idx, ex in enumerate(extra_updates):
            if not (ex.get("diff") or "").strip():
                missing_extra_diff_idx = ex_idx
                break
        if missing_extra_diff_idx is not None:
            return {
                "component": component,
                "env": env,
                "ok": False,
                "error": (
                    f"extra_updates[{missing_extra_diff_idx}] has no `diff` field — "
                    "refusing to commit. Regenerate via `find_tag_deploy_targets` "
                    "so every companion file's diff can be shown to the user."
                ),
            }

        denied = _reject_non_redeployable_env(env)
        if denied:
            return {"component": component, "env": env, "ok": False, "error": denied["error"]}

        if not repo_component:
            repo_component = (
                _normalize_component_for_repo(component, deploy_type)
                if deploy_type in ("ecs", "asg")
                else component
            )

        # Build the commit action list: main tag file + optional companions.
        actions: list[dict[str, Any]] = []
        main_action: dict[str, Any] = {
            "action": "update",
            "file_path": file_path,
            "content": new_content,
        }
        if last_commit_id:
            main_action["last_commit_id"] = last_commit_id
        actions.append(main_action)

        # Validate + include extra updates.
        extra_error: Optional[str] = None
        included_extras: list[dict[str, Any]] = []
        for ex_idx, ex in enumerate(extra_updates):
            ex_path = (ex.get("file_path") or "").strip()
            ex_new = ex.get("new_content")
            if not ex_path or ex_new is None:
                extra_error = (
                    f"extra_updates[{ex_idx}] missing file_path or new_content."
                )
                break
            if not ex_path.startswith(f"{env}/"):
                extra_error = (
                    f"extra_updates[{ex_idx}].file_path '{ex_path}' must be under '{env}/'."
                )
                break
            ex_action: dict[str, Any] = {
                "action": "update",
                "file_path": ex_path,
                "content": ex_new,
            }
            if ex.get("last_commit_id"):
                ex_action["last_commit_id"] = ex["last_commit_id"]
            actions.append(ex_action)
            included_extras.append(
                {"file_path": ex_path, "has_changes": ex.get("has_changes")}
            )

        entry: dict[str, Any] = {
            "component": component,
            "repo_component": repo_component,
            "env": env,
            "branch": branch,
            "file_path": file_path,
            "type": deploy_type or None,
            "tag": tag or None,
            "ticket": ticket or None,
            "commit_message": commit_message,
            "extra_updates": included_extras,
            "action_count": len(actions),
        }

        if extra_error:
            entry["ok"] = False
            entry["error"] = extra_error
            return entry

        try:
            res = _post_commit_multi(repo_component, branch, commit_message, actions)
        except RuntimeError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            return entry

        if res.status_code in (200, 201):
            data = res.json()
            commit_sha = data.get("id")
            entry["ok"] = True
            entry["commit_id"] = commit_sha
            entry["commit_url"] = data.get("web_url")

            if commit_sha:
                pipeline_info = _fetch_pipeline_for_commit(
                    repo_component, commit_sha, branch, wait_seconds=wait_for_pipeline_seconds
                )
                if pipeline_info is not None:
                    entry["pipeline"] = pipeline_info

            # ASG-side deploys need an explicit CD pipeline trigger for
            # two categories of components:
            #   * ``contact`` / ``recording`` — hardcoded in
            #     ``ASG_PIPELINE_TRIGGER_COMPONENTS``.
            #   * Components that ship BOTH ECS and ASG configs — the CD
            #     auto-trigger picks up the ECS side; the ASG side needs
            #     the explicit POST /pipeline. Detected via
            #     ``both_configs_present`` stamped by
            #     ``find_tag_deploy_targets``.
            both_configs_present = bool(t.get("both_configs_present"))
            if (
                deploy_type == "asg"
                and trigger_pipeline_for_asg_components
                and (
                    component in ASG_PIPELINE_TRIGGER_COMPONENTS
                    or both_configs_present
                )
            ):
                time.sleep(5)
                entry["extra_pipeline"] = _trigger_asg_pipeline(repo_component, branch, env)
        else:
            entry["ok"] = False
            entry["status_code"] = res.status_code
            entry["error"] = (res.text or "")[:500]

        return entry

    outcomes = _threaded_map(_process, list(targets), workers_capped)

    results: list[dict[str, Any]] = []
    for idx, entry, exc in outcomes:
        if exc is not None:
            t = targets[idx]
            results.append(
                {
                    "component": (t.get("component") or "").strip(),
                    "env": (t.get("env") or "").strip(),
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
        else:
            results.append(entry)

    succeeded = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    return {
        "ok": len(failed) == 0,
        "workers": workers_capped,
        "requested_count": len(targets),
        "success_count": len(succeeded),
        "failure_count": len(failed),
        "results": results,
    }


# ── Multi-file atomic commit: helpers ───────────────────────────────────────
# General-purpose atomic multi-file commit for eng ``-config`` repos, for the
# cases the tag-deploy / redeploy tools can't cover on their own:
#
#   * ``redeploy_targets`` only toggles the trailing newline on one file to
#     re-trigger the CD pipeline — no free-form edits.
#   * ``deploy_tag_targets`` computes the main file's ``new_content`` from
#     the tag regex (``lv_component_build`` for ECS,
#     ``oss_component_svn_revision`` for ASG) and takes ``extra_updates`` for
#     companion files. The main file's other fields can't be edited via
#     this tool because the tag regex owns ``new_content``.
#
# When the user asks for a change like "bump the tag AND change instance_type,
# min_size, max_size, scaling thresholds AND update docker.options", none of
# the existing tools accept it in a single atomic commit. The AI's only
# fallback used to be two separate commits back-to-back on the same branch —
# which races on the concurrency check and forces last_commit_id to be dropped.
# NEVER split a multi-file change across multiple commits. Use this pair.

def _resolve_multi_file_update(
    repo_component: str,
    env: str,
    branch: str,
    file_path: str,
    new_content: str,
    action: str = "update",
) -> dict[str, Any]:
    """Fetch the current content of one file and build its per-file diff payload.

    ``action`` picks the write mode:

      * ``"update"`` (default) — the file MUST exist. Fetches its current
        content, computes the diff against ``new_content``, and returns
        ``last_commit_id`` so the caller can fence off concurrent writers.
      * ``"create"`` — the file MUST NOT already exist. The diff renders
        as "+ every line" against an empty file. No ``last_commit_id``
        (nothing to race). Raises ``ValueError`` if the file already
        exists so a create action is never silently upgraded to an
        overwrite.
    """
    env_prefix = f"{env}/"
    if not file_path.startswith(env_prefix):
        raise ValueError(
            f"file_path '{file_path}' must live under '{env_prefix}'."
        )
    if action == "create":
        existing = _fetch_file_at_path(repo_component, file_path, branch)
        if existing is not None:
            raise ValueError(
                f"Cannot create '{file_path}' on branch '{branch}': file "
                "already exists. Use action='update' to modify it, or pick "
                "a different path."
            )
        return {
            "file_path": file_path,
            "new_content": new_content,
            "action": "create",
            "diff": _make_unified_diff("", new_content, file_path),
            "has_changes": True,
            "last_commit_id": None,
        }
    file_json = _fetch_file_at_path(repo_component, file_path, branch)
    if not file_json:
        raise FileNotFoundError(
            f"File not found: '{file_path}' on branch '{branch}'."
        )
    original_content = _decode(file_json)
    return {
        "file_path": file_path,
        "new_content": new_content,
        "action": "update",
        "diff": _make_unified_diff(original_content, new_content, file_path),
        "has_changes": original_content != new_content,
        "last_commit_id": file_json.get("last_commit_id") or file_json.get("commit_id"),
    }


# ── Multi-file atomic commit: tools ─────────────────────────────────────────

@mcp.tool()
def find_multi_file_commit_targets(
    updates: list[dict[str, Any]],
    workers: int = _WORKERS_DEFAULT,
) -> dict[str, Any]:
    """Preview an atomic multi-file commit per (component, env, branch). **Read-only.**

    Use this whenever a single deploy touches **more than one file** and the
    changes don't fit ``find_tag_deploy_targets`` (which only computes the
    main file's ``new_content`` from the tag regex and treats everything else
    as ``extra_updates``). Typical cases: the user wants to bump the tag AND
    edit other ASG fields (``instance_type``, ``min_size``, ``max_size``,
    scaling thresholds, …) AND update a companion file like
    ``docker.options`` — all in ONE commit.

    **Every file in one ``updates`` entry lands in a single atomic commit.**
    Do NOT split a multi-file change across multiple invocations of the write
    tool; that races the concurrency check and forces ``last_commit_id`` to be
    dropped. If the user's change touches multiple files, they belong together
    in one entry here.

    Each ``updates`` entry:
        {
          "component": "<repo or aws-side name>",
          "env": "<au1|eu1|stg4|tst2|tst3|uk2|uk3>",
          "branch": "<version branch, e.g. '4.0'>",
          "files": [
            {
              "file_path": "<env>/<file>",
              "new_content": "<full new contents>",
              "action": "update"|"create",   # optional, default "update"
            },
            ...
          ],
          "commit_message": "<optional; see below>",
          "lv_cd_deploy": true|false,   # optional, default true
          "type": "ecs"|"asg"|""       # optional; used to normalize the
                                         # component name against the ECS/ASG
                                         # name maps
        }

    Per-file ``action``:
      * ``"update"`` (default) — the file MUST already exist. Missing files
        fall into ``unresolved_files`` with fuzzy-match candidates plus a
        create-vs-update ``hint``. Old callers keep working unchanged.
      * ``"create"`` — the file MUST NOT already exist. Explicit opt-in;
        there is no auto "missing → create" fallback (that would turn AI
        typos into ghost files). If the file already exists the entry is
        malformed. The rendered diff shows every line of ``new_content``
        prefixed with ``+`` against an empty file; ``last_commit_id`` is
        not applicable. Updates and creates can be mixed in the same
        ``files`` list and all land in ONE atomic commit; each target
        reports ``create_count`` / ``update_count`` for auditability.
        If any create file targets an env folder that does NOT currently
        exist (e.g. onboarding a brand-new ``eu1/`` in a repo that never
        had one), the target carries ``folder_creation_summary`` — an
        explicit human-readable string the AI host MUST show the user
        verbatim before approval so a folder never appears silently.

    Commit message defaults:
      * If ``commit_message`` is set, it's used verbatim.
      * Otherwise the tool builds ``"Update <n> files in <env> - LV_CD_DEPLOY"``
        (or without the ``- LV_CD_DEPLOY`` marker when ``lv_cd_deploy=False``).

    For each entry the tool fetches every file's current content, computes a
    per-file unified diff, and returns the aggregated target. Missing files
    are surfaced with fuzzy-matched candidates under ``unresolved_files`` so
    the AI has to come back to the user before proceeding.

    Response shape:
      * ``targets``    — ready-to-commit entries with per-file ``files: [{
                          file_path, new_content, action, diff, has_changes,
                          last_commit_id}, ...]``, a top-level ``diff``
                          concatenating every per-file diff (convenient for
                          display), ``create_count`` / ``update_count``,
                          and — when any create file lands in a folder
                          that didn't exist before — ``folder_creation_summary``
                          (a human-readable string the AI host MUST show
                          to the user verbatim before approval).
      * ``needs_info`` — entries with missing core fields OR with
                          ``unresolved_files`` (missing files + candidates
                          + a create-vs-update ``hint``).
      * ``errors``     — hard failures (disallowed env, malformed input, …).

    The AI host **must** present every per-file diff (and the commit message)
    to the user, get explicit approval, then pass the target objects
    unchanged to ``commit_multi_file_targets`` with
    ``confirmed_by_user=True``.

    Bulk previews fan out across ``workers`` threads (1..30, default 10).
    Per-entry failures are isolated. ``targets`` / ``needs_info`` / ``errors``
    are kept in input order.
    """
    if not updates:
        return {"ok": False, "error": "updates must be a non-empty list."}

    workers_capped = _cap_workers(workers)

    def _process(entry: dict[str, Any]) -> dict[str, Any]:
        component = (entry.get("component") or "").strip()
        env = (entry.get("env") or "").strip()
        branch = (entry.get("branch") or "").strip()
        deploy_type = (entry.get("type") or "").strip().lower()
        commit_message_in = (entry.get("commit_message") or "").strip()
        lv_cd_deploy = bool(entry.get("lv_cd_deploy", True))
        files_in = entry.get("files") or []

        provided_summary = {
            "component": component or None,
            "env": env or None,
            "branch": branch or None,
            "type": deploy_type or None,
            "file_count": len(files_in),
            "commit_message_provided": bool(commit_message_in),
            "lv_cd_deploy": lv_cd_deploy,
        }

        # 1. Core fields.
        core_missing = [
            name
            for name, val in (
                ("component", component),
                ("env", env),
                ("branch", branch),
            )
            if not val
        ]
        if core_missing:
            return {
                "_kind": "needs_info",
                "payload": {
                    "provided": provided_summary,
                    "missing": core_missing,
                    "reason": (
                        "Missing core fields. Ask the user, then re-invoke "
                        "find_multi_file_commit_targets with the complete entry."
                    ),
                },
            }
        if not files_in:
            return {
                "_kind": "error",
                "payload": {
                    "provided": provided_summary,
                    "error": "`files` must be a non-empty list of {file_path, new_content}.",
                },
            }

        # 2. Env whitelist.
        denied = _reject_non_redeployable_env(env)
        if denied:
            return {
                "_kind": "error",
                "payload": {
                    "provided": provided_summary,
                    "error": denied["error"],
                    "allowed_envs": denied["allowed_envs"],
                },
            }

        # 3. Type validation.
        if deploy_type and deploy_type not in ("ecs", "asg"):
            return {
                "_kind": "error",
                "payload": {
                    "provided": provided_summary,
                    "error": (
                        f"Invalid type '{deploy_type}' — must be 'ecs', 'asg', "
                        "or '' to skip name normalization."
                    ),
                },
            }

        # 4. Resolve repo-side component name. If ``type`` is unset, fall back
        #    to the raw name (no map lookup) — callers who want normalization
        #    should pass the type.
        repo_component = (
            _normalize_component_for_repo(component, deploy_type)
            if deploy_type in ("ecs", "asg")
            else component
        )

        # 5. Fetch every file, build per-file diff. Missing files → unresolved.
        resolved_files: list[dict[str, Any]] = []
        unresolved_files: list[dict[str, Any]] = []
        malformed_files: list[dict[str, Any]] = []
        env_files_cache: Optional[list[dict[str, Any]]] = None

        for idx, f in enumerate(files_in):
            if not isinstance(f, dict):
                malformed_files.append(
                    {"index": idx, "error": "file entry must be a dict."}
                )
                continue
            file_path = (f.get("file_path") or "").strip()
            new_content = f.get("new_content")
            file_action = (f.get("action") or "update").strip().lower()
            if not file_path or new_content is None:
                malformed_files.append(
                    {
                        "index": idx,
                        "file_path": file_path or None,
                        "error": "file entry requires both `file_path` and `new_content`.",
                    }
                )
                continue
            if file_action not in ("update", "create"):
                malformed_files.append(
                    {
                        "index": idx,
                        "file_path": file_path,
                        "error": (
                            f"file entry `action` must be 'update' or "
                            f"'create' (got '{file_action}')."
                        ),
                    }
                )
                continue
            try:
                resolved_files.append(
                    _resolve_multi_file_update(
                        repo_component, env, branch, file_path, new_content,
                        file_action,
                    )
                )
            except FileNotFoundError as exc:
                # `action="update"` on a missing file — surface fuzzy-match
                # candidates AND a create-vs-update hint so the AI can
                # confirm intent with the user (typo vs a genuine create
                # that should have used `action="create"`).
                if env_files_cache is None:
                    env_files_cache = (
                        _list_env_tree_files(repo_component, env, branch, True) or []
                    )
                unresolved_files.append(
                    {
                        "requested": {
                            "file_path": file_path,
                            "action": file_action,
                            "new_content_provided": True,
                        },
                        "error": str(exc),
                        "hint": (
                            "If you meant to CREATE this file, re-invoke "
                            "with `action: 'create'` on this entry. If you "
                            "meant to UPDATE an existing file, pick from "
                            "the `candidates` below."
                        ),
                        "candidates": _fuzzy_match_env_files(file_path, env_files_cache),
                    }
                )
            except ValueError as exc:
                malformed_files.append(
                    {"index": idx, "file_path": file_path, "error": str(exc)}
                )

        if malformed_files:
            return {
                "_kind": "error",
                "payload": {
                    "provided": provided_summary,
                    "error": "One or more file entries are malformed.",
                    "malformed_files": malformed_files,
                },
            }

        if unresolved_files:
            return {
                "_kind": "needs_info",
                "payload": {
                    "provided": provided_summary,
                    "missing": ["files_resolution"],
                    "unresolved_files": unresolved_files,
                    "resolved_files": [
                        {"file_path": r["file_path"], "has_changes": r["has_changes"]}
                        for r in resolved_files
                    ],
                    "reason": (
                        "One or more requested files could not be found in "
                        "the repo. For each entry under `unresolved_files`, "
                        "show the user the `candidates` list (closest matching "
                        "files in the env) and ask them to pick — or call "
                        "`list_env_files` to browse. Then re-invoke "
                        "`find_multi_file_commit_targets` with the corrected "
                        "`files`. Do NOT guess a path."
                    ),
                },
            }

        # 6. Split creates vs updates and detect brand-new env folders.
        create_files = [r for r in resolved_files if r.get("action") == "create"]
        update_files = [r for r in resolved_files if r.get("action") != "create"]
        folder_creation_summary: Optional[str] = None
        if create_files:
            # Re-probe the env tree so we can distinguish "empty <env>/" from
            # "no <env>/ at all". `_list_env_tree_files` returns None only
            # when the folder itself is absent — which is exactly the case
            # that warrants a folder-creation warning.
            env_tree = _list_env_tree_files(repo_component, env, branch, True)
            if env_tree is None or len(env_tree) == 0:
                create_paths = [c["file_path"] for c in create_files]
                folder_creation_summary = (
                    f"Env folder '{env}/' does NOT currently exist under "
                    f"{repo_component}-config on branch '{branch}'. This commit "
                    f"will CREATE the folder along with {len(create_paths)} "
                    f"new file(s): {', '.join(create_paths)}."
                )

        # 7. Compose commit message.
        if commit_message_in:
            commit_message = commit_message_in
            if lv_cd_deploy and LV_CD_DEPLOY_COMMIT_MESSAGE not in commit_message:
                commit_message = f"{commit_message} - {LV_CD_DEPLOY_COMMIT_MESSAGE}"
        else:
            if create_files and not update_files:
                base = f"Create {len(create_files)} files in {env}"
            elif create_files and update_files:
                base = (
                    f"Create {len(create_files)} + update "
                    f"{len(update_files)} files in {env}"
                )
            else:
                base = f"Update {len(resolved_files)} files in {env}"
            commit_message = (
                f"{base} - {LV_CD_DEPLOY_COMMIT_MESSAGE}" if lv_cd_deploy else base
            )

        # 8. Aggregate diff for convenient display.
        aggregate_diff = "\n".join(f["diff"] for f in resolved_files if f["diff"])

        _, group = _endpoint()
        target = {
            "component": component,
            "repo_component": repo_component,
            "env": env,
            "branch": branch,
            "type": deploy_type or None,
            "repo_path": f"{group}/{repo_component}-config",
            "commit_message": commit_message,
            "lv_cd_deploy": lv_cd_deploy,
            "file_count": len(resolved_files),
            "create_count": len(create_files),
            "update_count": len(update_files),
            "folder_creation_summary": folder_creation_summary,
            "files": resolved_files,
            "diff": aggregate_diff,
            "has_changes": any(f["has_changes"] for f in resolved_files),
        }
        return {"_kind": "target", "payload": target}

    outcomes = _threaded_map(_process, list(updates), workers_capped)

    targets: list[dict[str, Any]] = []
    needs_info: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for idx, out, exc in outcomes:
        if exc is not None:
            errors.append(
                {
                    "provided": {"index": idx},
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            continue
        kind = out.get("_kind")
        payload = out.get("payload")
        if kind == "target":
            targets.append(payload)
        elif kind == "needs_info":
            needs_info.append(payload)
        else:
            errors.append(payload)

    return {
        "ok": True,
        "workers": workers_capped,
        "requested_count": len(updates),
        "match_count": len(targets),
        "needs_info_count": len(needs_info),
        "error_count": len(errors),
        "targets": targets,
        "needs_info": needs_info,
        "errors": errors,
        "instruction": (
            "First, handle `needs_info` and `errors` before proceeding. "
            "For `missing: ['files_resolution']`, show each "
            "`unresolved_files[*].requested.file_path` alongside its "
            "`candidates` list AND the entry's `hint` (which tells the "
            "user whether they may have meant `action: 'create'` instead "
            "of the default `update`). Ask the user to pick the correct "
            "path (or add the `create` action) and re-invoke this tool "
            "with corrected `files`. Do NOT invent a path or silently "
            "flip an update into a create. "
            "Second, for each ready target you MUST present ALL of the "
            "following to the user before asking for approval — no "
            "summarizing, no truncation, no hidden files: "
            "(1) `folder_creation_summary` VERBATIM if present (this "
            "warns that a brand-new env folder is about to be added — "
            "the user MUST see this before approving), "
            "(2) `create_count` and `update_count` so the user knows how "
            "many files are new vs modified, "
            "(3) EVERY entry in `files[]` — for each: its `file_path`, "
            "its `action`, and its FULL `diff` (create diffs render as "
            "`+ every line` against an empty file — show all lines, not "
            "just the first N), "
            "(4) the `commit_message` verbatim, "
            "(5) the resolved `component`, `env`, `branch`, `type`, and "
            "`repo_path`. "
            "Only after explicit user approval may you call "
            "`commit_multi_file_targets` with the exact target objects "
            "unchanged and `confirmed_by_user=True`. Every file in a "
            "target lands in ONE atomic commit — do NOT split by calling "
            "the write tool multiple times. That races the concurrency "
            "check and forces `last_commit_id` to be dropped."
        ),
    }


@mcp.tool()
def commit_multi_file_targets(
    targets: list[dict[str, Any]],
    confirmed_by_user: bool = False,
    trigger_pipeline_for_asg_components: bool = True,
    workers: int = _WORKERS_DEFAULT,
) -> dict[str, Any]:
    """Commit the confirmed multi-file changes atomically.

    **Two-step contract (enforced):**
      1. You must first call ``find_multi_file_commit_targets`` to get each
         file's ``diff`` and ``last_commit_id`` per target. Show every
         per-file diff (and, when present, ``folder_creation_summary``) to
         the user.
      2. Only after explicit approval may you call this tool with
         ``confirmed_by_user=True``. Each target must retain its
         ``files: [...]`` list from the preview (each with its own
         ``diff``, ``action``, and — for updates — ``last_commit_id``).
         Any file missing ``diff`` is rejected.

    Each target must contain ``env``, ``branch``, ``commit_message``,
    ``files: [{file_path, new_content, action, diff, last_commit_id?}, ...]``,
    and either ``component`` or ``repo_component``. These are produced by
    ``find_multi_file_commit_targets`` — pass them back unchanged.

    Per-file ``action``:
      * ``"update"`` (default) — committed as a GitLab ``update`` action;
        ``last_commit_id`` is honored to prevent racing another writer.
      * ``"create"`` — committed as a GitLab ``create`` action. GitLab
        rejects the commit if the file already exists (that's the fence
        on this path), so ``last_commit_id`` is not sent for creates.
    Updates and creates land in the SAME atomic commit — do not split
    them across two calls. Per-target results include ``create_count`` /
    ``update_count`` so post-hoc audits show what shipped.

    All files in a target land in **one atomic GitLab commit**. Multi-
    target batches run in parallel across ``workers`` threads (1..30,
    default 10). Per-target failures are isolated; per-target ``results``
    are returned in input order.

    ASG pipeline trigger — same behavior as ``deploy_tag_targets`` /
    ``redeploy_targets``. After a successful commit an additional API
    pipeline is triggered for ASG components where the config commit alone
    does NOT fire the CD pipeline:
      * ``contact`` / ``recording`` — hardcoded in
        ``ASG_PIPELINE_TRIGGER_COMPONENTS``. These components always deploy
        via API, so multi-file commits (e.g. tag bump + ``instance_type``
        + companion file) that touch these repos must also fire the API
        trigger — otherwise the commit lands but no pipeline runs.
      * Any target carrying ``both_configs_present=True`` (components that
        ship BOTH ECS and ASG configs) — the CD auto-trigger on those
        repos picks up the ECS side, so the ASG side needs an explicit
        pipeline POST.
    The trigger fires only when ``type == "asg"`` and
    ``trigger_pipeline_for_asg_components`` is True (default). Set the
    flag to False to opt out for a batch (rare).

    Returns per-target commit info (``commit_id``, ``commit_url``,
    ``extra_pipeline`` when the ASG trigger fires) plus success/failure
    counts.
    """
    if not targets:
        return {"ok": False, "error": "targets must be a non-empty list."}

    if not confirmed_by_user:
        return {
            "ok": False,
            "needs_confirmation": True,
            "requested_count": len(targets),
            "instruction": (
                "Diff review is mandatory. Present EVERY per-file "
                "`files[i].diff` from `find_multi_file_commit_targets` to the "
                "user, get explicit approval, and only then call this tool "
                "again with `confirmed_by_user=True`. Do NOT set the flag on "
                "the user's behalf."
            ),
        }

    workers_capped = _cap_workers(workers)

    def _process(t: dict[str, Any]) -> dict[str, Any]:
        component = (t.get("component") or "").strip()
        env = (t.get("env") or "").strip()
        branch = (t.get("branch") or "").strip()
        commit_message = (t.get("commit_message") or "").strip()
        deploy_type = (t.get("type") or "").strip().lower()
        repo_component = (t.get("repo_component") or "").strip()
        files_in = t.get("files") or []

        entry: dict[str, Any] = {
            "component": component,
            "repo_component": repo_component or None,
            "env": env,
            "branch": branch,
            "type": deploy_type or None,
            "commit_message": commit_message,
        }

        if not (component and env and branch and commit_message):
            entry["ok"] = False
            entry["error"] = (
                "Target missing required fields "
                "(component, env, branch, commit_message)."
            )
            return entry
        if not files_in:
            entry["ok"] = False
            entry["error"] = "Target has empty `files` — nothing to commit."
            return entry

        # Every file must carry a `diff` field from the preview.
        for f_idx, f in enumerate(files_in):
            if not isinstance(f, dict):
                entry["ok"] = False
                entry["error"] = f"files[{f_idx}] is not a dict."
                return entry
            if not (f.get("diff") or "").strip():
                entry["ok"] = False
                entry["error"] = (
                    f"files[{f_idx}] has no `diff` field — refusing to commit. "
                    "Regenerate via `find_multi_file_commit_targets` so every "
                    "file's diff can be shown to the user."
                )
                return entry

        denied = _reject_non_redeployable_env(env)
        if denied:
            entry["ok"] = False
            entry["error"] = denied["error"]
            return entry

        if not repo_component:
            repo_component = (
                _normalize_component_for_repo(component, deploy_type)
                if deploy_type in ("ecs", "asg")
                else component
            )
            entry["repo_component"] = repo_component

        # Build the atomic action list — every file → one action, ALL in
        # one commit. Never split.
        actions: list[dict[str, Any]] = []
        included_files: list[dict[str, Any]] = []
        create_count = 0
        update_count = 0
        for f_idx, f in enumerate(files_in):
            file_path = (f.get("file_path") or "").strip()
            new_content = f.get("new_content")
            file_action = (f.get("action") or "update").strip().lower()
            if not file_path or new_content is None:
                entry["ok"] = False
                entry["error"] = (
                    f"files[{f_idx}] missing file_path or new_content."
                )
                return entry
            if file_action not in ("update", "create"):
                entry["ok"] = False
                entry["error"] = (
                    f"files[{f_idx}].action must be 'update' or 'create' "
                    f"(got '{file_action}')."
                )
                return entry
            if not file_path.startswith(f"{env}/"):
                entry["ok"] = False
                entry["error"] = (
                    f"files[{f_idx}].file_path '{file_path}' must be under '{env}/'."
                )
                return entry
            action: dict[str, Any] = {
                "action": file_action,
                "file_path": file_path,
                "content": new_content,
            }
            # `last_commit_id` is only meaningful for updates. Creates
            # have no prior commit to fence against; GitLab rejects the
            # commit if the file already exists, which is the fence we
            # want on the create path.
            if file_action == "update" and f.get("last_commit_id"):
                action["last_commit_id"] = f["last_commit_id"]
            actions.append(action)
            if file_action == "create":
                create_count += 1
            else:
                update_count += 1
            included_files.append(
                {
                    "file_path": file_path,
                    "action": file_action,
                    "has_changes": f.get("has_changes"),
                }
            )

        entry["files"] = included_files
        entry["action_count"] = len(actions)
        entry["create_count"] = create_count
        entry["update_count"] = update_count

        try:
            res = _post_commit_multi(repo_component, branch, commit_message, actions)
        except RuntimeError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            return entry

        if res.status_code in (200, 201):
            data = res.json()
            entry["ok"] = True
            entry["commit_id"] = data.get("id")
            entry["commit_url"] = data.get("web_url")

            # ASG-side deploys need an explicit CD pipeline trigger for
            # two categories of components — same treatment as
            # ``deploy_tag_targets`` / ``redeploy_targets``:
            #   * ``contact`` / ``recording`` — hardcoded in
            #     ``ASG_PIPELINE_TRIGGER_COMPONENTS``. Config commit alone
            #     does not fire CD for these; they always deploy via API.
            #   * Components shipping BOTH ECS and ASG configs — CD
            #     auto-trigger picks up the ECS side only. Detected via
            #     ``both_configs_present`` on the target (callers can stamp
            #     it when building multi-file targets for such components).
            both_configs_present = bool(t.get("both_configs_present"))
            if (
                deploy_type == "asg"
                and trigger_pipeline_for_asg_components
                and (
                    component in ASG_PIPELINE_TRIGGER_COMPONENTS
                    or both_configs_present
                )
            ):
                time.sleep(5)
                entry["extra_pipeline"] = _trigger_asg_pipeline(
                    repo_component, branch, env
                )
        else:
            entry["ok"] = False
            entry["status_code"] = res.status_code
            entry["error"] = (res.text or "")[:500]
        return entry

    outcomes = _threaded_map(_process, list(targets), workers_capped)

    results: list[dict[str, Any]] = []
    for idx, entry, exc in outcomes:
        if exc is not None:
            t = targets[idx]
            results.append(
                {
                    "component": (t.get("component") or "").strip(),
                    "env": (t.get("env") or "").strip(),
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
        else:
            results.append(entry)

    succeeded = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    return {
        "ok": len(failed) == 0,
        "workers": workers_capped,
        "requested_count": len(targets),
        "success_count": len(succeeded),
        "failure_count": len(failed),
        "results": results,
    }


# ── CDN deploy: helpers ─────────────────────────────────────────────────────
# The CDN path mirrors the eng-config ``find_tag_deploy_targets`` /
# ``deploy_tag_targets`` flow so the AI host uses the same two-step diff
# review contract, the same tag validation, and the same ``extra_updates``
# companion-file mechanic. Differences vs. the eng-config side:
#
#   * Project: ``devops/cdn`` (not ``eng/<component>-config``).
#   * Repo branch we commit to: ``gitlab-ci`` (fixed). The user-supplied
#     ``branch`` (e.g. ``27.0``) is the component's *version* branch and
#     is only used to pick the right filename.
#   * File layout: one JSON file per (component, branch) under
#     ``<env>/<component>-<branch>.json``.
#   * Field updated: ``lv_component_version``. Rewritten via a string-level
#     regex so unrelated formatting is preserved.
#   * No ASG pipeline trigger — CDN deploys ship on HEAD of ``gitlab-ci``.
#   * Commit message: ``Deploy CDN <component> <tag> (<branch>) in <env>
#     - <ticket> - LV_CD_DEPLOY`` by default. The ``LV_CD_DEPLOY`` marker is
#     included on every commit unless the caller explicitly sets
#     ``lv_cd_deploy: false`` on the deployment entry (rare — only when the
#     user says so).
#   * Env policy: DENYLIST — every env folder is deployable by default;
#     only ``CDN_NON_DEPLOYABLE_ENVS`` (prod region envs) and
#     ``CDN_ENV_BLACKLIST`` (bin/global/prod) are blocked. New env folders
#     auto-inherit "allowed" — no image rebuild needed. The ``prod/``
#     folder has a per-file exception: filenames containing ``staging`` or
#     ``test`` are updatable.

def _cdn_encoded_project() -> str:
    return quote(f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}", safe="")


def _reject_non_cdn_deployable_env(
    env: str, file_name: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Return an error payload if ``env`` (or the specific file) is not
    allowed for CDN deploys.

    Denylist semantics: any env is allowed unless it's in
    ``CDN_NON_DEPLOYABLE_ENVS`` (prod region envs — always blocked) or
    ``CDN_ENV_BLACKLIST`` (bin/global/prod). The ``prod`` folder gets a
    per-file exception: filenames matching ``_CDN_PROD_FILENAME_ALLOW_RE``
    (staging/test) are still allowed — pass ``file_name`` to enable that
    check.
    """
    if _cdn_env_allows_file(env, file_name or ""):
        return None

    if env in CDN_NON_DEPLOYABLE_ENVS:
        reason = (
            f"Env '{env}' is a prod region env in CDN_NON_DEPLOYABLE_ENVS; "
            "CDN deploys are never allowed there."
        )
        hint = ""
    elif env == "prod":
        reason = (
            f"File '{file_name}' under 'prod/' is not allowed. Only "
            "filenames containing 'staging' or 'test' (e.g. "
            "lvp-ui-27.0test.json) are updatable in prod/."
        )
        hint = ""
    elif env in CDN_ENV_BLACKLIST:
        reason = (
            f"'{env}' is a non-env root folder (bin/global) — not deployable."
        )
        hint = ""
    else:
        # Shouldn't reach here (allowed by default), but defensive.
        reason = f"Env '{env}' is not allowed for CDN deploys."
        hint = ""

    return {
        "ok": False,
        "env": env,
        "file_name": file_name,
        "cdn_deploy_disallowed": True,
        "blocked_envs": sorted(CDN_NON_DEPLOYABLE_ENVS | CDN_ENV_BLACKLIST),
        "error": reason,
        "instruction": (
            "CDN deploys use a DENYLIST: any env folder in the CDN repo is "
            "allowed unless it's in the blocked set above. Report this "
            "restriction to the user; do not retry against another blocked env."
            + (f" {hint}" if hint else "")
        ),
    }


def _fetch_cdn_file_at_path(
    file_path: str, branch: str = CDN_DEFAULT_BRANCH
) -> Optional[dict[str, Any]]:
    """Fetch a file by its full repo path from ``devops/cdn``."""
    encoded_file = quote(file_path, safe="")
    url = _api(
        f"/projects/{_cdn_encoded_project()}/repository/files/{encoded_file}"
    )
    res = _get(url, params={"ref": branch})
    if res.status_code != 200:
        return None
    return res.json()


def _list_cdn_env_tree_files(
    env: str,
    branch: str = CDN_DEFAULT_BRANCH,
    recursive: bool = True,
) -> Optional[list[dict[str, Any]]]:
    """Return ``[{path, name, type}]`` blobs under ``<env>/`` in the CDN repo,
    or ``None`` if the env folder itself couldn't be listed."""
    items: list[dict[str, Any]] = []
    page = 1
    got_any_response = False
    while True:
        params: dict[str, Any] = {
            "ref": branch,
            "path": env,
            "per_page": 100,
            "page": page,
        }
        if recursive:
            params["recursive"] = "true"
        url = _api(f"/projects/{_cdn_encoded_project()}/repository/tree")
        try:
            res = _get(url, params=params)
        except RuntimeError:
            return None
        if res.status_code != 200:
            if page == 1:
                return None
            break
        got_any_response = True
        data = res.json()
        if not data:
            break
        items.extend(data)
        if "next" not in res.links:
            break
        page += 1
    if not got_any_response:
        return None
    return [
        {"path": it["path"], "name": it.get("name"), "type": it.get("type")}
        for it in items
        if it.get("type") == "blob"
    ]


def _cdn_component_file_path(component: str, version_label: str, env: str) -> str:
    return f"{env}/{component}-{version_label}.json"


def _update_cdn_content(content: str, tag: str) -> str:
    """Rewrite ``lv_component_version`` to ``tag`` via string-level regex.

    Preserves the rest of the file byte-for-byte. If the key isn't present
    the content is returned unchanged (the caller reports ``has_changes:
    false`` in that case). Whatever the caller passes as ``tag`` lands in
    the file verbatim — the caller is responsible for any suffix like
    ``/mfe``. See ``_detect_cdn_value_suffix_drop`` for the soft warning
    that fires when a suffix would be dropped by accident.
    """

    def _replace(match: "re.Match[str]") -> str:
        return f"{match.group(1)}{tag}{match.group(3)}"

    return _CDN_VERSION_RE.sub(_replace, content)


def _detect_cdn_value_suffix_drop(content: str, tag: str) -> Optional[str]:
    """Return a soft warning if the *current* ``lv_component_version`` value
    carries a suffix (separated by ``/`` or ``-``) that the incoming ``tag``
    does not — e.g. current ``27.0.47/mfe`` -> new ``27.0.53`` drops ``/mfe``.

    This is intentionally NOT a hard failure: dropping a suffix might be
    exactly what the user wants (e.g. splitting off the `/mfe` variant).
    The warning lands in the target's ``warnings`` list so the AI shows it
    alongside the diff and the user can confirm or resubmit with the
    suffix baked into ``tag``.
    """
    m = _CDN_VERSION_RE.search(content)
    if not m:
        return None
    current = m.group(2)
    for sep in ("/", "-"):
        if sep in current and sep not in tag:
            suffix = current.split(sep, 1)[1]
            return (
                f"Current lv_component_version is '{current}' — the '{sep}{suffix}' "
                f"suffix will be DROPPED by the new tag '{tag}'. If you meant "
                f"to keep it, resubmit with tag='{tag}{sep}{suffix}'. If you "
                f"meant to drop it, confirm and proceed."
            )
    return None


def _build_cdn_commit_message(
    component: str,
    tag: str,
    version_label: str,
    env: str,
    ticket: str,
    lv_cd_deploy: bool = True,
) -> str:
    """Return the CDN commit message.

    Default format (``lv_cd_deploy=True``):
        ``Deploy CDN designer 27.0.51 (27.0) in stg4 - DEVOPS-52986 - LV_CD_DEPLOY``

    When ``lv_cd_deploy=False`` the trailing marker is omitted:
        ``Deploy CDN designer 27.0.51 (27.0) in stg4 - DEVOPS-52986``

    ``ticket`` is optional — when empty that segment is dropped entirely:
        ``Deploy CDN designer 27.0.51 (27.0) in stg4 - LV_CD_DEPLOY``

    ``version_label`` is the version-series label baked into the filename
    (e.g. ``27.0`` or ``27.0test``), NOT a git branch. Every CDN commit lands
    on ``gitlab-ci`` regardless of this value.

    Any deployment is treated as an LV_CD_DEPLOY unless the caller
    explicitly opts out.
    """
    base = f"Deploy CDN {component} {tag} ({version_label}) in {env}"
    if ticket:
        base = f"{base} - {ticket}"
    return f"{base} - {LV_CD_DEPLOY_COMMIT_MESSAGE}" if lv_cd_deploy else base


def _resolve_cdn_extra_update(
    env: str, extra: dict[str, Any]
) -> dict[str, Any]:
    """CDN equivalent of ``_resolve_extra_update``. Companion files must live
    under the same ``<env>/`` folder as the main JSON."""
    file_path = (extra.get("file_path") or "").strip()
    new_content = extra.get("new_content")
    if not file_path or new_content is None:
        raise ValueError(
            "extra_updates entries require both `file_path` and `new_content`."
        )
    env_prefix = f"{env}/"
    if not file_path.startswith(env_prefix):
        raise ValueError(
            f"extra_updates.file_path '{file_path}' must live under '{env_prefix}'."
        )
    # Prod exception applies per-file: block real prod files even when the
    # main file was allowed through the staging/test carve-out.
    if not _cdn_env_allows_file(env, file_path):
        raise ValueError(
            f"extra_updates.file_path '{file_path}' is not allowed in env "
            f"'{env}' (prod/ files must contain 'staging' or 'test' in the "
            "filename)."
        )

    file_json = _fetch_cdn_file_at_path(file_path)
    if not file_json:
        raise FileNotFoundError(
            f"Extra CDN file not found: '{file_path}' on branch "
            f"'{CDN_DEFAULT_BRANCH}'."
        )
    original_content = _decode(file_json)
    return {
        "file_path": file_path,
        "new_content": new_content,
        "diff": _make_unified_diff(original_content, new_content, file_path),
        "has_changes": original_content != new_content,
        "last_commit_id": file_json.get("last_commit_id") or file_json.get("commit_id"),
    }


def _resolve_cdn_tag_deploy_target(
    component: str,
    env: str,
    version_label: str,
    tag: str,
    ticket: str,
    extra_updates: Optional[list[dict[str, Any]]] = None,
    lv_cd_deploy: bool = True,
) -> dict[str, Any]:
    """Fetch the CDN JSON file for a component and build the target payload.

    ``version_label`` is the version-series label baked into the filename
    (e.g. ``27.0`` or ``27.0test``). It is NOT a git branch — every CDN
    commit lands on ``gitlab-ci`` (surfaced as ``repo_branch``).
    """
    file_path = _cdn_component_file_path(component, version_label, env)
    file_json = _fetch_cdn_file_at_path(file_path)
    if file_json is None:
        raise FileNotFoundError(
            f"No CDN file for '{component}' version_label '{version_label}' in "
            f"env '{env}': expected '{file_path}' on {CDN_DEFAULT_BRANCH}."
        )
    original_content = _decode(file_json)
    new_content = _update_cdn_content(original_content, tag)
    suffix_drop_warning = _detect_cdn_value_suffix_drop(original_content, tag)

    resolved_extras: list[dict[str, Any]] = []
    unresolved_extras: list[dict[str, Any]] = []
    env_files_cache: Optional[list[dict[str, Any]]] = None
    for extra in (extra_updates or []):
        try:
            resolved_extras.append(_resolve_cdn_extra_update(env, extra))
        except FileNotFoundError as exc:
            if env_files_cache is None:
                env_files_cache = _list_cdn_env_tree_files(env, recursive=True) or []
            requested_path = (extra.get("file_path") or "").strip()
            unresolved_extras.append(
                {
                    "requested": {
                        "file_path": requested_path,
                        "new_content_provided": extra.get("new_content") is not None,
                    },
                    "error": str(exc),
                    "candidates": _fuzzy_match_env_files(requested_path, env_files_cache),
                }
            )

    return {
        "component": component,
        "env": env,
        "version_label": version_label,
        "tag": tag,
        "ticket": ticket,
        "source": "cdn",
        "repo_path": f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}",
        "repo_branch": CDN_DEFAULT_BRANCH,
        "file_path": file_path,
        "new_content": new_content,
        "diff": _make_unified_diff(original_content, new_content, file_path),
        "commit_message": _build_cdn_commit_message(
            component, tag, version_label, env, ticket, lv_cd_deploy=lv_cd_deploy
        ),
        "lv_cd_deploy": lv_cd_deploy,
        "has_changes": original_content != new_content,
        "last_commit_id": file_json.get("last_commit_id") or file_json.get("commit_id"),
        "extra_updates": resolved_extras,
        "unresolved_extras": unresolved_extras,
        "warnings": [suffix_drop_warning] if suffix_drop_warning else [],
    }


def _post_cdn_commit_multi(
    commit_message: str, actions: list[dict[str, Any]]
) -> requests.Response:
    """Commit multiple file updates atomically to ``devops/cdn`` on
    ``gitlab-ci``."""
    payload = {
        "branch": CDN_DEFAULT_BRANCH,
        "commit_message": commit_message,
        "actions": actions,
    }
    try:
        return requests.post(
            _api(f"/projects/{_cdn_encoded_project()}/repository/commits"),
            headers=_headers(),
            json=payload,
            timeout=30,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        base, _ = _endpoint()
        raise RuntimeError(
            f"GitLab endpoint {base} is not reachable ({exc.__class__.__name__}). "
            "Double-check your VPN connection and try again."
        ) from exc


# ── CDN deploy: tools ───────────────────────────────────────────────────────

@mcp.tool()
def list_cdn_env_components(
    env: str, version_label_filter: str = "", branch_filter: str = ""
) -> dict[str, Any]:
    """List CDN component JSON files under ``<env>/`` in devops/cdn. **Read-only.**

    Each file matches ``<component>-<version_label>.json`` and represents a
    deployable UI/component. ``version_label`` is the string baked into the
    filename (e.g. ``27.0``, ``27.0test``) — it is NOT a git branch. Every
    CDN commit lands on the fixed git branch ``gitlab-ci``
    (surfaced as ``repo_branch``).

    Args:
        env: Env folder name. Any env is allowed here (read-only) — the
            deploy tool applies the CDN denylist separately.
        version_label_filter: Optional filter (e.g. ``"27.0"``) to narrow
            the listing to a single component-version series.
        branch_filter: Legacy alias for ``version_label_filter``. Prefer
            ``version_label_filter`` in new code.

    Returns ``{ok, env, repo_branch (=gitlab-ci), match_count,
    components: [...]}`` where each entry is
    ``{component, version_label, file_path}``.
    """
    files = _list_cdn_env_tree_files(env, recursive=False)
    if files is None:
        return {
            "ok": False,
            "env": env,
            "repo_path": f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}",
            "repo_branch": CDN_DEFAULT_BRANCH,
            "error": (
                f"Could not list files under '{env}/' in "
                f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}@{CDN_DEFAULT_BRANCH}. "
                "Verify the env exists on the CDN repo."
            ),
        }

    label_filter = ((version_label_filter or branch_filter) or "").strip()
    components: list[dict[str, Any]] = []
    for f in files:
        name = f.get("name") or ""
        match = _CDN_FILENAME_RE.match(name)
        if not match:
            continue
        component, version_label = match.group(1), match.group(2)
        if label_filter and version_label != label_filter:
            continue
        components.append(
            {
                "component": component,
                "version_label": version_label,
                "file_path": f["path"],
            }
        )

    components.sort(key=lambda x: (x["component"], x["version_label"]))
    return {
        "ok": True,
        "env": env,
        "repo_path": f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}",
        "repo_branch": CDN_DEFAULT_BRANCH,
        "version_label_filter": label_filter or None,
        "match_count": len(components),
        "components": components,
    }


@mcp.tool()
def list_cdn_env_files(env: str, recursive: bool = True) -> dict[str, Any]:
    """List every file under ``<env>/`` in devops/cdn. **Read-only.**

    Use this before ``find_cdn_tag_deploy_targets`` when the user wants to
    modify a companion file (e.g. a ``.properties`` file next to the
    component JSON) alongside the ``lv_component_version`` change. Show
    the returned ``files`` list to the user, collect their new content per
    file, then pass those as ``extra_updates`` to
    ``find_cdn_tag_deploy_targets``.

    Args:
        env: Env folder name (any env; this is read-only).
        recursive: Include nested subfolders. Default ``True``.
    """
    files = _list_cdn_env_tree_files(env, recursive=recursive)
    if files is None:
        return {
            "ok": False,
            "env": env,
            "repo_path": f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}",
            "repo_branch": CDN_DEFAULT_BRANCH,
            "error": (
                f"Could not list files under '{env}/' in "
                f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}@{CDN_DEFAULT_BRANCH}."
            ),
        }
    return {
        "ok": True,
        "env": env,
        "repo_path": f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}",
        "repo_branch": CDN_DEFAULT_BRANCH,
        "recursive": recursive,
        "file_count": len(files),
        "files": files,
    }


@mcp.tool()
def get_cdn_component_config(
    component: str, env: str, version_label: str = "", branch: str = ""
) -> dict[str, Any]:
    """Fetch the raw CDN JSON file for a component/env/version_label. **Read-only.**

    Args:
        component: CDN component name (e.g. ``"designer"``, ``"lvp-ui"``).
        env: Env folder name.
        version_label: The label baked into the filename between the
            component and ``.json`` (e.g. ``"27.0"`` or ``"27.0test"``).
            This is NOT a git branch; every CDN commit lands on
            ``gitlab-ci`` (surfaced as ``repo_branch``).
        branch: Legacy alias for ``version_label``. Prefer ``version_label``.
    """
    label = (version_label or branch or "").strip()
    if not label:
        raise ValueError(
            "version_label is required (the string in the filename between "
            "the component and .json, e.g. '27.0' or '27.0test'). It is NOT "
            "a git branch; the git branch is always 'gitlab-ci'."
        )
    file_path = _cdn_component_file_path(component, label, env)
    file_json = _fetch_cdn_file_at_path(file_path)
    if file_json is None:
        raise FileNotFoundError(
            f"No CDN file for '{component}' version_label '{label}' in "
            f"env '{env}': expected '{file_path}' on {CDN_DEFAULT_BRANCH}."
        )
    return {
        "component": component,
        "env": env,
        "version_label": label,
        "repo_path": f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}",
        "repo_branch": CDN_DEFAULT_BRANCH,
        "file_path": file_path,
        "content": _decode(file_json),
        "last_commit_id": file_json.get("last_commit_id") or file_json.get("commit_id"),
    }


@mcp.tool()
def find_cdn_tag_deploy_targets(
    deployments: list[dict[str, Any]],
    workers: int = _WORKERS_DEFAULT,
) -> dict[str, Any]:
    """Preview a batch of CDN version updates. **Read-only.**

    Mirrors ``find_tag_deploy_targets`` but for ``devops/cdn @ gitlab-ci``.
    Each deployment updates ``lv_component_version`` inside
    ``<env>/<component>-<version_label>.json``. All changes for one target
    are committed atomically alongside any ``extra_updates`` (companion
    files under the same ``<env>/``).

    Required per deployment: ``component``, ``env``, ``version_label``,
    ``tag``. ``ticket`` is optional but recommended (see below).

    IMPORTANT terminology (don't confuse these two):
      * ``version_label`` — the string baked into the CDN filename between
        the component and ``.json`` (e.g. ``27.0``, ``27.0test``). It is
        NOT a git branch. The legacy input alias ``branch`` is still
        accepted but discouraged — do NOT quote ``version_label`` as a git
        branch when summarizing to the user. Always call it a version
        label / file version-series identifier.
      * ``repo_branch`` — the actual git branch the commit lands on. For
        CDN this is always ``gitlab-ci``, surfaced on every returned
        target and top-level response.

    The ``tag`` MUST be the full version literal the user provided
    (e.g. ``"27.0.51"`` or ``"27.0.53/mfe"``) — bare integers and majors
    that don't match ``version_label``'s major are rejected. Any suffix
    (``/mfe``, ``-AUTO``, ...) baked into ``tag`` lands in the file
    verbatim; the tool surfaces a soft warning if the new tag would drop a
    suffix that the current value carries.

    Optional per deployment:
      * ``ticket`` (e.g. ``DEVOPS-52986``) — recommended but not required.
        Included in the commit message when present
        (``Deploy CDN <component> <tag> (<version_label>) in <env> -
        <ticket> - LV_CD_DEPLOY``); when omitted, that segment is dropped
        and the target carries a non-blocking warning suggesting the AI
        ask the user if they'd like to add one.
      * ``lv_cd_deploy`` (default ``True``). The ``LV_CD_DEPLOY`` marker is
        appended to the commit message on every CDN deploy unless the
        caller explicitly sets ``lv_cd_deploy: False`` — only do that when
        the user says so.

    Optional ``extra_updates`` — list of ``{"file_path": "<env>/...",
    "new_content": "..."}`` companion files. Missing companion files land
    in ``needs_info`` with closest fuzzy matches.

    Env policy: DENYLIST. Any env folder in the CDN repo is deployable
    unless it's explicitly blocked. Blocked entries:
      * ``CDN_NON_DEPLOYABLE_ENVS`` — prod region envs
        (``na3, na4, na5, na6``). Always rejected.
      * ``CDN_ENV_BLACKLIST`` — non-env root folders (``bin, global``) and
        the special ``prod`` folder. Prod has a per-file exception:
        filenames containing ``staging`` or ``test`` (case-insensitive)
        are still updatable (e.g. ``prod/lvp-ui-27.0test.json``).
    Every other env auto-inherits "deployable" — new env folders onboard
    without a code / image change.

    Response shape (identical to ``find_tag_deploy_targets``):
      ``targets`` — ready-to-deploy with ``diff``, ``new_content``,
                     ``commit_message``, ``extra_updates``.
      ``needs_info`` — missing core fields (typically ``ticket``) OR
                        unresolved ``extra_updates`` with candidates.
      ``errors`` — hard failures (env not allowed, bad tag, missing file, …).

    Bulk previews fan out across ``workers`` threads (1..30, default 10).
    Per-entry failures are isolated. ``targets`` / ``needs_info`` / ``errors``
    are kept in input order.
    """
    if not deployments:
        return {"ok": False, "error": "deployments must be a non-empty list."}

    workers_capped = _cap_workers(workers)

    targets: list[dict[str, Any]] = []
    needs_info: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def _process(entry: dict[str, Any]) -> dict[str, Any]:
        """Return one bucketed dict: ``{"_kind": ..., "payload": ...}``."""
        component = (entry.get("component") or "").strip()
        env = (entry.get("env") or "").strip()
        # ``version_label`` is the string baked into the filename (e.g.
        # ``27.0``, ``27.0test``). ``branch`` accepted as a legacy alias for
        # backwards compat with older callers — but do NOT surface either as
        # a git branch when summarizing to the user; the git branch is
        # always ``gitlab-ci`` and lives on the ``repo_branch`` field.
        version_label = (
            (entry.get("version_label") or entry.get("branch") or "").strip()
        )
        tag = (entry.get("tag") or "").strip()
        ticket = (entry.get("ticket") or "").strip()
        extra_updates_in = entry.get("extra_updates") or []
        # LV_CD_DEPLOY marker is ON by default — every CDN deploy carries it
        # unless the user explicitly asks to omit it (rare).
        lv_cd_deploy = bool(entry.get("lv_cd_deploy", True))

        provided_summary = {
            "component": component or None,
            "env": env or None,
            "version_label": version_label or None,
            "tag": tag or None,
            "ticket": ticket or None,
            "extra_updates_count": len(extra_updates_in),
            "lv_cd_deploy": lv_cd_deploy,
            "source": "cdn",
            "repo_branch": CDN_DEFAULT_BRANCH,
        }

        # 1. Core fields.
        core_missing = [
            name
            for name, val in (
                ("component", component),
                ("env", env),
                ("version_label", version_label),
                ("tag", tag),
            )
            if not val
        ]
        if core_missing:
            return {
                "_kind": "needs_info",
                "payload": {
                    "provided": provided_summary,
                    "missing": core_missing,
                    "reason": (
                        "Missing core CDN deploy fields. Ask the user for "
                        "these values, then call find_cdn_tag_deploy_targets "
                        "again with the complete entry."
                    ),
                },
            }

        # 2. Env denylist — any env folder is deployable unless it's blocked
        #    (prod region envs, non-env root folders, or prod/ files that
        #    don't match the staging/test exception).
        main_file_name = f"{component}-{version_label}.json"
        denied = _reject_non_cdn_deployable_env(env, main_file_name)
        if denied:
            return {
                "_kind": "error",
                "payload": {
                    "provided": provided_summary,
                    "error": denied["error"],
                    "blocked_envs": denied["blocked_envs"],
                },
            }

        # 2b. Tag format (HARD) — reject bare integers, etc.
        tag_error = _validate_deploy_tag_format(tag)
        if tag_error:
            return {
                "_kind": "error",
                "payload": {"provided": provided_summary, "error": tag_error},
            }

        # 2c. Tag / version_label major cross-check (SOFT — warning only).
        # Compares the tag's leading integer against version_label's leading
        # integer. Attached to the target below so the user sees it
        # alongside the diff.
        tag_major_warning = _check_tag_branch_major_mismatch(tag, version_label)

        # 3. Ticket is optional — recommended but never blocking. When
        # omitted, ``_build_cdn_commit_message`` drops that segment and a
        # non-blocking warning is attached below.

        # 4. Build the diff / new_content / commit_message.
        try:
            target = _resolve_cdn_tag_deploy_target(
                component=component,
                env=env,
                version_label=version_label,
                tag=tag,
                ticket=ticket,
                extra_updates=extra_updates_in,
                lv_cd_deploy=lv_cd_deploy,
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            return {
                "_kind": "error",
                "payload": {"provided": provided_summary, "error": str(exc)},
            }

        # 4b. Route unresolved companion files to needs_info.
        unresolved = target.pop("unresolved_extras", [])
        if unresolved:
            return {
                "_kind": "needs_info",
                "payload": {
                    "provided": provided_summary,
                    "missing": ["extra_updates_resolution"],
                    "unresolved_extras": unresolved,
                    "resolved_extras": [
                        {"file_path": e["file_path"], "has_changes": e["has_changes"]}
                        for e in target.get("extra_updates", [])
                    ],
                    "reason": (
                        "One or more requested companion files could not be "
                        "found under the CDN env folder. Show the user each "
                        "`candidates` list and let them pick — or call "
                        "`list_cdn_env_files` to browse. Then re-invoke with "
                        "corrected `extra_updates`. Do NOT guess a path."
                    ),
                },
            }

        # 4c. Nudge the AI to ask about companion files when none were given.
        if not target.get("extra_updates"):
            target["companion_files_hint"] = (
                "No companion file updates were requested. Before asking for "
                "final approval on this CDN deployment, ask the user: 'Do you "
                "also need to modify any companion files in this env (e.g. a "
                ".properties or config file next to the component JSON) as "
                "part of this CDN deploy?' If yes, call `list_cdn_env_files` "
                "for the env, show the file list, collect the user's new "
                "content per file, then re-invoke `find_cdn_tag_deploy_targets` "
                "with `extra_updates` populated."
            )

        # Soft warnings (major mismatch, etc.) — non-blocking; must be
        # shown to the user alongside the diff before approval.
        if tag_major_warning:
            target.setdefault("warnings", []).append(tag_major_warning)
        if not ticket:
            target.setdefault("warnings", []).append(
                "No ticket was provided — the commit message will omit a "
                "ticket reference. Consider asking the user if they'd like "
                "to include one (e.g. 'DEVOPS-52986'), but this is optional; "
                "proceed without one if they confirm."
            )

        return {"_kind": "target", "payload": target}

    outcomes = _threaded_map(_process, list(deployments), workers_capped)

    for idx, out, exc in outcomes:
        if exc is not None:
            errors.append(
                {
                    "provided": {"index": idx, "source": "cdn"},
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            continue
        kind = out.get("_kind")
        payload = out.get("payload")
        if kind == "target":
            targets.append(payload)
        elif kind == "needs_info":
            needs_info.append(payload)
        else:
            errors.append(payload)

    return {
        "ok": True,
        "source": "cdn",
        "repo_path": f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}",
        "repo_branch": CDN_DEFAULT_BRANCH,
        "workers": workers_capped,
        "requested_count": len(deployments),
        "match_count": len(targets),
        "needs_info_count": len(needs_info),
        "error_count": len(errors),
        "targets": targets,
        "needs_info": needs_info,
        "errors": errors,
        "instruction": (
            "First, if `needs_info` is non-empty, handle it: "
            "(a) entries with missing core fields (`component`, `env`, "
            "`version_label`, `tag`) — ask the user and re-invoke; "
            "(b) `missing: ['extra_updates_resolution']` — show the user "
            "each `unresolved_extras[*].requested.file_path` alongside its "
            "`candidates` list (closest matching CDN files), ask them to "
            "pick the correct path (or call `list_cdn_env_files` to browse), "
            "then re-invoke with the corrected `extra_updates`. DO NOT "
            "invent a path or auto-pick a candidate. "
            "Second, for each ready target you MUST present ALL of the "
            "following to the user before asking for approval: "
            "(1) the full `diff` (main file, standard unified diff), "
            "(2) every `extra_updates[*].diff`, "
            "(3) the `commit_message` field verbatim, "
            "(4) the resolved `component`, `env`, `version_label`, `tag`, "
            "`ticket`, and note that this deploys to CDN (`repo_path` / "
            "`repo_branch=gitlab-ci`). **Never call `version_label` a git "
            "branch when summarizing to the user** — it's a filename label "
            "like `27.0` or `27.0test`; the actual git branch is always "
            "`gitlab-ci` and lives on the `repo_branch` field. "
            "(5) every string in `warnings` (if the field is present) — these "
            "are non-blocking cautions like tag/version-label major mismatch "
            "that the user should acknowledge before you proceed. "
            "Third, if a target carries `companion_files_hint`, follow it — "
            "ASK the user whether any CDN companion files need to be modified "
            "BEFORE asking for final approval. Only after explicit approval "
            "may you call `deploy_cdn_tag_targets` with the exact target "
            "objects unchanged and `confirmed_by_user=True`. Targets where "
            "`has_changes` is false will produce a no-op commit."
        ),
    }


@mcp.tool()
def deploy_cdn_tag_targets(
    targets: list[dict[str, Any]],
    confirmed_by_user: bool = False,
    workers: int = _WORKERS_DEFAULT,
) -> dict[str, Any]:
    """Commit the confirmed CDN version updates to ``devops/cdn @ gitlab-ci``.

    **Two-step contract (enforced):**
      1. Call ``find_cdn_tag_deploy_targets`` to get each target's ``diff``
         and every ``extra_updates[*].diff``. Show them all to the user.
      2. Only after explicit approval, call this tool with
         ``confirmed_by_user=True``. Targets must retain the ``diff`` from
         the preview; each ``extra_updates`` entry must also retain its
         own ``diff``. Anything missing ``diff`` is rejected.

    Each target must contain ``component``, ``env``, ``version_label``,
    ``tag``, ``ticket``, ``file_path``, ``new_content``, ``commit_message``,
    and ``diff`` — plus any ``extra_updates``. These are produced by
    ``find_cdn_tag_deploy_targets`` — pass them back unchanged. The legacy
    input alias ``branch`` is still accepted in place of ``version_label``.

    ``version_label`` is a filename label (e.g. ``27.0`` / ``27.0test``), NOT
    a git branch. All CDN commits land on ``gitlab-ci`` regardless.

    Per-target behavior:
      * Env denylist (``CDN_NON_DEPLOYABLE_ENVS`` + ``CDN_ENV_BLACKLIST``,
        with the prod/ per-file exception) is re-checked.
      * All actions (main file + extras) land in one atomic commit on
        ``gitlab-ci``.
      * The commit message from ``find_cdn_tag_deploy_targets`` is used
        verbatim. It defaults to a trailing ``- LV_CD_DEPLOY`` marker
        unless the preview target was built with ``lv_cd_deploy: false``.
      * ``last_commit_id`` is honored per file to prevent racing another writer.
      * No ASG pipeline trigger — CDN publishes on HEAD.

    Multi-target commits run in parallel across ``workers`` threads (1..30,
    default 10). Per-target failures are isolated. ``results`` are returned
    in the same order as the input ``targets``.
    """
    if not targets:
        return {"ok": False, "error": "targets must be a non-empty list."}

    if not confirmed_by_user:
        return {
            "ok": False,
            "needs_confirmation": True,
            "requested_count": len(targets),
            "instruction": (
                "Diff review is mandatory. Present the main `diff` field AND "
                "every `extra_updates[*].diff` from `find_cdn_tag_deploy_targets` "
                "to the user, get explicit approval, and only then call this "
                "tool again with `confirmed_by_user=True`. Do NOT set the flag "
                "on the user's behalf."
            ),
        }

    workers_capped = _cap_workers(workers)

    def _process(t: dict[str, Any]) -> dict[str, Any]:
        component = (t.get("component") or "").strip()
        env = (t.get("env") or "").strip()
        # ``branch`` accepted as legacy alias for ``version_label``.
        version_label = (
            (t.get("version_label") or t.get("branch") or "").strip()
        )
        file_path = (t.get("file_path") or "").strip()
        new_content = t.get("new_content")
        commit_message = (t.get("commit_message") or "").strip()
        tag = (t.get("tag") or "").strip()
        ticket = (t.get("ticket") or "").strip()
        last_commit_id = t.get("last_commit_id") or None
        extra_updates = t.get("extra_updates") or []
        diff = t.get("diff")

        entry: dict[str, Any] = {
            "component": component,
            "env": env,
            "version_label": version_label,
            "tag": tag or None,
            "ticket": ticket or None,
            "source": "cdn",
            "repo_path": f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}",
            "repo_branch": CDN_DEFAULT_BRANCH,
            "file_path": file_path,
            "commit_message": commit_message,
        }

        if not (component and env and version_label and file_path and commit_message) or new_content is None:
            entry["ok"] = False
            entry["error"] = (
                "Target missing required fields (component, env, "
                "version_label, file_path, new_content, commit_message)."
            )
            return entry

        if not diff:
            entry["ok"] = False
            entry["error"] = (
                "Target has no `diff` field — refusing to commit. Regenerate "
                "via `find_cdn_tag_deploy_targets` and show the diff to the "
                "user before committing."
            )
            return entry

        # Every extra update must also carry a `diff` field from the preview.
        missing_extra_diff_idx: Optional[int] = None
        for ex_idx, ex in enumerate(extra_updates):
            if not (ex.get("diff") or "").strip():
                missing_extra_diff_idx = ex_idx
                break
        if missing_extra_diff_idx is not None:
            entry["ok"] = False
            entry["error"] = (
                f"extra_updates[{missing_extra_diff_idx}] has no `diff` "
                "field — refusing to commit. Regenerate via "
                "`find_cdn_tag_deploy_targets` so every companion file's diff "
                "can be shown to the user."
            )
            return entry

        denied = _reject_non_cdn_deployable_env(
            env, file_path.rsplit("/", 1)[-1] if file_path else None
        )
        if denied:
            entry["ok"] = False
            entry["error"] = denied["error"]
            return entry

        # Build the action list: main file + optional companions.
        actions: list[dict[str, Any]] = []
        main_action: dict[str, Any] = {
            "action": "update",
            "file_path": file_path,
            "content": new_content,
        }
        if last_commit_id:
            main_action["last_commit_id"] = last_commit_id
        actions.append(main_action)

        extra_error: Optional[str] = None
        included_extras: list[dict[str, Any]] = []
        for ex_idx, ex in enumerate(extra_updates):
            ex_path = (ex.get("file_path") or "").strip()
            ex_new = ex.get("new_content")
            if not ex_path or ex_new is None:
                extra_error = (
                    f"extra_updates[{ex_idx}] missing file_path or new_content."
                )
                break
            if not ex_path.startswith(f"{env}/"):
                extra_error = (
                    f"extra_updates[{ex_idx}].file_path '{ex_path}' must be "
                    f"under '{env}/'."
                )
                break
            # Prod exception applies per-file: block real prod files even when
            # the main file was allowed through the staging/test carve-out.
            if not _cdn_env_allows_file(env, ex_path):
                extra_error = (
                    f"extra_updates[{ex_idx}].file_path '{ex_path}' is not "
                    f"allowed in env '{env}' (prod/ files must contain "
                    "'staging' or 'test' in the filename)."
                )
                break
            ex_action: dict[str, Any] = {
                "action": "update",
                "file_path": ex_path,
                "content": ex_new,
            }
            if ex.get("last_commit_id"):
                ex_action["last_commit_id"] = ex["last_commit_id"]
            actions.append(ex_action)
            included_extras.append(
                {"file_path": ex_path, "has_changes": ex.get("has_changes")}
            )

        entry["extra_updates"] = included_extras
        entry["action_count"] = len(actions)

        if extra_error:
            entry["ok"] = False
            entry["error"] = extra_error
            return entry

        try:
            res = _post_cdn_commit_multi(commit_message, actions)
        except RuntimeError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            return entry

        if res.status_code in (200, 201):
            data = res.json()
            entry["ok"] = True
            entry["commit_id"] = data.get("id")
            entry["commit_url"] = data.get("web_url")
        else:
            entry["ok"] = False
            entry["status_code"] = res.status_code
            entry["error"] = (res.text or "")[:500]

        return entry

    outcomes = _threaded_map(_process, list(targets), workers_capped)

    results: list[dict[str, Any]] = []
    for idx, entry, exc in outcomes:
        if exc is not None:
            t = targets[idx]
            results.append(
                {
                    "component": (t.get("component") or "").strip(),
                    "env": (t.get("env") or "").strip(),
                    "source": "cdn",
                    "repo_path": f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}",
                    "repo_branch": CDN_DEFAULT_BRANCH,
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
        else:
            results.append(entry)

    succeeded = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    return {
        "ok": len(failed) == 0,
        "source": "cdn",
        "repo_path": f"{CDN_GITLAB_GROUP}/{CDN_PROJECT_NAME}",
        "repo_branch": CDN_DEFAULT_BRANCH,
        "workers": workers_capped,
        "requested_count": len(targets),
        "success_count": len(succeeded),
        "failure_count": len(failed),
        "results": results,
    }


# ── Deploy-history: helpers ──────────────────────────────────────────────────
# Ported from lambda_handler.py so we can answer:
#   * "which components were deployed in <env> in the last N days"
#   * "was <component> deployed to <env> and which version"
#   * "latest version deployed to <component> on <branch(es)>"
# Every deployment we return has passed the same triple check the lambda uses:
#   1. LV_CD_DEPLOY marker present (or component is in the bypass set).
#   2. A pipeline on the same branch reached ``success``.
#   3. The historical config file at that SHA is *active* (nuke flags off,
#      desired_task_count > 0 for ECS, max_size > 0 for ASG).

def _find_key(data: Any, key: str) -> Any:
    """Recursively search nested dicts/lists for the first value under ``key``."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            result = _find_key(v, key)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _find_key(item, key)
            if result is not None:
                return result
    return None


def _find_all_values(data: Any, key: str) -> list[Any]:
    """Recursively find every value stored under ``key`` in nested dicts/lists."""
    results: list[Any] = []
    if isinstance(data, dict):
        if key in data:
            results.append(data[key])
        for v in data.values():
            results.extend(_find_all_values(v, key))
    elif isinstance(data, list):
        for item in data:
            results.extend(_find_all_values(item, key))
    return results


def _node_is_nuked(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    val = node.get("nuke_asg")
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).lower() == "true"


def _compute_nuke_asg(data: Any) -> bool:
    """Whole-ASG "nuke" verdict — top-level flag, or all pools nuked in multi_pool."""
    if not isinstance(data, dict):
        return False
    if _node_is_nuked(data):
        return True
    pools = data.get("multi_pool")
    if isinstance(pools, list) and pools:
        return all(_node_is_nuked(p) for p in pools)
    return False


def _parse_ecs_config(content: str) -> tuple[Optional[str], Optional[int], bool]:
    """Return (lv_component_build, desired_task_count, nuke_ecs)."""
    try:
        data = yaml.safe_load(content)
    except Exception:
        return None, None, False
    nuke_flag = _find_key(data, "nuke_ecs")
    if isinstance(nuke_flag, bool):
        nuke = nuke_flag
    elif nuke_flag is None:
        nuke = False
    else:
        nuke = str(nuke_flag).lower() == "true"
    build = _find_key(data, "lv_component_build")
    desired = _find_key(data, "desired_task_count")
    try:
        desired_int: Optional[int] = int(desired) if desired is not None else None
    except (ValueError, TypeError):
        desired_int = None
    return (str(build) if build is not None else None, desired_int, nuke)


def _parse_asg_config(
    filename: str, content: str
) -> tuple[Optional[str], Optional[str], int, bool]:
    """Return (oss_component_svn_revision, lv_deployment_version, max_size, nuke_asg).

    Handles both ``asg_config.json`` (raw JSON) and ``asg_config.js`` (Node
    ``module.exports = { ... };``) forms. Surfaces both version fields:

      * ``oss_component_svn_revision`` — the field the tag-deploy tools
        rewrite (component's build/SVN revision).
      * ``lv_deployment_version`` — the MyCompany top-level deploy version
        commonly displayed alongside components (may be identical across a
        release cohort). Present in most ASG configs.
    """
    if filename.endswith(".js"):
        text = re.sub(r"^\s*module\.exports\s*=\s*", "", content.strip()).rstrip(";").strip()
    else:
        text = content
    try:
        data = json.loads(text)
    except Exception:
        return None, None, 0, False

    nuke = _compute_nuke_asg(data)
    version = data.get("oss_component_svn_revision") if isinstance(data, dict) else None
    if version is None:
        version = _find_key(data, "oss_component_svn_revision")

    lv_dep = data.get("lv_deployment_version") if isinstance(data, dict) else None
    if lv_dep is None:
        lv_dep = _find_key(data, "lv_deployment_version")

    max_sizes = _find_all_values(data, "max_size")
    max_sizes_int: list[int] = []
    for ms in max_sizes:
        try:
            max_sizes_int.append(int(ms))
        except (ValueError, TypeError):
            pass
    max_size = max(max_sizes_int) if max_sizes_int else 0

    return (
        str(version) if version is not None else None,
        str(lv_dep) if lv_dep is not None else None,
        max_size,
        nuke,
    )


def _historical_active_and_version(
    filename: str, content: str
) -> tuple[bool, Optional[str], Optional[str], str]:
    """Return (active, version, lv_deployment_version, deploy_type) for a
    historical config file.

    ``deploy_type`` is ``"ecs"`` for fargate configs, ``"asg"`` for asg configs,
    ``"unknown"`` otherwise (fail-safe: unknown files are treated as active).

    ``lv_deployment_version`` is only populated for ASG configs — the ECS
    fargate configs do not carry this field, so it is ``None`` there.
    """
    if filename.startswith("fargate_config"):
        version, desired, nuke = _parse_ecs_config(content)
        active = (not nuke) and (desired != 0)
        return active, version, None, "ecs"
    if filename.startswith("asg_config"):
        version, lv_dep, max_size, nuke = _parse_asg_config(filename, content)
        active = (not nuke) and (max_size != 0)
        return active, version, lv_dep, "asg"
    return True, None, None, "unknown"


def _fetch_env_config_at_ref(component: str, env: str, ref: str) -> Optional[tuple[str, str]]:
    """Return (filename, decoded_content) for the env config file at ``ref``,
    trying fargate first, then asg. Returns None if no config file exists."""
    for filename in FARGATE_FILES:
        file_json = _fetch_file(component, env, filename, ref)
        if file_json:
            return filename, _decode(file_json)
    for filename in ASG_FILES:
        file_json = _fetch_file(component, env, filename, ref)
        if file_json:
            return filename, _decode(file_json)
    return None


def _pipeline_ref_matches(pipeline_ref: Optional[str], branch: str) -> bool:
    if not pipeline_ref:
        return False
    return pipeline_ref == branch or pipeline_ref == f"refs/heads/{branch}"


def _get_pipelines_for_sha(
    component: str, sha: str, ref: Optional[str] = None
) -> Optional[list[dict[str, Any]]]:
    """``None`` on API failure; list (possibly empty) otherwise, filtered to ``ref`` when given."""
    params: dict[str, Any] = {"sha": sha, "per_page": 20}
    if ref:
        params["ref"] = ref
    url = _api(f"/projects/{_encoded_project(component)}/pipelines")
    try:
        res = _get(url, params=params)
    except RuntimeError:
        return None
    if res.status_code != 200:
        return None
    pipelines = res.json()
    if ref:
        pipelines = [p for p in pipelines if _pipeline_ref_matches(p.get("ref"), ref)]
    return pipelines


def _list_commits_touching_path(
    component: str,
    branch: str,
    path: str,
    since: Optional[datetime] = None,
    max_pages: int = 3,
) -> list[dict[str, Any]]:
    """Commits (newest first) that touched ``path`` on the branch. Honors ``since``."""
    results: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        params: dict[str, Any] = {
            "ref_name": branch,
            "path": path,
            "per_page": 100,
            "page": page,
        }
        if since is not None:
            params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = _api(f"/projects/{_encoded_project(component)}/repository/commits")
        try:
            res = _get(url, params=params)
        except RuntimeError:
            break
        if res.status_code != 200:
            break
        data = res.json()
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
    return results


def _list_all_config_components() -> list[str]:
    """List all ``<component>-config`` repos in the configured GitLab group."""
    _, group = _endpoint()
    url = _api(f"/groups/{quote(group, safe='')}/projects")
    seen: set[str] = set()
    page = 1
    while True:
        try:
            res = _get(
                url,
                params={
                    "archived": "false",
                    "simple": "true",
                    "per_page": 100,
                    "page": page,
                },
            )
        except RuntimeError:
            break
        if res.status_code != 200:
            break
        data = res.json()
        if not data:
            break
        for proj in data:
            name = proj.get("path") or proj.get("name") or ""
            if name.endswith("-config"):
                seen.add(name[: -len("-config")])
        if "next" not in res.links:
            break
        page += 1
    return sorted(seen)


def _list_version_branches(component: str) -> list[str]:
    """Return version branches (``1.0``, ``4.0``, ``27.0``, ...) for a component's repo."""
    url = _api(f"/projects/{_encoded_project(component)}/repository/branches")
    results: list[str] = []
    page = 1
    while True:
        try:
            res = _get(url, params={"per_page": 100, "page": page})
        except RuntimeError:
            break
        if res.status_code != 200:
            break
        data = res.json()
        if not data:
            break
        for br in data:
            name = br.get("name", "")
            if _VERSION_BRANCH_RE.match(name):
                results.append(name)
        if "next" not in res.links:
            break
        page += 1
    return sorted(results, key=lambda x: float(x))


def _scan_pair_deployments(
    component: str,
    branch: str,
    env: str,
    since: Optional[datetime],
    latest_only: bool,
    include_bypass: bool,
) -> list[dict[str, Any]]:
    """Return every successful deploy on (component, branch, env) matching the filters."""
    is_bypass = component in LV_CD_DEPLOY_BYPASS_COMPONENTS
    if is_bypass and not include_bypass:
        return []

    commits = _list_commits_touching_path(component, branch, env, since=since)
    if not commits:
        return []

    if is_bypass:
        candidate_commits = commits
    else:
        candidate_commits = [c for c in commits if LV_CD_DEPLOY_MARKER in (c.get("message") or "")]

    if not candidate_commits:
        return []

    _, group = _endpoint()
    repo_path = f"{group}/{component}-config"

    results: list[dict[str, Any]] = []
    for commit in candidate_commits:
        sha = commit.get("id")
        if not sha:
            continue

        pipelines = _get_pipelines_for_sha(component, sha, ref=branch)
        if pipelines is None:
            continue  # API error → skip this commit (fail-safe: don't fabricate a success)

        success_pipeline = next(
            (p for p in pipelines if (p.get("status") or "").lower() == "success"),
            None,
        )
        if success_pipeline is None:
            continue

        file_info = _fetch_env_config_at_ref(component, env, sha)
        if file_info is None:
            continue
        filename, content = file_info
        active, version, lv_deployment_version, deploy_type = (
            _historical_active_and_version(filename, content)
        )

        message_first_line = (commit.get("message") or "").splitlines()
        summary = message_first_line[0][:200] if message_first_line else ""

        results.append(
            {
                "component": component,
                "branch": branch,
                "env": env,
                "type": deploy_type,
                "version": version,
                "lv_deployment_version": lv_deployment_version,
                "active_at_time": active,
                "commit_sha": sha,
                "short_sha": sha[:8],
                "committed_at": commit.get("committed_date") or commit.get("authored_date"),
                "author_name": commit.get("author_name"),
                "message": summary,
                "config_file": f"{env}/{filename}",
                "pipeline_id": success_pipeline.get("id"),
                "pipeline_url": success_pipeline.get("web_url"),
                "repo_path": repo_path,
                "bypass_component": is_bypass,
            }
        )

        if latest_only:
            break

    return results


# ── Deploy-history: tool ─────────────────────────────────────────────────────

@mcp.tool()
def list_env_deployments(
    env: str,
    since_days: int = 7,
    components: Optional[list[str]] = None,
    branches: Optional[list[str]] = None,
    latest_only: bool = False,
    include_bypass_components: bool = True,
    type: str = "",
    workers: int = 20,
) -> dict[str, Any]:
    """★ USE THIS TOOL to answer "what is currently deployed in ``<env>``" —
    a single threaded call that fans out across every ``-config`` repo and
    every version branch, verifying each candidate against a successful CD
    pipeline. Do NOT loop ``get_component_config_file`` per component — this
    tool already does the enumeration + fan-out + validation for you.

    **Always pass ``workers=30`` for env-wide scans.** The default (20)
    is a safe midpoint; the cap is 30. Anything below 20 will be slow
    enough that the MCP host may time the call out (this tool touches
    3–5 GitLab endpoints per (component, branch) pair, so a full-env
    scan is ~200–500 HTTP requests). If the caller needs a raw config
    read WITHOUT CD-pipeline validation (much faster), use
    ``get_env_config_values`` instead.

    Typical bulk-inventory call for env ``stg4``, ASG-only, latest deploy
    per (component, branch):

        list_env_deployments(env="stg4", type="asg", latest_only=True,
                             since_days=0, workers=30)

    Each returned row contains ``component``, ``branch``, ``type``,
    ``version`` (= ``oss_component_svn_revision`` for ASG /
    ``lv_component_build`` for ECS), ``lv_deployment_version`` (ASG only —
    the MyCompany top-level deploy version), ``committed_at``,
    ``pipeline_url``, etc. Build the table from those rows.

    A "deployment" here means a commit that passes the triple check used by
    ``lambda_handler.py``:

      1. It carries the ``LV_CD_DEPLOY`` marker in its message — or, for
         components in ``LV_CD_DEPLOY_BYPASS_COMPONENTS`` (``contact``,
         ``recording``), any commit touching the env folder qualifies.
      2. A pipeline that actually ran on the same branch reached ``success``.
      3. The historical config file at that SHA is *active* — ``nuke_ecs``
         off and ``desired_task_count > 0`` for ECS; ``nuke_asg`` off
         (respecting ``multi_pool``) and ``max_size > 0`` for ASG.

    This one tool answers all three common questions:

    * *"Which components were deployed to <env> this week?"* — call with
      just ``env`` and ``since_days=7``.
    * *"Was <component> deployed to <env> recently?"* — pass
      ``components=["<component>"]``.
    * *"Latest version deployed to <component> on <branch(es)>?"* — pass
      ``components=["<component>"]``, ``branches=["27.0", "4.0"]`` and
      ``latest_only=True``.

    Args:
        env: Environment folder name (any env — this is read-only).
        since_days: Only include commits from the last N days. Use ``0`` to
            disable the time filter and walk the full commit history.
        components: Optional list of component repo names (``acm``,
            ``email_gateway``, ``recording_purge``, ...). If omitted, every
            ``-config`` repo in the group is scanned. Consider narrowing
            this when you can — full-group scans are expensive.
        branches: Optional list of version branches (``"27.0"``, etc.). If
            omitted, every version branch of each component is scanned.
        latest_only: If ``True``, only the newest successful deploy per
            ``(component, branch)`` is returned.
        include_bypass_components: If ``False``, skips components in
            ``LV_CD_DEPLOY_BYPASS_COMPONENTS``.
        type: ``"ecs"``, ``"asg"``, or ``""`` (both). Post-scan filter on
            each row's parsed deploy type. Default ``""`` returns both.
        workers: Parallel workers (1..30). Default **20**. Pass **30** for
            env-wide scans — there is no reason to run this tool with
            fewer workers on a full-group scan.

    Returns:
        ``{deployments, deployment_count, components_scanned, pairs_checked, errors, ...}``.
        Each deployment entry contains ``component``, ``branch``, ``env``,
        ``type``, ``version``, ``lv_deployment_version`` (ASG only),
        ``active_at_time``, ``commit_sha``, ``committed_at``,
        ``author_name``, ``message``, ``pipeline_url``, ``config_file``,
        and ``repo_path``. Deployments are sorted newest first by
        ``committed_at``.

    Read-only.
    """
    if type and type not in ("ecs", "asg"):
        return {
            "ok": False,
            "env": env,
            "error": "type must be 'ecs', 'asg', or '' (both).",
        }

    workers_capped = max(1, min(int(workers or 1), 30))

    since_dt: Optional[datetime] = None
    if since_days and since_days > 0:
        since_dt = datetime.now(timezone.utc) - timedelta(days=since_days)

    if components:
        target_components = sorted({c.strip() for c in components if c.strip()})
    else:
        target_components = _list_all_config_components()

    if not target_components:
        return {
            "ok": False,
            "env": env,
            "error": "No components resolved. Pass `components` or ensure the group has -config repos.",
        }

    errors: list[dict[str, Any]] = []
    work_items: list[tuple[str, str]] = []
    if branches:
        # Explicit branch list — no need to hit GitLab per component.
        for component in target_components:
            for br in branches:
                if br and br.strip():
                    work_items.append((component, br.strip()))
    else:
        # Fan out `_list_version_branches` across the same worker pool the
        # scan uses — this is a per-component GitLab GET and was previously
        # the serial bottleneck before the actual deploy scan even started.
        branch_outcomes = _threaded_map(
            _list_version_branches, list(target_components), workers_capped
        )
        for idx, comp_branches, exc in branch_outcomes:
            component = target_components[idx]
            if exc is not None:
                errors.append(
                    {"component": component, "error": f"{exc.__class__.__name__}: {exc}"}
                )
                continue
            if not comp_branches:
                errors.append({"component": component, "error": "No version branches found."})
                continue
            for br in comp_branches:
                work_items.append((component, br))

    deployments: list[dict[str, Any]] = []
    if work_items:
        with ThreadPoolExecutor(max_workers=workers_capped) as pool:
            futures = {
                pool.submit(
                    _scan_pair_deployments,
                    comp,
                    br,
                    env,
                    since_dt,
                    latest_only,
                    include_bypass_components,
                ): (comp, br)
                for comp, br in work_items
            }
            for fut in as_completed(futures):
                comp, br = futures[fut]
                try:
                    deployments.extend(fut.result())
                except Exception as exc:  # noqa: BLE001
                    errors.append({"component": comp, "branch": br, "error": str(exc)})

    if type:
        deployments = [d for d in deployments if d.get("type") == type]

    deployments.sort(key=lambda d: d.get("committed_at") or "", reverse=True)

    return {
        "ok": True,
        "env": env,
        "since_days": since_days,
        "since": since_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if since_dt else None,
        "components_scanned": len(target_components),
        "pairs_checked": len(work_items),
        "workers": workers_capped,
        "latest_only": latest_only,
        "include_bypass_components": include_bypass_components,
        "type_filter": type or None,
        "deployment_count": len(deployments),
        "deployments": deployments,
        "errors": errors,
    }


# ── Baseline deployment finder ──────────────────────────────────────────────
# Pure orchestration over ``_scan_pair_deployments`` for the deploy-planner
# skill: given (component, branch), find the latest successful deploy in
# each candidate env and return them ranked newest-first. No new HTTP logic
# — same validation as ``list_env_deployments`` (LV_CD_DEPLOY marker,
# pipeline success, historical active state).


@mcp.tool()
def find_baseline_deployment(
    component: str,
    branch: str,
    candidate_envs: list[str],
    workers: int = _WORKERS_DEFAULT,
) -> dict[str, Any]:
    """Find the most recent successful deploy of ``(component, branch)`` in
    each ``candidate_envs`` entry, in parallel. **Read-only.**

    Wraps ``_scan_pair_deployments`` (the same helper
    ``list_env_deployments`` uses) with ``latest_only=True``,
    ``since=None``, ``include_bypass=True`` and fans out across envs. Every
    returned row passes the same triple check ``list_env_deployments``
    enforces: ``LV_CD_DEPLOY`` marker (waived for bypass components),
    pipeline success on the branch, historical config file active at that
    SHA.

    Use this from the mycompany-deploy-planner skill's Phase-1b step when a
    target ticket has no linked sibling and the skill needs a baseline
    from an already-deployed env — pass every nearby env in one call
    instead of looping ``list_env_deployments`` per env.

    Args:
        component: Repo-side component name (``acd``, ``email_gateway``,
            ``recording_purge``, …).
        branch: Version branch (e.g. ``"27.0"``).
        candidate_envs: Envs to scan in parallel (any env — read-only).
        workers: Parallel workers (1..30). Default 10.

    Returns ``{ok, component, branch, candidate_envs, workers,
    deployment_count, deployments, errors}`` where each deployment row
    has the same shape ``list_env_deployments.deployments[*]`` returns
    (component, branch, env, type, version, lv_deployment_version,
    commit_sha, committed_at, author_name, message, pipeline_url,
    config_file, repo_path, …). Deployments are sorted newest-first by
    ``committed_at``.
    """
    if not component or not component.strip():
        return {"ok": False, "error": "component is required."}
    if not branch or not branch.strip():
        return {"ok": False, "error": "branch is required."}
    if not candidate_envs:
        return {"ok": False, "error": "candidate_envs must be a non-empty list."}

    component = component.strip()
    branch = branch.strip()
    envs = sorted({e.strip() for e in candidate_envs if e and e.strip()})
    if not envs:
        return {
            "ok": False,
            "error": "candidate_envs must contain at least one non-empty env.",
        }

    workers_capped = _cap_workers(workers)

    def _one(env: str) -> list[dict[str, Any]]:
        return _scan_pair_deployments(
            component=component,
            branch=branch,
            env=env,
            since=None,
            latest_only=True,
            include_bypass=True,
        )

    outcomes = _threaded_map(_one, envs, workers_capped)

    deployments: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for idx, rows, exc in outcomes:
        env = envs[idx]
        if exc is not None:
            errors.append(
                {"env": env, "error": f"{exc.__class__.__name__}: {exc}"}
            )
            continue
        for row in rows or []:
            deployments.append(row)

    deployments.sort(key=lambda d: d.get("committed_at") or "", reverse=True)

    return {
        "ok": True,
        "component": component,
        "branch": branch,
        "candidate_envs": envs,
        "workers": workers_capped,
        "deployment_count": len(deployments),
        "deployments": deployments,
        "errors": errors,
    }


# ── Env config-value extractor: helpers ─────────────────────────────────────
# General-purpose bulk config reader. Answers "for env <X>, give me
# <parameter(s)> across every (ASG|ECS|any) component" in a single threaded
# call. Complements ``list_env_deployments`` (which validates CD-pipeline
# success) — this tool skips the pipeline check and just returns whatever
# is currently written in the config file, so it's faster.

def _probe_env_config_values_for(
    component: str,
    env: str,
    type_filter: str,
    branches_desc: list[str],
    keys: tuple[str, ...],
) -> Optional[dict[str, Any]]:
    """Walk ``branches_desc`` in order and return the first branch whose env
    config file exists for ``component`` under ``type_filter``. Returns a row
    dict (or ``None`` if none of the branches have a matching config).

    Includes ``tried_branches`` (branches actually probed, in order) and
    ``branch_fallback_used`` (True when >1 branch was probed before a hit)
    so the caller can see when the highest branch had no env config for
    this component and the tool walked down to an older branch. This
    prevents silent data loss for components where the newest version
    branch has not yet been onboarded to the target env (e.g. ``acd``
    where ``31.0`` has no ``uk2/`` folder but ``27.0`` does).
    """
    _, group = _endpoint()
    types_to_probe = ("ecs", "asg") if type_filter == "" else (type_filter,)
    tried: list[str] = []
    for branch in branches_desc:
        tried.append(branch)
        for t in types_to_probe:
            repo_component = _normalize_component_for_repo(component, t)
            files = FARGATE_FILES if t == "ecs" else ASG_FILES
            for fname in files:
                fj = _fetch_file(repo_component, env, fname, branch)
                if not fj:
                    continue
                content = _decode(fj)
                parsed = _parse_config(fname, content)
                values: dict[str, Any] = {}
                if isinstance(parsed, dict):
                    for k in keys:
                        values[k] = _find_key(parsed, k)
                else:
                    for k in keys:
                        values[k] = None
                return {
                    "component": component,
                    "repo_component": repo_component,
                    "branch": branch,
                    "type": t,
                    "env": env,
                    "file_path": f"{env}/{fname}",
                    "repo_path": f"{group}/{repo_component}-config",
                    "parsed_ok": parsed is not None,
                    "last_commit_id": fj.get("last_commit_id") or fj.get("commit_id"),
                    "values": values,
                    "tried_branches": tried[:],
                    "branch_fallback_used": len(tried) > 1,
                }
    return None


# ── Env config-value extractor: tool ────────────────────────────────────────

@mcp.tool()
def get_env_config_values(
    env: str,
    keys: list[str],
    type: str = "",
    components: Optional[list[str]] = None,
    branches: Optional[list[str]] = None,
    latest_branch_only: bool = True,
    workers: int = _WORKERS_DEFAULT,
) -> dict[str, Any]:
    """★ USE THIS TOOL for ANY question of the form "in env ``<X>``, give me
    ``<parameter(s)>`` across every (ASG|ECS|any) component" — one
    threaded call, no AI-side loops.

    The AI does NOT enumerate components or branches. This tool does that
    for you: enumerate every ``<component>-config`` repo in the configured
    group, resolve version branches, fetch the env config file, parse it,
    and extract each requested key (using the same recursive ``_find_key``
    the deploy-history parser uses, so nested values still surface).

    Do NOT loop ``get_component_config_file`` / ``get_component_config_summary``
    per component. Send the list of keys here and let the server fan out.

    Examples:

      * "For stg4, give me ``lv_deployment_version`` for every ASG
        component":

            get_env_config_values(
                env="stg4",
                keys=["lv_deployment_version"],
                type="asg",
            )

      * "For eu1, give me ``lv_component_build`` + ``desired_task_count``
        + ``cpu`` + ``memory`` for every ECS component":

            get_env_config_values(
                env="eu1",
                keys=["lv_component_build", "desired_task_count", "cpu", "memory"],
                type="ecs",
            )

      * "For tst3, give me ``instance_type`` + ``min_size`` + ``max_size``
        for both ASG and ECS on branches 27.0 and 4.0":

            get_env_config_values(
                env="tst3",
                keys=["instance_type", "min_size", "max_size"],
                branches=["27.0", "4.0"],
                latest_branch_only=False,
            )

    Args:
        env: Env folder name (any env — this is read-only).
        keys: One or more config keys to extract from the parsed config
            file. Any top-level or nested key that appears in
            ``fargate_config`` or ``asg_config`` files (e.g.
            ``lv_deployment_version``, ``oss_component_svn_revision``,
            ``lv_component_build``, ``min_size``, ``max_size``,
            ``desired_task_count``, ``instance_type``, ``cpu``,
            ``memory``, ``nuke_asg``, ``nuke_ecs``, …). Missing keys
            land as ``None`` on the row's ``values`` dict.
        type: ``"ecs"``, ``"asg"``, or ``""`` (both). Restricts which
            config files are probed per component. Default ``""``.
        components: Optional list of component repo names to restrict the
            scan (``acd``, ``email_gateway``, …). If omitted, every
            ``-config`` repo in the configured group is scanned.
        branches: Optional list of version branches. If omitted, the
            component's version branches are fetched from GitLab (see
            ``latest_branch_only`` for how they're filtered).
        latest_branch_only: If ``True`` (default) and ``branches`` is
            omitted, each component's branches are tried in **descending
            order** (highest version first) and the first branch whose
            env config file actually exists is used. This means a
            component whose highest branch (e.g. ``31.0``) has NOT yet
            been onboarded to ``<env>`` transparently falls back to the
            next-highest branch that has the env folder (e.g. ``27.0``)
            instead of being silently dropped from the results. Each row
            surfaces ``tried_branches`` (branches probed, in order) and
            ``branch_fallback_used`` (``True`` when >1 branch had to be
            tried). Set to ``False`` to scan **every** version branch of
            every component and produce one row per (component, branch)
            pair — much heavier but useful when you need the value on
            every branch. Ignored when ``branches`` is supplied
            (explicit list is always scanned in full).
        workers: Parallel workers (1..30). Default 10. Both the branch
            enumeration and the per-(component, branch) probe fan out
            through this pool.

    Returns:
        ``{ok, env, type_filter, keys, workers, components_scanned,
           pairs_checked, rows_count, rows: [...], errors: [...]}``.
        Each row: ``{component, repo_component, branch, type, env,
                     file_path, repo_path, parsed_ok, last_commit_id,
                     values: {key: value_or_None, ...}}``. Rows are
        sorted by ``(component, type, branch)``.

    NOTE: this is a *raw config* read. It does NOT verify the file was
    ever deployed successfully — no CD pipeline check. For "was this
    deployment successful?" / "what version was last deployed?" use
    ``list_env_deployments`` instead. For "what value is currently in
    the config file?" this is the right tool.

    **Silent-miss safety.** With ``latest_branch_only=True`` (the default)
    the tool walks each component's branches in DESCENDING order and
    stops at the first branch that has the env config file — it does NOT
    just probe the highest branch. This means components whose newest
    version branch has not been onboarded to ``<env>`` yet (e.g. ``acd``
    on ``31.0`` has no ``uk2/``, but ``27.0`` does) fall back
    transparently to the branch that DOES have the config. Rows include
    ``tried_branches`` + ``branch_fallback_used`` so you can see when a
    fallback occurred. If a component genuinely has no ``<env>`` folder
    on ANY of its branches, it's dropped from ``rows`` (and no error is
    raised, because "no config in this env" is a legitimate state — the
    component may not be deployed there at all).

    Read-only.
    """
    if not keys or not any((k or "").strip() for k in keys):
        return {"ok": False, "error": "`keys` must be a non-empty list."}
    if type and type not in ("ecs", "asg"):
        return {"ok": False, "error": "type must be 'ecs', 'asg', or '' (both)."}

    keys_tuple: tuple[str, ...] = tuple(
        sorted({k.strip() for k in keys if k and k.strip()})
    )
    workers_capped = _cap_workers(workers)

    # 1. Resolve components.
    if components:
        target_components = sorted({c.strip() for c in components if c.strip()})
    else:
        target_components = _list_all_config_components()
    if not target_components:
        return {
            "ok": False,
            "env": env,
            "error": (
                "No components resolved. Pass `components` or ensure the "
                "configured GitLab group has -config repos."
            ),
        }

    errors: list[dict[str, Any]] = []
    per_component_branches: dict[str, list[str]] = {}

    # 2. Resolve branches per component (always in descending order so the
    # walk-fallback in ``_probe_env_config_values_for`` tries newest first).
    if branches:
        user_branches = [b.strip() for b in branches if b and b.strip()]
        try:
            sorted_user = sorted(user_branches, key=lambda x: float(x), reverse=True)
        except ValueError:
            sorted_user = sorted(user_branches, reverse=True)
        for c in target_components:
            per_component_branches[c] = sorted_user
    else:
        outs = _threaded_map(
            _list_version_branches, list(target_components), workers_capped
        )
        for idx, comp_branches, exc in outs:
            comp = target_components[idx]
            if exc is not None:
                errors.append(
                    {"component": comp, "error": f"{exc.__class__.__name__}: {exc}"}
                )
                continue
            if not comp_branches:
                errors.append(
                    {"component": comp, "error": "No version branches found."}
                )
                continue
            # `_list_version_branches` returns ascending; keep desc so newer
            # branches are probed first (both in ``walk_mode`` and when
            # rendering the per-branch scan output).
            per_component_branches[comp] = list(reversed(comp_branches))

    # 3. Two modes:
    #    * ``latest_branch_only=True`` + no explicit ``branches`` = WALK
    #      MODE: for each component, probe branches in descending order and
    #      stop at the first branch that actually has the env config file.
    #      Produces at most one row per component. Prevents silent data
    #      loss when a component's highest branch has not yet been onboarded
    #      to <env> (e.g. `acd`'s 31.0 has no `uk2/` folder but 27.0 does).
    #    * ``latest_branch_only=False`` OR explicit ``branches`` = FLAT
    #      MODE: one row per (component, branch) pair, same as before.
    walk_mode = latest_branch_only and not branches

    rows: list[dict[str, Any]] = []
    if walk_mode:
        def _walk(comp: str) -> Optional[dict[str, Any]]:
            return _probe_env_config_values_for(
                comp, env, type, per_component_branches.get(comp, []), keys_tuple
            )

        outcomes = _threaded_map(_walk, list(target_components), workers_capped)
        for idx, row, exc in outcomes:
            comp = target_components[idx]
            if exc is not None:
                errors.append(
                    {"component": comp, "error": f"{exc.__class__.__name__}: {exc}"}
                )
                continue
            if row is not None:
                rows.append(row)
        pairs_checked = len(target_components)
    else:
        work_items: list[tuple[str, str]] = []
        for c in target_components:
            for br in per_component_branches.get(c, []):
                work_items.append((c, br))

        def _probe(item: tuple[str, str]) -> Optional[dict[str, Any]]:
            comp, br = item
            return _probe_env_config_values_for(comp, env, type, [br], keys_tuple)

        outcomes = _threaded_map(_probe, work_items, workers_capped)
        for idx, row, exc in outcomes:
            comp, br = work_items[idx]
            if exc is not None:
                errors.append(
                    {
                        "component": comp,
                        "branch": br,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
                continue
            if row is not None:
                rows.append(row)
        pairs_checked = len(work_items)

    rows.sort(key=lambda r: (r["component"], r["type"], r["branch"]))

    return {
        "ok": True,
        "env": env,
        "type_filter": type or None,
        "keys": list(keys_tuple),
        "latest_branch_only": latest_branch_only if not branches else None,
        "walk_mode": walk_mode,
        "workers": workers_capped,
        "components_scanned": len(target_components),
        "pairs_checked": pairs_checked,
        "rows_count": len(rows),
        "rows": rows,
        "errors": errors,
    }


# ── GitLab freestyle read-only bridge ────────────────────────────────────────
# The specialized tools above cover the deploy / redeploy / config-inspection
# workflows we care about the most. For everything else — commit history,
# blame, "who last touched this file", MR discussions, pipeline runs, tag
# lists, project search — the AI host can send a raw ``GET /api/v4/…`` request
# through ``gitlab_get``.
#
# HARD rules (defense in depth — this tool CAN NEVER MUTATE):
#
#   1. Only ``GET`` is ever issued. The HTTP method is hard-coded in the
#      helper below and is not exposed as a tool argument. There is no
#      ``method`` parameter to override — because we call ``requests.get``
#      directly, no ``POST`` / ``PUT`` / ``DELETE`` / ``PATCH`` can be
#      produced regardless of what the AI sends. This is not a mutation
#      *gate* (like the ECS/ASG redeploy tools) — it is a mutation
#      *impossibility*.
#
#   2. The path must resolve to ``/api/v4/…`` on the configured
#      GITLAB_ENDPOINT host. Absolute URLs, other schemes, host overrides,
#      leading ``//`` and ``..`` path segments are rejected up front.
#
#   3. Response body is capped (``_GITLAB_GET_BODY_CAP`` bytes) so a
#      chatty endpoint can't blow up the AI host's context window.
#
# For any deploy / redeploy / commit-write operation the caller MUST use the
# purpose-built tools (``deploy_tag_targets``, ``redeploy_targets``, …)
# which carry their own two-step confirmation contract. This freestyle bridge
# is deliberately unable to do those things.

# Only allow API paths — nothing outside ``/api/v4/`` (or ``/api/``) can be
# reached. Char class is deliberately narrow: letters, digits, ``.``, ``-``,
# ``_``, ``/``, ``%``. Query strings go via ``params``, not in the path.
_GITLAB_API_PATH_RE = re.compile(r"^/api/(?:v4|graphql)(?:/[A-Za-z0-9._%\-/]*)?$")

# Response body cap for the freestyle tool (200 KB). Big enough for a full
# commit-history page or a large MR diff, small enough not to nuke the AI
# host's context window if the caller forgets to paginate.
_GITLAB_GET_BODY_CAP = 200_000

# Response header allowlist — only fields useful for pagination /
# rate-limit reasoning are surfaced. GitLab exposes many other headers
# (some contain internal implementation details); we filter to keep the
# payload readable.
_GITLAB_GET_HEADERS_ALLOWLIST: frozenset[str] = frozenset(
    {
        "content-type",
        "x-total",
        "x-total-pages",
        "x-per-page",
        "x-page",
        "x-next-page",
        "x-prev-page",
        "ratelimit-limit",
        "ratelimit-remaining",
        "ratelimit-reset",
        "retry-after",
        "link",
    }
)


def _normalize_gitlab_path(path: str, component: str) -> tuple[str, Optional[str]]:
    """Return ``(cleaned_path, error)``.

    * Trims whitespace.
    * Rejects control chars, ``..``, absolute URLs, leading ``//``.
    * Substitutes ``{project}`` with the URL-encoded ``<component>-config``
      project id when ``component`` is provided (convenience so callers
      don't have to hand-encode the group + repo name).
    * If the path doesn't start with ``/``, prepends ``/api/v4/``.
    * Validates the final shape against ``_GITLAB_API_PATH_RE``.
    """
    if not path or not path.strip():
        return "", "`path` is required."
    cleaned = path.strip()
    if any(ord(c) < 0x20 for c in cleaned):
        return "", "path contains control characters — refused."
    if "://" in cleaned or cleaned.startswith("//"):
        return "", (
            "path must be a relative API path (e.g. `/api/v4/projects/...`) — "
            "absolute URLs are refused so the freestyle tool cannot be pointed "
            "at an off-GitLab host."
        )
    if ".." in cleaned:
        return "", (
            "path contains `..` — path traversal is refused. This tool talks "
            "to GitLab's REST/GraphQL API only."
        )

    if "{project}" in cleaned:
        if not component or not component.strip():
            return "", (
                "path uses the `{project}` placeholder but no `component` was "
                "provided. Pass `component=\"<repo-name-without--config-suffix>\"`."
            )
        cleaned = cleaned.replace("{project}", _encoded_project(component.strip()))

    if not cleaned.startswith("/"):
        cleaned = "/api/v4/" + cleaned.lstrip()

    if not _GITLAB_API_PATH_RE.match(cleaned):
        return "", (
            "path must match `^/api/(v4|graphql)(/…)?$`. No absolute URLs, no "
            "query strings (use `params`), no path traversal. Query strings go "
            "in the `params` dict, not inlined in the path."
        )

    return cleaned, None


@mcp.tool()
def gitlab_get(
    path: str,
    params: Optional[dict[str, Any]] = None,
    component: str = "",
    page: int = 0,
    per_page: int = 0,
) -> dict[str, Any]:
    """Freestyle **read-only** GET against the configured GitLab REST/GraphQL API.

    Use this for the flexible search / question-answering asks that the
    purpose-built tools don't cover — commit history, blame, "who last
    touched this file", MR discussions, pipeline runs, tag lists, project
    search, groups, users, activity, etc.

    **Safety contract:**

      * Only ``GET`` is ever issued. This is enforced at the HTTP layer
        (the tool calls ``requests.get`` directly and there is no
        ``method`` parameter to override) — it is *impossible* for this
        tool to POST/PUT/PATCH/DELETE, create/merge/close an MR, push a
        commit, delete a branch, run a pipeline, or perform any other
        state change. For anything that mutates a repo you MUST use the
        specialized tools (``deploy_tag_targets``, ``redeploy_targets``,
        …) which carry their own two-step user-confirmation contract.
      * Paths must resolve to ``/api/v4/…`` or ``/api/graphql`` on the
        configured ``GITLAB_ENDPOINT`` host. Absolute URLs, host overrides,
        ``..`` traversal, and shell characters are all refused up front.

    Args:
        path: Relative API path such as
            ``"/api/v4/projects/{project}/repository/commits"``,
            ``"/api/v4/projects/{project}/repository/files/eu1%2Ffargate_config.yml/blame"``,
            ``"/api/v4/projects/{project}/merge_requests"``, or
            ``"/api/v4/groups/eng/projects"``. If ``{project}`` appears
            it is substituted with the URL-encoded project id of
            ``<component>-config`` (see ``component``). If ``path`` does
            not begin with ``/`` it is prefixed with ``/api/v4/``.
        params: Optional query-string parameters, passed verbatim to
            GitLab. Common ones: ``{"ref_name": "27.0"}``,
            ``{"since": "2026-07-01T00:00:00Z"}``,
            ``{"path": "eu1/fargate_config.yml"}``,
            ``{"search": "designer"}``.
        component: Optional convenience — the component repo name
            (``acm``, ``email_gateway``, ``recording_purge``, …). When
            set, the ``{project}`` placeholder in ``path`` expands to
            the URL-encoded ``<group>/<component>-config`` project id.
            Ignored when ``{project}`` is not in the path.
        page: Optional page number (>=1). Sent as ``?page=…`` in
            addition to any ``params``. Ignored when 0.
        per_page: Optional page size (1..100, GitLab's max). Ignored
            when 0.

    Returns ``{ok, path, effective_path, status_code, content_type,
    response, raw_text, headers, truncated}``. When the body is JSON it
    is parsed into ``response``; otherwise the first
    ``_GITLAB_GET_BODY_CAP`` bytes land in ``raw_text``.
    """
    cleaned, err = _normalize_gitlab_path(path, component)
    if err:
        return {"ok": False, "path": path, "component": component or None, "error": err}

    query: dict[str, Any] = dict(params or {})
    if page and int(page) > 0:
        query.setdefault("page", int(page))
    if per_page and int(per_page) > 0:
        query.setdefault("per_page", max(1, min(int(per_page), 100)))

    base, _ = _endpoint()
    url = f"{base}{cleaned}"

    try:
        # Hard-coded ``GET``. There is no ``method`` param to override.
        resp = _get(url, params=query or None)
    except RuntimeError as exc:
        return {
            "ok": False,
            "path": path,
            "effective_path": cleaned,
            "params": query or None,
            "error": str(exc),
        }

    content_type = (resp.headers.get("Content-Type") or "").lower()
    body_parsed: Any = None
    raw_text: Optional[str] = None
    truncated = False

    text = resp.text or ""
    if len(text) > _GITLAB_GET_BODY_CAP:
        text = text[:_GITLAB_GET_BODY_CAP]
        truncated = True

    if "application/json" in content_type:
        try:
            # Parse from ``resp.text`` (already capped) so we never load a
            # massive JSON body into a Python object just to truncate it.
            body_parsed = json.loads(text) if not truncated else None
            if truncated:
                raw_text = text
        except (ValueError, json.JSONDecodeError):
            raw_text = text
    else:
        raw_text = text

    filtered_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() in _GITLAB_GET_HEADERS_ALLOWLIST
    }

    return {
        "ok": 200 <= resp.status_code < 300,
        "path": path,
        "effective_path": cleaned,
        "params": query or None,
        "status_code": resp.status_code,
        "content_type": content_type,
        "response": body_parsed,
        "raw_text": raw_text,
        "headers": filtered_headers,
        "truncated": truncated,
    }


# ── GitLab freestyle WRITE bridge (double-confirmation) ─────────────────────
# ``gitlab_get`` above is read-only by construction (hard-coded ``requests.get``,
# no ``method`` argument). This is the write-side escape hatch for anything the
# purpose-built tools (``deploy_tag_targets``, ``commit_multi_file_targets``,
# ``deploy_cdn_tag_targets``, …) don't cover — e.g. creating a file in a repo
# that isn't a ``<component>-config`` repo, deleting a branch, creating a tag,
# editing project settings. It bypasses EVERY safety rail those tools bake in
# (env whitelist/denylist, tag-format validation, atomic multi-file commits,
# ASG pipeline triggers, diff-only-on-known-fields) — so it requires TWO
# separate rounds of explicit user confirmation, not the single
# ``confirmed_by_user`` gate every other write tool here uses.

_GITLAB_WRITE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Matches ``/api/v4/projects/<id>/repository/files/<encoded path>`` with no
# query string — the one shape we know how to preview a content diff for.
_GITLAB_FILES_ENDPOINT_RE = re.compile(
    r"^/api/v4/projects/[^/]+/repository/files/[^/?]+$"
)


def _gitlab_write_fetch_current_file(files_path: str, ref: str) -> Optional[dict[str, Any]]:
    """GET the file at an already-built ``.../repository/files/<encoded>`` path
    (no query string) at ``ref``. Returns ``None`` ONLY on a genuine 404 (file
    doesn't exist). Connection/timeout failures and any other non-200/404
    status are raised as ``RuntimeError`` instead of being silently treated as
    "file doesn't exist" — a failed existence check must never be reported as
    a confirmed create/update."""
    base, _ = _endpoint()
    url = f"{base}{files_path}"
    res = _get(url, params={"ref": ref})
    if res.status_code == 404:
        return None
    if res.status_code != 200:
        raise RuntimeError(
            f"Existence check for '{files_path}' failed with HTTP "
            f"{res.status_code}: {(res.text or '')[:300]}"
        )
    return res.json()


def _gitlab_write_preview(
    method: str, cleaned_path: str, body: dict[str, Any]
) -> dict[str, Optional[str]]:
    """Best-effort unified diff for a ``repository/files`` content PUT/POST.

    Returns ``{"diff": ..., "diff_note": ...}`` — exactly one is populated
    (or both ``None`` when the call isn't a files-content write at all, e.g.
    triggering a pipeline or deleting a branch, which have nothing to diff).
    """
    if method not in ("PUT", "POST") or not _GITLAB_FILES_ENDPOINT_RE.match(cleaned_path):
        return {"diff": None, "diff_note": None}

    new_content = body.get("content")
    if new_content is None:
        return {
            "diff": None,
            "diff_note": "body_json has no `content` field — nothing to diff.",
        }
    ref = body.get("branch") or body.get("ref")
    if not ref:
        return {
            "diff": None,
            "diff_note": (
                "body_json has no `branch` — can't fetch the current file to "
                "build a diff preview. GitLab itself still requires `branch` "
                "in the body for this call to succeed."
            ),
        }

    try:
        current = _gitlab_write_fetch_current_file(cleaned_path, ref)
    except RuntimeError as exc:
        return {
            "diff": None,
            "diff_note": (
                f"Could not verify whether the file already exists: {exc} "
                "This is a real error, not confirmation that the file is "
                "missing — do not treat it as a safe create/update."
            ),
        }
    if method == "POST":
        if current is not None:
            return {
                "diff": None,
                "diff_note": (
                    "This is a POST (create) but a file already exists at this "
                    "path on this ref — GitLab will reject the commit. Use PUT "
                    "to update it instead."
                ),
            }
        return {"diff": _make_unified_diff("", new_content, "(new file)"), "diff_note": None}

    # PUT (update) — file must already exist.
    if current is None:
        return {
            "diff": None,
            "diff_note": (
                "This is a PUT (update) but no existing file was found at this "
                "path on this ref — GitLab will reject the commit. Use POST to "
                "create it instead."
            ),
        }
    old_content = _decode(current)
    return {"diff": _make_unified_diff(old_content, new_content, "(file)"), "diff_note": None}


@mcp.tool()
def gitlab_write(
    method: str,
    path: str,
    body_json: str = "",
    component: str = "",
    confirmed_by_user: bool = False,
    double_confirmed: bool = False,
) -> dict[str, Any]:
    """Freestyle **write** (POST/PUT/PATCH/DELETE) against the configured GitLab
    REST API. **Requires TWO separate rounds of explicit user confirmation**
    before anything is sent — stricter than every other write tool in this
    server, because this one has none of their built-in safety rails.

    Use this only when no purpose-built tool fits (``deploy_tag_targets``,
    ``commit_multi_file_targets``, ``deploy_cdn_tag_targets``, ``redeploy_targets``,
    …) — e.g. creating a file in a repo outside the ``<component>-config`` /
    ``devops/cdn`` shapes those tools know about, deleting a branch, creating a
    tag, editing project settings. It bypasses env whitelists/denylists, tag
    format validation, atomic multi-file commits, and ASG pipeline triggers —
    always prefer the specialized tool when one exists.

    **Three-call contract:**

      1. Call with ``confirmed_by_user=False`` (default) and
         ``double_confirmed=False`` (default). Returns a preview only —
         resolved ``method``/``effective_path``/``body``, plus a best-effort
         ``diff`` when the call looks like a ``repository/files`` content
         PUT/POST (``diff_note`` explains when one couldn't be built). Show
         ALL of this to the user and ask for approval. Nothing is sent yet.
      2. Only after that explicit approval, call again with
         ``confirmed_by_user=True`` (``double_confirmed`` still omitted).
         This STILL does not execute — it returns the identical preview under
         a second, more strongly worded warning, and asks for a SECOND,
         independent "yes" from the user specifically because this tool has
         no guardrails. Do not infer this second yes from the first.
      3. Only after that second explicit approval, call a third time with
         BOTH ``confirmed_by_user=True`` AND ``double_confirmed=True``. Only
         this exact combination executes the request.

    Never set either flag to ``True`` on the user's behalf, and never chain
    steps 1→2 or 2→3 within the same turn — each round's approval must come
    from its own distinct user message.

    Args:
        method: ``POST``, ``PUT``, ``PATCH``, or ``DELETE`` (case-insensitive).
            ``GET`` is refused — use ``gitlab_get`` for reads.
        path: Relative API path, e.g.
            ``"/api/v4/projects/{project}/repository/files/perf1%2Ffoo.json"``
            or a literal project path like
            ``"/api/v4/projects/devops%2Fcdn/repository/files/perf1%2Ffoo.json"``.
            Same validation as ``gitlab_get`` (``/api/v4/…`` or
            ``/api/graphql`` only; no absolute URLs, no ``..`` traversal).
        body_json: JSON object encoded as a string, e.g.
            ``'{"branch": "gitlab-ci", "content": "...", "commit_message": "..."}'``.
            Leave empty for bodyless calls (some ``DELETE`` endpoints).
        component: Optional convenience — expands a ``{project}`` placeholder
            in ``path`` to ``<group>/<component>-config``. Ignored when
            ``{project}`` isn't in ``path`` (e.g. CDN writes, which spell out
            the literal ``devops/cdn`` project path instead).
        confirmed_by_user: Round-1 approval gate.
        double_confirmed: Round-2 approval gate. Only takes effect together
            with ``confirmed_by_user=True``.

    Returns ``{ok, method, path, effective_path, body, commit_message, diff,
    diff_note, needs_confirmation, confirmation_round, instruction}`` while
    pending, or ``{ok, method, path, effective_path, body, commit_message,
    diff, status_code, response, raw_text}`` once actually executed.
    ``commit_message`` is extracted from ``body`` for convenience so it's
    never buried — always show it verbatim to the user alongside the diff.
    """
    method_up = (method or "").strip().upper()
    if method_up not in _GITLAB_WRITE_METHODS:
        return {
            "ok": False,
            "error": (
                f"method must be one of {sorted(_GITLAB_WRITE_METHODS)} — GET is "
                "refused here, use `gitlab_get` for reads."
            ),
        }

    cleaned, err = _normalize_gitlab_path(path, component)
    if err:
        return {"ok": False, "path": path, "component": component or None, "error": err}

    body: dict[str, Any] = {}
    if body_json and body_json.strip():
        try:
            parsed_body = json.loads(body_json)
        except (ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"body_json is not valid JSON: {exc}"}
        if not isinstance(parsed_body, dict):
            return {"ok": False, "error": "body_json must decode to a JSON object."}
        body = parsed_body

    preview = _gitlab_write_preview(method_up, cleaned, body)
    result: dict[str, Any] = {
        "ok": False,
        "method": method_up,
        "path": path,
        "effective_path": cleaned,
        "body": body,
        "commit_message": body.get("commit_message"),
        "diff": preview.get("diff"),
        "diff_note": preview.get("diff_note"),
    }

    if not confirmed_by_user:
        result["needs_confirmation"] = True
        result["confirmation_round"] = 1
        result["instruction"] = (
            "ROUND 1 of 2. Show `method`, `effective_path`, `commit_message`, "
            "and `diff` (or `diff_note` when no diff could be built) to the "
            "user verbatim — `commit_message` MUST be shown explicitly, not "
            "just left inside `body`. This is a freestyle write with NONE of "
            "the purpose-built tools' safety rails (no env whitelist/denylist, "
            "no tag-format check, no atomic multi-file commit, no ASG pipeline "
            "trigger). Get explicit approval, then re-invoke with "
            "`confirmed_by_user=True` — that call will NOT execute yet, it "
            "only advances to round 2."
        )
        return result

    if not double_confirmed:
        result["needs_confirmation"] = True
        result["confirmation_round"] = 2
        result["instruction"] = (
            "ROUND 2 of 2 — FINAL CONFIRMATION. The user already approved once; "
            "this is a SECOND, independent confirmation required because this "
            "tool bypasses every guardrail in this server. Show the SAME "
            "`method` / `effective_path` / `commit_message` / `diff` again and "
            "ask the user to confirm a second time in this conversation — do "
            "NOT infer this from the first approval. Only after that second "
            "explicit 'yes' may you re-invoke with BOTH "
            "`confirmed_by_user=True` AND `double_confirmed=True`. That exact "
            "combination is the only one that executes the request."
        )
        return result

    # Both confirmations received — execute.
    base, _ = _endpoint()
    url = f"{base}{cleaned}"
    try:
        resp = requests.request(
            method_up, url, headers=_headers(), json=body or None, timeout=30
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        result["error"] = (
            f"GitLab endpoint {base} is not reachable ({exc.__class__.__name__}). "
            "Double-check your VPN connection and try again."
        )
        return result

    content_type = (resp.headers.get("Content-Type") or "").lower()
    text = resp.text or ""
    if len(text) > _GITLAB_GET_BODY_CAP:
        text = text[:_GITLAB_GET_BODY_CAP]
    response_json: Any = None
    raw_text: Optional[str] = None
    if "application/json" in content_type:
        try:
            response_json = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            raw_text = text
    else:
        raw_text = text

    result["ok"] = 200 <= resp.status_code < 300
    result["status_code"] = resp.status_code
    result["response"] = response_json
    result["raw_text"] = raw_text
    return result


if __name__ == "__main__":
    mcp.run(transport="stdio")
