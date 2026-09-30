# Jira MCP Server

Dual-site, deploy-queue-aware Jira MCP server for the MyCompany DevOps
workflow. Talks to either or both of:

- **onprem** — MyCompany internal Jira **Server/Data Center**
  (default `https://jira.mycompany.com`).
  Auth: Personal Access Token (Bearer) via `JIRA_TOKEN`.
- **cloud** — MyCompany Jira **Cloud**
  (default `https://mycompany-prod.atlassian.net`).
  Auth: HTTP Basic with the Atlassian account email (`JIRA_CLOUD_EMAIL`) +
  API token (`JIRA_CLOUD_TOKEN`) — the scheme classic Atlassian Cloud API
  tokens require. Create the token at
  <https://id.atlassian.com/manage-profile/security/api-tokens>
  (lasts 1 year). Without the email the server falls back to Bearer,
  which only works for scoped API tokens / OAuth PATs and 403s on
  classic tokens. Jira Cloud also requires an explicit REST API version
  (`/rest/api/2/`) rather than the `latest` alias Server/Data Center
  accepts — the server rewrites this transparently per site.

If exactly one site is configured, every tool defaults to it when the
caller passes `site="auto"` (the default on every tool). If both are
configured, callers must pick one explicitly with `site="onprem"` or
`site="cloud"`.

TLS verification is intentionally always off (`verify=False`) on every
request this server makes — it's only ever run on a host with the
corporate VPN active, which MITM-inspects HTTPS with a self-signed cert
the container's CA store doesn't trust. Not configurable, no env toggle.

Every write tool (`add_jira_comment`, `transition_jira_ticket`,
`assign_jira_ticket`, `edit_jira_ticket`, `clone_jira_ticket`,
`create_jira_ticket`, `move_jira_ticket`) requires `confirmed_by_user=True`
before it touches Jira. The first call (default `confirmed_by_user=False`)
only returns a preview of what would happen — no write is issued until
the caller shows that preview to the user and re-invokes the same tool
with `confirmed_by_user=True`. This is enforced in code, independent of
whatever approval dialog the AI host itself shows.

## Configuration

| Env var | Purpose |
|---------|---------|
| `JIRA_BASE_URL` | onprem base URL (default `https://jira.mycompany.com`). |
| `JIRA_TOKEN` | onprem Personal Access Token (Bearer). |
| `JIRA_CLOUD_BASE_URL` | cloud base URL (default `https://mycompany-prod.atlassian.net`). |
| `JIRA_CLOUD_EMAIL` | Atlassian account email for cloud. Combined with the token to form HTTP Basic auth. |
| `JIRA_CLOUD_TOKEN` | cloud Atlassian API token. |

## Tools

Every tool accepts `site: "onprem" | "cloud" | "auto"` (default `"auto"`,
which resolves to the sole configured site). Use `site="cloud"` for MyCompany
MyCompany tickets like `https://mycompany-prod.atlassian.net/browse/OB-45834`.

