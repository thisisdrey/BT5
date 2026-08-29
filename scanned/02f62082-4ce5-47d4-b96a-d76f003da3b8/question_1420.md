# Q1420: increment via supply-collateral-add: make an aggregate and its per-item breakdown disagree

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls `amount` reach `increment` (mainnet/contracts/market/v0-market-vault.clar:137) in a state where it make an aggregate and its per-item breakdown disagree? Given that it advances the user-id nonce, the invariant that conversions never round in the user's favour in either direction breaks and the result is theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:137` -> `increment`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `increment` advances the user-id nonce. Reach it through `supply-collateral-add` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `supply-collateral-add` with `amount`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
