---
applyTo: "servers/**"
---

Review changes under `servers/**` **only** against the five security-by-design rules numbered below. Do not comment on anything else — no style, performance, naming, test-coverage, or general code-quality feedback. This is a security-only pass; out-of-scope comments make it unclear which of the 5 rules was actually violated.

For every violation, cite the rule number explicitly in the comment, e.g. "Rule 3 violated: ...". Never flag something without citing its rule number. Flag any violation as a required change, not a nitpick.

1. **Trust the boundary** — External data (API responses, ticket bodies, user input) must never be interpolated raw into an LLM prompt or tool response. It must first be parsed into specific, typed fields. Flag any f-string/`.format()`/string concatenation that embeds a raw external payload directly.
2. **Minimize exposure** — Every field a tool touches should have a deliberate reason to reach the LLM, be stored, or be logged. Flag fields passed through "just in case" (e.g. forwarding an entire API response object instead of the specific fields used).
3. **Validate output as untrusted** — Any LLM response used to drive an action (calling another API, writing data, choosing a branch of logic) must be validated against an explicit schema (e.g. `pydantic`/`jsonschema`) before use. Flag bare `json.loads()` of an LLM response used without validation.
4. **Fail visibly** — Flag bare `except:` or `except Exception: pass`/broad catch-alls that swallow errors, especially around auth or external calls. Errors must propagate or be explicitly, narrowly handled with a named exception type.
5. **Review at scale** — Flag any new external dependency, credential handling, or tool/skill capability added without a corresponding test or validation step in CI.
