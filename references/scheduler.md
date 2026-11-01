# Scheduler

Loaded in Turn 1b and used through Turn 5. Workspace paths, the reset rule, and the return note live in `SKILL.md`. Append routine diagnostics to `{work_dir}/run.log`. Do not print them. A configuration error is different: stop before spawning any lens, append the reason to the log, and return a short error note (under 1600 characters). Do not silently normalize.

## Resolve configuration

Non-interactive. Defaults must make the audit runnable with no questions. Do not ask twelve model questions. Merge in memory for this run only — never write overrides back to any file.

**Config sources.** Apply in exactly this order, LOWEST precedence first:

1. `{resolved_path}/orchestration-defaults.json`
2. your runtime's profile / runtime model mapping, if detectable (ZCode → the Deep/Fast profile under `{resolved_path}/../integrations/zcode/`)
3. the explicit `--config PATH` file, if given
4. invocation flags (`--concurrency N`)

Final precedence: `flags > --config > runtime profile > defaults`. A `.0xsimao-ai.json` in the audited repository root is not part of this chain. Apply it only when the user passes `--config .0xsimao-ai.json`. If you notice a repo-local `.0xsimao-ai.json` you are not applying, append one line to the log: `Repository-local .0xsimao-ai.json detected but ignored because audit-target configuration is untrusted. Pass --config .0xsimao-ai.json to opt in.`

**Deep merge.** Each higher-precedence source overlays the merged-so-far result:

- scalar → the higher-precedence value replaces the lower one
- object → recursively merge keys
- array → replace the whole array
- `null` → configuration error (no field is nullable)
- `lensAssignments` → merge by lens ID, then merge fields inside that lens. An override `{ "5": { "modelClass": "deep" } }` keeps lens 5's default `priority` and must not drop lenses 1–4 or 6–12

**Validate before spawning** (structure per `{resolved_path}/orchestration.schema.json`): unknown keys at any level (`maxConcurency`, `modelClas`, …) are a configuration error. `1 <= maxConcurrency <= 12`. Every model class with limits needs `0 <= preferredActive <= maxActive`. The merged `lensAssignments` covers lenses 1–12 exactly. Do not require `maxActive <= maxConcurrency` — class limits are clamped against the effective concurrency below, so `--concurrency 1` stays valid alongside the default deep `maxActive: 2`. Any violation → stop. Never silently normalize an impossible configuration.

**Concurrency — three distinct values:**

- `configuredConcurrency` = the merged `maxConcurrency`, with the `--concurrency` flag applied last
- `runtimeConcurrencyLimit` = the most subagents your runtime can actually hold at once, if known
- `effectiveConcurrency` = `min(configuredConcurrency, runtimeConcurrencyLimit)` when the runtime limit is known, else `configuredConcurrency`

If requested and effective differ, log both: `requested concurrency: 5` / `effective concurrency: 3 (runtime limit)`. Every scheduling decision uses only `effectiveConcurrency`.

**Effective class limits.** After `effectiveConcurrency` is resolved:

```
effectiveClassMax       = min(configuredMaxActive, effectiveConcurrency)
                          (effectiveConcurrency when the class omits maxActive)
effectiveClassPreferred = min(configuredPreferredActive, effectiveClassMax)
                          (effectiveClassMax when preferredActive is omitted)
```

So `--concurrency 1` → deep preferred/max 1/1, `--concurrency 2` → 1/2, `--concurrency 3` → 1/2. Every integer `1..12` is a valid concurrency.

**Resolve every lens** to:

```
{lens, bundle, priority, configuredModelClass, requestedModel, resolvedModel, concurrencyClass, attempt}
```

`bundle` is `{bundles_dir}/lens-NN-bundle.md` — same lens id, zero-padded.

1. `requestedModel` = the lens's direct `model` override → else its configured `modelClass`'s model → else the `defaultModelClass` model. A lens with no assignment takes `defaultModelClass` (`fast` → GLM-5.3-Flash under the ZCode profile — never default an unspecified lens to the deep class).
2. `resolvedModel` = `requestedModel`, changed only by an applied fallback.
3. `concurrencyClass` is derived from the model that will actually run: if `resolvedModel` is the `model` of a configured model class, use that class; otherwise the lens's configured `modelClass`. A lens configured `fast` but pinned `"model": "GLM-5.3"` resolves `concurrencyClass = deep` and counts against the Deep cap. A direct model override cannot bypass a model-class cap. Runtime spawn IDs may differ from these friendly names; keep the logical model identity separate from the runtime model ID.

