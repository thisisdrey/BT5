# Q1258: receive-underlying via redeem: make a health check read a different position than the one

## Question
Entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) while controlling the vault's available liquidity relative to the redemption, can an unprivileged attacker make `receive-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:291) make a health check read a different position than the one that will exist? `receive-underlying` pulls the underlying from a named account, so the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV would fail, yielding theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:291` -> `receive-underlying`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: the vault's available liquidity relative to the redemption
- Exploit idea: `receive-underlying` pulls the underlying from a named account. Reach it through `redeem` and make a health check read a different position than the one that will exist.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `redeem` with the vault's available liquidity relative to the redemption, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
