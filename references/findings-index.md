# Findings index

Write `{work_dir}/findings-index.json` in Turn 6. If the execution prompt gives a findings-index path or a JSON schema, write that path and satisfy that schema. When the prompt's schema and this file disagree, follow the prompt, keep `schema` equal to `whitehexlabs.audit-findings-index/v1` if the prompt allows it, and log the field mapping in `run.log`. This repository's schema is `references/findings-index.schema.json`.

The index is what cross-skill dedup consumes. The report stays the human writeup. Do not drop a finding or a lead from the index because it is already in the report.

## Completeness

One entry per final report item, plus every surviving lead.

- `kind: finding` uses severity `critical`, `high`, `medium`, `low`, or `info` (Info in the report is `info`).
- `kind: lead` uses severity `unverified`.
- `id` matches the report id (`H-1`, `M-1`, …). Leads use `LEAD-1`, `LEAD-2`, … in the order they appear.
- `group_key` is `Contract | function | bug-class`, the same key Turn 6 deduped on.
- `lenses` lists every lens id that reported the merged group.
- `locations` includes every root-cause line. `line` is the exact line, never a guess and never `0`.
- `description`, `impact`, `mitigation`, and `root_cause` are the full text. When dedup preserved Option A / Option B mitigations, `mitigation` contains every option.
- `evidence[].detail` is the full cited line or lines the finding depends on. Do not shorten evidence to a preview. Do not paste unrelated files.
- `counts` matches the entries: critical/high/medium/low/info by severity, `leads` by kind. This skill's report scale is High / Medium / Low / Info, so `critical` is `0` unless a parent schema asked for that bucket.
- `run_id` is the id from the execution prompt, or `""` when the prompt has none.
- `scope` is the in-scope paths. `target` is the invocation target (repo root, or the named files).
- Paths in `report_path` and `log_path` are relative to the audit root.

A finding with empty `evidence` is invalid. A lead still names the file and line it smells in, and its `evidence` detail is what was seen plus what could not be established — not a truncated smell.

Write the JSON with a shell or a file tool. Do not also print it.
