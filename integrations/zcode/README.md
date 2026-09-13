# ZCode integration

Runtime adapter for running the 0xSimao AI audit skill under ZCode. The
core skill in `SKILL.md` stays runtime-agnostic; this directory holds the
ZCode-specific pieces only.

## What this provides

ZCode can run custom subagents pinned to a specific model, so the audit's
model-aware scheduler can actually route different lenses to different
models. Instead of twelve near-identical agent definitions, there are two
reusable workers that differ **only by model** — never by audit
methodology:

| Worker | Model | Default lenses |
|---|---|---|
| `0xsimao-deep` | GLM-5.3 | 1, 2, 3, 4, 7, 12 |
| `0xsimao-fast` | GLM-5.3-Flash | 5, 6, 8, 9, 10, 11 |

Both workers carry the same contract: read the one lens bundle whose path
the parent supplies, follow the lens/method/shared rules inside it, never
read another lens's output, never spawn child subagents, and return only
their own findings/leads block.

The default model for any lens without an assignment is
**GLM-5.3-Flash** (`defaultModelClass: "fast"` in
`references/orchestration-defaults.json`). Unknown or newly added lenses
must not silently land on GLM-5.3.

## Default scheduling profile

From `references/orchestration-defaults.json`:

- max concurrency: **3** lenses active at once
- GLM-5.3 preferred active: **1** / hard max active: **2**
- while GLM-5.3-Flash work remains, the scheduler keeps only one
  GLM-5.3 worker active; once Flash work is drained, up to two GLM-5.3
  workers may run — with the default cap of 3, never all three slots
- normal initial spawn: lens 1 (deep) + lens 9 (fast) + lens 6 (fast)

The orchestrator (the main ZCode session) keeps whatever model the user
selected for it. This integration does not change the orchestrator model.

## Model IDs

The worker templates pin `GLM-5.3` and `GLM-5.3-Flash`. If your ZCode
build expects a different canonical model ID for these (for example a
lowercase or account-qualified form), edit the `model:` field in both
agent files to the ID your runtime reports — nothing else in the
templates needs to change. The generic config in
`references/orchestration-defaults.json` uses the friendly aliases
(`deep` → `GLM-5.3`, `fast` → `GLM-5.3-Flash`); ZCode resolves them to
its own runtime IDs at spawn time.

## Installation

ZCode loads custom subagents from the user's subagent configuration /
plugin mechanism — it does not read agent definitions out of this
repository. Installing the two workers is therefore an **explicit user
action**; nothing here writes outside this repo.

1. Copy `agents/0xsimao-deep.md` and `agents/0xsimao-fast.md` into the
   directory your ZCode loads custom subagents from (for a user-level
   install, your ZCode agents directory such as `~/.zcode/agents/`; a
   plugin or project-level location works the same way).
2. Start a **new ZCode session**. Subagent definitions — including their
   pinned models — are read at session start, so a running session will
   not pick up a changed or newly installed worker definition.
3. Invoke the audit as usual (`run 0xSimao AI`); the orchestrator in
   `SKILL.md` detects ZCode, resolves the Deep/Fast profile, and
   dispatches lens workers accordingly.

If your ZCode build supports a per-agent tool allowlist, you may
restrict the workers by excluding only the subagent-spawning tool. Leave
read, search, and shell tools available — the lenses need them for
cross-file lookups.

## If model routing is unavailable

If your ZCode build cannot select a per-subagent model:

- all 12 lenses still run, with independent contexts;
- the configured concurrency limits still apply;
- every worker falls back to the runtime's default/inherited model;
- the orchestrator prints one concise notice that per-lens model routing
  was unavailable.

The audit is never aborted solely because model routing is unsupported.
Per-lens fallback order when a configured model is unavailable at spawn
time: the lens's direct `model` override → its model class's model →
`GLM-5.3-Flash` (default class) → the runtime inherited model. A
fallback that changes the model actually used is reported once per lens.

## Overrides

See the main README and `SKILL.md` Turn 1b for the override chain
(flags → `--config` file → `.0xsimao-ai.json` in the audited repo → this
ZCode profile → `references/orchestration-defaults.json`). Two common
ZCode tweaks:

```json
{ "maxConcurrency": 4 }
```

```json
{ "lensAssignments": { "5": { "modelClass": "deep" } } }
```

The second promotes the cross-chain lens to GLM-5.3 — worth doing on
cross-chain-heavy targets.
