"""GitHub MCP server — reads default, writes segregated + confirm-gated.

Tools live in this server organized by tier:

1. **myorgname2 component tools** (``find_component_repo``,
   ``list_component_tags``, ``resolve_component_version``,
   ``get_component_file``, ``list_component_tree``,
   ``search_component_code``). These are hard-scoped to the
   ``myorgname2`` organization (or whatever ``GITHUB_ORG`` is set to) and
   use the deploy-tag / component-name conventions shared with the gitlab
   MCP. Use them when the AI is told a component + version.

2. **Generic multi-org tools** (``list_organizations``,
   ``check_org_access``, ``list_org_repos``, ``search_repos``,
   ``get_repo``, ``list_repo_branches``, ``list_repo_tags``,
   ``get_repo_file``, ``list_repo_tree``, ``list_repo_pull_requests``,
   ``get_pull_request``, ``list_repo_issues``, ``get_issue``,
   ``compare_refs``, ``search_repo_code``). These take an explicit
   ``org`` argument and work against ANY org the token can see
   (``myorgname1``, ``myorgname3``, ``myorgname4``, ...).

3. **GitHub Actions / Checks tools** (``get_pr_checks``,
   ``list_check_runs``, ``list_repo_workflows``, ``list_workflow_runs``,
   ``get_workflow_run``, ``list_workflow_run_jobs``,
   ``get_workflow_job``, ``get_workflow_job_logs``). Read the Checks
   tab / Actions runs on any PR, branch, or commit — including step
   details and (truncated) job logs to diagnose CI failures. Same
   ``org``-explicit shape as tier 2. Requires ``actions:read`` and
   ``checks:read`` on the token in addition to ``contents:read``.

4. **Narrow, confirm-gated writes** (``whoami``,
   ``list_repo_collaborators``, ``list_repo_invitations``,
   ``invite_repo_collaborator``, ``cancel_repo_invitation``,
   ``remove_repo_collaborator``, ``set_repo_visibility``). Repo-owner
   day-to-day operations. Uses the same ``GITHUB_TOKEN`` as reads;
   see "Write guard" below.

5. **Freestyle escape hatches** (``github_get``, ``github_write``).
   ``github_get`` is read-only against a broad allow-list (org
   members, teams, secrets metadata, notifications, gists, users,
   ``/user/packages``, ``/rate_limit``, ...). ``github_write`` is
   confirm-gated against a narrow allow-list of paths (``/repos/…``,
   ``/user/…``, ``/gists*``) — personal repos and org repos are
   treated the same. Everything else remains read-only via
   ``github_get``.

**Write guard.** One ``GITHUB_TOKEN`` for everything. Approval is the
only gate:

* **Confirm gate.** Every write tool takes ``confirm: bool = False``.
  Default returns a preview showing method + endpoint + payload +
  plain-english effect, so the AI can show the user what would change
  before executing. For ``PUT /repos/{owner}/{repo}/contents/{path}``
  the preview also includes a **unified diff** between the current file
  and the new base64-decoded content, so approval is on a real diff.
  Cheap insurance against prompt-injected content (issue bodies,
  READMEs, workflow logs) triggering unwanted writes.

**SAML SSO handling.** MyCompany orgs (``myorgname1``, ``myorgname3``, ...)
enforce SAML SSO on personal access tokens. The first time the token
touches such an org, GitHub returns a ``403`` with an
``X-GitHub-SSO: required; url=…`` header. Every tool detects that and
returns a structured payload with
``error = "SAML_SSO_AUTHORIZATION_REQUIRED"`` and an ``authorize_url``.
Once the user clicks the URL and authorizes the token, the next call
succeeds — no server restart needed.

Configuration (env vars):
  * ``GITHUB_TOKEN``   — required. PAT used by every tool. Read-only
    use needs ``contents:read`` / ``metadata:read`` / ``read:org`` (plus
    ``pull_requests:read``, ``issues:read``, ``actions:read``,
    ``checks:read`` for the corresponding tools). To also enable tier-4
    writes on your own repos, grant ``Administration: R/W`` +
    ``Metadata: R`` (fine-grained PAT) or the classic ``repo`` scope.
    The tier-4 owner guard refuses writes against any owner other than
    the token owner.
  * ``GITHUB_ORG``     — Default org for the myorgname2 component tools.
    Default ``myorgname2``. The generic multi-org tools ignore this.
  * ``GITHUB_BASE_URL``— GitHub API base URL. Default ``https://api.github.com``.
  * ``GITHUB_VERIFY_SSL`` — ``true``/``false``. Default ``false`` because the
    LV corporate VPN intercepts TLS with a firewall-signed cert that isn't in
    the container's trust store. Set to ``true`` if running outside the VPN.
"""

import base64
import difflib
import json
import os
import re
from typing import Any, Optional
from urllib.parse import quote

import requests
import urllib3
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lvx-github")

from version_notifier import install as _install_version_notifier  # noqa: E402
_install_version_notifier(mcp, "lvx-github", fetches_versions=True)

DEFAULT_BASE_URL = "https://api.github.com"
DEFAULT_ORG = "myorgname2"

# A component version is what the user reads off gitlab (fargate_config.yml
# ``lv_component_build`` / asg_config ``oss_component_svn_revision``) or off
# an AWS log. Same shape as the gitlab server's ``_DEPLOY_TAG_RE`` so bare
# integers get rejected up front.
_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9._\-]+)?$")

# ── Component-name maps ──────────────────────────────────────────────────────
# Mirrors the gitlab side (AWS-side name → repo-side name). GitHub repos in
# myorgname2 use the same names as the gitlab -config repos MINUS the
# "-config" suffix, so ``scheduling-config`` on gitlab → ``scheduling`` on
# github. When the AI passes an AWS-side name (e.g. ``emailgateway``), we
# also try the mapped repo-side name (``email_gateway``).
ECS_COMPONENT_MAP: dict[str, str] = {"emailgateway": "email_gateway"}
ASG_COMPONENT_MAP: dict[str, str] = {
    "recordingpurge": "recording_purge",
    "customscripts": "custom_scripts",
}


# ── Configuration ──────────────────────────────────────────────────────────

def _base_url() -> str:
    return os.environ.get("GITHUB_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _org() -> str:
    return os.environ.get("GITHUB_ORG", DEFAULT_ORG).strip("/")


def _verify_ssl() -> bool:
    return os.environ.get("GITHUB_VERIFY_SSL", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


if not _verify_ssl():
    # LV corp VPN intercepts TLS with LVVPN-US-PA01-FW; the container's CA
    # store doesn't include it. verify=False is the pragmatic default here.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN is not set. Provide it via your AI host MCP configuration."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lvx-github-mcp",
    }


def _api(path: str) -> str:
    return f"{_base_url()}{path}"


def _get(
    url: str,
    params: Optional[dict[str, Any]] = None,
    accept: Optional[str] = None,
) -> requests.Response:
    """HTTP GET against the GitHub API using ``GITHUB_TOKEN``.

    Only ``requests.get`` is ever invoked from this function. Write
    verbs go through ``_write_http()`` — same token, but isolated so
    write call sites are easy to grep for.

    ``accept`` overrides the ``Accept`` header (used e.g. for
    ``application/vnd.github.diff`` on compare / pull-request endpoints).
    """
    headers = _headers()
    if accept:
        headers["Accept"] = accept
    try:
        return requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30,
            verify=_verify_ssl(),
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise RuntimeError(
            f"GitHub API {_base_url()} is not reachable ({exc.__class__.__name__}). "
            "Double-check your internet connection and try again."
        ) from exc


def _paginated(
    url: str, params: Optional[dict[str, Any]] = None, cap_pages: int = 20
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    base_params = dict(params or {})
    per_page = int(base_params.setdefault("per_page", 100))
    page = 1
    while page <= cap_pages:
        base_params["page"] = page
        res = _get(url, params=base_params)
        if res.status_code != 200:
            break
        data = res.json()
        if not isinstance(data, list) or not data:
            break
        results.extend(data)
        if len(data) < per_page:
            break
        page += 1
    return results


# ── Repo resolution + fuzzy match ──────────────────────────────────────────

def _repo_candidates(component: str) -> list[str]:
    """Return candidate repo names for a component, trying the raw name and
    the AWS/repo-side maps in both directions."""
    seen: set[str] = set()
    out: list[str] = []

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            out.append(name)

    _add(component)
    for mapping in (ECS_COMPONENT_MAP, ASG_COMPONENT_MAP):
        if component in mapping:
            _add(mapping[component])
        for k, v in mapping.items():
            if v == component:
                _add(k)
    return out


def _fuzzy_org_repos(component: str, limit: int = 10) -> list[dict[str, Any]]:
    """List every repo in the org and rank by similarity to ``component``.

    Called only after direct lookups have failed, so cost is amortized.
    Silently returns ``[]`` if the org listing itself fails (e.g. token
    lacks org access) so we can still surface the primary error clearly.
    """
    try:
        repos = _paginated(
            _api(f"/orgs/{quote(_org(), safe='')}/repos"),
            params={"type": "all"},
        )
    except RuntimeError:
        return []

    cmp_lower = component.lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in repos:
        name = (r.get("name") or "").lower()
        if not name:
            continue
        score = difflib.SequenceMatcher(None, cmp_lower, name).ratio()
        if name == cmp_lower:
            score = 1.0
        elif cmp_lower in name or name in cmp_lower:
            score = max(score, 0.85)
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "name": r.get("name"),
            "full_name": r.get("full_name"),
            "html_url": r.get("html_url"),
            "similarity": round(score, 3),
        }
        for score, r in scored[:limit]
    ]


def _resolve_repo(component: str) -> dict[str, Any]:
    """Return ``{"ok": True, "name": <resolved>, ...}`` or
    ``{"ok": False, "candidates": [...], ...}`` when nothing matched.

    We try each candidate name via a direct ``GET /repos/{org}/{name}`` call
    (cheap) and only fall back to listing + fuzzy-matching all org repos
    when nothing hits. Auth / access failures short-circuit with an explicit
    error instead of leaking into the fuzzy-match path.
    """
    tried: list[str] = []
    for candidate in _repo_candidates(component):
        tried.append(candidate)
        url = _api(f"/repos/{quote(_org(), safe='')}/{quote(candidate, safe='')}")
        res = _get(url)
        if res.status_code == 200:
            data = res.json()
            return {
                "ok": True,
                "name": data.get("name") or candidate,
                "full_name": data.get("full_name") or f"{_org()}/{candidate}",
                "default_branch": data.get("default_branch"),
                "html_url": data.get("html_url"),
                "tried": tried,
            }
        if res.status_code == 401:
            return {
                "ok": False,
                "component": component,
                "error": (
                    "GitHub returned 401 Unauthorized. GITHUB_TOKEN is invalid "
                    "or expired. Regenerate a fine-grained PAT with read access "
                    "to the myorgname2 organization and update your AI host config."
                ),
                "tried": tried,
            }
        if res.status_code == 403:
            return {
                "ok": False,
                "component": component,
                "error": (
                    "GitHub returned 403 Forbidden. Either GITHUB_TOKEN lacks "
                    "access to the myorgname2 organization, or the API rate "
                    "limit has been exceeded."
                ),
                "tried": tried,
            }
        # 404 → keep trying other candidates.

    # No direct hit. Fuzzy-match against the org listing to help the user.
    suggestions = _fuzzy_org_repos(component)
    return {
        "ok": False,
        "component": component,
        "error": (
            f"No repo matched '{component}' in the '{_org()}' organization. "
            f"Tried: {', '.join(tried)}. "
            + (
                "Closest matches by name are listed under `candidates`."
                if suggestions
                else "No similar repo names found in the org."
            )
        ),
        "tried": tried,
        "candidates": suggestions,
        "instruction": (
            "Show the `candidates` list to the user and ask which repo they "
            "meant, then re-invoke this tool with the correct component name. "
            "Do NOT guess or auto-pick."
        ),
    }


# ── Tag / version resolution ───────────────────────────────────────────────

def _fetch_tag_ref(repo_full_name: str, tag: str) -> Optional[dict[str, Any]]:
    """Look up a tag ref directly. Returns the ref object or None."""
    url = _api(
        f"/repos/{repo_full_name}/git/ref/tags/{quote(tag, safe='')}"
    )
    r = _get(url)
    if r.status_code != 200:
        return None
    return r.json()


# ── Tools (all read-only) ──────────────────────────────────────────────────

@mcp.tool()
def find_component_repo(component: str) -> dict[str, Any]:
    """Locate a component's GitHub repo in the myorgname2 org. **Read-only.**

    Component names come from the gitlab side minus the ``-config`` suffix
    (e.g. gitlab ``scheduling-config`` → github ``scheduling``). If the
    exact name doesn't match, the tool falls back to listing the org and
    returns the closest matches under ``candidates`` so the AI host can
    disambiguate with the user instead of guessing.
    """
    return _resolve_repo(component)


@mcp.tool()
def list_component_tags(
    component: str, search: str = "", max_results: int = 100
) -> dict[str, Any]:
    """List tags on a myorgname2 repo. **Read-only.**

    Tags are the primary way to identify a deployed version. If the user
    knows the version they want (``27.0.51``), prefer
    ``resolve_component_version`` which does the version → tag mapping in
    one call. Use ``list_component_tags`` when browsing.
    """
    if max_results < 1:
        max_results = 100

    resolved = _resolve_repo(component)
    if not resolved.get("ok"):
        return resolved
    repo_full = resolved["full_name"]

    tags = _paginated(_api(f"/repos/{repo_full}/tags"))
    search_lower = (search or "").strip().lower()
    entries: list[dict[str, Any]] = []
    for t in tags:
        name = t.get("name") or ""
        if search_lower and search_lower not in name.lower():
            continue
        entries.append(
            {
                "name": name,
                "commit_sha": (t.get("commit") or {}).get("sha"),
            }
        )
        if len(entries) >= max_results:
            break
    return {
        "ok": True,
        "component": component,
        "repo": repo_full,
        "search": search or None,
        "tag_count": len(entries),
        "tags": entries,
    }


