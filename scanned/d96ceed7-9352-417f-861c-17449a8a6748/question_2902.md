# Q2902: calc-treasury-lp-preview via transfer: compose two individually correct mechanisms into an incorr

## Question
Entering through `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) while controlling the timing relative to a pledge or a liquidation, can an unprivileged attacker make `calc-treasury-lp-preview` (mainnet/contracts/vault/v0-vault-stx.clar:350) compose two individually correct mechanisms into an incorrect result? `calc-treasury-lp-preview` divides by `(- ta-preview reserve-inc)`, a denominator that can reach zero or underflow, so the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims would fail, yielding direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:350` -> `calc-treasury-lp-preview`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the timing relative to a pledge or a liquidation
- Exploit idea: `calc-treasury-lp-preview` divides by `(- ta-preview reserve-inc)`, a denominator that can reach zero or underflow. Reach it through `transfer` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `transfer` with the timing relative to a pledge or a liquidation, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