**Scheduler mode** — pick exactly one:

- `ROLLING`: the runtime exposes independent subagents AND per-worker completion events (or wait-for-any). Any one finish → refill its slot immediately.
- `BOUNDED_BATCH`: the runtime can run N concurrent subagents but only returns control after a foreground group finishes. Dispatch at most `effectiveConcurrency` per group — same fill rules, model-aware batch composition, lenses still independent. Log once: `Runtime does not expose independent completion events; using bounded-batch mode with effective concurrency N.` Never claim rolling refill in this mode.
- `SEQUENTIAL`: no subagent support at all. See the sequential fallback below.

**Log the resolved summary** (resolved models, not original classes):

```
Audit scheduler:
- mode: rolling
- requested/effective concurrency: 4/4
- GLM-5.3 preferred/max: 1/2
- GLM-5.3 lenses: 1, 2, 3, 4, 7, 12
- GLM-5.3-Flash lenses: 5, 6, 8, 9, 10, 11
- config: defaults + ZCode profile
```

Append `+ explicit --config` to the config line when `--config` was given. Do not log credentials, API keys, or unrelated runtime settings.

**Model fallback, admission failures, rate limits.**

- Model unavailable / quota / admission failure BEFORE the worker starts: apply the configured fallback chain once per lens — direct `model` override → the lens's model class model → the `defaultModelClass` model → the runtime's inherited/default model — then recompute `resolvedModel` AND `concurrencyClass`, keep the lens PENDING, and do not increment its execution attempt. Never re-submit the same rejected model. If a fallback changes the model actually used, log it once for that lens (for example `Lens 4 requested GLM-5.3 but it is unavailable; falling back to GLM-5.3-Flash.`). If no usable fallback exists: mark the lens FAILED with a concise diagnostic and block Turn 6. Do not loop. Never add sleep/poll loops.
- Rate-limit or quota errors during execution follow the same once-only rule: fall back to another model once if one is available, otherwise mark the lens FAILED and block Turn 6.
- If your runtime cannot select a per-subagent model at all: keep the configured concurrency behavior, run every worker on the runtime's default model (all lenses count against the global limit only), log one line that per-lens model routing was unavailable, and still run all 12 lenses. Never abort the audit over model routing.

## Turn 4 — lens pool

The 12 lenses run as independent subagents through a bounded-concurrency pool at `effectiveConcurrency`. Not one fixed wave of 12.

**Scheduler state.** Four mutually exclusive states. Every lens sits in exactly one. Their union is always {1..12}.

```
pending:   all 12 lens records from Turn 1b (attempt = 1)
active:    []
completed: []
failed:    []
```

A retry is a PENDING lens with `attempt > 1`. Do not keep a separate retry collection. `MAX_LENS_ATTEMPTS = 2` (the initial run plus one retry).

**Fill rules.** Run them at the start and after every freed slot, using only `effectiveConcurrency`, `effectiveClassPreferred`, `effectiveClassMax`, and each lens's `concurrencyClass`:

While `active` has fewer than `effectiveConcurrency` workers and an eligible lens is pending:

1. Eligibility: spawning the lens would not push its `concurrencyClass` over `effectiveClassMax` (classes without a configured cap are limited only globally).
2. Preference: while eligible work remains in any other class, keep each capped class at or below `effectiveClassPreferred`. Do not start a second worker of a capped class merely because it is next in line when eligible work exists elsewhere. Once the other classes have no pending work, the capped class may fill up to its hard cap.
3. Within the chosen class: highest-priority pending lens, ties → lower lens number.

Never exceed `effectiveConcurrency`. With the defaults (effective 4, deep hard max 2) at most two of the four slots are deep. The invariant `active(GLM-5.3) <= 2` holds regardless of per-lens overrides.

Default initial spawn: lens 1 `deep` + lenses 9, 6, 11 `fast`.