| Tool | What it does |
|------|--------------|
| `whoami_jira` | Return the identity attached to a site's token (username, display name, email, accountId). Wraps `/rest/api/latest/myself`. **Use this whenever the caller needs to know who "the current user" is on Jira** — never infer Jira identity from Confluence, GitLab, or any other MCP server. |
| `list_jira_tickets` | Minimal ticket list for any JQL query (defaults to open Change Requests, newest first). |
| `list_ready_to_deploy_tickets` | DevOps tickets that are **APPROVED** and in **Ready for deployment**, ordered by the deploy-queue key. Each ticket is tagged with its target env (from the env custom field, falling back to a token like `NA4`/`EU1`/`STG4` in the summary) and classified as `production`, `non_production`, or `unknown`. Supports filtering by exact env or by scope. MyCompany-specific — always runs against `site="onprem"`. |
| `search_jira_tickets` | **Freestyle JQL** search with a rich, pagination-aware response. Returns each ticket with `raw` / `flat` / `by_name` field views plus a `custom_field_names` map so human-readable field names are always available. Supports arbitrary `fields`, `expand`, `start_at`, `max_results` (1..100). Use when the user gives you their own JQL. |
| `get_jira_ticket` | Fetch a single ticket by key with all fields (defaults to `*all`). Returns the same `{raw, flat, by_name}` triple as `search_jira_tickets`. |
| `list_jira_projects` | Discover Jira projects visible to the token (key, name, category, lead). Use before composing JQL that references a `project = <key>` you're not sure exists. |
| `list_jira_fields` | Discover Jira field definitions including custom fields — id (`customfield_16400`), human name (`Environment`), searchable flag, clause names (`cf[16400]`), and schema. Filter by name/id substring. Set `custom_only=true` to skip system fields. |
| `get_jira_transitions` | List the workflow transitions currently allowed from the ticket's status. Source of truth for `transition_jira_ticket`. |
| `get_jira_editable_fields` | Scan a ticket for the fields that can be edited RIGHT NOW (via Jira's `editmeta`). Returns each field's id, human name, schema, allowed values, current value, and whether it's forbidden from generic editing (must go through a dedicated tool). Source of truth for `edit_jira_ticket`. |
| `add_jira_comment` | Post a comment to a ticket. |
| `transition_jira_ticket` | Move a ticket through the workflow. Rejects any transition not currently allowed by the project's workflow. Optional atomic `comment`. |
| `assign_jira_ticket` | Assign a ticket by username / display name / partial name. If the query matches multiple assignable users, returns a `candidates` list and refuses to guess. Pass `-1` / `unassigned` to unassign. |
| `edit_jira_ticket` | Set one or more fields (summary, description, priority, labels, environment, rollout instructions, any custom field, …). Accepts field ids or human names; normalizes values based on schema. Fields with dedicated tools (`status`, `assignee`, `comment`, `issuetype`, `project`, `reporter`, `attachment`, `worklog`, `issuelinks`, `subtasks`) are refused. |
| `clone_jira_ticket` | Create a new ticket by copying `summary` (prefixed `[CLONE]`), `description`, `priority`, `labels`, `components`, `versions`, `fixVersions`, `environment` and the MyCompany env/order custom fields from a source. Apply `overrides` to change any of those (or the target project). Assignee / status / comments / worklogs / attachments / links are NOT copied. Source and target must be on the same `site`. |
| `move_jira_ticket` | Move a ticket to another project on the same `site`. Jira REST has no atomic project-move, so this runs as an ordered sequence in one tool call: (1) create in target project (no `[CLONE]` prefix), (2) link source ↔ new (default `Cloners`), (3) comment on source with a pointer, (4) optionally transition the source through a workflow-allowed transition. Partial failure is reported per-step in `step_results`; the source is not automatically rolled back. |
| `check_for_updates` | On-demand release check for this image (via shared `version_notifier`). |

### Approval model

Every write tool requires `confirmed_by_user=True` before it executes.
Calling it with the default `confirmed_by_user=False` returns a preview
of what the write would do (the resolved diff, transition, payload,
etc.) and touches nothing; only a second, explicit call with
`confirmed_by_user=True` performs it. This is on top of whatever
approval prompt the AI host itself shows — the VS Code MCP dialog that
fires for every tool invocation, or the equivalent in whatever agent
you're using.

Jira-side correctness guards remain in the server:

- `transition_jira_ticket` fetches the currently allowed transitions from
  Jira and rejects any transition that isn't in that set. The workflow is
  the source of truth.
- `assign_jira_ticket` uses Jira's assignable-user search. Ambiguous
  queries (e.g. `"john"`) return a `candidates` list and no write is
  issued; the caller must re-invoke with the exact `username`.
- `edit_jira_ticket` fetches Jira's `editmeta` and rejects any field that
  isn't editable in the current workflow state or that has a dedicated
  tool. Values are normalized against each field's schema (single-select
  → `{value}`, priority/user → `{name}`, labels → list of strings, etc.);
  raw Jira dicts are passed through untouched.
- `clone_jira_ticket` never modifies the source. Assignee, status,
  comments, attachments, and links are NOT copied to the clone — set
  those on the clone with the dedicated tools after creation.
- `move_jira_ticket` executes its four steps in order under one tool
  call. On any step failure, later steps are skipped and `step_results`
  reports what did and didn't happen. Jira has no transaction across
  these endpoints, so a partial failure has to be finished manually.

## Configuration

| Env var | Purpose |
|---------|---------|
| `JIRA_BASE_URL` | Jira host base URL (default `https://jira.mycompany.com`). |
| `JIRA_TOKEN` | Personal access token / bearer token. Required. Never hardcode — inject via your AI host MCP config. |

## Example prompts

### Deploy queue

```
Show me the tickets ready to deploy this week
```
```
List the DevOps tickets ready to deploy to na4
```
```
What's in the deploy queue for production envs?
```
```
Show me only the non-production ready-to-deploy tickets from the last 3 days
```

### Ad-hoc JQL

```
Give me the 10 most recently updated Change Request tickets
```
```
Run this JQL and give me the top 20: project = DevOps AND status = "In Progress"
```

### Freestyle JQL with rich field access

```
Run this JQL for me and show env, priority, assignee for each result:
project = DevOps AND (cf[16400] in (STG4) OR summary ~ "*STG4*" OR
cf[16400] in (EU1) OR summary ~ "*EU1*" OR cf[16400] in (TST3) OR
summary ~ "*TST3*" OR cf[16400] in (UK2) OR summary ~ "*UK2*" OR
cf[16400] in (AU1) OR summary ~ "*AU1*" OR cf[16400] in (UK3) OR
summary ~ "*UK3*") AND comment ~ APPROVED AND Status = "Ready for deployment"
AND (ASSIGNEE != currentUser()) ORDER BY createdDate DESC
```
```
Fetch ticket DEVOPS-52986 with everything on it
```
```
Show me the changelog and comments for DEVOPS-52986
  → get_jira_ticket("DEVOPS-52986", expand=["changelog","renderedFields"])
```