@mcp.tool()
def list_component_branches(
    component: str, search: str = "", max_results: int = 50
) -> dict[str, Any]:
    """List branches on a myorgname2 repo. **Read-only.**

    Prefer ``list_component_tags`` when the user wants to look at a specific
    deployed version — deployed versions correspond to tags, not branches.
    Branches are useful for looking at in-flight work.
    """
    if max_results < 1:
        max_results = 50

    resolved = _resolve_repo(component)
    if not resolved.get("ok"):
        return resolved
    repo_full = resolved["full_name"]

    branches = _paginated(_api(f"/repos/{repo_full}/branches"))
    search_lower = (search or "").strip().lower()
    entries: list[dict[str, Any]] = []
    for b in branches:
        name = b.get("name") or ""
        if search_lower and search_lower not in name.lower():
            continue
        entries.append(
            {
                "name": name,
                "commit_sha": (b.get("commit") or {}).get("sha"),
                "protected": b.get("protected"),
            }
        )
        if len(entries) >= max_results:
            break
    return {
        "ok": True,
        "component": component,
        "repo": repo_full,
        "default_branch": resolved.get("default_branch"),
        "search": search or None,
        "branch_count": len(entries),
        "branches": entries,
    }


@mcp.tool()
def resolve_component_version(component: str, version: str) -> dict[str, Any]:
    """Resolve a component version to its GitHub tag + commit SHA. **Read-only.**

    Given a version like ``"27.0.51"`` (the value seen in gitlab
    ``fargate_config.yml`` / ``asg_config.js``, or in AWS logs), returns
    the matching tag name in the myorgname2 repo. Handles common tag
    conventions:

      * exact match: ``27.0.51``
      * ``v`` prefix: ``v27.0.51``
      * fuzzy substring fallback if no direct hit

    If multiple tags match ambiguously, returns them as ``candidates`` and
    asks the AI to disambiguate with the user. Feed the resulting
    ``resolved_tag`` (or ``commit_sha``) into ``get_component_file`` /
    ``list_component_tree`` to inspect code at that exact version.
    """
    version_str = (version or "").strip()
    if not _VERSION_RE.match(version_str):
        return {
            "ok": False,
            "component": component,
            "requested_version": version,
            "error": (
                f"Version '{version}' is not a full version literal "
                "(expected `N.N` or `N.N.N`, optionally with a suffix like "
                "`-AUTO`). Common cause: the AI split a version like "
                "`27.0.51` into branch=`27.0` + tag=`51`. Ask the user for "
                "the full version and re-invoke."
            ),
        }

    resolved = _resolve_repo(component)
    if not resolved.get("ok"):
        return resolved
    repo_full = resolved["full_name"]

    # 1. Try direct ref lookups: bare version, then v-prefixed.
    for candidate in (version_str, f"v{version_str}"):
        ref = _fetch_tag_ref(repo_full, candidate)
        if ref is not None:
            return {
                "ok": True,
                "component": component,
                "repo": repo_full,
                "requested_version": version_str,
                "resolved_tag": candidate,
                "commit_sha": (ref.get("object") or {}).get("sha"),
                "match_type": "exact",
            }

    # 2. Fall back to listing tags and substring-matching.
    tags = _paginated(_api(f"/repos/{repo_full}/tags"))
    version_lower = version_str.lower()
    matches: list[dict[str, Any]] = []
    for t in tags:
        name = (t.get("name") or "")
        if version_lower in name.lower():
            matches.append(
                {
                    "name": name,
                    "commit_sha": (t.get("commit") or {}).get("sha"),
                }
            )

    if not matches:
        return {
            "ok": False,
            "component": component,
            "repo": repo_full,
            "requested_version": version_str,
            "error": (
                f"No tag in {repo_full} matches version '{version_str}'. "
                "Ask the user to confirm the exact version, or call "
                "`list_component_tags` to browse."
            ),
        }
    if len(matches) == 1:
        return {
            "ok": True,
            "component": component,
            "repo": repo_full,
            "requested_version": version_str,
            "resolved_tag": matches[0]["name"],
            "commit_sha": matches[0]["commit_sha"],
            "match_type": "substring",
        }
    return {
        "ok": False,
        "component": component,
        "repo": repo_full,
        "requested_version": version_str,
        "error": (
            f"Multiple tags in {repo_full} match version '{version_str}'. "
            "Ambiguous — showing candidates."
        ),
        "candidates": matches,
        "instruction": (
            "Show `candidates` to the user and ask which exact tag to use. "
            "Then re-invoke this tool with the fully-qualified tag string, "
            "or pass the chosen tag name directly as `ref` to "
            "`get_component_file` / `list_component_tree`."
        ),
    }


@mcp.tool()
def get_component_file(
    component: str, ref: str, file_path: str
) -> dict[str, Any]:
    """Fetch a single file at a specific ref (tag / branch / commit SHA). **Read-only.**

    ``ref`` accepts any GitHub ref: a tag name (recommended for inspecting
    deployed versions — resolve one via ``resolve_component_version``), a
    branch name, or a full commit SHA. Returns the decoded UTF-8 content
    plus size and the GitHub web URL for citing to the user.

    Binary files (any content that doesn't decode as UTF-8) are refused
    with an error instead of returning garbage.
    """
    resolved = _resolve_repo(component)
    if not resolved.get("ok"):
        return resolved
    repo_full = resolved["full_name"]

    if not file_path or not ref:
        return {
            "ok": False,
            "error": "Both `ref` and `file_path` are required.",
        }

    url = _api(
        f"/repos/{repo_full}/contents/{quote(file_path.lstrip('/'), safe='/')}"
    )
    r = _get(url, params={"ref": ref})
    if r.status_code == 404:
        return {
            "ok": False,
            "component": component,
            "repo": repo_full,
            "ref": ref,
            "file_path": file_path,
            "error": (
                f"File '{file_path}' not found at ref '{ref}' in {repo_full}. "
                "Use `list_component_tree` to browse or `resolve_component_version` "
                "to double-check the ref."
            ),
        }
    if r.status_code != 200:
        return {
            "ok": False,
            "component": component,
            "repo": repo_full,
            "ref": ref,
            "file_path": file_path,
            "error": f"GitHub returned {r.status_code}: {(r.text or '')[:300]}",
        }

    data = r.json()
    if isinstance(data, list):
        return {
            "ok": False,
            "component": component,
            "repo": repo_full,
            "ref": ref,
            "file_path": file_path,
            "error": (
                f"'{file_path}' is a directory, not a file. Use "
                "`list_component_tree` to list its contents instead."
            ),
        }
    if data.get("type") != "file":
        return {
            "ok": False,
            "error": f"Not a file (type={data.get('type')}).",
        }

    content_b64 = data.get("content") or ""
    try:
        content = base64.b64decode(content_b64).decode("utf-8")
    except UnicodeDecodeError:
        return {
            "ok": False,
            "component": component,
            "repo": repo_full,
            "ref": ref,
            "file_path": file_path,
            "size": data.get("size"),
            "error": (
                "File appears to be binary; refusing to decode as UTF-8. "
                "Use the `html_url` in the GitHub UI to inspect it."
            ),
            "html_url": data.get("html_url"),
        }
    return {
        "ok": True,
        "component": component,
        "repo": repo_full,
        "ref": ref,
        "file_path": file_path,
        "size": data.get("size"),
        "html_url": data.get("html_url"),
        "content": content,
    }


@mcp.tool()
def list_component_tree(
    component: str,
    ref: str,
    path: str = "",
    recursive: bool = False,
    max_entries: int = 500,
) -> dict[str, Any]:
    """List files and directories at a path at a specific ref. **Read-only.**

    Args:
        component: Component name (myorgname2 repo).
        ref: Tag / branch / commit SHA. Use ``resolve_component_version``
            first to get the tag for a specific deployed version.
        path: Directory path relative to repo root. Empty = repo root.
        recursive: If ``True``, walks the whole tree at this ref. Uses the
            git-trees API and is capped at ``max_entries``. Ignores ``path``
            when set (returns the full tree; the AI can filter client-side).
        max_entries: Safety cap on entries returned. Default 500.
    """
    resolved = _resolve_repo(component)
    if not resolved.get("ok"):
        return resolved
    repo_full = resolved["full_name"]

    if not ref:
        return {"ok": False, "error": "`ref` is required."}

    if recursive:
        # /git/trees/<sha_or_ref>?recursive=1 — cheapest way to get a full
        # tree walk. GitHub returns "truncated: true" when the tree is
        # bigger than ~7MB / 100k entries; we also cap client-side.
        url = _api(f"/repos/{repo_full}/git/trees/{quote(ref, safe='')}")
        r = _get(url, params={"recursive": "1"})
        if r.status_code != 200:
            return {
                "ok": False,
                "component": component,
                "repo": repo_full,
                "ref": ref,
                "error": (
                    f"GitHub returned {r.status_code} when listing the tree at "
                    f"ref '{ref}': {(r.text or '')[:200]}"
                ),
            }
        data = r.json()
        raw_tree = data.get("tree") or []
        prefix = path.strip("/")
        entries: list[dict[str, Any]] = []
        for it in raw_tree:
            p = it.get("path") or ""
            if prefix and not (p == prefix or p.startswith(prefix + "/")):
                continue
            entries.append(
                {
                    "path": p,
                    "type": "blob" if it.get("type") == "blob" else it.get("type"),
                    "size": it.get("size"),
                    "sha": it.get("sha"),
                }
            )
            if len(entries) >= max_entries:
                break
        return {
            "ok": True,
            "component": component,
            "repo": repo_full,
            "ref": ref,
            "path": path or None,
            "recursive": True,
            "truncated": bool(data.get("truncated")) or len(entries) >= max_entries,
            "entry_count": len(entries),
            "entries": entries,
        }

    # Non-recursive: list a single directory via /contents.
    url = _api(
        f"/repos/{repo_full}/contents/{quote((path or '').lstrip('/'), safe='/')}"
    )
    r = _get(url, params={"ref": ref})
    if r.status_code == 404:
        return {
            "ok": False,
            "component": component,
            "repo": repo_full,
            "ref": ref,
            "path": path or None,
            "error": f"Path '{path}' not found at ref '{ref}'.",
        }
    if r.status_code != 200:
        return {
            "ok": False,
            "error": f"GitHub returned {r.status_code}: {(r.text or '')[:200]}",
        }
    data = r.json()
    if isinstance(data, dict):
        # /contents on a file returns the file dict, not a list.
        return {
            "ok": False,
            "error": (
                f"'{path}' is a file, not a directory. Use `get_component_file` "
                "to fetch it."
            ),
        }
    entries = [
        {
            "path": it.get("path"),
            "name": it.get("name"),
            "type": it.get("type"),
            "size": it.get("size"),
            "sha": it.get("sha"),
        }
        for it in data[:max_entries]
    ]
    return {
        "ok": True,
        "component": component,
        "repo": repo_full,
        "ref": ref,
        "path": path or None,
        "recursive": False,
        "entry_count": len(entries),
        "entries": entries,
    }


@mcp.tool()
def search_component_code(
    component: str, query: str, max_results: int = 20
) -> dict[str, Any]:
    """Search code inside a myorgname2 repo. **Read-only.**

    Typical use: after finding an error string in AWS logs, search the
    component's source for that string to locate the code path that
    produced it. Returns matching file paths + GitHub web URLs.

    **Important limitation:** GitHub's code search API runs against the
    repo's DEFAULT BRANCH ONLY. It does NOT support searching at an
    arbitrary tag/ref. Workflow for version-scoped inspection:

      1. Call ``search_component_code`` to locate candidate files by
         name / string match on the default branch.
      2. Call ``resolve_component_version`` to get the tag for the version
         under investigation.
      3. Call ``get_component_file`` with that tag as ``ref`` to read the
         actual file contents at the deployed version.

    Args:
        component: Component name.
        query: A GitHub code-search query (plain text or the code-search
            qualifiers like ``path:`` / ``extension:``). ``repo:`` is
            appended automatically.
        max_results: Cap on returned matches (max 100 per GitHub).
    """
    if not query or not query.strip():
        return {"ok": False, "error": "`query` is required."}
    if max_results < 1:
        max_results = 20
    if max_results > 100:
        max_results = 100

    resolved = _resolve_repo(component)
    if not resolved.get("ok"):
        return resolved
    repo_full = resolved["full_name"]

    q = f"{query.strip()} repo:{repo_full}"
    r = _get(_api("/search/code"), params={"q": q, "per_page": max_results})
    if r.status_code == 422:
        return {
            "ok": False,
            "component": component,
            "repo": repo_full,
            "query": query,
            "error": (
                "GitHub rejected the code-search query (422). Empty queries "
                "and some qualifier combinations aren't allowed. Try a more "
                "specific query."
            ),
        }
    if r.status_code == 403:
        return {
            "ok": False,
            "component": component,
            "repo": repo_full,
            "query": query,
            "error": (
                "GitHub returned 403 on /search/code — likely the code-search "
                "rate limit (30 req/min). Wait a minute and retry."
            ),
        }
    if r.status_code != 200:
        return {
            "ok": False,
            "error": f"GitHub returned {r.status_code}: {(r.text or '')[:300]}",
        }

    data = r.json()
    items: list[dict[str, Any]] = []
    for it in data.get("items") or []:
        items.append(
            {
                "file_path": it.get("path"),
                "html_url": it.get("html_url"),
                "score": it.get("score"),
            }
        )
    return {
        "ok": True,
        "component": component,
        "repo": repo_full,
        "query": query,
        "total_count": data.get("total_count"),
        "match_count": len(items),
        "matches": items,
        "note": (
            "GitHub code search runs against the repo's DEFAULT BRANCH ONLY. "
            "To view a match at a specific deployed tag/version, follow up "
            "with `resolve_component_version` + `get_component_file`."
        ),
    }


