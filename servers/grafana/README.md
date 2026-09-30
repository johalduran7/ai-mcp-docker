# Grafana / Loki MCP Server

**Strictly read-only** Grafana / Loki MCP server covering two Grafana
"providers", selected per-call via the `provider` argument (default
`"lvx"`):

* **`"lvx"`** — the original MyCompany Grafana, one per env at
  `http://logging-ui.service.<env>.mycompany.net:8080`, HTTP Basic auth with
  LDAP credentials. **Requires the Cisco AnyConnect VPN.**
* **`"mycompany"`** — MyCompany org's own Grafana (the `mon-*` hosts — what a user
  means when they say "mon"), one per env from a hardcoded map, reached via
  Okta SSO (username + password + MFA) instead of LDAP Basic auth.
  **Requires the Forticlient VPN.** Call `mycompany_grafana_login` first.

If a request is ambiguous about which Grafana is meant, ask before running
anything. A connection failure against a `"mycompany"` target almost always
means the wrong VPN client is active.

Every HTTP request is a `GET`. There are no tools that create, modify,
delete, transition, comment, or otherwise change anything on the Grafana
side. The `grafana_get` freestyle tool refuses any path that doesn't start
with `/api/` and never accepts an alternative method.

## Tools

| Tool | What it does |
|------|--------------|
| `grafana_health` | Verify connectivity + auth for an env. Hits `/api/health` (no auth) and `/api/datasources` (auth). Reports datasource count + types on success. Takes `provider`. |
| `mycompany_grafana_login` | **`mycompany` only.** Runs the Okta primary-auth + MFA + cookie-exchange flow and caches the authenticated session **in memory** for this container's lifetime (no host volume). Call with no `mfa_code` first; if it returns `needs_mfa: true`, ask the user for their 6-digit code and call again with `mfa_code="123456"`. |
| `list_datasources` | List every Grafana datasource in the env (Loki, ClickHouse, …). Includes uid so the AI can target a specific one via the proxy. Takes `provider`. |
| `list_loki_labels` | List Loki label names for a lookback window (default 60m). lvx dimensions: `component`, `host`, `image_name`, `profile`. mycompany dimensions: `service_name`, `openapm_product_name`, … Takes `provider`. |
| `list_loki_label_values` | List values of a single label (e.g. every `component` on lvx, or every `service_name` on mycompany). Takes `provider`. |
| `query_loki` | Run a LogQL `query_range` against Loki via the Grafana proxy. **Loki is the default datasource** — no need to specify. Two paths: (a) **convenience shortcut** with `labels` + optional `line_filter` (`\|= "..."`) / `line_regex` (`\|~ "..."`), or (b) **raw** `query` for aggregations, rates, and anything the shortcut can't express. On `provider="mycompany"`, pass just `service_name` — the constant `openapm_product_name="mycompany"` matcher is auto-injected. Supports `since_minutes` / `since_hours` / `since_days` or explicit `start_time` / `end_time` (ISO-8601), `limit` (1..5000), `direction`, `step`. |
| `query_loki_instant` | Instant Loki query — single point in time. Same shortcut vs. raw contract (and mycompany auto-defaults) as `query_loki`. |
| `grafana_get` | **Freestyle** read-only GET against Grafana's REST API. Path must start with `/api/` and match a strict character class (no absolute URLs, no traversal). Takes `provider`. |
| `check_for_updates` | On-demand release check for this image (via shared `version_notifier`). |

## Configuration

| Env var | Purpose |
|---------|---------|
| `GRAFANA_URL_TEMPLATE` | (`lvx`) Endpoint template with `{env}` placeholder (default `http://logging-ui.service.{env}.mycompany.net:8080`). |
| `GRAFANA_USERNAME` / `GRAFANA_PASSWORD` | (`lvx`) LDAP credentials. Required for `provider="lvx"`. |
| `GRAFANA_LOKI_UID_<ENV>` | Optional per-env override for the Loki datasource UID (lvx). For mycompany, use `GRAFANA_LOKI_UID_MyCompany_<ENV>`. When unset, the server auto-discovers by calling `/api/datasources` and picking `type == "loki"`. |
| `OKTA_USERNAME` / `OKTA_PASSWORD` | (`mycompany`) Okta credentials. Required for `provider="mycompany"`. |
| `OKTA_DOMAIN` | (`mycompany`) Okta domain. Optional, defaults to `mycompany.okta.com`. The per-env `mon-*` URLs stay hardcoded in `server.py` regardless. |

