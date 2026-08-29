# Q1450: convert-to-shares-preview via deposit: make a health check read a different position than the one

## Question
Entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) while controlling `amount`, can an unprivileged attacker make `convert-to-shares-preview` (mainnet/contracts/vault/v0-vault-stx.clar:308) make a health check read a different position than the one that will exist? `convert-to-shares-preview` returns u0 outright when `total-assets-preview` is non-zero and supply is zero, minting nothing for a real deposit, so the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:308` -> `convert-to-shares-preview`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `convert-to-shares-preview` returns u0 outright when `total-assets-preview` is non-zero and supply is zero, minting nothing for a real deposit. Reach it through `deposit` and make a health check read a different position than the one that will exist.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `deposit` with `amount`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
