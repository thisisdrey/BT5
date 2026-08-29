# Q2551: convert-to-assets-preview via transfer: satisfy a bound with a value the bound was never designed 

## Question
`convert-to-assets-preview` (mainnet/contracts/vault/v0-vault-stx.clar:317) prices a redemption against `total-assets-preview` and `total-supply-preview`. Can an unprivileged caller of `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752), by choosing the timing relative to a pledge or a liquidation, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV and producing direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:317` -> `convert-to-assets-preview`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the timing relative to a pledge or a liquidation
- Exploit idea: `convert-to-assets-preview` prices a redemption against `total-assets-preview` and `total-supply-preview`. Reach it through `transfer` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `transfer` with the timing relative to a pledge or a liquidation, then read `convert-to-assets-preview` state before and after in the same block and assert the two sides of the invariant are equal.