## Example prompts

### MyCompany Grafana (mon-*) — Okta login + queries

Envs mapped in `MyCompany_GRAFANA_ENV_URLS`: `tst2`, `tst3`, `stg4`, `na4`,
`na3`, `na6`, `na5`, `eu1`, `uk2`, `uk3`, `au1`, `perf1`.

```
Log in to MyCompany Grafana for au1
  → mycompany_grafana_login(env="au1")
  # → {ok: false, needs_mfa: true, ...} on first login this session
  → (ask user for their 6-digit code)
  → mycompany_grafana_login(env="au1", mfa_code="123456")
```
```
Pull the last hour of sbc logs on MyCompany Grafana in au1
  → query_loki(env="au1", provider="mycompany",
     labels={"service_name": "sbc"}, since_hours=1)
  # → LogQL: {openapm_product_name="mycompany",service_name="sbc"}
```
```
What service_names are logging in MyCompany Grafana for eu1 right now?
  → list_loki_label_values(env="eu1", label="service_name",
     provider="mycompany", since_hours=1)
```

### Connectivity / discovery

```
Is Grafana in na3 reachable?
```
```
List Grafana datasources in na3
```
```
Show me all Loki labels in stg4 from the last 24 hours
```
```
Which components are logging to Loki in na4 right now?
  → list_loki_label_values(env="na4", label="component", since_hours=1)
```

### LogQL freestyle queries

Loki is the **default (and only) log datasource** used by these tools. Two ways to query:

1. **Convenience shortcut** — pass `labels` (required) plus optional `line_filter` (case-sensitive substring, becomes `|= "..."`) and `line_regex` (RE2, becomes `|~ "..."`). The server composes the LogQL for you. Great for the common MyCompany labels: `component`, `host`, `image_id`, `image_name`, `profile`.
2. **Raw LogQL** — pass a full `query` for anything the shortcut can't express (aggregations, rate/sum/unwrap, etc.). When `query` is set, shortcut params are ignored (the response reports `shortcut_ignored: true`).

```
Pull the last 500 ACD logs on host 10.158.75.219 in na3 from the last hour
  → query_loki(env="na3",
     labels={"component":"ACD", "host":"10.158.75.219"},
     since_hours=1, limit=500)
  # → LogQL: {component="ACD",host="10.158.75.219"}
```
```
Search stg4 Loki for "ORA-00028" across all ACD hosts in the last 2 hours
  → query_loki(env="stg4",
     labels={"component":"ACD"},
     line_filter="ORA-00028",
     since_hours=2, limit=1000)
  # → LogQL: {component="ACD"} |= "ORA-00028"
```
```
Grab yesterday's ACD logs in na3 between 00:00 and 23:59 UTC
  → query_loki(env="na3",
     labels={"component":"ACD"},
     start_time="2026-07-13T00:00:00Z",
     end_time="2026-07-13T23:59:59Z",
     limit=5000)
```
```
Find case-insensitive "timeout" errors on ACD in eu1 in the last 15 minutes
  → query_loki(env="eu1",
     labels={"component":"ACD"},
     line_regex="(?i)timeout",
     since_minutes=15)
  # → LogQL: {component="ACD"} |~ "(?i)timeout"
```
```
Which ACD host is producing the most errors in eu1 right now? (raw LogQL)
  → query_loki(env="eu1",
     query='sum by (host) (rate({component="ACD"} |~ "ERROR" [5m]))',
     since_minutes=15)
```
```
Metric query at a specific point in time (instant)
  → query_loki_instant(env="na3",
     query='sum by (host) (rate({component="ACD"} |~ "ERROR" [5m]))',
     at_time="2026-07-14T12:00:00Z")
```

### Freestyle GET (anything else Grafana exposes)

```
Show me the raw /api/search results in na3
  → grafana_get(env="na3", path="/api/search", params={"query":"ACD"})
```
```
Fetch dashboard by UID in stg4
  → grafana_get(env="stg4", path="/api/dashboards/uid/<dashboard-uid>")
```
```
List Loki series matching {component="ACD"} in na4
  → grafana_get(env="na4",
     path="/api/datasources/proxy/uid/<loki-uid>/loki/api/v1/series",
     params={"match[]":"{component=\"ACD\"}"})
```
