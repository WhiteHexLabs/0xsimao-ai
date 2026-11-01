# Findings index

Write one YAML document, `{work_dir}/findings-index.yaml`, in Turn 6, after `report.md` exists. The shape is `references/findings-index.yaml`. The orchestrator accepts that YAML document and rejects JSON: a `.json` findings file is native output, not this index, and does not count. Do not write a second copy.

If the execution prompt gives an output path that ends in `.yaml`, write the document there instead, and do not also write one under `{work_dir}`. Two index files are ambiguous.

## When to write nothing

`run_id` and `step` are copied from the execution prompt. `step` is an integer. If either value is absent, do not write the file.

Write the file only when every indexed finding can be filled without guessing. If a report finding has no exact line, no root cause, or no impact, write no index. An empty `findings` list is valid only when the report itself states zero High, Medium, Low, and Info findings. Say which case you took in the return note:

- index written: `index: <path>`
- not written: `index: not emitted; finding count unknown (<reason>)`

Do not drop findings to keep the file small.

## Top level

Exactly these keys, and no others:

| Key | Value |
| --- | --- |
| `schema` | `whitehexlabs.audit-findings-index/v1` |
| `run_id` | the prompt's run id, unchanged |
| `step` | the prompt's step, as an integer |
| `coverage` | `summary` (required string) and `limitations` (list of strings) |
| `findings` | the list below |

`coverage.summary` states what was reviewed and how: file count, 12 lenses, which lenses read `source.md`, which read a primary set. `limitations` names real gaps. Include unverified leads here, one line each (`Contract::function — smell`). Leads are not findings, so they stay out of `findings`. An empty limitations list is allowed when there are none.

## One entry per report finding

Report order. `id` is `local-001`, `local-002`, … unique in this file. `original_finding_id` is the report id (`H-1`, `M-1`, `L-1`, `I-1`), or `null` when the report item has no id.

Exactly these keys on each finding:

| Key | Rule |
| --- | --- |
| `title` | The report title. Full text. |
| `root_cause` | The defect, the file, the line, and the missing or wrong operation. Full text. Every preserved mechanism stays. |
| `mechanism` | The numbered attack path with actors and amounts. Full text. |
| `locations` | Non-empty list of `path:line` strings (`src/Vault.sol:120`). One entry per root-cause line. No objects. |
| `preconditions` | List of strings. Internal and external preconditions. When both are genuinely none, one entry: `None`. |
| `impact` | Who loses what, and who is left holding the loss. Full text. |
| `requires_privileged_actor` | YAML boolean `true` or `false`, or the string `unknown`. Never omit it. Never quote `true` or `false`. `unknown` is not `false`. Use `true` only when the path needs a privileged role. Use `false` when an unprivileged caller, including a flash-loan caller, can do it. Use `unknown` when the writeup does not establish which. |
| `severity_hint` | `HIGH`, `MEDIUM`, `LOW`, `INFO`, or `CRITICAL`, matching the report bucket. `null` if the report states no severity. Advisory only. |
| `evidence` | Non-empty. Each item is exactly `artifact_id: primary-report` and `locator`. The locator names the heading in the primary report, for example `section 'H-1' of the primary report`. |

The report section holds the mitigation. This index has no mitigation field. Do not add `group_key`, `lenses`, `kind`, `counts`, or `producer`.
