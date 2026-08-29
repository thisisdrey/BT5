# Q1399: filter-out-debt-asset via borrow: turn an accounting residue into a permanently unclosable p

## Question
`filter-out-debt-asset` (mainnet/contracts/market/v0-4-market.clar:633) rebuilds the debt list without one asset, under `as-max-len? ... u64`. Can an unprivileged caller of `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), by choosing `receiver`, including a contract principal, use that to turn an accounting residue into a permanently unclosable position, violating the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction and producing direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:633` -> `filter-out-debt-asset`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `filter-out-debt-asset` rebuilds the debt list without one asset, under `as-max-len? ... u64`. Reach it through `borrow` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `borrow` with `receiver`, including a contract principal, then read `filter-out-debt-asset` state before and after in the same block and assert the two sides of the invariant are equal.
