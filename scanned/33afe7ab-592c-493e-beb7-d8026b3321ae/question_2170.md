# Q2170: interest-rate via redeem: compose two individually correct mechanisms into an incorr

## Question
Entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) while controlling `min-out`, can an unprivileged attacker make `interest-rate` (mainnet/contracts/vault/v0-vault-stx.clar:371) compose two individually correct mechanisms into an incorrect result? `interest-rate` interpolates the packed curve at the current utilization, so the invariant that no position row exists that the position mask does not represent would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:371` -> `interest-rate`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `min-out`
- Exploit idea: `interest-rate` interpolates the packed curve at the current utilization. Reach it through `redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `redeem` with `min-out`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
