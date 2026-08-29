# Q2602: zip via collateral-remove: compose two individually correct mechanisms into an incorr

## Question
Entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) while controlling the set of assets held, can an unprivileged attacker make `zip` (mainnet/contracts/vault/v0-vault-stx.clar:226) compose two individually correct mechanisms into an incorrect result? `zip` pairs the utilization and rate point lists element by element, so the invariant that no position row exists that the position mask does not represent would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:226` -> `zip`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `zip` pairs the utilization and rate point lists element by element. Reach it through `collateral-remove` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `collateral-remove` with the set of assets held, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