# ═════════════════════════════════════════════════════════════════════════
# Generic multi-org tools
# ═════════════════════════════════════════════════════════════════════════
#
# The tools above (find_component_repo, list_component_tags, …) are
# scoped to the myorgname2 org and rely on the shared component-naming
# convention. Everything below takes an explicit ``org`` argument so
# MyCompany's other GitHub orgs (``myorgname1``, ``myorgname3``,
# ``myorgname4``, …) can be explored from the same token.
#
# Design contract:
#   * Never hardcode an org list. ``list_organizations`` asks GitHub.
#   * SAML-SSO 403s always come back as a structured payload with
#     ``error = "SAML_SSO_AUTHORIZATION_REQUIRED"`` and an
#     ``authorize_url`` — the AI host surfaces the URL, the user clicks
#     it in a browser, then retries the tool call.
#   * Big responses come back with an opaque ``next_page_token``. Pass
#     it back on the next call to fetch more.
# ─────────────────────────────────────────────────────────────────────────

_ORG_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _validate_org(org: str) -> Optional[dict[str, Any]]:
    if not org or not _ORG_NAME_RE.match(org):
        return {
            "ok": False,
            "error": (
                f"Invalid org name: {org!r}. Must be 1-39 chars, alphanumeric "
                "or single hyphens, and can't start/end with a hyphen."
            ),
        }
    return None


def _validate_repo(repo: str) -> Optional[dict[str, Any]]:
    if not repo or not _REPO_NAME_RE.match(repo):
        return {
            "ok": False,
            "error": (
                f"Invalid repo name: {repo!r}. Must be 1-100 chars, "
                "alphanumeric / dot / underscore / hyphen."
            ),
        }
    return None


def _sso_url_from_response(res: requests.Response) -> Optional[str]:
    """Extract the SSO authorization URL from an ``X-GitHub-SSO`` header.

    Header shape observed on 403 responses::

        X-GitHub-SSO: required; url=https://github.com/orgs/<org>/sso?authorization_request=<token>
    """
    hdr = res.headers.get("X-GitHub-SSO") or res.headers.get("x-github-sso") or ""
    m = re.search(r"url=(\S+)", hdr)
    return m.group(1) if m else None


def _looks_like_sso_body(text: str) -> bool:
    low = (text or "").lower()
    return (
        "saml" in low
        or "sso" in low
        or "organization has enabled or enforced" in low
    )


def _sso_error_payload(
    org: str, res: requests.Response, hint: str = ""
) -> Optional[dict[str, Any]]:
    """Return a structured SSO-required payload if `res` indicates that,
    otherwise ``None``."""
    if res.status_code not in (401, 403):
        return None
    sso_url = _sso_url_from_response(res)
    if not sso_url and not _looks_like_sso_body(res.text or ""):
        return None
    if not sso_url:
        sso_url = f"https://github.com/orgs/{quote(org, safe='')}/sso"
    return {
        "ok": False,
        "org": org,
        "error": "SAML_SSO_AUTHORIZATION_REQUIRED",
        "message": (
            f"Your GITHUB_TOKEN has not been authorized for the '{org}' "
            "organization via SAML SSO. Open `authorize_url` in a browser "
            "signed into your SSO identity provider, click 'Authorize' next "
            "to this token, then retry the same tool call. No server restart "
            "is required."
        ),
        "authorize_url": sso_url,
        "org_sso_page": f"https://github.com/orgs/{quote(org, safe='')}/sso",
        "instruction": (
            "Present `authorize_url` to the user as a clickable link and STOP. "
            "Do NOT retry the tool in a loop. Wait for the user to confirm "
            "they've completed the SSO authorization, then call the tool once "
            "more with the same arguments."
        ),
        "hint": hint or None,
    }


def _error_payload(
    res: requests.Response, org: str = "", context: str = ""
) -> dict[str, Any]:
    """Convert a non-200 GitHub response into a structured error dict.

    SSO-required 403s take precedence over the generic 403 path.
    """
    sso = _sso_error_payload(org, res, hint=context) if org else None
    if sso:
        return sso
    status = res.status_code
    body_snippet = (res.text or "")[:400]
    if status == 401:
        return {
            "ok": False,
            "status": 401,
            "error": (
                "GITHUB_TOKEN is invalid or expired. Regenerate the token "
                "and update your AI host MCP config."
            ),
            "context": context or None,
        }
    if status == 403:
        return {
            "ok": False,
            "status": 403,
            "error": (
                "GitHub returned 403. Likely causes: token missing scope for "
                "this resource, or REST rate limit hit (60/min unauth, "
                "5000/hr auth; /search is 30/min). Body: "
                f"{body_snippet}"
            ),
            "context": context or None,
        }
    if status == 404:
        return {
            "ok": False,
            "status": 404,
            "error": f"Not found. {context}".strip(),
        }
    if status == 422:
        return {
            "ok": False,
            "status": 422,
            "error": f"GitHub rejected the request (422). Body: {body_snippet}",
            "context": context or None,
        }
    return {
        "ok": False,
        "status": status,
        "error": f"GitHub returned {status}: {body_snippet}",
        "context": context or None,
    }


# ── Opaque pagination cursor ──────────────────────────────────────────────
# We wrap GitHub's `page` / `per_page` in a base64-JSON token so the AI host
# treats it as opaque and just round-trips it.

def _encode_cursor(state: dict[str, Any]) -> str:
    return (
        base64.urlsafe_b64encode(
            json.dumps(state, separators=(",", ":")).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )


def _decode_cursor(token: str) -> dict[str, Any]:
    if not token:
        return {}
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _paginated_slice(
    url: str,
    org: str,
    params: Optional[dict[str, Any]] = None,
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 100,
    context: str = "",
) -> dict[str, Any]:
    """Fetch up to ``max_items`` items across sequential pages.

    Returns either
    ``{"ok": True, "items": [...], "count": N, "next_page_token": <token or None>}``
    or a structured error dict (SSO / 401 / 403 / 404 / etc.).
    """
    per_page = max(1, min(int(per_page), 100))
    max_items = max(1, min(int(max_items), 1000))

    state = _decode_cursor(page_token)
    page = int(state.get("page", 1))
    if state.get("per_page"):
        per_page = max(1, min(int(state["per_page"]), 100))

    base_params = dict(params or {})
    base_params["per_page"] = per_page

    collected: list[Any] = []
    last_page_full = False

    while len(collected) < max_items:
        base_params["page"] = page
        res = _get(url, params=base_params)
        if res.status_code != 200:
            return _error_payload(res, org=org, context=context)
        data = res.json()
        if not isinstance(data, list) or not data:
            last_page_full = False
            break
        collected.extend(data)
        last_page_full = len(data) == per_page
        page += 1
        if not last_page_full:
            break

    truncated = collected[:max_items]
    next_token = None
    if last_page_full and len(collected) >= max_items:
        next_token = _encode_cursor({"page": page, "per_page": per_page})

    return {
        "ok": True,
        "items": truncated,
        "count": len(truncated),
        "next_page_token": next_token,
    }


def _slice_local(
    items: list[Any], page_token: str = "", per_page: int = 100
) -> tuple[list[Any], Optional[str]]:
    """Slice an in-memory list using an opaque cursor. Useful for endpoints
    that don't paginate on GitHub's side (e.g. git/trees recursive)."""
    per_page = max(1, min(int(per_page), 1000))
    state = _decode_cursor(page_token)
    start = int(state.get("offset", 0))
    end = start + per_page
    chunk = items[start:end]
    next_token = (
        _encode_cursor({"offset": end, "per_page": per_page})
        if end < len(items)
        else None
    )
    return chunk, next_token


def _paginated_slice_wrapped(
    url: str,
    org: str,
    wrapper_key: str,
    params: Optional[dict[str, Any]] = None,
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 100,
    context: str = "",
) -> dict[str, Any]:
    """Same contract as ``_paginated_slice`` but for endpoints that return
    ``{"total_count": N, "<wrapper_key>": [...]}`` instead of a bare list.

    Used for check-runs, workflow-runs, workflow-run jobs, and workflows
    listings — all of GitHub Actions / Checks reads share this shape.
    """
    per_page = max(1, min(int(per_page), 100))
    max_items = max(1, min(int(max_items), 1000))

    state = _decode_cursor(page_token)
    page = int(state.get("page", 1))
    if state.get("per_page"):
        per_page = max(1, min(int(state["per_page"]), 100))

    base_params = dict(params or {})
    base_params["per_page"] = per_page

    collected: list[Any] = []
    total_count = 0
    last_page_full = False

    while len(collected) < max_items:
        base_params["page"] = page
        res = _get(url, params=base_params)
        if res.status_code != 200:
            return _error_payload(res, org=org, context=context)
        data = res.json() or {}
        items = data.get(wrapper_key) or []
        total_count = int(data.get("total_count") or total_count)
        if not items:
            last_page_full = False
            break
        collected.extend(items)
        last_page_full = len(items) == per_page
        page += 1
        if not last_page_full:
            break

    truncated = collected[:max_items]
    next_token = None
    if last_page_full and len(collected) >= max_items:
        next_token = _encode_cursor({"page": page, "per_page": per_page})

    return {
        "ok": True,
        "items": truncated,
        "count": len(truncated),
        "total_count": total_count,
        "next_page_token": next_token,
    }


# ── Generic multi-org tools ───────────────────────────────────────────────

@mcp.tool()
def list_organizations() -> dict[str, Any]:
    """List every GitHub organization the current token can see. **Read-only.**

    Calls ``GET /user/orgs`` and returns each org's login + description +
    URL. Requires the ``read:org`` scope on the token. Orgs that enforce
    SAML SSO show up here as soon as membership is visible, but any
    org-scoped call may still return
    ``SAML_SSO_AUTHORIZATION_REQUIRED`` on first use — that's normal;
    surface the returned ``authorize_url`` to the user.

    Typical flow: call this once to discover orgs (``myorgname1``,
    ``myorgname2``, ``myorgname3``, ``myorgname4`` at MyCompany), then
    invoke ``check_org_access`` or ``list_org_repos`` per org.
    """
    url = _api("/user/orgs")
    res = _get(url, params={"per_page": 100})
    if res.status_code != 200:
        return _error_payload(res, context="listing organizations")
    data = res.json() or []
    orgs = [
        {
            "login": o.get("login"),
            "id": o.get("id"),
            "description": o.get("description"),
            "url": o.get("url"),
            "html_url": (
                f"https://github.com/{o.get('login')}" if o.get("login") else None
            ),
        }
        for o in data
        if isinstance(o, dict)
    ]
    return {
        "ok": True,
        "count": len(orgs),
        "organizations": orgs,
        "note": (
            "Any org may still require SAML SSO authorization for this token. "
            "If a subsequent call returns SAML_SSO_AUTHORIZATION_REQUIRED, "
            "click the returned `authorize_url` and retry."
        ),
    }


@mcp.tool()
def check_org_access(org: str) -> dict[str, Any]:
    """Verify that the token can read ``org``. **Read-only.**

    Hits ``GET /orgs/{org}`` and reports one of:

      * ``ok=True`` — token is authorized for this org.
      * ``error="SAML_SSO_AUTHORIZATION_REQUIRED"`` — token needs SSO
        approval; ``authorize_url`` is included.
      * ``error="NOT_FOUND"`` — org doesn't exist or token can't see it.
      * ``error="UNAUTHORIZED"`` — the token itself is invalid.

    Cheap enough to call before a batch of tools targeting a new org.
    """
    if err := _validate_org(org):
        return err
    url = _api(f"/orgs/{quote(org, safe='')}")
    res = _get(url)
    if res.status_code == 200:
        data = res.json()
        return {
            "ok": True,
            "org": org,
            "id": data.get("id"),
            "description": data.get("description"),
            "html_url": data.get("html_url"),
            "public_repos": data.get("public_repos"),
            "total_private_repos": data.get("total_private_repos"),
        }
    if res.status_code == 404:
        return {
            "ok": False,
            "org": org,
            "error": "NOT_FOUND",
            "message": (
                f"Organization '{org}' doesn't exist or your token has no "
                "visibility into it."
            ),
        }
    return _error_payload(res, org=org, context=f"checking access to org {org}")


@mcp.tool()
def list_org_repos(
    org: str,
    search: str = "",
    repo_type: str = "all",
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 100,
) -> dict[str, Any]:
    """List repositories in ``org`` with optional name-substring filter. **Read-only.**

    Args:
        org: GitHub organization login (e.g. ``myorgname1``).
        search: Optional case-insensitive substring filter on repo name.
            Applied after fetching from GitHub — for a real repo search
            across an org, prefer ``search_repos``.
        repo_type: One of ``all`` / ``public`` / ``private`` / ``forks``
            / ``sources`` / ``member``. Default ``all``.
        page_token: Opaque cursor returned by a previous call. Pass it
            back to fetch the next slice; omit or empty to start over.
        per_page: Items per underlying GitHub page (max 100).
        max_items: Cap on items returned this call.

    Returns items with ``name``, ``full_name``, ``description``,
    ``default_branch``, ``visibility``, ``archived``, ``html_url``, and
    a ``next_page_token`` when more results are available.
    """
    if err := _validate_org(org):
        return err
    if repo_type not in {"all", "public", "private", "forks", "sources", "member"}:
        return {"ok": False, "error": f"Invalid repo_type: {repo_type!r}"}

    result = _paginated_slice(
        _api(f"/orgs/{quote(org, safe='')}/repos"),
        org=org,
        params={"type": repo_type},
        page_token=page_token,
        per_page=per_page,
        max_items=max_items,
        context=f"listing repos in {org}",
    )
    if not result.get("ok"):
        return result

    search_lower = (search or "").strip().lower()
    entries: list[dict[str, Any]] = []
    for r in result["items"]:
        name = (r.get("name") or "")
        if search_lower and search_lower not in name.lower():
            continue
        entries.append(
            {
                "name": name,
                "full_name": r.get("full_name"),
                "description": r.get("description"),
                "default_branch": r.get("default_branch"),
                "visibility": r.get("visibility"),
                "archived": r.get("archived"),
                "html_url": r.get("html_url"),
                "updated_at": r.get("updated_at"),
            }
        )
    return {
        "ok": True,
        "org": org,
        "repo_type": repo_type,
        "search": search or None,
        "count": len(entries),
        "next_page_token": result.get("next_page_token"),
        "repos": entries,
    }


@mcp.tool()
def search_repos(
    query: str,
    org: str = "",
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 50,
) -> dict[str, Any]:
    """Search repositories via ``/search/repositories``. **Read-only.**

    ``query`` accepts the full GitHub search-repositories syntax (e.g.
    ``kafka in:name language:java``). When ``org`` is given, ``org:<org>``
    is appended. Rate-limited to 30 requests/min server-side.
    """
    if not query or not query.strip():
        return {"ok": False, "error": "`query` is required."}
    if org:
        if err := _validate_org(org):
            return err
        q = f"{query.strip()} org:{org}"
    else:
        q = query.strip()

    state = _decode_cursor(page_token)
    page = int(state.get("page", 1))
    per_page = max(1, min(int(state.get("per_page") or per_page), 100))

    res = _get(
        _api("/search/repositories"),
        params={"q": q, "page": page, "per_page": per_page},
    )
    if res.status_code != 200:
        return _error_payload(res, org=org, context=f"search_repos q={q!r}")
    data = res.json() or {}
    items = data.get("items") or []
    entries = [
        {
            "name": it.get("name"),
            "full_name": it.get("full_name"),
            "description": it.get("description"),
            "default_branch": it.get("default_branch"),
            "html_url": it.get("html_url"),
            "score": it.get("score"),
            "updated_at": it.get("updated_at"),
        }
        for it in items
    ][:max_items]

    total = int(data.get("total_count") or 0)
    consumed = (page - 1) * per_page + len(entries)
    next_token = (
        _encode_cursor({"page": page + 1, "per_page": per_page})
        if consumed < total and len(items) == per_page
        else None
    )
    return {
        "ok": True,
        "query": q,
        "total_count": total,
        "count": len(entries),
        "next_page_token": next_token,
        "repos": entries,
    }


@mcp.tool()
def get_repo(org: str, repo: str) -> dict[str, Any]:
    """Fetch repository metadata for ``org/repo``. **Read-only.**

    Handy first call before any tree/file operation — returns the
    ``default_branch`` you may need as a ``ref`` fallback.
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    url = _api(f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}")
    res = _get(url)
    if res.status_code != 200:
        return _error_payload(res, org=org, context=f"get_repo {org}/{repo}")
    data = res.json()
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "full_name": data.get("full_name"),
        "description": data.get("description"),
        "default_branch": data.get("default_branch"),
        "visibility": data.get("visibility"),
        "archived": data.get("archived"),
        "html_url": data.get("html_url"),
        "topics": data.get("topics"),
        "language": data.get("language"),
        "open_issues_count": data.get("open_issues_count"),
        "pushed_at": data.get("pushed_at"),
        "updated_at": data.get("updated_at"),
    }


@mcp.tool()
def list_repo_branches(
    org: str,
    repo: str,
    search: str = "",
    page_token: str = "",
    per_page: int = 50,
    max_items: int = 100,
) -> dict[str, Any]:
    """List branches on ``org/repo``. **Read-only.**"""
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    result = _paginated_slice(
        _api(f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}/branches"),
        org=org,
        page_token=page_token,
        per_page=per_page,
        max_items=max_items,
        context=f"listing branches for {org}/{repo}",
    )
    if not result.get("ok"):
        return result
    search_lower = (search or "").strip().lower()
    entries: list[dict[str, Any]] = []
    for b in result["items"]:
        name = b.get("name") or ""
        if search_lower and search_lower not in name.lower():
            continue
        entries.append(
            {
                "name": name,
                "commit_sha": (b.get("commit") or {}).get("sha"),
                "protected": b.get("protected"),
            }
        )
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "search": search or None,
        "count": len(entries),
        "next_page_token": result.get("next_page_token"),
        "branches": entries,
    }


@mcp.tool()
def list_repo_tags(
    org: str,
    repo: str,
    search: str = "",
    page_token: str = "",
    per_page: int = 50,
    max_items: int = 100,
) -> dict[str, Any]:
    """List tags on ``org/repo``. **Read-only.**"""
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    result = _paginated_slice(
        _api(f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}/tags"),
        org=org,
        page_token=page_token,
        per_page=per_page,
        max_items=max_items,
        context=f"listing tags for {org}/{repo}",
    )
    if not result.get("ok"):
        return result
    search_lower = (search or "").strip().lower()
    entries: list[dict[str, Any]] = []
    for t in result["items"]:
        name = t.get("name") or ""
        if search_lower and search_lower not in name.lower():
            continue
        entries.append(
            {
                "name": name,
                "commit_sha": (t.get("commit") or {}).get("sha"),
            }
        )
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "search": search or None,
        "count": len(entries),
        "next_page_token": result.get("next_page_token"),
        "tags": entries,
    }


@mcp.tool()
def get_repo_file(
    org: str, repo: str, ref: str, file_path: str
) -> dict[str, Any]:
    """Fetch a single file at ``ref`` from ``org/repo``. **Read-only.**

    ``ref`` accepts a branch, tag, or full commit SHA. Refuses binary
    content (returns ``html_url`` instead of garbage).
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not ref or not file_path:
        return {"ok": False, "error": "`ref` and `file_path` are required."}
    url = _api(
        f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
        f"/contents/{quote(file_path.lstrip('/'), safe='/')}"
    )
    res = _get(url, params={"ref": ref})
    if res.status_code != 200:
        return _error_payload(
            res,
            org=org,
            context=f"get_repo_file {org}/{repo}@{ref}:{file_path}",
        )
    data = res.json()
    if isinstance(data, list):
        return {
            "ok": False,
            "error": (
                f"'{file_path}' is a directory. Use `list_repo_tree` instead."
            ),
        }
    if data.get("type") != "file":
        return {"ok": False, "error": f"Not a file (type={data.get('type')})."}
    try:
        content = base64.b64decode(data.get("content") or "").decode("utf-8")
    except UnicodeDecodeError:
        return {
            "ok": False,
            "org": org,
            "repo": repo,
            "ref": ref,
            "file_path": file_path,
            "size": data.get("size"),
            "html_url": data.get("html_url"),
            "error": (
                "File appears to be binary; refusing to decode as UTF-8. Use "
                "`html_url` in the GitHub UI to inspect it."
            ),
        }
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "ref": ref,
        "file_path": file_path,
        "size": data.get("size"),
        "html_url": data.get("html_url"),
        "content": content,
    }


