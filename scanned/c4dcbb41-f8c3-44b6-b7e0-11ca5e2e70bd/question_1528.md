# Q1528: mask-to-list-iter via borrow: make an aggregate and its per-item breakdown disagree

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the future mask produced by the new debt bit reach `mask-to-list-iter` (mainnet/contracts/market/v0-4-market.clar:440) in a state where it make an aggregate and its per-item breakdown disagree? Given that it appends under `(unwrap-panic (as-max-len? ... u64))`, aborting if the position exceeds the bound, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:440` -> `mask-to-list-iter`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the future mask produced by the new debt bit
- Exploit idea: `mask-to-list-iter` appends under `(unwrap-panic (as-max-len? ... u64))`, aborting if the position exceeds the bound. Reach it through `borrow` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `borrow` with the future mask produced by the new debt bit, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
