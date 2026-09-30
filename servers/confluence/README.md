# Confluence MCP Server

Dual-site Confluence MCP server for the MyCompany DevOps workflow.
Talks to either or both of:

- **cloud** — MyCompany Confluence **Cloud**
  (default `https://mycompany-prod.atlassian.net/wiki`).
  Auth: HTTP Basic with the Atlassian account email +
  API token — the scheme classic Atlassian Cloud API tokens require.
  Create the token at
  <https://id.atlassian.com/manage-profile/security/api-tokens>
  (lasts 1 year). Without the email the server falls back to Bearer,
  which only works for scoped API tokens / OAuth PATs and 403s on
  classic tokens with `"Current user not permitted to use Confluence"`.
- **onprem** — MyCompany internal Confluence **Data Center**
  (default `https://library.mycompany.com`).
  Auth: Personal Access Token (Bearer).
  Configure at <https://library.mycompany.com/plugins/personalaccesstokens/usertokens.action>
  (never expires).

If exactly one site is configured, every tool defaults to it when the
caller passes `site="auto"`. If both are configured, callers must
pick one explicitly — or use `"both"` on the search-style tools that
fan out in parallel.

TLS verification is intentionally always off (`verify=False`) on every
request this server makes — it's only ever run on a host with the
corporate VPN active, which MITM-inspects HTTPS with a self-signed cert
the container's CA store doesn't trust. Not configurable, no env toggle.

## Configuration

| Env var | Purpose |
|---------|---------|
| `CONFLUENCE_CLOUD_BASE_URL` | cloud base URL (default `https://mycompany-prod.atlassian.net/wiki`). |
| `CONFLUENCE_CLOUD_EMAIL` | Atlassian account email for cloud. Combined with the token to form HTTP Basic auth. |
| `CONFLUENCE_CLOUD_TOKEN` | Atlassian API token for cloud. Falls back to Bearer if the email is not set. |
| `CONFLUENCE_ONPREM_BASE_URL` | onprem base URL (default `https://library.mycompany.com`). |
| `CONFLUENCE_ONPREM_TOKEN` | MyCompany Confluence Personal Access Token. |

Never hardcode tokens — inject them through the AI host's MCP config or
via `.env` in this folder (git-ignored).

## Tools

### Read

| Tool | What it does |
|------|--------------|
| `list_confluence_sites` | Report which sites are configured (base URL, token set) plus the identity attached to each token (display name / email / username), auto-discovered from `/rest/api/user/current`. Use first when unsure. |
| `list_confluence_spaces` | List spaces visible to the token. Optional client-side substring filter + Confluence-side `type` filter (`global`/`personal`/`archived`). |
| `list_recent_pages` | Pages modified in the last `since_days`, optionally scoped by `space_key`. Uses CQL `lastmodified > now("-Nd")`. Supports `site="both"`. |
| `search_confluence` | **Freestyle CQL** with `site="cloud"`, `"onprem"`, `"auto"`, or `"both"` (parallel fan-out via thread pool). |
| `batch_search_confluence` | Fires many CQL queries in parallel across a thread pool, per site. Ideal for sweeping many OAQS deployment terms or component keywords in one call. |
| `get_confluence_page` | Fetch a single page by `page_id` **or** a full URL (id is extracted from `/pages/<id>/…` or `?pageId=<id>`). Returns body in `storage` (default) or `view`, plus a plain-text projection. |
| `read_confluence_page` | **Token-saving paginated reader.** Fetches once, splits into bounded chunks (default ~2500 chars) keyed by heading path, cursor-paginated via `next_cursor`. Set `pattern` to grep server-side — only chunks containing regex matches are returned, each with `matches: [{match, snippet, position}]`. Every response embeds `page.url` so the AI can always cite the source. |
| `get_confluence_page_by_title` | Resolve a page by exact `space_key` + `title`. Returns `candidates` if multiple match. |
| `list_page_children` | List child pages of a page. |
| `list_page_attachments` | List attachments (name, media type, size, download URL). |
| `get_page_history` | Version history for a page. |
| `list_page_labels` | Labels on a page. |
| `search_confluence_users` | User search via CQL (`type=user AND user.fullname ~ "…"`). Works on Cloud and DC. |
| `check_for_updates` | On-demand release check for this image (shared `version_notifier`). |

### Write (host per-call approval)

