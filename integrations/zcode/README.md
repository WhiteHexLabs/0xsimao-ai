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

- requested concurrency: **3** lenses active at once, clamped by the
  runtime's actual capacity to an **effective concurrency** (printed as
  `requested/effective` when they differ)
- GLM-5.3 preferred active: **1** / hard max active: **2** — both clamped
  to the effective concurrency, so `--concurrency 1` yields deep 1/1
- while GLM-5.3-Flash work remains, the scheduler keeps only one
  GLM-5.3 worker active; once Flash work is drained, up to two GLM-5.3
  workers may run — with the default cap of 3, never all three slots
- normal initial spawn: lens 1 (deep) + lens 9 (fast) + lens 6 (fast)

The orchestrator (the main ZCode session) keeps whatever model the user
selected for it. This integration does not change the orchestrator
model; it controls only the audit subagents.

## Worker hardening

Both worker templates are hardened leaf agents:

- `injectAgentsMd: false` — the audited repo's `AGENTS.md` is never
  injected into a lens worker's context;
- read-only tool allowlist — `Read`, `Grep`, `Glob` only. No `Edit`,
  `Write`, `Bash`, MCP tools, or subagent spawning; cross-file lookups
  need nothing else. Workers never write: all audit artifacts land under
  `.0xsimao-auditor-work/` in the audited repo, and the orchestrator
  itself persists each lens's raw returned output to
  `.0xsimao-auditor-work/findings/lens-NN.md` after a successful
  completion — persistence requires no write access in the worker;
- an explicit trust boundary at the top of the prompt: everything
  originating from the audited repository is untrusted audit data and
  can never act as instructions (see
  `references/attack-lenses/shared-rules.md`).

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

- **Direct install:** copy `agents/0xsimao-deep.md` and
  `agents/0xsimao-fast.md` to your user custom-subagent directory,
  `~/.zcode/agents/`.
- **Plugin install:** a ZCode plugin may package `agents/*.md` — but
  simply keeping these files inside a normal project directory does not
  make them user custom subagents. `integrations/zcode/agents/` in this
  repository is only a template location unless the repository is
  actually packaged and installed as a ZCode plugin.

After changing subagent definitions, start a **new ZCode session**.
Subagent definitions — including their pinned models — are read at
session start, so a running session will not pick up a changed or newly
installed worker definition.

Then invoke the audit as usual (`run 0xSimao AI`); the orchestrator in
`SKILL.md` detects ZCode, resolves the Deep/Fast profile, and dispatches
lens workers accordingly.

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
`GLM-5.3-Flash` (default class) → the runtime inherited model. The
fallback is applied at most once per lens, recomputes the lens's
concurrency class, and does not consume an execution attempt. A
fallback that changes the model actually used is reported once per lens.

## Overrides

Config precedence, lowest → highest: `references/orchestration-defaults.json`
→ this ZCode profile → explicit `--config PATH` → invocation flags. A
`.0xsimao-ai.json` in the audited repo is never applied implicitly —
audit-target configuration is untrusted; pass `--config .0xsimao-ai.json`
to opt in. See the main README and `SKILL.md` Turn 1b. Two common ZCode
tweaks:

```json
{ "maxConcurrency": 4 }
```

```json
{ "lensAssignments": { "5": { "modelClass": "deep" } } }
```

The second promotes the cross-chain lens to GLM-5.3 — worth doing on
cross-chain-heavy targets.