### Discovery

```
List Jira projects
```
```
Which Jira project keys have "devops" in their name?
```
```
Which Jira custom fields hold the environment / deploy order?
  → list_jira_fields(search="environment")
  → list_jira_fields(search="deploy")
```
```
Show me every custom field
  → list_jira_fields(custom_only=true)
```

### Workflow → transition

```
What transitions are available on DEVOPS-52986?
  → get_jira_transitions("DEVOPS-52986")
```
```
Move DEVOPS-52986 to Ready for deployment
  → get_jira_transitions("DEVOPS-52986")   # confirm the allowed set
  → transition_jira_ticket("DEVOPS-52986", transition="Ready for deployment")
```
```
Transition DEVOPS-52986 to In Progress with a comment
  → transition_jira_ticket("DEVOPS-52986", transition="In Progress",
                            comment="Starting deployment")
```

### Comments

```
Comment on DEVOPS-52986 saying 'APPROVED for STG4 deployment'
  → add_jira_comment("DEVOPS-52986", body="APPROVED for STG4 deployment")
```

### Assignment (with disambiguation)

```
Assign DEVOPS-52986 to john
  → assign_jira_ticket("DEVOPS-52986", assignee="john")
     # if multiple matches, returns candidates=[{username, display_name, email, active}, ...]
  # user picks john.duran
  → assign_jira_ticket("DEVOPS-52986", assignee="john.duran")
```
```
Unassign DEVOPS-52986
  → assign_jira_ticket("DEVOPS-52986", assignee="unassigned")
```

### Editing arbitrary fields

```
What can I edit on DEVOPS-52986?
  → get_jira_editable_fields("DEVOPS-52986")
     # returns every editable field with id, human name, schema,
     # allowed_values, current_value, and whether it's forbidden here
     # (status/assignee/comment/etc. must use their dedicated tool)
```
```
Change the summary and rollout instructions on DEVOPS-52986
  → edit_jira_ticket("DEVOPS-52986", fields={
        "Summary": "[STG4] Deploy component-x v1.2.3",
        "Rollout Instructions": "1) drain; 2) deploy; 3) smoke test"
    })
```
```
Change env to STG4, priority to High, add label 'hotfix'
  → edit_jira_ticket("DEVOPS-52986", fields={
        "Environment": "STG4",       # normalized to {"value": "STG4"}
        "Priority": "High",          # normalized to {"name": "High"}
        "Labels": ["deploy", "hotfix"]
    })
```
```
Edit a custom field by id with a raw Jira value (bypasses normalization)
  → edit_jira_ticket("DEVOPS-52986", fields={
        "customfield_16400": {"value": "NA4"}
    })
```

### Cloning tickets

```
Clone DEVOPS-52986 to open a matching NA4 deployment
  → clone_jira_ticket("DEVOPS-52986", overrides={
        "Environment": "NA4",
        "Summary": "[NA4] Deploy component-x v1.2.3"
    })
     # Assignee, status, comments, attachments, links are NOT copied.
```
```
Clone into a different project
  → clone_jira_ticket("DEVOPS-52986", overrides={"project": "SUPPORT"})
```

### Moving tickets between projects

```
Move DEVOPS-52986 to the SUPPORT project
  → move_jira_ticket("DEVOPS-52986", target_project="SUPPORT")
     # runs 4 steps in one call:
     #   1. create in SUPPORT (no [CLONE] prefix)
     #   2. link source ↔ new via Cloners
     #   3. comment on source: "Moved to <new_key> in a different project."
     #   4. (skipped here — close_source=False by default)
     # `step_results` reports each step's outcome
```
```
Move and close the source ticket
  → get_jira_transitions("DEVOPS-52986")  # find the closing transition name
  → move_jira_ticket("DEVOPS-52986",
                      target_project="SUPPORT",
                      close_source=True,
                      close_transition="Close")
```
```
Move with a custom source comment and a different issuetype
  → move_jira_ticket("DEVOPS-52986",
                      target_project="SUPPORT",
                      target_issuetype="Task",
                      source_move_comment="Handed off to Support: {new_ticket_key}",
                      link_type="Relates")
```

Notes:
- Jira REST has no atomic project-move, so this is a scripted sequence.
  Rollback on partial failure is manual.
- The transition on the source (when `close_source=True`) must be allowed
  by the source's current workflow — same rule as `transition_jira_ticket`.
- `{new_ticket_key}` in `source_move_comment` is substituted with the
  real new-ticket key at post time.
