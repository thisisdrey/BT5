# Q0070: unwrap-status via collateral-add: convert a rounding direction into a repeatable extraction

## Question
Entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) while controlling the position's existing collateral and debt composition, can an unprivileged attacker make `unwrap-status` (mainnet/contracts/registry/v0-assets.clar:111) convert a rounding direction into a repeatable extraction? `unwrap-status` resolves `status` with `unwrap-panic`, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:111` -> `unwrap-status`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the position's existing collateral and debt composition
- Exploit idea: `unwrap-status` resolves `status` with `unwrap-panic`. Reach it through `collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `collateral-add` with the position's existing collateral and debt composition, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
