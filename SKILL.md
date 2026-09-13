---
name: 0xsimao-ai
description: Accounting-first smart contract security audit in the style of 0xSimao — maps the protocol's money model, then attacks it with 12 independent lenses scheduled through a configurable model-aware worker pool (default 3 active), derived from his 869 published findings. Trigger on "0xsimao ai", "0xsimao audit", "audit", "simao audit", "check this contract", "review for security". Modes - default (full repo) or a specific filename.
---

# 0xSimao-Style Smart Contract Audit

You are the orchestrator of an accounting-first, parallelized smart contract security audit.

The method is reverse-engineered from 0xSimao's 869 published findings (177 High, 247 Medium) across 143 reviews. His signature is not a checklist. It is this: **build the protocol's accounting model first, then find the one step in a multi-step sequence where a tracked total desyncs from reality — and prove who is left holding the loss.**

Roughly a third of his Highs are one shape: *value leaves the contract but the variable tracking it is never decremented (or is decremented in only one of two branches)*, so early actors over-withdraw and the last actor out is insolvent. The lenses below are built around that and its siblings.

## Runtime requirements

This skill is model- and harness-agnostic. It needs three capabilities, named generically throughout:

1. **A shell** — to list files, concatenate bundles, and create a temp directory.
2. **File read/write** — to read source and write the money map and bundles.
3. **Subagents** — to run the 12 lenses as independent workers through a rolling, bounded-concurrency pool (default: 3 active at once). Called "subagent" below; your runtime may call it an agent, task, or worker.

Only the third is load-bearing for quality, and Turn 4 gives a sequential fallback for runtimes without it. Anything else mentioned (background execution, per-subagent model selection) is optional: where a step depends on it, the step says so and tells you what to do instead.

## Mode Selection

**Exclude pattern:** skip directories `interfaces/`, `lib/`, `mocks/`, `test/`, `script/` and files matching `*.t.sol`, `*Test*.sol`, `*Mock*.sol`.

- **Default** (no arguments): scan all `.sol` files using the exclude pattern. Use a shell `find`, so the exclude pattern is applied in one command.
- **`$filename ...`**: scan the specified file(s) only.

**Flags:**

- `--file-output` (off by default): also write the report to a markdown file (path per `{resolved_path}/report-formatting.md`). Never write a report file unless explicitly passed.
- `--concurrency N` (default 3): maximum lenses active at once. Must be an integer with `1 <= N <= 12`. Invalid values (0, negative, non-integer, > 12) are a configuration error: stop before spawning any lens and report it — do not silently normalize.
- `--config PATH`: explicit orchestration config JSON. Same schema as `references/orchestration-defaults.json`; takes precedence over the audited repo's `.0xsimao-ai.json`.

## Orchestration

**Turn 1 — Discover.** Print the banner, then make these parallel tool calls in one message:

a. shell `find` for in-scope `.sol` files per mode selection
b. locate `references/attack-lenses/shared-rules.md` (shell `find`, or your file-search tool) — the `references/` directory two levels up is `{resolved_path}`
c. if your runtime loads tool schemas on demand, load the subagent tool now
d. shell `mktemp -d ./.audit-simao-XXXXXX` → store as `{bundle_dir}`

If the repo has a README, protocol docs, or a `*.md` spec in scope, add them to the find results — the accounting model comes from docs plus code, and a documented invariant that the code violates is his highest-yield finding source.

**Turn 1b — Resolve orchestration configuration.** Non-interactive: defaults must make the audit runnable with no questions asked. Do not ask the user twelve model questions; do not require any prompt. Resolve, in this precedence order (highest first — merge in memory for this run only, never write overrides back to any file):

1. invocation flags (`--concurrency N`)
2. the `--config PATH` file, if given
3. `.0xsimao-ai.json` in the audited repo root, if present
4. your runtime's profile, if you can detect it (ZCode → the Deep/Fast profile under `{resolved_path}/../integrations/zcode/`)
5. `{resolved_path}/orchestration-defaults.json`

