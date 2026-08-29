# Q5554: mask-to-list-internal via collateral-add: make an aggregate and its per-item breakdown disagree

## Question
Entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) while controlling the three `price-feeds` buffers and their order, can an unprivileged attacker make `mask-to-list-internal` (mainnet/contracts/market/v0-4-market.clar:435) make an aggregate and its per-item breakdown disagree? `mask-to-list-internal` expands mask bits into a list bounded at 64 entries, so the invariant that only the acting principal's own position is mutated would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:435` -> `mask-to-list-internal`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the three `price-feeds` buffers and their order
- Exploit idea: `mask-to-list-internal` expands mask bits into a list bounded at 64 entries. Reach it through `collateral-add` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `collateral-add` with the three `price-feeds` buffers and their order, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
