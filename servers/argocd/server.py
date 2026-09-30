"""ArgoCD MCP server — SSO login orchestration + read-only app inspection.

Companion to the AWS / GitHub / GitLab MCPs. Wraps the ``argocd`` CLI
running inside the container so an AI host can list applications,
inspect app state / manifests / resources / logs / events, and browse
projects and clusters — without the user having to shell out to
``argocd`` themselves.

**IMPORTANT for AI hosts: argocd is a Kubernetes proxy.** Every
per-resource operation this MCP exposes (``get_app_logs``,
``get_app_events``, ``get_app_resource_events``, ``get_app_resources``,
``get_resource``, ``get_resource_tree``, ...) is served by
argocd-server proxying the target cluster on your behalf using
credentials it already holds. You do NOT need kubectl, kubeconfig,
IAM, EKS access, or any other Kubernetes-side tool to read pod logs,
live manifests, the resource graph, or events. If a tool doesn't
give you what you need, add a specialized tool here — do NOT tell
the user "I need Kubernetes access".

Credential model
----------------

Same pattern as ``lvx-aws`` uses for ``~/.aws`` / ``~/.awsume``: the
container mounts the host's ``~/.config/argocd`` as a live read-write
volume, so any session written on the host is immediately visible
inside the container and vice versa.

Two ways to authenticate:

1. **Host-side.** Run ``argocd login <server> --sso --grpc-web`` in your
   own terminal. The tokens land in ``~/.config/argocd/config`` and the
   container picks them up on the next tool call.

2. **Through the AI host.** Call the ``argocd_login`` tool. The server
   starts ``argocd login --sso --grpc-web`` inside the container,
   captures the ``Performing authorization_code flow login: <URL>`` line
   from stderr, and returns the URL to the AI host. The user clicks
   it, authenticates in their browser, and the OAuth callback lands on
   ``http://localhost:8085/auth/callback`` (published from the container
   via ``docker run -p 8085:8085``). ``argocd`` writes the token and
   exits. The next tool call succeeds silently.

**THIS SERVER IS READ-ONLY.** No sync / rollback / delete / patch /
create tools. The freestyle ``argocd_cli`` tool refuses any subcommand
outside a curated allow-list of read verbs.

Configuration (env vars):
  * ``ARGOCD_SERVER``          — required. Default
    ``argocd.devops.mycompany.com``. May be overridden per session
    via the container's ``-e ARGOCD_SERVER=...`` argument.
  * ``ARGOCD_CONFIG_DIR``      — where ``argocd`` reads / writes its
    config file. Default ``/root/.config/argocd`` — the compose mount
    binds this to the host's ``~/.config/argocd``.
  * ``ARGOCD_SSO_PORT``        — local OAuth2 callback port used during
    ``argocd login --sso``. Default ``8085``. Deliberately NOT ``8085``
    (argocd's own default) so a login started through this MCP does not
    collide with a login the user is running from their host terminal
    on the same machine. Must match the ``-p <port>:<port>`` published
    on ``docker run``.
  * ``ARGOCD_LOGIN_TIMEOUT``   — seconds to wait for the user to
    complete SSO in their browser after the URL is returned. Default
    ``180``.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import quote

import requests
import urllib3
import yaml
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lvx-argocd")
from version_notifier import install as _install_version_notifier
_install_version_notifier(mcp, "lvx-argocd")


DEFAULT_SERVER = "argocd.devops.mycompany.com"
DEFAULT_SSO_PORT = 8085
DEFAULT_LOGIN_TIMEOUT = 180

# argocd CLI subcommands (space-separated) that are safe read-only verbs.
# Freestyle `argocd_cli` refuses anything else. Matches the surface the AI
# actually needs (app / cluster / proj / repo / account / context / version).
_READ_ONLY_CLI_ALLOWLIST: frozenset[str] = frozenset({
    "app get",
    "app list",
    "app manifests",
    "app resources",
    "app history",
    "app diff",
    "app logs",
    "app wait",
    "cluster list",
    "cluster get",
    "proj list",
    "proj get",
    "proj role list",
    "proj windows list",
    "repo list",
    "repo get",
    "repocreds list",
    "account get-user-info",
    "account can-i",
    "context",
    "version",
    "cert list",
})

# Explicitly refused subcommands (documented so the AI can explain why).
_WRITE_CLI_DENYLIST: frozenset[str] = frozenset({
    "app sync",
    "app rollback",
    "app delete",
    "app patch",
    "app create",
    "app edit",
    "app set",
    "app unset",
    "app terminate-op",
    "app actions run",
    "cluster add",
    "cluster rm",
    "proj create",
    "proj delete",
    "proj role",
    "proj add-source",
    "proj remove-source",
    "proj add-destination",
    "proj remove-destination",
    "repo add",
    "repo rm",
    "repocreds add",
    "repocreds rm",
    "login",
    "logout",
    "relogin",
})

_APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/]{0,252}$")
_SSO_URL_RE = re.compile(r"https?://\S+")

# ── Config helpers ──────────────────────────────────────────────────────

def _server() -> str:
    return (os.environ.get("ARGOCD_SERVER") or DEFAULT_SERVER).strip()


def _sso_port() -> int:
    raw = os.environ.get("ARGOCD_SSO_PORT") or ""
    try:
        return int(raw) if raw.strip() else DEFAULT_SSO_PORT
    except ValueError:
        return DEFAULT_SSO_PORT


def _login_timeout() -> int:
    raw = os.environ.get("ARGOCD_LOGIN_TIMEOUT") or ""
    try:
        return int(raw) if raw.strip() else DEFAULT_LOGIN_TIMEOUT
    except ValueError:
        return DEFAULT_LOGIN_TIMEOUT


def _config_dir() -> str:
    return (
        os.environ.get("ARGOCD_CONFIG_DIR")
        or os.path.join(os.path.expanduser("~"), ".config", "argocd")
    )


def _config_path() -> str:
    return os.path.join(_config_dir(), "config")


def _base_env() -> dict[str, str]:
    """Environment for every argocd subprocess.

    ``XDG_CONFIG_HOME`` is set so ``argocd`` picks up the mounted config
    dir regardless of whether it defaults to ``~/.config/argocd`` or
    ``$XDG_CONFIG_HOME/argocd``. ``BROWSER=true`` prevents the login
    flow from trying to invoke ``xdg-open`` inside the container — the
    URL is printed to stderr either way.
    """
    env = dict(os.environ)
    env.setdefault(
        "XDG_CONFIG_HOME",
        os.path.dirname(_config_dir().rstrip("/")) or "/root/.config",
    )
    env["BROWSER"] = "true"
    return env


# ── argocd invocation ───────────────────────────────────────────────────

def _run_argocd(
    args: list[str],
    timeout: int = 60,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argocd <args>`` synchronously and return the result.

    ``--grpc-web`` and ``--server <ARGOCD_SERVER>`` are appended
    automatically so callers only pass the verb + resource-specific
    flags. When ``ARGOCD_VERIFY_SSL`` is off (the default under corp
    VPN) ``--insecure`` is also appended so the CLI accepts the VPN's
    intercepted cert. Never raises — the caller inspects
    ``returncode`` / ``stderr`` and shapes an error payload.
    """
    cmd = ["argocd", *args, "--server", _server(), "--grpc-web"]
    if not _verify_ssl():
        cmd.append("--insecure")
    return subprocess.run(
        cmd,
        env=_base_env(),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── REST API helpers ────────────────────────────────────────────────────
# Direct HTTPS to /api/v1/... is 10-100x faster than shelling out to the
# CLI for anything that lists apps, because we can request field
# projection and skip the CLI's client-side merging + table rendering.
# Auth token is read out of the same ~/.config/argocd/config the CLI
# writes when the user finishes SSO.

def _verify_ssl() -> bool:
    # Default OFF — the LV / MyCompany corp VPN intercepts TLS with a
    # firewall-signed cert that isn't in the container's CA store.
    # Callers can re-enable strict verification with
    # ``ARGOCD_VERIFY_SSL=true`` when running off VPN.
    return (os.environ.get("ARGOCD_VERIFY_SSL") or "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


if not _verify_ssl():
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _load_argocd_config() -> Optional[dict[str, Any]]:
    """Parse ``~/.config/argocd/config`` as YAML. Returns ``None`` if
    missing or malformed."""
    path = _config_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _token_and_server() -> Optional[tuple[str, str]]:
    """Return ``(server, auth_token)`` from the shared config, or ``None``
    when unauthenticated."""
    cfg = _load_argocd_config()
    if not cfg:
        return None
    ctx_name = _server()
    for user in cfg.get("users") or []:
        if user.get("name") == ctx_name and user.get("auth-token"):
            return ctx_name, user["auth-token"]
    for user in cfg.get("users") or []:
        if user.get("auth-token"):
            return user.get("name") or ctx_name, user["auth-token"]
    return None


class _ArgocdApiError(Exception):
    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        super().__init__(kind)
        self.kind = kind
        self.payload = payload


def _api_get(path: str, params: Optional[dict[str, Any]] = None, timeout: int = 30) -> Any:
    """GET ``https://<server><path>`` with the shared bearer token.

    Raises ``_ArgocdApiError`` on any non-2xx or transport failure so the
    caller can decide whether to fall back to the CLI or auto-refresh
    SSO. Returns the parsed JSON body on success.
    """
    creds = _token_and_server()
    if not creds:
        raise _ArgocdApiError("NOT_LOGGED_IN", {"reason": "no token in config"})
    server, token = creds
    url = f"https://{server}{path}"
    try:
        r = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "lvx-argocd-mcp",
            },
            params=params,
            timeout=timeout,
            verify=_verify_ssl(),
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise _ArgocdApiError(
            "API_UNREACHABLE",
            {"message": f"{exc.__class__.__name__}: {exc}"},
        ) from exc
    if r.status_code == 401 or r.status_code == 403:
        raise _ArgocdApiError(
            "NOT_LOGGED_IN",
            {"status_code": r.status_code, "body": (r.text or "")[:400]},
        )
    if r.status_code != 200:
        # Some argocd deployments 500 when the OIDC refresh token is
        # stale — the body still contains the OAuth2 verdict, so surface
        # it as NOT_LOGGED_IN and let the caller auto-refresh SSO.
        body_low = (r.text or "").lower()
        if (
            "refresh token" in body_low
            or "invalid_grant" in body_low
            or "invalid_request" in body_low
            or "invalid_token" in body_low
            or "unauthenticated" in body_low
        ):
            raise _ArgocdApiError(
                "NOT_LOGGED_IN",
                {"status_code": r.status_code, "body": (r.text or "")[:400]},
            )
        raise _ArgocdApiError(
            "API_ERROR",
            {"status_code": r.status_code, "body": (r.text or "")[:500]},
        )
    try:
        return r.json()
    except ValueError as exc:
        raise _ArgocdApiError("API_ERROR", {"message": f"non-JSON body: {exc}"}) from exc


def _summarize_app_item(app: dict[str, Any]) -> dict[str, Any]:
    meta = app.get("metadata") or {}
    spec = app.get("spec") or {}
    status = app.get("status") or {}
    dest = spec.get("destination") or {}
    source = spec.get("source") or (spec.get("sources") or [{}])[0] or {}
    return {
        "name": meta.get("name"),
        "namespace_argocd": meta.get("namespace"),
        "project": spec.get("project"),
        "sync_status": (status.get("sync") or {}).get("status"),
        "health_status": (status.get("health") or {}).get("status"),
        "operation_state": (status.get("operationState") or {}).get("phase"),
        "dest_server": dest.get("server") or dest.get("name"),
        "dest_namespace": dest.get("namespace"),
        "repo_url": source.get("repoURL"),
        "path": source.get("path"),
        "target_revision": source.get("targetRevision"),
    }


# Fields projection used by the /applications listing so we don't drag
# down the full spec+status tree for every app in the org.
_LIST_APP_FIELDS: str = ",".join([
    "items.metadata.name",
    "items.metadata.namespace",
    "items.spec.project",
    "items.spec.destination",
    "items.spec.source",
    "items.spec.sources",
    "items.status.sync.status",
    "items.status.sync.revision",
    "items.status.health.status",
    "items.status.operationState.phase",
])


def _match_app(
    name: str,
    dest_server: str,
    dest_ns: str,
    pattern: str,
    search_lower: str,
    cluster_lower: str,
    namespace_lower: str,
) -> bool:
    if pattern and not fnmatch.fnmatchcase(name, pattern):
        return False
    if search_lower and search_lower not in name.lower():
        return False
    if cluster_lower and cluster_lower not in (dest_server or "").lower():
        return False
    if namespace_lower and namespace_lower not in (dest_ns or "").lower():
        return False
    return True


def _auto_start_sso_login() -> dict[str, Any]:
    """Kick off an SSO login flow if one isn't already running, returning
    the payload every session-expired error piggybacks on."""
    with _LOGIN_LOCK:
        existing = _LOGIN_STATE.get("process")
        if existing is not None and existing.poll() is None and _LOGIN_STATE.get("sso_url"):
            return {
                "authorize_url": _LOGIN_STATE["sso_url"],
                "server": _LOGIN_STATE.get("server") or _server(),
                "sso_port": _sso_port(),
                "already_in_progress": True,
            }
        _reset_login_state()
        url, err = _start_login_subprocess()
        if err:
            return {"login_start_error": err}
        return {
            "authorize_url": url,
            "server": _server(),
            "sso_port": _sso_port(),
            "already_in_progress": False,
        }


def _attach_auto_sso(payload: dict[str, Any]) -> dict[str, Any]:
    """Kick off a fresh SSO login and merge the resulting ``authorize_url``
    + user-facing instructions into ``payload``. Callers set
    ``payload["error"] = "NOT_LOGGED_IN"`` themselves before invoking."""
    started = _auto_start_sso_login()
    if started.get("login_start_error"):
        payload["message"] = (
            f"argocd session for '{_server()}' is missing or expired, "
            "and auto-starting the SSO login failed. See "
            "`login_start_error` for details, or run "
            f"`argocd login {_server()} --sso --grpc-web` on your host."
        )
        payload["login_start_error"] = started["login_start_error"]
        return payload
    payload["auto_sso_started"] = True
    payload["authorize_url"] = started["authorize_url"]
    payload["sso_port"] = started["sso_port"]
    payload["already_in_progress"] = started.get("already_in_progress", False)
    payload["message"] = (
        f"argocd session for '{_server()}' is missing or expired. "
        "I started an SSO login for you — the URL is in "
        "`authorize_url`. Click it, complete SSO in your browser, and "
        "then retry the original request."
    )
    payload["instruction"] = (
        "Present `authorize_url` to the user as a clickable link and "
        "STOP. Do NOT poll in a loop. After the user says they've "
        "signed in, retry the ORIGINAL tool call (not `argocd_login`) "
        "— the newly-written token will be picked up automatically."
    )
    return payload


def _api_error_response(exc: _ArgocdApiError, context: str) -> dict[str, Any]:
    """Convert an ``_ArgocdApiError`` into an MCP tool response.

    NOT_LOGGED_IN piggybacks on the auto-start SSO path so the caller
    gets back an ``authorize_url`` in the same turn. Every other kind
    is passed through verbatim.
    """
    if exc.kind == "NOT_LOGGED_IN":
        payload: dict[str, Any] = {
            "ok": False,
            "error": "NOT_LOGGED_IN",
            "context": context,
            "server": _server(),
            "api_body": (exc.payload.get("body") or "")[:400],
        }
        return _attach_auto_sso(payload)
    return {
        "ok": False,
        "error": exc.kind,
        "context": context,
        **exc.payload,
    }


def _argocd_error(res: subprocess.CompletedProcess[str], context: str) -> dict[str, Any]:
    stderr = (res.stderr or "").strip()
    stdout = (res.stdout or "").strip()
    low = (stderr + "\n" + stdout).lower()
    if (
        # Direct session verdicts
        "not logged" in low
        or "invalid session" in low
        or "token is expired" in low
        or "token has expired" in low
        or "expired token" in low
        or "unauthenticated" in low
        or "unauthorized" in low
        or "401 " in low
        or "403 forbidden" in low
        # OAuth2 refresh-flow failures — argocd re-uses the refresh token
        # transparently on every call, so a stale/claimed one surfaces
        # here even when the local ~/.config/argocd/config looks fine.
        or "refresh token" in low
        or "refresh_token" in low
        or "invalid_grant" in low
        or "invalid_request" in low
        or "invalid_token" in low
        or ("oauth2" in low and "invalid" in low)
    ):
        payload: dict[str, Any] = {
            "ok": False,
            "error": "NOT_LOGGED_IN",
            "context": context,
            "server": _server(),
            "stderr": stderr[:500],
        }
        return _attach_auto_sso(payload)
    return {
        "ok": False,
        "error": "ARGOCD_CLI_ERROR",
        "context": context,
        "returncode": res.returncode,
        "stderr": stderr[:2000],
        "stdout_snippet": stdout[:500],
    }


def _parse_json_or_text(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _validate_app_name(name: str) -> Optional[dict[str, Any]]:
    if not name or not _APP_NAME_RE.match(name):
        return {
            "ok": False,
            "error": (
                f"Invalid app name {name!r}. Expected lowercase alphanumerics "
                "and `.`, `_`, `-`, `/`, up to 253 chars."
            ),
        }
    return None


# ── SSO login state ─────────────────────────────────────────────────────
# argocd login runs the OAuth2 authorization_code flow: it prints a URL,
# opens a local callback listener on ARGOCD_SSO_PORT, and blocks until
# the user completes auth in their browser. We start it as a subprocess,
# scrape the URL from stderr, and hand it back to the AI host.

_LOGIN_LOCK = threading.Lock()
_LOGIN_STATE: dict[str, Any] = {
    "process": None,          # subprocess.Popen | None
    "started_at": None,       # float epoch seconds
    "server": None,           # str
    "sso_url": None,          # str (URL the user clicks)
    "reader_thread": None,    # threading.Thread
    "output_lines": [],       # list[str] (captured stderr for diagnostics)
    "finished": False,        # True once argocd login exits
    "returncode": None,       # int | None
}


def _reset_login_state() -> None:
    _LOGIN_STATE.update({
        "process": None,
        "started_at": None,
        "server": None,
        "sso_url": None,
        "reader_thread": None,
        "output_lines": [],
        "finished": False,
        "returncode": None,
    })


def _reader_loop(proc: subprocess.Popen[str], url_ready: threading.Event) -> None:
    """Drain argocd login's stderr, capture the SSO URL when we see it."""
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip("\n")
            _LOGIN_STATE["output_lines"].append(line)
            # argocd prints: "Performing authorization_code flow login: <URL>"
            if _LOGIN_STATE.get("sso_url") is None and "authorization_code" in line.lower():
                m = _SSO_URL_RE.search(line)
                if m:
                    _LOGIN_STATE["sso_url"] = m.group(0)
                    url_ready.set()
            if len(_LOGIN_STATE["output_lines"]) > 500:
                # Bounded — argocd chatters during the callback wait.
                del _LOGIN_STATE["output_lines"][:100]
    finally:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        _LOGIN_STATE["finished"] = True
        _LOGIN_STATE["returncode"] = proc.returncode
        url_ready.set()


def _start_login_subprocess() -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Kick off ``argocd login --sso`` and wait briefly for the URL."""
    if not os.path.isdir(_config_dir()):
        try:
            os.makedirs(_config_dir(), exist_ok=True)
        except OSError as exc:
            return None, {
                "ok": False,
                "error": "CONFIG_DIR_UNWRITABLE",
                "message": (
                    f"Cannot create argocd config dir {_config_dir()!r}: "
                    f"{exc}. Check the docker volume mount for ~/.config/argocd."
                ),
            }

    server = _server()
    cmd = [
        "argocd", "login", server,
        "--sso",
        "--grpc-web",
        "--sso-port", str(_sso_port()),
        # Suppress argocd's xdg-open call — the container has no browser
        # and it treats a failed browser launch as fatal (process exits
        # before it can accept the callback). We hand the URL to the AI
        # host instead and the user clicks it on their own machine.
        "--sso-launch-browser=false",
    ]
    if not _verify_ssl():
        # `--insecure` skips cert verification; `--skip-test-tls` skips
        # argocd's initial TLS probe which otherwise triggers a
        # "server is not configured with TLS. Proceed (y/n)?" stdin
        # prompt behind corp VPNs, hanging the subprocess.
        cmd += ["--insecure", "--skip-test-tls"]
    try:
        proc = subprocess.Popen(
            cmd,
            env=_base_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return None, {
            "ok": False,
            "error": "ARGOCD_CLI_MISSING",
            "message": (
                "The `argocd` CLI is not on PATH inside the container. This "
                "is a build defect — report it to the registry maintainer."
            ),
        }
    # We pointed stderr → stdout above, so read from stdout.
    proc.stderr = proc.stdout

    url_ready = threading.Event()
    _LOGIN_STATE["process"] = proc
    _LOGIN_STATE["started_at"] = time.time()
    _LOGIN_STATE["server"] = server
    _LOGIN_STATE["sso_url"] = None
    _LOGIN_STATE["output_lines"] = []
    _LOGIN_STATE["finished"] = False
    _LOGIN_STATE["returncode"] = None
    thread = threading.Thread(
        target=_reader_loop, args=(proc, url_ready), daemon=True
    )
    _LOGIN_STATE["reader_thread"] = thread
    thread.start()

    # Give argocd a few seconds to print the URL. If it exits fast it's
    # almost certainly a config error (bad server URL, network, ...).
    url_ready.wait(timeout=10)
    if _LOGIN_STATE.get("sso_url"):
        return _LOGIN_STATE["sso_url"], None
    if _LOGIN_STATE.get("finished"):
        return None, {
            "ok": False,
            "error": "ARGOCD_LOGIN_FAILED_TO_START",
            "returncode": _LOGIN_STATE.get("returncode"),
            "output": "\n".join(_LOGIN_STATE.get("output_lines") or [])[-2000:],
            "message": (
                f"argocd login exited before printing the SSO URL. Verify "
                f"`{server}` is reachable from the container and the "
                "argocd server accepts SSO logins."
            ),
        }
    return None, {
        "ok": False,
        "error": "ARGOCD_LOGIN_URL_TIMEOUT",
        "message": (
            "argocd login did not print an SSO URL within 10s. This "
            "usually means the argocd server is unreachable — check "
            "network connectivity / DNS."
        ),
        "output": "\n".join(_LOGIN_STATE.get("output_lines") or [])[-2000:],
    }


# ── Tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def argocd_context() -> dict[str, Any]:
    """Show the current argocd server + config location. **Read-only.**

    Reports which ArgoCD instance this MCP server is pointed at
    (``ARGOCD_SERVER``), the path of the shared config file, whether
    the config file currently exists, and the SSO callback port used
    during ``argocd_login``. Handy first call before anything else.
    """
    cfg_path = _config_path()
    return {
        "ok": True,
        "server": _server(),
        "grpc_web": True,
        "config_dir": _config_dir(),
        "config_file": cfg_path,
        "config_present": os.path.isfile(cfg_path),
        "sso_port": _sso_port(),
        "login_timeout_seconds": _login_timeout(),
    }


@mcp.tool()
def argocd_status() -> dict[str, Any]:
    """Check whether the container has a valid ArgoCD session. **Read-only.**

    Calls ``argocd account get-user-info --grpc-web``. When logged in
    returns ``{ok: True, logged_in: True, username, iss}``. When the
    session is missing or expired, this tool auto-starts an SSO login
    flow (same as ``argocd_login``) and returns ``authorize_url`` so the
    AI can hand the click-through URL to the user in a single turn.
    """
    try:
        res = _run_argocd(["account", "get-user-info", "-o", "json"], timeout=30)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "ARGOCD_TIMEOUT",
            "message": (
                f"argocd account get-user-info timed out. Is {_server()} "
                "reachable from the container?"
            ),
        }
    if res.returncode != 0:
        # `_argocd_error` already auto-starts SSO and attaches
        # `authorize_url` when it detects NOT_LOGGED_IN, so just pass it
        # through with `logged_in: False` for callers that key off it.
        err = _argocd_error(res, "account get-user-info")
        if err.get("error") == "NOT_LOGGED_IN":
            err["logged_in"] = False
        return err
    data = _parse_json_or_text(res.stdout)
    if isinstance(data, dict):
        logged_in = bool(data.get("loggedIn"))
        payload: dict[str, Any] = {
            "ok": True,
            "logged_in": logged_in,
            "server": _server(),
            "username": data.get("username"),
            "iss": data.get("iss"),
            "groups": data.get("groups"),
        }
        if not logged_in:
            # `account get-user-info` succeeds even when there's no
            # session — it just reports loggedIn: false. Auto-start SSO
            # here too, not just on the hard-error path below.
            return _attach_auto_sso(payload)
        return payload
    return {"ok": True, "server": _server(), "raw": data}


@mcp.tool()
def argocd_login(force: bool = False) -> dict[str, Any]:
    """Start an SSO login flow and return the URL for the user to click.

    Behavior:

      1. If ``force`` is ``False`` (default) and ``argocd_status`` shows
         we're already logged in, returns immediately without launching
         a login.
      2. Otherwise runs ``argocd login <server> --sso --grpc-web
         --sso-port <ARGOCD_SSO_PORT>`` as a subprocess inside the
         container, scrapes the ``authorization_code`` URL from
         ``argocd``'s stderr, and returns it under ``authorize_url``.
      3. The user opens ``authorize_url`` in their host browser and
         completes SSO. Their browser then hits
         ``http://localhost:<ARGOCD_SSO_PORT>/auth/callback``, which is
         published from the container by
         ``docker run -p <port>:<port>``. ``argocd`` inside the container
         receives the callback, writes the token to the shared
         ``~/.config/argocd/config``, and exits.
      4. Any subsequent tool call succeeds silently — no restart needed.

    Only ONE login flow may be in progress at a time. If one is already
    running, this tool returns the existing ``authorize_url`` so the AI
    can re-surface it to the user.

    Args:
        force: When ``True``, skip the "already logged in" short-circuit
            and start a fresh login even if a valid session exists.

    Returns ``{ok, authorize_url, server, sso_port, instruction,
    login_timeout_seconds}`` on success, or a structured error otherwise.
    """
    if not force:
        st = argocd_status()
        if st.get("ok") and st.get("logged_in"):
            return {
                "ok": True,
                "already_logged_in": True,
                "server": _server(),
                "username": st.get("username"),
                "note": (
                    "Session is already active. Pass `force=True` to start "
                    "a new SSO login anyway."
                ),
            }

    with _LOGIN_LOCK:
        existing = _LOGIN_STATE.get("process")
        if existing is not None and existing.poll() is None:
            url = _LOGIN_STATE.get("sso_url")
            if url:
                return {
                    "ok": True,
                    "authorize_url": url,
                    "server": _LOGIN_STATE.get("server"),
                    "sso_port": _sso_port(),
                    "already_in_progress": True,
                    "instruction": (
                        "A login flow is already running. Open `authorize_url` "
                        "in your browser to complete SSO. When done, retry the "
                        "original tool call."
                    ),
                }
        # No live flow — clean up any completed one, then start fresh.
        _reset_login_state()
        url, err = _start_login_subprocess()
        if err:
            return err

    return {
        "ok": True,
        "authorize_url": url,
        "server": _server(),
        "sso_port": _sso_port(),
        "login_timeout_seconds": _login_timeout(),
        "instruction": (
            "Present `authorize_url` to the user as a clickable link and "
            "STOP. Do NOT poll in a loop. After the user completes SSO in "
            "their browser, ArgoCD writes the token to the shared config "
            "and the next tool call will succeed. To confirm, call "
            "`argocd_status` — but only once, after the user says they've "
            "finished signing in."
        ),
    }


@mcp.tool()
def argocd_login_status() -> dict[str, Any]:
    """Check whether an in-flight ``argocd_login`` flow has completed.

    Returns the state of the most recent ``argocd_login`` invocation:

      * ``state="idle"``          — no login flow has been started this
        process.
      * ``state="pending"``       — argocd login is running and waiting
        for the user's browser callback. ``authorize_url`` is included
        so the AI can re-surface it.
      * ``state="succeeded"``     — argocd login exited 0; the config is
        updated.
      * ``state="failed"``        — argocd login exited non-zero. The
        tail of its output is included under ``output``.

    Use ``argocd_status`` afterwards to double-check the actual session
    (this tool only reports on the subprocess, not on the token).
    """
    proc = _LOGIN_STATE.get("process")
    if proc is None:
        return {"ok": True, "state": "idle"}
    if proc.poll() is None:
        return {
            "ok": True,
            "state": "pending",
            "server": _LOGIN_STATE.get("server"),
            "authorize_url": _LOGIN_STATE.get("sso_url"),
            "elapsed_seconds": (
                round(time.time() - (_LOGIN_STATE.get("started_at") or time.time()), 1)
            ),
            "note": "Waiting for the browser callback. Ask the user to complete SSO.",
        }
    rc = _LOGIN_STATE.get("returncode")
    tail = "\n".join(_LOGIN_STATE.get("output_lines") or [])[-2000:]
    if rc == 0:
        return {
            "ok": True,
            "state": "succeeded",
            "server": _LOGIN_STATE.get("server"),
            "output": tail,
        }
    return {
        "ok": True,
        "state": "failed",
        "server": _LOGIN_STATE.get("server"),
        "returncode": rc,
        "output": tail,
    }


@mcp.tool()
def list_apps(
    project: str = "",
    projects: str = "",
    selector: str = "",
    cluster: str = "",
    namespace: str = "",
    name_pattern: str = "",
    search: str = "",
    repo: str = "",
    limit: int = 500,
) -> dict[str, Any]:
    """List ArgoCD applications. **Read-only.** Fast REST-API path.

    Hits ``GET /api/v1/applications`` directly with **field projection**
    (only the fields this tool returns) plus server-side filters for
    ``projects`` / ``selector`` / ``repo``. Roughly 10-100x faster than
    ``argocd app list -o json`` because we skip the full spec+status
    tree for every app and the CLI's client-side merge / render.

    Falls back to the CLI transparently if the API is unreachable or the
    stored token is missing / expired — same NOT_LOGGED_IN auto-refresh
    path as every other tool.

    Args:
        project: Single project filter (server-side). Alias for one entry
            in ``projects``.
        projects: Comma-separated list of projects to include
            (server-side).
        selector: Kubernetes label selector, e.g. ``"env=prod,team=cab"``
            (server-side).
        cluster: Substring match on the app's destination server URL
            (client-side).
        namespace: Substring match on the app's destination namespace
            (client-side).
        name_pattern: **fnmatch glob** on the app name, case-sensitive
            (client-side). Examples: ``"*preagent*"``,
            ``"apps-outbound-*-u2lo"``. Use this instead of listing all
            apps when you know part of the name.
        search: Case-insensitive substring on the app name (client-side).
            Kept for backward compat — prefer ``name_pattern`` for glob
            semantics.
        repo: Substring match on the source repo URL (server-side).
        limit: Max entries returned (default 500). Applied AFTER filters.

    Returns each match's ``name``, ``project``, ``sync_status``,
    ``health_status``, ``operation_state``, ``dest_server``,
    ``dest_namespace``, ``repo_url``, ``path``, ``target_revision``.
    """
    proj_list: list[str] = []
    if project:
        proj_list.append(project.strip())
    if projects:
        proj_list.extend([p.strip() for p in projects.split(",") if p.strip()])

    params: dict[str, Any] = {"fields": _LIST_APP_FIELDS}
    if proj_list:
        params["projects"] = ",".join(sorted(set(proj_list)))
    if selector:
        params["selector"] = selector
    if repo:
        params["repo"] = repo

    api_error: Optional[dict[str, Any]] = None
    items: list[dict[str, Any]] = []
    try:
        body = _api_get("/api/v1/applications", params=params, timeout=45)
        items = (body or {}).get("items") or []
    except _ArgocdApiError as exc:
        api_error = {"kind": exc.kind, **exc.payload}

    # Fall back to CLI on API failure. Reuses the existing auto-refresh
    # path (`_argocd_error` scrapes NOT_LOGGED_IN and starts SSO).
    if api_error is not None:
        args = ["app", "list", "-o", "json"]
        if project:
            args += ["--project", project]
        if selector:
            args += ["--selector", selector]
        try:
            res = _run_argocd(args, timeout=90)
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": "ARGOCD_TIMEOUT",
                "context": "app list (CLI fallback)",
                "api_error": api_error,
            }
        if res.returncode != 0:
            err = _argocd_error(res, "app list")
            err.setdefault("api_error", api_error)
            return err
        data = _parse_json_or_text(res.stdout)
        if not isinstance(data, list):
            return {
                "ok": False,
                "error": "UNEXPECTED_ARGOCD_OUTPUT",
                "context": "app list",
                "api_error": api_error,
            }
        items = data

    pattern = name_pattern.strip() if name_pattern else ""
    search_lower = (search or "").strip().lower()
    cluster_lower = (cluster or "").strip().lower()
    namespace_lower = (namespace or "").strip().lower()

    entries: list[dict[str, Any]] = []
    for app in items:
        summary = _summarize_app_item(app)
        name = summary.get("name") or ""
        if not _match_app(
            name,
            summary.get("dest_server") or "",
            summary.get("dest_namespace") or "",
            pattern,
            search_lower,
            cluster_lower,
            namespace_lower,
        ):
            continue
        entries.append(summary)
        if len(entries) >= max(1, int(limit)):
            break

    return {
        "ok": True,
        "server": _server(),
        "source": "cli" if api_error else "api",
        "filters": {
            "projects": proj_list or None,
            "selector": selector or None,
            "cluster": cluster or None,
            "namespace": namespace or None,
            "name_pattern": pattern or None,
            "search": search or None,
            "repo": repo or None,
        },
        "count": len(entries),
        "total_before_filter": len(items),
        "apps": entries,
        "api_error": api_error,
    }


@mcp.tool()
def find_apps(
    pattern: str,
    project: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Find apps whose name matches an fnmatch glob. **Read-only.**

    Thin wrapper over ``list_apps`` for the common "I know a piece of
    the name" case, e.g. ``find_apps("*preagent*")``. Same fast REST
    path, same auto-refresh, same output shape as ``list_apps`` (just
    ``pattern`` is required and there are fewer knobs).

    Args:
        pattern: fnmatch glob applied to the app name, case-sensitive.
            Examples: ``"*preagent*"``, ``"apps-outbound-*-u2lo"``,
            ``"mycompany-*"``.
        project: Optional project name to scope the query (server-side).
        limit: Max results (default 100).
    """
    if not pattern or not pattern.strip():
        return {"ok": False, "error": "`pattern` is required."}
    return list_apps(project=project, name_pattern=pattern, limit=limit)


@mcp.tool()
def batch_get_apps(
    names: list[str],
    refresh: bool = False,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Fetch full ``get_app`` details for multiple apps in parallel. **Read-only.**

    Typical use: pipe the ``apps[].name`` list from ``list_apps`` /
    ``find_apps`` back in here to get the full ArgoCD Application state
    for each match without waiting on N sequential CLI calls.

    Args:
        names: List of application names. Empty list returns an empty
            result. Duplicates are de-duplicated.
        refresh: Passed to ``get_app`` for each name — adds
            ``--refresh`` so ArgoCD re-checks the source.
        max_workers: Concurrency cap (default 8, hard-capped at 16 so we
            don't hammer the argocd API).
    """
    unique_names = [n for n in dict.fromkeys(names or []) if isinstance(n, str) and n.strip()]
    if not unique_names:
        return {"ok": True, "server": _server(), "count": 0, "results": {}}

    workers = max(1, min(int(max_workers), 16))
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(get_app, n, refresh): n for n in unique_names}
        for fut in futures:
            n = futures[fut]
            try:
                results[n] = fut.result()
            except Exception as exc:  # defensive — get_app catches its own errors
                results[n] = {"ok": False, "error": "UNEXPECTED", "message": str(exc)}
    ok_count = sum(1 for r in results.values() if r.get("ok"))
    return {
        "ok": True,
        "server": _server(),
        "count": len(results),
        "ok_count": ok_count,
        "results": results,
    }


@mcp.tool()
def get_app(name: str, refresh: bool = False) -> dict[str, Any]:
    """Fetch a single application's full state. **Read-only.**

    Wraps ``argocd app get <name> --grpc-web -o json``. Returns the
    full ArgoCD Application object trimmed to the fields the AI
    typically needs: spec source(s), destination, sync + health, latest
    operation, and the resource tree summary.

    Args:
        name: Application name (e.g. ``"apps-outbound-outbound-preagent-u2lo"``).
        refresh: When ``True``, adds ``--refresh`` so ArgoCD re-checks
            the source before returning.
    """
    if err := _validate_app_name(name):
        return err
    args = ["app", "get", name, "-o", "json"]
    if refresh:
        args.append("--refresh")
    try:
        res = _run_argocd(args, timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ARGOCD_TIMEOUT", "context": f"app get {name}"}
    if res.returncode != 0:
        return _argocd_error(res, f"app get {name}")

    data = _parse_json_or_text(res.stdout)
    if not isinstance(data, dict):
        return {"ok": True, "name": name, "raw": data}
    meta = data.get("metadata") or {}
    spec = data.get("spec") or {}
    status = data.get("status") or {}
    dest = spec.get("destination") or {}
    sources = spec.get("sources") or (
        [spec.get("source")] if spec.get("source") else []
    )
    resources = [
        {
            "kind": r.get("kind"),
            "name": r.get("name"),
            "namespace": r.get("namespace"),
            "status": r.get("status"),
            "health": (r.get("health") or {}).get("status"),
            "message": (r.get("health") or {}).get("message"),
        }
        for r in (status.get("resources") or [])
    ]
    return {
        "ok": True,
        "server": _server(),
        "name": meta.get("name"),
        "namespace_argocd": meta.get("namespace"),
        "project": spec.get("project"),
        "destination": {
            "server": dest.get("server") or dest.get("name"),
            "namespace": dest.get("namespace"),
        },
        "sources": [
            {
                "repo_url": s.get("repoURL"),
                "path": s.get("path"),
                "target_revision": s.get("targetRevision"),
                "chart": s.get("chart"),
                "ref": s.get("ref"),
            }
            for s in sources if s
        ],
        "sync_status": (status.get("sync") or {}).get("status"),
        "sync_revision": (status.get("sync") or {}).get("revision"),
        "health_status": (status.get("health") or {}).get("status"),
        "health_message": (status.get("health") or {}).get("message"),
        "operation_state": {
            "phase": (status.get("operationState") or {}).get("phase"),
            "message": (status.get("operationState") or {}).get("message"),
            "started_at": (status.get("operationState") or {}).get("startedAt"),
            "finished_at": (status.get("operationState") or {}).get("finishedAt"),
        },
        "resource_count": len(resources),
        "resources": resources,
        "conditions": status.get("conditions") or [],
    }


@mcp.tool()
def get_app_manifests(name: str, revision: str = "") -> dict[str, Any]:
    """Fetch the rendered manifests for an app. **Read-only.**

    Wraps ``argocd app manifests <name> --grpc-web``. Returns the raw
    multi-document YAML/JSON stream ArgoCD would apply. Optional
    ``revision`` targets a specific git ref.
    """
    if err := _validate_app_name(name):
        return err
    args = ["app", "manifests", name]
    if revision:
        args += ["--revision", revision]
    try:
        res = _run_argocd(args, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ARGOCD_TIMEOUT", "context": f"app manifests {name}"}
    if res.returncode != 0:
        return _argocd_error(res, f"app manifests {name}")
    text = res.stdout or ""
    return {
        "ok": True,
        "name": name,
        "revision": revision or None,
        "manifests": text,
        "size_bytes": len(text),
    }


@mcp.tool()
def get_app_resources(name: str) -> dict[str, Any]:
    """List resources managed by an app. **Read-only.**

    Wraps ``argocd app resources <name> --grpc-web``. Cleaner than
    ``get_app`` when you only want the resource tree.
    """
    if err := _validate_app_name(name):
        return err
    try:
        res = _run_argocd(["app", "resources", name], timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ARGOCD_TIMEOUT", "context": f"app resources {name}"}
    if res.returncode != 0:
        return _argocd_error(res, f"app resources {name}")
    return {"ok": True, "name": name, "resources_text": res.stdout}


@mcp.tool()
def get_app_history(name: str) -> dict[str, Any]:
    """Show an app's deployment history. **Read-only.**

    Wraps ``argocd app history <name> --grpc-web -o json``. Returns the
    list of prior sync revisions with timestamps + git revisions.
    """
    if err := _validate_app_name(name):
        return err
    try:
        res = _run_argocd(["app", "history", name, "-o", "json"], timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ARGOCD_TIMEOUT", "context": f"app history {name}"}
    if res.returncode != 0:
        return _argocd_error(res, f"app history {name}")
    data = _parse_json_or_text(res.stdout)
    return {"ok": True, "name": name, "history": data}


@mcp.tool()
def get_app_logs(
    name: str,
    container: str = "",
    kind: str = "",
    group: str = "",
    resource_name: str = "",
    resource_namespace: str = "",
    since_seconds: int = 0,
    tail: int = 200,
    follow: bool = False,
) -> dict[str, Any]:
    """Fetch pod logs for an app **proxied through argocd**. **Read-only.**

    Same source as the argocd UI's Logs tab. **argocd-server holds the
    cluster credentials and reads the logs on your behalf — no kubectl,
    kubeconfig, IAM, or EKS access is required.** If this tool returns
    empty output it means there are no logs to show (e.g. the pod
    never scheduled), NOT that Kubernetes access is missing.

    Wraps ``argocd app logs <name> --grpc-web``. Streaming (``follow``)
    is NOT supported through this tool because MCP tool calls are
    one-shot — ``follow=True`` is ignored (documented so the AI knows).

    Args:
        name: Application name.
        container: Container name inside the pod (optional).
        kind: Kubernetes resource kind to fetch logs from. Argocd
            resolves the kind to the underlying pods, so this works for
            ``Pod`` (default when unset), ``Deployment``, ``Rollout``
            (Argo Rollouts), ``StatefulSet``, ``ReplicaSet``, ``Job``,
            ``DaemonSet``, etc.
        group: API group of the resource (e.g. ``"apps"`` for
            Deployment/StatefulSet, ``"argoproj.io"`` for Rollout).
            Empty for core resources like Pod.
        resource_name: Specific K8s resource name to scope the query
            (e.g. a specific pod name from ``get_app_resources``).
        resource_namespace: K8s namespace of the resource.
        since_seconds: Relative log window (e.g. ``600`` = last 10min).
        tail: Max lines (default 200).
        follow: Ignored — one-shot only.

    Empty result? The pod may not exist (never scheduled). Call
    ``get_app_events`` or ``get_app_resource_events`` to see why.
    """
    if err := _validate_app_name(name):
        return err
    args = ["app", "logs", name]
    if container:
        args += ["--container", container]
    if kind:
        args += ["--kind", kind]
    if group:
        args += ["--group", group]
    if resource_name:
        args += ["--name", resource_name]
    if resource_namespace:
        args += ["--namespace", resource_namespace]
    if since_seconds and since_seconds > 0:
        args += ["--since-seconds", str(int(since_seconds))]
    if tail and tail > 0:
        args += ["--tail", str(int(tail))]
    try:
        res = _run_argocd(args, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ARGOCD_TIMEOUT", "context": f"app logs {name}"}
    if res.returncode != 0:
        return _argocd_error(res, f"app logs {name}")
    text = res.stdout or ""
    return {
        "ok": True,
        "name": name,
        "container": container or None,
        "tail": tail,
        "since_seconds": since_seconds or None,
        "size_bytes": len(text),
        "follow_ignored": bool(follow),
        "logs": text,
    }


def _summarize_events(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim a Kubernetes EventList response down to the AI-useful bits."""
    out: list[dict[str, Any]] = []
    for ev in items or []:
        meta = ev.get("metadata") or {}
        involved = ev.get("involvedObject") or {}
        source = ev.get("source") or {}
        out.append({
            "type": ev.get("type"),
            "reason": ev.get("reason"),
            "message": ev.get("message"),
            "count": ev.get("count"),
            "first_seen": ev.get("firstTimestamp") or ev.get("eventTime"),
            "last_seen": ev.get("lastTimestamp") or meta.get("creationTimestamp"),
            "involved": {
                "kind": involved.get("kind"),
                "name": involved.get("name"),
                "namespace": involved.get("namespace"),
                "uid": involved.get("uid"),
                "field_path": involved.get("fieldPath"),
            },
            "source": source.get("component") or source.get("host"),
            "reporting_controller": ev.get("reportingComponent"),
            "action": ev.get("action"),
        })
    return out


@mcp.tool()
def get_app_events(name: str, limit: int = 200) -> dict[str, Any]:
    """Fetch application-level events for an app **proxied through argocd**. **Read-only.**

    Wraps ``GET /api/v1/applications/{name}/events``. Same source as
    the argocd UI's Events tab on the application. **argocd-server
    holds the cluster credentials and reads the events on your behalf
    — no kubectl, kubeconfig, IAM, or EKS access is required.**

    Use this to see why an app is stuck (sync failures, health probes,
    scheduling problems on the app's owned resources). For events
    scoped to a SPECIFIC K8s object inside the app (e.g. a specific
    pod's ``FailedScheduling`` events), call
    ``get_app_resource_events`` instead.

    Args:
        name: Application name.
        limit: Max events returned (default 200). Applied after fetch.
    """
    if err := _validate_app_name(name):
        return err
    try:
        body = _api_get(
            f"/api/v1/applications/{quote(name, safe='')}/events",
            timeout=45,
        )
    except _ArgocdApiError as exc:
        return _api_error_response(exc, f"app events {name}")
    items = (body or {}).get("items") or []
    events = _summarize_events(items)[: max(1, int(limit))]
    return {
        "ok": True,
        "server": _server(),
        "name": name,
        "count": len(events),
        "total": len(items),
        "events": events,
    }


@mcp.tool()
def get_app_resource_events(
    name: str,
    resource_name: str,
    resource_namespace: str,
    kind: str = "Pod",
    group: str = "",
    version: str = "v1",
    limit: int = 200,
) -> dict[str, Any]:
    """Fetch Kubernetes events for a specific resource managed by an app. **Read-only.**

    Wraps ``GET /api/v1/applications/{name}/resource/events``. Same
    source as the argocd UI's Events tab on an individual resource
    (Pod, Deployment, Rollout, ReplicaSet, ...). **Proxied through
    argocd-server — no kubectl / kubeconfig / IAM / EKS required.**

    This is the answer for "why isn't this pod running?", "why did
    Rollout X fail?", "what does ``FailedScheduling`` say?" — the same
    detail the argocd UI shows on the Events tab, delivered as
    structured data.

    Args:
        name: Application name (e.g. ``"aai-aaimock-server-grpc-do"``).
        resource_name: Specific K8s resource name
            (e.g. ``"aai-aaimock-server-grpc-do-69f588976f-8xbgv"``).
            Get these from ``get_app`` / ``get_app_resources``.
        resource_namespace: Resource's K8s namespace (e.g. ``"aai"``).
        kind: K8s Kind of the resource. Common values: ``"Pod"``
            (default), ``"Deployment"``, ``"ReplicaSet"``, ``"Rollout"``,
            ``"StatefulSet"``, ``"Service"``, ``"Ingress"``.
        group: API group. Empty for core (Pod, Service, ConfigMap,
            ...). ``"apps"`` for Deployment/StatefulSet/ReplicaSet/
            DaemonSet. ``"argoproj.io"`` for Rollout / Application.
        version: API version. ``"v1"`` (default) for most; ``"v1alpha1"``
            for Rollout.
        limit: Max events returned (default 200).
    """
    if err := _validate_app_name(name):
        return err
    if not resource_name or not resource_name.strip():
        return {"ok": False, "error": "`resource_name` is required."}
    if not resource_namespace or not resource_namespace.strip():
        return {"ok": False, "error": "`resource_namespace` is required."}
    if not kind or not kind.strip():
        return {"ok": False, "error": "`kind` is required."}

    params = {
        "namespace": resource_namespace,
        "resourceName": resource_name,
        "kind": kind,
        "group": group,
        "version": version or "v1",
    }
    try:
        body = _api_get(
            f"/api/v1/applications/{quote(name, safe='')}/resource/events",
            params=params,
            timeout=45,
        )
    except _ArgocdApiError as exc:
        return _api_error_response(exc, f"app resource events {name}/{kind}/{resource_name}")
    items = (body or {}).get("items") or []
    events = _summarize_events(items)[: max(1, int(limit))]
    return {
        "ok": True,
        "server": _server(),
        "name": name,
        "resource": {
            "kind": kind,
            "name": resource_name,
            "namespace": resource_namespace,
            "group": group or None,
            "version": version or "v1",
        },
        "count": len(events),
        "total": len(items),
        "events": events,
    }


@mcp.tool()
def get_resource(
    name: str,
    resource_name: str,
    resource_namespace: str,
    kind: str,
    group: str = "",
    version: str = "v1",
) -> dict[str, Any]:
    """Fetch the LIVE Kubernetes manifest of a resource managed by an app. **Read-only.**

    Wraps ``GET /api/v1/applications/{name}/resource``. Same source as
    the argocd UI's **Live Manifest** view on any resource card.
    **Proxied through argocd-server — no kubectl / kubeconfig / IAM /
    EKS required.**

    Works for **any** K8s kind argocd can see through the destination
    cluster: ``Pod``, ``Deployment``, ``ReplicaSet``, ``StatefulSet``,
    ``DaemonSet``, ``Job``, ``CronJob``, ``Service``, ``Ingress``,
    ``VirtualService`` (Istio), ``DestinationRule``, ``ConfigMap``,
    ``ServiceAccount``, ``Role``, ``RoleBinding``, ``HorizontalPodAutoscaler``,
    ``PodDisruptionBudget``, ``NetworkPolicy``, ``ExternalSecret``,
    ``PrometheusRule``, ``ServiceMonitor``, ``Rollout``
    (``argoproj.io/v1alpha1``), and any CRD the cluster hosts.

    Note on secrets: argocd redacts the ``data`` block of ``Secret``
    kinds server-side, so this returns metadata only for those.

    Args:
        name: Application name.
        resource_name: K8s resource name (from ``get_app`` /
            ``get_resource_tree`` / ``get_app_resources``).
        resource_namespace: K8s namespace of the resource.
        kind: K8s Kind (e.g. ``"Pod"``, ``"Deployment"``, ``"Rollout"``).
        group: API group. Empty for core kinds (Pod, Service, ConfigMap,
            Secret, ServiceAccount, ...). ``"apps"`` for
            Deployment/StatefulSet/ReplicaSet/DaemonSet. ``"argoproj.io"``
            for Rollout / Application. ``"networking.k8s.io"`` for
            Ingress. ``"networking.istio.io"`` for VirtualService /
            DestinationRule.
        version: API version. ``"v1"`` (default) for most; ``"v1alpha1"``
            for Rollout; ``"v2"`` for HPA (autoscaling/v2).

    Returns ``{ok, name, resource, manifest}`` where ``manifest`` is the
    parsed JSON of the live object (spec, status, metadata — everything).
    """
    if err := _validate_app_name(name):
        return err
    if not resource_name or not resource_name.strip():
        return {"ok": False, "error": "`resource_name` is required."}
    if not resource_namespace or not resource_namespace.strip():
        return {"ok": False, "error": "`resource_namespace` is required."}
    if not kind or not kind.strip():
        return {"ok": False, "error": "`kind` is required."}

    params = {
        "namespace": resource_namespace,
        "resourceName": resource_name,
        "kind": kind,
        "group": group,
        "version": version or "v1",
    }
    try:
        body = _api_get(
            f"/api/v1/applications/{quote(name, safe='')}/resource",
            params=params,
            timeout=45,
        )
    except _ArgocdApiError as exc:
        return _api_error_response(exc, f"resource {name}/{kind}/{resource_name}")

    # argocd returns {"manifest": "<JSON-or-YAML string>"} — parse it so
    # the AI gets structured data instead of a blob.
    raw = (body or {}).get("manifest")
    manifest: Any = None
    if isinstance(raw, str):
        try:
            manifest = json.loads(raw)
        except (ValueError, TypeError):
            try:
                manifest = yaml.safe_load(raw)
            except yaml.YAMLError:
                manifest = raw
    else:
        manifest = raw

    return {
        "ok": True,
        "server": _server(),
        "name": name,
        "resource": {
            "kind": kind,
            "name": resource_name,
            "namespace": resource_namespace,
            "group": group or None,
            "version": version or "v1",
        },
        "manifest": manifest,
    }


def _summarize_tree_node(node: dict[str, Any]) -> dict[str, Any]:
    parent_refs = [
        {
            "kind": p.get("kind"),
            "group": p.get("group"),
            "name": p.get("name"),
            "namespace": p.get("namespace"),
            "uid": p.get("uid"),
        }
        for p in (node.get("parentRefs") or [])
    ]
    net = node.get("networkingInfo") or {}
    return {
        "kind": node.get("kind"),
        "group": node.get("group") or "",
        "version": node.get("version") or "v1",
        "name": node.get("name"),
        "namespace": node.get("namespace"),
        "uid": node.get("uid"),
        "health": (node.get("health") or {}).get("status"),
        "health_message": (node.get("health") or {}).get("message"),
        "created_at": node.get("createdAt"),
        "resource_version": node.get("resourceVersion"),
        "images": node.get("images"),
        "info": node.get("info"),
        "networking": {
            "target_labels": net.get("targetLabels"),
            "target_refs": net.get("targetRefs"),
            "external_urls": net.get("externalURLs"),
            "ingress": net.get("ingress"),
        } if net else None,
        "parent_refs": parent_refs or None,
    }


@mcp.tool()
def get_resource_tree(name: str) -> dict[str, Any]:
    """Fetch the full resource tree for an app **proxied through argocd**. **Read-only.**

    Wraps ``GET /api/v1/applications/{name}/resource-tree``. Same source
    as the argocd UI's tree / graph view. Returns EVERY live object
    argocd owns for the app — the top-level resources declared in git
    plus every child argocd discovered through ``ownerReferences``
    (Deployment → ReplicaSet → Pod, Rollout → ReplicaSet → Pod,
    Service → Endpoints, Ingress, HPA, etc.).

    **No kubectl / kubeconfig / IAM / EKS required.** This is the
    single fastest way to answer "what does argocd actually see for
    this app?" — including pod-level detail, images, ingress URLs,
    health per node, and the parent-child links you'd draw manually.

    Feed a node's ``kind`` / ``name`` / ``namespace`` / ``group`` /
    ``version`` back into ``get_resource``, ``get_app_logs``, or
    ``get_app_resource_events`` for deeper inspection.

    Args:
        name: Application name.
    """
    if err := _validate_app_name(name):
        return err
    try:
        body = _api_get(
            f"/api/v1/applications/{quote(name, safe='')}/resource-tree",
            timeout=45,
        )
    except _ArgocdApiError as exc:
        return _api_error_response(exc, f"resource-tree {name}")

    nodes = [_summarize_tree_node(n) for n in (body or {}).get("nodes") or []]
    orphaned = [_summarize_tree_node(n) for n in (body or {}).get("orphanedNodes") or []]
    hosts = [
        {
            "name": h.get("name"),
            "system_info": h.get("systemInfo"),
            "resources_info": h.get("resourcesInfo"),
        }
        for h in (body or {}).get("hosts") or []
    ]
    # Kind histogram makes it easy for the AI to know at a glance what
    # kinds of resources this app owns.
    kind_counts: dict[str, int] = {}
    health_counts: dict[str, int] = {}
    for n in nodes:
        k = n.get("kind") or "Unknown"
        kind_counts[k] = kind_counts.get(k, 0) + 1
        h = n.get("health") or "Unknown"
        health_counts[h] = health_counts.get(h, 0) + 1
    return {
        "ok": True,
        "server": _server(),
        "name": name,
        "node_count": len(nodes),
        "orphaned_count": len(orphaned),
        "kind_counts": kind_counts,
        "health_counts": health_counts,
        "nodes": nodes,
        "orphaned_nodes": orphaned,
        "hosts": hosts,
    }


@mcp.tool()
def app_diff(name: str, revision: str = "") -> dict[str, Any]:
    """Show diff between live cluster state and target manifests. **Read-only.**

    Wraps ``argocd app diff <name> --grpc-web``. Non-zero return codes
    are expected here — argocd returns 1 when there IS a diff. We treat
    any output as success.
    """
    if err := _validate_app_name(name):
        return err
    args = ["app", "diff", name]
    if revision:
        args += ["--revision", revision]
    try:
        res = _run_argocd(args, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ARGOCD_TIMEOUT", "context": f"app diff {name}"}
    stdout = res.stdout or ""
    stderr = res.stderr or ""
    # Session errors still bubble up as failures.
    if res.returncode != 0 and not stdout and "context deadline exceeded" not in stderr.lower():
        err = _argocd_error(res, f"app diff {name}")
        if err.get("error") in ("NOT_LOGGED_IN", "ARGOCD_CLI_ERROR") and stdout == "":
            return err
    return {
        "ok": True,
        "name": name,
        "revision": revision or None,
        "has_diff": bool(stdout.strip()),
        "diff": stdout,
        "stderr": stderr.strip() or None,
    }


@mcp.tool()
def list_clusters() -> dict[str, Any]:
    """List clusters registered with ArgoCD. **Read-only.**"""
    try:
        res = _run_argocd(["cluster", "list", "-o", "json"], timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ARGOCD_TIMEOUT", "context": "cluster list"}
    if res.returncode != 0:
        return _argocd_error(res, "cluster list")
    data = _parse_json_or_text(res.stdout)
    if not isinstance(data, list):
        return {"ok": True, "server": _server(), "raw": data}
    entries = [
        {
            "name": c.get("name"),
            "server": c.get("server"),
            "connection_status": (c.get("connectionState") or {}).get("status"),
            "connection_message": (c.get("connectionState") or {}).get("message"),
            "server_version": c.get("serverVersion"),
            "namespaces": c.get("namespaces"),
            "labels": c.get("labels"),
        }
        for c in data
    ]
    return {"ok": True, "server": _server(), "count": len(entries), "clusters": entries}


@mcp.tool()
def list_projects() -> dict[str, Any]:
    """List ArgoCD projects. **Read-only.**"""
    try:
        res = _run_argocd(["proj", "list", "-o", "json"], timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ARGOCD_TIMEOUT", "context": "proj list"}
    if res.returncode != 0:
        return _argocd_error(res, "proj list")
    data = _parse_json_or_text(res.stdout)
    if not isinstance(data, list):
        return {"ok": True, "server": _server(), "raw": data}
    entries = [
        {
            "name": (p.get("metadata") or {}).get("name"),
            "description": (p.get("spec") or {}).get("description"),
            "source_repos": (p.get("spec") or {}).get("sourceRepos"),
            "destinations": (p.get("spec") or {}).get("destinations"),
        }
        for p in data
    ]
    return {"ok": True, "server": _server(), "count": len(entries), "projects": entries}


@mcp.tool()
def list_repos() -> dict[str, Any]:
    """List repositories registered with ArgoCD. **Read-only.**"""
    try:
        res = _run_argocd(["repo", "list", "-o", "json"], timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ARGOCD_TIMEOUT", "context": "repo list"}
    if res.returncode != 0:
        return _argocd_error(res, "repo list")
    data = _parse_json_or_text(res.stdout)
    if not isinstance(data, list):
        return {"ok": True, "server": _server(), "raw": data}
    entries = [
        {
            "repo": r.get("repo"),
            "type": r.get("type"),
            "name": r.get("name"),
            "connection_status": (r.get("connectionState") or {}).get("status"),
            "connection_message": (r.get("connectionState") or {}).get("message"),
        }
        for r in data
    ]
    return {"ok": True, "server": _server(), "count": len(entries), "repos": entries}


@mcp.tool()
def argocd_cli(command: str, timeout: int = 120) -> dict[str, Any]:
    """Freestyle **read-only** ``argocd`` CLI passthrough.

    Escape hatch for read verbs the specialized tools don't cover.
    ``command`` is the argocd verb chain WITHOUT the leading ``argocd``
    binary and WITHOUT ``--server`` / ``--grpc-web`` (both are injected
    automatically).

    Only subcommands in the read-only allow-list are permitted. Explicit
    write verbs (``app sync``, ``app rollback``, ``app delete``,
    ``cluster add``, ``proj create``, ``repo add``, ``login`` / ``logout``,
    ...) are rejected with ``error="WRITE_COMMAND_REFUSED"``. Use the
    matching first-class tool (or ``argocd_login``) for supported flows.

    ``command`` is shlex-split; single quotes and shell metacharacters
    (``|``, ``;``, ``$(...)``, backticks, ``&&``) are refused.

    Examples::

        argocd_cli(command="app get apps-outbound-outbound-preagent-u2lo -o wide")
        argocd_cli(command="cluster get https://kubernetes.default.svc -o yaml")
        argocd_cli(command="proj get outbound -o yaml")
        argocd_cli(command="version --client")
    """
    if not command or not command.strip():
        return {"ok": False, "error": "`command` is required."}
    if any(ch in command for ch in ("|", ";", "$(", "`", "&&", ">>", "<<")):
        return {
            "ok": False,
            "error": "SHELL_METACHARACTER_REFUSED",
            "message": (
                "Shell metacharacters are not allowed in `command`. Pass "
                "only argocd flags and positional args."
            ),
        }
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return {"ok": False, "error": f"Could not parse `command`: {exc}"}
    if not parts:
        return {"ok": False, "error": "`command` is empty after parsing."}
    if parts[0] == "argocd":
        parts = parts[1:]
    if not parts:
        return {"ok": False, "error": "`command` is empty after stripping `argocd`."}

    # Match the longest allow-listed prefix (e.g. "app get", "proj role list").
    verb = " ".join(parts[:2]).lower()
    verb3 = " ".join(parts[:3]).lower()
    matched: Optional[str] = None
    for candidate in (verb3, verb, parts[0].lower()):
        if candidate in _READ_ONLY_CLI_ALLOWLIST:
            matched = candidate
            break

    if matched is None:
        if verb in _WRITE_CLI_DENYLIST or parts[0].lower() in _WRITE_CLI_DENYLIST:
            return {
                "ok": False,
                "error": "WRITE_COMMAND_REFUSED",
                "verb": verb,
                "message": (
                    f"`argocd {verb}` is a write / mutating command and is "
                    "not exposed by this MCP. This server is read-only. "
                    "Use `argocd_login` for the login flow if needed."
                ),
            }
        return {
            "ok": False,
            "error": "UNKNOWN_OR_UNSUPPORTED_VERB",
            "verb": verb,
            "message": (
                f"`argocd {verb}` is not on the read-only allow-list. "
                f"Allowed verbs: {', '.join(sorted(_READ_ONLY_CLI_ALLOWLIST))}."
            ),
        }

    # Reject any embedded --server / --grpc-web overrides — we always inject ours.
    for arg in parts:
        if arg in ("--server", "--grpc-web"):
            return {
                "ok": False,
                "error": "RESERVED_FLAG",
                "flag": arg,
                "message": (
                    f"Do not pass `{arg}` in `command`; it's injected "
                    "automatically."
                ),
            }

    timeout = max(5, min(int(timeout), 300))
    try:
        res = _run_argocd(parts, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "ARGOCD_TIMEOUT",
            "context": f"argocd {' '.join(parts)}",
            "timeout_seconds": timeout,
        }
    if res.returncode != 0:
        # `app diff` legitimately returns non-zero when there's a diff.
        if matched == "app diff" and res.stdout:
            return {
                "ok": True,
                "verb": matched,
                "command": " ".join(parts),
                "stdout": res.stdout,
                "stderr": res.stderr,
                "returncode": res.returncode,
            }
        return _argocd_error(res, f"argocd {' '.join(parts)}")
    return {
        "ok": True,
        "verb": matched,
        "command": " ".join(parts),
        "stdout": res.stdout,
        "stderr": res.stderr or None,
        "returncode": res.returncode,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")

