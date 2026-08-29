# Q1690: send-underlying via transfer: make a health check read a different position than the one

## Question
Entering through `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) while controlling `amount`, can an unprivileged attacker make `send-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:296) make a health check read a different position than the one that will exist? `send-underlying` pushes the underlying under an `as-contract?` post-condition scope, so the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV would fail, yielding theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:296` -> `send-underlying`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `send-underlying` pushes the underlying under an `as-contract?` post-condition scope. Reach it through `transfer` and make a health check read a different position than the one that will exist.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `transfer` with `amount`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