@mcp.tool()
def list_repo_tree(
    org: str,
    repo: str,
    ref: str,
    path: str = "",
    recursive: bool = False,
    page_token: str = "",
    max_entries: int = 500,
) -> dict[str, Any]:
    """List files/directories at ``path`` and ``ref`` in ``org/repo``. **Read-only.**

    Args:
        org / repo / ref: target repo + branch/tag/SHA.
        path: Directory prefix (empty = repo root). When ``recursive`` is
            True, ``path`` is applied as a filter over the full tree.
        recursive: Walk the whole tree at ``ref`` via
            ``git/trees?recursive=1``. Very large trees are paginated
            locally via ``page_token``.
        page_token: Opaque cursor for the next slice (recursive mode only).
        max_entries: Items returned per call.
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not ref:
        return {"ok": False, "error": "`ref` is required."}

    if recursive:
        url = _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/git/trees/{quote(ref, safe='')}"
        )
        res = _get(url, params={"recursive": "1"})
        if res.status_code != 200:
            return _error_payload(
                res,
                org=org,
                context=f"list_repo_tree {org}/{repo}@{ref}",
            )
        data = res.json()
        raw_tree = data.get("tree") or []
        prefix = path.strip("/")
        filtered = [
            {
                "path": it.get("path"),
                "type": it.get("type"),
                "size": it.get("size"),
                "sha": it.get("sha"),
            }
            for it in raw_tree
            if not prefix
            or (it.get("path") == prefix)
            or ((it.get("path") or "").startswith(prefix + "/"))
        ]
        chunk, next_token = _slice_local(
            filtered, page_token=page_token, per_page=max_entries
        )
        return {
            "ok": True,
            "org": org,
            "repo": repo,
            "ref": ref,
            "path": path or None,
            "recursive": True,
            "truncated_by_github": bool(data.get("truncated")),
            "total_matched": len(filtered),
            "count": len(chunk),
            "next_page_token": next_token,
            "entries": chunk,
        }

    url = _api(
        f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
        f"/contents/{quote((path or '').lstrip('/'), safe='/')}"
    )
    res = _get(url, params={"ref": ref})
    if res.status_code != 200:
        return _error_payload(
            res,
            org=org,
            context=f"list_repo_tree {org}/{repo}@{ref}:{path}",
        )
    data = res.json()
    if isinstance(data, dict):
        return {
            "ok": False,
            "error": (
                f"'{path}' is a file, not a directory. Use `get_repo_file`."
            ),
        }
    entries = [
        {
            "path": it.get("path"),
            "name": it.get("name"),
            "type": it.get("type"),
            "size": it.get("size"),
            "sha": it.get("sha"),
        }
        for it in data[:max_entries]
    ]
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "ref": ref,
        "path": path or None,
        "recursive": False,
        "count": len(entries),
        "entries": entries,
    }


@mcp.tool()
def list_repo_pull_requests(
    org: str,
    repo: str,
    state: str = "open",
    search: str = "",
    base: str = "",
    head: str = "",
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 50,
) -> dict[str, Any]:
    """List PRs on ``org/repo``. **Read-only.**

    Args:
        state: ``open`` / ``closed`` / ``all``. Default ``open``.
        search: Optional case-insensitive substring filter on title.
        base: Filter by base branch (e.g. ``main``).
        head: Filter by head ``user:branch`` (see GitHub docs).
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if state not in {"open", "closed", "all"}:
        return {"ok": False, "error": f"Invalid state: {state!r}"}
    params: dict[str, Any] = {"state": state, "sort": "updated", "direction": "desc"}
    if base:
        params["base"] = base
    if head:
        params["head"] = head
    result = _paginated_slice(
        _api(f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}/pulls"),
        org=org,
        params=params,
        page_token=page_token,
        per_page=per_page,
        max_items=max_items,
        context=f"list_repo_pull_requests {org}/{repo}",
    )
    if not result.get("ok"):
        return result
    search_lower = (search or "").strip().lower()
    entries: list[dict[str, Any]] = []
    for pr in result["items"]:
        title = pr.get("title") or ""
        if search_lower and search_lower not in title.lower():
            continue
        entries.append(
            {
                "number": pr.get("number"),
                "title": title,
                "state": pr.get("state"),
                "draft": pr.get("draft"),
                "user": (pr.get("user") or {}).get("login"),
                "base": (pr.get("base") or {}).get("ref"),
                "head": (pr.get("head") or {}).get("ref"),
                "html_url": pr.get("html_url"),
                "created_at": pr.get("created_at"),
                "updated_at": pr.get("updated_at"),
                "merged_at": pr.get("merged_at"),
            }
        )
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "state": state,
        "search": search or None,
        "count": len(entries),
        "next_page_token": result.get("next_page_token"),
        "pull_requests": entries,
    }


@mcp.tool()
def get_pull_request(
    org: str,
    repo: str,
    number: int,
    include_diff: bool = False,
    max_diff_bytes: int = 200_000,
) -> dict[str, Any]:
    """Fetch details for PR ``number`` on ``org/repo``. **Read-only.**

    When ``include_diff`` is True, also fetches the unified diff via
    ``Accept: application/vnd.github.diff`` and truncates it to
    ``max_diff_bytes`` characters so the response stays LLM-friendly.
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not isinstance(number, int) or number < 1:
        return {"ok": False, "error": "`number` must be a positive integer."}
    base_path = (
        f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}/pulls/{number}"
    )
    res = _get(_api(base_path))
    if res.status_code != 200:
        return _error_payload(
            res, org=org, context=f"get_pull_request {org}/{repo}#{number}"
        )
    pr = res.json()
    out: dict[str, Any] = {
        "ok": True,
        "org": org,
        "repo": repo,
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "draft": pr.get("draft"),
        "merged": pr.get("merged"),
        "merged_at": pr.get("merged_at"),
        "merged_by": (pr.get("merged_by") or {}).get("login"),
        "user": (pr.get("user") or {}).get("login"),
        "base": {
            "ref": (pr.get("base") or {}).get("ref"),
            "sha": (pr.get("base") or {}).get("sha"),
        },
        "head": {
            "ref": (pr.get("head") or {}).get("ref"),
            "sha": (pr.get("head") or {}).get("sha"),
        },
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "html_url": pr.get("html_url"),
        "body": pr.get("body"),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
    }
    if include_diff:
        dres = _get(_api(base_path), accept="application/vnd.github.diff")
        if dres.status_code == 200:
            diff = dres.text or ""
            out["diff"] = diff[:max_diff_bytes]
            out["diff_truncated"] = len(diff) > max_diff_bytes
            out["diff_bytes"] = len(diff)
        else:
            out["diff_error"] = _error_payload(
                dres, org=org, context=f"pr diff {org}/{repo}#{number}"
            )
    return out


@mcp.tool()
def list_repo_issues(
    org: str,
    repo: str,
    state: str = "open",
    labels: str = "",
    search: str = "",
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 50,
) -> dict[str, Any]:
    """List issues on ``org/repo`` (PRs excluded). **Read-only.**

    Args:
        state: ``open`` / ``closed`` / ``all``. Default ``open``.
        labels: Comma-separated label filter (e.g. ``bug,p1``).
        search: Optional case-insensitive substring filter on title.
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if state not in {"open", "closed", "all"}:
        return {"ok": False, "error": f"Invalid state: {state!r}"}
    params: dict[str, Any] = {"state": state, "sort": "updated", "direction": "desc"}
    if labels:
        params["labels"] = labels
    result = _paginated_slice(
        _api(f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}/issues"),
        org=org,
        params=params,
        page_token=page_token,
        per_page=per_page,
        max_items=max_items,
        context=f"list_repo_issues {org}/{repo}",
    )
    if not result.get("ok"):
        return result
    search_lower = (search or "").strip().lower()
    entries: list[dict[str, Any]] = []
    for it in result["items"]:
        if it.get("pull_request"):
            continue  # /issues returns PRs too; filter them out.
        title = it.get("title") or ""
        if search_lower and search_lower not in title.lower():
            continue
        entries.append(
            {
                "number": it.get("number"),
                "title": title,
                "state": it.get("state"),
                "user": (it.get("user") or {}).get("login"),
                "labels": [
                    (lbl or {}).get("name") for lbl in (it.get("labels") or [])
                ],
                "comments": it.get("comments"),
                "html_url": it.get("html_url"),
                "created_at": it.get("created_at"),
                "updated_at": it.get("updated_at"),
                "closed_at": it.get("closed_at"),
            }
        )
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "state": state,
        "labels": labels or None,
        "search": search or None,
        "count": len(entries),
        "next_page_token": result.get("next_page_token"),
        "issues": entries,
    }