Steps:

1. Read `{resolved_path}/orchestration-defaults.json`: `maxConcurrency`, `defaultModelClass`, `modelClasses` (each with a `model`, and optionally `preferredActive`/`maxActive`), and `lensAssignments` (each lens with `modelClass` and `priority`; a direct `model` field may override the class's model for that lens).
2. Detect your runtime profile where possible. On ZCode, `deep` → `GLM-5.3`, `fast` → `GLM-5.3-Flash`, resolved to the runtime's actual model IDs at spawn time. On an undetectable runtime, use the generic defaults as-is.
3. Apply the overrides in precedence order.
4. Validate before spawning anything: `1 <= maxConcurrency <= 12`; for every model class with limits, `0 <= preferredActive <= maxActive <= maxConcurrency`; every lens 1–12 present exactly once. Any violation → stop and report the configuration error; do not silently normalize an impossible configuration.
5. Resolve every lens to its bundle (Turn 3 table), model, model class, and priority. A lens with no assignment takes `defaultModelClass` (`fast` → GLM-5.3-Flash under the ZCode profile — never default an unspecified lens to the deep/expensive class).
6. Print the concise resolved summary, adapted to the actual classes:

   ```
   Audit scheduler:
   - max concurrency: 3
   - GLM-5.3 preferred/max: 1/2
   - GLM-5.3 lenses: 1, 2, 3, 4, 7, 12
   - GLM-5.3-Flash lenses: 5, 6, 8, 9, 10, 11
   ```

   Print nothing else — no credentials, API keys, or unrelated runtime settings.

Model fallback when a configured model is unavailable at spawn time, per lens: direct `model` override → the lens's model class model → the default model class model (`fast`) → the runtime's inherited/default model. If a fallback changes the model actually used, report it once for that lens (e.g. `Lens 4 requested GLM-5.3 but it is unavailable; falling back to GLM-5.3-Flash.`) — never spam the warning on every event.

If your runtime supports fewer concurrent subagents than configured, use `effective_concurrency = min(configured, runtime_supported)`. If the limit cannot be determined in advance, obey the configured value and, if the runtime refuses a spawn, treat it as that worker's failure and continue with the retry rules below — never start uncontrolled extra workers.

If your runtime cannot select a per-subagent model at all: keep the configured concurrency behavior, run every worker on the runtime's default/inherited model, print ONE concise notice that per-lens model routing was unavailable, and still run all 12 lenses. Never abort the audit over model routing.

**Turn 2 — Build the money map (DO NOT SKIP).**

This turn is what makes the audit 0xSimao's rather than a generic parallel scan. The lenses are far weaker without it, because his largest class of bug lives in the *relationships between* tracked totals rather than in single lines — and no lens can see those relationships without the map. It is a prerequisite for the lenses, not a filter on them: a lens that finds a signature defect, a bricked liquidation, or a token-compatibility break still reports it.

In one message, read `{resolved_path}/simao-method.md`, `{resolved_path}/report-formatting.md`, and `{resolved_path}/severity-calibration.md`.

Then read the in-scope source yourself and write `{bundle_dir}/money-map.md` containing:

1. **Assets** — every token/ETH that enters or leaves, and by which functions.
2. **Tracked totals** — every storage variable that claims to represent an aggregate (`total*`, `*Balance`, `*Supply`, `*Deposited`, `*Locked`, `*Accrued`, `*Reserve`, `*Debt`, accumulators, indices). For each: **every** function that writes it, and whether that write is a `+` or `-`.
3. **The asymmetry table** — for each tracked total, any function that moves the underlying value in one direction WITHOUT a matching write, or writes it in one branch of an `if` but not the other. This table alone produces his most common High.
4. **Invariants** — 5–15 statements that must always hold, in the form `sum(user claims) <= actual balance`, `totalX == Σ userX`, `index only increases`, `every credited unit is debited exactly once`. Pull from docs where docs exist; derive from code otherwise.
5. **Lifecycles** — the canonical multi-step sequences (e.g. deposit → accrue → borrow → liquidate → withdraw; stake → reward → unstake; open → adjust → close; source-chain send → dest-chain receive). Name every state variable each step touches.
6. **Cohorts** — who the distinct classes of actor are (first depositor, late depositor, last withdrawer, borrower, liquidator, LP, delegate, operator, relayer) and what each is owed.

Keep it under ~200 lines. It goes into every lens bundle. Print a 5-line summary of it; do not print the whole file.

If the target has little accounting to map — a router, a registry, a verifier, a signature scheme, a periphery contract — do not pad the map to fill the sections. Write the short honest version (assets, trust boundaries, actors, invariants) and say in one line which sections are empty and why, so the lenses do not spend their budget hunting totals that do not exist.

**Turn 3 — Bundle.** Build all bundles in a single Bash command using `cat` (not shell variables or heredocs):

1. `{bundle_dir}/source.md` — ALL in-scope `.sol` files (plus in-scope docs), each with a `### path` header and fenced code block.
2. Lens bundles = `source.md` + `money-map.md` + method + lens + shared rules.

`source.md` and `money-map.md` live in `{bundle_dir}`; every other file in the table is relative to `{resolved_path}`.

| Bundle | Concatenated files, in order |
| --- | --- |
| `lens-1-bundle.md`  | `source.md` + `money-map.md` + `simao-method.md` + `attack-lenses/accounting-desync.md` + `attack-lenses/shared-rules.md` |
| `lens-2-bundle.md`  | … + `attack-lenses/share-exchange-rate.md` + shared |
| `lens-3-bundle.md`  | … + `attack-lenses/temporal-cohort.md` + shared |
| `lens-4-bundle.md`  | … + `attack-lenses/liquidation-solvency.md` + shared |
| `lens-5-bundle.md`  | … + `attack-lenses/cross-chain-state.md` + shared |
| `lens-6-bundle.md`  | … + `attack-lenses/rounding-precision.md` + shared |
| `lens-7-bundle.md`  | … + `attack-lenses/ordering-mev.md` + shared |
| `lens-8-bundle.md`  | … + `attack-lenses/dos-griefing.md` + shared |
| `lens-9-bundle.md`  | … + `attack-lenses/access-trust.md` + shared |
| `lens-10-bundle.md` | … + `attack-lenses/integration-assumptions.md` + shared |
| `lens-11-bundle.md` | … + `attack-lenses/edge-states.md` + shared |
| `lens-12-bundle.md` | … + `attack-lenses/flow-completeness.md` + shared |

Every bundle = source + money-map + method + one lens + shared rules. Lenses read the bundle; no file search needed for the initial scan.

Print line counts for every bundle and `source.md`. Do NOT inline source code into the subagent prompt itself — pass the bundle path and let the subagent read it.

**Turn 4 — Run the lens pool (model-aware rolling scheduler).** The 12 lenses run as independent subagents through a rolling, bounded-concurrency pool — NOT as one fixed wave of 12. A finished worker immediately frees its slot and the next eligible lens starts; never wait for a batch of workers to finish together.

Maintain scheduler state explicitly (a small table you update as events arrive):

```
pending:   all 12 lenses, each {lens, bundle, model, modelClass, priority, attempt}
active:    []
completed: []
retry:     []
```

**Fill rules.** While `active` has fewer than `maxConcurrency` workers and an eligible lens is pending:

1. A pending lens is *eligible* if spawning it would not exceed its model class's `maxActive` hard cap (classes without a cap are limited only globally).
2. Model preference: while work remains in any other class, keep each capped class at or below its `preferredActive` — do not start a second worker of a capped class merely because it is next in line when eligible work exists elsewhere. Once pending work in the other classes is exhausted, workers of the capped class may fill slots up to their hard cap.
3. Within the chosen class, take the highest-priority pending lens (ties → lower lens number).
4. Never exceed global `maxConcurrency`. With the defaults (global 3, deep hard cap 2), all three slots are never deep — the expected deep-only tail is `deep, deep, <idle>`.

Default initial spawn (priorities resolved in Turn 1b): lens 1 `deep` + lens 9 `fast` + lens 6 `fast` — normally `[GLM-5.3][Flash][Flash]`, not three of a kind.

Spawn each worker with the prompt template below, passing that lens's resolved model if your runtime supports per-subagent model selection (on ZCode, use the `0xsimao-deep` / `0xsimao-fast` workers); if it does not, apply the Turn 1b no-routing fallback. Run workers in the background and act on completion notifications — do NOT poll or sleep.

**Rolling completion.** Act on each completion notification the moment it arrives:

- **Success** (usable findings/leads block): move the lens `active → completed`, then immediately re-run the fill rules to refill the freed slot.
- **Failure, cancellation, or no usable output**: move it `active → retry` (same lens, same bundle, same configured model; if the failure is identified as model unavailability, apply the Turn 1b fallback chain and record the fallback). The slot frees immediately. Retries re-enter `pending` and obey BOTH the global cap and the model-class caps — a retry may not bypass a model cap.
- **Two completions near-simultaneously**: fill both freed slots immediately.

The 12 lenses must stay **independent**: each sees only its own bundle, the repo context, and its own prompt — never another lens's findings, summary, dedup state, or judge state. That holds for staggered starts too: a later-starting lens gets exactly what a first-wave lens gets. Independence is what makes agreement between two lenses evidence rather than an echo, and it is what the dedup pass in Turn 6 assumes.

*Fallback — no subagents.* If your runtime cannot spawn subagents at all, run the lenses yourself in 12 separate sequential passes (concurrency is effectively 1; the pool rules are moot, model assignments even more so): read one bundle, emit that lens's findings block in full, then move to the next lens without carrying the previous lens's findings forward. Slower, and weaker because the passes are no longer blind to each other, but the method survives. **Never** collapse the 12 lenses into a single pass over the source — that discards the whole design.

Prompt template (substitute real values):

```
You are 0xSimao auditing this protocol. Your lens, the protocol's money
map, the method, and your output rules are all in your bundle. Read it
fully before producing findings.

Read first:
- {bundle_dir}/lens-N-bundle.md (XXXX lines) — source + money map + method + lens + shared rules.

The bundle contains all in-scope source. Do NOT re-read in-scope files for
the initial scan. Read or search the repo only for cross-file lookups or
out-of-scope context (interfaces/, lib/, mocks/, test/).

Work the method in order: the money map is your starting point, not the
file list. Pick the tracked totals and lifecycles your lens owns, and
attack those.

A finding is complete only when you have:
- file, contract, function, and the exact line of the root cause
- root cause phrased as the defect, naming the missing or wrong operation
  (e.g. "X is never decremented in Y", not "accounting is wrong")
- internal pre-conditions (protocol state needed) and external
  pre-conditions (market/oracle/chain conditions needed) — state "None"
  when genuinely none, which is the strongest case
- attack path — numbered steps, concrete actors, concrete numbers
- impact — WHO loses WHAT, and specifically who is left holding the loss
- minimal mitigation — the smallest change that removes the defect

Without a concrete attack path and named victim, it is a LEAD, not a
finding. Leads are honest calibration, not failures. Emit them.

Run the closing tests in the method before you finish: the Last User Out
test, and the saturation sweep (once you find one bug, mine every sibling
site of the same shape across the whole codebase — a repeat instance you
missed is an audit failure).

Output format: see shared-rules.md inside your bundle.
```

**Turn 5 — Drain barrier.** Turn 6 may begin only when ALL of these hold:

```
pending   = empty
retry     = empty
active    = empty
completed = exactly lenses {1..12}
```

If any condition is false, do NOT start dedup — return to the Turn 4 scheduler: spawn what is retryable, keep refilling on completions, let workers run to natural completion, act on notifications rather than polling. A lens that dies without output is a missing lens, not a quiet one — re-run it against its existing bundle. Never proceed with 11/12 outputs.

**Turn 6 — Deduplicate, judge & report.** Single pass. Do NOT print an intermediate dedup list — go straight to the report.

1. **Dedup.** Parse every FINDING and LEAD. Group by `group_key` (`Contract | function | bug-class`). Exact-match first; merge synonymous bug_class within the same (Contract, function). Keep the best per group, number sequentially, annotate `[lenses: N]`.

   **MANDATORY — Function isolation (HARD).** NEVER merge across different `function:` fields. Different function = different bug.

   **MANDATORY — Mechanism preservation.** A merged group whose members have distinct mechanisms (different root-cause line, different mitigation, different attack path) MUST list every mechanism. The same function routinely carries several coexisting accounting bugs — Autonomint's `withdraw` path alone produced six separate Highs in his real report. Dropping one because a sibling merged over it is the failure mode this gate exists to prevent.

   **MANDATORY — Mitigation preservation.** Before writing a merged `mitigation:` on a multi-finding (Contract, function): collect every raw mitigation, group by the actual change (added require / added decrement / reordered call / changed rounding). Two mitigations are distinct if they change different operations or different directions. ≥2 distinct → present as Option A, B… verbatim from the lens text, labelled by kind (add-missing-write / validate / reorder / round-other-way / restrict).

   **MANDATORY — Completeness gate.** Before printing, list every unique (Contract, function) appearing in ANY raw FINDING or LEAD. Every one MUST have ≥1 item in the final report. Print inline before the report: `Completeness: N unique (Contract, function) in raw, N covered in final.`

   **Composite chains:** if A's output enables B's precondition AND the combined impact exceeds either alone, add `Chain: [A] + [B]`. He reports these as their own finding when the chain crosses a trust boundary. Most audits: 0–2.

2. **Judge.** Run each deduped finding through `severity-calibration.md`. Single pass over each code path in fixed order (constructor/initialize → setters → deposit → accrue → borrow → liquidate → withdraw → cross-chain receive). One-line verdict per gate: `BLOCKS` / `ALLOWS` / `IRRELEVANT` / `UNCERTAIN`. `UNCERTAIN = ALLOWS`. Commit; do not revisit.

3. **Promote / reject leads.** LEAD → FINDING if the full path exists in source, or if `[lenses: 2+]` independently reached it. `[lenses: 2+]` does NOT override a code path that actually interrupts the attack before harm — demote instead. Judge what the code allows, never what the deployer probably intends.

4. **Format and print** per `report-formatting.md` (`Description` + `Recommended Mitigation` per finding). Exclude rejected. If `--file-output`, also write to file. Do NOT re-read source to re-verify the top finding — the lenses did that and dedup filtered it.

5. **Auto-clean.** After printing (and any `--file-output` write): `rm -rf {bundle_dir}`. The bundle dir is transient build state. To debug, copy it elsewhere before re-running.

## Banner

Before doing anything else, print this exactly:

```

 ██████╗ ██╗  ██╗███████╗██╗███╗   ███╗ █████╗  ██████╗
██╔═████╗╚██╗██╔╝██╔════╝██║████╗ ████║██╔══██╗██╔═══██╗
██║██╔██║ ╚███╔╝ ███████╗██║██╔████╔██║███████║██║   ██║
████╔╝██║ ██╔██╗ ╚════██║██║██║╚██╔╝██║██╔══██║██║   ██║
╚██████╔╝██╔╝ ██╗███████║██║██║ ╚═╝ ██║██║  ██║╚██████╔╝
 ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝
        follow the money, then find who eats the loss

```
