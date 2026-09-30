# OpenSearch MCP Server

Read-only MCP server that talks to MyCompany OpenSearch domains in AWS.

## What it does

Given an env (e.g. `stg4`) and an AWS profile (substring accepted, e.g. `stg4`
→ `lv-stg4-dr`) it:

1. Uses the in-container cache of `{domain: endpoint}` for that env. On a
   cache miss it fetches the endpoint via `opensearch:ListDomainNames` +
   `opensearch:DescribeDomains`. The cache is process-memory only; it
   goes away on container restart and is rebuilt on demand.
2. Fetches HTTP Basic credentials from AWS Secrets Manager. Secret name is
   `<env>/devops/databases`. The JSON payload contains keys of the form
   `<domain_no_env>_opensearch_username` and `<domain_no_env>_opensearch_password`
   where `<domain_no_env>` is the domain name with a trailing `-<env>` stripped
   (if present) and dashes converted to underscores. Example:
   `qm-logger-stg4` → `qm_logger_opensearch_username` / `qm_logger_opensearch_password`.
3. Issues an HTTPS GET to the domain endpoint using HTTP Basic auth and
   returns parsed JSON.

## Credential model (mirrors lvx-aws)

- Base profiles are read from `~/.aws/config` (mounted from the host).
- Session profiles are expected in `~/.aws/credentials`
  (naming: `<base>-session`).
- Awsume is installed inside the container and reuses the host `~/.awsume`
  cache. On any expired-token error, the tool transparently runs
  `awsume <base> -o <base>-session` and retries the call.
- If awsume actually needs an MFA code (not just a cached refresh), the
  tool returns `needs_mfa: true` and the AI host must prompt the user for
  the code and re-invoke the same tool with `mfa_code=<code>`.

The user is never asked to run anything in their terminal.

## Read-only guarantee

Every OpenSearch call is a `GET`. There are no write, update, delete,
snapshot-create, reindex, or settings-mutation tools. The freestyle
`opensearch_get` tool refuses any path that is not on the read-only
allow-list.

## MyCompany QM naming convention

MyCompany QM indices on OpenSearch follow a strict two-part convention:

- **Index pattern:** `<client_id>_qm-<NNNNNN>` — e.g. `100262_qm-000001`,
  `100262_qm-000002`. The numeric suffix grows as new indices are rolled
  over. A single client can have any number of these.
- **Alias:** `<client_id>_qm` — e.g. `100262_qm`. All of a client's
  indices are bound to the single alias, and exactly one of them is the
  current **write-index** (`is_write_index: true` on the alias binding).
  The others serve read traffic through the same alias.

The client-oriented tools (`resolve_client`, `check_writable_indices`,
`list_closed_indices`, `check_shards`, `check_segments`) accept a
`client_id` argument that auto-derives both the index pattern and the
alias, so you don't have to compose them by hand. Every tool that accepts
`client_id` also still accepts an explicit `index_pattern` / `alias` for
cases outside the QM convention.

## Available tools

### Session / credentials

- `list_config_profiles()`
- `awsume_refresh_session(profile_name, mfa_code?, session_profile?, duration_seconds?)`

### Domain & connectivity

- `list_opensearch_domains(profile_name, env, force_refresh?, ...)`

### Indices, aliases, docs

- `check_indices(profile_name, env, domain, index_pattern="*", ...)`
- `count_docs(profile_name, env, domain, index, query_json?, ...)`
- `check_aliases(profile_name, env, domain, alias_pattern="*", ...)`
- `list_closed_indices(profile_name, env, domain, index_pattern="*", client_id?, ...)`
- `check_writable_indices(profile_name, env, domain, alias?, client_id?, ...)`
- `get_doc(profile_name, env, domain, index, doc_id, source_includes?, source_excludes?, ...)`
- `get_index_fields(profile_name, env, domain, index, ...)` — flattened
  field paths + types + raw mapping.

### MyCompany client shortcut

- `resolve_client(profile_name, env, domain, client_id, ...)` — one-shot:
  every `<client_id>_qm-*` index + the `<client_id>_qm` alias bindings +
  the current write-index in a single call.

### Shards / segments / capacity

- `check_shards(profile_name, env, domain, index_pattern="*", client_id?, ...)`
- `check_segments(profile_name, env, domain, index_pattern="*", client_id?, ...)`
- `cluster_health(profile_name, env, domain, level="cluster", ...)`
- `cluster_stats(profile_name, env, domain, ...)`
- `list_nodes(profile_name, env, domain, ...)`
- `get_capacity(profile_name, env, domain, ...)`

### Snapshots

- `list_snapshot_repos(profile_name, env, domain, ...)`
- `list_snapshots(profile_name, env, domain, repository, snapshot_pattern="_all", ...)`

### Freestyle escape hatch

- `opensearch_get(profile_name, env, domain, path, query_params_json?, ...)`
  — freestyle **READ-ONLY** GET restricted to the read-only allow-list.

### Release / updates

- `check_for_updates()` (via `version_notifier`).

Every tool that reaches AWS or the domain accepts the common
`session_profile`, `region`, `auto_refresh_on_expired`, `mfa_code`, and
`duration_seconds` args, with the same semantics as lvx-aws.

## Freestyle GET allow-list

The `opensearch_get` tool refuses any path outside these:

