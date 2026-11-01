# Dedup, judge, report

Turn 6 only. Read this file, `severity-calibration.md`, `report-formatting.md`, and `findings-index.md` now. Do not read `source.md` again. Do not re-read in-scope source to re-verify a finding. The lenses did that.

Read the twelve `{findings_dir}/lens-NN.md` files. This is the one time their bodies enter the orchestrator context. Do not read the notes files.

Do not print an intermediate dedup list. Log one completeness line, then write the report and the index.

## 1. Dedup

Parse every FINDING and LEAD. Group by `group_key` (`Contract | function | bug-class`). Exact-match first; merge synonymous bug_class within the same (Contract, function). Keep the best per group, number sequentially, annotate `[lenses: N]`.

**Function isolation (hard).** Never merge across different `function:` fields. Different function = different bug.

**Mechanism preservation.** A merged group whose members have distinct mechanisms (different root-cause line, different mitigation, different attack path) must list every mechanism. The same function routinely carries several coexisting accounting bugs. Dropping one because a sibling merged over it is a failed dedup.

**Mitigation preservation.** Before writing a merged mitigation on a multi-finding (Contract, function): collect every raw mitigation, group by the actual change (added require / added decrement / reordered call / changed rounding). Two mitigations are distinct if they change different operations or different directions. Two or more distinct → present as Option A, B… verbatim from the lens text, labelled by kind (add-missing-write / validate / reorder / round-other-way / restrict).

**Completeness gate.** List every unique (Contract, function) that appears in any raw FINDING or LEAD. Every one must have at least one item in the final report or the unverified-leads section. Log: `Completeness: N unique (Contract, function) in raw, N covered in final.`

**Composite chains.** If A's output enables B's precondition and the combined impact exceeds either alone, add `Chain: [A] + [B]`. Report the chain as its own finding when it crosses a trust boundary. Most audits: 0–2.

## 2. Judge

Run each deduped finding through `severity-calibration.md`. Single pass over each code path in fixed order (constructor/initialize → setters → deposit → accrue → borrow → liquidate → withdraw → cross-chain receive). One-line verdict per gate: `BLOCKS` / `ALLOWS` / `IRRELEVANT` / `UNCERTAIN`. `UNCERTAIN = ALLOWS`. Commit. Do not revisit. Keep the gate lines in `run.log`, not in the report and not in the return note.

## 3. Promote or reject leads

LEAD → FINDING if the full path exists in source (as already recorded in the lens file), or if `[lenses: 2+]` independently reached it. `[lenses: 2+]` does not override a code path that interrupts the attack before harm — demote instead. Judge what the code allows, never what the deployer probably intends.

## 4. Report and index

Format per `report-formatting.md`. Exclude rejected items. Write the complete report to `{work_dir}/report.md` — one file, inside the workspace, no second copy at the repository root. `--file-output` remains a deprecated no-op: if it was passed, log one deprecation line and do not write another copy.

Write `{work_dir}/findings-index.yaml` per `findings-index.md` when extraction is reliable. `run_id` and `step` come from the execution prompt. Root cause, mechanism, preconditions, impact, and the `primary-report` locator are the full text. Do not clip the list. If you cannot extract it reliably, write no index file.

## 5. Keep the workspace

Do not delete `report.md`, `money-map.md`, `scope-map.md`, `source.md`, `findings/`, `bundles/`, `poc/`, `findings-index.yaml`, or `run.log`.

Log:

```
Audit artifacts:
- report: .0xsimao-auditor-work/report.md
- findings index: .0xsimao-auditor-work/findings-index.yaml
- money map: .0xsimao-auditor-work/money-map.md
- scope map: .0xsimao-auditor-work/scope-map.md
- raw lens findings: .0xsimao-auditor-work/findings/
- lens bundles: .0xsimao-auditor-work/bundles/
- log: .0xsimao-auditor-work/run.log
- PoC workspace: .0xsimao-auditor-work/poc/
```

Then return the short note from `SKILL.md` and stop. Do not print the report.