@mcp.tool()
def get_issue(org: str, repo: str, number: int) -> dict[str, Any]:
    """Fetch a single issue on ``org/repo``. **Read-only.**"""
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not isinstance(number, int) or number < 1:
        return {"ok": False, "error": "`number` must be a positive integer."}
    res = _get(
        _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/issues/{number}"
        )
    )
    if res.status_code != 200:
        return _error_payload(
            res, org=org, context=f"get_issue {org}/{repo}#{number}"
        )
    it = res.json()
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "number": it.get("number"),
        "title": it.get("title"),
        "state": it.get("state"),
        "user": (it.get("user") or {}).get("login"),
        "labels": [(lbl or {}).get("name") for lbl in (it.get("labels") or [])],
        "assignees": [
            (a or {}).get("login") for a in (it.get("assignees") or [])
        ],
        "comments": it.get("comments"),
        "html_url": it.get("html_url"),
        "body": it.get("body"),
        "created_at": it.get("created_at"),
        "updated_at": it.get("updated_at"),
        "closed_at": it.get("closed_at"),
    }


@mcp.tool()
def compare_refs(
    org: str,
    repo: str,
    base: str,
    head: str,
    include_diff: bool = False,
    max_diff_bytes: int = 200_000,
) -> dict[str, Any]:
    """Compare two refs (branch / tag / SHA) via ``/compare``. **Read-only.**

    Returns the aggregate ahead/behind counts, the commit list, and each
    changed file's status/additions/deletions. Set ``include_diff=True``
    to also fetch the unified diff (truncated to ``max_diff_bytes``).
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not base or not head:
        return {"ok": False, "error": "`base` and `head` are required."}
    path = (
        f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
        f"/compare/{quote(base, safe='')}...{quote(head, safe='')}"
    )
    res = _get(_api(path))
    if res.status_code != 200:
        return _error_payload(
            res,
            org=org,
            context=f"compare_refs {org}/{repo} {base}...{head}",
        )
    data = res.json()
    files = [
        {
            "filename": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
            "changes": f.get("changes"),
            "blob_url": f.get("blob_url"),
        }
        for f in (data.get("files") or [])
    ]
    commits = [
        {
            "sha": c.get("sha"),
            "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0],
            "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
            "html_url": c.get("html_url"),
        }
        for c in (data.get("commits") or [])
    ]
    out: dict[str, Any] = {
        "ok": True,
        "org": org,
        "repo": repo,
        "base": base,
        "head": head,
        "status": data.get("status"),
        "ahead_by": data.get("ahead_by"),
        "behind_by": data.get("behind_by"),
        "total_commits": data.get("total_commits"),
        "html_url": data.get("html_url"),
        "commits": commits,
        "files": files,
    }
    if include_diff:
        dres = _get(_api(path), accept="application/vnd.github.diff")
        if dres.status_code == 200:
            diff = dres.text or ""
            out["diff"] = diff[:max_diff_bytes]
            out["diff_truncated"] = len(diff) > max_diff_bytes
            out["diff_bytes"] = len(diff)
        else:
            out["diff_error"] = _error_payload(
                dres,
                org=org,
                context=f"compare diff {org}/{repo} {base}...{head}",
            )
    return out


@mcp.tool()
def search_repo_code(
    org: str,
    repo: str,
    query: str,
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 30,
) -> dict[str, Any]:
    """Search code inside ``org/repo`` via ``/search/code``. **Read-only.**

    Same GitHub-side limitation as ``search_component_code``: matches
    come from the default branch only. Follow up with ``get_repo_file``
    at a specific tag/SHA if you need the version-scoped view.
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not query or not query.strip():
        return {"ok": False, "error": "`query` is required."}

    state = _decode_cursor(page_token)
    page = int(state.get("page", 1))
    per_page = max(1, min(int(state.get("per_page") or per_page), 100))

    q = f"{query.strip()} repo:{org}/{repo}"
    res = _get(
        _api("/search/code"),
        params={"q": q, "page": page, "per_page": per_page},
    )
    if res.status_code != 200:
        return _error_payload(
            res, org=org, context=f"search_repo_code {org}/{repo} q={query!r}"
        )
    data = res.json() or {}
    items = data.get("items") or []
    matches = [
        {
            "file_path": it.get("path"),
            "html_url": it.get("html_url"),
            "score": it.get("score"),
        }
        for it in items
    ][:max_items]
    total = int(data.get("total_count") or 0)
    consumed = (page - 1) * per_page + len(matches)
    next_token = (
        _encode_cursor({"page": page + 1, "per_page": per_page})
        if consumed < total and len(items) == per_page
        else None
    )
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "query": query,
        "total_count": total,
        "count": len(matches),
        "next_page_token": next_token,
        "matches": matches,
        "note": (
            "GitHub code search only indexes the default branch. To view a "
            "match at a specific tag/SHA, follow up with `get_repo_file`."
        ),
    }


# ── GitHub Actions / Checks tools ─────────────────────────────────────────
# Everything below is read-only against the Actions + Checks APIs.
# Token needs `actions:read` and `checks:read` in addition to
# `contents:read`. Same SSO handling as the rest of the server.

# Check-run conclusions that count as "not green" for the failure summaries.
# `neutral` and `skipped` are treated as passing — matches how the GitHub
# UI shows them in the PR merge-status rollup.
_CHECK_FAIL_CONCLUSIONS = frozenset({
    "failure", "timed_out", "action_required", "cancelled", "startup_failure",
})

_VALID_RUN_STATUSES = frozenset({
    "queued", "in_progress", "completed", "waiting", "requested", "pending",
    "action_required", "cancelled", "failure", "neutral", "skipped", "stale",
    "success", "timed_out",
})


def _summarize_check_run(cr: dict[str, Any]) -> dict[str, Any]:
    """Trim a check-run object down to the fields the AI actually needs."""
    app = cr.get("app") or {}
    return {
        "id": cr.get("id"),
        "name": cr.get("name"),
        "status": cr.get("status"),
        "conclusion": cr.get("conclusion"),
        "started_at": cr.get("started_at"),
        "completed_at": cr.get("completed_at"),
        "head_sha": cr.get("head_sha"),
        "html_url": cr.get("html_url"),
        "details_url": cr.get("details_url"),
        "app": {"slug": app.get("slug"), "name": app.get("name")} if app else None,
        "output": {
            "title": (cr.get("output") or {}).get("title"),
            "summary": (cr.get("output") or {}).get("summary"),
        } if cr.get("output") else None,
    }


def _summarize_workflow_run(wr: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": wr.get("id"),
        "name": wr.get("name"),
        "display_title": wr.get("display_title"),
        "workflow_id": wr.get("workflow_id"),
        "path": wr.get("path"),
        "event": wr.get("event"),
        "status": wr.get("status"),
        "conclusion": wr.get("conclusion"),
        "run_number": wr.get("run_number"),
        "run_attempt": wr.get("run_attempt"),
        "head_branch": wr.get("head_branch"),
        "head_sha": wr.get("head_sha"),
        "actor": (wr.get("actor") or {}).get("login"),
        "run_started_at": wr.get("run_started_at"),
        "created_at": wr.get("created_at"),
        "updated_at": wr.get("updated_at"),
        "html_url": wr.get("html_url"),
    }


def _summarize_job(job: dict[str, Any], include_steps: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": job.get("id"),
        "run_id": job.get("run_id"),
        "run_attempt": job.get("run_attempt"),
        "name": job.get("name"),
        "workflow_name": job.get("workflow_name"),
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "head_sha": job.get("head_sha"),
        "html_url": job.get("html_url"),
        "labels": job.get("labels"),
        "runner_name": job.get("runner_name"),
    }
    if include_steps:
        out["steps"] = [
            {
                "name": s.get("name"),
                "status": s.get("status"),
                "conclusion": s.get("conclusion"),
                "number": s.get("number"),
                "started_at": s.get("started_at"),
                "completed_at": s.get("completed_at"),
            }
            for s in (job.get("steps") or [])
        ]
    return out


def _fetch_job_logs_text(
    org: str, repo: str, job_id: int
) -> tuple[requests.Response, Optional[str]]:
    """Follow GH's log redirect manually so we don't leak the bearer token to
    the storage backend (S3 / Azure blob) and can bound the fetch timeout.

    Returns ``(response_used_for_status, text_or_None)``.
    """
    url = _api(
        f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
        f"/actions/jobs/{job_id}/logs"
    )
    try:
        res = requests.get(
            url,
            headers=_headers(),
            timeout=30,
            verify=_verify_ssl(),
            allow_redirects=False,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise RuntimeError(
            f"GitHub API {_base_url()} is not reachable ({exc.__class__.__name__})."
        ) from exc
    if res.status_code in (301, 302, 303, 307, 308):
        location = res.headers.get("Location") or res.headers.get("location")
        if not location:
            return res, None
        try:
            # Fresh request — no Authorization header — pre-signed URL includes its own auth.
            res2 = requests.get(
                location, timeout=60, verify=_verify_ssl(), allow_redirects=True
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            raise RuntimeError(
                f"Log storage backend not reachable ({exc.__class__.__name__})."
            ) from exc
        return res2, (res2.text if res2.status_code == 200 else None)
    if res.status_code == 200:
        return res, res.text
    return res, None


@mcp.tool()
def get_pr_checks(
    org: str,
    repo: str,
    number: int,
    include_success: bool = False,
) -> dict[str, Any]:
    """Report Checks/Actions status for PR ``number``. **Read-only.**

    One-shot summary: resolves the PR's head SHA, then combines the
    check-runs API and the combined-commit-status API into a single
    rollup so an AI can answer "which checks failed on this PR?"
    without chaining calls.

    Returns ``rollup`` counts, plus a ``failed`` list of check runs whose
    conclusion is in ``failure / timed_out / action_required / cancelled /
    startup_failure``. Set ``include_success=True`` to also return the
    passing runs under ``passed``.
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not isinstance(number, int) or number < 1:
        return {"ok": False, "error": "`number` must be a positive integer."}

    pr_path = (
        f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}/pulls/{number}"
    )
    pr_res = _get(_api(pr_path))
    if pr_res.status_code != 200:
        return _error_payload(
            pr_res, org=org, context=f"get_pr_checks {org}/{repo}#{number}"
        )
    pr = pr_res.json() or {}
    head_sha = (pr.get("head") or {}).get("sha")
    if not head_sha:
        return {
            "ok": False,
            "error": f"PR #{number} has no head SHA (deleted branch?).",
        }

    check_runs = _paginated_slice_wrapped(
        _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/commits/{head_sha}/check-runs"
        ),
        org=org,
        wrapper_key="check_runs",
        per_page=100,
        max_items=200,
        context=f"pr check-runs {org}/{repo}#{number}",
    )
    if not check_runs.get("ok"):
        return check_runs

    runs = [_summarize_check_run(cr) for cr in check_runs["items"]]
    rollup: dict[str, int] = {}
    failed: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    in_progress: list[dict[str, Any]] = []
    for r in runs:
        status = r.get("status") or "unknown"
        concl = r.get("conclusion")
        key = concl if status == "completed" else status
        rollup[key] = rollup.get(key, 0) + 1
        if status != "completed":
            in_progress.append(r)
        elif concl in _CHECK_FAIL_CONCLUSIONS:
            failed.append(r)
        else:
            passed.append(r)

    status_res = _get(
        _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/commits/{head_sha}/status"
        )
    )
    combined: dict[str, Any] = {}
    if status_res.status_code == 200:
        cs = status_res.json() or {}
        combined = {
            "state": cs.get("state"),
            "total_count": cs.get("total_count"),
            "statuses": [
                {
                    "context": s.get("context"),
                    "state": s.get("state"),
                    "description": s.get("description"),
                    "target_url": s.get("target_url"),
                    "created_at": s.get("created_at"),
                    "updated_at": s.get("updated_at"),
                }
                for s in (cs.get("statuses") or [])
            ],
        }

    out: dict[str, Any] = {
        "ok": True,
        "org": org,
        "repo": repo,
        "number": number,
        "head_sha": head_sha,
        "head_ref": (pr.get("head") or {}).get("ref"),
        "html_url": pr.get("html_url"),
        "check_runs_total": check_runs.get("total_count"),
        "rollup": rollup,
        "failed_count": len(failed),
        "in_progress_count": len(in_progress),
        "passed_count": len(passed),
        "failed": failed,
        "in_progress": in_progress,
        "combined_status": combined,
        "next_page_token": check_runs.get("next_page_token"),
    }
    if include_success:
        out["passed"] = passed
    return out


@mcp.tool()
def list_check_runs(
    org: str,
    repo: str,
    ref: str,
    status: str = "",
    check_name: str = "",
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 100,
) -> dict[str, Any]:
    """List check runs for a commit ``ref`` (SHA, branch, or tag). **Read-only.**

    Optional filters: ``status`` (``queued`` / ``in_progress`` /
    ``completed``) and ``check_name`` (exact match against the check-run
    name). Each item is the same trimmed shape used by ``get_pr_checks``.
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not ref or not ref.strip():
        return {"ok": False, "error": "`ref` is required."}
    if status and status not in ("queued", "in_progress", "completed"):
        return {
            "ok": False,
            "error": "`status` must be one of: queued, in_progress, completed.",
        }

    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    if check_name:
        params["check_name"] = check_name

    page = _paginated_slice_wrapped(
        _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/commits/{quote(ref, safe='')}/check-runs"
        ),
        org=org,
        wrapper_key="check_runs",
        params=params,
        page_token=page_token,
        per_page=per_page,
        max_items=max_items,
        context=f"list_check_runs {org}/{repo} ref={ref}",
    )
    if not page.get("ok"):
        return page
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "ref": ref,
        "total_count": page.get("total_count"),
        "count": page.get("count"),
        "next_page_token": page.get("next_page_token"),
        "check_runs": [_summarize_check_run(cr) for cr in page["items"]],
    }


