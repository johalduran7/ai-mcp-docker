# GitLab MCP Server

Drives inspection **and** deployments through MyCompany `<component>-config` repos
plus the CDN repo (`devops/cdn @ gitlab-ci`). Talks to
`https://vcs.build.mycompany.net/eng` by default.

> Every GitLab call goes through the corporate GitLab, so you **must** be on
> the corporate VPN when a gitlab tool runs.

## Tools

### Inspection

| Tool | What it does |
|------|--------------|
| `list_component_branches` | List branches of a `<component>-config` repo, with optional name filter (e.g. `27.0`). |
| `get_component_config_file` | Fetch the raw config file for a component / env / branch. |
| `list_env_files` | List every file under an env directory. |
| `detect_component_deployment_type` | Report whether a component deploys via ECS (Fargate), ASG, both, or none. |
| `get_component_config_summary` | Parse a component's config and return commonly used settings (version, counts, ASG nuke state, …). |
| `list_env_deployments` | List successful deployments landed in an env, filterable by time window, component, and branch. |

### Deployment actions (write)

| Tool | What it does |
|------|--------------|
| `find_redeploy_targets` | **Read-only** preview of the `LV_CD_DEPLOY` toggle change for one or more components, showing a unified diff. |
| `redeploy_targets` | Commit the confirmed `LV_CD_DEPLOY` change to trigger a redeploy. |
| `find_tag_deploy_targets` | **Read-only** preview of a batch of version-tag deployments. |
| `deploy_tag_targets` | Commit the confirmed tag deployments to their config repos and trigger ASG pipelines where needed. |

### CDN deploys (`devops/cdn` @ `gitlab-ci`)

| Tool | What it does |
|------|--------------|
| `list_cdn_env_components` | **Read-only** list of CDN component JSON files under `<env>/`. |
| `list_cdn_env_files` | **Read-only** listing of every file under `<env>/` in the CDN repo (for companion-file discovery). |
| `get_cdn_component_config` | Fetch the raw CDN JSON file for a component / env / branch. |
| `find_cdn_tag_deploy_targets` | **Read-only** preview of CDN `lv_component_version` updates (with optional companion `extra_updates`). |
| `deploy_cdn_tag_targets` | Commit the confirmed CDN version updates atomically to `gitlab-ci`. |
| `check_for_updates` | On-demand release check for this image (via shared `version_notifier`). |

> CDN deploys land in `devops/cdn @ gitlab-ci` and do NOT go through `LV_CD_DEPLOY` / CI pipelines — CDN ships whatever HEAD has. The CDN whitelist spans both non-prod (`au1, eu1, stg4, tst2, tst3, uk2, uk3`) and prod (`na3, na4, na5, na6`) since CDN's primary purpose is pushing UI assets to production.

> Write tools are two-step by design: a `find_*` preview produces the exact targets/diffs, which you then pass to the matching commit tool. Nothing is committed without an explicit second call.

> **Known gap:** none of the CDN write tools support *creating* a brand-new file (`find_cdn_tag_deploy_targets` / `deploy_cdn_tag_targets` only rewrite `lv_component_version` inside a file that already exists). Onboarding a component to a CDN env that has never had it before currently has no purpose-built tool — use `gitlab_write` (below) instead.

### Freestyle bridge (anything the tables above don't cover)

| Tool | What it does |
|------|--------------|
| `gitlab_get` | **Read-only** GET against any `/api/v4/…` or `/api/graphql` endpoint. Hard-coded to `requests.get` — physically cannot POST/PUT/PATCH/DELETE regardless of input. |
| `gitlab_write` | **Write** (POST/PUT/PATCH/DELETE) against any `/api/v4/…` endpoint. Escape hatch for anything the purpose-built tools don't cover — e.g. creating a brand-new file in `devops/cdn`, deleting a branch, creating a tag, editing project settings. Bypasses every safety rail the specialized tools bake in (env whitelist/denylist, tag-format validation, atomic multi-file commits, ASG pipeline triggers), so it requires **two separate rounds of explicit user confirmation** (`confirmed_by_user=True` then, on a later call, also `double_confirmed=True`) instead of the single-flag gate every other write tool here uses. Prefer a purpose-built tool whenever one fits the task. |

> `gitlab_write`'s three-call contract: (1) no flags → preview only (`method`/`effective_path`/`body`/best-effort `diff`); (2) `confirmed_by_user=True` → same preview again under a stronger warning, still doesn't execute; (3) `confirmed_by_user=True` **and** `double_confirmed=True` → executes. Each round's approval must come from its own distinct user message in the conversation — never set both flags from a single approval.

## Configuration

| Env var | Purpose |
|---------|---------|
| `GITLAB_ENDPOINT` | GitLab base URL + group path (default `https://vcs.build.mycompany.net/eng`). |
| `GITLAB_TOKEN` | Personal / project access token with `api` scope. Required. Inject via your AI host MCP config. |

## Example prompts

### Inspection

```
List branches for the acm-config repo
```
```
Show me the fargate config for scheduling in eu1 on 27.0
```
```
List every file under eu1/ in scheduling-config on 27.0
```
```
Is contact deployed via ECS or ASG for uk2 on 27.0?
```
```
Summarize the asg config for agentproxy in eu1 on 4.0 (min, max, desired, nuke, version)
```

### Deploy history

```
What was deployed to uk2 this week?
```
```
Was acm deployed to eu1 recently?
```
```
Latest version deployed to scheduling on 27.0 and 26.0 in tst3
```

### Redeploy (LV_CD_DEPLOY toggle)

```
Preview a redeploy of scheduling in eu1 on 27.0
```
```
Redeploy acm and email_gateway in tst3 on 27.0 — show me the diffs first
```

### Tag deploy

```
Deploy 27.0.9 to scheduling in eu1
```
```
Deploy these tags:
  eu1/27.0/scheduling/ecs/DEVOPS-52986/27.0.9
  eu1/27.0/agent-compliance/ecs/DEVOPS-52988/27.0.11
```
```
Deploy 4.0.10 to agentproxy in uk2 and also bump timeout in eu1/scheduling.properties
```

### CDN deploy (`devops/cdn` @ `gitlab-ci`)

```
List CDN components in na4
```
```
Show me the CDN file for designer in na4 on 27.0
```
```
Deploy CDN designer 27.0.51 to na4 for DEVOPS-52986
```
```
Deploy CDN agent-login-ui 27.0.10 to eu1 and also update na4/designer.properties
```

> CDN deploys can target both non-prod (`au1, eu1, stg4, tst2, tst3, uk2, uk3`) AND production (`na3, na4, na5, na6`) — CDN's primary use case is pushing UI assets to prod. Any other env label is rejected.

### Freestyle write (double confirmation)

```
Create perf1/e-learning-component-manager-ui-27.0.json in devops/cdn with lv_component_version 26.3.6 — show me the diff first
```
```
(after reviewing the diff) yes, go ahead
```
```
(after the second, stronger warning) confirmed, do it
```

> Only reach for this when no purpose-built tool fits (e.g. creating a file in a CDN env that's never had it before). It requires two separate explicit "yes"es in the conversation before anything is sent — never expect it to execute on the first or second call.
