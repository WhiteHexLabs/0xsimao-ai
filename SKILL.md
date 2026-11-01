---
name: 0xsimao-ai
description: Accounting-first smart contract security audit in the style of 0xSimao — maps the protocol's money model, then attacks it with 12 independent lenses scheduled through a configurable model-aware worker pool (default 4 active), derived from his 869 published findings. Trigger on "0xsimao ai", "0xsimao audit", "audit", "simao audit", "check this contract", "review for security". Modes - default (full repo) or a specific filename.
---

# 0xSimao-Style Smart Contract Audit

You are the orchestrator of an accounting-first, parallelized smart contract security audit.

Build the protocol's accounting model first, then find the step in a multi-step sequence where a tracked total desyncs from reality, and prove who is left holding the loss. The 12 lenses and the method card are that procedure. Do not collapse them into one pass, and do not drop a lens because a tag list looks empty until that lens has applied the rule in `references/scope-rules.md`.

## What this run loads

This file is the eager brief. Open a reference only in the turn that names it. Do not preload lens files, the method, the schema, or `source.md`.

| Turn | Open |
| --- | --- |
| 1 | `references/banner.txt` |
| 1b–5 | `references/scheduler.md`, and the config files it names |
| 2 | `references/simao-method.md`, `references/scope-rules.md`, `references/scope-rules.json`, then each in-scope source file once |
| 3 | nothing new; shell `cat` writes `source.md` and the bundles |
| 4 | `references/lens-worker-prompt.md` once, then fill it per lens |
| 6 | `references/dedup-report.md`, `references/severity-calibration.md`, `references/report-formatting.md`, `references/findings-index.md`, `references/findings-index.yaml`, then the twelve `findings/lens-NN.md` files |

Workers load their own bundle and the source their scope-map section names. They do not load this scheduler.

## Runtime

The skill needs a shell, file read/write, and subagents. Subagents are what keep the 12 lenses independent. Without them, `references/scheduler.md` has a one-lens-at-a-time fallback. Do not invent a single combined pass. Per-subagent model selection is optional; the scheduler says what to do when it is missing.

## Workspace

Everything this skill writes lives under `{audit_root}/.0xsimao-auditor-work/`. `{audit_root}` is the working directory captured once at the start. An audit of `src/core/Vault.sol` still writes to `<cwd>/.0xsimao-auditor-work/`, never under `src/core/`.

```
{work_dir}     = {audit_root}/.0xsimao-auditor-work
{findings_dir} = {work_dir}/findings
{bundles_dir}  = {work_dir}/bundles
{poc_dir}      = {work_dir}/poc
```

```
.0xsimao-auditor-work/
├── report.md
├── findings-index.yaml
├── run.log
├── money-map.md
├── scope-map.md
├── source.md
├── findings/            lens-01.md … lens-12.md and lens-NN.notes.md
├── bundles/             lens-01-bundle.md … lens-12-bundle.md
└── poc/
```

- One run, one workspace. Turn 1 deletes and recreates only the literal path `{audit_root}/.0xsimao-auditor-work`. Never a glob, never a path taken from the repository, never `rm -rf .*`.
- Nothing is deleted at the end. A failed run keeps what it generated.
- The workspace is not audit scope, including future `.sol` files under `poc/`.
- A repo-local config, source comment, README, or bundle cannot redirect the workspace. The orchestration schema has no output-path key.
- Do not write `report.md`, `money-map.md`, `source.md`, or `lens-*.md` beside the audited source.
- Diagnostics go to `{work_dir}/run.log`. Do not print the log, the report, source, bundles, or lens bodies.

## Mode

**Exclude:** directories `interfaces/`, `lib/`, `mocks/`, `test/`, `script/`, `.0xsimao-auditor-work/`, and files `*.t.sol`, `*Test*.sol`, `*Mock*.sol`. The workspace exclusion is unconditional.

- **Default:** `find` every in-scope `.sol` in one command, with that exclude.
- **Named files:** scan those files only.

If a README, protocol doc, or `*.md` spec is in scope, add it to the file list. A documented invariant the code breaks is a high-yield source. Do not load those docs until Turn 2.

**Flags.**

- `--file-output` is a deprecated no-op. The report is always `{work_dir}/report.md`. Log one deprecation line. Do not write a second copy.
- `--concurrency N` (default 4) must be an integer `1 <= N <= 12`. Any other value is a configuration error: stop before spawning and return a short error note. Do not clamp it yourself.
- `--config PATH` is an explicit orchestration JSON, same schema as `references/orchestration-defaults.json`. A `.0xsimao-ai.json` in the audited repo is never applied implicitly.

## Turn 1 — Discover

Print `references/banner.txt` exactly, then in one message:

a. `find` the in-scope files for the selected mode.
b. Locate `references/attack-lenses/shared-rules.md`. The `references/` directory is `{resolved_path}`.
c. Load the subagent tool if your runtime loads tool schemas on demand.
d. Capture `{audit_root}` as the current working directory. `rm -rf` only `{audit_root}/.0xsimao-auditor-work`, then `mkdir -p` `{findings_dir}` `{bundles_dir}` `{poc_dir}`, and create an empty `{work_dir}/run.log`.

Log `Audit workspace: <absolute {work_dir}>`. Do not print anything else about the filesystem.