@mcp.tool()
def list_repo_workflows(
    org: str,
    repo: str,
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 100,
) -> dict[str, Any]:
    """List Actions workflows defined in ``.github/workflows``. **Read-only.**"""
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    page = _paginated_slice_wrapped(
        _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/actions/workflows"
        ),
        org=org,
        wrapper_key="workflows",
        page_token=page_token,
        per_page=per_page,
        max_items=max_items,
        context=f"list_repo_workflows {org}/{repo}",
    )
    if not page.get("ok"):
        return page
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "total_count": page.get("total_count"),
        "count": page.get("count"),
        "next_page_token": page.get("next_page_token"),
        "workflows": [
            {
                "id": w.get("id"),
                "name": w.get("name"),
                "path": w.get("path"),
                "state": w.get("state"),
                "html_url": w.get("html_url"),
                "badge_url": w.get("badge_url"),
                "created_at": w.get("created_at"),
                "updated_at": w.get("updated_at"),
            }
            for w in page["items"]
        ],
    }


@mcp.tool()
def list_workflow_runs(
    org: str,
    repo: str,
    workflow: str = "",
    branch: str = "",
    event: str = "",
    status: str = "",
    actor: str = "",
    head_sha: str = "",
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 100,
) -> dict[str, Any]:
    """List Actions workflow runs for ``org/repo``. **Read-only.**

    All filters are optional and passed straight through to GitHub:
      * ``workflow``: workflow ID (number-as-string) OR file name
        (e.g. ``ci.yml``). When set, hits the per-workflow endpoint.
      * ``branch``, ``event`` (``push`` / ``pull_request`` / ``schedule``
        / ``workflow_dispatch`` / ...), ``actor`` (login), ``head_sha``.
      * ``status``: run status/conclusion — ``queued``, ``in_progress``,
        ``completed``, ``success``, ``failure``, ``cancelled``,
        ``skipped``, ``timed_out``, ``action_required``, ``neutral``,
        ``stale``, ``requested``, ``waiting``, ``pending``.
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if status and status not in _VALID_RUN_STATUSES:
        return {
            "ok": False,
            "error": (
                f"Invalid status {status!r}. Allowed: "
                + ", ".join(sorted(_VALID_RUN_STATUSES))
            ),
        }

    params: dict[str, Any] = {}
    if branch:
        params["branch"] = branch
    if event:
        params["event"] = event
    if status:
        params["status"] = status
    if actor:
        params["actor"] = actor
    if head_sha:
        params["head_sha"] = head_sha

    if workflow:
        url = _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/actions/workflows/{quote(workflow, safe='')}/runs"
        )
        ctx = f"list_workflow_runs {org}/{repo} workflow={workflow}"
    else:
        url = _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/actions/runs"
        )
        ctx = f"list_workflow_runs {org}/{repo}"

    page = _paginated_slice_wrapped(
        url,
        org=org,
        wrapper_key="workflow_runs",
        params=params,
        page_token=page_token,
        per_page=per_page,
        max_items=max_items,
        context=ctx,
    )
    if not page.get("ok"):
        return page
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "workflow": workflow or None,
        "filters": {k: v for k, v in params.items() if k != "per_page"},
        "total_count": page.get("total_count"),
        "count": page.get("count"),
        "next_page_token": page.get("next_page_token"),
        "workflow_runs": [_summarize_workflow_run(wr) for wr in page["items"]],
    }


@mcp.tool()
def get_workflow_run(
    org: str, repo: str, run_id: int
) -> dict[str, Any]:
    """Fetch details for a single Actions workflow run. **Read-only.**"""
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not isinstance(run_id, int) or run_id < 1:
        return {"ok": False, "error": "`run_id` must be a positive integer."}
    res = _get(
        _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/actions/runs/{run_id}"
        )
    )
    if res.status_code != 200:
        return _error_payload(
            res, org=org, context=f"get_workflow_run {org}/{repo}#{run_id}"
        )
    wr = res.json() or {}
    out = _summarize_workflow_run(wr)
    out.update({
        "ok": True,
        "org": org,
        "repo": repo,
        "referenced_workflows": [
            {
                "path": w.get("path"),
                "sha": w.get("sha"),
                "ref": w.get("ref"),
            }
            for w in (wr.get("referenced_workflows") or [])
        ],
        "pull_requests": [
            {"number": p.get("number"), "url": p.get("url")}
            for p in (wr.get("pull_requests") or [])
        ],
        "triggering_actor": (wr.get("triggering_actor") or {}).get("login"),
        "logs_url": wr.get("logs_url"),
        "jobs_url": wr.get("jobs_url"),
    })
    return out


@mcp.tool()
def list_workflow_run_jobs(
    org: str,
    repo: str,
    run_id: int,
    filter: str = "latest",
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 100,
) -> dict[str, Any]:
    """List jobs for a workflow run. **Read-only.**

    ``filter`` is either ``latest`` (default — the most recent attempt of
    each job) or ``all`` (every attempt, useful when a job was re-run).
    Each item includes ``steps`` so you can see which step failed.
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not isinstance(run_id, int) or run_id < 1:
        return {"ok": False, "error": "`run_id` must be a positive integer."}
    if filter not in ("latest", "all"):
        return {"ok": False, "error": "`filter` must be 'latest' or 'all'."}

    page = _paginated_slice_wrapped(
        _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/actions/runs/{run_id}/jobs"
        ),
        org=org,
        wrapper_key="jobs",
        params={"filter": filter},
        page_token=page_token,
        per_page=per_page,
        max_items=max_items,
        context=f"list_workflow_run_jobs {org}/{repo} run={run_id}",
    )
    if not page.get("ok"):
        return page
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "run_id": run_id,
        "filter": filter,
        "total_count": page.get("total_count"),
        "count": page.get("count"),
        "next_page_token": page.get("next_page_token"),
        "jobs": [_summarize_job(j, include_steps=True) for j in page["items"]],
    }


@mcp.tool()
def get_workflow_job(
    org: str, repo: str, job_id: int
) -> dict[str, Any]:
    """Fetch a single workflow-run job (with its steps). **Read-only.**"""
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not isinstance(job_id, int) or job_id < 1:
        return {"ok": False, "error": "`job_id` must be a positive integer."}
    res = _get(
        _api(
            f"/repos/{quote(org, safe='')}/{quote(repo, safe='')}"
            f"/actions/jobs/{job_id}"
        )
    )
    if res.status_code != 200:
        return _error_payload(
            res, org=org, context=f"get_workflow_job {org}/{repo}#{job_id}"
        )
    job = res.json() or {}
    out = _summarize_job(job, include_steps=True)
    out.update({"ok": True, "org": org, "repo": repo})
    return out


@mcp.tool()
def get_workflow_job_logs(
    org: str,
    repo: str,
    job_id: int,
    max_bytes: int = 200_000,
    tail: bool = True,
) -> dict[str, Any]:
    """Fetch the plain-text logs for a workflow-run job. **Read-only.**

    Logs can be huge; ``max_bytes`` caps how much is returned (default
    200 KB). When ``tail`` is True (default), the LAST ``max_bytes`` are
    returned — CI failures almost always live at the end of the log, so
    this is usually what you want. Set ``tail=False`` to keep the head
    instead.

    Follows GitHub's log-URL redirect internally; the bearer token is
    stripped before hitting the storage backend so it never leaks
    outside ``api.github.com``.
    """
    if err := _validate_org(org):
        return err
    if err := _validate_repo(repo):
        return err
    if not isinstance(job_id, int) or job_id < 1:
        return {"ok": False, "error": "`job_id` must be a positive integer."}
    max_bytes = max(1_000, min(int(max_bytes), 5_000_000))

    res, text = _fetch_job_logs_text(org, repo, job_id)
    if text is None:
        return _error_payload(
            res, org=org, context=f"get_workflow_job_logs {org}/{repo}#{job_id}"
        )
    total = len(text)
    truncated = total > max_bytes
    if truncated:
        body = text[-max_bytes:] if tail else text[:max_bytes]
    else:
        body = text
    return {
        "ok": True,
        "org": org,
        "repo": repo,
        "job_id": job_id,
        "total_bytes": total,
        "returned_bytes": len(body),
        "truncated": truncated,
        "tail": tail,
        "logs": body,
    }


# ═════════════════════════════════════════════════════════════════════════
# Freestyle read-only GET (tier 5)
# ═════════════════════════════════════════════════════════════════════════
#
# Escape hatch for GitHub REST endpoints we haven't wrapped in a
# dedicated tool. Only `_get()` (GITHUB_TOKEN, GET-only) is ever called
# from here — the write path is completely separate. `path` is
# validated against a positive prefix allow-list of read-only paths.

_GET_ALLOWED_PREFIXES: tuple[str, ...] = (
    "/user",             # /user, /user/orgs, /user/repos, /user/packages, /user/emails, ...
    "/users/",           # per-user reads
    "/orgs/",            # org metadata / members / teams / repos / secrets (read) / hooks (read)
    "/repos/",           # everything under a repo
    "/search/",          # /search/{repositories|code|issues|users|commits|topics|labels}
    "/gists/",
    "/gists",
    "/notifications",    # GET-only; PUT/DELETE never issued here
    "/rate_limit",
    "/meta",
    "/licenses",
    "/gitignore/",
    "/emojis",
    "/octocat",
    "/versions",         # GHES
    "/zen",
)

# Response wrappers we recognize so pagination can walk them. Order = priority.
_KNOWN_WRAPPER_KEYS: tuple[str, ...] = (
    "items",             # /search/*
    "workflow_runs",
    "check_runs",
    "check_suites",
    "jobs",
    "workflows",
    "artifacts",
    "runners",
    "self_hosted_runners",
    "secrets",
    "variables",
    "environments",
    "deployments",
    "installations",
    "repositories",
)


def _is_get_path_allowed(path: str) -> bool:
    if not path or not path.startswith("/") or ".." in path:
        return False
    for prefix in _GET_ALLOWED_PREFIXES:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
        elif path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _extract_org_from_path(path: str) -> str:
    m = re.match(r"^/(?:orgs|repos)/([A-Za-z0-9._-]+)", path)
    return m.group(1) if m else ""


def _detect_wrapper_key(body: dict[str, Any]) -> str:
    for k in _KNOWN_WRAPPER_KEYS:
        if isinstance(body.get(k), list):
            return k
    return ""