Spawn each worker with `references/lens-worker-prompt.md`, passing that lens's `resolvedModel` when the runtime supports per-subagent model selection (on ZCode, the `0xsimao-deep` / `0xsimao-fast` workers). If it does not, apply the no-routing fallback above. In ROLLING mode run workers in the background and act on completion notifications. Do not poll or sleep. In BOUNDED_BATCH mode dispatch one group of at most `effectiveConcurrency`, wait for the whole group, then compose the next group.

**Completion events.** Act on each the moment it arrives (ROLLING; BOUNDED_BATCH applies the same rules per group):

- **Success.** A lens is COMPLETED only after `{findings_dir}/lens-NN.md` exists, is non-empty, and a shell check (not a file read) shows it contains a `FINDING` line, a `LEAD` line, or a `NO_SURFACE:` line. Log the count. Do not read the findings file, the notes file, or the worker return into context. Then `ACTIVE → COMPLETED`, then re-run the fill rules. The worker's return is a short status line. If it is longer, do not paste it into the log; log `lens NN return truncated for log` and keep going.
- **Execution failure** — the worker started and then crashed, was cancelled, or the shell check failed: it consumes an attempt. Delete a partial `lens-NN.md` from this attempt (a `.tmp` left behind too). Attempts remaining → `ACTIVE → PENDING`, `attempt += 1`. The retry obeys both the global cap and the class caps. No attempts remaining → `ACTIVE → FAILED`, and Turn 6 is blocked. A failed attempt must not leave a `findings/lens-NN.md` that could be mistaken for a completed lens. On a retry, the worker renames onto `lens-NN.md` only after its new text is complete.
- **Admission/concurrency refusal** — the runtime rejects the spawn so the lens never started: the lens stays PENDING with its attempt unchanged, and `effectiveConcurrency` drops to `min(effectiveConcurrency, currently active)` for the rest of the run unless the runtime later proves more capacity. Do not immediately re-attempt the identical spawn and do not sleep. With the reduced limit, the fill rules stop offering that slot.
- **Model unavailable before start:** apply the fallback once, recompute `resolvedModel` and `concurrencyClass`, keep the lens PENDING with its attempt unchanged.

Two completions near-simultaneously: process both, then fill both freed slots.

The 12 lenses stay independent. Each sees only its own bundle, the source its scope-map section names, and its own prompt — never another lens's findings, notes, summary, or dedup state. A later-starting lens gets exactly what a first-wave lens gets.

**Who writes the file.** The worker writes `{findings_dir}/lens-NN.md` and `{findings_dir}/lens-NN.notes.md`. The orchestrator does not rewrite a file that already passed the shell check. If the worker runtime has no write tool, the worker returns the full FINDING/LEAD text once (evidence uncut) and the orchestrator is the one that stages it to `lens-NN.md.tmp` and renames it after the same usability check. That fallback is one transfer. Do not ask the worker to also send a summary that repeats the body.

**Sequential fallback.** If the runtime cannot spawn subagents, run the 12 lenses yourself, one at a time (concurrency 1). For each lens: read its bundle, read source only as its Lens reads section says, and do not re-open `source.md` or a file this context already read. Write that lens's findings file before starting the next lens. Do not paste the previous lens's findings into the next pass. Slower, and weaker because the passes are no longer blind to each other, but the method survives. Never collapse the 12 lenses into a single pass.

**Marker check.** After a success, a shell count of marker lines in the notes file is enough. Log `lens NN markers=<n>`. Do not read the notes file. Do not retry a lens only because the marker count is low — a retry repeats the source read. The findings file remains the completion artifact.

## Turn 5 — drain barrier

Turn 6 may begin only when:

```
completed = exactly lenses {1..12}
active    = empty
pending   = empty
failed    = empty
```

and every `{findings_dir}/lens-01.md` … `lens-12.md` exists and passed the success check.

If `failed` is non-empty: stop. Log the failed lens ids, attempt counts, and reasons. Keep the workspace. Return a short note that the audit is incomplete. Do not write a report or a findings index. Never proceed with 11/12.

A lens that is COMPLETED in scheduler state but whose findings file is missing is incomplete: persist it if the worker already returned a body, otherwise re-run the lens. Do not enter Turn 6.

While any condition is false, do not start dedup. Return to Turn 4.