| Tool | What it does |
|------|--------------|
| `create_confluence_page` | Create a page under a space, optionally as child of `parent_page_id`. |
| `update_confluence_page` | Update a page's body (and optionally title). The current version is fetched automatically; the required version bump is computed for you. |
| `delete_confluence_page` | Trash a page. Destructive; recovery is from the space's trash UI. |
| `add_confluence_comment` | Post a comment on a page. |
| `add_confluence_labels` | Add one or more labels to a page. |

### Approval model

Every write tool executes its Confluence write directly. User consent
is handled by the AI host's per-tool-call approval prompt — the VS Code
MCP dialog that fires for every tool invocation, or the equivalent in
whatever agent you're using. Same pattern as the Jira and AWS MCP tools
in this repo.

## Example prompts

### Discovery

```
Which Confluence sites am I connected to?
  → list_confluence_sites()
```
```
List all spaces on the onprem confluence
  → list_confluence_spaces(site="onprem")
```
```
Find spaces on cloud whose name contains "runbook"
  → list_confluence_spaces(site="cloud", search="runbook")
```

### Recent activity

```
What Confluence pages changed in the last 3 days on onprem?
  → list_recent_pages(site="onprem", since_days=3)
```
```
Show me pages in the IN space on cloud modified in the last week
  → list_recent_pages(site="cloud", space_key="IN", since_days=7)
```
```
Show recent activity across BOTH confluences
  → list_recent_pages(site="both", since_days=7)
```

### Search

```
Find OAQS-related pages on onprem
  → search_confluence(
        cql='text ~ "OAQS" AND type = page',
        site="onprem",
        max_results=25)
```
```
Same search across both confluences in parallel
  → search_confluence(
        cql='text ~ "OAQS deployment" AND type = page',
        site="both")
```
```
Batch sweep for many OAQS deployment runbooks in parallel
  → batch_search_confluence(
        queries=[
          'text ~ "OAQS deploy stg4"',
          'text ~ "OAQS deploy tst2"',
          'text ~ "OAQS deploy na4"',
          'label = "oaqs" AND lastmodified > now("-30d")'
        ],
        site="both",
        max_results_per_query=10)
```

### Page fetch

```
Fetch the homepage of the IN space on cloud
  → get_confluence_page(url="https://mycompany-prod.atlassian.net/wiki/spaces/IN/overview?homepageId=15042334",
                        site="cloud")
```
```
Get a specific page by id
  → get_confluence_page(page_id="12345", site="onprem")
```
```
Find a page by exact title
  → get_confluence_page_by_title(space_key="IN", title="OAQS Runbook", site="cloud")
```
```
List children of a runbook overview
  → list_page_children(page_id="12345", site="onprem")
```

### Users

```
Find John on onprem confluence
  → search_confluence_users(query="John Duran", site="onprem")
```

### Write (approval loop by host)

```
Create a page under the IN space referencing today's deployment
  → create_confluence_page(
        space_key="IN",
        title="STG4 OAQS Deployment 2026-08-11",
        body="<p>Deployment summary…</p>",
        site="cloud")
```
```
Update an existing page (no version number needed — computed automatically)
  → update_confluence_page(
        page_id="12345",
        body="<p>Updated content</p>",
        version_comment="Add rollback notes",
        site="onprem")
```
```
Comment on a page
  → add_confluence_comment(
        page_id="12345",
        body="Reviewed for the STG4 rollout — LGTM",
        site="onprem")
```
```
Label a page
  → add_confluence_labels(page_id="12345", labels=["oaqs", "runbook"], site="cloud")
```
```
Trash a page (destructive — host prompts for approval)
  → delete_confluence_page(page_id="12345", site="onprem")
```

## Notes on CQL

Both Confluence Cloud and Data Center support CQL through
`GET /rest/api/search?cql=…`. Common building blocks:

- `type = page | blogpost | attachment | comment | user`
- `text ~ "phrase"` — full-text search
- `title ~ "phrase"` — title search
- `space = "KEY"`
- `label = "name"` (or `label in ("a","b")`)
- `creator = "username"` / `contributor = "username"`
- `lastmodified > now("-7d")`
- `parent = <page_id>`
- ORDER BY: `ORDER BY lastmodified DESC` / `ORDER BY title ASC`

Use `site="both"` on `search_confluence` and `list_recent_pages`, or
`batch_search_confluence` for many-in-one-shot parallel sweeps.
