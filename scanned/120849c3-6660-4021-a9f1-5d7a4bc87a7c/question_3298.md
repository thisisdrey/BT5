# Q3298: unwrap-status via borrow: turn an accounting residue into a permanently unclosable p

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling `amount`, can an unprivileged attacker make `unwrap-status` (mainnet/contracts/registry/v0-assets.clar:111) turn an accounting residue into a permanently unclosable position? `unwrap-status` resolves `status` with `unwrap-panic`, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:111` -> `unwrap-status`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `unwrap-status` resolves `status` with `unwrap-panic`. Reach it through `borrow` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `borrow` with `amount`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
