# Scope rules

Read this once, in Turn 2, together with `scope-rules.json`. The generated `{work_dir}/scope-map.md` is the only file workers use to choose source. Do not paste file bodies into the scope map.

## Why some lenses still read the whole tree

Detection coverage is unchanged. These lenses read `{work_dir}/source.md` once because a skip is a miss:

| Lens | Why the whole tree |
| --- | --- |
| 1 accounting-desync | Absent writes and one-sided branches are defined by what is *not* there. The money map points; the code confirms every total. |
| 6 rounding-precision | A division or cast can sit in any arithmetic, including a helper the map does not call a "total". |
| 9 access-trust | The bug is often a missing modifier or an unchecked target. Absence has no tag. |
| 11 edge-states | The lens checklist is every stateful function, including empty, first, and last. |
| 12 flow-completeness | Inverse and sibling symmetry only show up when every paired function is visible. |

The other lenses read the primary set below. They still read any file the money map, their lens surfaces, or a saturation hit names, even if it was not listed. They do not speculatively read the rest of the tree.

## Tags

Assign tags only for patterns you actually saw while reading the file for the money map. Vocabulary is `scope-rules.json` `tagVocabulary`.

Also set two booleans on every fully read file: `loop` (any loop, including over a storage array or a batch) and `external_call` (any call to another contract, token, or oracle). A missing boolean, or a file you did not finish, means `uncertain: true`. Uncertain files are primary for every lens.

Tag `lifecycle` on every file the money map names. Tag `liquidation`, `cross-chain`, `async`, `shares`, `rewards`, `ordering`, `grief`, or `integration` when the map's own sections cite that file for that topic. A lifecycle step named liquidate, send, or callback must tag the files that implement it. Do not leave a cited file untagged.

## Primary set

From `scope-rules.json`:

- Lenses in `fullSourceLenses` read `source.md` (every in-scope file). Their scope-map section says `FULL`.
- Any `uncertain` file is listed under every lens.
- Otherwise a narrow lens's files are those whose tags intersect `narrowTags`.
- An empty primary list is valid. The worker reads the money map and the lens, writes `NO_SURFACE` when nothing in the map or the lens applies, and does not open the tree.

## Scope map shape

Write `{work_dir}/scope-map.md` with:

1. One block per in-scope file: path, line count, contracts, function names (no bodies), storage variables written, tags, `loop`, `external_call`, `uncertain`.
2. A `## Lens reads` section. For each lens `01`–`12`: either `FULL source.md` or `PRIMARY` followed by the path list (or `(none)`).

Function names are the saturation index. Workers search this index for sibling names (`withdraw`, `redeem`, `emergencyWithdraw`, `claim`, expiry and cross-chain variants, and the inverses in the lens). A hit promotes that file to "read in full" even when it was not primary.

## source.md

Write `{work_dir}/source.md` once, with a shell `cat`: every in-scope `.sol` file and in-scope doc, each under a `### path` header and a fence. Do not read `source.md` back. Do not concatenate it into lens bundles.
