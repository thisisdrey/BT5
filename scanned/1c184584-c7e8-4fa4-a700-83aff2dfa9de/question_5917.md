# Q5917: total-debt via supply-collateral-add: satisfy a bound with a value the bound was never designed 

## Question
Can an unprivileged attacker entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), controlling the `ft` trait principal deciding which vault is routed to, drive `total-debt` (mainnet/contracts/vault/v0-vault-stx.clar:328) — which computes cumulative debt from `principal-scaled` and `index` — to satisfy a bound with a value the bound was never designed to admit, breaking the invariant that conversions never round in the user's favour in either direction, and cause direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:328` -> `total-debt`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal deciding which vault is routed to
- Exploit idea: `total-debt` computes cumulative debt from `principal-scaled` and `index`. Reach it through `supply-collateral-add` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `supply-collateral-add` with the `ft` trait principal deciding which vault is routed to, then read `total-debt` state before and after in the same block and assert the two sides of the invariant are equal.
