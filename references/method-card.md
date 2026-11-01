# Method card

How to think. Your lens file is what to look for. The money map is the protocol model — start there, not at the file list. Verbatim finding titles for your surfaces live in the lens file; use them. This card is the procedure. It does not replace the lens.

## Thesis

Value moves in sequences (deposit, accrue, borrow, liquidate, withdraw). Drift is any way tracked state stops describing reality:

| Drift | Shape |
| --- | --- |
| Missing write | value moved and nothing recorded it, or one branch updates and the sibling does not |
| Wrong write | wrong amount, direction, rounding, units, or decimals |
| Mistimed write | right value at the wrong moment, so a snapshot is taken against state that is about to change |
| Wrong party | the total is intact; the split between cohorts is not |
| Untrustworthy input | a number propagated faithfully from an oracle, a live balance, or a caller-controlled parameter |
| Unreachable state | accounting is correct but a revert, pause, unbounded loop, or missing path means it can never be settled |

A defect nobody loses money from is a curiosity. Every candidate must answer: **who is left holding the loss?** Name a class of actor (last withdrawer, solvent lenders, the cohort that held through accrual, the account a forged message was drawn against). If the only person worse off is the caller, there is no finding.

Accounting drift is the largest class, not the only one. DoS, stuck funds, access and signature defects, token assumptions, oracle and parameter validation, and cross-chain state are real and are what your lens file owns. Do not force a non-accounting bug into the drift frame. When the target has little accounting, keep the map short and work the lens on its own terms.

## Phase 0 — Vocabulary

Before hunting, state in plain English: what a user gives and what claim they receive; what the claim is worth and which variables set it; where yield comes from and who is entitled; who absorbs a shortfall. A fuzzy spot is where the bug is. Mark it in the notes file and come back.

## Phase 1 — Use the money map

The orchestrator already built the map. Use its asymmetry table (moved without tracking, tracked in one branch only, never tracked, live `balanceOf` used as an accounting source). Do not rebuild the map from scratch. If a section your lens needs is missing or contradicts the code you read, say so in a LEAD and follow the code.

## Phase 2 — Invariants

Break one of these with a sequence, or show that a documented invariant is not enforced:

- `Σ(all user withdrawable) <= actual token balance`
- `totalX == Σ userX` for every aggregate with a per-user counterpart
- every credited unit is debited exactly once, in exactly one place
- indices, cumulative rates, and timestamps never go backwards and cannot be advanced for free
- a user's claim cannot increase from an action that costs them nothing
- state on chain A and chain B agree after every message settles

## Phase 3 — Lifecycles, not functions

After each step, write which accounting variables changed and which should have. Run:

1. deposit → accrue yield → withdraw
2. deposit → borrow → price moves → liquidate → withdraw
3. stake → rewards distributed → unstake → claim
4. deposit → withdraw → deposit again (re-entry into the same slot)
5. open → adjust → close (adjust must keep every invariant open established)
6. source-chain send → dest-chain receive → failure/retry
7. the same sequence at expiry, after maturity, and after the vault has ended

At every step ask: what did we just change that something else still assumes the old value of?

## Phase 4 — Cohorts and time

On every distribution, reward, fee, yield, and profit path:

1. Can someone join immediately before a distribution and take value they did not earn?
2. Can someone leave immediately before a loss and push it onto whoever stays?
3. **Last User Out.** Every user withdraws, worst order. Does the last one get paid in full? If the protocol is short by anything, the leak is upstream — find that step.

For cumulative-rate and index accounting: can the index advance without cost, and is the user's snapshot taken at the right moment?

## Phase 5 — Saturate

Follow the saturation rule in `shared-rules.md`. A repeat instance you missed is an audit failure. Escalate a DoS that conceals theft, and a rounding error that loops into a drain, before you write the finding.

## Phase 6 — Prove it

Separate internal pre-conditions (protocol state) from external ones (market, oracle, chain). `None` is the strongest case — do not pad. The attack path is numbered, with actors and amounts. Impact names the victim class. If you cannot fill the path with real numbers, emit a LEAD. Output shape is `shared-rules.md`. Do not copy the reasoning markers into the findings file.

## Phase 7 — Minimal mitigation

One change, the smallest that removes the defect, in the protocol's own terms: the operation to add, remove, or reorder. Not "refactor the accounting" and not "consider adding validation."

## Habits

- Root cause is a missing or wrong operation at a named line. Point at the absence.
- Judge what the code allows, never what the deployer probably intends.
- When a path looks clean, name three concrete ways to defeat it (addresses, values, states) before accepting it.
