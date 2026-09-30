# GitHub MCP Server

**Strictly read-only** source-inspection MCP server. Two tiers of tools live
here side by side:

1. **myorgname2 component tools** — the original set, hard-scoped to the
   `myorgname2` org (or whatever `GITHUB_ORG` is set to). Companion to
   `mcp-gitlab`: when a CloudWatch / Loki error points at a specific
   deployed version, use these to view the source at that tag.
2. **Generic multi-org tools** — take an explicit `org` argument so the
   same token can scan `myorgname1`, `myorgname3`, `myorgname4`, or any
   other org the token can see. No org list is hardcoded; call
   `list_organizations` to discover them.

No tool ever writes to GitHub. The `GITHUB_TOKEN` only needs
`contents:read` / `metadata:read` (plus `read:org`, `pull_requests:read`,
`issues:read` for the corresponding multi-org tools, and
`actions:read` / `checks:read` for the Actions / Checks tools).

## SAML SSO — the first-time-per-org dance

MyCompany orgs (`myorgname1`, `myorgname3`, …) enforce SAML SSO on personal
access tokens. The first time this server touches such an org, GitHub
returns `403` with an `X-GitHub-SSO: required; url=…` header. Instead of
throwing, every tool detects this and returns:

```jsonc
{
  "ok": false,
  "org": "myorgname1",
  "error": "SAML_SSO_AUTHORIZATION_REQUIRED",
  "authorize_url": "https://github.com/orgs/myorgname1/sso?authorization_request=...",
  "org_sso_page": "https://github.com/orgs/myorgname1/sso",
  "message": "…click Authorize next to this token, then retry the same tool call…",
  "instruction": "Present authorize_url to the user as a clickable link and STOP…"
}
```

The AI host must surface `authorize_url`, wait for the user to click it
and complete the SSO redirect, then retry the same tool call. No server
restart is needed — the same token is re-authorized on GitHub's side.

## myorgname2 naming convention (component tools only)

Repo names follow the GitLab side minus the `-config` suffix:
gitlab `scheduling-config` → github `scheduling`. AWS-side names like
`emailgateway` are mapped to their repo-side counterpart (`email_gateway`)
automatically. When a component can't be matched, the tool falls back to
fuzzy-matching against the whole org and returns the closest names as
`candidates` so the AI has to disambiguate with you.

## Tools

### myorgname2 component tools (org fixed by `GITHUB_ORG`)

| Tool | What it does |
|------|--------------|
| `find_component_repo` | Resolve a component name to a myorgname2 repo, with fuzzy-matched suggestions on failure. |
| `list_component_tags` | List tags on the repo (with optional search filter). Preferred over branches when inspecting deployed versions. |
| `list_component_branches` | List branches on the repo. |
| `resolve_component_version` | Map a version string (e.g. `27.0.51`) to the matching tag + commit SHA. Handles `v`-prefixed and substring tag names. |
| `get_component_file` | Fetch a single file at a specific ref (tag / branch / commit SHA). Refuses binary files. |
| `list_component_tree` | List files/directories under a path at a specific ref (optionally recursive). |
| `search_component_code` | GitHub code search inside the repo. **Runs against the default branch only** — use `resolve_component_version` + `get_component_file` afterward to view a match at a specific deployed tag. |
| `check_for_updates` | On-demand release check for this image (via shared `version_notifier`). |

### Generic multi-org tools (explicit `org` argument)

| Tool | What it does |
|------|--------------|
| `list_organizations` | List every org the token can see (`/user/orgs`). Requires `read:org`. |
| `check_org_access` | Verify token access to a specific org. Surfaces `SAML_SSO_AUTHORIZATION_REQUIRED` with `authorize_url` when needed. |
| `list_org_repos` | List repos in an org with optional substring search and cursor pagination. |
| `search_repos` | GitHub-side repo search (`/search/repositories`), scoped to an org when given. |
| `get_repo` | Repo metadata (default branch, visibility, topics, timestamps). |
| `list_repo_branches` | List branches on `org/repo`, with search + pagination. |
| `list_repo_tags` | List tags on `org/repo`, with search + pagination. |
| `get_repo_file` | Fetch a single file at a ref from `org/repo`. Refuses binary. |
| `list_repo_tree` | List a directory at a ref, or walk the full tree recursively with cursor pagination. |
| `list_repo_pull_requests` | List PRs with state / base / head / title-search filters. |
| `get_pull_request` | PR details, optionally with unified diff (`include_diff=True`). |
| `list_repo_issues` | List issues (PRs filtered out) with state / labels / title-search filters. |
| `get_issue` | Single-issue details. |
| `compare_refs` | Diff two refs (`/compare/base...head`); commit list + per-file stats, optionally with unified diff. |
| `search_repo_code` | Code search inside `org/repo` (default branch only, same GitHub limitation). |