- `/_cat/*`
- `/_cluster/health`, `/_cluster/state`, `/_cluster/stats`, `/_cluster/settings`
- `/_nodes*`
- `/_snapshot*`
- `/_alias*`, `/_aliases*`
- `/_mapping*`, `/_settings*`
- `/_search`, `/_count`, `/_stats`
- `/_tasks*`, `/_ilm*`, `/_ism*`
- `/_plugins/*`, `/_opendistro/*`
- Per-index: `/<index>/_search`, `/<index>/_count`, `/<index>/_mapping`,
  `/<index>/_settings`, `/<index>/_alias`, `/<index>/_stats`,
  `/<index>/_shard_stores`, `/<index>/_segments`, `/<index>/_doc/<id>`

Any relative path, any path containing `..`, or any path outside the
allow-list is refused.

## VPN reachability hint

If the AWS SDK step succeeds (the domain is discovered and cached) but the
subsequent HTTPS call to the domain endpoint fails at the network layer
(DNS failure, connection refused, connect/read timeout), the response
carries `vpn_hint: true`, a `network_error_kind` label, and an
`instruction` telling the AI host to remind the user to double-check the
VPN. **This is a warning based on the exception type, not an actual VPN
probe** — no connectivity check is performed.

## Example prompts

### Discovery

```
List OpenSearch domains in stg4
  → list_opensearch_domains(profile_name="stg4", env="stg4")
```
```
Refresh the OpenSearch domain cache for eu1
  → list_opensearch_domains(profile_name="eu1", env="eu1", force_refresh=true)
```

### MyCompany client_id lookup

The most common ask: given a `client_id`, resolve the QM indices, the alias,
and the current write-index in one call.

```
Is there anything for client 106105 on qm-logger in stg4?
  → resolve_client(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", client_id="106105")
```
```
Which index is the current write-index for client 100262 in stg4?
  → check_writable_indices(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", client_id="100262")
```
```
Show me every closed 100262_qm-* index in stg4
  → list_closed_indices(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", client_id="100262")
```

> The client tools auto-derive both `<client_id>_qm-*` (index pattern) and
> `<client_id>_qm` (alias). If your index doesn't follow the QM convention,
> use the `alias` / `index_pattern` args instead of `client_id`.

### Indices

```
Is there an index qm-2026-07 on qm-logger in stg4?
  → check_indices(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", index_pattern="qm-2026-07*")
```
```
List every index on the qm-logger-stg4 domain
  → check_indices(profile_name="stg4", env="stg4", domain="qm-logger-stg4")
```
```
How many docs are in qm-2026-07 on qm-logger in stg4?
  → count_docs(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", index="qm-2026-07*")
```
```
Count docs matching campaignId=11111 in qm-* in stg4
  → count_docs(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", index="qm-*",
     query_json='{"query":{"term":{"campaignId":"11111"}}}')
```
```
Which fields are on 100262_qm-000001 in stg4?
  → get_index_fields(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", index="100262_qm-000001")
```
```
Fetch document abc123 from 100262_qm-000001 in stg4
  → get_doc(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", index="100262_qm-000001", doc_id="abc123")
```
```
Fetch only campaignId and callDate for doc abc123 on 100262_qm alias
  → get_doc(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", index="100262_qm", doc_id="abc123",
     source_includes="campaignId,callDate")
```

### Aliases

```
List every alias on the qm-logger-stg4 domain
  → check_aliases(profile_name="stg4", env="stg4", domain="qm-logger-stg4")
```
```
Which indices back the qm-current alias in stg4?
  → check_aliases(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", alias_pattern="qm-current")
```

### Shards & segments

```
Show me the shards (primary + replicas) for client 100262 in stg4
  → check_shards(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", client_id="100262")
```
```
Any unassigned shards on qm-logger-stg4?
  → check_shards(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4")
  # Response summary reports unassigned_count.
```
```
Segment breakdown for 100262_qm-000001 in stg4
  → check_segments(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", index_pattern="100262_qm-000001")
```
```
Segment count and total docs across every client 100262 index
  → check_segments(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", client_id="100262")
```

### Snapshots

```
List snapshot repositories on qm-logger-stg4
  → list_snapshot_repos(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4")
```
```
List every snapshot in the daily-s3 repo on qm-logger-stg4
  → list_snapshots(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", repository="daily-s3")
```
```
Just the snapshots from July 2026
  → list_snapshots(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", repository="daily-s3",
     snapshot_pattern="snap-2026-07-*")
```

### Cluster health / stats / capacity

```
What's the cluster health for qm-logger-stg4?
  → cluster_health(profile_name="stg4", env="stg4", domain="qm-logger-stg4")
```
```
Show me health per index on qm-logger-stg4
  → cluster_health(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", level="indices")
```
```
Cluster stats for qm-logger-stg4
  → cluster_stats(profile_name="stg4", env="stg4", domain="qm-logger-stg4")
```
```
Per-node stats on qm-logger-stg4 (heap, cpu, disk-used %)
  → list_nodes(profile_name="stg4", env="stg4", domain="qm-logger-stg4")
```
```
How much disk is left on the qm-logger-stg4 cluster?
  → get_capacity(profile_name="stg4", env="stg4", domain="qm-logger-stg4")
```

### Freestyle read-only GET

```
Show me the mappings for qm-2026-07 on qm-logger-stg4
  → opensearch_get(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", path="/qm-2026-07/_mapping")
```
```
Search qm-* for the last 5 docs
  → opensearch_get(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4",
     path="/qm-*/_search",
     query_params_json='{"size":"5","sort":"@timestamp:desc"}')
```
```
Cluster-level settings on qm-logger-stg4
  → opensearch_get(profile_name="stg4", env="stg4",
     domain="qm-logger-stg4", path="/_cluster/settings")
```

### MFA prompt

If the awsume cached MFA session has expired, the tool returns
`needs_mfa: true`. Re-invoke the same tool with the code:

```
Use awsume_refresh_session with profile_name: stg4, mfa_code: 123456
```