def _parse_query_params_json(
    raw: str,
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    """Return (params_dict, error_payload_or_None). ``page`` / ``per_page``
    are reserved — the tool manages those via its own args."""
    if not raw or not raw.strip():
        return {}, None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return {}, {
            "ok": False,
            "error": f"query_params_json is not valid JSON: {exc}",
        }
    if not isinstance(data, dict):
        return {}, {
            "ok": False,
            "error": "query_params_json must decode to a JSON object.",
        }
    for reserved in ("page", "per_page"):
        if reserved in data:
            return {}, {
                "ok": False,
                "error": (
                    f"Do not pass '{reserved}' in query_params_json; use the "
                    "tool's `page_token` / `per_page` arguments instead."
                ),
            }
    return data, None


@mcp.tool()
def github_get(
    path: str,
    query_params_json: str = "",
    list_key: str = "",
    page_token: str = "",
    per_page: int = 30,
    max_items: int = 100,
    org: str = "",
) -> dict[str, Any]:
    """Freestyle **read-only** GET against the GitHub REST API.

    Use this for endpoints without a dedicated tool — org members, org
    teams, org secrets metadata, notifications, gists, users,
    ``/user/packages``, ``/rate_limit``, etc. Uses ``GITHUB_TOKEN``.
    For owner-scoped writes (your own repos, ``/user/…``, ``/gists*``)
    use ``github_write``.

    Args:
        path: API path starting with ``/`` (e.g. ``/orgs/myorgname2/members``).
            Must NOT contain a query string — pass query params via
            ``query_params_json``. Allow-list: ``/user``, ``/users/``,
            ``/orgs/``, ``/repos/``, ``/search/``, ``/gists``,
            ``/notifications``, ``/rate_limit``, ``/meta``, ``/licenses``,
            ``/gitignore/``, ``/emojis``, ``/octocat``, ``/versions``,
            ``/zen``.
        query_params_json: Optional JSON object of query params
            (``'{"role": "admin"}'``). ``page`` / ``per_page`` reserved.
        list_key: Optional explicit wrapper key when the response is a
            dict-wrapped list. Auto-detected for common shapes.
        page_token: Opaque cursor from a prior call.
        per_page: GitHub per-page size (max 100). Default 30.
        max_items: Cap on items returned this call (max 1000). Default
            100. Prevents blowing past the AI-host response cap on
            endpoints with thousands of rows.
        org: Optional org hint for SSO errors. Auto-extracted from
            ``/orgs/{org}/…`` and ``/repos/{org}/…``.

    Response modes:
      * ``mode="list"`` — endpoint returned a JSON array. Paged up to
        ``max_items`` into ``items`` with ``next_page_token``.
      * ``mode="wrapped"`` — endpoint returned
        ``{"total_count": N, "<list_key>": [...]}``. Same pagination;
        ``total_count`` included.
      * ``mode="single"`` — endpoint returned a single JSON object.
        Returned under ``data``.
      * ``mode="raw"`` — endpoint returned a scalar / non-JSON. Returned
        under ``data``.

    Examples::

        github_get(path="/orgs/myorgname2/members")
        github_get(path="/orgs/myorgname2/members",
                   query_params_json='{"role":"admin"}')
        github_get(path="/orgs/myorgname2/public_members")
        github_get(path="/orgs/myorgname2/teams")
        github_get(path="/orgs/myorgname2/teams/devops/members")
        github_get(path="/user/packages",
                   query_params_json='{"package_type":"container"}')
        github_get(path="/user/packages/container/lvx-gitlab")
        github_get(path="/rate_limit")
    """
    if "?" in path or "#" in path:
        return {
            "ok": False,
            "error": (
                "Do not embed a query string in `path`. Pass query params "
                "via `query_params_json`."
            ),
        }
    if not _is_get_path_allowed(path):
        return {
            "ok": False,
            "error": (
                f"Path {path!r} is not on the read-only GET allow-list. "
                "Allowed prefixes: " + ", ".join(_GET_ALLOWED_PREFIXES)
            ),
        }
    params, param_err = _parse_query_params_json(query_params_json)
    if param_err:
        return param_err

    effective_org = org or _extract_org_from_path(path)
    per_page = max(1, min(int(per_page), 100))
    max_items = max(1, min(int(max_items), 1000))

    state = _decode_cursor(page_token)
    page = int(state.get("page", 1))
    if state.get("per_page"):
        per_page = max(1, min(int(state["per_page"]), 100))

    query = dict(params)
    query["per_page"] = per_page
    query["page"] = page

    res = _get(_api(path), params=query)
    if res.status_code != 200:
        return _error_payload(
            res, org=effective_org, context=f"github_get {path}"
        )

    try:
        body = res.json()
    except ValueError:
        return {
            "ok": True,
            "path": path,
            "mode": "raw",
            "content_type": res.headers.get("Content-Type"),
            "data": res.text,
        }

    # --- Mode 1: bare JSON list -----------------------------------------
    if isinstance(body, list):
        collected: list[Any] = list(body)
        last_full = len(body) == per_page
        page += 1
        while last_full and len(collected) < max_items:
            query["page"] = page
            r = _get(_api(path), params=query)
            if r.status_code != 200:
                return _error_payload(
                    r, org=effective_org, context=f"github_get {path}"
                )
            try:
                chunk = r.json()
            except ValueError:
                break
            if not isinstance(chunk, list) or not chunk:
                last_full = False
                break
            collected.extend(chunk)
            last_full = len(chunk) == per_page
            page += 1
        truncated = collected[:max_items]
        next_token = (
            _encode_cursor({"page": page, "per_page": per_page})
            if last_full and len(collected) >= max_items
            else None
        )
        return {
            "ok": True,
            "path": path,
            "mode": "list",
            "count": len(truncated),
            "next_page_token": next_token,
            "items": truncated,
        }

    # --- Mode 2: wrapped list -------------------------------------------
    if isinstance(body, dict):
        key = list_key or _detect_wrapper_key(body)
        if key and isinstance(body.get(key), list):
            items = list(body.get(key) or [])
            total_count = body.get("total_count")
            last_full = len(items) == per_page
            page += 1
            while last_full and len(items) < max_items:
                query["page"] = page
                r = _get(_api(path), params=query)
                if r.status_code != 200:
                    return _error_payload(
                        r, org=effective_org, context=f"github_get {path}"
                    )
                try:
                    chunk_body = r.json() or {}
                except ValueError:
                    break
                chunk = chunk_body.get(key) or []
                if not chunk:
                    last_full = False
                    break
                items.extend(chunk)
                last_full = len(chunk) == per_page
                page += 1
            truncated = items[:max_items]
            next_token = (
                _encode_cursor({"page": page, "per_page": per_page})
                if last_full and len(items) >= max_items
                else None
            )
            return {
                "ok": True,
                "path": path,
                "mode": "wrapped",
                "list_key": key,
                "total_count": total_count,
                "count": len(truncated),
                "next_page_token": next_token,
                "items": truncated,
            }
        # --- Mode 3: single resource dict -------------------------------
        return {"ok": True, "path": path, "mode": "single", "data": body}

    # --- Mode 4: scalar / other -----------------------------------------
    return {"ok": True, "path": path, "mode": "raw", "data": body}


# ═════════════════════════════════════════════════════════════════════════
# Narrow, confirm-gated writes (tier 4)
# ═════════════════════════════════════════════════════════════════════════
#
# One `GITHUB_TOKEN` for everything. Approval is the only gate:
#
#   Confirm gate: every write tool takes `confirm: bool = False`.
#   `confirm=False` returns a preview (method + endpoint + payload +
#   plain-english effect). Only `confirm=True` actually hits GitHub.
#   Two-call pattern makes prompt-injected writes much harder. Personal
#   repos and org repos are treated the same — whether the write goes
#   through depends on the scopes the token actually has.
#
# Scope covers repo-owner day-to-day operations: collaborator invite /
# cancel / remove and repo visibility flips.

_ALLOWED_PERMISSIONS = frozenset(
    {"pull", "triage", "push", "maintain", "admin"}
)
_ALLOWED_REPO_VISIBILITIES = frozenset({"public", "private", "internal"})


def _write_http(
    method: str,
    url: str,
    json_body: Optional[dict[str, Any]] = None,
) -> requests.Response:
    """PUT / PATCH / DELETE against the GitHub API using ``GITHUB_TOKEN``.

    Kept separate from ``_get()`` so write call sites are easy to audit
    (grep for ``_write_http``). Refuses GET on purpose — every read goes
    through ``_get()``.
    """
    if method.upper() == "GET":
        raise RuntimeError("Use _get() for reads. _write_http is for writes only.")
    try:
        return requests.request(
            method,
            url,
            headers=_headers(),
            json=json_body,
            timeout=30,
            verify=_verify_ssl(),
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise RuntimeError(
            f"GitHub API {_base_url()} is not reachable ({exc.__class__.__name__})."
        ) from exc


def _validate_owner(owner: str) -> Optional[dict[str, Any]]:
    """Owner = user login OR org login. GitHub uses the same name rules for both."""
    if not owner or not _ORG_NAME_RE.match(owner):
        return {"ok": False, "error": f"Invalid owner name: {owner!r}"}
    return None


def _validate_username(username: str) -> Optional[dict[str, Any]]:
    if not username or not _ORG_NAME_RE.match(username):
        return {"ok": False, "error": f"Invalid username: {username!r}"}
    return None


def _preview(
    action: str,
    method: str,
    endpoint: str,
    payload: Optional[dict[str, Any]],
    effect: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "confirmed": False,
        "preview": {
            "action": action,
            "method": method,
            "endpoint": endpoint,
            "payload": payload,
            "effect": effect,
        },
        "next_step": (
            f"To execute, re-call `{action}` with the SAME arguments plus "
            "`confirm=True`. Show `effect` to the user first and get "
            "explicit confirmation — do NOT auto-retry with confirm=True."
        ),
    }


# ── Contents diff builder (used by github_write's preview) ────────────────
# Turns `PUT /repos/{owner}/{repo}/contents/{path}` into a unified diff
# between the file currently on the target branch (or default branch) and
# the new base64-decoded content. So the human approves a real diff, not
# a base64 blob.

_CONTENTS_PATH_RE = re.compile(r"^/repos/([^/]+)/([^/]+)/contents/(.+)$")


def _decode_text_or_binary(raw: bytes) -> tuple[bool, Optional[str]]:
    """Return (is_binary, decoded_text). Text = valid UTF-8 with no NUL bytes."""
    if b"\x00" in raw:
        return True, None
    try:
        return False, raw.decode("utf-8")
    except UnicodeDecodeError:
        return True, None


def _build_contents_diff(
    method: str,
    path: str,
    body: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return a diff-preview dict for ``PUT /repos/.../contents/...``, else None."""
    if method != "PUT":
        return None
    m = _CONTENTS_PATH_RE.match(path)
    if not m:
        return None
    owner, repo, file_path = m.group(1), m.group(2), m.group(3)
    if not body or "content" not in body:
        return {
            "kind": "contents",
            "owner": owner, "repo": repo, "file_path": file_path,
            "note": "No `content` field in body — skipping diff.",
        }
    branch = body.get("branch")
    try:
        new_raw = base64.b64decode(body["content"], validate=False)
    except (ValueError, TypeError) as exc:
        return {
            "kind": "contents",
            "owner": owner, "repo": repo, "file_path": file_path,
            "note": f"Could not base64-decode new content: {exc}",
        }
    new_is_binary, new_text = _decode_text_or_binary(new_raw)

    try:
        res = _get(_api(path), params={"ref": branch} if branch else None)
    except RuntimeError as exc:
        return {
            "kind": "contents",
            "owner": owner, "repo": repo, "file_path": file_path,
            "branch": branch,
            "note": f"Could not fetch current file for diff: {exc}",
        }

    old_text: Optional[str] = None
    old_is_binary = False
    old_size: Optional[int] = None
    if res.status_code == 404:
        old_status = "new_file"
    elif res.status_code == 200:
        old_status = "updates_existing"
        try:
            old_json = res.json() or {}
        except ValueError:
            old_json = {}
        if isinstance(old_json, list):
            return {
                "kind": "contents",
                "owner": owner, "repo": repo, "file_path": file_path,
                "branch": branch,
                "note": "Path refers to a directory, not a file — refusing to diff.",
            }
        raw_old_b64 = (old_json.get("content") or "").replace("\n", "")
        old_size = old_json.get("size")
        try:
            old_raw = base64.b64decode(raw_old_b64, validate=False) if raw_old_b64 else b""
        except (ValueError, TypeError):
            old_raw = b""
        old_is_binary, old_text = _decode_text_or_binary(old_raw)
    else:
        return {
            "kind": "contents",
            "owner": owner, "repo": repo, "file_path": file_path,
            "branch": branch,
            "note": f"GET {path} returned status {res.status_code}; cannot build diff.",
        }

    result: dict[str, Any] = {
        "kind": "contents",
        "owner": owner, "repo": repo, "file_path": file_path,
        "branch": branch, "status": old_status,
        "new_size": len(new_raw), "old_size": old_size,
        "new_is_binary": new_is_binary, "old_is_binary": old_is_binary,
    }
    if new_is_binary or old_is_binary:
        result["note"] = (
            "Binary content detected — skipping textual diff. Verify the "
            "target file manually before confirming."
        )
        return result

    diff_lines = list(difflib.unified_diff(
        (old_text or "").splitlines(keepends=False),
        (new_text or "").splitlines(keepends=False),
        fromfile=f"a/{file_path}" if old_status == "updates_existing" else "/dev/null",
        tofile=f"b/{file_path}",
        lineterm="",
    ))
    if not diff_lines:
        result["diff"] = ""
        result["note"] = "No textual changes — new content is identical to current."
        return result

    max_lines, max_bytes = 400, 20_000
    truncated = False
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines]
        truncated = True
    diff_text = "\n".join(diff_lines)
    if len(diff_text) > max_bytes:
        diff_text = diff_text[:max_bytes]
        truncated = True
    result["diff"] = diff_text
    if truncated:
        result["diff_truncated"] = True
    return result


# ── Read tools that support the write flow (use GITHUB_TOKEN) ─────────────

@mcp.tool()
def whoami() -> dict[str, Any]:
    """Return the authenticated user's login. **Read-only (GITHUB_TOKEN).**

    Handy first call before any write — confirms which account the token
    is acting as so the AI can show the user "you're about to invite X to
    a repo owned by <you>" during the preview.
    """
    res = _get(_api("/user"))
    if res.status_code != 200:
        return _error_payload(res, context="whoami /user")
    data = res.json() or {}
    return {
        "ok": True,
        "login": data.get("login"),
        "id": data.get("id"),
        "name": data.get("name"),
        "email": data.get("email"),
        "html_url": data.get("html_url"),
        "type": data.get("type"),
    }


@mcp.tool()
def list_repo_collaborators(owner: str, repo: str) -> dict[str, Any]:
    """List current collaborators on ``owner/repo``. **Read-only (GITHUB_TOKEN).**

    Call before ``invite_repo_collaborator`` to check whether the target
    user already has access (invites to existing collaborators return 204
    with no email sent).
    """
    if err := _validate_owner(owner):
        return err
    if err := _validate_repo(repo):
        return err
    res = _get(
        _api(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/collaborators"
        ),
        params={"per_page": 100},
    )
    if res.status_code != 200:
        return _error_payload(
            res, org=owner, context=f"list_repo_collaborators {owner}/{repo}"
        )
    data = res.json() or []
    return {
        "ok": True,
        "owner": owner,
        "repo": repo,
        "count": len(data),
        "collaborators": [
            {
                "login": c.get("login"),
                "id": c.get("id"),
                "type": c.get("type"),
                "role_name": c.get("role_name"),
                "permissions": c.get("permissions"),
                "html_url": c.get("html_url"),
            }
            for c in data
        ],
    }


@mcp.tool()
def list_repo_invitations(owner: str, repo: str) -> dict[str, Any]:
    """List pending collaborator invitations on ``owner/repo``. **Read-only.**

    Returns ``invitation_id`` values you can pass to
    ``cancel_repo_invitation``.
    """
    if err := _validate_owner(owner):
        return err
    if err := _validate_repo(repo):
        return err
    res = _get(
        _api(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/invitations"
        ),
        params={"per_page": 100},
    )
    if res.status_code != 200:
        return _error_payload(
            res, org=owner, context=f"list_repo_invitations {owner}/{repo}"
        )
    data = res.json() or []
    return {
        "ok": True,
        "owner": owner,
        "repo": repo,
        "count": len(data),
        "invitations": [
            {
                "id": inv.get("id"),
                "invitee": (inv.get("invitee") or {}).get("login"),
                "inviter": (inv.get("inviter") or {}).get("login"),
                "permissions": inv.get("permissions"),
                "created_at": inv.get("created_at"),
                "expired": inv.get("expired"),
                "html_url": inv.get("html_url"),
            }
            for inv in data
        ],
    }


# ── Write tools (owner-guarded, confirm-gated) ────────────────────────────

@mcp.tool()
def invite_repo_collaborator(
    owner: str,
    repo: str,
    username: str,
    permission: str = "pull",
    confirm: bool = False,
) -> dict[str, Any]:
    """Invite ``username`` as a collaborator on ``owner/repo``.

    **Write. Requires ``confirm=True``.**

    Sends a GitHub email invitation the invitee must accept before access
    is granted. Default ``confirm=False`` returns a preview only.

    ``permission`` ∈ {``pull``, ``triage``, ``push``, ``maintain``,
    ``admin``}. Default ``pull`` — the safest choice for "give them
    access to pull a GHCR package linked to this repo".

    Return codes: 201 = new invitation created, 204 = user was already a
    collaborator (no email sent).
    """
    if err := _validate_owner(owner):
        return err
    if err := _validate_repo(repo):
        return err
    if err := _validate_username(username):
        return err
    if permission not in _ALLOWED_PERMISSIONS:
        return {
            "ok": False,
            "error": (
                f"Invalid permission {permission!r}. Allowed: "
                + ", ".join(sorted(_ALLOWED_PERMISSIONS))
            ),
        }
    endpoint = (
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/collaborators/{quote(username, safe='')}"
    )
    payload = {"permission": permission}
    effect = (
        f"Sends an email invitation to '{username}' for collaborator access "
        f"to {owner}/{repo} with '{permission}' permission. The invitee has "
        "7 days to accept via the email link. Cancel via "
        "list_repo_invitations + cancel_repo_invitation if needed."
    )
    if not confirm:
        return _preview(
            "invite_repo_collaborator", "PUT", endpoint, payload, effect
        )
    res = _write_http("PUT", _api(endpoint), json_body=payload)
    if res.status_code not in (201, 204):
        return _error_payload(
            res, org=owner, context=f"invite {username} to {owner}/{repo}"
        )
    data: dict[str, Any] = {}
    if res.status_code == 201 and (res.text or "").strip():
        try:
            body = res.json()
            if isinstance(body, dict):
                data = body
        except ValueError:
            pass
    return {
        "ok": True,
        "confirmed": True,
        "action": "invite_repo_collaborator",
        "owner": owner,
        "repo": repo,
        "username": username,
        "permission": permission,
        "status_code": res.status_code,
        "invitation": {
            "id": data.get("id"),
            "invitee": (data.get("invitee") or {}).get("login"),
            "created_at": data.get("created_at"),
            "html_url": data.get("html_url"),
        } if data else None,
        "note": (
            "204 = user was already a collaborator (no email sent). "
            "201 = new invitation created; invitee has 7 days to accept."
        ),
    }


@mcp.tool()
def cancel_repo_invitation(
    owner: str,
    repo: str,
    invitation_id: int,
    confirm: bool = False,
) -> dict[str, Any]:
    """Cancel a pending repo invitation.

    **Write. Requires ``confirm=True``.**

    Use ``list_repo_invitations`` to find the ``invitation_id`` first.
    Reversible only by sending a fresh invitation via
    ``invite_repo_collaborator``.
    """
    if err := _validate_owner(owner):
        return err
    if err := _validate_repo(repo):
        return err
    if not isinstance(invitation_id, int) or invitation_id < 1:
        return {
            "ok": False,
            "error": "`invitation_id` must be a positive integer.",
        }
    endpoint = (
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/invitations/{invitation_id}"
    )
    effect = (
        f"Deletes pending invitation #{invitation_id} on {owner}/{repo}. "
        "The invitee's email link stops working. Reversible only by "
        "sending a fresh invitation."
    )
    if not confirm:
        return _preview(
            "cancel_repo_invitation", "DELETE", endpoint, None, effect
        )
    res = _write_http("DELETE", _api(endpoint))
    if res.status_code != 204:
        return _error_payload(
            res,
            org=owner,
            context=f"cancel invitation {owner}/{repo}#{invitation_id}",
        )
    return {
        "ok": True,
        "confirmed": True,
        "action": "cancel_repo_invitation",
        "owner": owner,
        "repo": repo,
        "invitation_id": invitation_id,
        "status_code": 204,
    }


@mcp.tool()
def remove_repo_collaborator(
    owner: str,
    repo: str,
    username: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Remove ``username`` as a collaborator on ``owner/repo``.

    **Write. Requires ``confirm=True``.**

    Immediately revokes clone / pull / push access (and any package-pull
    rights inherited from this repo). Reversible by re-inviting.
    """
    if err := _validate_owner(owner):
        return err
    if err := _validate_repo(repo):
        return err
    if err := _validate_username(username):
        return err
    endpoint = (
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/collaborators/{quote(username, safe='')}"
    )
    effect = (
        f"Immediately revokes '{username}'s collaborator access to "
        f"{owner}/{repo}. They lose clone/pull/push and any package-pull "
        "rights inherited from this repo. Reversible by re-inviting."
    )
    if not confirm:
        return _preview(
            "remove_repo_collaborator", "DELETE", endpoint, None, effect
        )
    res = _write_http("DELETE", _api(endpoint))
    if res.status_code != 204:
        return _error_payload(
            res, org=owner, context=f"remove {username} from {owner}/{repo}"
        )
    return {
        "ok": True,
        "confirmed": True,
        "action": "remove_repo_collaborator",
        "owner": owner,
        "repo": repo,
        "username": username,
        "status_code": 204,
    }


@mcp.tool()
def set_repo_visibility(
    owner: str,
    repo: str,
    visibility: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Change ``owner/repo``'s visibility.

    **Write. Requires ``confirm=True``.**

    ``visibility`` ∈ {``public``, ``private``, ``internal``} (``internal``
    requires GitHub Enterprise).

    **High-impact change** — flipping a private repo to public exposes
    every commit in its history to the internet immediately. Org policy
    may block or auto-revert. There is no GitHub-side cooldown or undo;
    the AI MUST show the effect to the user and get explicit confirmation
    before re-invoking with ``confirm=True``.
    """
    if err := _validate_owner(owner):
        return err
    if err := _validate_repo(repo):
        return err
    if visibility not in _ALLOWED_REPO_VISIBILITIES:
        return {
            "ok": False,
            "error": (
                f"Invalid visibility {visibility!r}. Allowed: "
                + ", ".join(sorted(_ALLOWED_REPO_VISIBILITIES))
            ),
        }
    endpoint = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    payload = {"visibility": visibility}
    if visibility == "public":
        effect = (
            f"WARNING: PUBLICLY EXPOSES {owner}/{repo} and its FULL COMMIT "
            "HISTORY to anyone on the internet. Immediate — no cooldown. "
            "Org policy may auto-revert or reject. Any secret ever "
            "committed to this repo becomes public. Verify with the user "
            "before confirming."
        )
    elif visibility == "private":
        effect = (
            f"Makes {owner}/{repo} private. Anonymous / non-collaborator "
            "clone URLs stop working immediately. Existing forks may "
            "retain code that was public before the flip."
        )
    else:
        effect = (
            f"Sets {owner}/{repo} visibility to 'internal' (visible to all "
            "org members). Requires GitHub Enterprise."
        )
    if not confirm:
        return _preview(
            "set_repo_visibility", "PATCH", endpoint, payload, effect
        )
    res = _write_http("PATCH", _api(endpoint), json_body=payload)
    if res.status_code != 200:
        return _error_payload(
            res,
            org=owner,
            context=f"set_repo_visibility {owner}/{repo} -> {visibility}",
        )
    data = res.json() or {}
    return {
        "ok": True,
        "confirmed": True,
        "action": "set_repo_visibility",
        "owner": owner,
        "repo": repo,
        "visibility": data.get("visibility"),
        "private": data.get("private"),
        "html_url": data.get("html_url"),
        "status_code": 200,
    }


# ═════════════════════════════════════════════════════════════════════════
# Freestyle write escape hatch
# ═════════════════════════════════════════════════════════════════════════
#
# Same owner-guard + confirm-gate as the tier-4 tools, but for arbitrary
# REST endpoints. Allow-list is narrower than `github_get`: only
# self-scoped paths. `/repos/{owner}/…` is only allowed when `owner`
# equals the token owner. `/user/…` and `/gists*` are inherently self.
# Org paths and other people's repos remain read-only via `github_get`.

_WRITE_ALLOWED_METHODS: frozenset[str] = frozenset(
    {"POST", "PATCH", "PUT", "DELETE"}
)

_WRITE_ALLOWED_PREFIXES: tuple[str, ...] = (
    "/repos/",   # owner-checked below against whoami.login
    "/user",     # /user, /user/emails, /user/keys, /user/starred/…
    "/user/",
    "/gists",    # personal gists — create/edit/delete
    "/gists/",
)


def _is_write_path_allowed(path: str) -> bool:
    if not path or not path.startswith("/") or ".." in path:
        return False
    for prefix in _WRITE_ALLOWED_PREFIXES:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
        elif path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _parse_body_json(
    raw: str,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Return (body_dict_or_None, error_payload_or_None). Empty body is OK."""
    if not raw or not raw.strip():
        return None, None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return None, {
            "ok": False,
            "error": f"body_json is not valid JSON: {exc}",
        }
    if not isinstance(data, dict):
        return None, {
            "ok": False,
            "error": "body_json must decode to a JSON object.",
        }
    return data, None


@mcp.tool()
def github_write(
    method: str,
    path: str,
    body_json: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """Freestyle **confirm-gated write** against the GitHub REST API.

    **Write. Requires `confirm=True`.**

    Same auth as reads (single ``GITHUB_TOKEN``). Personal repos and
    org repos are treated the same — whether the write actually goes
    through depends on the scopes the token has (and SAML-SSO
    authorization on that org). Allow-list of paths: ``/repos/…``,
    ``/user/…``, ``/gists*``. Other org paths (``/orgs/…``,
    org-package writes) remain read-only via ``github_get``.

    **Diff preview for file writes.** When called as
    ``PUT /repos/{owner}/{repo}/contents/{path}`` with ``confirm=False``
    (default), the preview includes a ``preview.diff`` field: a unified
    diff between the current file on the target branch (or default
    branch when ``branch`` is omitted) and the new base64-decoded
    content in ``body_json``. Show that diff to the user and get
    explicit approval before re-calling with ``confirm=True``. Binary
    content and directories skip the diff with an explanatory note.

    Args:
        method: One of ``POST``, ``PATCH``, ``PUT``, ``DELETE``. Case
            insensitive. ``GET`` is refused — use ``github_get``.
        path: API path starting with ``/``. Allow-list: ``/repos/…``,
            ``/user``, ``/user/…``, ``/gists``, ``/gists/…``. Must NOT
            contain a query string.
        body_json: JSON object encoded as a string. Empty for methods
            that don't take a body (e.g. most ``DELETE`` endpoints,
            ``PUT /user/starred/…``).
        confirm: ``False`` (default) returns a preview showing method +
            endpoint + payload + effect (+ ``diff`` for contents PUTs).
            Show `effect` (and `diff` when present) to the user and get
            explicit confirmation before re-calling with ``confirm=True``.
            Never auto-retry with ``confirm=True`` from the same LLM
            turn — approval must come from the human user.

    Examples::

        # Update your own repo's description
        github_write(method="PATCH",
                     path="/repos/<your-github-login>/<your-repo>",
                     body_json='{"description":"New description"}',
                     confirm=True)

        # Push a file to a repo you own. First call (confirm=False, default)
        # returns preview.diff — show it to the user, get approval, then
        # re-call with confirm=True.
        github_write(method="PUT",
                     path="/repos/<your-github-login>/<your-repo>/contents/README.md",
                     body_json='{"message":"docs: fix typo",'
                               '"content":"<base64>",'
                               '"branch":"main",'
                               '"sha":"<current-blob-sha>"}')

        # Replace repo topics
        github_write(method="PUT",
                     path="/repos/<your-github-login>/<your-repo>/topics",
                     body_json='{"names":["mcp","github","docker"]}',
                     confirm=True)

        # Star any repo (self-action under /user)
        github_write(method="PUT",
                     path="/user/starred/octocat/hello-world",
                     confirm=True)

        # Delete a personal gist
        github_write(method="DELETE",
                     path="/gists/abc123",
                     confirm=True)
    """
    m = (method or "").upper().strip()
    if m == "GET":
        return {
            "ok": False,
            "error": "Use `github_get` for reads.",
        }
    if m not in _WRITE_ALLOWED_METHODS:
        return {
            "ok": False,
            "error": (
                "method must be one of "
                f"{sorted(_WRITE_ALLOWED_METHODS)}; got {method!r}."
            ),
        }
    if "?" in path or "#" in path:
        return {
            "ok": False,
            "error": (
                "Do not embed a query string in `path`. Use body_json for "
                "POST/PATCH/PUT parameters."
            ),
        }
    if not _is_write_path_allowed(path):
        return {
            "ok": False,
            "error": (
                f"Path {path!r} is not on the freestyle-write allow-list. "
                "Allowed prefixes: " + ", ".join(_WRITE_ALLOWED_PREFIXES) +
                ". Use `github_get` for reads on other paths."
            ),
        }

    if path.startswith("/repos/"):
        owner = _extract_org_from_path(path)
        if not owner:
            return {
                "ok": False,
                "error": f"Could not extract owner from path {path!r}.",
            }

    body, body_err = _parse_body_json(body_json)
    if body_err:
        return body_err

    effect = (
        f"{m} {path} — freestyle write. The server does not know the "
        "semantic effect of arbitrary endpoints; preview the body (and "
        "`diff` when present) carefully and get explicit user approval "
        "before executing."
    )
    if not confirm:
        preview = _preview("github_write", m, path, body, effect)
        diff_info = _build_contents_diff(m, path, body)
        if diff_info is not None:
            preview["preview"]["diff"] = diff_info
        return preview

    res = _write_http(m, _api(path), json_body=body)
    if res.status_code >= 400:
        return _error_payload(res, context=f"github_write {m} {path}")

    try:
        data = res.json()
    except ValueError:
        data = res.text or None
    return {
        "ok": True,
        "confirmed": True,
        "action": "github_write",
        "method": m,
        "path": path,
        "status_code": res.status_code,
        "data": data,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
