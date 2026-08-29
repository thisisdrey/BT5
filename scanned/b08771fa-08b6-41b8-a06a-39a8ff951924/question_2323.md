# Q2323: convert-to-assets-preview via redeem: satisfy a bound with a value the bound was never designed 

## Question
`convert-to-assets-preview` (mainnet/contracts/vault/v0-vault-stx.clar:317) prices a redemption against `total-assets-preview` and `total-supply-preview`. Can an unprivileged caller of `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797), by choosing the gap between the `assets` var and the real balance, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that only the acting principal's own position is mutated and producing direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:317` -> `convert-to-assets-preview`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: the gap between the `assets` var and the real balance
- Exploit idea: `convert-to-assets-preview` prices a redemption against `total-assets-preview` and `total-supply-preview`. Reach it through `redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `redeem` with the gap between the `assets` var and the real balance, then read `convert-to-assets-preview` state before and after in the same block and assert the two sides of the invariant are equal.