> GitHub's code search API only searches the **default branch**. To inspect a match at a specific deployed version, ask the AI to follow up with `resolve_component_version` + `get_component_file` (myorgname2) or `get_repo_file` (multi-org).

### GitHub Actions / Checks tools (explicit `org` argument)

Requires `actions:read` + `checks:read` on the token, on top of the
scopes already needed by the multi-org tools.

| Tool | What it does |
|------|--------------|
| `get_pr_checks` | One-shot rollup of Checks + combined commit status for a PR's head SHA. Returns `rollup` counts, a `failed` list of check runs, in-progress runs, and the classic combined status. Set `include_success=True` to also get passing runs. |
| `list_check_runs` | Raw check-runs list for any commit `ref` (SHA / branch / tag) with optional `status` and `check_name` filters. |
| `list_repo_workflows` | List Actions workflows defined under `.github/workflows`. |
| `list_workflow_runs` | List workflow runs with optional `workflow` (id or filename), `branch`, `event`, `status`, `actor`, `head_sha` filters. |
| `get_workflow_run` | Details for a single workflow run (attempt info, referenced workflows, linked PRs, jobs URL). |
| `list_workflow_run_jobs` | Jobs for a workflow run, `filter=latest` (default) or `all` attempts. Includes per-step status so you can see which step failed. |
| `get_workflow_job` | Single job with its steps. |
| `get_workflow_job_logs` | Plain-text job logs, size-capped via `max_bytes` (default 200 KB). `tail=True` (default) keeps the LAST N bytes — where CI failures live. Bearer token is stripped before hitting the log-storage backend. |

Typical failure-diagnosis chain:

```
get_pr_checks(org, repo, number)
  → pick a failed check → its details_url points at an Actions run
list_workflow_runs(..., head_sha=<PR head SHA>)   # or read run_id from the URL
list_workflow_run_jobs(org, repo, run_id)
  → find the job whose conclusion is "failure"
get_workflow_job_logs(org, repo, job_id, tail=True)
```

### Narrow, confirm-gated writes

**Segregated auth** — every write tool below uses `GITHUB_WRITE_TOKEN`,
NOT `GITHUB_TOKEN`. If `GITHUB_WRITE_TOKEN` is unset the tool returns
`error = "GITHUB_WRITE_TOKEN_NOT_SET"` and reads keep working.

**Two-call confirm gate** — every write tool takes `confirm: bool = False`.
Default returns a preview (method, endpoint, payload, plain-english
`effect`) without touching GitHub. Only `confirm=True` executes. The AI
host is expected to show `effect` to the user and get explicit
confirmation before re-invoking.

**Owner guard** — the write tools only act on repos whose owner matches
the write token's own login (looked up via `/user` and cached in-process).
Any call with a different `owner` returns `error = "OWNER_NOT_ALLOWED"`
and the write is refused. Both preview and execute paths are guarded — a
blocked owner never even gets a preview.

| Tool | HTTP | Effect |
|------|------|--------|
| `whoami()` | `GET /user` (read token) | Returns the authenticated user's login — call before writes to confirm identity. |
| `list_repo_collaborators(owner, repo)` | `GET /repos/{owner}/{repo}/collaborators` (read) | Current collaborators. |
| `list_repo_invitations(owner, repo)` | `GET /repos/{owner}/{repo}/invitations` (read) | Pending invites + their `invitation_id`. |
| `invite_repo_collaborator(owner, repo, username, permission="pull", confirm=False)` | `PUT /repos/{owner}/{repo}/collaborators/{username}` | Sends email invite. Permissions: `pull` / `triage` / `push` / `maintain` / `admin`. |
| `cancel_repo_invitation(owner, repo, invitation_id, confirm=False)` | `DELETE /repos/{owner}/{repo}/invitations/{invitation_id}` | Cancels a pending invite. |
| `remove_repo_collaborator(owner, repo, username, confirm=False)` | `DELETE /repos/{owner}/{repo}/collaborators/{username}` | Revokes access immediately. |
| `set_repo_visibility(owner, repo, visibility, confirm=False)` | `PATCH /repos/{owner}/{repo}` | Flips `public` / `private` / `internal`. HIGH-impact when going private → public — no cooldown, no undo. |

**Scope**: intended for repos where the token owner is a full admin
(typically: repos you own). Org-owned repos will usually reject writes
without org-admin rights. Org-scoped admin writes (repo transfer,
org-package visibility, org settings, ...) are deliberately NOT here.

**Packages**: user-owned GHCR / npm / maven / etc. packages don't have
"invite user" semantics via REST. To grant a colleague pull-access to a
user-owned private package: (1) in the GitHub UI, link the package to a
repo you own via Package Settings → "Connect Repository"; (2) set the
package's access to "Inherit access from the repository"; (3) use
`invite_repo_collaborator` on that repo — pull-access to the package
flows through automatically.