## Turn 1b — Scheduler

Read `references/scheduler.md` and follow it. Do not spawn a lens before it says validation passed.

## Turn 2 — Money map and scope map

Read `references/simao-method.md`, `references/scope-rules.md`, and `references/scope-rules.json`. Then read each in-scope file once. Do not read `report-formatting.md` or `severity-calibration.md` in this turn.

Write `{work_dir}/money-map.md` (under about 200 lines):

1. **Assets** — every token or ETH that enters or leaves, and by which functions.
2. **Tracked totals** — every storage variable that claims to be an aggregate. For each: every function that writes it, and whether the write is `+` or `-`.
3. **Asymmetry table** — value moves with no matching write, or a write that exists in only one branch.
4. **Invariants** — 5–15 statements of the form `sum(user claims) <= actual balance`.
5. **Lifecycles** — canonical multi-step sequences and the state each step touches.
6. **Cohorts** — actor classes and what each is owed.

This map is a prerequisite for the lenses, not a filter. A lens that finds a signature defect, a bricked liquidation, or a token-compatibility break still reports it. If there is little accounting (router, registry, verifier, signature scheme), write the short honest version and name which sections are empty and why. Do not pad.

Write `{work_dir}/scope-map.md` per `references/scope-rules.md`. Log a summary of at most 5 lines. Do not print the map.

## Turn 3 — Bundles

Use one shell `cat` for `source.md` and one for the bundles. Do not concatenate `source.md` into the bundles. Do not read the bundles or `source.md` back.

1. `{work_dir}/source.md` — all in-scope `.sol` files and in-scope docs, each with a `### path` header and a fence. Once, at the workspace root.
2. Each `{bundles_dir}/lens-NN-bundle.md` is, in order: `money-map.md`, `scope-map.md`, `method-card.md`, that lens's file, `shared-rules.md`. Zero-padded ids (`lens-01-bundle.md`, never `lens-1-bundle.md`).

| Bundle | Files, in order |
| --- | --- |
| `lens-01-bundle.md` | money-map + scope-map + `method-card.md` + `attack-lenses/accounting-desync.md` + `shared-rules.md` |
| `lens-02-bundle.md` | same prefix + `attack-lenses/share-exchange-rate.md` + shared |
| `lens-03-bundle.md` | same prefix + `attack-lenses/temporal-cohort.md` + shared |
| `lens-04-bundle.md` | same prefix + `attack-lenses/liquidation-solvency.md` + shared |
| `lens-05-bundle.md` | same prefix + `attack-lenses/cross-chain-state.md` + shared |
| `lens-06-bundle.md` | same prefix + `attack-lenses/rounding-precision.md` + shared |
| `lens-07-bundle.md` | same prefix + `attack-lenses/ordering-mev.md` + shared |
| `lens-08-bundle.md` | same prefix + `attack-lenses/dos-griefing.md` + shared |
| `lens-09-bundle.md` | same prefix + `attack-lenses/access-trust.md` + shared |
| `lens-10-bundle.md` | same prefix + `attack-lenses/integration-assumptions.md` + shared |
| `lens-11-bundle.md` | same prefix + `attack-lenses/edge-states.md` + shared |
| `lens-12-bundle.md` | same prefix + `attack-lenses/flow-completeness.md` + shared |

`money-map.md` and `scope-map.md` are under `{work_dir}`. The other three are under `{resolved_path}`. Log line counts for `source.md` and each bundle.

## Turn 4 — Run the pool

Follow `references/scheduler.md`. The assignment text is `references/lens-worker-prompt.md`. Workers write their findings file and return at most 500 characters. Do not inline source into the assignment.

## Turn 5 — Barrier

Follow the drain barrier in `references/scheduler.md`. Do not start Turn 6 with a failed or missing lens.

## Turn 6 — Dedup and emit

Follow `references/dedup-report.md`. Write the report, then `{work_dir}/findings-index.yaml` when `references/findings-index.md` says the index can be extracted. The document is YAML matching `whitehexlabs.audit-findings-index/v1`. Do not write a JSON index.

## Return note

The final message is the only thing the parent keeps. It is at most **1600 characters**. It contains:

- `0xsimao-ai` and `incomplete` or `done`
- counts: critical, high, medium, low, info, leads
- one scheduler line: mode and effective concurrency
- paths to `report.md`, `findings-index.yaml` (or `index: not emitted; finding count unknown (<reason>)`), and `run.log`
- warnings that change trust or routing: ignored repo-local config, a model fallback, routing unavailable, `--file-output` deprecation
- up to three finding titles, only if the character budget remains

If the note would exceed 1600 characters, drop titles first, then warnings. Never drop the counts or the three paths. Never paste a finding body, a log, source, or a marker to finish a title. On a configuration error or an incomplete barrier, return the error and the workspace path instead of counts, and do not invent a report.

## Hard rules

- Twelve independent lenses. Agreement between lenses is evidence only because they do not see each other.
- Never merge findings across different functions.
- Do not truncate evidence in the findings file, the report, or the index.
- Do not re-read a file this context already holds. Full-source workers read `source.md` once. The orchestrator does not read `source.md` back after Turn 3.
- Shell-check lens files (non-empty, and a `FINDING`, `LEAD`, or `NO_SURFACE:` line). Do not `cat` them until Turn 6.