Examples:

```
Give octocat pull access to my mcp-gitlab package
  → whoami()   # confirm token owner
  → list_repo_collaborators(owner="<your-github-login>", repo="<your-repo>")   # already there?
  → invite_repo_collaborator(owner="<your-github-login>", repo="<your-repo>",
     username="octocat", permission="pull")
     # returns preview; show `effect` to the user
  → invite_repo_collaborator(..., confirm=True)   # after user confirms
```
```
Cancel a stale invite
  → list_repo_invitations(owner="<your-github-login>", repo="<your-repo>")
  → cancel_repo_invitation(owner="<your-github-login>", repo="<your-repo>",
     invitation_id=<id>, confirm=True)
```
```
Flip a repo private (safer direction)
  → set_repo_visibility(owner="<your-github-login>", repo="<your-repo>", visibility="private")
  → set_repo_visibility(..., confirm=True)
```
```
Flip a repo public (HIGH-IMPACT — read the preview's WARNING carefully)
  → set_repo_visibility(owner="<your-github-login>", repo="<your-repo>", visibility="public")
  → # only after explicit user confirmation:
  → set_repo_visibility(..., confirm=True)
```

### Pagination cursor

Tools that can return long lists (`list_org_repos`, `list_repo_branches`,
`list_repo_tags`, `list_repo_tree` recursive, `list_repo_pull_requests`,
`list_repo_issues`, `search_repos`, `search_repo_code`,
`list_check_runs`, `list_repo_workflows`, `list_workflow_runs`,
`list_workflow_run_jobs`) return `next_page_token` when there are more
results. Pass the token back verbatim on the next call as `page_token`
to fetch the next slice. The token is an opaque base64-JSON blob —
treat it as a string.

## Configuration

| Env var | Purpose |
|---------|---------|
| `GITHUB_ORG` | **Default org for the myorgname2 component tools** (default `myorgname2`). Ignored by the multi-org tools. |
| `GITHUB_BASE_URL` | GitHub API base URL (default `https://api.github.com`). |
| `GITHUB_TOKEN` | PAT with `contents:read` + `metadata:read` (+ `read:org`, `pull_requests:read`, `issues:read` for multi-org, + `actions:read`, `checks:read` for Actions / Checks tools). Required — reads use this. Inject via your AI host MCP config. |
| `GITHUB_WRITE_TOKEN` | Optional. **Separate** PAT that enables the tier-4 write tools. Fine-grained scopes: `Administration: R/W` (visibility) + `Metadata: R`. Classic: `repo`. If unset, write tools return `GITHUB_WRITE_TOKEN_NOT_SET` and reads keep working. Reads and writes are kept on different tokens on purpose. |
| `GITHUB_VERIFY_SSL` | `true`/`false` (default `false` for the LV VPN cert-intercept case). |

## Example prompts

### Read-only source inspection (myorgname2)

```
Find the myorgname2 repo for scheduling
```
```
List tags for the scheduling repo matching 27.0
```
```
What tag matches version 27.0.51 for scheduling on GitHub?
```
```
Show me src/main/java/.../SchedulingService.java at tag 27.0.51 for scheduling
```
```
List the files under src/main/resources at tag 27.0.51 for scheduling
```

### Multi-org exploration

```
Which GitHub organizations can I see with my token?
```
```
Do I have SSO-authorized access to myorgname1?
```
```
List repos in myorgname1 that contain "kubernetes" in the name.
```
```
Show me the open PRs in myorgname3/acd-platform, sorted by most recently updated.
```
```
Give me the diff for PR #4821 in myorgname3/acd-platform.
```
```
Compare tags v27.0.9 and v27.0.51 in myorgname1/acddevops-kubernetes-charts and
include the unified diff.
```
```
Walk the repo tree of myorgname1/acddevops-kubernetes-charts at main under
charts/scheduling and list every values.yaml.
```

### Investigating an error from logs (typical AWS → GitHub workflow)

```
I'm seeing this error in the eu1 scheduling logs on version 27.0.51:
"ORA-00028: your session has been killed"
Find the code path that would produce it.
```
```
Search the recording repo for "ConnectionResetException" and then show me
that file at tag 27.0.9.
```

### Investigating a failing PR check (GitHub Actions)

```
PR #96726 in myorgname1/acddevops-kubernetes-charts has failing checks.
Give me a report on which checks failed.
```
```
For the failing check on that PR, find the Actions run and list its jobs.
```
```
Show me the last 100 KB of logs for job <job_id> in
myorgname1/acddevops-kubernetes-charts.
```
```
List the last 10 workflow runs on the main branch of myorgname3/acd-platform
where the conclusion was failure.
```

